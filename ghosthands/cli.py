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
import sys
from pathlib import Path
from typing import Any

import structlog

from ghosthands.agent.prompts import build_task_prompt
from ghosthands.bridge.profile_adapter import (
    camel_to_snake_profile,
    normalize_profile_defaults,
)
from ghosthands.bridge.protocol import (
    listen_for_cancel,
    wait_for_review_command,
)

# Force unbuffered I/O for reliable JSONL streaming
os.environ["PYTHONUNBUFFERED"] = "1"

# Suppress browser-use's own logging setup so we control stderr exclusively
os.environ["BROWSER_USE_SETUP_LOGGING"] = "false"

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
    parser.add_argument("--email", default=None, help="Login email (deprecated; prefer GH_EMAIL)")
    parser.add_argument("--password", default=None, help="Login password (deprecated; prefer GH_PASSWORD)")
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
    # --profile takes precedence (inline JSON or @filepath)
    if args.profile:
        raw = args.profile
        if raw.startswith("@"):
            path = Path(raw[1:])
            if not path.exists():
                raise FileNotFoundError(f"Profile file not found: {path}")
            return json.loads(path.read_text())
        return json.loads(raw)

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

            return load_resume_from_file(str(path))
        except Exception:
            return data

    # Environment variable fallback (for desktop bridge)
    profile_text = os.environ.get("GH_USER_PROFILE_TEXT", "")
    if profile_text:
        return json.loads(profile_text)

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

    resume_path = str(Path(args.resume).resolve()) if args.resume else ""
    if resume_path:
        os.environ["GH_RESUME_PATH"] = resume_path

    return resume_path


def _load_runtime_settings():
    """Load settings after CLI-provided environment overrides are applied."""
    from ghosthands.config.settings import Settings

    return Settings()


def _resolve_sensitive_data(
    args: argparse.Namespace,
    app_settings,
    embedded_credentials: dict[str, Any] | None = None,
    platform: str = "generic",
) -> dict[str, str] | None:
    """Resolve credentials with priority: profile creds > env vars > CLI flags.

    When the Desktop app embeds a ``credentials`` key in the profile JSON,
    we resolve platform-specific credentials first, then fall back to
    ``generic``, then ``GH_EMAIL``/``GH_PASSWORD`` env vars, then the
    deprecated ``--email``/``--password`` CLI flags.

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
    if args.email or args.password:
        logger.warning(
            "cli.credentials_flags_deprecated",
            detail="Use GH_EMAIL and GH_PASSWORD environment variables instead of --email/--password",
            has_email_flag=bool(args.email),
            has_password_flag=bool(args.password),
        )

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
    # Priority 4: deprecated CLI flags
    email = creds_email or app_settings.email or args.email or ""
    password = creds_password or app_settings.password or args.password or ""

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


# ── JSONL agent run ───────────────────────────────────────────────────


async def run_agent_jsonl(args: argparse.Namespace) -> None:
    """Run the agent with JSONL event output on stdout."""
    from ghosthands.output.jsonl import (
        emit_awaiting_review,
        emit_browser_ready,
        emit_cost,
        emit_done,
        emit_error,
        emit_status,
    )

    # -- Load profile -------------------------------------------------------
    try:
        profile = _load_profile(args)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        emit_error(f"Failed to load profile: {e}", fatal=True)
        sys.exit(1)

    # -- Convert camelCase keys from Desktop bridge to snake_case ----------
    profile = camel_to_snake_profile(profile)

    # -- Extract embedded credentials before they leak into env/profile ----
    # We pop them so they don't end up in GH_USER_PROFILE_TEXT env var.
    embedded_credentials = profile.pop("credentials", None)

    # -- Normalize profile defaults for DomHand ----------------------------
    profile = normalize_profile_defaults(profile)

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
    sensitive_data = _resolve_sensitive_data(args, app_settings, embedded_credentials, platform=platform)

    # -- Browser ------------------------------------------------------------
    browser_profile = BrowserProfile(headless=args.headless, keep_alive=True)
    browser = BrowserSession(browser_profile=browser_profile)

    # -- Task prompt --------------------------------------------------------
    task = build_task_prompt(args.job_url, resume_path, sensitive_data)

    emit_status(
        f"Starting application: {args.job_url}",
        step=1,
        max_steps=args.max_steps,
        job_id=job_id,
    )

    # -- Step hooks for live JSONL events -----------------------------------
    async def _on_step_start(ag: Agent) -> None:
        step = ag.state.n_steps
        goal = ""
        if ag.state.last_model_output:
            goal = ag.state.last_model_output.next_goal or ""
        emit_status(
            goal or f"Step {step}...",
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
            await browser.close()
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
            emit_done(
                success=True,
                message="Application filled -- browser open for review",
                fields_filled=filled_count,
                fields_failed=failed_count,
                job_id=job_id,
                lease_id=lease_id,
                result_data=result_data,
            )
            emit_awaiting_review()
            await wait_for_review_command(browser, job_id, lease_id)
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
            await browser.close()
            sys.exit(1)

    except Exception as e:
        emit_error(str(e), fatal=True, job_id=job_id)
        with contextlib.suppress(Exception):
            await browser.close()
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
    sensitive_data = _resolve_sensitive_data(args, app_settings, embedded_credentials, platform=platform)

    # -- Browser ------------------------------------------------------------
    browser_profile = BrowserProfile(headless=args.headless, keep_alive=True)
    browser = BrowserSession(browser_profile=browser_profile)

    # -- Task prompt --------------------------------------------------------
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
        await browser.close()


# ── Entry point ───────────────────────────────────────────────────────


def main() -> None:
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

            emit_error(str(e), fatal=True)
        else:
            print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
