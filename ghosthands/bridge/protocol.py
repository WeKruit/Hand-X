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
_answer_events: dict[str, asyncio.Event] = {}
_hitl_lock = asyncio.Lock()
# M13: Cancel event — set when the run is cancelled so pending
# get_field_answer() calls wake up immediately instead of blocking 300s.
# Must be cleared at run start via reset_hitl_state().
_cancel_event = asyncio.Event()


def reset_hitl_state() -> None:
    """Clear all HITL state between runs.

    Must be called at the start of each job to prevent cancel/answer
    leakage from previous runs in the same process.
    """
    _pending_answers.clear()
    _answer_events.clear()
    _cancel_event.clear()


def put_field_answer(field_id: str, answer: str, *, field_label: str = "") -> None:
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
    _pending_answers[key] = answer
    evt = _answer_events.get(key)
    if evt:
        evt.set()


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
        # Check if answer already arrived
        if key in _pending_answers:
            return _pending_answers.pop(key)

        # Create event while holding the lock
        evt = asyncio.Event()
        _answer_events[key] = evt

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
                return _pending_answers.pop(key, None)

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


async def listen_for_cancel(
    agent: Any,
    cancel_requested: asyncio.Event | None = None,
) -> None:
    """Read stdin concurrently during agent run for cancel and answer commands."""
    while not agent.state.stopped:
        try:
            line = await read_stdin_line(timeout=1.0)
        except TimeoutError:
            continue
        except asyncio.CancelledError:
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
            agent.state.stopped = True
            break

        line = line.strip()
        if not line:
            continue

        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue

        # S-12: ignore valid JSON that isn't an object (e.g. [], "str", 1)
        if not isinstance(cmd, dict):
            continue

        cmd_type = cmd.get("type")
        if cmd_type in {"cancel", "cancel_job"}:
            logger.info("cancel_command_received_from_stdin", command_type=cmd_type)
            _cancel_event.set()  # M13: wake any pending HITL waits
            if cancel_requested is not None:
                cancel_requested.set()
            agent.state.stopped = True
            break
        elif cmd_type == "answer_field":
            field_id = cmd.get("field_id", "")
            field_label = cmd.get("field_label", "")
            answer = cmd.get("answer", "")
            key = field_id or field_label
            if key:
                logger.info("answer_field_received", field_id=field_id, field_label=field_label)
                put_field_answer(key, answer)
        elif cmd_type == "skip_field":
            field_id = cmd.get("field_id", "")
            field_label = cmd.get("field_label", "")
            key = field_id or field_label
            if key:
                logger.info("skip_field_received", field_id=field_id, field_label=field_label)
                put_field_answer(key, "")


async def wait_for_review_command(browser: Any, job_id: str, lease_id: str) -> str:
    """Wait for a command from Electron on stdin.

    Expected commands:
    - {"type": "complete_review"} -- user approved, close browser
    - {"type": "cancel_job"}     -- user cancelled, close browser
    - {"type": "cancel"}         -- user cancelled, close browser

    Times out after 24 hours if no command is received.

    Returns
    -------
    str
        One of ``"complete"``, ``"cancel"``, ``"timeout"``, or ``"eof"``.

    The CLI is responsible for emitting the terminal ``done`` event once
    this function returns a review outcome.
    """
    from ghosthands.output.jsonl import emit_error, emit_status

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
                    "Your review session will expire in about 1 hour. Please submit or cancel soon.",
                    job_id=job_id,
                )
                warning_emitted = True

            if remaining <= 0:
                logger.warning("review_timeout_exceeded", timeout_seconds=review_timeout_seconds)
                emit_error(
                    "Review session expired — please submit or cancel your application",
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

            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                continue

            # S-12: ignore valid JSON that isn't an object (e.g. [], "str", 1)
            if not isinstance(cmd, dict):
                continue

            cmd_type = cmd.get("type", "")

            if cmd_type == "complete_review":
                logger.info("review_completed", job_id=job_id, lease_id=lease_id)
                emit_status("Review complete -- closing browser", job_id=job_id)
                result = "complete"
                break
            elif cmd_type in {"cancel", "cancel_job"}:
                logger.info("review_cancelled", job_id=job_id, lease_id=lease_id)
                emit_status("Review cancelled by user", job_id=job_id)
                result = "cancel"
                break
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        with contextlib.suppress(Exception):
            await browser.stop()

    return result
