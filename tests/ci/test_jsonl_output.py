"""Baseline regression tests for ghosthands.output.jsonl.

These tests capture the CURRENT behavior of the JSONL event emitter so that
future changes (especially Stream S2 renaming "type" -> "event") can be
validated against a known-good baseline.

Every emit_*() function writes a single JSON line to the JSONL output stream.
When the stdout guard is NOT installed (_jsonl_out is None), the fallback
target is sys.stdout.  These tests patch sys.stdout to capture output.
"""

from __future__ import annotations

import io
import json
import time
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_emit(fn, *args, **kwargs) -> dict:
    """Call an emit function, capture its JSONL line, return parsed dict.

    Because _get_output() falls back to sys.stdout when the stdout guard
    is not installed, patching sys.stdout with an io.StringIO buffer is
    sufficient for test isolation.
    """
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        fn(*args, **kwargs)
    line = buf.getvalue().strip()
    assert line, "emit function produced no output"
    return json.loads(line)


# ---------------------------------------------------------------------------
# emit_event — core emitter
# ---------------------------------------------------------------------------


class TestEmitEvent:
    """Tests for the core emit_event() function."""

    def test_output_uses_event_key(self):
        """emit_event uses 'event' key for the event type.

        # CHANGED in S2: Key renamed from "type" to "event" to match
        # the Desktop app's HandXEvent interface.
        """
        from ghosthands.output.jsonl import emit_event

        obj = _capture_emit(emit_event, "status", message="hello")
        # CHANGED in S2: "event" key (was "type")
        assert "event" in obj
        assert obj["event"] == "status"

    def test_includes_timestamp(self):
        """Every event includes an integer millisecond timestamp."""
        from ghosthands.output.jsonl import emit_event

        before_ms = int(time.time() * 1000)
        obj = _capture_emit(emit_event, "test_event")
        after_ms = int(time.time() * 1000)

        assert "timestamp" in obj
        assert isinstance(obj["timestamp"], int)
        # Timestamp should be within a reasonable window
        assert before_ms <= obj["timestamp"] <= after_ms + 1

    def test_kwargs_passed_through(self):
        """Extra keyword arguments appear as top-level keys in the event."""
        from ghosthands.output.jsonl import emit_event

        obj = _capture_emit(emit_event, "custom", foo="bar", count=42)
        assert obj["foo"] == "bar"
        assert obj["count"] == 42

    def test_none_values_omitted(self):
        """Keyword arguments with None values are omitted from the output."""
        from ghosthands.output.jsonl import emit_event

        obj = _capture_emit(emit_event, "sparse", present="yes", absent=None)
        assert "present" in obj
        assert "absent" not in obj

    def test_compact_json_separators(self):
        """Output uses compact JSON separators (no spaces after : and ,)."""
        from ghosthands.output.jsonl import emit_event

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            emit_event("compact_test", key="value")
        line = buf.getvalue().strip()
        # Compact separators: no space after colon or comma
        assert ": " not in line
        assert ", " not in line

    def test_output_ends_with_newline(self):
        """Each emitted event is terminated by a newline character."""
        from ghosthands.output.jsonl import emit_event

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            emit_event("newline_test")
        raw = buf.getvalue()
        assert raw.endswith("\n")

    def test_output_is_single_line(self):
        """Each event is exactly one line of JSON (JSONL format)."""
        from ghosthands.output.jsonl import emit_event

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            emit_event("single_line", data="test")
        lines = buf.getvalue().strip().split("\n")
        assert len(lines) == 1

    def test_output_goes_to_stdout_not_stderr(self):
        """When the stdout guard is NOT installed, output goes to sys.stdout."""
        from ghosthands.output.jsonl import emit_event

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            emit_event("target_test")

        assert stdout_buf.getvalue().strip() != ""
        assert stderr_buf.getvalue() == ""


# ---------------------------------------------------------------------------
# emit_status
# ---------------------------------------------------------------------------


