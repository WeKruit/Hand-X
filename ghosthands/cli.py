"""Hand-X CLI -- entry point for the bundled desktop binary.

This module is the interface between the Electron desktop app and the
browser-use agent.  Communication happens via stdio:

- stdout -> JSONL events (ProgressEvent-compatible)
- stderr -> structured logging
- stdin  -> commands from Electron (cancel, complete_review, cancel_job)

Usage (from Electron -- JSONL mode):
    python -m ghosthands \\
        --job-url "https://..." \\
        --profile '{"name": "Jane", ...}' \\
        --resume /path/to/resume.pdf \\
        --output-format jsonl \\
        --proxy-url "https://valet.../api/v1/local-workers/anthropic" \\
        --runtime-grant "lwrg_v1_..." \\
        --max-steps 50

Usage (human-readable output for development):
    python -m ghosthands \\
        --job-url "https://..." \\
        --test-data examples/apply_to_job_sample_data.json \\
        --resume examples/resume.pdf

The --output-format flag controls output:
  - "jsonl" (default): stdout is a clean JSONL stream, all logging to stderr
  - "human": regular print-based output for terminal use
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from ghosthands.bridge.profile_adapter import (
    camel_to_snake_profile,
    normalize_profile_defaults,
)
from ghosthands.bridge.protocol import (
    listen_for_cancel,
    wait_for_review_command,
)

# Backward-compatible alias retained for older internal imports/tests.
_camel_to_snake_profile = camel_to_snake_profile

# Force unbuffered I/O for reliable JSONL streaming
os.environ["PYTHONUNBUFFERED"] = "1"

# Suppress browser-use's own logging setup so we control stderr exclusively
os.environ["BROWSER_USE_SETUP_LOGGING"] = "false"

from ghosthands.agent.hooks import install_same_tab_guard

logger = structlog.get_logger()

# ── Argument parsing ──────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    # Strip optional "apply" subcommand for backwards compat:
    #   hand-x apply --job-url ...  AND  hand-x --job-url ...  both work.
    argv = sys.argv[1:]
    if argv and argv[0] == "apply":
        argv = argv[1:]

    parser = argparse.ArgumentParser(
        prog="hand-x",
        description="Hand-X -- browser automation engine for job applications",
    )

    # Required
    parser.add_argument("--job-url", required=True, help="Job posting URL to apply to")

    # Profile source (one of these is required)
    parser.add_argument("--profile", default=None, help="Applicant profile as JSON string or @filepath")
    parser.add_argument("--test-data", default=None, help="Path to applicant data JSON file")

    # Optional
    parser.add_argument("--resume", default=None, help="Path to resume PDF")
    parser.add_argument("--job-id", default="", help="Job ID for event tracking")
    parser.add_argument("--lease-id", default="", help="Lease ID for event tracking")
    parser.add_argument("--model", default=None, help="LLM model override")
    parser.add_argument("--max-steps", type=int, default=50, help="Max agent steps (default: 50)")
    parser.add_argument("--max-budget", type=float, default=0.50, help="Max LLM budget USD")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument(
        "--output-format",
        choices=["jsonl", "human"],
        default="jsonl",
        help="Output format: jsonl for IPC, human for terminal (default: jsonl)",
    )

    # VALET proxy
    parser.add_argument("--proxy-url", default=None, help="VALET LLM proxy URL")
    parser.add_argument("--runtime-grant", default=None, help="VALET runtime grant token")

    # Playwright
    parser.add_argument("--browsers-path", default=None, help="Path to Playwright browser binaries")

    # Desktop-owned browser (CDP)
    parser.add_argument(
        "--cdp-url",
        type=str,
        default=None,
        help="Connect to an existing browser via CDP URL instead of launching a new one (Desktop-owned browser mode)",
    )

    return parser.parse_args(argv)


# ── Logging setup ─────────────────────────────────────────────────────


def _setup_logging() -> None:
    """Route ALL logging to stderr so stdout stays clean."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    structlog.configure(
        cache_logger_on_first_use=True,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.KeyValueRenderer(
                key_order=["event", "level", "logger", "timestamp"],
            ),
        ],
    )


# ── Profile loading ───────────────────────────────────────────────────


