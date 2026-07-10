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
import math
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
_state_lock = threading.Lock()
_pipe_broken = False
_event_version = 0
_last_timestamp_ms = 0
_run_status = "running"
_max_total_usd = 0.0
_max_prompt_tokens = 0
_max_completion_tokens = 0


def _set_run_status(status: str) -> None:
    global _run_status
    with _state_lock:
        _run_status = status


def _get_run_status() -> str:
    with _state_lock:
        return _run_status


def reset_event_state() -> None:
    """Reset per-process ordering and cumulative cost state before a run."""
    global _event_version, _last_timestamp_ms, _max_completion_tokens
    global _max_prompt_tokens, _max_total_usd, _pipe_broken, _run_status
    with _emit_lock:
        _event_version = 0
        _last_timestamp_ms = 0
        _pipe_broken = False
    with _state_lock:
        _run_status = "running"
        _max_total_usd = 0.0
        _max_prompt_tokens = 0
        _max_completion_tokens = 0


def emit_event(event_type: str, **kwargs: Any) -> None:
    """Emit a single JSONL event.

    Every event gets ``event`` and ``timestamp``.  All other fields are
    passed through as keyword arguments -- ``None`` values are omitted
    to keep the wire format compact.
    """
    global _event_version, _last_timestamp_ms, _pipe_broken
    if _pipe_broken:
        if event_type in ("done", "error"):
            print(
                f"WARNING: JSONL pipe broken — suppressed critical event '{event_type}'",
                file=sys.stderr,
            )
        return

    with _emit_lock:
        _event_version += 1
        _last_timestamp_ms = max(_last_timestamp_ms + 1, int(time.time() * 1000))
        event: dict[str, Any] = {
            "type": event_type,
            "event": event_type,
            "version": _event_version,
            "sequence": _event_version,
            "timestamp": _last_timestamp_ms,
        }
        for key, value in kwargs.items():
            if key in {"type", "event", "version", "sequence", "timestamp"}:
                continue
            if value is not None:
                event[key] = value

        line = json.dumps(event, separators=(",", ":")) + "\n"
        try:
            out = _get_output()
            out.write(line)
            out.flush()
        except (BrokenPipeError, OSError):
            _pipe_broken = True
            print("JSONL pipe broken — further events will be suppressed", file=sys.stderr)


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
        status=_get_run_status(),
        step=step,
        maxSteps=max_steps,
        jobId=job_id or None,
    )


def emit_phase(phase: str, detail: str | None = None) -> None:
    """Emit a high-level progress phase for user display."""
    emit_event("phase", status=_get_run_status(), phase=phase, detail=detail)


def emit_field_filled(
    field: str,
    value: str,
    *,
    method: str = "domhand",
    field_id: str | None = None,
    question_type: str | None = None,
    source: str | None = None,
    answer_mode: str | None = None,
    confidence: float | None = None,
    required: bool | None = None,
    section_label: str | None = None,
    state: str | None = None,
) -> None:
    """Emit after a form field is successfully filled."""
    emit_event(
        "field_filled",
        status=_get_run_status(),
        field=field,
        value=value,
        method=method,
        field_id=field_id,
        question_type=question_type,
        source=source,
        answer_mode=answer_mode,
        confidence=confidence,
        required=required,
        section_label=section_label,
        state=state,
    )


def emit_field_failed(
    field: str,
    reason: str,
) -> None:
    """Emit when a field fill attempt fails."""
    emit_event("field_failed", status=_get_run_status(), field=field, reason=reason)


def emit_progress(
    step: int,
    max_steps: int,
    *,
    description: str = "",
) -> None:
    """Emit a progress snapshot."""
    emit_event(
        "progress",
        status=_get_run_status(),
        step=step,
        maxSteps=max_steps,
        description=description,
    )


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
        status=_get_run_status(),
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
    code: str | None = None,
) -> None:
    """Emit an error event."""
    emit_event(
        "error",
        message=message,
        fatal=fatal,
        jobId=job_id or None,
        code=code,
    )


def emit_cost(
    total_usd: float,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_summary: dict[str, Any] | None = None,
) -> None:
    """Emit a cost snapshot (cumulative LLM spend)."""
    global _max_completion_tokens, _max_prompt_tokens, _max_total_usd
    with _state_lock:
        _max_total_usd = max(_max_total_usd, float(total_usd or 0.0))
        _max_prompt_tokens = max(_max_prompt_tokens, int(prompt_tokens or 0))
        _max_completion_tokens = max(_max_completion_tokens, int(completion_tokens or 0))
        cumulative_usd = round(_max_total_usd, 6)
        cumulative_prompt_tokens = _max_prompt_tokens
        cumulative_completion_tokens = _max_completion_tokens

    summary = dict(cost_summary or {})
    summary["actualCostCents"] = max(
        int(summary.get("actualCostCents") or 0),
        math.ceil(cumulative_usd * 100),
    )
    summary["total_tracked_cost_usd"] = max(
        float(summary.get("total_tracked_cost_usd") or 0.0),
        cumulative_usd,
    )
    emit_event(
        "cost",
        status=_get_run_status(),
        total_usd=cumulative_usd,
        prompt_tokens=cumulative_prompt_tokens,
        completion_tokens=cumulative_completion_tokens,
        cost_summary=summary,
        costSummary=summary,
    )


def emit_browser_ready(
    cdp_url: str,
    *,
    target_id: str | None = None,
    page_url: str | None = None,
) -> None:
    """Emit the browser endpoint and exact target selected for this run."""
    emit_event(
        "browser_ready",
        status=_get_run_status(),
        cdpUrl=cdp_url,
        targetId=target_id,
        pageUrl=page_url,
    )


