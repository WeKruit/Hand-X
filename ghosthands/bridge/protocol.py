"""Desktop bridge stdin/stdout protocol helpers.

Manages the JSONL command protocol between the Electron desktop app and
the Hand-X engine.  Provides safe, serialized stdin reading and command
listeners for cancel and review workflows.
"""

from __future__ import annotations

import asyncio
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
# Mark the worker thread as daemon so it dies with the process (R-4 fix)
stdin_executor._thread_name_prefix = "hand-x-stdin"


async def read_stdin_line(timeout: float | None = None) -> str:
    """Read a single line from stdin with optional timeout.

    Uses a module-level lock to ensure only one reader at a time (R-2 fix),
    and a dedicated daemon ThreadPoolExecutor so cancelled reads don't leak
    threads from the default pool (R-4 fix).
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
                    return await future
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                loop.remove_reader(fileno)
        except (AttributeError, NotImplementedError, OSError, ValueError):
            line_future = loop.run_in_executor(stdin_executor, sys.stdin.readline)
            if timeout is None:
                return await line_future
            return await asyncio.wait_for(line_future, timeout=timeout)


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


async def wait_for_review_command(browser: Any, job_id: str, lease_id: str) -> None:
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
                line = await read_stdin_line(timeout=min(remaining, 5.0))
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
