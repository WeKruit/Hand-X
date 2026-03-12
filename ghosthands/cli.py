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
import concurrent.futures
import contextlib
import json
import logging
import os
import sys
import time as _time
from pathlib import Path
from typing import Any

import structlog

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
) -> dict[str, str] | None:
    """Resolve credentials from environment first, then deprecated CLI flags."""
    if args.email or args.password:
        logger.warning(
            "cli.credentials_flags_deprecated",
            detail="Use GH_EMAIL and GH_PASSWORD environment variables instead of --email/--password",
            has_email_flag=bool(args.email),
            has_password_flag=bool(args.password),
        )

    email = app_settings.email or args.email or ""
    password = app_settings.password or args.password or ""

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
    sensitive_data = _resolve_sensitive_data(args, app_settings)

    # -- Browser ------------------------------------------------------------
    browser_profile = BrowserProfile(headless=args.headless, keep_alive=True)
    browser = BrowserSession(browser_profile=browser_profile)

    # -- Task prompt --------------------------------------------------------
    task = _build_task_prompt(args.job_url, resume_path, sensitive_data)

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
        cancel_task = asyncio.create_task(_listen_for_cancel(agent, cancel_requested))
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

        if cancel_requested.is_set():
            emit_done(
                success=False,
                message="Job cancelled by user",
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
                fields_filled=total_steps,
                job_id=job_id,
                lease_id=lease_id,
                result_data=result_data,
            )
            emit_awaiting_review()
            await _wait_for_review_command(browser, job_id, lease_id)
        else:
            emit_done(
                success=False,
                message=blocker or final_result or "Agent did not complete successfully",
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
    sensitive_data = _resolve_sensitive_data(args, app_settings)

    # -- Browser ------------------------------------------------------------
    browser_profile = BrowserProfile(headless=args.headless, keep_alive=True)
    browser = BrowserSession(browser_profile=browser_profile)

    # -- Task prompt --------------------------------------------------------
    task = _build_task_prompt(args.job_url, resume_path, sensitive_data)

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


# ── Shared helpers ────────────────────────────────────────────────────


def _build_task_prompt(
    job_url: str,
    resume_path: str,
    sensitive_data: dict | None,
) -> str:
    """Build the task prompt for the agent."""
    task = (
        f"Go to {job_url} and fill out the job application form completely.\n"
        "\n"
        "CRITICAL -- Action Order:\n"
        "1. After navigating to the page, your FIRST action MUST be domhand_fill.\n"
        "2. After domhand_fill completes, review its output to see which fields were filled and which failed.\n"
        "3. For failed dropdowns/selects, use domhand_select.\n"
        f"4. For file uploads (resume), use domhand_upload or upload_file action with path: {resume_path}\n"
        "5. Only use generic browser-use actions (click, input_text) as a LAST RESORT.\n"
        "6. After all fields on the current page are filled, click Next/Continue/Save to advance.\n"
        "7. On each new page, call domhand_fill AGAIN as the first action.\n"
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
        "- Do NOT click the final Submit button. Stop at the review page and use the done action.\n"
        "- If anything pops up blocking the form, close it and continue.\n"
    )
    return task


_stdin_lock = asyncio.Lock()
_stdin_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="hand-x-stdin",
)
# Mark the worker thread as daemon so it dies with the process (R-4 fix)
_stdin_executor._thread_name_prefix = "hand-x-stdin"


async def _read_stdin_line(timeout: float | None = None) -> str:
    """Read a single line from stdin with optional timeout.

    Uses a module-level lock to ensure only one reader at a time (R-2 fix),
    and a dedicated daemon ThreadPoolExecutor so cancelled reads don't leak
    threads from the default pool (R-4 fix).
    """
    async with _stdin_lock:
        loop = asyncio.get_running_loop()

        try:
            fileno = sys.stdin.fileno()
            future: asyncio.Future[str] = loop.create_future()

            def _on_stdin_ready() -> None:
                if future.done():
                    return
                try:
                    future.set_result(sys.stdin.readline())
                except Exception as exc:
                    future.set_exception(exc)

            loop.add_reader(fileno, _on_stdin_ready)
            try:
                if timeout is None:
                    return await future
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                loop.remove_reader(fileno)
        except (AttributeError, NotImplementedError, OSError, ValueError):
            line_future = loop.run_in_executor(_stdin_executor, sys.stdin.readline)
            if timeout is None:
                return await line_future
            return await asyncio.wait_for(line_future, timeout=timeout)


async def _listen_for_cancel(
    agent: Any,
    cancel_requested: asyncio.Event | None = None,
) -> None:
    """Read stdin concurrently during agent run for cancel commands."""
    while not agent.state.stopped:
        try:
            line = await _read_stdin_line(timeout=1.0)
        except TimeoutError:
            continue
        except asyncio.CancelledError:
            raise
        except Exception:
            continue

        if not line:
            break

        line = line.strip()
        if not line:
            continue

        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue

        cmd_type = cmd.get("type")
        if cmd_type in {"cancel", "cancel_job"}:
            logger.info("cancel_command_received_from_stdin", command_type=cmd_type)
            if cancel_requested is not None:
                cancel_requested.set()
            agent.state.stopped = True
            break


async def _wait_for_review_command(browser, job_id: str, lease_id: str) -> None:
    """Wait for a command from Electron on stdin.

    Expected commands:
    - {"type": "complete_review"} -- user approved, close browser
    - {"type": "cancel_job"}     -- user cancelled, close browser
    - {"type": "cancel"}         -- user cancelled, close browser

    Times out after 30 minutes if no command is received.

    NOTE: Does NOT emit a second ``done`` event.  The main flow already
    emitted ``done`` before entering review.  Emitting another would
    confuse the Desktop App event handler (R-1 fix).
    """
    from ghosthands.output.jsonl import emit_error, emit_status

    review_timeout_seconds = 30 * 60  # 30 minutes
    start_time = _time.monotonic()

    try:
        while True:
            elapsed = _time.monotonic() - start_time
            remaining = review_timeout_seconds - elapsed
            if remaining <= 0:
                logger.warning("review_timeout_exceeded", timeout_seconds=review_timeout_seconds)
                emit_error(
                    "Review timed out after 30 minutes",
                    fatal=True,
                    job_id=job_id,
                )
                break

            try:
                line = await _read_stdin_line(timeout=min(remaining, 5.0))
            except TimeoutError:
                continue

            if not line:
                break  # stdin closed -- Electron died

            line = line.strip()
            if not line:
                continue

            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue

            cmd_type = cmd.get("type", "")

            if cmd_type == "complete_review":
                logger.info("review_completed", job_id=job_id, lease_id=lease_id)
                emit_status("Review complete -- closing browser", job_id=job_id)
                break
            elif cmd_type in {"cancel", "cancel_job"}:
                logger.info("review_cancelled", job_id=job_id, lease_id=lease_id)
                emit_status("Review cancelled by user", job_id=job_id)
                break
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        with contextlib.suppress(Exception):
            await browser.close()


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