class TestEmitStatus:
    """Tests for the emit_status() convenience emitter."""

    def test_basic_status(self):
        """emit_status produces a status event with a message."""
        from ghosthands.output.jsonl import emit_status

        obj = _capture_emit(emit_status, "Processing step 1")
        # CHANGED in S2: uses "event" key (was "type")
        assert obj["event"] == "status"
        assert obj["message"] == "Processing step 1"
        assert "timestamp" in obj

    def test_status_with_step_info(self):
        """emit_status passes step and maxSteps through."""
        from ghosthands.output.jsonl import emit_status

        obj = _capture_emit(emit_status, "Working", step=3, max_steps=10)
        assert obj["step"] == 3
        assert obj["maxSteps"] == 10

    def test_status_omits_none_optional_fields(self):
        """Optional fields that are None are omitted (compact wire format)."""
        from ghosthands.output.jsonl import emit_status

        obj = _capture_emit(emit_status, "Minimal")
        assert "step" not in obj
        assert "maxSteps" not in obj
        # job_id defaults to "" which becomes None via `or None`, so omitted
        assert "jobId" not in obj

    def test_status_with_job_id(self):
        """emit_status includes jobId when provided."""
        from ghosthands.output.jsonl import emit_status

        obj = _capture_emit(emit_status, "Active", job_id="job-123")
        assert obj["jobId"] == "job-123"

    def test_status_empty_job_id_omitted(self):
        """Empty string job_id is converted to None and omitted."""
        from ghosthands.output.jsonl import emit_status

        obj = _capture_emit(emit_status, "Active", job_id="")
        assert "jobId" not in obj


# ---------------------------------------------------------------------------
# emit_done
# ---------------------------------------------------------------------------


class TestEmitDone:
    """Tests for the emit_done() convenience emitter."""

    def test_done_success(self):
        """emit_done with success=True includes expected fields."""
        from ghosthands.output.jsonl import emit_done

        obj = _capture_emit(
            emit_done,
            success=True,
            message="Completed",
            fields_filled=5,
            job_id="job-1",
            lease_id="lease-1",
        )
        assert obj["event"] == "done"
        assert obj["success"] is True
        assert obj["message"] == "Completed"
        assert obj["fields_filled"] == 5
        assert obj["jobId"] == "job-1"
        assert obj["leaseId"] == "lease-1"
        assert "timestamp" in obj

    def test_done_failure(self):
        """emit_done with success=False."""
        from ghosthands.output.jsonl import emit_done

        obj = _capture_emit(emit_done, success=False, message="Failed to fill")
        assert obj["event"] == "done"
        assert obj["success"] is False
        assert obj["message"] == "Failed to fill"

    def test_done_with_result_data(self):
        """emit_done passes result_data dict through."""
        from ghosthands.output.jsonl import emit_done

        result = {"steps": 10, "costUsd": 0.05}
        obj = _capture_emit(
            emit_done, success=True, message="OK", result_data=result
        )
        assert obj["resultData"] == result

    def test_done_omits_empty_optional_strings(self):
        """Empty string job_id and lease_id are omitted (converted to None)."""
        from ghosthands.output.jsonl import emit_done

        obj = _capture_emit(
            emit_done, success=True, message="OK", job_id="", lease_id=""
        )
        assert "jobId" not in obj
        assert "leaseId" not in obj

    def test_done_fields_filled_default_zero(self):
        """fields_filled defaults to 0 and is included (not omitted — 0 is not None)."""
        from ghosthands.output.jsonl import emit_done

        obj = _capture_emit(emit_done, success=True, message="OK")
        assert obj["fields_filled"] == 0


# ---------------------------------------------------------------------------
# emit_field_filled
# ---------------------------------------------------------------------------


