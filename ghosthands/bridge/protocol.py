"""Desktop bridge stdin/stdout protocol helpers.

Manages the JSONL command protocol between the Electron desktop app and
the Hand-X engine.  Provides safe, serialized stdin reading and command
listeners for cancel and review workflows.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import json
import sys
import time as _time
from typing import Any

import structlog

logger = structlog.get_logger()

_COMMAND_TYPES = frozenset(
    {
        "answer_field",
        "save_answer",
        "skip_field",
        "pause_job",
        "resume_job",
        "cancel",
        "cancel_job",
        "complete_review",
    }
)


def parse_bridge_command(line: str) -> dict[str, Any] | None:
    """Parse one supported JSON object from the VALET stdin channel."""
    try:
        command = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(command, dict) or command.get("type") not in _COMMAND_TYPES:
        return None
    if command["type"] in {"answer_field", "save_answer", "skip_field"} and not (
        str(command.get("field_id") or "").strip() or str(command.get("field_label") or "").strip()
    ):
        return None
    return command


stdin_lock = asyncio.Lock()
stdin_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="hand-x-stdin",
)
atexit.register(stdin_executor.shutdown, wait=False, cancel_futures=True)


_STDIN_LINE_MAX_BYTES = 65536  # S-09: guard against memory exhaustion


async def read_stdin_line(timeout: float | None = None) -> str:
    """Read a single line from stdin with optional timeout.

    Uses a module-level lock to ensure only one reader at a time (R-2 fix),
    and a dedicated daemon ThreadPoolExecutor so cancelled reads don't leak
    threads from the default pool (R-4 fix).

    Lines longer than _STDIN_LINE_MAX_BYTES are rejected (S-09): an empty
    string is returned so the caller treats the line as a no-op.
    """
    async with stdin_lock:
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
                    line = await future
                else:
                    line = await asyncio.wait_for(future, timeout=timeout)
            finally:
                loop.remove_reader(fileno)
        except (AttributeError, NotImplementedError, OSError, ValueError):
            line_future = loop.run_in_executor(stdin_executor, sys.stdin.readline)
            if timeout is None:
                line = await line_future
            else:
                line = await asyncio.wait_for(line_future, timeout=timeout)

    # S-09: reject oversized lines to prevent memory exhaustion attacks.
    if len(line) > _STDIN_LINE_MAX_BYTES:
        logger.warning(
            "stdin_line_too_large",
            size=len(line),
            limit=_STDIN_LINE_MAX_BYTES,
        )
        return ""
    return line


# ── Shared answer queue for HITL field answers from Desktop ──────────
# When the Desktop sends { type: "answer_field", field_id, answer },
# the answer is stored here. domhand_fill can await get_field_answer()
# to block until the user responds.
#
# Keys are field_id (unique per field occurrence) to avoid collisions
# when two fields share the same label (e.g. "Email" in personal info
# and "Email" in emergency contact).  Falls back to field_label for
# backward compatibility with older Desktop versions.
_pending_answers: dict[str, str] = {}
_saved_answer_keys: set[str] = set()
_answer_events: dict[str, asyncio.Event] = {}
_hitl_lock = asyncio.Lock()
# M13: Cancel event — set when the run is cancelled so pending
# get_field_answer() calls wake up immediately instead of blocking 300s.
# Must be cleared at run start via reset_hitl_state().
_cancel_event = asyncio.Event()
_run_resume_event = asyncio.Event()
_run_resume_event.set()


_hitl_available = False  # Set True when listen_for_cancel starts (JSONL/Desktop mode)


def reset_hitl_state() -> None:
    """Clear all HITL state between runs.

    Must be called at the start of each job to prevent cancel/answer
    leakage from previous runs in the same process.
    """
    global _cancel_event, _hitl_available, _run_resume_event
    _pending_answers.clear()
    _saved_answer_keys.clear()
    _answer_events.clear()
    _cancel_event = asyncio.Event()
    _run_resume_event = asyncio.Event()
    _run_resume_event.set()
    _hitl_available = False


def activate_hitl() -> None:
    """Mark stdin HITL active before the listener task is scheduled."""
    global _hitl_available
    _hitl_available = True


async def wait_for_run_resume() -> bool:
    """Wait at a browser-action boundary until resumed or cancelled."""
    if _cancel_event.is_set():
        return False
    if not _hitl_available:
        return True
    await _run_resume_event.wait()
    return not _cancel_event.is_set()


def _pause_agent(agent: Any) -> None:
    pause = getattr(agent, "pause", None)
    if callable(pause):
        pause()
    agent.state.paused = True
    pause_event = getattr(agent, "_external_pause_event", None)
    if isinstance(pause_event, asyncio.Event):
        pause_event.clear()
    _run_resume_event.clear()


def _resume_agent(agent: Any) -> None:
    resume = getattr(agent, "resume", None)
    if callable(resume):
        resume()
    agent.state.paused = False
    pause_event = getattr(agent, "_external_pause_event", None)
    if isinstance(pause_event, asyncio.Event):
        pause_event.set()
    _run_resume_event.set()


def _stop_agent(agent: Any) -> None:
    stop = getattr(agent, "stop", None)
    if callable(stop):
        stop()
    agent.state.stopped = True
    pause_event = getattr(agent, "_external_pause_event", None)
    if isinstance(pause_event, asyncio.Event):
        pause_event.set()
    _run_resume_event.set()


def is_hitl_available() -> bool:
    """Return True if a stdin listener is active (JSONL/Desktop mode).

    In non-JSONL CLI mode, no listener runs, so HITL waits would block
    indefinitely. Callers should skip the wait and treat as "no answer".
    """
    return _hitl_available


def _normalize_field_answer(answer: Any) -> str:
    if isinstance(answer, (list, tuple)):
        return ", ".join(str(item).strip() for item in answer if str(item).strip())
    if isinstance(answer, bool):
        return "true" if answer else "false"
    return str(answer or "")


def put_field_answer(
    field_id: str,
    answer: Any,
    *,
    field_label: str = "",
    save_answer: bool = False,
) -> None:
    """Store an answer received from the Desktop for a pending HITL field.

    Parameters
    ----------
    field_id:
        Unique key for the field (preferred).  Falls back to *field_label*
        when empty/None for backward compatibility.
    answer:
        The user-provided value (empty string for a skip).
    field_label:
        Optional human-readable label, used as fallback key when
        *field_id* is not provided.
    """
    key = field_id or field_label
    if not key:
        return
    answer = _normalize_field_answer(answer)
    _pending_answers[key] = answer
    if save_answer:
        _saved_answer_keys.add(key)
    else:
        _saved_answer_keys.discard(key)
    evt = _answer_events.get(key)
    if evt:
        evt.set()
    # Also store under field_label if different from key, so that
    # get_field_answer waiting on field_id can also be woken by a
    # legacy Desktop that only sends field_label.
    if field_label and field_label != key:
        _pending_answers[field_label] = answer
        if save_answer:
            _saved_answer_keys.add(field_label)
        else:
            _saved_answer_keys.discard(field_label)
        evt2 = _answer_events.get(field_label)
        if evt2:
            evt2.set()


def consume_field_answer_save(field_id: str, *, field_label: str = "") -> bool:
    """Consume whether the most recent answer requested verified-answer persistence."""
    keys = {key for key in (field_id, field_label) if key}
    saved = bool(keys & _saved_answer_keys)
    _saved_answer_keys.difference_update(keys)
    return saved


async def get_field_answer(
    field_id: str,
    timeout: float = 300.0,
    *,
    field_label: str = "",
) -> str | None:
    """Wait for a HITL answer from the Desktop, with timeout.

    Parameters
    ----------
    field_id:
        Unique key for the field (preferred).  Falls back to *field_label*
        when empty/None for backward compatibility.
    timeout:
        Seconds to wait before giving up.
    field_label:
        Optional human-readable label, used as fallback key when
        *field_id* is not provided.

    Returns the answer string, or None if timed out.
    """
    key = field_id or field_label
    if not key:
        return None

    async with _hitl_lock:
        # Check if answer already arrived (under either key)
        if key in _pending_answers:
            return _pending_answers.pop(key)
        if field_label and field_label != key and field_label in _pending_answers:
            return _pending_answers.pop(field_label)

        # Register event under primary key. Also register under field_label
        # so legacy Desktop answers (keyed by label only) wake us.
        evt = asyncio.Event()
        _answer_events[key] = evt
        if field_label and field_label != key:
            _answer_events[field_label] = evt  # same event, two keys

    # Wait outside the lock so other coroutines can put_field_answer.
    # M13: Also watch the cancel event so cancellation wakes us immediately.
    try:
        cancel_task = asyncio.create_task(_cancel_event.wait())
        answer_task = asyncio.create_task(evt.wait())
        done, pending = await asyncio.wait(
            {cancel_task, answer_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

        if cancel_task in done:
            # Run was cancelled — treat as skip
            logger.info("hitl_cancelled_during_wait", field_id=key)
            return None

        if answer_task in done:
            async with _hitl_lock:
                answer = _pending_answers.pop(key, None)
                if answer is None and field_label and field_label != key:
                    answer = _pending_answers.pop(field_label, None)
                return answer

        # Neither completed — timeout
        async with _hitl_lock:
            _pending_answers.pop(key, None)  # Clean up stale answer if it arrived late
        return None
    except (asyncio.TimeoutError, TimeoutError):
        async with _hitl_lock:
            _pending_answers.pop(key, None)
        return None
    finally:
        async with _hitl_lock:
            _answer_events.pop(key, None)
            if field_label and field_label != key:
                _answer_events.pop(field_label, None)
                _pending_answers.pop(field_label, None)


async def listen_for_cancel(
    agent: Any,  # noqa: ANN401
    cancel_requested: asyncio.Event | None = None,
    *,
    job_id: str = "",
) -> None:
    """Read stdin concurrently during agent run for cancel and answer commands."""
    global _hitl_available
    from ghosthands.output.jsonl import emit_paused, emit_resumed, emit_run_state

    _hitl_available = True
    while not agent.state.stopped:
        try:
            line = await read_stdin_line(timeout=1.0)
        except TimeoutError:
            continue
        except asyncio.CancelledError:
            _hitl_available = False
            raise
        except Exception:
            await asyncio.sleep(0.1)
            continue

        if not line:
            # EOF — stdin closed (Electron died); treat as cancellation
            logger.warning("stdin_eof_treating_as_cancel")
            _cancel_event.set()  # M13: wake any pending HITL waits
            if cancel_requested is not None:
                cancel_requested.set()
            _stop_agent(agent)
            break

        line = line.strip()
        if not line:
            continue

        cmd = parse_bridge_command(line)
        if cmd is None:
            continue

        cmd_type = cmd.get("type")
        if cmd_type in {"cancel", "cancel_job"}:
            logger.info("cancel_command_received_from_stdin", command_type=cmd_type)
            _cancel_event.set()  # M13: wake any pending HITL waits
            if cancel_requested is not None:
                cancel_requested.set()
            _stop_agent(agent)
            break
        elif cmd_type == "pause_job":
            if not bool(getattr(agent.state, "paused", False)):
                _pause_agent(agent)
                target_id = getattr(getattr(agent, "browser_session", None), "agent_focus_target_id", None)
                emit_paused(job_id=job_id, target_id=target_id)
                emit_run_state("paused", job_id=job_id, target_id=target_id)
        elif cmd_type == "resume_job":
            if bool(getattr(agent.state, "paused", False)):
                _resume_agent(agent)
                target_id = getattr(getattr(agent, "browser_session", None), "agent_focus_target_id", None)
                emit_resumed(job_id=job_id, target_id=target_id)
                emit_run_state("running", job_id=job_id, target_id=target_id)
        elif cmd_type in {"answer_field", "save_answer"}:
            field_id = cmd.get("field_id", "")
            field_label = cmd.get("field_label", "")
            answer = cmd.get("answer", "")
            key = field_id or field_label
            if key:
                logger.info("answer_field_received", field_id=field_id, field_label=field_label)
                put_field_answer(
                    field_id,
                    answer,
                    field_label=field_label,
                    save_answer=cmd_type == "save_answer" or cmd.get("save_answer") is True,
                )
                if not bool(getattr(agent.state, "paused", False)):
                    emit_run_state("running", message="Input received", job_id=job_id)
        elif cmd_type == "skip_field":
            field_id = cmd.get("field_id", "")
            field_label = cmd.get("field_label", "")
            key = field_id or field_label
            if key:
                logger.info("skip_field_received", field_id=field_id, field_label=field_label)
                put_field_answer(field_id, "", field_label=field_label)
                if not bool(getattr(agent.state, "paused", False)):
                    emit_run_state("running", message="Field skipped", job_id=job_id)
    _hitl_available = False


async def wait_for_review_command(browser: Any, job_id: str, lease_id: str) -> str:
    """Wait for a command from Electron on stdin.

    Expected commands:
    - {"type": "complete_review"} -- user approved, detach engine
    - {"type": "cancel_job"}     -- user cancelled, detach engine
    - {"type": "cancel"}         -- user cancelled, detach engine

    Times out after 24 hours if no command is received.

    Returns
    -------
    str
        One of ``"complete"``, ``"cancel"``, ``"timeout"``, or ``"eof"``.

    The CLI is responsible for emitting the terminal ``done`` event once
    this function returns a review outcome.
    """
    from ghosthands.output.jsonl import emit_error, emit_paused, emit_resumed, emit_run_state, emit_status

    review_timeout_seconds = 24 * 60 * 60  # 24 hours — users may come back next day
    warning_before_seconds = 60 * 60  # Warn 1 hour before timeout.
    warning_emitted = False
    start_time = _time.monotonic()
    result = "eof"

    try:
        while True:
            elapsed = _time.monotonic() - start_time
            remaining = review_timeout_seconds - elapsed

            if not warning_emitted and remaining <= warning_before_seconds and remaining > 0:
                emit_status(
                    "Your review session will expire in about 1 hour. Please mark review complete or cancel soon.",
                    job_id=job_id,
                )
                warning_emitted = True

            if remaining <= 0:
                logger.warning("review_timeout_exceeded", timeout_seconds=review_timeout_seconds)
                emit_error(
                    "Review session expired; the engine detached and the browser tab remains open",
                    fatal=True,
                    job_id=job_id,
                )
                result = "timeout"
                break

            try:
                line = await read_stdin_line(timeout=min(remaining, 5.0))
            except TimeoutError:
                continue

            if not line:
                result = "eof"
                break  # stdin closed -- Electron died

            line = line.strip()
            if not line:
                continue

            cmd = parse_bridge_command(line)
            if cmd is None:
                continue

            cmd_type = cmd.get("type", "")

            if cmd_type == "complete_review":
                logger.info("review_completed", job_id=job_id, lease_id=lease_id)
                emit_status("Review complete -- detaching engine", job_id=job_id)
                result = "complete"
                break
            elif cmd_type in {"cancel", "cancel_job"}:
                logger.info("review_cancelled", job_id=job_id, lease_id=lease_id)
                emit_status("Review cancelled by user", job_id=job_id)
                result = "cancel"
                break
            elif cmd_type == "pause_job":
                target_id = getattr(browser, "agent_focus_target_id", None)
                emit_paused(job_id=job_id, target_id=target_id)
                emit_run_state("paused", job_id=job_id, target_id=target_id)
            elif cmd_type == "resume_job":
                target_id = getattr(browser, "agent_focus_target_id", None)
                emit_resumed(job_id=job_id, target_id=target_id)
                emit_run_state("review_ready", job_id=job_id, target_id=target_id)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        with contextlib.suppress(Exception):
            await browser.detach_keep_alive()

    return result