def emit_run_state(
    state: str,
    *,
    message: str | None = None,
    job_id: str = "",
    target_id: str | None = None,
) -> None:
    """Emit an explicit VALET run-state transition."""
    status = {
        "paused": "waiting_human",
        "waiting_human": "waiting_human",
        "review_ready": "review_ready",
        "cancelled": "cancelled",
        "budget_reached": "budget_reached",
        "failed_retryable": "failed_retryable",
        "failed_terminal": "failed_terminal",
    }.get(state, "running")
    _set_run_status(status)
    emit_event(
        "run_state",
        status=status,
        state=state,
        message=message,
        jobId=job_id or None,
        targetId=target_id,
    )


def emit_paused(*, job_id: str = "", target_id: str | None = None) -> None:
    """Emit the acknowledgement for ``pause_job``."""
    _set_run_status("waiting_human")
    emit_event(
        "paused",
        status="waiting_human",
        state="paused",
        paused=True,
        jobId=job_id or None,
        targetId=target_id,
    )


def emit_resumed(*, job_id: str = "", target_id: str | None = None) -> None:
    """Emit the acknowledgement for ``resume_job``."""
    _set_run_status("running")
    emit_event(
        "resumed",
        status="running",
        state="running",
        paused=False,
        jobId=job_id or None,
        targetId=target_id,
    )


def emit_needs_answer(
    *,
    field_id: str,
    label: str,
    field_type: str,
    options: list[str],
    required: bool,
    section: str,
    job_id: str = "",
) -> None:
    """Emit one Go-compatible HITL question."""
    normalized_type = str(field_type or "text").strip().lower().replace("_", "-")
    if normalized_type in {"select", "choice", "radio", "radio-group", "button-group"}:
        normalized_type = "choice"
    elif normalized_type in {"checkbox", "checkboxes", "checkbox-group", "multi-select"}:
        normalized_type = "checkbox"
    else:
        normalized_type = "text"
    question = {
        "fieldId": field_id,
        "fieldLabel": label,
        "fieldType": normalized_type,
        "options": list(options),
        "required": bool(required),
        "section": section,
    }
    _set_run_status("waiting_human")
    emit_event(
        "needs_answer",
        status="waiting_human",
        message=f"Input required: {label}",
        jobId=job_id or None,
        waiting_human=True,
        questions=[question],
        field={
            "id": field_id,
            "label": label,
            "type": normalized_type,
            "options": list(options),
            "required": bool(required),
            "section": section,
        },
    )


def emit_review_ready(
    *,
    cdp_url: str,
    target_id: str,
    page_url: str | None = None,
    message: str = "Application ready for review",
    cost_summary: dict[str, Any] | None = None,
) -> None:
    """Emit the terminal presubmit checkpoint without claiming submission."""
    if not cdp_url.strip():
        raise ValueError("review_ready requires an explicit cdp_url")
    if not target_id.strip():
        raise ValueError("review_ready requires an explicit target_id")
    summary = dict(cost_summary or {})
    if summary:
        total_usd = float(summary.get("total_tracked_cost_usd") or 0.0)
        summary["actualCostCents"] = max(
            int(summary.get("actualCostCents") or 0),
            math.ceil(total_usd * 100),
        )
    _set_run_status("review_ready")
    emit_event(
        "review_ready",
        status="review_ready",
        message=message,
        cdpUrl=cdp_url,
        targetId=target_id,
        pageUrl=page_url,
        review_ready=True,
        costSummary=summary or None,
    )


def emit_account_created(
    platform: str,
    email: str,
    password: str,
    *,
    domain: str | None = None,
    credential_status: str = "pending_verification",
    note: str | None = None,
    evidence: str | None = None,
    url: str = "",
) -> None:
    """Emit when a new ATS platform account is created during automation.

    The Desktop app stores the credentials from this IPC payload directly,
    so the plaintext password must be included alongside the legacy
    ``password_provided`` flag.
    """
    emit_event(
        "account_created",
        platform=platform,
        domain=domain or None,
        email=email,
        password=password,
        password_provided=True,
        credentialStatus=credential_status or "active",
        note=note or None,
        evidence=evidence or None,
        url=url or None,
    )


def emit_awaiting_review(
    message: str = (
        "We've filled out your application. Please review the form in the browser "
        "window, verify all fields are correct, then mark review complete."
    ),
    cdp_url: str | None = None,
    target_id: str | None = None,
    page_url: str | None = None,
) -> None:
    """Emit awaiting_review event when browser is open for user review."""
    _set_run_status("review_ready")
    emit_event(
        "awaiting_review",
        status="review_ready",
        message=message,
        cdpUrl=cdp_url,
        targetId=target_id,
        pageUrl=page_url,
    )


# ── Protocol handshake ───────────────────────────────────────────────

PROTOCOL_VERSION = 1


def emit_handshake() -> None:
    """Emit protocol version handshake as the first JSONL event."""
    reset_event_state()
    emit_event("handshake", protocol_version=PROTOCOL_VERSION, min_desktop_version="0.1.0")


# ── Lease protocol events ────────────────────────────────────────────


def emit_lease_acquired(lease_id: str, job_id: str = "") -> None:
    """Emit when a lease is acquired from the Desktop app."""
    emit_event("lease_acquired", leaseId=lease_id, jobId=job_id or None)


def emit_lease_released(lease_id: str, reason: str = "completed") -> None:
    """Emit when a lease is released (agent done or cancelled)."""
    emit_event("lease_released", leaseId=lease_id, reason=reason)


def emit_lease_heartbeat(lease_id: str) -> None:
    """Emit periodic lease heartbeat to indicate the process is alive."""
    emit_event("lease_heartbeat", leaseId=lease_id)