class TestEmitFieldFilled:
    """Tests for the emit_field_filled() convenience emitter."""

    def test_basic_field_filled(self):
        """emit_field_filled produces a field_filled event."""
        from ghosthands.output.jsonl import emit_field_filled

        obj = _capture_emit(emit_field_filled, "first_name", "Jane")
        assert obj["event"] == "field_filled"
        assert obj["field"] == "first_name"
        assert obj["value"] == "Jane"
        assert "timestamp" in obj

    def test_default_method_is_domhand(self):
        """The default method is 'domhand'."""
        from ghosthands.output.jsonl import emit_field_filled

        obj = _capture_emit(emit_field_filled, "email", "a@b.com")
        assert obj["method"] == "domhand"

    def test_custom_method(self):
        """A custom method can be specified."""
        from ghosthands.output.jsonl import emit_field_filled

        obj = _capture_emit(
            emit_field_filled, "email", "a@b.com", method="browser-use"
        )
        assert obj["method"] == "browser-use"


# ---------------------------------------------------------------------------
# emit_field_failed
# ---------------------------------------------------------------------------


class TestEmitFieldFailed:
    """Tests for the emit_field_failed() convenience emitter."""

    def test_basic_field_failed(self):
        """emit_field_failed produces a field_failed event."""
        from ghosthands.output.jsonl import emit_field_failed

        obj = _capture_emit(emit_field_failed, "phone", "Element not found")
        assert obj["event"] == "field_failed"
        assert obj["field"] == "phone"
        assert obj["reason"] == "Element not found"
        assert "error" not in obj
        assert "timestamp" in obj


# ---------------------------------------------------------------------------
# emit_progress
# ---------------------------------------------------------------------------


class TestEmitProgress:
    """Tests for the emit_progress() convenience emitter."""

    def test_basic_progress(self):
        """emit_progress produces a progress event with step/maxSteps."""
        from ghosthands.output.jsonl import emit_progress

        obj = _capture_emit(emit_progress, 5, 10)
        assert obj["event"] == "progress"
        assert obj["step"] == 5
        assert obj["maxSteps"] == 10
        assert "timestamp" in obj

    def test_progress_description_default(self):
        """The default description is an empty string."""
        from ghosthands.output.jsonl import emit_progress

        obj = _capture_emit(emit_progress, 0, 8)
        assert obj["description"] == ""

    def test_progress_custom_description(self):
        """A custom description can be specified."""
        from ghosthands.output.jsonl import emit_progress

        obj = _capture_emit(emit_progress, 3, 8, description="Filling page 2")
        assert obj["description"] == "Filling page 2"


# ---------------------------------------------------------------------------
# emit_error
# ---------------------------------------------------------------------------


class TestEmitError:
    """Tests for the emit_error() convenience emitter."""

    def test_basic_error(self):
        """emit_error produces an error event with a message."""
        from ghosthands.output.jsonl import emit_error

        obj = _capture_emit(emit_error, "Something went wrong")
        assert obj["event"] == "error"
        assert obj["message"] == "Something went wrong"
        assert "timestamp" in obj

    def test_error_fatal_default_false(self):
        """Fatal defaults to False and is included (not None)."""
        from ghosthands.output.jsonl import emit_error

        obj = _capture_emit(emit_error, "Oops")
        assert obj["fatal"] is False

    def test_error_fatal_true(self):
        """Fatal can be set to True."""
        from ghosthands.output.jsonl import emit_error

        obj = _capture_emit(emit_error, "Crash", fatal=True)
        assert obj["fatal"] is True

    def test_error_with_job_id(self):
        """emit_error includes jobId when provided."""
        from ghosthands.output.jsonl import emit_error

        obj = _capture_emit(emit_error, "Fail", job_id="job-99")
        assert obj["jobId"] == "job-99"

    def test_error_empty_job_id_omitted(self):
        """Empty string job_id is converted to None and omitted."""
        from ghosthands.output.jsonl import emit_error

        obj = _capture_emit(emit_error, "Fail", job_id="")
        assert "jobId" not in obj


