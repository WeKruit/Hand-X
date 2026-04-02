"""One-shot terminal JSONL contract for Desktop IPC: always ``cost`` then ``done``.

Every run should end with this pair so the Electron host can persist spend/steps —
whether the user cancels gracefully, the agent fails, we error out, or the process
receives SIGTERM/SIGINT. Only the ``terminationStatus`` (and related flags) change.

``result_data`` includes ``agentHistory`` (full serialized browser-use step history + usage
metadata when available). The in-process snapshot is refreshed after each agent step so
SIGTERM still receives the last completed history alongside ``costSummary``.

``SIGKILL`` cannot be intercepted from Python; no contract is possible there.
"""

from __future__ import annotations

import signal
import threading
from typing import Any

_lock = threading.RLock()
_emitted = False
_state: dict[str, Any] = {
    "job_id": "",
    "lease_id": "",
    "platform": "generic",
    "last_cost_summary": {},
    "step_count": 0,
    "agent_history_payload": None,
}


def reset() -> None:
    """Call at the start of each JSONL run."""
    global _emitted, _state
    with _lock:
        _emitted = False
        _state = {
            "job_id": "",
            "lease_id": "",
            "platform": "generic",
            "last_cost_summary": {},
            "step_count": 0,
            "agent_history_payload": None,
        }


def configure(*, job_id: str = "", lease_id: str = "", platform: str = "generic") -> None:
    with _lock:
        _state["job_id"] = job_id
        _state["lease_id"] = lease_id
        _state["platform"] = platform


def update_runtime_snapshot(
    cost_summary: dict[str, Any] | None,
    *,
    step_count: int | None = None,
    agent_history: dict[str, Any] | None = None,
) -> None:
    """Best-effort state for signal handlers (cost, steps, full agent history JSON)."""
    with _lock:
        if cost_summary:
            _state["last_cost_summary"] = dict(cost_summary)
        if step_count is not None:
            _state["step_count"] = int(step_count)
        if agent_history is not None:
            _state["agent_history_payload"] = agent_history


def was_terminal_emitted() -> bool:
    with _lock:
        return _emitted


def emit_run_terminal(
    *,
    termination_status: str,
    success: bool,
    message: str,
    fields_filled: int = 0,
    fields_failed: int = 0,
    job_id: str | None = None,
    lease_id: str | None = None,
    result_data: dict[str, Any] | None = None,
    cost_summary: dict[str, Any] | None = None,
    total_cost_usd: float | None = None,
    agent_history: dict[str, Any] | None = None,
) -> bool:
    """Emit final ``cost`` + ``done``. Idempotent: returns False if already emitted."""
    global _emitted
    from ghosthands.output.jsonl import emit_cost, emit_done

    with _lock:
        if _emitted:
            return False
        _emitted = True
        snap = dict(_state)
        jid = job_id if job_id is not None else snap.get("job_id") or ""
        lid = lease_id if lease_id is not None else snap.get("lease_id") or ""
        cs = cost_summary if cost_summary is not None else (snap.get("last_cost_summary") or {})
        if not isinstance(cs, dict):
            cs = {}
        steps = int(snap.get("step_count") or 0)
        plat = str(snap.get("platform") or "generic")
        snap_agent_hist = snap.get("agent_history_payload")

    total = float(total_cost_usd if total_cost_usd is not None else cs.get("total_tracked_cost_usd") or 0)
    pt = int(cs.get("total_tracked_prompt_tokens") or 0)
    ct = int(cs.get("total_tracked_completion_tokens") or 0)

    rd: dict[str, Any] = {**(result_data or {})}
    rd["terminationStatus"] = termination_status
    rd.setdefault("platform", plat)
    if "steps" not in rd:
        rd["steps"] = steps
    rd.setdefault("costUsd", round(total, 6))
    rd.setdefault("costSummary", cs)
    rd["success"] = success
    ah = agent_history if agent_history is not None else snap_agent_hist
    if isinstance(ah, dict) and "agentHistory" not in rd:
        rd["agentHistory"] = ah

    emit_cost(
        total_usd=total,
        prompt_tokens=pt,
        completion_tokens=ct,
        cost_summary=cs if cs else None,
    )
    emit_done(
        success=success,
        message=message,
        fields_filled=fields_filled,
        fields_failed=fields_failed,
        job_id=jid,
        lease_id=lid,
        result_data=rd,
    )
    return True


def _signal_handler(signum: int, frame: object) -> None:  # noqa: ARG001
    name = (
        "sigterm"
        if signum == signal.SIGTERM
        else "sigint"
        if signum == signal.SIGINT
        else f"signal_{signum}"
    )
    emit_run_terminal(
        termination_status=name,
        success=False,
        message=f"Process terminated ({name})",
        result_data={"cancelled": True, "success": False},
    )
    import sys

    raise SystemExit(1 if signum != signal.SIGINT else 130)


def install_signal_handlers() -> None:
    """Register SIGTERM/SIGINT to emit the terminal contract then exit.

    Call after :func:`reset` / :func:`configure` so job ids are populated.
    """
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
