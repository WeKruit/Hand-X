"""Job executor — orchestrates a single job from claim to completion.

Takes a claimed job row, loads resume + credentials, detects the platform,
creates the browser-use agent, runs it, and reports results back to the
database and VALET.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import traceback
from typing import Any
from urllib.parse import urlparse

import structlog

from ghosthands.config.settings import settings
from ghosthands.integrations.credentials import (
    decrypt_credentials,
)
from ghosthands.integrations.database import Database
from ghosthands.integrations.resume_loader import load_resume
from ghosthands.integrations.valet_callback import ValetClient
from ghosthands.platforms import detect_platform as detect_platform_config
from ghosthands.worker.cost_tracker import (
    BudgetExceededError,
    CostTracker,
    StepLimitExceededError,
    resolve_quality_preset,
)
from ghosthands.worker.hitl import HITLManager

logger = structlog.get_logger()

# ── Platform detection ────────────────────────────────────────────────────

EXECUTOR_ONLY_PLATFORM_PATTERNS: dict[str, list[str]] = {
    "ashby": ["ashbyhq.com"],
    "icims": ["icims.com"],
}


def detect_platform(url: str) -> str:
    """Detect ATS platform from a job URL. Returns platform name or 'generic'."""
    shared_platform = detect_platform_config(url)
    if shared_platform != "generic":
        return shared_platform

    normalized = url.lower()
    for platform, patterns in EXECUTOR_ONLY_PLATFORM_PATTERNS.items():
        if any(pattern in normalized for pattern in patterns):
            return platform
    return "generic"


def validate_domain(url: str, allowed_domains: list[str]) -> bool:
    """Check if the URL's domain is in the allowed list."""
    try:
        hostname = urlparse(url).hostname
        if not hostname:
            return False
        return any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains)
    except Exception:
        return False


# ── Heartbeat task ────────────────────────────────────────────────────────

HEARTBEAT_INTERVAL = 30
HEARTBEAT_JITTER = 5


async def _heartbeat_loop(db: Database, job_id: str) -> None:
    """Background task that updates the heartbeat timestamp periodically.

    Runs until cancelled. Prevents the stuck-job recovery from reclaiming
    our job while it's still running. Includes random jitter to avoid
    thundering-herd heartbeats across multiple workers.
    """
    import random

    while True:
        try:
            await db.heartbeat(job_id)
        except Exception as exc:
            logger.warning("executor.heartbeat_failed", job_id=job_id, error=str(exc))
        interval = HEARTBEAT_INTERVAL + random.uniform(0, HEARTBEAT_JITTER)
        await asyncio.sleep(interval)


# ── Main executor ─────────────────────────────────────────────────────────