# ---------------------------------------------------------------------------
# emit_cost
# ---------------------------------------------------------------------------


class TestEmitCost:
    """Tests for the emit_cost() convenience emitter."""

    def test_basic_cost(self):
        """emit_cost produces a cost event with total_usd."""
        from ghosthands.output.jsonl import emit_cost

        obj = _capture_emit(emit_cost, 0.123456)
        assert obj["event"] == "cost"
        assert obj["total_usd"] == 0.123456
        assert "timestamp" in obj

    def test_cost_rounds_to_six_decimals(self):
        """total_usd is rounded to 6 decimal places."""
        from ghosthands.output.jsonl import emit_cost

        obj = _capture_emit(emit_cost, 0.12345678901)
        assert obj["total_usd"] == round(0.12345678901, 6)

    def test_cost_token_defaults(self):
        """Token counts default to 0."""
        from ghosthands.output.jsonl import emit_cost

        obj = _capture_emit(emit_cost, 0.01)
        assert obj["prompt_tokens"] == 0
        assert obj["completion_tokens"] == 0

    def test_cost_with_tokens(self):
        """Token counts are passed through when provided."""
        from ghosthands.output.jsonl import emit_cost

        obj = _capture_emit(
            emit_cost, 0.05, prompt_tokens=1000, completion_tokens=500
        )
        assert obj["prompt_tokens"] == 1000
        assert obj["completion_tokens"] == 500


# ---------------------------------------------------------------------------
# _get_output fallback behavior
# ---------------------------------------------------------------------------


class TestGetOutput:
    """Tests for the _get_output() fallback logic."""

    def test_fallback_is_stdout_when_guard_not_installed(self):
        """Without install_stdout_guard(), _get_output() returns sys.stdout."""
        from ghosthands.output.jsonl import _get_output, _jsonl_out

        # In test context, the guard should not be installed
        assert _jsonl_out is None
        out = _get_output()
        import sys
        assert out is sys.stdout


# ---------------------------------------------------------------------------
# Progress field additions (ADDED in S2)
# ---------------------------------------------------------------------------


class TestEmitProgressS2:
    """Tests for the desktop-bridge emit_progress() signature (step, max_steps)."""

    def test_progress_includes_step_and_maxsteps(self):
        """emit_progress emits step and maxSteps."""
        from ghosthands.output.jsonl import emit_progress

        obj = _capture_emit(emit_progress, 5, 10)
        assert obj["step"] == 5
        assert obj["maxSteps"] == 10

    def test_progress_description_included_when_empty(self):
        """description is included even when empty (desktop-bridge format)."""
        from ghosthands.output.jsonl import emit_progress

        obj = _capture_emit(emit_progress, 3, 8)
        assert obj["description"] == ""

    def test_progress_description_included_when_provided(self):
        """description is included when a non-empty string is provided."""
        from ghosthands.output.jsonl import emit_progress

        obj = _capture_emit(emit_progress, 3, 8, description="Filling page 2")
        assert obj["description"] == "Filling page 2"


# ---------------------------------------------------------------------------
# Lease protocol events (ADDED in S2)
# ---------------------------------------------------------------------------


class TestEmitHandshake:
    """Tests for the emit_handshake() protocol event (desktop-bridge format)."""

    def test_handshake_default_version(self):
        """emit_handshake emits a handshake event with protocol_version=1."""
        from ghosthands.output.jsonl import emit_handshake

        obj = _capture_emit(emit_handshake)
        assert obj["event"] == "handshake"
        assert obj["protocol_version"] == 1
        assert obj["min_desktop_version"] == "0.1.0"
        assert "timestamp" in obj

    def test_handshake_has_min_desktop_version(self):
        """emit_handshake includes min_desktop_version field."""
        from ghosthands.output.jsonl import emit_handshake

        obj = _capture_emit(emit_handshake)
        assert "min_desktop_version" in obj


