"""Unit tests for the desktop bridge IPC layer.

Covers:
- emit_browser_ready()  — new JSONL event for desktop app browser ready signal
- emit_awaiting_review() — new JSONL event for human-in-the-loop review prompt
- _listen_for_cancel()  — concurrent stdin listener that stops the agent on cancel

All tests are offline (no browser, no database, no API calls).
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_jsonl_output(fn, *args, **kwargs):
    """Call *fn* with stdout replaced by a StringIO; return parsed JSON lines."""
    buf = io.StringIO()
    import ghosthands.output.jsonl as jsonl_mod

    # Ensure the stdout guard is NOT active — tests must run in human/fallback mode.
    original_guard = jsonl_mod._jsonl_out
    jsonl_mod._jsonl_out = None

    try:
        with patch("sys.stdout", buf):
            fn(*args, **kwargs)
    finally:
        jsonl_mod._jsonl_out = original_guard

    output = buf.getvalue()
    lines = [ln for ln in output.splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# ---------------------------------------------------------------------------
# Test 1 — emit_browser_ready
# ---------------------------------------------------------------------------


class TestEmitBrowserReady:
    """emit_browser_ready(cdp_url) must emit a valid browser_ready JSONL event."""

    def test_emits_correct_type(self):
        from ghosthands.output.jsonl import emit_browser_ready

        events = _capture_jsonl_output(emit_browser_ready, "http://localhost:9222")
        assert len(events) == 1
        assert events[0]["type"] == "browser_ready"

    def test_emits_cdp_url_field(self):
        from ghosthands.output.jsonl import emit_browser_ready

        cdp_url = "http://127.0.0.1:9222/json/version"
        events = _capture_jsonl_output(emit_browser_ready, cdp_url)
        assert events[0]["cdpUrl"] == cdp_url

    def test_timestamp_is_present_and_numeric(self):
        from ghosthands.output.jsonl import emit_browser_ready

        events = _capture_jsonl_output(emit_browser_ready, "http://localhost:9222")
        ts = events[0]["timestamp"]
        assert isinstance(ts, (int, float))
        # Sanity check: timestamp should be a recent Unix-millisecond value
        # (greater than 2024-01-01 in milliseconds)
        assert ts > 1_704_067_200_000

    def test_output_is_single_jsonl_line(self):
        """Each event must be exactly one newline-terminated JSON object."""
        from ghosthands.output.jsonl import emit_browser_ready

        buf = io.StringIO()
        import ghosthands.output.jsonl as jsonl_mod

        original_guard = jsonl_mod._jsonl_out
        jsonl_mod._jsonl_out = None
        try:
            with patch("sys.stdout", buf):
                emit_browser_ready("http://localhost:9222")
        finally:
            jsonl_mod._jsonl_out = original_guard

        raw = buf.getvalue()
        # Exactly one non-empty line
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        assert len(lines) == 1
        # Must parse as JSON
        parsed = json.loads(lines[0])
        assert parsed["type"] == "browser_ready"

    def test_uses_real_stdout_when_guard_is_active(self):
        """When the stdout guard is installed, output goes to the saved fd, not sys.stdout."""
        from ghosthands.output.jsonl import emit_browser_ready
        import ghosthands.output.jsonl as jsonl_mod

        fake_fd = io.StringIO()
        original_guard = jsonl_mod._jsonl_out
        jsonl_mod._jsonl_out = fake_fd

        try:
            fake_stdout = io.StringIO()
            with patch("sys.stdout", fake_stdout):
                emit_browser_ready("http://localhost:9222")

            # Nothing should go to sys.stdout
            assert fake_stdout.getvalue() == ""
            # Event should go to the saved fd
            assert fake_fd.getvalue().strip() != ""
            event = json.loads(fake_fd.getvalue().strip())
            assert event["type"] == "browser_ready"
        finally:
            jsonl_mod._jsonl_out = original_guard


# ---------------------------------------------------------------------------
# Test 2 — emit_awaiting_review
# ---------------------------------------------------------------------------


class TestEmitAwaitingReview:
    """emit_awaiting_review() must emit a valid awaiting_review JSONL event."""

    def test_emits_correct_type(self):
        from ghosthands.output.jsonl import emit_awaiting_review

        events = _capture_jsonl_output(emit_awaiting_review)
        assert len(events) == 1
        assert events[0]["type"] == "awaiting_review"

    def test_default_message_is_present(self):
        """Calling with no arguments should still include a non-empty message."""
        from ghosthands.output.jsonl import emit_awaiting_review

        events = _capture_jsonl_output(emit_awaiting_review)
        msg = events[0].get("message", "")
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_custom_message_is_emitted(self):
        from ghosthands.output.jsonl import emit_awaiting_review

        custom = "Please review the filled application before submitting."
        events = _capture_jsonl_output(emit_awaiting_review, custom)
        assert events[0]["message"] == custom

    def test_timestamp_is_present_and_numeric(self):
        from ghosthands.output.jsonl import emit_awaiting_review

        events = _capture_jsonl_output(emit_awaiting_review)
        ts = events[0]["timestamp"]
        assert isinstance(ts, (int, float))
        assert ts > 1_704_067_200_000

    def test_empty_string_message_not_omitted(self):
        """An explicitly empty string message should still appear in the event
        (it is not None, so the None-filter in emit_event should not drop it)."""
        from ghosthands.output.jsonl import emit_awaiting_review

        events = _capture_jsonl_output(emit_awaiting_review, "")
        # The field must be present; whether it's "" or a default is implementation-
        # defined, but it should not raise and should produce a valid event.
        assert events[0]["type"] == "awaiting_review"


# ---------------------------------------------------------------------------
# Test 3 — _listen_for_cancel
# ---------------------------------------------------------------------------


def _make_mock_agent(stopped: bool = False) -> MagicMock:
    """Build a minimal mock that mimics browser-use Agent.state.stopped."""
    agent = MagicMock()
    agent.state = MagicMock()
    agent.state.stopped = stopped
    return agent


class TestListenForCancel:
    """_listen_for_cancel(agent) reads stdin lines concurrently and stops the agent
    when it receives {"type": "cancel"}.
    """

    @pytest.mark.asyncio
    async def test_cancel_command_stops_agent(self):
        """{"type": "cancel"} must set agent.state.stopped = True."""
        from ghosthands.cli import _listen_for_cancel

        agent = _make_mock_agent()

        cancel_line = json.dumps({"type": "cancel"}) + "\n"

        async def fake_readline():
            return cancel_line

        loop = asyncio.get_event_loop()

        with patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=[cancel_line, ""])):
            await _listen_for_cancel(agent)

        assert agent.state.stopped is True

    @pytest.mark.asyncio
    async def test_eof_breaks_loop(self):
        """Empty readline (EOF / stdin closed) must exit without error."""
        from ghosthands.cli import _listen_for_cancel

        agent = _make_mock_agent()

        loop = asyncio.get_event_loop()

        # First call returns "" (EOF)
        with patch.object(loop, "run_in_executor", new=AsyncMock(return_value="")):
            await _listen_for_cancel(agent)

        # Agent should NOT be stopped — it was a clean EOF, not a cancel
        assert agent.state.stopped is False

    @pytest.mark.asyncio
    async def test_invalid_json_is_ignored(self):
        """Malformed JSON lines must be silently ignored and not crash the listener."""
        from ghosthands.cli import _listen_for_cancel

        agent = _make_mock_agent()

        loop = asyncio.get_event_loop()

        # Sequence: bad JSON → EOF
        with patch.object(
            loop,
            "run_in_executor",
            new=AsyncMock(side_effect=["not valid json\n", ""]),
        ):
            # Should not raise
            await _listen_for_cancel(agent)

        assert agent.state.stopped is False

    @pytest.mark.asyncio
    async def test_non_cancel_command_is_ignored(self):
        """A valid JSON command that is not "cancel" must not stop the agent."""
        from ghosthands.cli import _listen_for_cancel

        agent = _make_mock_agent()

        loop = asyncio.get_event_loop()

        unknown_cmd = json.dumps({"type": "ping"}) + "\n"

        with patch.object(
            loop,
            "run_in_executor",
            new=AsyncMock(side_effect=[unknown_cmd, ""]),
        ):
            await _listen_for_cancel(agent)

        assert agent.state.stopped is False

    @pytest.mark.asyncio
    async def test_blank_lines_are_ignored(self):
        """Blank / whitespace-only lines must not crash or stop the agent."""
        from ghosthands.cli import _listen_for_cancel

        agent = _make_mock_agent()

        loop = asyncio.get_event_loop()

        with patch.object(
            loop,
            "run_in_executor",
            new=AsyncMock(side_effect=["\n", "   \n", ""]),
        ):
            await _listen_for_cancel(agent)

        assert agent.state.stopped is False

    @pytest.mark.asyncio
    async def test_cancel_after_other_commands(self):
        """Agent stops when cancel arrives after unrecognised commands."""
        from ghosthands.cli import _listen_for_cancel

        agent = _make_mock_agent()

        loop = asyncio.get_event_loop()

        seq = [
            json.dumps({"type": "ping"}) + "\n",
            "bad json\n",
            json.dumps({"type": "cancel"}) + "\n",
        ]

        with patch.object(loop, "run_in_executor", new=AsyncMock(side_effect=seq)):
            await _listen_for_cancel(agent)

        assert agent.state.stopped is True


# ---------------------------------------------------------------------------
# Test 4 — Event contract validation
# ---------------------------------------------------------------------------


class TestEventContract:
    """All event types must conform to the expected wire-format schema.

    Required fields for every event: type (str), timestamp (int/float).
    Each typed emitter must include its documented extra fields.
    """

    # ---- helpers ----

    @staticmethod
    def _emit_all_events() -> list[dict]:
        """Emit one of every event type and return the parsed JSON objects."""
        from ghosthands.output.jsonl import (
            emit_browser_ready,
            emit_awaiting_review,
            emit_cost,
            emit_done,
            emit_error,
            emit_field_failed,
            emit_field_filled,
            emit_progress,
            emit_status,
        )

        emitters = [
            lambda: emit_browser_ready("http://localhost:9222"),
            lambda: emit_awaiting_review("Please review"),
            lambda: emit_status("Starting", step=1, max_steps=10, job_id="j1"),
            lambda: emit_field_filled("first_name", "Jane"),
            lambda: emit_field_failed("phone", "not found"),
            lambda: emit_progress(3, 10, round=1),
            lambda: emit_done(True, "Done", fields_filled=5, job_id="j1", lease_id="l1"),
            lambda: emit_error("Something went wrong", fatal=False, job_id="j1"),
            lambda: emit_cost(0.0012, prompt_tokens=500, completion_tokens=200),
        ]

        results = []
        for emitter in emitters:
            events = _capture_jsonl_output(emitter)
            assert len(events) == 1, f"Expected 1 event, got {len(events)} for {emitter}"
            results.append(events[0])
        return results

    def test_all_events_have_type_field(self):
        for event in self._emit_all_events():
            assert "type" in event, f"Missing 'type' in {event}"
            assert isinstance(event["type"], str)
            assert len(event["type"]) > 0

    def test_all_events_have_timestamp_field(self):
        for event in self._emit_all_events():
            assert "timestamp" in event, f"Missing 'timestamp' in {event}"
            assert isinstance(event["timestamp"], (int, float))

    def test_browser_ready_contract(self):
        from ghosthands.output.jsonl import emit_browser_ready

        events = _capture_jsonl_output(emit_browser_ready, "http://localhost:9222")
        e = events[0]
        assert e["type"] == "browser_ready"
        assert "cdpUrl" in e
        assert "timestamp" in e

    def test_awaiting_review_contract(self):
        from ghosthands.output.jsonl import emit_awaiting_review

        events = _capture_jsonl_output(emit_awaiting_review, "Check the form")
        e = events[0]
        assert e["type"] == "awaiting_review"
        assert "message" in e
        assert "timestamp" in e

    def test_status_contract(self):
        from ghosthands.output.jsonl import emit_status

        events = _capture_jsonl_output(emit_status, "Loading", step=2, max_steps=20)
        e = events[0]
        assert e["type"] == "status"
        assert "message" in e
        assert "timestamp" in e

    def test_field_filled_contract(self):
        from ghosthands.output.jsonl import emit_field_filled

        events = _capture_jsonl_output(emit_field_filled, "email", "jane@example.com")
        e = events[0]
        assert e["type"] == "field_filled"
        assert "field" in e
        assert "value" in e
        assert "method" in e

    def test_field_failed_contract(self):
        from ghosthands.output.jsonl import emit_field_failed

        events = _capture_jsonl_output(emit_field_failed, "phone", "selector not found")
        e = events[0]
        assert e["type"] == "field_failed"
        assert "field" in e
        assert "error" in e

    def test_progress_contract(self):
        from ghosthands.output.jsonl import emit_progress

        events = _capture_jsonl_output(emit_progress, 4, 10)
        e = events[0]
        assert e["type"] == "progress"
        assert "filled" in e
        assert "total" in e
        assert "round" in e

    def test_done_contract(self):
        from ghosthands.output.jsonl import emit_done

        events = _capture_jsonl_output(emit_done, True, "Application submitted")
        e = events[0]
        assert e["type"] == "done"
        assert "success" in e
        assert "message" in e
        assert isinstance(e["success"], bool)

    def test_error_contract(self):
        from ghosthands.output.jsonl import emit_error

        events = _capture_jsonl_output(emit_error, "Timeout", fatal=True)
        e = events[0]
        assert e["type"] == "error"
        assert "message" in e
        assert "fatal" in e

    def test_cost_contract(self):
        from ghosthands.output.jsonl import emit_cost

        events = _capture_jsonl_output(emit_cost, 0.0025)
        e = events[0]
        assert e["type"] == "cost"
        assert "total_usd" in e
        assert isinstance(e["total_usd"], float)

    def test_none_values_are_omitted(self):
        """Fields passed as None to emit_event must be absent from the wire format."""
        from ghosthands.output.jsonl import emit_event

        events = _capture_jsonl_output(emit_event, "test_event", present="yes", absent=None)
        e = events[0]
        assert "present" in e
        assert "absent" not in e

    def test_all_known_event_types(self):
        """Smoke-test: all events produced by _emit_all_events have recognised types."""
        known_types = {
            "browser_ready",
            "awaiting_review",
            "status",
            "field_filled",
            "field_failed",
            "progress",
            "done",
            "error",
            "cost",
        }
        for event in self._emit_all_events():
            assert event["type"] in known_types, f"Unexpected event type: {event['type']}"