async def execute_job(
    job: dict[str, Any],
    db: Database,
    valet: ValetClient,
) -> dict[str, Any]:
    """Execute a single automation job end-to-end.

    Steps:
    1. Parse job metadata and validate
    2. Notify VALET that job is running
    3. Load user's resume profile
    4. Load and decrypt credentials (if needed)
    5. Detect ATS platform from target URL
    6. Validate domain is allowed
    7. Create browser-use agent with DomHand actions
    8. Run agent loop with cost tracking and heartbeats
    9. Process result
    10. Update DB and fire VALET completion callback
    11. Return result

    Args:
            job: Full job row from ``gh_automation_jobs``.
            db: Connected Database instance.
            valet: VALET callback client.

    Returns:
            Result dict with ``success``, ``summary``, cost info, etc.
    """
    job_id = str(job["id"])
    user_id = str(job.get("user_id", ""))
    target_url = job.get("target_url", "")
    job_type = job.get("job_type", "apply")
    input_data = job.get("input_data") or {}
    valet_task_id = job.get("valet_task_id")

    # Parse input_data if it's a string
    if isinstance(input_data, str):
        try:
            input_data = json.loads(input_data)
        except json.JSONDecodeError:
            input_data = {}

    log = logger.bind(job_id=job_id, job_type=job_type, user_id=user_id)
    log.info("executor.starting", target_url=target_url)

    # Initialize cost tracker
    quality = resolve_quality_preset(input_data)
    cost_tracker = CostTracker(
        job_id=job_id,
        max_budget=settings.max_budget_per_job,
        quality_preset=quality,
        job_type=job_type,
        max_steps=settings.max_steps_per_job,
    )

    # Initialize HITL manager
    hitl = HITLManager(db=db, valet=valet, worker_id=settings.worker_id)

    # Start heartbeat background task
    heartbeat_task = asyncio.create_task(_heartbeat_loop(db, job_id))

    try:
        # Step 1: Notify VALET we're running
        await valet.report_running(
            job_id=job_id,
            valet_task_id=valet_task_id,
            worker_id=settings.worker_id,
        )

        # Step 2: Load resume profile
        resume_profile: dict[str, Any] | None = None
        if user_id:
            try:
                resume_profile = await load_resume(db, user_id)
                log.info("executor.resume_loaded", name=resume_profile.get("full_name", ""))
            except ValueError as exc:
                log.warning("executor.no_resume", error=str(exc))
                # Resume is optional for some job types
                resume_profile = None

        # Step 3: Load and decrypt credentials
        credentials: dict[str, str] | None = None
        if user_id:
            try:
                cred_row = await db.load_credentials(job_id)
                if cred_row and cred_row.get("encrypted_secret"):
                    if settings.credential_encryption_key:
                        credentials = decrypt_credentials(
                            cred_row["encrypted_secret"],
                            settings.credential_encryption_key,
                        )
                        log.info("executor.credentials_decrypted", platform=cred_row.get("platform"))
                    else:
                        log.warning("executor.no_encryption_key")
            except Exception as exc:
                log.warning("executor.credential_load_failed", error=str(exc))

        # Step 3b: Generate credentials for account creation if none stored.
        # Uses the applicant's email + a generated password so the agent can
        # create an account on platforms that require login (e.g. Workday).
        if not credentials and resume_profile:
            applicant_email = resume_profile.get("email", "")
            if applicant_email:
                import secrets
                import string

                generated_pw = "".join(
                    secrets.choice(string.ascii_letters + string.digits + "!@#$%&") for _ in range(18)
                )
                credentials = {"email": applicant_email, "password": generated_pw}
                log.info("executor.credentials_generated", email=applicant_email)

        # Step 4: Detect platform and validate domain
        platform = detect_platform(target_url)
        log.info("executor.platform_detected", platform=platform)

        if not validate_domain(target_url, settings.allowed_domains):
            error_msg = f"Domain not allowed: {urlparse(target_url).hostname}"
            log.error("executor.domain_blocked", url=target_url)
            await _fail_job(
                db,
                valet,
                job_id,
                "domain_blocked",
                error_msg,
                valet_task_id=valet_task_id,
                cost_tracker=cost_tracker,
            )
            return {"success": False, "error": error_msg, "error_code": "domain_blocked"}

        # Step 5: Report progress — setup complete
        await valet.report_progress(
            job_id=job_id,
            step=1,
            total_steps=5,
            description="Setup complete, launching browser agent",
            valet_task_id=valet_task_id,
        )

        # Step 6: Create and run the browser-use agent
        # TODO: This is the integration point for browser-use.
        # When the agent module is ready, this will call:
        #   agent = create_agent(platform, target_url, resume_profile, credentials)
        #   result = await agent.run(max_steps=settings.max_steps_per_job)
        #
        # For now, we execute a placeholder that returns a structured result.
        result = await _run_agent(
            job_id=job_id,
            target_url=target_url,
            platform=platform,
            resume_profile=resume_profile,
            credentials=credentials,
            input_data=input_data,
            cost_tracker=cost_tracker,
            hitl=hitl,
            valet_task_id=valet_task_id,
            log=log,
        )

        # Step 7: Report progress — agent complete
        await valet.report_progress(
            job_id=job_id,
            step=4,
            total_steps=5,
            description="Application submitted, finalizing",
            valet_task_id=valet_task_id,
        )

        # Step 8: Write result to DB
        unified_cost = result.get("cost_summary")
        if not isinstance(unified_cost, dict) or not unified_cost:
            unified_cost = cost_tracker.get_summary()
        steps_taken = int(result.get("steps_taken") or unified_cost.get("step_count") or 0)
        result_data = {
            "success": result.get("success", True),
            "summary": result.get("summary", "Job completed"),
            "platform": platform,
            "steps_taken": steps_taken,
            "cost": unified_cost,
            **{k: v for k, v in result.items() if k not in ("success", "summary")},
        }

        await db.write_job_result(job_id, result_data)
        await db.write_job_event(job_id, "completed", metadata={**unified_cost, "steps_taken": steps_taken})

        # Step 9: Fire VALET completion callback
        await valet.report_completion(
            job_id=job_id,
            success=True,
            result=result_data,
            valet_task_id=valet_task_id,
            worker_id=settings.worker_id,
            cost={
                "total_cost_usd": float(
                    unified_cost.get("total_tracked_cost_usd", unified_cost.get("total_cost_usd", 0.0)) or 0.0
                ),
                "action_count": steps_taken,
                "total_tokens": int(unified_cost.get("total_tracked_tokens", unified_cost.get("total_tokens", 0)) or 0),
            },
        )

        log.info(
            "executor.completed",
            success=True,
            steps=steps_taken,
            cost=float(unified_cost.get("total_tracked_cost_usd", unified_cost.get("total_cost_usd", 0.0)) or 0.0),
        )

        return result_data

    except BudgetExceededError as exc:
        log.warning("executor.budget_exceeded", cost=exc.snapshot.total_cost)
        error_data = {
            "error_code": "budget_exceeded",
            "error": str(exc),
            "cost": exc.snapshot.to_dict(),
        }
        await _fail_job(
            db,
            valet,
            job_id,
            "budget_exceeded",
            str(exc),
            valet_task_id=valet_task_id,
            cost_tracker=cost_tracker,
        )
        return {"success": False, **error_data}

    except StepLimitExceededError as exc:
        log.warning("executor.step_limit_exceeded", steps=exc.step_count, limit=exc.limit)
        error_data = {
            "error_code": "step_limit_exceeded",
            "error": str(exc),
        }
        await _fail_job(
            db,
            valet,
            job_id,
            "step_limit_exceeded",
            str(exc),
            valet_task_id=valet_task_id,
            cost_tracker=cost_tracker,
        )
        return {"success": False, **error_data}

    except asyncio.CancelledError:
        log.info("executor.cancelled")
        await db.update_job_status(job_id, "cancelled")
        raise

    except Exception as exc:
        log.error(
            "executor.unhandled_error",
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        await _fail_job(
            db,
            valet,
            job_id,
            "internal_error",
            str(exc),
            valet_task_id=valet_task_id,
            cost_tracker=cost_tracker,
        )
        return {"success": False, "error": str(exc), "error_code": "internal_error"}

    finally:
        # Always cancel heartbeat
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


# ── Internal helpers ──────────────────────────────────────────────────────


async def _run_agent(
    job_id: str,
    target_url: str,
    platform: str,
    resume_profile: dict[str, Any] | None,
    credentials: dict[str, str] | None,
    input_data: dict[str, Any],
    cost_tracker: CostTracker,
    hitl: HITLManager,
    valet_task_id: str | None,
    log: Any,
) -> dict[str, Any]:
    """Run the browser-use agent for the job.

    Creates a fully-configured agent via the factory, runs it against the
    target URL, and returns structured results.
    """
    # ── Set profile env var for domhand_fill ──────────────────────
    # domhand_fill reads GH_USER_PROFILE_PATH to generate field answers.
    # Write to temp file instead of env var to avoid /proc/pid/environ exposure.
    import stat
    import tempfile

    from ghosthands.agent.factory import run_job_agent
    from ghosthands.agent.prompts import (
        FAIL_OVER_CUSTOM_WIDGET,
        FAIL_OVER_NATIVE_SELECT,
        build_completion_detection_text,
    )

    profile = resume_profile or {}
    profile_fd, profile_path = tempfile.mkstemp(prefix="gh_profile_", suffix=".json")
    try:
        os.fchmod(profile_fd, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        os.write(profile_fd, json.dumps(profile, indent=2).encode())
        os.close(profile_fd)
        os.environ["GH_USER_PROFILE_PATH"] = profile_path
    except Exception:
        os.close(profile_fd)
        os.unlink(profile_path)
        raise

    # ── Build task prompt ────────────────────────────────────────
    resume_path = input_data.get("resume_path", "")
    if resume_path:
        os.environ["GH_RESUME_PATH"] = resume_path

    workday_start_flow_rules = ""
    if platform == "workday":
        workday_start_flow_rules = (
            "- If a start dialog offers a SAME-SITE option such as 'Autofill with Resume' or 'Apply with Resume', prefer that path over manual entry.\n"
            "- Do NOT choose external apply/import options such as LinkedIn, Indeed, Google, or other third-party account flows.\n"
            "- After uploading a resume on Workday, WAIT for the filename or a success message to appear and for the Continue button to become enabled before clicking it.\n"
            "- Do NOT upload a resume and click Continue in the same action batch.\n"
            "- Once Workday 'Autofill with Resume' / 'Apply with Resume' has been chosen and the resume/auth step succeeds, assume Workday is carrying that resume parse forward. On later pages, do NOT go searching again for another resume upload field unless the page visibly says the resume is missing or requires re-upload.\n"
            "- On Workday My Experience, if 'Type to Add Skills' is visible, treat it as a special searchable selectinput. Add ONE skill at a time from the FIRST 15 profile skills only: clear stale query text, type ONE skill, then wait 2-3 seconds for results. Skills are the exception where Enter is often required: press Enter ONLY if the SAME exact skill is visibly present in the suggestion list or an exact highlighted result is ready to commit. After Enter, wait another 2-3 seconds and VERIFY a chip/token for THAT SAME skill appears before treating it as done. If no chip appears, that skill is still unresolved. Then select/confirm that SAME profile skill if needed. Clear the query before the next skill. Never paste a comma-separated list of skills into that widget. If the exact profile skill returns 'No Items.' or has no exact result, you may try one or two deterministic SAME-SKILL aliases only (for example React -> ReactJS, ExpressJS -> Express, MySQLDB -> MySQL). If those same-skill aliases also miss, skip THAT SAME skill and continue. Do NOT substitute a different skill. If the skills widget is visible and still empty, resolve it BEFORE clicking 'Add Another' or expanding another repeater.\n"
            "- On Workday, if the stepper/current page says 'Review', STOP immediately and call done(success=True). Never scroll that page, never click 'Submit', and never click any button on Review.\n"
        )

    task = f"""Go to {target_url} and fill out the job application form completely.

CRITICAL — Action Order:
1. If a popup, modal, interstitial, or newsletter prompt is visibly blocking the form, call domhand_close_popup first. Use Escape or coordinate clicks only if domhand_close_popup fails.
2. After navigating to the page, your FIRST action MUST be domhand_fill. It fills ALL visible form fields in one call via DOM manipulation. Do NOT use click or input actions before trying domhand_fill.
2a. REPEATER SECTIONS: On pages with repeater sections (Education, Work Experience, Skills, Languages, Licenses and Certificates — any section with an 'Add' button), you MUST use domhand_fill_repeaters(section=<name>) instead of domhand_fill for those sections. Call it ONCE per section. It handles expanding, filling, and committing all entries automatically. If a section has no visible 'Add' button or fields, skip it — it is optional on this application. Do NOT loop searching for a section that has no interactive elements.
3. After domhand_fill completes, call domhand_assess_state ONCE to learn whether advancement is allowed and which required blockers remain.
   If a field appeared in domhand_fill failures but is NOT in the latest domhand_assess_state unresolved list, do NOT touch it again unless it is still visibly empty or visibly invalid on the page.
4. Treat domhand_fill as a page-level progressive conquer pass, not a same-page control loop. After the first domhand_fill on a page, do NOT keep calling domhand_fill again for ordinary text, tel, textarea, search, or standard select/combobox fields on that SAME page.
5. After the first domhand_fill on a page, prefer normal browser-use/manual recovery for remaining generic fields (text inputs, tel inputs, ordinary dropdowns, clicks, typing, option selection). Use DomHand control tools only when the blocker is clearly a widget DomHand specializes in (radio/checkbox/toggle/button-group/non-searchable custom dropdown) or a repeater-entry fill. Workday searchable selectinput widgets are NOT DomHand-specialized beyond the first domhand_fill pass. Manual recovery must target only the unresolved fields or fields that are still visibly empty/invalid on the page. Do NOT broadly re-enter nearby fields that already look settled. You may fix adjacent visible fields in the same section together when the correction is obvious (for example Phone + Location, or address subfields). Do NOT let a stale "primary blocker" notion stop you from fixing a clearly editable neighboring field.
6. For optional fields, only retry when the applicant profile clearly maps to that field with high confidence. If the optional match is ambiguous, leave it blank.
7. For file uploads (resume), use domhand_upload or upload_file action.
8. Use domhand_assess_state as a checkpoint before Next/Continue/Save or when the current visible section is genuinely ambiguous. Do NOT let a stale blocker set stop you from correcting a visibly editable field manually.
9. Do NOT call domhand_assess_state after every single manual correction. Finish the same-page manual/browser-use fixes first, then checkpoint once before advancing. After all fields on the current page are filled, click Next/Continue/Save when domhand_assess_state shows no unresolved required fields, no visible errors, no opaque blockers, and an enabled advance control. Do NOT let mismatched/unverified readback noise alone block a visibly successful manual recovery.
9a. Visual gate beats stale DOM optimism: before clicking Next/Continue/Save, inspect the current required section. If any red validation text such as "This info is required" is visible, or a required radio/button-group question still has no visibly selected option, do NOT advance even if domhand_assess_state says advanceable.
9b. Proceed/advance is primarily a browser-use local decision, not a domhand_assess_state loop. Once the latest domhand_assess_state on the current page says advance_allowed=yes and you do not visibly see red validation errors or unselected required radio/button-group controls, stop calling domhand_fill/domhand_assess_state on that page and click Next/Continue/Save immediately.
10. On each new page, call domhand_fill AGAIN as the first action. EXCEPT on Workday after a successful same-site 'Autofill with Resume' / 'Apply with Resume' start: do not go searching again for another resume upload field on later pages unless the page visibly says the resume is missing or requires re-upload.

COMPLETION STATES:
{build_completion_detection_text(platform)}

Other rules:
- {"Use the provided credentials to log in if needed." if credentials else "If a login wall appears, report it as a blocker."}
- Do NOT click the final Submit button. Use the completion-state rules above and stop with the done action when the page is review, confirmation, or an allowed presubmit_single_page state.
- If anything pops up blocking the form, call domhand_close_popup first. Only fall back to Escape or coordinate clicks if that DOM-first popup close action fails.
- Every non-consent applicant value must come from the provided user profile. If the profile does not provide it, leave it empty or unresolved.
- Never invent placeholder personal info like John, Doe, or John Doe. Use the exact applicant identity from the provided profile only.
{workday_start_flow_rules.rstrip()}
- Use domhand_assess_state before clicking Next/Continue/Save and when the current visible section is genuinely ambiguous. Do not turn it into a same-page reverification loop.
- For searchable or multi-layer dropdowns, type/search, WAIT 2-3 seconds for the list to update, and keep clicking until the final leaf option is selected and the field visibly changes.
- Do NOT click a dropdown option and then Save/Continue in the same action batch. Wait briefly, verify the field settled, then continue.
- If domhand_select returns {FAIL_OVER_NATIVE_SELECT}, do NOT click the native <select>. Use dropdown_options(index=...) to inspect the exact option text/value, then select_dropdown(index=..., text=...) with the exact text/value.
- If domhand_select returns {FAIL_OVER_CUSTOM_WIDGET}, stop retrying domhand_select, open the widget manually, search if supported, and click the final leaf option.
- If domhand_fill or domhand_select returns "domhand_retry_capped" for a blocker, stop repeating that SAME DomHand strategy on that field/value pair. Switch to browser-use/manual recovery unless the control is clearly a DomHand-specialized widget.
- For searchable employer/school/skill/language widgets, NEVER substitute a different applicant value just because the original search returned no results. Keep the same requested value. For 'Name of Latest Employer', use the latest work-experience employer only; if no safe match exists, leave it unresolved for browser/manual fallback. Do NOT switch that field to 'Other'. Source/referral widgets are the exception: they are generous and may use safe alternatives like 'LinkedIn', 'Job Board/Social Media', 'Company Careers Page', 'Website', or 'Other'.
- For Workday searchable selectinput widgets (Skills, School or University, Field of Study, employer search, language search, and similar prompt-search controls EXCEPT source/referral fields), allow ONLY the initial domhand_fill attempt. If that first DomHand attempt misses, returns 'No Items.', or fails to commit a visible chip/value, STOP using domhand_fill, domhand_select, and domhand_interact_control on that SAME widget for the rest of the page. Switch strictly to browser-use/manual recovery.
- Browser-use/manual recipe for a Workday searchable selectinput widget: stay on the SAME widget, clear any stale query first, open the widget's own prompt/search affordance inside that field, type the SAME requested value once, then wait 2-3 seconds and inspect the visible suggestion list. For Skills specifically, Enter may be required, but ONLY when the SAME exact skill is visibly present or an exact highlighted result is ready to commit; after Enter, wait another 2-3 seconds and VERIFY the matching chip/token appears. For non-Skills searchable widgets, do NOT use Enter unless the exact visible option clearly requires it. Click the matching option if needed. If no option appears, do NOT append more text and do NOT keep retyping variants into the same input. You may try one or two deterministic SAME-SKILL aliases only (for example React -> ReactJS, ExpressJS -> Express, MySQLDB -> MySQL); otherwise leave that widget unresolved. Do NOT substitute a different value.
- Source/referral fields such as 'How Did You Hear About Us?' are intentionally generous. Preferred answers in order: 'LinkedIn', 'Job Board/Social Media', 'Company Careers Page', 'Website', then 'Other'. If one safe answer has no exact leaf, try the next safe answer. Spend at most 3 actions total on that field and do NOT loop.
- Visual gate beats stale DOM optimism: if red validation text such as "This info is required" is visible, or a required radio/button-group still has no visibly selected option, do NOT click Next/Continue/Save yet.
- For Oracle / HCM repeater pages (Work Experience, College / University, Technical Skills, Language Skills, Licenses and Certificates), treat domhand_assess_state as advisory only. Do NOT reopen, overwrite, or delete an already-saved repeater tile just because assess_state or an old plan mentions that section.
- If a scoped domhand_fill on a visible Oracle repeater returns filled_count=0, expands the wrong repeater, or leaves the same editor visible, stop using domhand_fill/domhand_assess_state as the recovery loop for that repeater. Switch to browser-use/manual actions inside that SAME visible editor only, wait about 1 second after each open/select action for Oracle to settle, and commit that repeater before touching any other section.
- If a College / University editor is visible, stay inside that SAME education editor until it is committed and collapsed into a saved tile. Do NOT start Technical Skills, Language Skills, or Licenses while the education editor is still open.
- On Oracle education editors, Start Date Month and Start Date Year are hard prerequisites for saving. Do NOT click 'Add Education' until those Start Date fields are visibly populated and the current education editor no longer shows required-date validation.
- On Oracle inline repeater editors, a filled form is NOT saved until the visible bottom commit button is clicked and the tile appears. After filling Technical Skills / Language Skills / Licenses, explicitly click the matching bottom button ('Add Skill', 'Add Language', or 'Add License') and verify the editor collapsed before moving on.
- For phone country code or phone type dropdowns, if the first term fails, try close variants like "United States +1", "United States", "+1", "USA", "US", "Mobile", and "Cell" before giving up.
- For stubborn checkbox/radio/button controls, if the intended option still does not stick after 2 tries, stop blind retries: click the currently selected option once to clear/reset stale state, then click the intended option again and verify the visible state changed.
- For text/date/search inputs that visibly contain the value but still show validation errors, stay on that SAME field: commit it with Enter when appropriate, then blur or Tab away so the page re-validates it before moving on.
- For date fields, prefer clicking a visible date icon/calendar button and selecting the actual picker cell. Only type the date when no usable picker affordance exists or picker interaction has already failed.
- Use the latest domhand_assess_state as guidance, not as a hard gate. If a visible field is still empty or invalid, you may correct it even when the stale blocker list lags behind.
- Keep working near the current unresolved section and continue downward. Do NOT scroll back to the top just to re-check earlier fields unless a specific earlier required field is visibly empty or invalid.
- When close to completion, keep memory and next_goal short. Do NOT restate the whole form or do a top-to-bottom verification loop once a terminal completion state is reached.
- If the page looks blank or partially loaded after clicking a start/continue button, WAIT 5-10 seconds before retrying, going back, or reopening the same dialog.
- Never use navigate() to return to the original job URL after entering the application flow. Waiting is the default recovery.
"""

    # ── Status callback for cost tracking + VALET progress ───────
    step_count = 0
    last_cost_usd = 0.0
    last_usage_input_tokens = 0
    last_usage_output_tokens = 0

    async def _on_status(status: dict) -> None:
        nonlocal step_count, last_cost_usd, last_usage_input_tokens, last_usage_output_tokens
        step_count += 1
        current_cost_usd = float(status.get("cost_usd") or 0.0)
        raw_step_cost = status.get("step_cost")
        step_cost = float(raw_step_cost) if raw_step_cost is not None else max(current_cost_usd - last_cost_usd, 0.0)

        input_tokens: int | None = None
        output_tokens: int | None = None
        if raw_step_cost is not None:
            if status.get("input_tokens") is not None and status.get("output_tokens") is not None:
                input_tokens = int(status["input_tokens"])
                output_tokens = int(status["output_tokens"])
        else:
            usage_input_tokens = status.get("usage_input_tokens")
            usage_output_tokens = status.get("usage_output_tokens")
            if usage_input_tokens is not None and usage_output_tokens is not None:
                input_tokens = max(int(usage_input_tokens) - last_usage_input_tokens, 0)
                output_tokens = max(int(usage_output_tokens) - last_usage_output_tokens, 0)

        if (
            input_tokens is not None
            and output_tokens is not None
            and (step_cost > 0 or input_tokens > 0 or output_tokens > 0)
        ):
            try:
                cost_tracker.track_step(
                    step=step_count,
                    tokens_in=input_tokens,
                    tokens_out=output_tokens,
                    model=status.get("model", settings.agent_model),
                )
            except KeyError:
                log.warning(
                    "executor.cost_tracking_model_unknown",
                    model=status.get("model", settings.agent_model),
                    step=step_count,
                )

        last_cost_usd = current_cost_usd
        if status.get("usage_input_tokens") is not None:
            last_usage_input_tokens = int(status["usage_input_tokens"])
        if status.get("usage_output_tokens") is not None:
            last_usage_output_tokens = int(status["usage_output_tokens"])

    log.info("executor.launching_agent", platform=platform, headless=settings.headless)

    # ── Run the agent ────────────────────────────────────────────
    try:
        result = await run_job_agent(
            task=task,
            resume_profile=profile,
            credentials=credentials,
            platform=platform,
            headless=settings.headless,
            max_steps=settings.max_steps_per_job,
            job_id=job_id,
            max_budget=settings.max_budget_per_job,
            on_status_update=_on_status,
        )
    finally:
        # Clean up PII temp file
        with contextlib.suppress(OSError):
            os.unlink(profile_path)
        os.environ.pop("GH_USER_PROFILE_PATH", None)

    log.info(
        "executor.agent_finished",
        success=result.get("success"),
        steps=result.get("steps"),
        cost=result.get("cost_usd"),
        blocker=result.get("blocker"),
    )

    # ── HITL: pause on blocker ───────────────────────────────────
    if result.get("blocker"):
        log.info("executor.hitl_pause", blocker=result["blocker"])
        await hitl.pause_job(
            job_id=job_id,
            reason=result["blocker"],
            interaction_type="blocker",
            valet_task_id=valet_task_id,
        )
        signal = await hitl.wait_for_resume(job_id)
        if signal and signal.get("action") == "cancel":
            return {
                "success": False,
                "summary": "Cancelled by user",
                "steps_taken": result.get("steps", 0),
                "cost_usd": result.get("cost_usd", 0.0),
                "cost_summary": result.get("cost_summary"),
                "blocker": result.get("blocker"),
                "error_code": "user_cancelled",
            }
        # On resume, result is already captured -- continue to completion
        log.warning(
            "hitl.resume_not_implemented",
            msg="Resume after pause is not yet implemented — returning original result",
        )

    # ── Map to executor result format ────────────────────────────
    return {
        "success": result.get("success", False),
        "summary": result.get("extracted_text") or "Agent completed",
        "steps_taken": result.get("steps", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "cost_summary": result.get("cost_summary"),
        "blocker": result.get("blocker"),
    }


async def _fail_job(
    db: Database,
    valet: ValetClient,
    job_id: str,
    error_code: str,
    error_message: str,
    valet_task_id: str | None = None,
    cost_tracker: CostTracker | None = None,
) -> None:
    """Mark a job as failed in DB and notify VALET."""
    await db.update_job_status(
        job_id,
        "failed",
        metadata={"error_code": error_code, "message": error_message},
    )
    await db.write_job_event(
        job_id,
        "failed",
        metadata={"error_code": error_code, "error": error_message},
    )

    cost = None
    if cost_tracker:
        summary = cost_tracker.get_summary()
        cost = {
            "total_cost_usd": summary["total_cost_usd"],
            "action_count": summary["step_count"],
            "total_tokens": summary["total_tokens"],
        }

    await valet.report_completion(
        job_id=job_id,
        success=False,
        result={"error": error_message, "error_code": error_code},
        valet_task_id=valet_task_id,
        worker_id=settings.worker_id,
        cost=cost,
    )