def _load_profile(args: argparse.Namespace) -> dict:
    """Load applicant profile from --profile or --test-data."""

    def _validate_profile(profile: Any) -> dict:
        """Assert the parsed value is a JSON object (dict), not a list or scalar."""
        if not isinstance(profile, dict):
            raise ValueError("Profile must be a JSON object")
        return profile

    # --profile takes precedence (inline JSON or @filepath)
    if args.profile:
        raw = args.profile
        if raw.startswith("@"):
            path = Path(raw[1:])
            if not path.exists():
                raise FileNotFoundError(f"Profile file not found: {path}")
            return _validate_profile(json.loads(path.read_text()))
        return _validate_profile(json.loads(raw))

    # --test-data: load from JSON file
    if args.test_data:
        path = Path(args.test_data)
        if not path.exists():
            raise FileNotFoundError(f"Test data file not found: {path}")
        with open(path) as f:
            data = json.load(f)
        # Try to normalize via resume_loader if available
        try:
            from ghosthands.integrations.resume_loader import load_resume_from_file

            return _validate_profile(load_resume_from_file(str(path)))
        except Exception:
            return _validate_profile(data)

    # Environment variable fallback (for desktop bridge)
    profile_text = os.environ.get("GH_USER_PROFILE_TEXT", "")
    if profile_text:
        return _validate_profile(json.loads(profile_text))

    raise ValueError("Either --profile, --test-data, or GH_USER_PROFILE_TEXT env var is required")


def _apply_runtime_env(
    args: argparse.Namespace,
    profile: dict[str, Any],
) -> str:
    """Set runtime environment variables expected by downstream modules."""
    if args.proxy_url:
        os.environ["GH_LLM_PROXY_URL"] = args.proxy_url
    if args.runtime_grant:
        os.environ["GH_LLM_RUNTIME_GRANT"] = args.runtime_grant
    if args.browsers_path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = args.browsers_path

    os.environ["GH_USER_PROFILE_TEXT"] = json.dumps(profile, indent=2)
    os.environ["GH_USER_PROFILE_JSON"] = json.dumps(profile)

    resume_path = str(Path(args.resume).resolve()) if args.resume else ""
    if resume_path:
        os.environ["GH_RESUME_PATH"] = resume_path

    return resume_path


def _load_runtime_settings():
    """Load settings after CLI-provided environment overrides are applied."""
    from ghosthands.config.settings import Settings

    return Settings()


def _resolve_sensitive_data(
    app_settings,
    embedded_credentials: dict[str, Any] | None = None,
    platform: str = "generic",
) -> dict[str, str] | None:
    """Resolve credentials with priority: profile creds > env vars.

    When the Desktop app embeds a ``credentials`` key in the profile JSON,
    we resolve platform-specific credentials first, then fall back to
    ``generic``, then ``GH_EMAIL``/``GH_PASSWORD`` env vars.

    Parameters
    ----------
    embedded_credentials:
        The ``credentials`` dict popped from the profile JSON (if any).
        Structure: ``{"generic": {"email": ..., "password": ...},
        "workday": {...}, "application_password": "..."}``
    platform:
        The already-detected platform string (e.g. ``"workday"``,
        ``"greenhouse"``).  Callers must detect this once via
        ``detect_platform()`` and pass it in to avoid redundant calls.
    """
    # ── Extract embedded credentials from profile ────────────────
    creds_email = ""
    creds_password = ""

    if embedded_credentials and isinstance(embedded_credentials, dict):
        # Priority 1: platform-specific credentials
        platform_creds = embedded_credentials.get(platform) or {}
        if isinstance(platform_creds, dict) and platform_creds.get("email") and platform_creds.get("password"):
            creds_email = platform_creds["email"]
            creds_password = platform_creds["password"]
        else:
            # Priority 2: generic credentials
            generic_creds = embedded_credentials.get("generic") or {}
            if isinstance(generic_creds, dict) and generic_creds.get("email") and generic_creds.get("password"):
                creds_email = generic_creds["email"]
                creds_password = generic_creds["password"]

        # Also check application_password as fallback for password only
        if creds_email and not creds_password:
            creds_password = embedded_credentials.get("application_password", "")

    # Priority 3: env vars (GH_EMAIL / GH_PASSWORD via app_settings)
    email = creds_email or app_settings.email or ""
    password = creds_password or app_settings.password or ""

    if email and password:
        return {"email": email, "password": password}
    return None


