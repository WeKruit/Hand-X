"""JSONL event emitter for desktop app IPC.

Emits events to a dedicated file descriptor that match the GH-Desktop-App's
ProgressEvent interface.  All logging and stray print() calls go to stderr
so the JSONL stream on stdout is never corrupted.

The stdout guard (``install_stdout_guard``) MUST be called before any
library imports to capture the real stdout fd.  After installation:

- ``sys.stdout`` points to stderr (stray prints are safe)
- ``emit_event()`` writes to the saved fd (clean JSONL stream)
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import IO, Any

# ── Stdout guard ──────────────────────────────────────────────────────

_jsonl_out: IO[str] | None = None  # Set by install_stdout_guard()


def install_stdout_guard() -> None:
    """Redirect sys.stdout to stderr; reserve the real stdout fd for JSONL.

    This MUST be called as early as possible -- before any library import
    that might cache ``sys.stdout``.  After this call:

    - ``print("anything")`` goes to stderr (safe)
    - ``emit_event(...)`` writes to the original stdout fd (clean JSONL)
    """
    import os

    global _jsonl_out

    if _jsonl_out is not None:
        return  # Already installed

    real_fd = os.dup(sys.stdout.fileno())
    _jsonl_out = os.fdopen(real_fd, "w", buffering=1)  # line-buffered
    sys.stdout = sys.stderr  # type: ignore[assignment]


def _get_output() -> IO[str]:
    """Return the JSONL output stream.

    If the guard was installed, this is the saved real-stdout fd.
    Otherwise falls back to ``sys.stdout`` (human-mode / tests).
    """
    if _jsonl_out is not None:
        return _jsonl_out
    return sys.stdout


# ── Core emitter ──────────────────────────────────────────────────────

_emit_lock = threading.Lock()


def emit_event(event_type: str, **kwargs: Any) -> None:
    """Emit a single JSONL event.

    Every event gets ``event`` and ``timestamp``.  All other fields are
    passed through as keyword arguments -- ``None`` values are omitted
    to keep the wire format compact.
    """
    event: dict[str, Any] = {
        "event": event_type,
        "timestamp": int(time.time() * 1000),
    }
    for key, value in kwargs.items():
        if value is not None:
            event[key] = value

    line = json.dumps(event, separators=(",", ":")) + "\n"
    with _emit_lock:
        out = _get_output()
        out.write(line)
        out.flush()


# ── Typed convenience emitters ────────────────────────────────────────


def emit_status(
    message: str,
    *,
    step: int | None = None,
    max_steps: int | None = None,
    job_id: str = "",
) -> None:
    """Emit a status update (agent progressed to a new step)."""
    emit_event(
        "status",
        message=message,
        step=step,
        maxSteps=max_steps,
        jobId=job_id or None,
    )


def emit_field_filled(
    field: str,
    value: str,
    *,
    method: str = "domhand",
) -> None:
    """Emit after a form field is successfully filled."""
    emit_event("field_filled", field=field, value=value, method=method)


def emit_field_failed(
    field: str,
    reason: str,
) -> None:
    """Emit when a field fill attempt fails."""
    emit_event("field_failed", field=field, reason=reason)


def emit_progress(
    step: int,
    max_steps: int,
    *,
    description: str = "",
) -> None:
    """Emit a progress snapshot."""
    emit_event("progress", step=step, maxSteps=max_steps, description=description)


def emit_done(
    success: bool,
    message: str,
    *,
    fields_filled: int = 0,
    fields_failed: int = 0,
    job_id: str = "",
    lease_id: str = "",
    result_data: dict[str, Any] | None = None,
) -> None:
    """Emit when the job is complete (success or failure)."""
    emit_event(
        "done",
        success=success,
        message=message,
        fields_filled=fields_filled,
        fields_failed=fields_failed,
        jobId=job_id or None,
        leaseId=lease_id or None,
        resultData=result_data,
    )


def emit_error(
    message: str,
    *,
    fatal: bool = False,
    job_id: str = "",
) -> None:
    """Emit an error event."""
    emit_event(
        "error",
        message=message,
        fatal=fatal,
        jobId=job_id or None,
    )


def emit_cost(
    total_usd: float,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """Emit a cost snapshot (cumulative LLM spend)."""
    emit_event(
        "cost",
        total_usd=round(total_usd, 6),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def emit_browser_ready(cdp_url: str) -> None:
    """Emit browser_ready event with CDP WebSocket URL."""
    emit_event("browser_ready", cdpUrl=cdp_url)


def emit_account_created(
    platform: str,
    email: str,
    password: str,
    *,
    url: str = "",
) -> None:
    """Emit when a new ATS platform account is created during automation."""
    emit_event(
        "account_created",
        platform=platform,
        email=email,
        password=password,
        url=url or None,
    )


def emit_awaiting_review(message: str = "Application filled — waiting for review") -> None:
    """Emit awaiting_review event when browser is open for user review."""
    emit_event("awaiting_review", message=message)


# ── Protocol handshake ───────────────────────────────────────────────

PROTOCOL_VERSION = 1


def emit_handshake() -> None:
    """Emit protocol version handshake as the first JSONL event."""
    emit_event("handshake", protocol_version=PROTOCOL_VERSION, min_desktop_version="0.1.0")
