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


async def listen_for_cancel(
    agent: Any,
    cancel_requested: asyncio.Event | None = None,
) -> None:
    """Read stdin concurrently during agent run for cancel commands."""
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
            if cancel_requested is not None:
                cancel_requested.set()
            agent.state.stopped = True
            break


async def wait_for_review_command(browser: Any, job_id: str, lease_id: str) -> str:
    """Wait for a command from Electron on stdin.

    Expected commands:
    - {"type": "complete_review"} -- user approved, close browser
    - {"type": "cancel_job"}     -- user cancelled, close browser
    - {"type": "cancel"}         -- user cancelled, close browser

    Times out after 30 minutes if no command is received.

    Returns
    -------
    str
        One of ``"complete"``, ``"cancel"``, ``"timeout"``, or ``"eof"``.

    The CLI is responsible for emitting the terminal ``done`` event once
    this function returns a review outcome.
    """
    from ghosthands.output.jsonl import emit_error, emit_status

    review_timeout_seconds = 30 * 60  # 30 minutes
    warning_before_seconds = 5 * 60  # Warn 5 minutes before timeout.
    warning_emitted = False
    start_time = _time.monotonic()
    result = "eof"

    try:
        while True:
            elapsed = _time.monotonic() - start_time
            remaining = review_timeout_seconds - elapsed

            if not warning_emitted and remaining <= warning_before_seconds and remaining > 0:
                emit_status(
                    "Your review session will expire in 5 minutes. Please submit or cancel soon.",
                    job_id=job_id,
                )
                warning_emitted = True

            if remaining <= 0:
                logger.warning("review_timeout_exceeded", timeout_seconds=review_timeout_seconds)
                emit_error(
                    "Review timed out after 30 minutes",
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
    # NOTE: browser cleanup is NOT done here — the caller (cli.py) is
    # responsible for calling _cleanup_browser() with ownership awareness.
    # Previously this finally block called browser.stop() unconditionally,
    # which only disconnects Playwright but doesn't kill a self-launched
    # Chromium process, causing process leaks.

    return result