def _warn_if_proxy_overrides_direct_keys(
    args: argparse.Namespace,
    app_settings,
) -> None:
    """Warn when VALET proxy mode is active alongside direct Anthropic keys."""
    if (args.proxy_url or app_settings.llm_proxy_url) and (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GH_ANTHROPIC_API_KEY")
    ):
        logger.warning(
            "llm.proxy_mode_active",
            detail="Direct API keys ignored when --proxy-url is set",
            proxy_url=app_settings.llm_proxy_url,
        )


async def _cleanup_browser(browser, desktop_owns_browser: bool) -> None:
    """Shut down the browser session with ownership-aware cleanup.

    When the Desktop app owns the browser (CDP mode), we only disconnect
    from the session via ``stop()`` — the browser process stays alive.
    When Hand-X launched the browser itself, we tear it down fully via
    ``close()`` (the pre-existing behavior).
    """
    if desktop_owns_browser:
        await browser.stop()
    else:
        await browser.close()


@dataclass(frozen=True)
class _RuntimeErrorSignal:
    """User-facing error details for known proxy/runtime failures."""

    code: str
    message: str
    fatal: bool = True
    keep_browser_open: bool = False


def _iter_exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    """Return the causal exception chain from outermost to innermost."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc

    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__

    return tuple(chain)


def _classify_runtime_error(exc: BaseException, *, proxy_mode: bool) -> _RuntimeErrorSignal | None:
    """Map known proxy/runtime failures to Desktop-friendly error events."""
    if not proxy_mode:
        return None

    status_codes: set[int] = set()
    text_chunks: list[str] = []
    headers: dict[str, str] = {}

    for candidate in _iter_exception_chain(exc):
        status_code = getattr(candidate, "status_code", None)
        if isinstance(status_code, int):
            status_codes.add(status_code)

        message = getattr(candidate, "message", None)
        if message:
            text_chunks.append(str(message))

        body = getattr(candidate, "body", None)
        if body:
            if isinstance(body, dict | list):
                text_chunks.append(json.dumps(body, default=str))
            else:
                text_chunks.append(str(body))

        candidate_text = str(candidate)
        if candidate_text:
            text_chunks.append(candidate_text)

        response = getattr(candidate, "response", None)
        if response is not None:
            response_status = getattr(response, "status_code", None)
            if isinstance(response_status, int):
                status_codes.add(response_status)

            response_headers = getattr(response, "headers", None)
            if response_headers is not None:
                with contextlib.suppress(Exception):
                    for key, value in response_headers.items():
                        headers[str(key).lower()] = str(value)

            with contextlib.suppress(Exception):
                response_text = response.text
                if response_text:
                    text_chunks.append(str(response_text))

    combined_text = " ".join(text_chunks).lower()

    if 429 in status_codes and headers.get("x-budget-exhausted", "").lower() == "true":
        return _RuntimeErrorSignal(
            code="BUDGET_EXHAUSTED",
            message=(
                "This application required too many AI steps. The partially completed "
                "form is still open in the browser — you can finish it manually."
            ),
            keep_browser_open=True,
        )

    if 401 in status_codes and "expired" in combined_text:
        return _RuntimeErrorSignal(
            code="GRANT_EXPIRED",
            message="Your automation session expired. Please try again.",
            keep_browser_open=True,
        )

    return None


def _handle_review_result(
    review_result: str,
    *,
    fields_filled: int,
    fields_failed: int,
    job_id: str,
    lease_id: str,
    result_data: dict[str, Any],
) -> int | None:
    """Emit the terminal review result event and return the desired exit code."""
    from ghosthands.output.jsonl import emit_done

    if review_result == "complete":
        emit_done(
            success=True,
            message="Application submitted — review completed",
            fields_filled=fields_filled,
            fields_failed=fields_failed,
            job_id=job_id,
            lease_id=lease_id,
            result_data=result_data,
        )
        return None

    if review_result == "cancel":
        emit_done(
            success=False,
            message="Review cancelled by user",
            fields_filled=fields_filled,
            fields_failed=fields_failed,
            job_id=job_id,
            lease_id=lease_id,
            result_data={**result_data, "success": False, "cancelled": True},
        )
        return 1

    if review_result == "timeout":
        emit_done(
            success=False,
            message="Review timed out after 30 minutes. The browser window is still open — you can submit manually.",
            fields_filled=fields_filled,
            fields_failed=fields_failed,
            job_id=job_id,
            lease_id=lease_id,
            result_data={**result_data, "success": False, "timedOut": True},
        )
        return 1

    emit_done(
        success=False,
        message="Desktop disconnected",
        fields_filled=fields_filled,
        fields_failed=fields_failed,
        job_id=job_id,
        lease_id=lease_id,
        result_data={**result_data, "success": False},
    )
    return 1


# ── JSONL agent run ───────────────────────────────────────────────────


async def run_agent_jsonl(args: argparse.Namespace) -> None:
    """Run the agent with JSONL event output on stdout."""
    from ghosthands.output.jsonl import (
        emit_awaiting_review,
        emit_browser_ready,
        emit_cost,
        emit_done,
        emit_error,
        emit_phase,
        emit_status,
    )

    app_settings = None
    browser = None
    job_id = ""
    lease_id = ""
    desktop_owns_browser = False
    last_phase: str | None = None

    def _emit_phase_if_changed(phase: str, detail: str | None = None) -> None:
        nonlocal last_phase
        if phase == last_phase:
            return
        emit_phase(phase, detail=detail)
        last_phase = phase

    # -- Load profile -------------------------------------------------------
    try:
        profile = _load_profile(args)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.error("profile_load_failed", error=str(e))
        emit_error("Failed to load applicant profile", fatal=True)
        sys.exit(1)

    # -- Convert camelCase keys from Desktop bridge to snake_case ----------
    profile = camel_to_snake_profile(profile)

    # -- Extract embedded credentials before they leak into env/profile ----
    # We pop them so they don't end up in GH_USER_PROFILE_TEXT env var.
    embedded_credentials = profile.pop("credentials", None)

    # -- Normalize profile defaults for DomHand ----------------------------
    profile = normalize_profile_defaults(profile)
    _emit_phase_if_changed("Starting application")

    # -- Set env vars -------------------------------------------------------
    resume_path = _apply_runtime_env(args, profile)

    # -- Install DomHand field event callback --------------------------------
    from ghosthands.output import field_events

    field_events.install_jsonl_callback()

    # -- Import heavy deps after env setup ----------------------------------
    from browser_use import Agent, BrowserProfile, BrowserSession, Tools

    app_settings = _load_runtime_settings()

    # -- Resolve job_id / lease_id: CLI args take precedence, env fallback ---
    job_id = args.job_id or app_settings.job_id
    lease_id = args.lease_id or app_settings.lease_id

    emit_status("Hand-X engine initialized", job_id=job_id)
    emit_status("Setting up agent...", job_id=job_id)

    _warn_if_proxy_overrides_direct_keys(args, app_settings)

    from ghosthands.llm.client import get_chat_model

    llm = get_chat_model(model=args.model)

    # -- DomHand actions ----------------------------------------------------
    tools: Tools = Tools()
    try:
        from ghosthands.actions import register_domhand_actions

        register_domhand_actions(tools)
        emit_status("DomHand actions registered", job_id=job_id)
    except Exception as e:
        emit_status(f"DomHand unavailable: {e}, using generic actions", job_id=job_id)

    # -- Platform detection -------------------------------------------------
    platform = "generic"
    try:
        from ghosthands.platforms import detect_platform

        platform = detect_platform(args.job_url)
    except ImportError:
        pass

    # -- System prompt ------------------------------------------------------
    system_ext = ""
    try:
        from ghosthands.agent.prompts import build_system_prompt

        system_ext = build_system_prompt(profile, platform)
    except ImportError:
        pass

    # -- Credentials --------------------------------------------------------
    sensitive_data = _resolve_sensitive_data(app_settings, embedded_credentials, platform=platform)

    # -- Domain lockdown ----------------------------------------------------
    from ghosthands.security.domain_lockdown import DomainLockdown

    lockdown = DomainLockdown(job_url=args.job_url, platform=platform)
    allowed_domains = lockdown.get_allowed_domains()

    # -- Browser ------------------------------------------------------------
    cdp_url = args.cdp_url or os.environ.get("GH_CDP_URL")
    desktop_owns_browser = cdp_url is not None

    if cdp_url:
        # Desktop-owned browser: connect to existing browser via CDP URL.
        # Do not launch a new browser; headless flag is irrelevant here.
        browser_profile = BrowserProfile(keep_alive=True, allowed_domains=allowed_domains)
        browser = BrowserSession(browser_profile=browser_profile, cdp_url=cdp_url)
        emit_status("Connecting to Desktop-owned browser via CDP", job_id=job_id)
    else:
        browser_profile = BrowserProfile(
            headless=args.headless,
            keep_alive=True,
            allowed_domains=allowed_domains,
            aboutblank_loading_logo_enabled=True,
            demo_mode=False,
            interaction_highlight_color="rgb(37, 99, 235)",
        )
        browser = BrowserSession(browser_profile=browser_profile)

    # -- Task prompt --------------------------------------------------------
    from ghosthands.agent.prompts import build_task_prompt

    task = build_task_prompt(args.job_url, resume_path, sensitive_data)

    emit_status(
        f"Starting application: {args.job_url}",
        step=1,
        max_steps=args.max_steps,
        job_id=job_id,
    )

    # -- Step hooks for live JSONL events -----------------------------------
    async def _on_step_start(ag: Agent) -> None:
        from ghosthands.agent.hooks import infer_phase_from_goal

        await install_same_tab_guard(ag)
        step = ag.state.n_steps
        goal = ""
        if ag.state.last_model_output:
            goal = ag.state.last_model_output.next_goal or ""
        phase = infer_phase_from_goal(goal)
        if phase:
            _emit_phase_if_changed(phase, detail=goal or None)
        emit_status(
            phase or goal or f"Step {step}...",
            step=step,
            max_steps=args.max_steps,
            job_id=job_id,
        )

    async def _on_step_end(ag: Agent) -> None:
        usage = ag.history.usage
        if usage:
            emit_cost(
                total_usd=usage.total_cost or 0.0,
                prompt_tokens=usage.total_prompt_tokens or 0,
                completion_tokens=usage.total_completion_tokens or 0,
            )

        # Budget check
        if usage and usage.total_cost and usage.total_cost >= args.max_budget:
            ag.state.stopped = True
            emit_error("Budget exceeded", fatal=False, job_id=job_id)

    # -- Run ----------------------------------------------------------------
    try:
        await browser.start()
        if browser.cdp_url:
            emit_browser_ready(browser.cdp_url)
        else:
            logger.warning("cli.browser_ready_missing_cdp_url")
            emit_status(
                "Browser CDP URL unavailable — live review attachment disabled",
                job_id=job_id,
            )

        # -- Create agent ---------------------------------------------------
        available_files = [resume_path] if resume_path else []
        agent = Agent(
            task=task,
            llm=llm,
            browser_session=browser,
            tools=tools,
            extend_system_message=system_ext or None,
            sensitive_data=sensitive_data,
            available_file_paths=available_files or None,
            use_vision=True,
            max_actions_per_step=5,
            calculate_cost=True,
            use_judge=False,
        )

        cancel_requested = asyncio.Event()
        cancel_task = asyncio.create_task(listen_for_cancel(agent, cancel_requested))
        try:
            _emit_phase_if_changed("Navigating to application")
            history = await agent.run(
                max_steps=args.max_steps,
                on_step_start=_on_step_start,
                on_step_end=_on_step_end,
            )
        finally:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task

        is_done = history.is_done()
        final_result = history.final_result()
        total_cost = history.usage.total_cost if history.usage else 0.0
        total_steps = len(history.history) if history.history else 0

        # Final cost event
        if history.usage:
            emit_cost(
                total_usd=total_cost,
                prompt_tokens=history.usage.total_prompt_tokens or 0,
                completion_tokens=history.usage.total_completion_tokens or 0,
            )

        # Get real field counts from DomHand callback
        from ghosthands.output.field_events import get_field_counts

        filled_count, failed_count = get_field_counts()

        if cancel_requested.is_set():
            emit_done(
                success=False,
                message="Job cancelled by user",
                fields_filled=filled_count,
                fields_failed=failed_count,
                job_id=job_id,
                lease_id=lease_id,
                result_data={
                    "success": False,
                    "steps": total_steps,
                    "costUsd": round(total_cost, 6),
                    "finalResult": final_result,
                    "blocker": None,
                    "platform": platform,
                    "cancelled": True,
                },
            )
            await _cleanup_browser(browser, desktop_owns_browser)
            sys.exit(1)

        # Determine outcome
        success = is_done and bool(final_result)
        blocker: str | None = None
        if final_result and "blocker:" in final_result.lower():
            blocker = final_result
            success = False

        result_data = {
            "success": success,
            "steps": total_steps,
            "costUsd": round(total_cost, 6),
            "finalResult": final_result,
            "blocker": blocker,
            "platform": platform,
        }

        if success:
            # I-02/U-01: emit status (not done) before review so the terminal
            # event is only sent once, after the user has actually reviewed.
            _emit_phase_if_changed("Reviewing filled fields")
            emit_status("Application filled — awaiting review", job_id=job_id)

            # Resolve CDP URL and current page URL for Desktop review attachment
            review_cdp_url = browser.cdp_url
            review_page_url: str | None = None
            with contextlib.suppress(Exception):
                review_page_url = await browser.get_current_page_url()

            emit_awaiting_review(
                cdp_url=review_cdp_url,
                page_url=review_page_url,
            )
            review_result = await wait_for_review_command(browser, job_id, lease_id)
            exit_code = _handle_review_result(
                review_result,
                fields_filled=filled_count,
                fields_failed=failed_count,
                job_id=job_id,
                lease_id=lease_id,
                result_data=result_data,
            )
            if exit_code is not None:
                sys.exit(exit_code)
        else:
            emit_done(
                success=False,
                message=blocker or final_result or "Agent did not complete successfully",
                fields_filled=filled_count,
                fields_failed=failed_count,
                job_id=job_id,
                lease_id=lease_id,
                result_data=result_data,
            )
            await _cleanup_browser(browser, desktop_owns_browser)
            sys.exit(1)

    except Exception as e:
        logger.error("agent_run_failed", error=str(e))
        runtime_error = _classify_runtime_error(
            e,
            proxy_mode=bool(args.proxy_url or (app_settings and app_settings.llm_proxy_url)),
        )
        if runtime_error is not None:
            emit_error(
                runtime_error.message,
                fatal=runtime_error.fatal,
                job_id=job_id,
                code=runtime_error.code,
            )
            if browser is not None:
                with contextlib.suppress(Exception):
                    if runtime_error.keep_browser_open:
                        await browser.stop()
                    else:
                        await _cleanup_browser(browser, desktop_owns_browser)
            sys.exit(1)

        emit_error("Agent encountered an unexpected error", fatal=True, job_id=job_id)
        if browser is not None:
            with contextlib.suppress(Exception):
                await _cleanup_browser(browser, desktop_owns_browser)
        sys.exit(1)


# ── Human-readable agent run ─────────────────────────────────────────


async def run_agent_human(args: argparse.Namespace) -> None:
    """Run the agent with human-readable terminal output.

    This replicates the examples/apply_to_job.py experience for developers
    who want to test from the command line without parsing JSONL.
    """
    # -- Load profile -------------------------------------------------------
    try:
        profile = _load_profile(args)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # -- Convert camelCase keys from Desktop bridge to snake_case ----------
    profile = camel_to_snake_profile(profile)

    # -- Extract embedded credentials before they leak into env/profile ----
    embedded_credentials = profile.pop("credentials", None)

    # -- Normalize profile defaults for DomHand ----------------------------
    profile = normalize_profile_defaults(profile)

    # -- Set env vars -------------------------------------------------------
    resume_path = _apply_runtime_env(args, profile)

    # -- Import after env setup ---------------------------------------------
    from browser_use import Agent, BrowserProfile, BrowserSession, Tools

    app_settings = _load_runtime_settings()
    _warn_if_proxy_overrides_direct_keys(args, app_settings)

    from ghosthands.llm.client import get_chat_model

    llm = get_chat_model(model=args.model)

    # -- DomHand actions ----------------------------------------------------
    tools: Tools = Tools()
    try:
        from ghosthands.actions import register_domhand_actions

        register_domhand_actions(tools)
        print("DomHand actions registered")
    except Exception as e:
        print(f"DomHand unavailable: {e}")

    # -- Platform detection -------------------------------------------------
    platform = "generic"
    try:
        from ghosthands.platforms import detect_platform

        platform = detect_platform(args.job_url)
    except ImportError:
        pass

    # -- System prompt ------------------------------------------------------
    system_ext = ""
    try:
        from ghosthands.agent.prompts import build_system_prompt

        system_ext = build_system_prompt(profile, platform)
    except ImportError:
        pass

    # -- Credentials --------------------------------------------------------
    sensitive_data = _resolve_sensitive_data(app_settings, embedded_credentials, platform=platform)

    # -- Browser ------------------------------------------------------------
    cdp_url = args.cdp_url or os.environ.get("GH_CDP_URL")
    desktop_owns_browser = cdp_url is not None

    if cdp_url:
        browser_profile = BrowserProfile(keep_alive=True)
        browser = BrowserSession(browser_profile=browser_profile, cdp_url=cdp_url)
        print(f"Connecting to Desktop-owned browser via CDP: {cdp_url}")
    else:
        browser_profile = BrowserProfile(
            headless=args.headless,
            keep_alive=True,
            aboutblank_loading_logo_enabled=True,
            demo_mode=False,
            interaction_highlight_color="rgb(37, 99, 235)",
        )
        browser = BrowserSession(browser_profile=browser_profile)

    # -- Task prompt --------------------------------------------------------
    from ghosthands.agent.prompts import build_task_prompt

    task = build_task_prompt(args.job_url, resume_path, sensitive_data)

    # -- Agent --------------------------------------------------------------
    available_files = [resume_path] if resume_path else []
    agent = Agent(
        task=task,
        llm=llm,
        browser_session=browser,
        tools=tools,
        extend_system_message=system_ext or None,
        sensitive_data=sensitive_data,
        available_file_paths=available_files or None,
        use_vision=True,
        max_actions_per_step=5,
        calculate_cost=True,
        use_judge=False,
    )

    print()
    print("=" * 60)
    print(f"  URL:       {args.job_url}")
    print(f"  Platform:  {platform}")
    print(f"  Model:     {getattr(llm, 'model', '?')}")
    print(f"  Resume:    {resume_path or '(none)'}")
    print(f"  Headless:  {args.headless}")
    print(f"  CDP URL:   {cdp_url or '(launching own browser)'}")
    print(f"  Max steps: {args.max_steps}")
    proxy_url = os.environ.get("GH_LLM_PROXY_URL", "")
    print(f"  LLM:       {'Proxy: ' + proxy_url if proxy_url else 'Direct API'}")
    print("=" * 60)
    print()

    history = await agent.run(max_steps=args.max_steps)

    print()
    print("=" * 60)
    print("  RESULT")
    print("=" * 60)
    print(f"  Done:    {history.is_done()}")
    print(f"  Steps:   {len(history.history) if history.history else 0}")
    if history.usage:
        print(f"  Cost:    ${history.usage.total_cost:.4f}")
        print(f"  Tokens:  {history.usage.total_prompt_tokens} in / {history.usage.total_completion_tokens} out")
    result = history.final_result()
    if result:
        print(f"  Output:  {result[:500]}")
    print("=" * 60)
    print()
    print("  Browser is still open -- review the application before submitting.")
    print("  Press Ctrl+C to close when done.")
    print()

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nClosing browser...")
        await _cleanup_browser(browser, desktop_owns_browser)


# ── Entry point ───────────────────────────────────────────────────────


def main() -> None:
    # S-08: Install SIGTERM handler so the process exits cleanly when the
    # desktop app terminates the child process.  SystemExit is caught by
    # the existing KeyboardInterrupt/Exception handlers in both
    # run_agent_jsonl and run_agent_human.
    def _handle_sigterm(signum: int, frame: object) -> None:
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    args = parse_args()

    is_jsonl = args.output_format == "jsonl"

    # Install stdout guard BEFORE any library imports in JSONL mode.
    # This saves the real stdout fd for JSONL and redirects sys.stdout
    # to stderr so stray print() calls from any library are safe.
    if is_jsonl:
        from ghosthands.output.jsonl import emit_handshake, install_stdout_guard

        install_stdout_guard()
        emit_handshake()

    _setup_logging()

    runner = run_agent_jsonl if is_jsonl else run_agent_human

    try:
        asyncio.run(runner(args))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        if is_jsonl:
            from ghosthands.output.jsonl import emit_error

            logger.error("fatal_startup_error", error=str(e))
            emit_error("Hand-X encountered a fatal error", fatal=True)
        else:
            print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
