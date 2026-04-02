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
import atexit
import contextlib
import json
import logging
import os
import re
import signal
import stat
import sys
import tempfile
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
    reset_hitl_state,
    wait_for_review_command,
)

# Backward-compatible alias retained for older internal imports/tests.
_camel_to_snake_profile = camel_to_snake_profile

# Force unbuffered I/O for reliable JSONL streaming
os.environ["PYTHONUNBUFFERED"] = "1"

# Suppress browser-use's own logging setup so we control stderr exclusively
os.environ["BROWSER_USE_SETUP_LOGGING"] = "false"

from ghosthands.agent.hooks import (
    consume_blocked_final_submit,
    install_final_submit_guard,
    install_same_tab_guard,
)

logger = structlog.get_logger()

# Pre-step budget guard: estimated cost of one agent step in USD.
# If remaining budget is less than this, the agent stops before the next step.
_STEP_COST_ESTIMATE = 0.10


def _profile_debug_enabled() -> bool:
    return os.getenv("GH_DEBUG_PROFILE_PASS_THROUGH") == "1"


def _profile_debug_preview(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "EMPTY"
    if len(text) <= 96:
        return text
    return f"{text[:93]}..."


async def _stabilize_account_created_marker(
    agent: Agent,
    marker_status: str | None,
    marker_note: str | None,
    marker_evidence: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Downgrade overly optimistic account-created markers until auth success is proven."""
    auth_state = await _probe_generated_auth_state(getattr(agent, "browser_session", None))
    if auth_state == "authenticated_or_application_resumed":
        return "active", marker_note, marker_evidence

    if auth_state == "native_login":
        return (
            "active",
            "Create Account succeeded and the page is now on the native Sign In form. "
            "This is the expected next step, not email verification.",
            "auth_marker_native_login_after_create",
        )

    if auth_state == "verification_required":
        return (
            "pending_verification",
            "Account likely exists, but email verification is still required.",
            "auth_marker_pending_verification",
        )

    if marker_status != "active":
        return marker_status, marker_note, marker_evidence

    if auth_state in {"still_create_account", "explicit_auth_error", "unknown_pending"}:
        logger.info(
            "cli.account_created_marker_downgraded",
            extra={"auth_state": auth_state, "from_status": marker_status},
        )
        note = "Account creation signals were observed, but the run has not proven a post-auth success state yet."
        return "pending_verification", note, "auth_marker_pending_post_auth_confirmation"

    return marker_status, marker_note, marker_evidence


async def _probe_generated_auth_state(browser_session) -> str | None:
    """Probe the current auth page to distinguish native sign-in from verification."""
    if browser_session is None:
        return None
    try:
        page = await browser_session.get_current_page()
        if page is None:
            return None
        state = await page.evaluate(
            r"""() => {
                const normalize = (text) => String(text || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const textNodes = Array.from(
                    document.querySelectorAll('h1, h2, h3, button, a, [role="button"], label, p, span, div')
                )
                    .map((el) => normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || ''))
                    .filter(Boolean);
                const hasText = (patterns) => textNodes.some((text) => patterns.some((pattern) => pattern.test(text)));

                const passwordCount = document.querySelectorAll('input[type="password"]').length;
                const emailCount = document.querySelectorAll(
                    'input[type="email"], input[name*="email" i], input[id*="email" i]'
                ).length;
                const confirmPasswordVisible =
                    passwordCount >= 2 ||
                    document.querySelector('[data-automation-id="verifyPassword"]') !== null;
                const verificationSignals = hasText([
                    /\bverify your account\b/,
                    /\bverification email\b/,
                    /\bconfirm your email\b/,
                    /\bcheck your inbox\b/,
                    /\bcheck your spam\b/,
                    /\bverify your email address\b/,
                    /\bverification code\b/,
                    /\bsecurity code\b/,
                    /\benter the code sent to\b/,
                ]);
                const explicitAuthError = hasText([
                    /\binvalid\b/,
                    /\bincorrect\b/,
                    /\bwrong email\b/,
                    /\bwrong password\b/,
                    /\baccount.*locked\b/,
                    /\bsign in failed\b/,
                ]);
                const signInSignals = hasText([/\bsign in\b/, /\blog in\b/, /\blogin\b/]);
                const createAccountSignals = hasText([/\bcreate account\b/, /\bregister\b/, /\bsign up\b/]);
                const applicationSignals = hasText([
                    /\bmy information\b/,
                    /\bmy experience\b/,
                    /\bapplication questions\b/,
                    /\bvoluntary disclosures\b/,
                    /\bself identify\b/,
                    /\breview\b/,
                    /\bsave and continue\b/,
                    /\bautofill with resume\b/,
                ]);

                let authState = 'unknown_pending';
                if (applicationSignals) {
                    authState = 'authenticated_or_application_resumed';
                } else if (verificationSignals) {
                    authState = 'verification_required';
                } else if (confirmPasswordVisible || createAccountSignals) {
                    authState = 'still_create_account';
                } else if (emailCount >= 1 && passwordCount >= 1 && signInSignals) {
                    authState = 'native_login';
                } else if (explicitAuthError) {
                    authState = 'explicit_auth_error';
                }

                return authState;
            }"""
        )
        return str(state or "unknown_pending")
    except Exception:
        logger.debug("cli.generated_auth_state_probe_failed", exc_info=True)
        return None


def _derive_workday_state_from_browser_text(browser_text: str) -> dict[str, Any]:
    """Derive Workday start/auth signals from BrowserSession's DOM snapshot text."""
    text = re.sub(r"\s+", " ", str(browser_text or "")).strip().lower()
    if not text:
        return {}

    def _has(patterns: list[str]) -> bool:
        return any(re.search(pattern, text) for pattern in patterns)

    def _has_button_label(label: str) -> bool:
        escaped = re.escape(label.lower())
        return bool(
            re.search(rf"\[\d+\]<button[^>]*>\s*{escaped}\b", text)
            or re.search(rf"\[\d+\]<a[^>]*role=button[^>]*>\s*{escaped}\b", text)
        )

    password_hits = len(re.findall(r"\bpassword\b", text))
    email_hits = len(re.findall(r"\bemail(?: address)?\b", text))

    return {
        "hasAcceptCookies": _has_button_label("Accept Cookies"),
        "hasApply": _has_button_label("Apply"),
        "hasAutofillWithResume": _has_button_label("Autofill with Resume"),
        "hasApplyWithResume": _has_button_label("Apply with Resume"),
        "resumeAutofillSignals": _has(
            [
                r"\bautofill with resume\b",
                r"\bapply with resume\b",
                r"\bupload resume\b",
                r"\bresume/cv\b",
                r"\bdrop files here\b",
                r"\bselect files\b",
            ]
        ),
        "emailCount": 1 if email_hits else 0,
        "passwordCount": min(password_hits, 2),
        "confirmPasswordVisible": _has([r"\bconfirm password\b", r"\bverify password\b"]),
        "signInSignals": _has([r"\bsign in\b", r"\blog in\b", r"\blogin\b"]),
        "createAccountSignals": _has([r"\bcreate account\b", r"\bregister\b", r"\bsign up\b"]),
        "verificationBanner": _has(
            [
                r"\ban email has been sent to you\b",
                r"\bplease verify your account\b",
                r"\bverification email\b",
                r"\bconfirm your email\b",
                r"\bcheck your inbox\b",
                r"\bcheck your spam\b",
                r"\bverify your email address\b",
            ]
        ),
        "verificationCodeRequired": _has(
            [
                r"\bverification code\b",
                r"\bsecurity code\b",
                r"\benter the code sent to\b",
                r"\bone time passcode\b",
                r"\botp\b",
            ]
        ),
        "explicitAuthError": _has(
            [
                r"\binvalid\b",
                r"\bincorrect\b",
                r"\bwrong email\b",
                r"\bwrong password\b",
                r"\baccount.*locked\b",
                r"\bsign in failed\b",
                r"\bthere was an error\b",
            ]
        ),
        "applicationSignals": _has(
            [
                r"\bmy information\b",
                r"\bmy experience\b",
                r"\bapplication questions\b",
                r"\bvoluntary disclosures\b",
                r"\bself identify\b",
                r"\breview\b",
                r"\bsave and continue\b",
            ]
        ),
    }


async def _inspect_workday_runtime_state(browser_session, page) -> dict[str, Any]:
    """Inspect lightweight Workday page state for deterministic auth handling."""
    derived_from_browser_text: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        derived_from_browser_text = _derive_workday_state_from_browser_text(
            await browser_session.get_state_as_text()
        )
    try:
        state = await page.evaluate(
            r"""() => {
                const normalize = (text) => String(text || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const textFrom = (el) => normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                const allText = Array.from(document.querySelectorAll('h1, h2, h3, button, a, [role="button"], label, p, span, div'))
                    .map(textFrom)
                    .filter(Boolean);
                const buttonTexts = Array.from(document.querySelectorAll('button, a, [role="button"]'))
                    .map(textFrom)
                    .filter(Boolean);
                const hasText = (patterns) => allText.some((text) => patterns.some((pattern) => pattern.test(text)));
                const hasButton = (patterns) => buttonTexts.some((text) => patterns.some((pattern) => pattern.test(text)));

                const passwordCount = document.querySelectorAll('input[type="password"]').length;
                const emailCount = document.querySelectorAll(
                    'input[type="email"], input[name*="email" i], input[id*="email" i]'
                ).length;
                const confirmPasswordVisible =
                    passwordCount >= 2 ||
                    document.querySelector('[data-automation-id="verifyPassword"]') !== null;
                const signInSignals = hasText([/\bsign in\b/, /\blog in\b/, /\blogin\b/]);
                const createAccountSignals = hasText([/\bcreate account\b/, /\bregister\b/, /\bsign up\b/]);
                const verificationBanner = hasText([
                    /\ban email has been sent to you\b/,
                    /\bplease verify your account\b/,
                    /\bverification email\b/,
                    /\bconfirm your email\b/,
                    /\bcheck your inbox\b/,
                    /\bcheck your spam\b/,
                    /\bverify your email address\b/,
                ]);
                const verificationCodeRequired =
                    hasText([/\bverification code\b/, /\bsecurity code\b/, /\benter the code sent to\b/, /\bone time passcode\b/, /\botp\b/]) ||
                    document.querySelector('input[inputmode="numeric"], input[autocomplete="one-time-code"]') !== null;
                const explicitAuthError = hasText([
                    /\binvalid\b/,
                    /\bincorrect\b/,
                    /\bwrong email\b/,
                    /\bwrong password\b/,
                    /\baccount.*locked\b/,
                    /\bsign in failed\b/,
                    /\bthere was an error\b/,
                ]);
                const applicationSignals = hasText([
                    /\bmy information\b/,
                    /\bmy experience\b/,
                    /\bapplication questions\b/,
                    /\bvoluntary disclosures\b/,
                    /\bself identify\b/,
                    /\breview\b/,
                    /\bsave and continue\b/,
                ]);
                const resumeAutofillSignals =
                    hasText([/\bautofill with resume\b/, /\bapply with resume\b/, /\bupload resume\b/, /\bresume\/cv\b/, /\bdrop files here\b/, /\bselect files\b/]) ||
                    document.querySelector('input[type="file"]') !== null;

                return {
                    hasAcceptCookies: hasButton([/\baccept cookies\b/]),
                    hasApply: hasButton([/^\bapply\b$/, /^\bapply now\b$/]),
                    hasAutofillWithResume: hasButton([/\bautofill with resume\b/]),
                    hasApplyWithResume: hasButton([/\bapply with resume\b/]),
                    resumeAutofillSignals,
                    emailCount,
                    passwordCount,
                    confirmPasswordVisible,
                    signInSignals,
                    createAccountSignals,
                    verificationBanner,
                    verificationCodeRequired,
                    explicitAuthError,
                    applicationSignals,
                };
            }"""
        )
        state = state if isinstance(state, dict) else {}
        for key, value in derived_from_browser_text.items():
            if isinstance(value, bool):
                state[key] = bool(state.get(key)) or value
            elif isinstance(value, int):
                state[key] = max(int(state.get(key) or 0), value)
        return state
    except Exception:
        logger.debug("cli.inspect_workday_runtime_state_failed", exc_info=True)
        return derived_from_browser_text


async def _poll_workday_runtime_state(
    browser_session,
    page,
    *,
    attempts: int = 5,
    sleep_seconds: float = 1.0,
) -> dict[str, Any]:
    """Wait briefly for Workday page signals before falling back to the agent loop."""
    last_state: dict[str, Any] = {}
    signal_keys = (
        "hasAcceptCookies",
        "hasApply",
        "hasAutofillWithResume",
        "hasApplyWithResume",
        "emailCount",
        "passwordCount",
        "confirmPasswordVisible",
        "verificationBanner",
        "verificationCodeRequired",
        "applicationSignals",
        "explicitAuthError",
    )
    for attempt in range(max(1, attempts)):
        last_state = await _inspect_workday_runtime_state(browser_session, page)
        if any(bool(last_state.get(key)) for key in signal_keys):
            return last_state
        if attempt + 1 < max(1, attempts):
            await asyncio.sleep(sleep_seconds)
    return last_state


def _workday_is_pure_verification_blocker(state: dict[str, Any]) -> bool:
    """True only when Workday requires verification without an actionable sign-in form."""
    if not state:
        return False
    if bool(state.get("applicationSignals")):
        return False
    has_signin_form = int(state.get("emailCount") or 0) >= 1 and int(state.get("passwordCount") or 0) >= 1
    if has_signin_form and not bool(state.get("confirmPasswordVisible")):
        return False
    return bool(state.get("verificationCodeRequired")) or bool(state.get("verificationBanner"))


async def _fill_first_matching_input(page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            elements = await page.get_elements_by_css_selector(selector)
        except Exception:
            elements = []
        if not elements:
            continue
        try:
            await elements[0].fill(value, clear=True)
            return True
        except Exception:
            continue
    return False


async def _fill_password_fields(page, password: str, *, count: int = 1) -> bool:
    """Fill up to ``count`` visible password fields on the current page."""
    try:
        elements = await page.get_elements_by_css_selector('input[type="password"]')
    except Exception:
        elements = []
    if not elements:
        return False

    filled = 0
    for element in elements[: max(1, count)]:
        try:
            await element.fill(password, clear=True)
            filled += 1
        except Exception:
            continue
    return filled >= max(1, count)


async def _click_button_label_if_present(browser_session, label: str) -> bool:
    from ghosthands.actions.domhand_click_button import DomHandClickButtonParams, domhand_click_button

    result = await domhand_click_button(DomHandClickButtonParams(button_label=label), browser_session)
    return bool(result and not result.error)


def _build_current_page_continuation_task(
    original_task: str,
    *,
    platform: str,
    stage: str = "current_page",
) -> str:
    """Rewrite the task so the agent continues from the current authenticated page."""
    replacement = (
        "Continue from the CURRENT page and fill out the job application from here. "
        "Do NOT navigate back to the original job URL or repeat the auth/start-dialog flow.\n"
    )
    rewritten = re.sub(
        r"^Go to .*? and fill out the job application form completely\.\n",
        replacement,
        original_task,
        count=1,
        flags=re.DOTALL,
    )
    if rewritten == original_task:
        rewritten = f"{replacement}\n{original_task}"
    if platform == "workday":
        if stage == "auth_complete":
            prefix = "WORKDAY AUTH IS ALREADY COMPLETE FOR THIS RUN. Stay on the CURRENT page.\n"
        elif stage == "auth_page":
            prefix = (
                "WORKDAY START FLOW HAS ALREADY REACHED THE NATIVE AUTH PAGE FOR THIS RUN. "
                "Stay on the CURRENT page and continue from there.\n"
            )
        elif stage == "resume_autofill":
            prefix = (
                "WORKDAY START FLOW IS ALREADY OPEN ON THE AUTOFILL WITH RESUME PAGE. "
                "Stay on the CURRENT page and continue from there.\n"
            )
        else:
            prefix = "WORKDAY START FLOW IS ALREADY OPEN FOR THIS RUN. Stay on the CURRENT page.\n"
        rewritten = prefix + rewritten
    return rewritten


async def _run_workday_auth_preface(
    browser_session,
    *,
    job_url: str,
    app_settings,
    platform: str,
) -> tuple[str, str]:
    """Deterministically execute Workday auth/start-dialog steps before the agent loop.

    Returns (status, message):
    - ("continue", msg): browser is positioned on the post-auth/current application page
    - ("blocker", msg): deterministic blocker reached before form filling
    - ("noop", msg): no deterministic preface action was applicable
    """
    if not app_settings or platform != "workday":
        return "noop", "non-workday"
    if not app_settings.email or not app_settings.password:
        return "noop", "missing_credentials"

    page = await browser_session.get_current_page()
    if page is None:
        page = await browser_session.new_page(job_url)
    else:
        current = ""
        with contextlib.suppress(Exception):
            current = await page.get_url()
        if not current or current == "about:blank":
            await page.goto(job_url)
    state = await _poll_workday_runtime_state(browser_session, page, attempts=8)

    if state.get("hasAcceptCookies"):
        await _click_button_label_if_present(browser_session, "Accept Cookies")
        state = await _poll_workday_runtime_state(browser_session, page, attempts=8)

    if state.get("hasApply"):
        await _click_button_label_if_present(browser_session, "Apply")
        state = await _poll_workday_runtime_state(browser_session, page, attempts=8)

    if state.get("hasAutofillWithResume"):
        await _click_button_label_if_present(browser_session, "Autofill with Resume")
        state = await _poll_workday_runtime_state(browser_session, page, attempts=12)
    elif state.get("hasApplyWithResume"):
        await _click_button_label_if_present(browser_session, "Apply with Resume")
        state = await _poll_workday_runtime_state(browser_session, page, attempts=12)

    if bool(state.get("resumeAutofillSignals")) and not (
        int(state.get("emailCount") or 0) >= 1 or int(state.get("passwordCount") or 0) >= 1
    ):
        return "continue_resume_autofill", "workday preface reached the Autofill with Resume page"
    if (
        bool(state.get("createAccountSignals"))
        or bool(state.get("signInSignals"))
        or bool(state.get("confirmPasswordVisible"))
        or int(state.get("emailCount") or 0) >= 1
        or int(state.get("passwordCount") or 0) >= 1
    ):
        return "continue_auth_page", "workday preface reached the native auth page"
    if bool(state.get("applicationSignals")):
        return "continue_auth_complete", "workday auth preface completed"
    return "noop", "no deterministic workday preface transition"


async def _should_resume_from_false_verification_blocker(
    final_result: str,
    *,
    platform: str,
    app_settings,
    browser_session,
) -> bool:
    """Return True when a verification blocker is false and the page is native sign-in."""
    if platform != "workday":
        return False
    if not (
        app_settings
        and (
            app_settings.credential_source == "generated"
            or (app_settings.credential_source == "user" and app_settings.credential_intent == "create_account")
        )
    ):
        return False
    if "email verification required" not in str(final_result or "").lower():
        return False
    auth_state = await _probe_generated_auth_state(browser_session)
    return auth_state == "native_login"


# ── Argument parsing ──────────────────────────────────────────────────


def _handle_smoke_test_import() -> None:
    """Handle --smoke-test-import early, before argparse (which requires --job-url)."""
    argv = sys.argv[1:]
    if "--smoke-test-import" in argv:
        idx = argv.index("--smoke-test-import")
        if idx + 1 < len(argv):
            module_name = argv[idx + 1]
            try:
                __import__(module_name)
                sys.exit(0)
            except ImportError:
                sys.exit(1)
        sys.exit(1)


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

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__import__('ghosthands').__version__}",
    )

    # Required
    parser.add_argument("--job-url", required=True, help="Job posting URL to apply to")

    # Profile source (one of these is required)
    parser.add_argument("--profile", default=None, help="Applicant profile as JSON string or @filepath")
    parser.add_argument("--test-data", default=None, help="Path to applicant data JSON file")
    parser.add_argument("--user-id", default=None, help="VALET user UUID for DB-backed profile loading")
    parser.add_argument(
        "--resume-id",
        default=None,
        help="Specific VALET resume UUID to use when loading a DB-backed profile",
    )

    # Optional
    parser.add_argument("--resume", default=None, help="Path to resume PDF")
    parser.add_argument("--job-id", default="", help="Job ID for event tracking")
    parser.add_argument("--lease-id", default="", help="Lease ID for event tracking")
    parser.add_argument("--model", default=None, help="LLM model override")
    parser.add_argument("--max-steps", type=int, default=50, help="Max agent steps (default: 50)")
    parser.add_argument("--max-budget", type=float, default=0.50, help="Max LLM budget USD")
    parser.add_argument(
        "--submit-intent",
        choices=["review", "submit"],
        default=None,
        help="Whether to stop at review (default) or explicitly allow final submit",
    )
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

    # Security
    parser.add_argument(
        "--allowed-domains",
        type=str,
        default=None,
        help="Comma-separated list of additional allowed domains",
    )

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


class _CompactFormatter(logging.Formatter):
    """Shorten noisy logger names while keeping everything else intact."""

    _REWRITES = {
        "Agent": "Agent",
        "BrowserSession": "Session",
        "tools": "tools",
        "dom": "dom",
    }

    def format(self, record: logging.LogRecord) -> str:
        name = record.name
        if isinstance(name, str) and name.startswith("browser_use."):
            for fragment, short in self._REWRITES.items():
                if fragment in name:
                    record.name = short
                    break
            else:
                parts = name.split(".")
                if len(parts) >= 2:
                    record.name = parts[-1]
        return super().format(record)


def _setup_logging() -> None:
    """Route ALL logging to stderr so stdout stays clean."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_CompactFormatter("%(levelname)s [%(name)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    for noisy in ("google_genai.models", "google_genai", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

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


def _validate_profile_object(profile: Any) -> dict:
    """Assert the parsed value is a JSON object (dict), not a list or scalar."""
    if not isinstance(profile, dict):
        raise ValueError("Profile must be a JSON object")
    return profile


async def _load_profile_async(args: argparse.Namespace) -> dict:
    """Load applicant profile from CLI/env sources for async callers."""

    # --profile takes precedence (inline JSON or @filepath)
    if args.profile:
        raw = args.profile
        if raw.startswith("@"):
            path = Path(raw[1:])
            if not path.exists():
                raise FileNotFoundError(f"Profile file not found: {path}")
            return _validate_profile_object(json.loads(path.read_text()))
        return _validate_profile_object(json.loads(raw))

    user_id = getattr(args, "user_id", None) or os.environ.get("GH_USER_ID", "").strip() or None
    resume_id = getattr(args, "resume_id", None) or os.environ.get("GH_RESUME_ID", "").strip() or None
    if resume_id and not user_id:
        raise ValueError("--resume-id requires --user-id")
    if user_id:
        # Prefer DB-direct (apply.sh has GH_DATABASE_URL).
        # Fall back to VALET API (Desktop passes proxy URL + runtime grant).
        db_url = os.environ.get("GH_DATABASE_URL", "").strip()
        if db_url:
            return _validate_profile_object(await _load_profile_from_user_resume_async(user_id, resume_id))
        # Desktop path: call VALET API → same _map_to_profile() normalization
        api_url = (os.environ.get("GH_LLM_PROXY_URL") or "").strip()
        grant = (os.environ.get("GH_LLM_RUNTIME_GRANT") or "").strip()
        if api_url and grant:
            from ghosthands.integrations.resume_loader import load_runtime_profile_from_api

            profile = await load_runtime_profile_from_api(api_url, grant, resume_id)
            # Overlay Desktop-only data (credentials, runtime learning) from GH_USER_PROFILE_TEXT
            _overlay = _load_desktop_overlay_fields()
            if _overlay:
                for k, v in _overlay.items():
                    if v and k not in profile:
                        profile[k] = v
            return _validate_profile_object(profile)

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

            return _validate_profile_object(load_resume_from_file(str(path)))
        except Exception:
            return _validate_profile_object(data)

    # File-based profile (preferred — avoids /proc/pid/environ exposure)
    profile_path = os.environ.get("GH_USER_PROFILE_PATH", "")
    if profile_path:
        p = Path(profile_path)
        if p.is_file():
            return _validate_profile_object(json.loads(p.read_text(encoding="utf-8")))

    # Environment variable fallback (for backwards compat with desktop bridge)
    profile_text = os.environ.get("GH_USER_PROFILE_TEXT", "")
    if profile_text:
        return _validate_profile_object(json.loads(profile_text))

    raise ValueError("Either --profile, --test-data, GH_USER_PROFILE_PATH, or GH_USER_PROFILE_TEXT env var is required")


def _load_desktop_overlay_fields() -> dict[str, Any] | None:
    """Extract Desktop-only fields (credentials, runtime learning) from GH_USER_PROFILE_TEXT.

    These fields are not in the VALET DB — they're ephemeral Desktop data
    that must be overlaid on top of the API-loaded profile.
    """
    raw = os.environ.get("GH_USER_PROFILE_TEXT", "").strip()
    if not raw:
        p = os.environ.get("GH_USER_PROFILE_PATH", "").strip()
        if p and Path(p).is_file():
            raw = Path(p).read_text(encoding="utf-8")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    overlay: dict[str, Any] = {}
    for key in (
        "credentials",
        "learnedQuestionAliases",
        "learned_question_aliases",
        "learnedInteractionRecipes",
        "learned_interaction_recipes",
    ):
        if key in data and data[key]:
            overlay[key] = data[key]
    return overlay if overlay else None


def _load_profile(args: argparse.Namespace) -> dict:
    """Load applicant profile from CLI/env sources for sync callers."""
    user_id = getattr(args, "user_id", None)
    resume_id = getattr(args, "resume_id", None)
    if resume_id and not user_id:
        raise ValueError("--resume-id requires --user-id")
    if user_id:
        return _validate_profile_object(_load_profile_from_user_resume(user_id, resume_id))
    return asyncio.run(_load_profile_async(args))


async def _load_profile_from_user_resume_async(
    user_id: str,
    resume_id: str | None = None,
) -> dict[str, Any]:
    """Load a runtime profile from VALET's database using user/resume IDs."""
    database_url = os.environ.get("GH_DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("GH_DATABASE_URL is required when using --user-id / --resume-id")

    from ghosthands.integrations.database import Database
    from ghosthands.integrations.resume_loader import load_runtime_profile

    db = Database(database_url)
    await db.connect()
    try:
        return await load_runtime_profile(db, user_id=user_id, resume_id=resume_id)
    finally:
        await db.close()


def _load_profile_from_user_resume(user_id: str, resume_id: str | None = None) -> dict[str, Any]:
    """Load a runtime profile from VALET's database using user/resume IDs."""
    return asyncio.run(_load_profile_from_user_resume_async(user_id, resume_id))


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
    if getattr(args, "submit_intent", None):
        os.environ["GH_SUBMIT_INTENT"] = args.submit_intent

    # Write profile to temp file instead of env var (avoids /proc/pid/environ exposure)
    profile_fd, profile_path = tempfile.mkstemp(prefix="gh_profile_", suffix=".json")
    try:
        os.fchmod(profile_fd, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        os.write(profile_fd, json.dumps(profile, indent=2).encode())
        os.close(profile_fd)
        os.environ["GH_USER_PROFILE_PATH"] = profile_path
        atexit.register(_cleanup_profile_tempfile)
    except Exception:
        os.close(profile_fd)
        os.unlink(profile_path)
        raise

    resume_path = str(Path(args.resume).resolve()) if args.resume else ""
    if resume_path:
        os.environ["GH_RESUME_PATH"] = resume_path

    return resume_path


def _cleanup_profile_tempfile() -> None:
    """Remove the temporary profile file created by _apply_runtime_env."""
    profile_path = os.environ.pop("GH_USER_PROFILE_PATH", "")
    if profile_path:
        with contextlib.suppress(OSError):
            os.unlink(profile_path)


def _load_runtime_settings():
    """Load settings after CLI-provided environment overrides are applied."""
    from ghosthands.config.settings import Settings

    return Settings()


def _resolve_sensitive_data(
    app_settings,
    embedded_credentials: dict[str, Any] | None = None,
    platform: str = "generic",
) -> dict[str, str] | None:
    """Resolve credentials with priority: user-provided env creds > profile creds > env vars.

    When the Desktop app embeds a ``credentials`` key in the profile JSON,
    we resolve platform-specific credentials first, then fall back to
    ``generic``, then ``GH_EMAIL``/``GH_PASSWORD`` env vars.

    Exception: when ``credential_source == "user"``, the local GH_EMAIL /
    GH_PASSWORD override is authoritative and must win over any embedded
    profile credentials. This is required for fresh-account testing where the
    applicant profile email differs from the auth email.

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

    prefer_user_env = str(getattr(app_settings, "credential_source", "") or "").strip().lower() == "user"

    if prefer_user_env:
        email = app_settings.email or creds_email or ""
        password = app_settings.password or creds_password or ""
    else:
        # Priority 3: env vars (GH_EMAIL / GH_PASSWORD via app_settings)
        email = creds_email or app_settings.email or ""
        password = creds_password or app_settings.password or ""

    if email and password:
        return {"email": email, "password": password}
    return None


def _log_auth_debug_credentials(
    sensitive_data: dict[str, str] | None,
    *,
    platform: str,
) -> None:
    """Emit plaintext auth credentials during local debugging when explicitly enabled."""
    if os.environ.get("GH_DEBUG_AUTH_CREDENTIALS") != "1":
        return

    email = (sensitive_data or {}).get("email", "")
    password = (sensitive_data or {}).get("password", "")
    print(
        f"[AUTH_DEBUG][cli] platform={platform} email={email or 'EMPTY'} password={password or 'EMPTY'} "
        f"password_length={len(password)}",
        file=sys.stderr,
        flush=True,
    )
    logger.warning(
        "auth.debug_credentials",
        platform=platform,
        email=email or "EMPTY",
        password=password or "EMPTY",
        password_length=len(password),
    )


def _infer_account_created_marker_from_text(
    combined_text: str,
) -> tuple[str | None, str | None, str | None]:
    """Infer a conservative account-created marker from agent memory/eval text.

    Only explicit AUTH_RESULT markers or concrete verification-language heuristics
    should emit account-created events. Generic phrases like "new account" or
    "create account" are too weak and cause false positives while the agent is
    merely navigating to the registration form.
    """
    combined = (combined_text or "").lower()

    if "auth_result=account_created_pending_verification" in combined:
        return (
            "pending_verification",
            "Account likely exists, but email verification is still required.",
            "auth_marker_pending_verification",
        )
    if "auth_result=account_created_active" in combined:
        return (
            "active",
            "Account creation succeeded and the run moved past the auth wall.",
            "auth_marker_active",
        )

    verification_signals = (
        "email verification required",
        "verify your account",
        "check your inbox",
        "confirm your email",
        "verification email",
    )
    if any(signal in combined for signal in verification_signals):
        return (
            "pending_verification",
            "Account likely exists, but email verification is still required.",
            "heuristic_pending_verification",
        )

    return None, None, None


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


async def _cleanup_browser(
    browser,
    desktop_owns_browser: bool,
    *,
    keep_browser_alive: bool = False,
) -> None:
    """Shut down the browser session with ownership-aware cleanup.

    When the Desktop app owns the browser (CDP mode), we only disconnect
    from the session via ``stop()`` — the browser process stays alive for
    the Desktop app to manage.

    When Hand-X launched the browser itself, Desktop/local-worker JSONL mode
    now leaves the browser entirely untouched so the user can keep inspecting
    the failed page. Only standalone human-mode runs should kill it here.
    """
    if keep_browser_alive:
        logger.info("browser.cleanup_detaching_keep_alive")
        await browser.detach_keep_alive()
        return
    if desktop_owns_browser:
        await browser.stop()
    else:
        await browser.kill()


@dataclass(frozen=True)
class _RuntimeErrorSignal:
    """User-facing error details for known proxy/runtime failures."""

    code: str
    message: str
    fatal: bool = True
    keep_browser_open: bool = False


@dataclass(frozen=True)
class _OpenQuestionIssue:
    field_label: str
    field_id: str | None = None
    field_type: str = "text"
    question_text: str | None = None
    section: str | None = None
    section_path: str | None = None
    current_value: str | None = None
    visible_error: str | None = None
    widget_kind: str | None = None
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RecoveredFieldAnswer:
    field_id: str
    field_label: str
    answer: str
    question_text: str | None = None
    section_path: str | None = None


def _normalize_issue_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9\s]", " ", str(value or "").strip().lower()).strip()


def _issue_recovery_key(issue: _OpenQuestionIssue, fallback_index: int) -> str:
    return str(issue.field_id or "").strip() or f"open-question-{fallback_index}"


def _issue_is_auth_like(issue: _OpenQuestionIssue) -> bool:
    label = _normalize_issue_text(issue.field_label or issue.question_text)
    if issue.field_type == "password":
        return True
    return any(token in label for token in ("email", "e mail", "username", "user name", "login", "password"))


def _issue_supports_answer_recovery(issue: _OpenQuestionIssue) -> bool:
    """Return True only for issues that should enter answer-recovery inference."""
    if _issue_is_auth_like(issue):
        return True

    label = _normalize_issue_text(issue.field_label or issue.question_text)
    field_type = str(issue.field_type or "").strip().lower()
    widget_kind = str(issue.widget_kind or "").strip().lower()
    if field_type in {
        "radio-group",
        "radio",
        "button-group",
        "checkbox-group",
        "checkbox",
        "toggle",
        "file",
        "password",
    }:
        return False
    if widget_kind in {"radio", "checkbox", "toggle", "button-group"}:
        return False
    if label.startswith(("have you", "are you", "do you", "did you", "will you", "can you", "were you", "would you")):
        return False
    if field_type in {"select", "text", "textarea", "search", "date", "number", "email", "tel", "url"}:
        return True
    return bool(issue.options)


def _auth_override_answer_for_issue(issue: _OpenQuestionIssue) -> str:
    email = (os.environ.get("GH_EMAIL") or "").strip()
    password = (os.environ.get("GH_PASSWORD") or "").strip()
    label = _normalize_issue_text(issue.question_text or issue.field_label)
    if "password" in label:
        return password
    if any(token in label for token in ("email", "e mail", "username", "user name", "login")):
        return email
    return ""


def _resolve_auth_recovery_answers(
    issues: list[_OpenQuestionIssue],
) -> tuple[list[_RecoveredFieldAnswer], list[_OpenQuestionIssue]]:
    resolved: list[_RecoveredFieldAnswer] = []
    unresolved: list[_OpenQuestionIssue] = []
    for index, issue in enumerate(issues, start=1):
        if not _issue_is_auth_like(issue):
            unresolved.append(issue)
            continue
        answer = _auth_override_answer_for_issue(issue)
        if not answer:
            unresolved.append(issue)
            continue
        resolved.append(
            _RecoveredFieldAnswer(
                field_id=_issue_recovery_key(issue, index),
                field_label=issue.field_label,
                answer=answer,
                question_text=issue.question_text,
                section_path=issue.section_path or issue.section,
            )
        )
    return resolved, unresolved


_ANSWER_NEEDED_BLOCKER_PATTERNS = (
    "missing from the applicant profile",
    "missing from the profile",
    "missing required field",
    "cannot invent",
    "cannot infer",
    "needs user input",
    "requires user input",
    "user must answer",
    "unable to answer",
    "not in the profile",
    "not in profile",
    "unresolved required",
    "required field",
)
_TERMINAL_BLOCKER_PATTERNS = (
    "captcha",
    "access denied",
    "position closed",
    "login required",
    "email verification required",
    "verify your account",
    "account needs email verification",
    "stored credentials are invalid",
    "credential is invalid",
)
_REQUIRED_FIELD_RE = re.compile(r"required field ['\"](?P<field>[^'\"]+)['\"]", re.IGNORECASE)
_SECTION_RE = re.compile(r"on the ['\"](?P<section>[^'\"]+)['\"] page", re.IGNORECASE)


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

    if 401 in status_codes and any(keyword in combined_text for keyword in ("expired", "revoked", "grant")):
        return _RuntimeErrorSignal(
            code="GRANT_EXPIRED",
            message="Your automation session expired. Please try again.",
            keep_browser_open=True,
        )

    return None


def _looks_like_answer_needed_blocker(text: str | None) -> bool:
    lowered = (text or "").lower()
    return bool(lowered) and any(pattern in lowered for pattern in _ANSWER_NEEDED_BLOCKER_PATTERNS)


def _looks_like_terminal_blocker(text: str | None) -> bool:
    lowered = (text or "").lower()
    return bool(lowered) and any(pattern in lowered for pattern in _TERMINAL_BLOCKER_PATTERNS)


def _extract_application_state_json(summary: str) -> dict[str, Any]:
    marker = "APPLICATION_STATE_JSON:"
    if marker not in summary:
        return {}
    _, _, tail = summary.partition(marker)
    raw = tail.strip()
    if not raw:
        return {}
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    return {}


async def _collect_open_question_issues_from_browser(browser: Any) -> list[_OpenQuestionIssue]:
    with contextlib.suppress(Exception):
        from ghosthands.actions.domhand_assess_state import domhand_assess_state
        from ghosthands.actions.views import DomHandAssessStateParams

        result = await domhand_assess_state(DomHandAssessStateParams(), browser)
        summary = result.extracted_content or ""
        payload: dict[str, Any] = {}
        meta = getattr(result, "metadata", None) or {}
        raw_state = meta.get("application_state_json")
        if isinstance(raw_state, str) and raw_state.strip():
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                parsed = json.loads(raw_state)
                if isinstance(parsed, dict):
                    payload = parsed
        if not payload:
            payload = _extract_application_state_json(summary)
        issues = payload.get("unresolved_required_fields")
        if isinstance(issues, list):
            collected: list[_OpenQuestionIssue] = []
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                field_label = str(issue.get("name") or "").strip()
                if not field_label:
                    continue
                candidate = _OpenQuestionIssue(
                    field_label=field_label,
                    field_id=str(issue.get("field_id") or "").strip() or None,
                    field_type=str(issue.get("field_type") or "text").strip() or "text",
                    question_text=str(issue.get("question_text") or field_label).strip() or field_label,
                    section=str(issue.get("section") or "").strip() or None,
                    section_path=str(issue.get("section_path") or issue.get("section") or "").strip() or None,
                    current_value=str(issue.get("current_value") or "").strip() or None,
                    visible_error=str(issue.get("visible_error") or "").strip() or None,
                    widget_kind=str(issue.get("widget_kind") or "").strip() or None,
                    options=tuple(
                        str(option).strip() for option in (issue.get("options") or []) if str(option).strip()
                    ),
                )
                if _issue_supports_answer_recovery(candidate):
                    collected.append(candidate)
            if collected:
                return collected
    return []


def _issues_from_blocker_text(blocker: str) -> list[_OpenQuestionIssue]:
    match = _REQUIRED_FIELD_RE.search(blocker)
    if not match:
        return []
    section_match = _SECTION_RE.search(blocker)
    return [
        _OpenQuestionIssue(
            field_label=match.group("field").strip(),
            question_text=blocker.strip(),
            section=section_match.group("section").strip() if section_match else None,
        )
    ]


async def _auto_answer_open_question_issues(
    issues: list[_OpenQuestionIssue],
    profile: dict[str, Any] | None,
) -> tuple[list[_RecoveredFieldAnswer], list[_OpenQuestionIssue]]:
    """Resolve open-question issues from saved profile/default evidence first.

    This runs before Desktop HITL is shown. It covers cases where the browser
    reports unresolved required fields but the applicant profile already
    contains a safe answer, such as Workday language rubrics, referral source,
    phone type, or EEO decline defaults.
    """
    if not issues:
        return [], issues
    if profile is None:
        profile = {}
    if not isinstance(profile, dict):
        return [], issues

    from ghosthands.actions.domhand_fill import (
        _AUTHORITATIVE_SELECT_DEFAULTS,
        _AUTHORITATIVE_TEXT_DEFAULTS,
        _EEO_DECLINE_DEFAULTS,
        _build_profile_answer_map,
        _default_screening_answer,
        _find_best_profile_answer,
        _known_profile_value,
        _semantic_profile_value_for_field,
        _normalize_match_label,
        _parse_profile_evidence,
    )
    from ghosthands.actions.views import FormField
    from ghosthands.runtime_learning import confirm_learned_question_alias

    evidence = _parse_profile_evidence(json.dumps(profile))
    answer_map = _build_profile_answer_map(profile, evidence)
    resolved: list[_RecoveredFieldAnswer] = []
    unresolved: list[_OpenQuestionIssue] = []

    for index, issue in enumerate(issues, start=1):
        if not _issue_supports_answer_recovery(issue):
            unresolved.append(issue)
            continue
        label = (issue.field_label or issue.question_text or "").strip()
        if not label:
            unresolved.append(issue)
            continue

        norm = _normalize_match_label(label)
        answer = _known_profile_value(label, evidence)
        if not answer:
            answer = _find_best_profile_answer(label, answer_map, minimum_confidence="medium")
        if not answer:
            synthetic_field = FormField(
                field_id=issue.field_id or issue.field_label,
                name=label,
                raw_label=issue.question_text or label,
                field_type=issue.field_type or "text",
                section=issue.section or "",
                required=True,
                current_value=issue.current_value or "",
                options=list(issue.options),
                choices=list(issue.options),
            )
            answer = _default_screening_answer(synthetic_field, profile)
            if not answer:
                answer = await _semantic_profile_value_for_field(
                    synthetic_field,
                    evidence,
                    profile,
                )

        if not answer and issue.field_type == "select":
            answer = _AUTHORITATIVE_SELECT_DEFAULTS.get(norm)
        if not answer:
            answer = _AUTHORITATIVE_TEXT_DEFAULTS.get(norm)
        if not answer:
            answer = _EEO_DECLINE_DEFAULTS.get(norm)

        cleaned = str(answer).strip() if answer is not None else ""
        if cleaned:
            confirm_learned_question_alias(label)
            resolved.append(
                _RecoveredFieldAnswer(
                    field_id=_issue_recovery_key(issue, index),
                    field_label=issue.field_label,
                    answer=cleaned,
                    question_text=issue.question_text,
                    section_path=issue.section_path or issue.section,
                )
            )
        else:
            unresolved.append(issue)

    return resolved, unresolved


async def _infer_open_question_answers_with_domhand(
    issues: list[_OpenQuestionIssue],
    profile: dict[str, Any] | None,
) -> tuple[list[_RecoveredFieldAnswer], list[_OpenQuestionIssue]]:
    """Use DomHand's LLM-backed field inference for open questions before HITL."""
    if not issues:
        return [], issues
    if not isinstance(profile, dict):
        return [], issues

    from ghosthands.actions.domhand_fill import infer_answers_for_fields
    from ghosthands.actions.views import FormField

    synthetic_fields: list[FormField] = []
    issue_by_field_id: dict[str, _OpenQuestionIssue] = {}
    unresolved: list[_OpenQuestionIssue] = []
    for index, issue in enumerate(issues, start=1):
        if not _issue_supports_answer_recovery(issue):
            unresolved.append(issue)
            continue
        if issue.field_type == "file":
            unresolved.append(issue)
            continue
        if issue.field_type == "textarea" and not issue.options:
            unresolved.append(issue)
            continue
        field_id = issue.field_id or f"open-question-{index}"
        synthetic_fields.append(
            FormField(
                field_id=field_id,
                name=issue.field_label,
                raw_label=issue.question_text or issue.field_label,
                field_type=issue.field_type or "text",
                section=issue.section or "",
                required=True,
                options=list(issue.options),
                choices=list(issue.options),
                visible=True,
            )
        )
        issue_by_field_id[field_id] = issue

    if not synthetic_fields:
        return [], unresolved

    inferred = await infer_answers_for_fields(
        synthetic_fields,
        profile_text=json.dumps(profile),
        profile_data=profile,
    )

    resolved: list[_RecoveredFieldAnswer] = []
    for field in synthetic_fields:
        issue = issue_by_field_id[field.field_id]
        answer = str(inferred.get(field.field_id) or "").strip()
        if answer:
            resolved.append(
                _RecoveredFieldAnswer(
                    field_id=field.field_id,
                    field_label=issue.field_label,
                    answer=answer,
                    question_text=issue.question_text,
                    section_path=issue.section_path or issue.section,
                )
            )
        else:
            unresolved.append(issue)

    return resolved, unresolved


async def _request_open_question_answers(
    browser: Any,
    blocker: str,
    *,
    timeout_seconds: float,
    issues: list[_OpenQuestionIssue] | None = None,
    profile: dict[str, Any] | None = None,
) -> tuple[list[_RecoveredFieldAnswer], bool]:
    from ghosthands.output.jsonl import emit_event

    if issues is None:
        issues = await _collect_open_question_issues_from_browser(browser)
    if not issues:
        issues = _issues_from_blocker_text(blocker)
    issues = [issue for issue in issues if _issue_supports_answer_recovery(issue)]
    if not issues:
        return [], False

    auth_answers, remaining_issues = _resolve_auth_recovery_answers(issues)
    auto_answers, unresolved_issues = await _auto_answer_open_question_issues(remaining_issues, profile)
    llm_answers: list[_RecoveredFieldAnswer] = []
    if unresolved_issues:
        llm_answers, unresolved_issues = await _infer_open_question_answers_with_domhand(unresolved_issues, profile)
    recovered_answers: dict[str, _RecoveredFieldAnswer] = {
        answer.field_id: answer for answer in auth_answers + auto_answers + llm_answers
    }
    if auth_answers:
        emit_event(
            "status",
            message=f"Using explicit auth override for {len(auth_answers)} auth field(s) before continuing locally",
        )
    if auto_answers:
        emit_event(
            "status",
            message=f"Using saved profile defaults for {len(auto_answers)} field(s) before continuing locally",
        )
    if llm_answers:
        emit_event(
            "status",
            message=f"DomHand inferred {len(llm_answers)} additional field answer(s) before continuing locally",
        )
    if unresolved_issues:
        emit_event(
            "status",
            message=(
                "Open-question HITL is disabled for apply flows; leaving "
                f"{len(unresolved_issues)} field(s) for continued best-effort recovery"
            ),
        )
    return list(recovered_answers.values()), False


def _build_recovery_task(base_task: str, answers: list[_RecoveredFieldAnswer]) -> str:
    answer_lines = "\n".join(
        f"- [field_id={answer.field_id}] {answer.field_label}"
        + (f" [section={answer.section_path}]" if answer.section_path else "")
        + f": {answer.answer}"
        for answer in answers
    )
    return (
        f"{base_task}\n\n"
        "RECOVERED ANSWERS JUST PROVIDED:\n"
        f"{answer_lines}\n"
        "Continue from the CURRENT page in the EXISTING browser session.\n"
        "Use these answers immediately to finish the blocked required fields.\n"
        "Do NOT restart the application or navigate back to the job posting unless the page is irrecoverably broken."
    )


def _extract_best_effort_guess_summary(
    filled_field_records: list[dict[str, Any]] | None,
) -> tuple[int, list[dict[str, Any]]]:
    if not isinstance(filled_field_records, list):
        return 0, []

    guessed_fields: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in filled_field_records:
        if not isinstance(record, dict):
            continue
        if str(record.get("answer_mode") or "").strip() != "best_effort_guess":
            continue
        prompt_text = str(record.get("field") or record.get("prompt_text") or "").strip()
        if not prompt_text:
            continue
        section_label = str(record.get("section_label") or "").strip()
        dedupe_key = (prompt_text, section_label)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        guessed_fields.append(
            {
                "promptText": prompt_text,
                "sectionLabel": section_label or None,
                "required": record.get("required") is True,
            }
        )

    return len(guessed_fields), guessed_fields


def _handle_review_result(
    review_result: str,
    *,
    fields_filled: int,
    fields_failed: int,
    job_id: str,
    lease_id: str,
    result_data: dict[str, Any],
    cost_summary: dict[str, Any],
    total_cost_usd: float,
) -> int | None:
    """Emit the terminal review result (cost + done) and return the desired exit code."""
    from ghosthands.output.jsonl_terminal import emit_run_terminal

    if review_result == "complete":
        emit_run_terminal(
            termination_status="completed",
            success=True,
            message="Application submitted — review completed",
            fields_filled=fields_filled,
            fields_failed=fields_failed,
            job_id=job_id,
            lease_id=lease_id,
            result_data=result_data,
            cost_summary=cost_summary,
            total_cost_usd=total_cost_usd,
        )
        return None

    if review_result == "cancel":
        emit_run_terminal(
            termination_status="review_cancelled",
            success=False,
            message="Review cancelled by user",
            fields_filled=fields_filled,
            fields_failed=fields_failed,
            job_id=job_id,
            lease_id=lease_id,
            result_data={**result_data, "success": False, "cancelled": True},
            cost_summary=cost_summary,
            total_cost_usd=total_cost_usd,
        )
        return 1

    if review_result == "timeout":
        emit_run_terminal(
            termination_status="review_timeout",
            success=False,
            message="Review timed out after 30 minutes. The browser window is still open — you can submit manually.",
            fields_filled=fields_filled,
            fields_failed=fields_failed,
            job_id=job_id,
            lease_id=lease_id,
            result_data={**result_data, "success": False, "timedOut": True},
            cost_summary=cost_summary,
            total_cost_usd=total_cost_usd,
        )
        return 1

    emit_run_terminal(
        termination_status="review_disconnect",
        success=False,
        message="Desktop disconnected",
        fields_filled=fields_filled,
        fields_failed=fields_failed,
        job_id=job_id,
        lease_id=lease_id,
        result_data={**result_data, "success": False},
        cost_summary=cost_summary,
        total_cost_usd=total_cost_usd,
    )
    return 1


# ── JSONL agent run ───────────────────────────────────────────────────


def _jsonl_done_signals_incomplete_outcome(final_result: str, history: Any) -> bool:
    """True when the agent called done() but the run is step-capped or explicitly partial.

    Without this, Hand-X treats any truthy final_result as success and emits awaiting_review,
    which mislabels max-step / partial fills as 'ready to submit'.
    """
    t = (final_result or "").lower()
    phrases = (
        "partially completed",
        "partially complete",
        "partial completion",
        "terminated due to step limit",
        "due to step limit",
        "step limit",
        "maximum steps",
        "max steps",
        "not fully submitted",
        "without submitting",
        "could not complete",
        "unable to complete the full",
    )
    if any(p in t for p in phrases):
        return True
    try:
        for entry in reversed((getattr(history, "history", None) or [])[-12:]):
            mo = getattr(entry, "model_output", None)
            if mo is None:
                continue
            cs = getattr(mo, "current_state", None)
            if cs is None:
                continue
            blob = (
                f"{getattr(cs, 'memory', '')} "
                f"{getattr(cs, 'evaluation_previous_goal', '')} "
                f"{getattr(cs, 'next_goal', '')}"
            ).lower()
            if (
                "step limit" in blob
                or "maximum steps" in blob
                or "terminated due to step limit" in blob
            ):
                return True
    except Exception:
        pass
    return False


async def run_agent_jsonl(args: argparse.Namespace) -> None:
    """Run the agent with JSONL event output on stdout."""
    from ghosthands.cost_summary import summarize_history_cost
    from ghosthands.output import jsonl_terminal as jt
    from ghosthands.output.agent_history_payload import build_agent_history_payload
    from ghosthands.output.jsonl import (
        emit_account_created,
        emit_awaiting_review,
        emit_browser_ready,
        emit_cost,
        emit_error,
        emit_phase,
        emit_status,
    )

    jt.reset()
    jt.configure(
        job_id=getattr(args, "job_id", None) or "",
        lease_id=getattr(args, "lease_id", None) or "",
        platform="generic",
    )
    jt.install_signal_handlers()

    app_settings = None
    browser = None
    job_id = ""
    lease_id = ""
    desktop_owns_browser = False
    keep_worker_browser_alive = args.output_format == "jsonl"
    last_phase: str | None = None
    account_created_emitted = False

    def _emit_phase_if_changed(phase: str, detail: str | None = None) -> None:
        nonlocal last_phase
        if phase == last_phase:
            return
        emit_phase(phase, detail=detail)
        last_phase = phase

    # -- Load profile -------------------------------------------------------
    try:
        profile = await _load_profile_async(args)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.error("profile_load_failed", error=str(e))
        emit_error("Failed to load applicant profile", fatal=True)
        jt.emit_run_terminal(
            termination_status="profile_load_failed",
            success=False,
            message=f"Failed to load applicant profile: {e}",
            result_data={"success": False, "error": str(e)},
        )
        sys.exit(1)

    # -- Convert camelCase keys from Desktop bridge to snake_case ----------
    profile = camel_to_snake_profile(profile)

    from ghosthands.runtime_learning import reset_runtime_learning_state

    reset_runtime_learning_state()

    # -- Extract embedded credentials before they leak into env/profile ----
    # We pop them so they don't end up in GH_USER_PROFILE_TEXT env var.
    embedded_credentials = profile.pop("credentials", None)

    # -- Normalize profile defaults for DomHand ----------------------------
    profile = normalize_profile_defaults(profile)
    if _profile_debug_enabled():
        logger.info(
            "cli.profile_bridge_summary",
            extra={
                "address": _profile_debug_preview(profile.get("address")),
                "city": _profile_debug_preview(profile.get("city")),
                "state": _profile_debug_preview(profile.get("state")),
                "postal_code": _profile_debug_preview(profile.get("postal_code") or profile.get("zip")),
                "county": _profile_debug_preview(profile.get("county")),
                "linkedin": _profile_debug_preview(profile.get("linkedin") or profile.get("linkedin_url")),
                "work_authorization": _profile_debug_preview(
                    profile.get("work_authorization") or profile.get("authorized_to_work_in_us")
                ),
                "visa_sponsorship": _profile_debug_preview(
                    profile.get("visa_sponsorship") or profile.get("needs_visa_sponsorship")
                ),
                "citizenship_status": _profile_debug_preview(profile.get("citizenship_status")),
                "visa_type": _profile_debug_preview(profile.get("visa_type")),
                "citizenship_country": _profile_debug_preview(profile.get("citizenship_country")),
                "us_citizen": _profile_debug_preview(profile.get("us_citizen")),
                "export_control_eligible": _profile_debug_preview(profile.get("export_control_eligible")),
                "gender": _profile_debug_preview(profile.get("gender")),
                "race_ethnicity": _profile_debug_preview(profile.get("race_ethnicity")),
                "veteran_status": _profile_debug_preview(profile.get("veteran_status")),
                "disability_status": _profile_debug_preview(profile.get("disability_status")),
                "sexual_orientation": _profile_debug_preview(profile.get("sexual_orientation")),
                "salary_expectation": _profile_debug_preview(profile.get("salary_expectation")),
                "english_proficiency": _profile_debug_preview(profile.get("english_proficiency")),
                "spoken_languages": _profile_debug_preview(profile.get("spoken_languages")),
                "how_did_you_hear": _profile_debug_preview(profile.get("how_did_you_hear")),
                "learned_question_aliases": len(
                    profile.get("learnedQuestionAliases") or profile.get("learned_question_aliases") or []
                ),
                "learned_interaction_recipes": len(
                    profile.get("learnedInteractionRecipes") or profile.get("learned_interaction_recipes") or []
                ),
                "answer_bank_count": len(profile.get("answerBank") or profile.get("answer_bank") or []),
                "education_count": len(profile.get("education") or []),
                "education_has_dates": bool(
                    profile.get("education")
                    and isinstance(profile["education"], list)
                    and len(profile["education"]) > 0
                    and isinstance(profile["education"][0], dict)
                    and (profile["education"][0].get("start_date") or profile["education"][0].get("startDate"))
                ),
            },
        )
    _emit_phase_if_changed("Starting application")

    # -- Set env vars -------------------------------------------------------
    resume_path = _apply_runtime_env(args, profile)

    # -- Install DomHand field event callback --------------------------------
    from ghosthands.output import field_events

    field_events.install_jsonl_callback()

    # -- Import heavy deps after env setup ----------------------------------
    logger.warning("startup.importing_browser_use")
    from browser_use import Agent, BrowserProfile, BrowserSession, Tools

    app_settings = _load_runtime_settings()

    # -- Resolve job_id / lease_id: CLI args take precedence, env fallback ---
    job_id = args.job_id or app_settings.job_id
    lease_id = args.lease_id or app_settings.lease_id

    emit_status("Hand-X engine initialized", job_id=job_id)
    emit_status("Setting up agent...", job_id=job_id)

    _warn_if_proxy_overrides_direct_keys(args, app_settings)

    logger.warning("startup.creating_llm_client", model=args.model)
    from ghosthands.llm.client import get_chat_model

    llm = get_chat_model(model=args.model)
    logger.warning("startup.llm_client_ready", model_type=type(llm).__name__)

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
        logger.warning("startup.platform_detected", platform=platform)
    except ImportError:
        pass

    jt.configure(job_id=job_id, lease_id=lease_id, platform=platform)

    # -- System prompt ------------------------------------------------------
    system_ext = ""
    try:
        from ghosthands.agent.prompts import build_system_prompt

        system_ext = build_system_prompt(profile, platform)
    except ImportError:
        pass

    # -- Credentials --------------------------------------------------------
    sensitive_data = _resolve_sensitive_data(app_settings, embedded_credentials, platform=platform)
    _log_auth_debug_credentials(sensitive_data, platform=platform)

    # -- Domain lockdown ----------------------------------------------------
    from ghosthands.security.domain_lockdown import DomainLockdown

    lockdown = DomainLockdown(job_url=args.job_url, platform=platform)
    lockdown.freeze()
    allowed_domains: list[str] = []  # Disabled: ATS sites redirect across domains (e.g. Goldman→Oracle Cloud)

    # -- Browser ------------------------------------------------------------
    cdp_url = args.cdp_url or os.environ.get("GH_CDP_URL")
    cdp_target_id = os.environ.get("GH_TARGET_ID")
    desktop_owns_browser = cdp_url is not None

    if cdp_url:
        # Desktop-owned browser: connect to existing browser via CDP URL.
        # Do not launch a new browser; headless flag is irrelevant here.
        # If GH_TARGET_ID is set, attach to that specific tab (shared-browser mode).
        browser_profile = BrowserProfile(keep_alive=True, allowed_domains=allowed_domains)
        browser = BrowserSession(browser_profile=browser_profile, cdp_url=cdp_url, target_id=cdp_target_id)
        if cdp_target_id:
            emit_status(f"Connecting to Desktop-owned browser via CDP (target: {cdp_target_id[:8]}...)", job_id=job_id)
        else:
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

    task = build_task_prompt(
        args.job_url,
        resume_path,
        sensitive_data,
        credential_source=app_settings.credential_source,
        credential_intent=app_settings.credential_intent,
        submit_intent=app_settings.submit_intent,
        platform=platform,
    )

    emit_status(
        f"Starting application: {args.job_url}",
        step=1,
        max_steps=args.max_steps,
        job_id=job_id,
    )

    # -- Step hooks for live JSONL events -----------------------------------
    _prefill_done = False
    last_cost_summary: dict[str, Any] = {}

    async def _on_step_start(ag: Agent) -> None:
        nonlocal _prefill_done, last_cost_summary
        from ghosthands.agent.hooks import infer_phase_from_goal
        from ghosthands.agent.oracle_step_tuning import maybe_tighten_max_actions_for_oracle_focus

        await install_same_tab_guard(ag)
        await maybe_tighten_max_actions_for_oracle_focus(ag)
        await install_final_submit_guard(ag, allow_submit=app_settings.submit_intent == "submit")
        step = ag.state.n_steps
        n_hist = len(ag.history.history or [])
        sc = max(n_hist, int(step))
        jt.update_runtime_snapshot(last_cost_summary if last_cost_summary else None, step_count=sc)
        if step > 0 and step % 10 == 0:
            cost_summary = summarize_history_cost(ag.history, ag.browser_session)
            last_cost_summary = cost_summary
            hist_payload = build_agent_history_payload(ag.history, sensitive_data)
            jt.update_runtime_snapshot(
                cost_summary,
                step_count=max(n_hist, int(step)),
                agent_history=hist_payload,
            )
        goal = ""
        if ag.state.last_model_output:
            goal = ag.state.last_model_output.next_goal or ""
        phase = infer_phase_from_goal(goal)
        if phase:
            _emit_phase_if_changed(phase, detail=goal or None)
        logger.warning("agent.step", step=step, phase=phase or "unknown", goal=goal[:120] if goal else "")
        emit_status(
            phase or goal or f"Step {step}...",
            step=step,
            max_steps=args.max_steps,
            job_id=job_id,
        )

        # Auto-prefill: on step 1 for non-Workday platforms, call domhand_fill
        # before the LLM sees the page. DomHand already handles Greenhouse/Lever
        # forms; this ensures it runs immediately instead of waiting for the LLM
        # to decide (which can hang on large pages).
        if step == 1 and not _prefill_done and platform not in ("workday",):
            _prefill_done = True
            try:
                from ghosthands.actions.domhand_fill import domhand_fill as _domhand_fill
                from ghosthands.actions.views import DomHandFillParams

                _emit_phase_if_changed("Auto-filling form fields")
                result = await _domhand_fill(DomHandFillParams(), ag.browser_session)
                if result and not result.error:
                    ag.state.last_result = [result]
                    logger.warning(
                        "auto_prefill.completed",
                        has_content=bool(result.extracted_content),
                    )
                else:
                    logger.warning(
                        "auto_prefill.no_fields",
                        error=result.error if result else "no result",
                    )
            except Exception as exc:
                logger.warning("auto_prefill.failed", error=str(exc))

    async def _on_step_end(ag: Agent) -> None:
        nonlocal account_created_emitted, last_cost_summary
        from browser_use.agent.views import ActionResult

        if app_settings.submit_intent != "submit":
            blocked_submit = await consume_blocked_final_submit(ag)
            if blocked_submit:
                label = str(blocked_submit.get("label") or "submit")
                ag.state.last_result = [
                    ActionResult(
                        extracted_content=(
                            f"Runtime blocked final submit control '{label}' because submit_intent=review. "
                            "This run must stop before final submission. Use done(success=True) when the form is ready for user review."
                        ),
                        include_extracted_content_only_once=True,
                    )
                ]

        usage = ag.history.usage
        cost_summary = summarize_history_cost(ag.history, ag.browser_session)
        last_cost_summary = cost_summary
        hist_payload = build_agent_history_payload(ag.history, sensitive_data)
        jt.update_runtime_snapshot(
            cost_summary,
            step_count=len(ag.history.history or []),
            agent_history=hist_payload,
        )
        tracked_cost = float(cost_summary["total_tracked_cost_usd"])
        if usage or tracked_cost:
            emit_cost(
                total_usd=tracked_cost,
                prompt_tokens=int(cost_summary["total_tracked_prompt_tokens"]),
                completion_tokens=int(cost_summary["total_tracked_completion_tokens"]),
                cost_summary=cost_summary,
            )

        # Budget check
        if tracked_cost >= args.max_budget:
            ag.state.stopped = True
            emit_error("Budget exceeded", fatal=False, job_id=job_id)

        # Pre-step budget guard: stop if less than estimated step cost remaining
        if (args.max_budget - tracked_cost) < _STEP_COST_ESTIMATE:
            ag.state.stopped = True

        # ── Account creation detection ──
        # Detect successful account creation from the agent's evaluation and
        # emit immediately so Desktop records the credential before the job
        # finishes/crashes. This must cover both generated credentials and
        # user-provided create-account flows.
        if (
            not account_created_emitted
            and app_settings
            and (
                app_settings.credential_source == "generated"
                or (app_settings.credential_source == "user" and app_settings.credential_intent == "create_account")
            )
            and app_settings.email
            and app_settings.password
            and ag.history.history
        ):
            last = ag.history.history[-1]
            if last.model_output and last.model_output.current_state:
                memory = (last.model_output.current_state.memory or "").lower()
                eval_text = (last.model_output.current_state.evaluation_previous_goal or "").lower()
                combined = memory + " " + eval_text
                marker_status, marker_note, marker_evidence = _infer_account_created_marker_from_text(combined)

                if marker_status:
                    try:
                        marker_status, marker_note, marker_evidence = await _stabilize_account_created_marker(
                            ag,
                            marker_status,
                            marker_note,
                            marker_evidence,
                        )
                        url = args.job_url if hasattr(args, "job_url") else ""
                        hostname = ""
                        platform = url
                        try:
                            from urllib.parse import urlparse

                            hostname = (urlparse(url).hostname or "").lower()
                        except Exception:
                            hostname = ""

                        if "myworkdayjobs.com" in hostname or "myworkday.com" in hostname or "workday.com" in hostname:
                            platform = "workday"
                        elif "greenhouse.io" in hostname:
                            platform = "greenhouse"
                        elif "smartrecruiters.com" in hostname:
                            platform = "smartrecruiters"
                        elif "icims.com" in hostname:
                            platform = "icims"
                        elif "taleo.net" in hostname:
                            platform = "taleo"
                        elif "bamboohr.com" in hostname:
                            platform = "bamboohr"
                        elif "lever.co" in hostname:
                            platform = "lever"
                        elif "ashbyhq.com" in hostname:
                            platform = "ashby"
                        elif hostname:
                            platform = hostname

                        emit_account_created(
                            platform=platform,
                            domain=hostname or None,
                            email=app_settings.email,
                            password=app_settings.password,
                            credential_status=marker_status,
                            note=marker_note,
                            evidence=marker_evidence,
                            url=url,
                        )
                        account_created_emitted = True
                        logger.info(
                            "cli.account_created_emitted",
                            extra={"url": url, "credential_status": marker_status},
                        )
                    except Exception:
                        logger.warning("cli.account_created_emit_failed", exc_info=True)

    # -- Run ----------------------------------------------------------------
    try:
        logger.warning(
            "startup.launching_browser",
            headless=args.headless,
            cdp_url=bool(cdp_url),
            platform=platform,
        )
        try:
            await asyncio.wait_for(browser.start(), timeout=60)
        except asyncio.TimeoutError:
            logger.error("startup.browser_launch_timeout", timeout_seconds=60)
            emit_error("Browser failed to start within 60 seconds", fatal=True, job_id=job_id)
            jt.emit_run_terminal(
                termination_status="browser_launch_timeout",
                success=False,
                message="Browser failed to start within 60 seconds",
                job_id=job_id,
                lease_id=lease_id,
                result_data={"success": False},
            )
            sys.exit(1)
        logger.warning("startup.browser_ready", cdp_url=bool(browser.cdp_url))
        if browser.cdp_url:
            emit_browser_ready(browser.cdp_url)
        else:
            logger.warning("cli.browser_ready_missing_cdp_url")
            emit_status(
                "Browser CDP URL unavailable — live review attachment disabled",
                job_id=job_id,
            )

        workday_preface_status = "noop"
        workday_preface_message = ""
        if platform == "workday":
            workday_preface_status, workday_preface_message = await _run_workday_auth_preface(
                browser,
                job_url=args.job_url,
                app_settings=app_settings,
                platform=platform,
            )
            logger.info(
                "cli.workday_auth_preface",
                extra={"status": workday_preface_status, "message": workday_preface_message},
            )
            if workday_preface_status == "blocker":
                emit_error(workday_preface_message, fatal=False, job_id=job_id)
                jt.emit_run_terminal(
                    termination_status="workday_preface_blocker",
                    success=False,
                    message=workday_preface_message,
                    job_id=job_id,
                    lease_id=lease_id,
                    result_data={"success": False, "browserOpen": True},
                )
                return

        # -- Create agent ---------------------------------------------------
        available_files = [resume_path] if resume_path else []
        initial_task = task
        directly_open_url = True
        if workday_preface_status.startswith("continue"):
            if workday_preface_status == "continue_auth_complete":
                workday_stage = "auth_complete"
            elif workday_preface_status == "continue_auth_page":
                workday_stage = "auth_page"
            else:
                workday_stage = "resume_autofill"
            initial_task = _build_current_page_continuation_task(task, platform=platform, stage=workday_stage)
            directly_open_url = False

        async def _run_agent_once(current_task: str) -> tuple[Any, bool]:
            agent = Agent(
                task=current_task,
                llm=llm,
                browser_session=browser,
                tools=tools,
                extend_system_message=system_ext or None,
                sensitive_data=sensitive_data,
                available_file_paths=available_files or None,
                use_vision="auto",
                max_actions_per_step=app_settings.agent_max_actions_per_step,
                max_history_items=app_settings.agent_max_history_items,
                calculate_cost=True,
                use_judge=False,
                directly_open_url=directly_open_url,
            )

            reset_hitl_state()
            cancel_requested = asyncio.Event()
            cancel_task = asyncio.create_task(listen_for_cancel(agent, cancel_requested))
            try:
                _emit_phase_if_changed("Navigating to application")
                logger.warning("startup.agent_run_starting", max_steps=args.max_steps)
                history = await agent.run(
                    max_steps=args.max_steps,
                    on_step_start=_on_step_start,
                    on_step_end=_on_step_end,
                )
            finally:
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task
            return history, cancel_requested.is_set()

        history = None
        is_done = False
        final_result = ""
        total_cost = 0.0
        total_steps = 0
        cancelled = False
        resumed_after_false_verification = False
        hitl_recovery_task = initial_task
        # Single run — never restart the agent from scratch. Restarting loses all
        # form progress (the new agent navigates back to the job URL). If the agent
        # dies from consecutive failures, accept the result rather than wasting a
        # second full run on the same stuck field.
        history, cancelled = await _run_agent_once(hitl_recovery_task)
        is_done = history.is_done()
        final_result = history.final_result() or ""
        cost_summary = summarize_history_cost(history, browser)
        total_cost += float(cost_summary["total_tracked_cost_usd"])
        total_steps += len(history.history) if history.history else 0

        if await _should_resume_from_false_verification_blocker(
            final_result,
            platform=platform,
            app_settings=app_settings,
            browser_session=browser,
        ):
            resumed_after_false_verification = True
            emit_status(
                "False verification blocker detected; continuing from native Sign In",
                job_id=job_id,
            )
            directly_open_url = False
            continuation_task = (
                "Continue from the CURRENT page without navigating back to the job URL. "
                "Create Account already succeeded and Workday is now on the native Sign In page. "
                "This is NOT email verification. Sign in ONCE with the SAME email/password, then continue the application. "
                "Only report email verification if the page explicitly shows inbox/code/verify-email text."
            )
            history, second_cancelled = await _run_agent_once(continuation_task)
            cancelled = cancelled or second_cancelled
            is_done = history.is_done()
            final_result = history.final_result() or ""
            cost_summary = summarize_history_cost(history, browser)
            total_cost += float(cost_summary["total_tracked_cost_usd"])
            total_steps += len(history.history) if history.history else 0

        # Get real field counts and successful field provenance from DomHand callback
        from ghosthands.output.field_events import get_field_counts, get_filled_field_records

        filled_count, failed_count = get_field_counts()
        filled_field_records = get_filled_field_records()
        best_effort_guess_count, best_effort_guess_fields = _extract_best_effort_guess_summary(
            filled_field_records
        )

        if cancelled:
            from ghosthands.runtime_learning import export_runtime_learning_payload

            runtime_learning_payload = export_runtime_learning_payload()
            if _profile_debug_enabled():
                logger.info(
                    "cli.runtime_learning_export",
                    extra={
                        "learned_question_aliases": len(
                            runtime_learning_payload.get("learned_question_aliases") or []
                        ),
                        "learned_interaction_recipes": len(
                            runtime_learning_payload.get("learned_interaction_recipes") or []
                        ),
                        "cancelled": True,
                    },
                )
            cancel_rd: dict[str, Any] = {
                "success": False,
                "steps": total_steps,
                "costUsd": round(total_cost, 6),
                "costSummary": cost_summary,
                "finalResult": final_result,
                "blocker": None,
                "platform": platform,
                "cancelled": True,
                "best_effort_guess_count": best_effort_guess_count,
                "best_effort_guess_fields": best_effort_guess_fields,
                **runtime_learning_payload,
            }
            cancel_rd["agentHistory"] = build_agent_history_payload(history, sensitive_data)
            jt.emit_run_terminal(
                termination_status="cancelled",
                success=False,
                message="Job cancelled by user",
                fields_filled=filled_count,
                fields_failed=failed_count,
                job_id=job_id,
                lease_id=lease_id,
                cost_summary=cost_summary,
                total_cost_usd=total_cost,
                result_data=cancel_rd,
            )
            await _cleanup_browser(
                browser,
                desktop_owns_browser,
                keep_browser_alive=keep_worker_browser_alive,
            )
            sys.exit(1)

        # Determine outcome
        success = is_done and bool(final_result)
        blocker: str | None = None
        if final_result and "blocker:" in final_result.lower():
            blocker = final_result
            success = False
        if success and _jsonl_done_signals_incomplete_outcome(final_result, history):
            success = False
            if blocker is None:
                blocker = final_result
        if app_settings.submit_intent != "submit" and final_result:
            final_lower = final_result.lower()
            if "application submitted" in final_lower or "submitted successfully" in final_lower:
                blocker = "guard breach: application was submitted despite submit_intent=review"
                success = False

        result_data = {
            "success": success,
            "steps": total_steps,
            "costUsd": round(total_cost, 6),
            "costSummary": cost_summary,
            "finalResult": final_result,
            "blocker": blocker,
            "platform": platform,
            "resumedAfterFalseVerification": resumed_after_false_verification,
            "best_effort_guess_count": best_effort_guess_count,
            "best_effort_guess_fields": best_effort_guess_fields,
        }
        from ghosthands.runtime_learning import export_runtime_learning_payload

        runtime_learning_payload = export_runtime_learning_payload()
        if _profile_debug_enabled():
            logger.info(
                "cli.runtime_learning_export",
                extra={
                    "learned_question_aliases": len(
                        runtime_learning_payload.get("learned_question_aliases") or []
                    ),
                    "learned_interaction_recipes": len(
                        runtime_learning_payload.get("learned_interaction_recipes") or []
                    ),
                    "cancelled": False,
                },
            )
        result_data.update(runtime_learning_payload)
        result_data["agentHistory"] = build_agent_history_payload(history, sensitive_data)

        if success:
            # I-02/U-01: emit status (not done) before review so the terminal
            # event is only sent once, after the user has actually reviewed.
            _emit_phase_if_changed("Reviewing filled fields")
            emit_status("Application filled — awaiting review", job_id=job_id)

            # Last agent step may skip emit_cost when usage + tracked USD are both empty;
            # push one snapshot so Desktop Cost & Progress is not stuck at $0.00.
            _tc = float(cost_summary.get("total_tracked_cost_usd") or 0.0)
            _pt = int(cost_summary.get("total_tracked_prompt_tokens") or 0)
            _ct = int(cost_summary.get("total_tracked_completion_tokens") or 0)
            emit_cost(
                total_usd=_tc,
                prompt_tokens=_pt,
                completion_tokens=_ct,
                cost_summary=cost_summary,
            )

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
                cost_summary=cost_summary,
                total_cost_usd=total_cost,
            )
            if exit_code is not None:
                sys.exit(exit_code)
        else:
            jt.emit_run_terminal(
                termination_status="agent_incomplete",
                success=False,
                message=blocker or final_result or "Agent did not complete successfully",
                fields_filled=filled_count,
                fields_failed=failed_count,
                job_id=job_id,
                lease_id=lease_id,
                cost_summary=cost_summary,
                total_cost_usd=total_cost,
                result_data=result_data,
            )
            await _cleanup_browser(
                browser,
                desktop_owns_browser,
                keep_browser_alive=keep_worker_browser_alive,
            )
            sys.exit(1)

    except (KeyboardInterrupt, asyncio.CancelledError) as intr:
        logger.warning("agent_run_interrupted", error=str(intr))
        jt.emit_run_terminal(
            termination_status="interrupted",
            success=False,
            message="Run interrupted",
            job_id=job_id,
            lease_id=lease_id,
            cost_summary=last_cost_summary or None,
            result_data={"cancelled": True, "success": False},
        )
        if browser is not None:
            with contextlib.suppress(Exception):
                await _cleanup_browser(
                    browser,
                    desktop_owns_browser,
                    keep_browser_alive=keep_worker_browser_alive,
                )
        sys.exit(130)

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
            jt.emit_run_terminal(
                termination_status="runtime_error",
                success=False,
                message=runtime_error.message,
                job_id=job_id,
                lease_id=lease_id,
                cost_summary=last_cost_summary or None,
                result_data={
                    "success": False,
                    "code": runtime_error.code,
                },
            )
            if browser is not None:
                with contextlib.suppress(Exception):
                    if runtime_error.keep_browser_open and keep_worker_browser_alive:
                        logger.info("browser.cleanup_detaching_runtime_error_keep_alive")
                        await browser.detach_keep_alive()
                    elif runtime_error.keep_browser_open:
                        await browser.detach_keep_alive()
                    else:
                        await _cleanup_browser(
                            browser,
                            desktop_owns_browser,
                            keep_browser_alive=keep_worker_browser_alive,
                        )
            sys.exit(1)

        emit_error("Agent encountered an unexpected error", fatal=True, job_id=job_id)
        jt.emit_run_terminal(
            termination_status="unexpected_error",
            success=False,
            message=str(e),
            job_id=job_id,
            lease_id=lease_id,
            cost_summary=last_cost_summary or None,
            result_data={"success": False},
        )
        if browser is not None:
            with contextlib.suppress(Exception):
                await _cleanup_browser(
                    browser,
                    desktop_owns_browser,
                    keep_browser_alive=keep_worker_browser_alive,
                )
        sys.exit(1)


# ── Human-readable agent run ─────────────────────────────────────────


def _print_cost_breakdown(cost_summary: dict[str, Any]) -> None:
    """Print a per-subsystem cost breakdown for human-mode output."""
    total = float(cost_summary.get("total_tracked_cost_usd") or 0.0)
    bu_cost = float(cost_summary.get("browser_use_cost_usd") or 0.0)
    dh_cost = float(cost_summary.get("domhand_cost_usd") or 0.0)
    sh_calls = int(cost_summary.get("stagehand_calls") or 0)
    sh_used = bool(cost_summary.get("stagehand_used"))

    print(f"  Cost:    ${total:.4f}")
    print(
        f"    browser-use  ${bu_cost:.4f}  "
        f"({int(cost_summary.get('browser_use_prompt_tokens') or 0)} in / "
        f"{int(cost_summary.get('browser_use_completion_tokens') or 0)} out)"
    )
    print(
        f"    domhand      ${dh_cost:.4f}  "
        f"({int(cost_summary.get('domhand_prompt_tokens') or 0)} in / "
        f"{int(cost_summary.get('domhand_completion_tokens') or 0)} out, "
        f"{int(cost_summary.get('domhand_llm_calls') or 0)} calls)"
    )
    if sh_used:
        print(f"    stagehand    (untracked, {sh_calls} calls)")
    if cost_summary.get("untracked_cost_possible"):
        reasons = ", ".join(cost_summary.get("untracked_reasons") or []) or "unknown"
        print(f"  Note:    Additional untracked cost possible ({reasons})")


def _print_human_result_summary(
    history: Any,
    cost_summary: dict[str, Any],
    *,
    interrupted: bool = False,
) -> None:
    """Print the human-mode result summary for completed or interrupted runs."""
    title = "  RESULT (interrupted)" if interrupted else "  RESULT"
    steps = len(getattr(history, "history", None) or [])
    result = history.final_result() if history else None
    done = bool(history.is_done()) if history else False

    print()
    print("=" * 60)
    print(title)
    print("=" * 60)
    print(f"  Done:    {done}")
    print(f"  Steps:   {steps}")
    _print_cost_breakdown(cost_summary)
    if result:
        print(f"  Output:  {str(result)[:500]}")
    print("=" * 60)
    print()


async def run_agent_human(args: argparse.Namespace) -> None:
    """Run the agent with human-readable terminal output.

    This replicates the examples/apply_to_job.py experience for developers
    who want to test from the command line without parsing JSONL.
    """
    from ghosthands.cost_summary import summarize_history_cost

    # -- Load profile -------------------------------------------------------
    try:
        profile = await _load_profile_async(args)
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
    _log_auth_debug_credentials(sensitive_data, platform=platform)

    # -- Domain lockdown ----------------------------------------------------
    from ghosthands.security.domain_lockdown import DomainLockdown

    lockdown = DomainLockdown(job_url=args.job_url, platform=platform)
    lockdown.freeze()
    allowed_domains: list[str] = []  # Disabled: ATS sites redirect across domains (e.g. Goldman→Oracle Cloud)

    # -- Browser ------------------------------------------------------------
    cdp_url = args.cdp_url or os.environ.get("GH_CDP_URL")
    desktop_owns_browser = cdp_url is not None

    if cdp_url:
        browser_profile = BrowserProfile(keep_alive=True, allowed_domains=allowed_domains)
        browser = BrowserSession(browser_profile=browser_profile, cdp_url=cdp_url)
        print(f"Connecting to Desktop-owned browser via CDP: {cdp_url}")
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

    task = build_task_prompt(
        args.job_url,
        resume_path,
        sensitive_data,
        credential_source=app_settings.credential_source,
        credential_intent=app_settings.credential_intent,
        submit_intent=app_settings.submit_intent,
        platform=platform,
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

    print("Starting browser...")
    await asyncio.wait_for(browser.start(), timeout=60)

    workday_preface_status = "noop"
    workday_preface_message = ""
    directly_open_url = True
    initial_task = task
    if platform == "workday":
        workday_preface_status, workday_preface_message = await _run_workday_auth_preface(
            browser,
            job_url=args.job_url,
            app_settings=app_settings,
            platform=platform,
        )
        logger.info(
            "cli.workday_auth_preface_human",
            extra={"status": workday_preface_status, "message": workday_preface_message},
        )
        if workday_preface_status == "blocker":
            print(f"Workday auth blocker: {workday_preface_message}")
            print("Browser is still open for inspection. Press Ctrl+C to close when done.")
            print()
            try:
                while True:
                    await asyncio.sleep(1)
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("\nClosing browser...")
                await _cleanup_browser(browser, desktop_owns_browser)
                return
        if workday_preface_status.startswith("continue"):
            if workday_preface_status == "continue_auth_complete":
                workday_stage = "auth_complete"
            elif workday_preface_status == "continue_auth_page":
                workday_stage = "auth_page"
            else:
                workday_stage = "resume_autofill"
            directly_open_url = False
            initial_task = _build_current_page_continuation_task(task, platform=platform, stage=workday_stage)

    # -- Agent --------------------------------------------------------------
    available_files = [resume_path] if resume_path else []
    agent = Agent(
        task=initial_task,
        llm=llm,
        browser_session=browser,
        tools=tools,
        extend_system_message=system_ext or None,
        sensitive_data=sensitive_data,
        available_file_paths=available_files or None,
        use_vision="auto",
        max_actions_per_step=app_settings.agent_max_actions_per_step,
        max_history_items=app_settings.agent_max_history_items,
        calculate_cost=True,
        use_judge=False,
        directly_open_url=directly_open_url,
        step_timeout=300,
    )

    async def _on_step_start_human(ag: Agent) -> None:
        from ghosthands.agent.oracle_step_tuning import maybe_tighten_max_actions_for_oracle_focus

        await install_same_tab_guard(ag)
        await maybe_tighten_max_actions_for_oracle_focus(ag)
        await install_final_submit_guard(ag, allow_submit=app_settings.submit_intent == "submit")

    async def _on_step_end_human(ag: Agent) -> None:
        from browser_use.agent.views import ActionResult

        if app_settings.submit_intent != "submit":
            blocked_submit = await consume_blocked_final_submit(ag)
            if blocked_submit:
                label = str(blocked_submit.get("label") or "submit")
                ag.state.last_result = [
                    ActionResult(
                        extracted_content=(
                            f"Runtime blocked final submit control '{label}' because submit_intent=review. "
                            "Stop at review; do not submit."
                        ),
                        include_extracted_content_only_once=True,
                    )
                ]

    try:
        history = await agent.run(
            max_steps=args.max_steps,
            on_step_start=_on_step_start_human,
            on_step_end=_on_step_end_human,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        history = getattr(agent, "history", None)
        cost_summary = summarize_history_cost(history, browser)
        _print_human_result_summary(history, cost_summary, interrupted=True)
        print("Closing browser...")
        await _cleanup_browser(browser, desktop_owns_browser)
        raise

    sys.stdout.flush()
    sys.stderr.flush()
    try:
        cost_summary = summarize_history_cost(history, browser)
    except Exception as e:
        print(f"\n  [ERROR] Cost summary failed: {e}", file=sys.stderr)
        sys.stderr.flush()
        cost_summary = {"total_tracked_cost_usd": 0, "browser_use_cost_usd": 0,
                        "domhand_cost_usd": 0, "stagehand_calls": 0, "stagehand_used": False,
                        "total_tracked_prompt_tokens": 0, "total_tracked_completion_tokens": 0,
                        "untracked_cost_possible": True, "untracked_reasons": [str(e)]}
    try:
        _print_human_result_summary(history, cost_summary)
    except Exception as e:
        print(f"\n  [ERROR] Result summary failed: {e}", file=sys.stderr)
        sys.stderr.flush()
    sys.stdout.flush()
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
    # S-08: SIGTERM for human mode only. JSONL mode installs handlers inside
    # run_agent_jsonl so SIGTERM emits the terminal cost+done contract first.
    def _handle_sigterm(signum: int, frame: object) -> None:
        raise SystemExit(1)

    # Handle --smoke-test-import before argparse (bypasses --job-url requirement)
    _handle_smoke_test_import()

    args = parse_args()

    is_jsonl = args.output_format == "jsonl"

    if not is_jsonl:
        signal.signal(signal.SIGTERM, _handle_sigterm)

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
        if is_jsonl:
            from ghosthands.output.jsonl_terminal import emit_run_terminal, was_terminal_emitted

            if not was_terminal_emitted():
                emit_run_terminal(
                    termination_status="keyboard_interrupt",
                    success=False,
                    message="Keyboard interrupt",
                    result_data={"cancelled": True, "success": False},
                )
        sys.exit(130)
    except Exception as e:
        if is_jsonl:
            from ghosthands.output.jsonl import emit_error
            from ghosthands.output.jsonl_terminal import emit_run_terminal, was_terminal_emitted

            logger.error("fatal_startup_error", error=str(e))
            emit_error("Hand-X encountered a fatal error", fatal=True)
            if not was_terminal_emitted():
                emit_run_terminal(
                    termination_status="fatal_startup",
                    success=False,
                    message=str(e),
                    result_data={"success": False},
                )
        else:
            print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
