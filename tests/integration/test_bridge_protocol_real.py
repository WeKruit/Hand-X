"""Integration tests — real asyncio protocol layer for the desktop bridge.

Exercises ``ghosthands.bridge.protocol`` with actual asyncio event loops,
real OS pipes, and real file descriptors.  No mocks, no monkeypatching.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipe_stdin():
    """Create a real OS pipe and return (read_fd, write_fd, file_object).

    The returned file object wraps the read end and is suitable for
    replacing ``sys.stdin``.  The write end is a raw fd for ``os.write()``.
    """
    r, w = os.pipe()
    read_file = os.fdopen(r, "r")
    return read_file, w


class _FakeAgentState:
    """Minimal agent state object with a ``stopped`` flag."""

    def __init__(self) -> None:
        self.stopped = False


class _FakeAgent:
    """Minimal agent object compatible with ``listen_for_cancel``."""

    def __init__(self) -> None:
        self.state = _FakeAgentState()


class _FakeBrowser:
    """Minimal browser object compatible with ``wait_for_review_command``.

    Tracks whether ``.close()`` was called.
    """

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Test: read_stdin_line
# ---------------------------------------------------------------------------


class TestReadStdinLineReal:
    """Test ``read_stdin_line`` with real OS pipes and asyncio."""

    async def test_read_single_line_from_pipe(self):
        """Read one JSON line from a real OS pipe."""
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            os.write(w, b'{"type":"test"}\n')
            os.close(w)

            from ghosthands.bridge.protocol import read_stdin_line

            line = await read_stdin_line(timeout=5.0)
            parsed = json.loads(line)
            assert parsed["type"] == "test"
        finally:
            sys.stdin = old_stdin

    async def test_read_empty_line(self):
        """When the pipe is closed (EOF), readline returns empty string."""
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            os.close(w)  # Close immediately -> EOF

            from ghosthands.bridge.protocol import read_stdin_line

            line = await read_stdin_line(timeout=5.0)
            assert line == ""
        finally:
            sys.stdin = old_stdin

    async def test_timeout_on_empty_pipe(self):
        """Verify asyncio.TimeoutError when no data arrives within timeout."""
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import read_stdin_line

            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await read_stdin_line(timeout=0.3)
        finally:
            sys.stdin = old_stdin
            os.close(w)

    async def test_multiple_lines_sequential(self):
        """Read multiple lines sequentially from a real pipe."""
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            os.write(w, b'{"n":1}\n{"n":2}\n{"n":3}\n')
            os.close(w)

            from ghosthands.bridge.protocol import read_stdin_line

            line1 = await read_stdin_line(timeout=5.0)
            line2 = await read_stdin_line(timeout=5.0)
            line3 = await read_stdin_line(timeout=5.0)

            assert json.loads(line1)["n"] == 1
            assert json.loads(line2)["n"] == 2
            assert json.loads(line3)["n"] == 3
        finally:
            sys.stdin = old_stdin

    async def test_read_plain_text_line(self):
        """read_stdin_line works for non-JSON text too."""
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            os.write(w, b"hello world\n")
            os.close(w)

            from ghosthands.bridge.protocol import read_stdin_line

            line = await read_stdin_line(timeout=5.0)
            assert line.strip() == "hello world"
        finally:
            sys.stdin = old_stdin

    async def test_read_with_no_timeout(self):
        """When timeout is None and data is available, read completes."""
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            os.write(w, b"no-timeout-test\n")
            os.close(w)

            from ghosthands.bridge.protocol import read_stdin_line

            # Wrap in wait_for to prevent test hang if read_stdin_line blocks
            line = await asyncio.wait_for(read_stdin_line(timeout=None), timeout=5.0)
            assert line.strip() == "no-timeout-test"
        finally:
            sys.stdin = old_stdin


# ---------------------------------------------------------------------------
# Test: listen_for_cancel
# ---------------------------------------------------------------------------


class TestListenForCancelReal:
    """Test ``listen_for_cancel`` with real asyncio event loops and OS pipes."""

    async def test_cancel_sets_agent_stopped(self):
        """Send ``{"type":"cancel"}`` via pipe, verify agent.state.stopped is set."""
        agent = _FakeAgent()
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import listen_for_cancel

            async def send_cancel_after_delay():
                await asyncio.sleep(0.2)
                os.write(w, b'{"type":"cancel"}\n')
                os.close(w)

            await asyncio.wait_for(
                asyncio.gather(
                    listen_for_cancel(agent),
                    send_cancel_after_delay(),
                ),
                timeout=10.0,
            )
            assert agent.state.stopped is True
        finally:
            sys.stdin = old_stdin

    async def test_cancel_job_also_stops_agent(self):
        """The ``cancel_job`` command type should also stop the agent."""
        agent = _FakeAgent()
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import listen_for_cancel

            async def send_cancel_job():
                await asyncio.sleep(0.2)
                os.write(w, b'{"type":"cancel_job"}\n')
                os.close(w)

            await asyncio.wait_for(
                asyncio.gather(
                    listen_for_cancel(agent),
                    send_cancel_job(),
                ),
                timeout=10.0,
            )
            assert agent.state.stopped is True
        finally:
            sys.stdin = old_stdin

    async def test_cancel_event_is_set(self):
        """When a cancel_requested Event is provided, it should be set."""
        agent = _FakeAgent()
        cancel_event = asyncio.Event()
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import listen_for_cancel

            async def send_cancel():
                await asyncio.sleep(0.2)
                os.write(w, b'{"type":"cancel"}\n')
                os.close(w)

            await asyncio.wait_for(
                asyncio.gather(
                    listen_for_cancel(agent, cancel_requested=cancel_event),
                    send_cancel(),
                ),
                timeout=10.0,
            )
            assert cancel_event.is_set()
            assert agent.state.stopped is True
        finally:
            sys.stdin = old_stdin

    async def test_invalid_json_is_skipped(self):
        """Invalid JSON on stdin should be ignored, not crash the listener."""
        agent = _FakeAgent()
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import listen_for_cancel

            async def send_garbage_then_cancel():
                await asyncio.sleep(0.2)
                os.write(w, b"not json\n")
                os.write(w, b"{malformed\n")
                os.write(w, b'{"type":"unknown_command"}\n')
                await asyncio.sleep(0.1)
                os.write(w, b'{"type":"cancel"}\n')
                os.close(w)

            await asyncio.wait_for(
                asyncio.gather(
                    listen_for_cancel(agent),
                    send_garbage_then_cancel(),
                ),
                timeout=10.0,
            )
            assert agent.state.stopped is True
        finally:
            sys.stdin = old_stdin

    async def test_stdin_eof_treated_as_cancel(self):
        """When stdin is closed (EOF), listen_for_cancel treats it as cancellation."""
        agent = _FakeAgent()
        cancel_event = asyncio.Event()
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import listen_for_cancel

            async def close_stdin():
                await asyncio.sleep(0.2)
                os.close(w)

            await asyncio.wait_for(
                asyncio.gather(
                    listen_for_cancel(agent, cancel_requested=cancel_event),
                    close_stdin(),
                ),
                timeout=10.0,
            )
            # EOF in bridge mode is treated as cancellation (Electron died)
            assert agent.state.stopped is True
            assert cancel_event.is_set()
        finally:
            sys.stdin = old_stdin


# ---------------------------------------------------------------------------
# Test: wait_for_review_command
# ---------------------------------------------------------------------------


class TestWaitForReviewCommandReal:
    """Test ``wait_for_review_command`` with real pipes."""

    async def test_complete_review_closes_browser(self):
        """Sending ``complete_review`` should close the browser and return ``"complete"``."""
        browser = _FakeBrowser()
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import wait_for_review_command

            async def send_complete_review():
                await asyncio.sleep(0.3)
                os.write(w, b'{"type":"complete_review"}\n')
                os.close(w)

            results = await asyncio.wait_for(
                asyncio.gather(
                    wait_for_review_command(browser, job_id="test-job-1", lease_id="test-lease-1"),
                    send_complete_review(),
                ),
                timeout=10.0,
            )
            assert browser.closed is True
            assert results[0] == "complete"
        finally:
            sys.stdin = old_stdin

    async def test_cancel_closes_browser(self):
        """Sending ``cancel`` during review should close the browser and return ``"cancel"``."""
        browser = _FakeBrowser()
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import wait_for_review_command

            async def send_cancel():
                await asyncio.sleep(0.3)
                os.write(w, b'{"type":"cancel"}\n')
                os.close(w)

            results = await asyncio.wait_for(
                asyncio.gather(
                    wait_for_review_command(browser, job_id="test-job-2", lease_id="test-lease-2"),
                    send_cancel(),
                ),
                timeout=10.0,
            )
            assert browser.closed is True
            assert results[0] == "cancel"
        finally:
            sys.stdin = old_stdin

    async def test_cancel_job_closes_browser(self):
        """Sending ``cancel_job`` during review should close the browser and return ``"cancel"``."""
        browser = _FakeBrowser()
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import wait_for_review_command

            async def send_cancel_job():
                await asyncio.sleep(0.3)
                os.write(w, b'{"type":"cancel_job"}\n')
                os.close(w)

            results = await asyncio.wait_for(
                asyncio.gather(
                    wait_for_review_command(browser, job_id="test-job-3", lease_id="test-lease-3"),
                    send_cancel_job(),
                ),
                timeout=10.0,
            )
            assert browser.closed is True
            assert results[0] == "cancel"
        finally:
            sys.stdin = old_stdin

    async def test_invalid_json_ignored_then_complete(self):
        """Invalid JSON should be skipped; a subsequent valid command should work and return ``"complete"``."""
        browser = _FakeBrowser()
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import wait_for_review_command

            async def send_garbage_then_complete():
                await asyncio.sleep(0.3)
                os.write(w, b"not json at all\n")
                os.write(w, b'{"type":"unknown"}\n')
                await asyncio.sleep(0.1)
                os.write(w, b'{"type":"complete_review"}\n')
                os.close(w)

            results = await asyncio.wait_for(
                asyncio.gather(
                    wait_for_review_command(browser, job_id="test-job-4", lease_id="test-lease-4"),
                    send_garbage_then_complete(),
                ),
                timeout=10.0,
            )
            assert browser.closed is True
            assert results[0] == "complete"
        finally:
            sys.stdin = old_stdin

    async def test_eof_closes_browser(self):
        """If stdin is closed (Electron died), the browser should be closed and return ``"eof"``."""
        browser = _FakeBrowser()
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import wait_for_review_command

            async def close_stdin():
                await asyncio.sleep(0.3)
                os.close(w)

            results = await asyncio.wait_for(
                asyncio.gather(
                    wait_for_review_command(browser, job_id="test-job-5", lease_id="test-lease-5"),
                    close_stdin(),
                ),
                timeout=10.0,
            )
            assert browser.closed is True
            assert results[0] == "eof"
        finally:
            sys.stdin = old_stdin


# ---------------------------------------------------------------------------
# Test: concurrent stdin access
# ---------------------------------------------------------------------------


class TestConcurrentStdinAccess:
    """Verify the stdin_lock serializes concurrent readers.

    The module-level ``stdin_lock`` is an ``asyncio.Lock`` that binds to the
    first event loop that acquires it.  Since pytest-asyncio creates a fresh
    loop per test, we must reset the lock before each test to avoid
    "bound to a different event loop" errors.
    """

    @staticmethod
    def _reset_stdin_lock() -> None:
        """Replace the module-level stdin_lock with a fresh instance."""
        import ghosthands.bridge.protocol as proto

        proto.stdin_lock = asyncio.Lock()

    async def test_concurrent_reads_do_not_interleave(self):
        """Two concurrent read_stdin_line calls should each get a complete line."""
        self._reset_stdin_lock()
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import read_stdin_line

            # Write two lines before starting readers
            os.write(w, b"line-alpha\nline-beta\n")
            os.close(w)

            # Launch two readers concurrently
            results = await asyncio.wait_for(
                asyncio.gather(
                    read_stdin_line(timeout=5.0),
                    read_stdin_line(timeout=5.0),
                ),
                timeout=10.0,
            )

            stripped = sorted(r.strip() for r in results)
            assert stripped == ["line-alpha", "line-beta"]
        finally:
            sys.stdin = old_stdin

    async def test_delayed_write_with_concurrent_readers(self):
        """Readers waiting on stdin should each get data once written."""
        self._reset_stdin_lock()
        read_file, w = _make_pipe_stdin()
        old_stdin = sys.stdin
        sys.stdin = read_file
        try:
            from ghosthands.bridge.protocol import read_stdin_line

            async def delayed_write():
                await asyncio.sleep(0.3)
                os.write(w, b"delayed-1\ndelayed-2\n")
                os.close(w)

            read1_task = asyncio.create_task(read_stdin_line(timeout=5.0))
            read2_task = asyncio.create_task(read_stdin_line(timeout=5.0))
            write_task = asyncio.create_task(delayed_write())

            results = await asyncio.wait_for(
                asyncio.gather(read1_task, read2_task, write_task),
                timeout=10.0,
            )

            # First two results are the read lines, third is None from write task
            read_results = sorted(r.strip() for r in results[:2])
            assert read_results == ["delayed-1", "delayed-2"]
        finally:
            sys.stdin = old_stdin


# ---------------------------------------------------------------------------
# Test: read_stdin_line executor fallback (Windows / no-fileno path)
# ---------------------------------------------------------------------------


class _NoFilenoStdin:
    """Fake stdin that raises on ``.fileno()`` to trigger the executor fallback.

    Simulates the Windows/no-fileno path where ``loop.add_reader()`` is not
    available and ``read_stdin_line`` must fall back to
    ``loop.run_in_executor()``.
    """

    def __init__(self, data: str = "") -> None:
        self._lines = data.splitlines(keepends=True)
        self._index = 0

    def fileno(self) -> int:
        raise NotImplementedError("fake stdin has no fileno")

    def readline(self) -> str:
        if self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            return line
        return ""  # EOF


class _BlockingNoFilenoStdin(_NoFilenoStdin):
    """Fake stdin that blocks on ``.readline()`` until released (for timeout tests).

    Uses a ``threading.Event`` so the blocking thread can be woken up during
    test cleanup, preventing it from holding the executor for 30 seconds.
    """

    def __init__(self) -> None:
        super().__init__("")
        import threading

        self._release = threading.Event()

    def readline(self) -> str:
        # Block until released (up to 10s safety cap)
        self._release.wait(timeout=10)
        return ""

    def release(self) -> None:
        """Unblock the readline thread."""
        self._release.set()


class TestReadStdinLineFallback:
    """Test ``read_stdin_line`` executor fallback (no-fileno path).

    On Windows (and in environments where stdin has no real fd), the
    ``loop.add_reader()`` call raises ``NotImplementedError``.
    ``read_stdin_line`` catches this and falls back to
    ``loop.run_in_executor()``.  These tests verify that fallback works
    correctly by providing a fake stdin that raises on ``.fileno()``.
    """

    @staticmethod
    def _reset_stdin_state() -> None:
        """Replace the module-level stdin_lock and executor with fresh instances.

        This prevents test contamination -- especially after the timeout test
        which leaves a blocked thread in the old executor.
        """
        import concurrent.futures

        import ghosthands.bridge.protocol as proto

        proto.stdin_lock = asyncio.Lock()
        proto.stdin_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="hand-x-stdin-test",
        )

    async def test_fallback_reads_single_line(self):
        """Executor fallback reads a single JSON line correctly."""
        self._reset_stdin_state()
        fake_stdin = _NoFilenoStdin('{"type":"fallback_test"}\n')
        old_stdin = sys.stdin
        sys.stdin = fake_stdin
        try:
            from ghosthands.bridge.protocol import read_stdin_line

            line = await asyncio.wait_for(
                read_stdin_line(timeout=5.0),
                timeout=10.0,
            )
            import json

            parsed = json.loads(line)
            assert parsed["type"] == "fallback_test"
        finally:
            sys.stdin = old_stdin

    async def test_fallback_reads_multiple_lines(self):
        """Executor fallback reads multiple sequential lines."""
        self._reset_stdin_state()
        fake_stdin = _NoFilenoStdin('{"n":1}\n{"n":2}\n{"n":3}\n')
        old_stdin = sys.stdin
        sys.stdin = fake_stdin
        try:
            import json

            from ghosthands.bridge.protocol import read_stdin_line

            line1 = await asyncio.wait_for(read_stdin_line(timeout=5.0), timeout=10.0)
            line2 = await asyncio.wait_for(read_stdin_line(timeout=5.0), timeout=10.0)
            line3 = await asyncio.wait_for(read_stdin_line(timeout=5.0), timeout=10.0)

            assert json.loads(line1)["n"] == 1
            assert json.loads(line2)["n"] == 2
            assert json.loads(line3)["n"] == 3
        finally:
            sys.stdin = old_stdin

    async def test_fallback_eof_returns_empty(self):
        """When fake stdin has no data, readline returns empty string (EOF)."""
        self._reset_stdin_state()
        fake_stdin = _NoFilenoStdin("")  # No data -> immediate EOF
        old_stdin = sys.stdin
        sys.stdin = fake_stdin
        try:
            from ghosthands.bridge.protocol import read_stdin_line

            line = await asyncio.wait_for(
                read_stdin_line(timeout=5.0),
                timeout=10.0,
            )
            assert line == ""
        finally:
            sys.stdin = old_stdin

    async def test_fallback_plain_text(self):
        """Executor fallback works for non-JSON text."""
        self._reset_stdin_state()
        fake_stdin = _NoFilenoStdin("hello from fallback\n")
        old_stdin = sys.stdin
        sys.stdin = fake_stdin
        try:
            from ghosthands.bridge.protocol import read_stdin_line

            line = await asyncio.wait_for(
                read_stdin_line(timeout=5.0),
                timeout=10.0,
            )
            assert line.strip() == "hello from fallback"
        finally:
            sys.stdin = old_stdin

    async def test_fallback_timeout(self):
        """Executor fallback respects timeout when stdin blocks.

        Note: The executor fallback uses ``loop.run_in_executor()`` which
        cannot be truly cancelled (the thread keeps blocking).  The
        ``asyncio.wait_for`` raises ``TimeoutError`` to the caller, which
        is the correct behavior -- the caller gets the timeout, even though
        the background thread lingers until the process exits.

        This test is intentionally last in the class because the blocking
        thread contaminates the shared executor.  We release the blocking
        stdin and replace the executor in cleanup.
        """
        self._reset_stdin_state()
        fake_stdin = _BlockingNoFilenoStdin()
        old_stdin = sys.stdin
        sys.stdin = fake_stdin
        try:
            from ghosthands.bridge.protocol import read_stdin_line

            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await read_stdin_line(timeout=0.5)
        finally:
            sys.stdin = old_stdin
            # Release the blocked thread so it doesn't linger for 10 seconds
            fake_stdin.release()
            # Replace the contaminated executor with a fresh one
            self._reset_stdin_state()
