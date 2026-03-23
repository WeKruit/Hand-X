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

from ghosthands.agent.hooks import install_same_tab_guard

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
    if marker_status != "active":
        return marker_status, marker_note, marker_evidence

    auth_state = None
    try:
        from ghosthands.button_attempts import capture_button_state

        snapshot = await capture_button_state(agent.browser_session)
        auth_state = str(snapshot.get("auth_state") or "unknown_pending")
    except Exception:
        logger.debug("cli.account_created_auth_state_probe_failed", exc_info=True)
        auth_state = None

    if auth_state == "authenticated_or_application_resumed":
        return marker_status, marker_note, marker_evidence

    if auth_state in {
        "native_login",
        "still_create_account",
        "verification_required",
        "explicit_auth_error",
        "unknown_pending",
    }:
        logger.info(
            "cli.account_created_marker_downgraded",
            extra={"auth_state": auth_state, "from_status": marker_status},
        )
        if auth_state == "native_login":
            note = "Account creation reached the native sign-in page, but sign-in success is not confirmed yet."
        elif auth_state == "verification_required":
            note = "Account likely exists, but email verification is still required."
        else:
            note = "Account creation signals were observed, but the run has not proven a post-auth success state yet."
        return "pending_verification", note, "auth_marker_pending_post_auth_confirmation"

    return marker_status, marker_note, marker_evidence


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

    # File-based profile (preferred — avoids /proc/pid/environ exposure)
    profile_path = os.environ.get("GH_USER_PROFILE_PATH", "")
    if profile_path:
        p = Path(profile_path)
        if p.is_file():
            return _validate_profile(json.loads(p.read_text(encoding="utf-8")))

    # Environment variable fallback (for backwards compat with desktop bridge)
    profile_text = os.environ.get("GH_USER_PROFILE_TEXT", "")
    if profile_text:
        return _validate_profile(json.loads(profile_text))

    raise ValueError("Either --profile, --test-data, GH_USER_PROFILE_PATH, or GH_USER_PROFILE_TEXT env var is required")


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
        emit_account_created,
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
        profile = _load_profile(args)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.error("profile_load_failed", error=str(e))
        emit_error("Failed to load applicant profile", fatal=True)
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
                "work_authorization": _profile_debug_preview(profile.get("work_authorization")),
                "visa_sponsorship": _profile_debug_preview(profile.get("visa_sponsorship")),
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

    task = build_task_prompt(
        args.job_url,
        resume_path,
        sensitive_data,
        credential_source=app_settings.credential_source,
        credential_intent=app_settings.credential_intent,
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

    async def _on_step_start(ag: Agent) -> None:
        nonlocal _prefill_done
        from ghosthands.agent.hooks import infer_phase_from_goal

        await install_same_tab_guard(ag)
        step = ag.state.n_steps
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
        nonlocal account_created_emitted

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

        # Pre-step budget guard: stop if less than estimated step cost remaining
        if usage and usage.total_cost and (args.max_budget - usage.total_cost) < _STEP_COST_ESTIMATE:
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

        # -- Create agent ---------------------------------------------------
        available_files = [resume_path] if resume_path else []

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
                max_actions_per_step=5,
                calculate_cost=True,
                use_judge=False,
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
        hitl_recovery_task = task
        # Single run — never restart the agent from scratch. Restarting loses all
        # form progress (the new agent navigates back to the job URL). If the agent
        # dies from consecutive failures, accept the result rather than wasting a
        # second full run on the same stuck field.
        history, cancelled = await _run_agent_once(hitl_recovery_task)
        is_done = history.is_done()
        final_result = history.final_result() or ""
        total_cost += history.usage.total_cost if history.usage else 0.0
        total_steps += len(history.history) if history.history else 0

        # Final cost event
        if history and history.usage:
            emit_cost(
                total_usd=total_cost,
                prompt_tokens=history.usage.total_prompt_tokens or 0,
                completion_tokens=history.usage.total_completion_tokens or 0,
            )

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
                    "best_effort_guess_count": best_effort_guess_count,
                    "best_effort_guess_fields": best_effort_guess_fields,
                    **runtime_learning_payload,
                },
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

        result_data = {
            "success": success,
            "steps": total_steps,
            "costUsd": round(total_cost, 6),
            "finalResult": final_result,
            "blocker": blocker,
            "platform": platform,
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
            await _cleanup_browser(
                browser,
                desktop_owns_browser,
                keep_browser_alive=keep_worker_browser_alive,
            )
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
        if browser is not None:
            with contextlib.suppress(Exception):
                await _cleanup_browser(
                    browser,
                    desktop_owns_browser,
                    keep_browser_alive=keep_worker_browser_alive,
                )
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
    _log_auth_debug_credentials(sensitive_data, platform=platform)

    # -- Domain lockdown ----------------------------------------------------
    from ghosthands.security.domain_lockdown import DomainLockdown

    lockdown = DomainLockdown(job_url=args.job_url, platform=platform)
    lockdown.freeze()
    allowed_domains = lockdown.get_allowed_domains()

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
        platform=platform,
    )

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
        use_vision="auto",
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
