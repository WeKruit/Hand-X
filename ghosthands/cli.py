"""Hand-X CLI -- entry point for the bundled desktop binary.

This module is the interface between the Electron desktop app and the
browser-use agent.  Communication happens via stdio:

- stdout -> JSONL events (ProgressEvent-compatible)
- stderr -> structured logging
- stdin  -> commands from Electron (complete_review, cancel_job)

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
import uuid
from pathlib import Path

# Force unbuffered I/O for reliable JSONL streaming
os.environ["PYTHONUNBUFFERED"] = "1"

# Suppress browser-use's own logging setup so we control stderr exclusively
os.environ["BROWSER_USE_SETUP_LOGGING"] = "false"

from ghosthands.agent.hooks import install_same_tab_guard
from ghosthands.config.settings import settings

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
    parser.add_argument("--email", default=None, help="Login email")
    parser.add_argument("--password", default=None, help="Login password")
    parser.add_argument(
        "--output-format",
        choices=["jsonl", "human"],
        default="jsonl",
        help="Output format: jsonl for IPC, human for terminal (default: jsonl)",
    )

    # VALET proxy
    parser.add_argument("--proxy-url", default=None, help="VALET LLM proxy URL")
    parser.add_argument("--runtime-grant", default=None, help="VALET runtime grant token")

    # Security
    parser.add_argument(
        "--allowed-domains",
        type=str,
        default=None,
        help="Comma-separated list of additional allowed domains",
    )

    # Playwright
    parser.add_argument("--browsers-path", default=None, help="Path to Playwright browser binaries")

    # Browser engine
    parser.add_argument(
        "--engine",
        choices=["chromium", "firefox", "auto"],
        default="auto",
        help="Browser engine to use: chromium, firefox (Camoufox), or auto (route-based selection, default)",
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

    raise ValueError("Either --profile or --test-data is required")


# ── JSONL agent run ───────────────────────────────────────────────────


async def run_agent_jsonl(args: argparse.Namespace) -> None:
    """Run the agent with JSONL event output on stdout."""
    from ghosthands.output.jsonl import (
        emit_browser_ready,
        emit_cost,
        emit_done,
        emit_error,
        emit_handshake,
        emit_lease_acquired,
        emit_lease_released,
        emit_status,
    )

    # -- Lease ID resolution ---------------------------------------------------
    lease_id = args.lease_id or str(uuid.uuid4())

    # -- Protocol handshake (must be the very first event) ----------------------
    emit_handshake()

    emit_status("Hand-X engine initialized", job_id=args.job_id)

    # -- Preflight: load profile, set env, import deps -------------------------
    try:
        # -- Load profile -------------------------------------------------------
        profile = _load_profile(args)

        # -- Set env vars -------------------------------------------------------
        if args.proxy_url:
            os.environ["GH_LLM_PROXY_URL"] = args.proxy_url
        if args.runtime_grant:
            os.environ["GH_LLM_RUNTIME_GRANT"] = args.runtime_grant
        if args.browsers_path:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = args.browsers_path

        os.environ["GH_USER_PROFILE_TEXT"] = json.dumps(profile, indent=2)
        os.environ["GH_USER_PROFILE_JSON"] = json.dumps(profile)
        if args.resume:
            os.environ["GH_RESUME_PATH"] = str(Path(args.resume).resolve())

        # -- Install DomHand field event callback --------------------------------
        from ghosthands.output import field_events

        field_events.install_jsonl_callback()

        # -- Import heavy deps after env setup ----------------------------------
        from browser_use import Agent, BrowserProfile, BrowserSession, Tools
        from ghosthands.llm.client import get_chat_model

        emit_status("Setting up agent...", job_id=args.job_id)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        emit_error(f"Preflight failed: {e}", fatal=True)
        emit_lease_released(lease_id, reason="error")
        sys.exit(1)
    except Exception as e:
        emit_error(f"Preflight failed: {e}", fatal=True)
        emit_lease_released(lease_id, reason="error")
        sys.exit(1)

    # -- Lease acquired (after all preflight succeeds) -------------------------
    emit_lease_acquired(lease_id, job_id=args.job_id)

    # -- Init block (LLM, tools, browser, agent) ----------------------------
    # Wrapped in try/except so lease_released is always emitted on failure.
    try:
        llm = get_chat_model(model=args.model)

        # -- DomHand actions ----------------------------------------------------
        tools: Tools = Tools()
        try:
            from ghosthands.actions import register_domhand_actions

            register_domhand_actions(tools)
            emit_status("DomHand actions registered", job_id=args.job_id)
        except Exception as e:
            emit_status(f"DomHand unavailable: {e}, using generic actions", job_id=args.job_id)

        # -- Platform detection -------------------------------------------------
        platform = "generic"
        try:
            from ghosthands.platforms import detect_platform

            platform = detect_platform(args.job_url)
        except ImportError:
            pass

        # -- Domain lockdown ----------------------------------------------------
        from ghosthands.security.domain_lockdown import create_lockdown_for_platform

        additional_domains: list[str] | None = None
        if args.allowed_domains:
            additional_domains = [d.strip() for d in args.allowed_domains.split(",") if d.strip()]

        lockdown = create_lockdown_for_platform(
            job_url=args.job_url,
            platform=platform,
            additional_domains=additional_domains,
        )
        allowed_domains = lockdown.get_allowed_domains()

        # -- System prompt ------------------------------------------------------
        system_ext = ""
        try:
            from ghosthands.agent.prompts import build_system_prompt

            system_ext = build_system_prompt(profile, platform)
        except ImportError:
            pass

        # -- Credentials --------------------------------------------------------
        sensitive_data: dict[str, str | dict[str, str]] | None = None
        if args.email and args.password:
            sensitive_data = {"email": args.email, "password": args.password}

        # -- Engine selection ---------------------------------------------------
        engine = args.engine
        if engine == "auto":
            from browser_use.browser.providers.route_selector import RouteSelector

            engine = RouteSelector.select_engine(args.job_url, platform)
            emit_status(f"Auto-selected browser engine: {engine}", job_id=args.job_id)

        # TODO: wire CamoufoxProvider when ready
        if engine == "firefox":
            emit_status(
                "Firefox/Camoufox engine is not yet wired — falling back to chromium",
                job_id=args.job_id,
            )
            engine = "chromium"

        # -- Browser ------------------------------------------------------------
        browser_profile = BrowserProfile(
            headless=args.headless,
            keep_alive=True,
            aboutblank_loading_logo_enabled=True,
            demo_mode=False,
            interaction_highlight_color="rgb(37, 99, 235)",
            wait_between_actions=settings.wait_between_actions,
            allowed_domains=allowed_domains,
            engine=engine,
        )
        browser = BrowserSession(browser_profile=browser_profile)

        # -- Task prompt --------------------------------------------------------
        resume_path = str(Path(args.resume).resolve()) if args.resume else ""
        task = _build_task_prompt(args.job_url, resume_path, sensitive_data, platform)

        # -- Create agent -------------------------------------------------------
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
            max_actions_per_step=settings.agent_max_actions_per_step,
            calculate_cost=True,
            use_judge=False,
        )

        emit_status(
            f"Starting application: {args.job_url}",
            step=1,
            max_steps=args.max_steps,
            job_id=args.job_id,
        )

        # -- Step hooks for live JSONL events -----------------------------------
        _browser_ready_emitted = False

        async def _on_step_start(ag: Agent) -> None:
            nonlocal _browser_ready_emitted
            await install_same_tab_guard(ag)

            # Emit browser_ready once, on the first step (browser is now live)
            if not _browser_ready_emitted:
                _browser_ready_emitted = True
                cdp_url = getattr(ag.browser_session, "cdp_url", "") or ""
                emit_browser_ready(cdp_url)

            step = ag.state.n_steps
            goal = ""
            if ag.state.last_model_output:
                goal = ag.state.last_model_output.next_goal or ""
            emit_status(
                goal or f"Step {step}...",
                step=step,
                max_steps=args.max_steps,
                job_id=args.job_id,
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
                emit_error("Budget exceeded", fatal=False, job_id=args.job_id)

    except Exception as e:
        emit_error(str(e), fatal=True, job_id=args.job_id)
        emit_lease_released(lease_id, reason="error")
        sys.exit(1)

    # -- Run ----------------------------------------------------------------
    try:
        history = await agent.run(
            max_steps=args.max_steps,
            on_step_start=_on_step_start,
            on_step_end=_on_step_end,
        )

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
            emit_status(
                "Application filled -- browser open for review",
                job_id=args.job_id,
            )
            review_outcome = await _wait_for_review_command(browser, args.job_id, lease_id)
            emit_done(
                success=(review_outcome == "completed"),
                message="Review complete" if review_outcome == "completed" else "Job cancelled by user",
                fields_filled=total_steps,
                job_id=args.job_id,
                lease_id=lease_id,
                result_data=result_data,
            )
            emit_lease_released(lease_id, reason=review_outcome)
        else:
            emit_done(
                success=False,
                message=blocker or final_result or "Agent did not complete successfully",
                job_id=args.job_id,
                lease_id=lease_id,
                result_data=result_data,
            )
            emit_lease_released(lease_id, reason="failed")
            await browser.kill()
            sys.exit(1)

    except Exception as e:
        emit_error(str(e), fatal=True, job_id=args.job_id)
        emit_lease_released(lease_id, reason="error")
        with contextlib.suppress(Exception):
            await browser.kill()
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

    # -- Set env vars -------------------------------------------------------
    if args.proxy_url:
        os.environ["GH_LLM_PROXY_URL"] = args.proxy_url
    if args.runtime_grant:
        os.environ["GH_LLM_RUNTIME_GRANT"] = args.runtime_grant
    if args.browsers_path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = args.browsers_path

    os.environ["GH_USER_PROFILE_TEXT"] = json.dumps(profile, indent=2)
    os.environ["GH_USER_PROFILE_JSON"] = json.dumps(profile)
    if args.resume:
        os.environ["GH_RESUME_PATH"] = str(Path(args.resume).resolve())

    # -- Import after env setup ---------------------------------------------
    from browser_use import Agent, BrowserProfile, BrowserSession, Tools
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

    # -- Domain lockdown ----------------------------------------------------
    from ghosthands.security.domain_lockdown import create_lockdown_for_platform

    additional_domains: list[str] | None = None
    if args.allowed_domains:
        additional_domains = [d.strip() for d in args.allowed_domains.split(",") if d.strip()]

    lockdown = create_lockdown_for_platform(
        job_url=args.job_url,
        platform=platform,
        additional_domains=additional_domains,
    )
    allowed_domains = lockdown.get_allowed_domains()

    # -- System prompt ------------------------------------------------------
    system_ext = ""
    try:
        from ghosthands.agent.prompts import build_system_prompt

        system_ext = build_system_prompt(profile, platform)
    except ImportError:
        pass

    # -- Credentials --------------------------------------------------------
    sensitive_data: dict[str, str | dict[str, str]] | None = None
    if args.email and args.password:
        sensitive_data = {"email": args.email, "password": args.password}

    # -- Engine selection ---------------------------------------------------
    engine = args.engine
    if engine == "auto":
        from browser_use.browser.providers.route_selector import RouteSelector

        engine = RouteSelector.select_engine(args.job_url, platform)
        print(f"Auto-selected browser engine: {engine}")

    # TODO: wire CamoufoxProvider when ready
    if engine == "firefox":
        print("WARNING: Firefox/Camoufox engine is not yet wired — falling back to chromium")
        engine = "chromium"

    # -- Browser ------------------------------------------------------------
    browser_profile = BrowserProfile(
        headless=args.headless,
        keep_alive=True,
        aboutblank_loading_logo_enabled=True,
        demo_mode=False,
        interaction_highlight_color="rgb(37, 99, 235)",
        wait_between_actions=settings.wait_between_actions,
        allowed_domains=allowed_domains,
        engine=engine,
    )
    browser = BrowserSession(browser_profile=browser_profile)

    # -- Task prompt --------------------------------------------------------
    resume_path = str(Path(args.resume).resolve()) if args.resume else ""
    task = _build_task_prompt(args.job_url, resume_path, sensitive_data, platform)

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
        max_actions_per_step=settings.agent_max_actions_per_step,
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
        await browser.kill()


# ── Shared helpers ────────────────────────────────────────────────────


def _build_task_prompt(
    job_url: str,
    resume_path: str,
    sensitive_data: dict | None,
    platform: str,
) -> str:
    """Build the task prompt for the agent."""
    from ghosthands.agent.prompts import (
        FAIL_OVER_CUSTOM_WIDGET,
        FAIL_OVER_NATIVE_SELECT,
        build_completion_detection_text,
    )

    task = (
        f"Go to {job_url} and fill out the job application form completely.\n"
        "\n"
        "ACTION ORDER FOR FORM PAGES:\n"
        "1. On each form page, your FIRST action MUST be domhand_fill.\n"
        "2. Review domhand_fill output -- handle unresolved fields with domhand_select.\n"
        f"3. For file uploads (resume), use domhand_upload or upload_file with path: {resume_path}\n"
        "4. Only use generic browser-use actions (click, input_text) as a LAST RESORT.\n"
        "5. After all fields are filled, click Next/Continue/Save to advance.\n"
        "6. On each new form page, call domhand_fill AGAIN as the first action.\n"
        "\n"
        "ACTION ORDER FOR AUTH PAGES (Create Account / Sign In):\n"
        "Do NOT call domhand_fill on auth pages -- it uses the wrong email.\n"
        "Instead: (1) input credentials, (2) domhand_check_agreement to check\n"
        "the 'I agree' checkbox -- THIS IS REQUIRED or the button silently fails,\n"
        "(3) VERIFY the checkbox is checked, (4) domhand_click_button to submit.\n"
        "\n"
        "DROPDOWN RULE: After clicking a dropdown option, STOP and observe.\n"
        "Do NOT batch a dropdown click with 'Save and Continue' or any other action.\n"
        "Dropdowns may reveal sub-options that need a second selection.\n"
        f"If domhand_select returns {FAIL_OVER_NATIVE_SELECT}, do NOT click the native <select>.\n"
        "Use dropdown_options(index=...) to inspect the exact option text/value,\n"
        "then use select_dropdown(index=..., text=...) with the exact text/value.\n"
        f"If domhand_select returns {FAIL_OVER_CUSTOM_WIDGET}, stop retrying domhand_select,\n"
        "open the widget manually, search if supported, and click the final leaf option.\n"
        "For phone country code or phone type dropdowns, if the first term fails,\n"
        "try close variants like 'United States +1', 'United States', '+1',\n"
        "'USA', 'US', 'Mobile', and 'Cell' before giving up.\n"
        "For stubborn checkbox/radio/button controls, if the intended option still\n"
        "does not stick after 2 tries, click the currently selected option once to\n"
        "clear/reset stale state, then click the intended option again and verify\n"
        "the visible state changed.\n"
        "For text/date/search inputs that visibly contain the value but still show\n"
        "validation errors, focus the field and press Enter or Tab to commit it.\n"
        "Use domhand_assess_state before any large scroll, before clicking\n"
        "Next/Continue/Save, and before calling done(). Follow its unresolved\n"
        "field list and scroll_bias instead of doing a full-page reverification loop.\n"
        "\n"
        "COMPLETION STATES:\n"
        f"{build_completion_detection_text(platform)}\n"
        "\n"
        "When close to completion, keep memory and next_goal short.\n"
        "Do NOT re-verify the entire form once a terminal completion state is reached.\n"
        "\n"
        "Other rules:\n"
    )
    if sensitive_data:
        task += (
            "- Use the provided credentials to log in or create an account if needed. "
            "For Workday, fill email + password + confirm password on the Create Account page.\n"
        )
    else:
        task += "- If a login wall appears, report it as a blocker.\n"
    task += (
        "- Do NOT click the final Submit button. Use the completion-state rules above and stop with the done action when the page is review, confirmation, or an allowed presubmit_single_page state.\n"
        "- If anything pops up blocking the form, call domhand_close_popup first. Only fall back to Escape or coordinate clicks if that DOM-first popup close action fails.\n"
        "- Every non-consent applicant value must come from the provided user profile. If the profile does not provide it, leave it empty or unresolved.\n"
        "- Never invent placeholder personal info like John, Doe, or John Doe. Use the exact applicant identity from the provided profile only.\n"
        "- Keep working near the current unresolved section and continue downward. Do NOT scroll back to the top just to re-check earlier fields unless a specific earlier required field is visibly empty or invalid.\n"
    )
    return task


async def _wait_for_review_command(browser, job_id: str, lease_id: str) -> str:
    """Wait for a command from Electron on stdin.

    Expected commands:
    - {"event": "complete_review"} -- user approved, close browser
    - {"event": "cancel_job"}     -- user cancelled, close browser

    Returns:
        "completed", "cancelled", or "aborted".
    """
    loop = asyncio.get_event_loop()
    outcome = "aborted"

    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break  # stdin closed -- Electron died

            line = line.strip()
            if not line:
                continue

            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue

            cmd_type = cmd.get("event", "")

            if cmd_type == "complete_review":
                outcome = "completed"
                break
            elif cmd_type == "cancel_job":
                outcome = "cancelled"
                break
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        with contextlib.suppress(Exception):
            await browser.kill()

    return outcome


# ── Entry point ───────────────────────────────────────────────────────


def main() -> None:
    args = parse_args()

    is_jsonl = args.output_format == "jsonl"

    # Install stdout guard BEFORE any library imports in JSONL mode.
    # This saves the real stdout fd for JSONL and redirects sys.stdout
    # to stderr so stray print() calls from any library are safe.
    if is_jsonl:
        from ghosthands.output.jsonl import install_stdout_guard

        install_stdout_guard()

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
