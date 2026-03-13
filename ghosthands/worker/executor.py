"""Job executor — orchestrates a single job from claim to completion.

Takes a claimed job row, loads resume + credentials, detects the platform,
creates the browser-use agent, runs it, and reports results back to the
database and VALET.
"""

from __future__ import annotations

import asyncio
import json
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


async def _heartbeat_loop(db: Database, job_id: str, interval: float = 30.0) -> None:
    """Background task that updates the heartbeat timestamp periodically.

    Runs until cancelled. Prevents the stuck-job recovery from reclaiming
    our job while it's still running.
    """
    while True:
        try:
            await db.heartbeat(job_id)
        except Exception as exc:
            logger.warning("executor.heartbeat_failed", job_id=job_id, error=str(exc))
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
        cost_summary = cost_tracker.get_summary()
        result_data = {
            "success": result.get("success", True),
            "summary": result.get("summary", "Job completed"),
            "platform": platform,
            "steps_taken": cost_summary["step_count"],
            "cost": cost_summary,
            **{k: v for k, v in result.items() if k not in ("success", "summary")},
        }

        await db.write_job_result(job_id, result_data)
        await db.write_job_event(job_id, "completed", metadata=cost_summary)

        # Step 9: Fire VALET completion callback
        await valet.report_completion(
            job_id=job_id,
            success=True,
            result=result_data,
            valet_task_id=valet_task_id,
            worker_id=settings.worker_id,
            cost={
                "total_cost_usd": cost_summary["total_cost_usd"],
                "action_count": cost_summary["step_count"],
                "total_tokens": cost_summary["total_tokens"],
            },
        )

        log.info(
            "executor.completed",
            success=True,
            steps=cost_summary["step_count"],
            cost=cost_summary["total_cost_usd"],
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
    import os

    from ghosthands.agent.factory import run_job_agent
    from ghosthands.agent.prompts import (
        FAIL_OVER_CUSTOM_WIDGET,
        FAIL_OVER_NATIVE_SELECT,
        build_completion_detection_text,
    )

    # ── Set profile env var for domhand_fill ──────────────────────
    # domhand_fill reads GH_USER_PROFILE_TEXT to generate field answers.
    profile = resume_profile or {}
    os.environ["GH_USER_PROFILE_TEXT"] = json.dumps(profile, indent=2)
    os.environ["GH_USER_PROFILE_JSON"] = json.dumps(profile)

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
        )

    task = f"""Go to {target_url} and fill out the job application form completely.

CRITICAL — Action Order:
1. If a popup, modal, interstitial, or newsletter prompt is visibly blocking the form, call domhand_close_popup first. Use Escape or coordinate clicks only if domhand_close_popup fails.
2. After navigating to the page, your FIRST action MUST be domhand_fill. It fills ALL visible form fields in one call via DOM manipulation. Do NOT use click or input actions before trying domhand_fill.
3. Immediately call domhand_assess_state to classify the page, unresolved required fields, and scroll direction.
4. After domhand_fill completes, review its output to see which fields were filled and which failed.
5. For failed dropdowns/selects, use domhand_select. Retry failed fields even if they are optional when the applicant profile provides a value (address, website, referral source, LinkedIn, etc.).
   For optional fields, only retry when the applicant profile clearly maps to that field with high confidence. If the optional match is ambiguous, leave it blank.
6. For file uploads (resume), use domhand_upload or upload_file action.
7. Only use generic browser-use actions (click, input) as a LAST RESORT for fields DomHand could not handle.
8. Before any large scroll or any Next/Continue/Save click, call domhand_assess_state again and follow its unresolved field list plus scroll_bias.
9. After all fields on the current page are filled, click Next/Continue/Save to advance when the page is still in `advanceable`.
10. On each new page, call domhand_fill AGAIN as the first action.

COMPLETION STATES:
{build_completion_detection_text(platform)}

Other rules:
- {"Use the provided credentials to log in if needed." if credentials else "If a login wall appears, report it as a blocker."}
- Do NOT click the final Submit button. Use the completion-state rules above and stop with the done action when the page is review, confirmation, or an allowed presubmit_single_page state.
- If anything pops up blocking the form, call domhand_close_popup first. Only fall back to Escape or coordinate clicks if that DOM-first popup close action fails.
- Every non-consent applicant value must come from the provided user profile. If the profile does not provide it, leave it empty or unresolved.
- Never invent placeholder personal info like John, Doe, or John Doe. Use the exact applicant identity from the provided profile only.
{workday_start_flow_rules.rstrip()}
- Use domhand_assess_state before any large scroll, before clicking Next/Continue/Save, and before calling done(). Follow its unresolved field list and scroll_bias instead of doing a full-page reverification loop.
- For searchable or multi-layer dropdowns, type/search, WAIT 2-3 seconds for the list to update, and keep clicking until the final leaf option is selected and the field visibly changes.
- Do NOT click a dropdown option and then Save/Continue in the same action batch. Wait briefly, verify the field settled, then continue.
- If domhand_select returns {FAIL_OVER_NATIVE_SELECT}, do NOT click the native <select>. Use dropdown_options(index=...) to inspect the exact option text/value, then select_dropdown(index=..., text=...) with the exact text/value.
- If domhand_select returns {FAIL_OVER_CUSTOM_WIDGET}, stop retrying domhand_select, open the widget manually, search if supported, and click the final leaf option.
- For phone country code or phone type dropdowns, if the first term fails, try close variants like "United States +1", "United States", "+1", "USA", "US", "Mobile", and "Cell" before giving up.
- For stubborn checkbox/radio/button controls, if the intended option still does not stick after 2 tries, stop blind retries: click the currently selected option once to clear/reset stale state, then click the intended option again and verify the visible state changed.
- For text/date/search inputs that visibly contain the value but still show validation errors, focus the field and press Enter or Tab to commit it before moving on.
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

    log.info(
        "executor.agent_finished",
        success=result.get("success"),
        steps=result.get("steps"),
        cost=result.get("cost_usd"),
        blocker=result.get("blocker"),
    )

    # ── Map to executor result format ────────────────────────────
    return {
        "success": result.get("success", False),
        "summary": result.get("extracted_text") or "Agent completed",
        "steps_taken": result.get("steps", 0),
        "cost_usd": result.get("cost_usd", 0.0),
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