class TestEmitBrowserReady:
    """Tests for the emit_browser_ready() lease protocol event."""

    def test_browser_ready(self):
        """emit_browser_ready emits a browser_ready event with cdpUrl."""
        from ghosthands.output.jsonl import emit_browser_ready

        obj = _capture_emit(emit_browser_ready, "ws://127.0.0.1:9222/devtools/browser/abc")
        assert obj["event"] == "browser_ready"
        assert obj["cdpUrl"] == "ws://127.0.0.1:9222/devtools/browser/abc"
        assert "timestamp" in obj

    def test_browser_ready_empty_url(self):
        """emit_browser_ready with empty CDP URL still emits the event."""
        from ghosthands.output.jsonl import emit_browser_ready

        obj = _capture_emit(emit_browser_ready, "")
        assert obj["event"] == "browser_ready"
        assert obj["cdpUrl"] == ""


class TestEmitLeaseAcquired:
    """Tests for the emit_lease_acquired() lease protocol event."""

    def test_lease_acquired_basic(self):
        """emit_lease_acquired emits a lease_acquired event with leaseId."""
        from ghosthands.output.jsonl import emit_lease_acquired

        obj = _capture_emit(emit_lease_acquired, "lease-abc-123")
        assert obj["event"] == "lease_acquired"
        assert obj["leaseId"] == "lease-abc-123"
        assert "timestamp" in obj

    def test_lease_acquired_with_job_id(self):
        """emit_lease_acquired includes jobId when provided."""
        from ghosthands.output.jsonl import emit_lease_acquired

        obj = _capture_emit(emit_lease_acquired, "lease-1", job_id="job-42")
        assert obj["leaseId"] == "lease-1"
        assert obj["jobId"] == "job-42"

    def test_lease_acquired_empty_job_id_omitted(self):
        """Empty string job_id is converted to None and omitted."""
        from ghosthands.output.jsonl import emit_lease_acquired

        obj = _capture_emit(emit_lease_acquired, "lease-1", job_id="")
        assert "jobId" not in obj

    def test_lease_acquired_default_job_id_omitted(self):
        """Default job_id (empty string) is omitted."""
        from ghosthands.output.jsonl import emit_lease_acquired

        obj = _capture_emit(emit_lease_acquired, "lease-1")
        assert "jobId" not in obj


class TestEmitLeaseReleased:
    """Tests for the emit_lease_released() lease protocol event."""

    def test_lease_released_default_reason(self):
        """emit_lease_released emits a lease_released event with default reason."""
        from ghosthands.output.jsonl import emit_lease_released

        obj = _capture_emit(emit_lease_released, "lease-abc-123")
        assert obj["event"] == "lease_released"
        assert obj["leaseId"] == "lease-abc-123"
        assert obj["reason"] == "completed"
        assert "timestamp" in obj

    def test_lease_released_custom_reason(self):
        """emit_lease_released accepts a custom reason."""
        from ghosthands.output.jsonl import emit_lease_released

        obj = _capture_emit(emit_lease_released, "lease-1", reason="error")
        assert obj["reason"] == "error"

    def test_lease_released_failed_reason(self):
        """emit_lease_released with reason='failed'."""
        from ghosthands.output.jsonl import emit_lease_released

        obj = _capture_emit(emit_lease_released, "lease-1", reason="failed")
        assert obj["reason"] == "failed"


class TestEmitLeaseHeartbeat:
    """Tests for the emit_lease_heartbeat() lease protocol event."""

    def test_lease_heartbeat(self):
        """emit_lease_heartbeat emits a lease_heartbeat event with leaseId."""
        from ghosthands.output.jsonl import emit_lease_heartbeat

        obj = _capture_emit(emit_lease_heartbeat, "lease-abc-123")
        assert obj["event"] == "lease_heartbeat"
        assert obj["leaseId"] == "lease-abc-123"
        assert "timestamp" in obj

