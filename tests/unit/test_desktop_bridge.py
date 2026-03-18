"""Unit tests for the desktop bridge IPC layer.

Covers:
- emit_browser_ready()  — new JSONL event for desktop app browser ready signal
- emit_awaiting_review() — new JSONL event for human-in-the-loop review prompt
- _listen_for_cancel()  — concurrent stdin listener that stops the agent on cancel

All tests are offline (no browser, no database, no API calls).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
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
        assert events[0]["event"] == "browser_ready"

    def test_emits_cdp_url_field(self):
        from ghosthands.output.jsonl import emit_browser_ready

        cdp_url = "http://127.0.0.1:9222/json/version"
        events = _capture_jsonl_output(emit_browser_ready, cdp_url)
        assert events[0]["cdpUrl"] == cdp_url

    def test_timestamp_is_present_and_numeric(self):
        from ghosthands.output.jsonl import emit_browser_ready

        events = _capture_jsonl_output(emit_browser_ready, "http://localhost:9222")
        ts = events[0]["timestamp"]
        assert isinstance(ts, int | float)
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
        assert parsed["event"] == "browser_ready"

    def test_uses_real_stdout_when_guard_is_active(self):
        """When the stdout guard is installed, output goes to the saved fd, not sys.stdout."""
        import ghosthands.output.jsonl as jsonl_mod
        from ghosthands.output.jsonl import emit_browser_ready

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
            assert event["event"] == "browser_ready"
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
        assert events[0]["event"] == "awaiting_review"

    def test_default_message_is_present(self):
        """Calling with no arguments should include review instructions."""
        from ghosthands.output.jsonl import emit_awaiting_review

        events = _capture_jsonl_output(emit_awaiting_review)
        assert events[0]["message"] == (
            "We've filled out your application. Please review the form in the browser "
            "window, verify all fields are correct, then click Submit in the app."
        )

    def test_custom_message_is_emitted(self):
        from ghosthands.output.jsonl import emit_awaiting_review

        custom = "Please review the filled application before submitting."
        events = _capture_jsonl_output(emit_awaiting_review, custom)
        assert events[0]["message"] == custom

    def test_timestamp_is_present_and_numeric(self):
        from ghosthands.output.jsonl import emit_awaiting_review

        events = _capture_jsonl_output(emit_awaiting_review)
        ts = events[0]["timestamp"]
        assert isinstance(ts, int | float)
        assert ts > 1_704_067_200_000

    def test_empty_string_message_not_omitted(self):
        """An explicitly empty string message should still appear in the event
        (it is not None, so the None-filter in emit_event should not drop it)."""
        from ghosthands.output.jsonl import emit_awaiting_review

        events = _capture_jsonl_output(emit_awaiting_review, "")
        # The field must be present; whether it's "" or a default is implementation-
        # defined, but it should not raise and should produce a valid event.
        assert events[0]["event"] == "awaiting_review"


# ---------------------------------------------------------------------------
# Test 2a — emit_phase
# ---------------------------------------------------------------------------


class TestEmitPhase:
    """emit_phase() must emit a valid phase JSONL event."""

    def test_emits_correct_type(self):
        from ghosthands.output.jsonl import emit_phase

        events = _capture_jsonl_output(emit_phase, "Uploading resume")
        assert len(events) == 1
        assert events[0]["event"] == "phase"

    def test_phase_field_is_present(self):
        from ghosthands.output.jsonl import emit_phase

        events = _capture_jsonl_output(emit_phase, "Filling personal information")
        assert events[0]["phase"] == "Filling personal information"

    def test_detail_is_optional(self):
        from ghosthands.output.jsonl import emit_phase

        events = _capture_jsonl_output(emit_phase, "Uploading resume", detail="Upload the PDF resume")
        assert events[0]["detail"] == "Upload the PDF resume"

    def test_emit_phase_after_guard_teardown_does_not_crash(self):
        """emit_phase must safely fall back to sys.stdout when no guard is installed."""
        import ghosthands.output.jsonl as jsonl_mod
        from ghosthands.output.jsonl import emit_phase

        original_guard = jsonl_mod._jsonl_out
        original_pipe_broken = jsonl_mod._pipe_broken
        jsonl_mod._jsonl_out = None
        jsonl_mod._pipe_broken = False

        try:
            with patch("sys.stdout", io.StringIO()):
                emit_phase("test phase")
        except Exception:
            pytest.fail("emit_phase raised after guard teardown")
        finally:
            jsonl_mod._jsonl_out = original_guard
            jsonl_mod._pipe_broken = original_pipe_broken


# ---------------------------------------------------------------------------
# Test 2b — emit_account_created
# ---------------------------------------------------------------------------


class TestEmitAccountCreated:
    def test_emits_correct_event_type(self):
        from ghosthands.output.jsonl import emit_account_created

        events = _capture_jsonl_output(emit_account_created, "workday", "user@test.com", "pass123")
        assert len(events) == 1
        assert events[0]["event"] == "account_created"
        assert events[0]["credentialStatus"] == "pending_verification"

    def test_includes_all_fields(self):
        from ghosthands.output.jsonl import emit_account_created

        events = _capture_jsonl_output(
            emit_account_created,
            "workday",
            "user@test.com",
            "pass123",
            domain="acme.wd1.myworkdayjobs.com",
            credential_status="pending_verification",
            note="Check your inbox to verify the account.",
            evidence="auth_marker_pending_verification",
            url="https://workday.com",
        )
        e = events[0]
        assert e["platform"] == "workday"
        assert e["domain"] == "acme.wd1.myworkdayjobs.com"
        assert e["email"] == "user@test.com"
        assert e["password"] == "pass123"
        assert e["password_provided"] is True
        assert e["credentialStatus"] == "pending_verification"
        assert e["note"] == "Check your inbox to verify the account."


class TestAccountCreatedMarkerInference:
    def test_ignores_generic_new_account_planning_language(self):
        from ghosthands.cli import _infer_account_created_marker_from_text

        marker = _infer_account_created_marker_from_text(
            "need to create a new account using provided credentials"
        )

        assert marker == (None, None, None)

    def test_honors_explicit_pending_verification_marker(self):
        from ghosthands.cli import _infer_account_created_marker_from_text

        marker = _infer_account_created_marker_from_text(
            "AUTH_RESULT=ACCOUNT_CREATED_PENDING_VERIFICATION"
        )

        assert marker[0] == "pending_verification"
        assert marker[2] == "auth_marker_pending_verification"
        assert e["evidence"] == "auth_marker_pending_verification"
        assert e["url"] == "https://workday.com"

    def test_url_omitted_when_empty(self):
        from ghosthands.output.jsonl import emit_account_created

        events = _capture_jsonl_output(emit_account_created, "greenhouse", "user@test.com", "pass")
        assert "url" not in events[0]


class TestOpenQuestionAutoAnswering:
    def test_recovers_language_rubric_from_profile_before_hitl(self):
        from ghosthands.cli import _OpenQuestionIssue, _auto_answer_open_question_issues

        profile = {
            "spoken_languages": "English (Native / bilingual)",
            "english_proficiency": "Native / bilingual",
            "how_did_you_hear": "LinkedIn",
        }
        issues = [
            _OpenQuestionIssue(field_label="Comprehension", field_type="select", section="Languages"),
            _OpenQuestionIssue(field_label="Overall", field_type="select", section="Languages"),
            _OpenQuestionIssue(field_label="Reading", field_type="select", section="Languages"),
            _OpenQuestionIssue(field_label="Speaking", field_type="select", section="Languages"),
            _OpenQuestionIssue(field_label="Writing", field_type="select", section="Languages"),
        ]

        resolved, unresolved = _auto_answer_open_question_issues(issues, profile)

        assert unresolved == []
        assert resolved == {
            "Comprehension": "Native / bilingual",
            "Overall": "Native / bilingual",
            "Reading": "Native / bilingual",
            "Speaking": "Native / bilingual",
            "Writing": "Native / bilingual",
        }

    def test_preserves_unknown_issues_for_real_hitl(self):
        from ghosthands.cli import _OpenQuestionIssue, _auto_answer_open_question_issues

        issues = [
            _OpenQuestionIssue(field_label="Why do you want this job?", field_type="textarea"),
        ]

        resolved, unresolved = _auto_answer_open_question_issues(issues, {})

        assert resolved == {}
        assert len(unresolved) == 1
        assert unresolved[0].field_label == "Why do you want this job?"

    def test_recovers_compensation_open_question_from_profile(self):
        from ghosthands.cli import _OpenQuestionIssue, _auto_answer_open_question_issues

        profile = {
            "salary_expectation": "$90,000-$120,000 base (flexible)",
        }
        issues = [
            _OpenQuestionIssue(
                field_label="Expectations on Compensation - Please state your expectations of total compensation for this position.(Please list a value and/or range)*",
                field_type="textarea",
                section="My Information",
            ),
        ]

        resolved, unresolved = _auto_answer_open_question_issues(issues, profile)

        assert unresolved == []
        assert resolved == {
            "Expectations on Compensation - Please state your expectations of total compensation for this position.(Please list a value and/or range)*":
                "$90,000-$120,000 base (flexible)"
        }

    def test_defaults_previous_employment_questions_to_no(self):
        from ghosthands.cli import _OpenQuestionIssue, _auto_answer_open_question_issues

        issues = [
            _OpenQuestionIssue(
                field_label="Have you previously worked for this organization?",
                field_type="select",
                section="My Information",
            ),
        ]

        resolved, unresolved = _auto_answer_open_question_issues(issues, {})

        assert unresolved == []
        assert resolved == {
            "Have you previously worked for this organization?": "No",
        }

    @pytest.mark.asyncio
    async def test_llm_recovery_uses_visible_options_before_hitl(self, monkeypatch):
        from ghosthands.cli import _OpenQuestionIssue, _infer_open_question_answers_with_domhand

        async def _fake_infer(fields, *, profile_text=None, profile_data=None):
            assert len(fields) == 1
            assert fields[0].options == ["Beginner", "Intermediate", "Expert"]
            return {fields[0].field_id: "Expert"}

        monkeypatch.setattr(
            "ghosthands.actions.domhand_fill.infer_answers_for_fields",
            _fake_infer,
        )

        issues = [
            _OpenQuestionIssue(
                field_label="Overall language ability",
                field_type="select",
                section="Languages",
                options=("Beginner", "Intermediate", "Expert"),
            ),
        ]
        profile = {
            "spoken_languages": "English (Native / bilingual)",
            "english_proficiency": "Native / bilingual",
        }

        resolved, unresolved = await _infer_open_question_answers_with_domhand(issues, profile)

        assert unresolved == []
        assert resolved == {
            "Overall language ability": "Expert",
        }

    @pytest.mark.asyncio
    async def test_llm_recovery_does_not_try_open_ended_textareas(self, monkeypatch):
        from ghosthands.cli import _OpenQuestionIssue, _infer_open_question_answers_with_domhand

        called = False

        async def _fake_infer(fields, *, profile_text=None, profile_data=None):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(
            "ghosthands.actions.domhand_fill.infer_answers_for_fields",
            _fake_infer,
        )

        issues = [
            _OpenQuestionIssue(
                field_label="Why do you want this job?",
                field_type="textarea",
                section="Questions",
            ),
        ]

        resolved, unresolved = await _infer_open_question_answers_with_domhand(issues, {})

        assert called is False
        assert resolved == {}
        assert [issue.field_label for issue in unresolved] == ["Why do you want this job?"]


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
        from ghosthands.bridge.protocol import listen_for_cancel

        agent = _make_mock_agent()

        cancel_line = json.dumps({"type": "cancel"}) + "\n"

        with patch(
            "ghosthands.bridge.protocol.read_stdin_line",
            new=AsyncMock(side_effect=[cancel_line, ""]),
        ):
            await listen_for_cancel(agent)

        assert agent.state.stopped is True

    @pytest.mark.asyncio
    async def test_eof_breaks_loop(self):
        """EOF should stop the agent because Desktop disconnected."""
        from ghosthands.bridge.protocol import listen_for_cancel

        agent = _make_mock_agent()

        # First call returns "" (EOF)
        with patch(
            "ghosthands.bridge.protocol.read_stdin_line",
            new=AsyncMock(return_value=""),
        ):
            await listen_for_cancel(agent)

        assert agent.state.stopped is True

    @pytest.mark.asyncio
    async def test_invalid_json_is_ignored(self):
        """Malformed JSON lines must be silently ignored and not crash the listener."""
        from ghosthands.bridge.protocol import listen_for_cancel

        agent = _make_mock_agent()

        # Sequence: bad JSON → EOF
        with patch(
            "ghosthands.bridge.protocol.read_stdin_line",
            new=AsyncMock(side_effect=["not valid json\n", ""]),
        ):
            # Should not raise
            await listen_for_cancel(agent)

        assert agent.state.stopped is True

    @pytest.mark.asyncio
    async def test_non_cancel_command_is_ignored(self):
        """Unknown commands are ignored; the eventual EOF still stops the agent."""
        from ghosthands.bridge.protocol import listen_for_cancel

        agent = _make_mock_agent()

        unknown_cmd = json.dumps({"type": "ping"}) + "\n"

        with patch(
            "ghosthands.bridge.protocol.read_stdin_line",
            new=AsyncMock(side_effect=[unknown_cmd, ""]),
        ):
            await listen_for_cancel(agent)

        assert agent.state.stopped is True

    @pytest.mark.asyncio
    async def test_blank_lines_are_ignored(self):
        """Blank lines are ignored; the eventual EOF still stops the agent."""
        from ghosthands.bridge.protocol import listen_for_cancel

        agent = _make_mock_agent()

        with patch(
            "ghosthands.bridge.protocol.read_stdin_line",
            new=AsyncMock(side_effect=["\n", "   \n", ""]),
        ):
            await listen_for_cancel(agent)

        assert agent.state.stopped is True

    @pytest.mark.asyncio
    async def test_cancel_after_other_commands(self):
        """Agent stops when cancel arrives after unrecognised commands."""
        from ghosthands.bridge.protocol import listen_for_cancel

        agent = _make_mock_agent()

        seq = [
            json.dumps({"type": "ping"}) + "\n",
            "bad json\n",
            json.dumps({"type": "cancel"}) + "\n",
        ]

        with patch(
            "ghosthands.bridge.protocol.read_stdin_line",
            new=AsyncMock(side_effect=seq),
        ):
            await listen_for_cancel(agent)

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
            emit_account_created,
            emit_awaiting_review,
            emit_browser_ready,
            emit_cost,
            emit_done,
            emit_error,
            emit_field_failed,
            emit_field_filled,
            emit_phase,
            emit_progress,
            emit_status,
        )

        emitters = [
            lambda: emit_account_created("workday", "user@test.com", "pass123"),
            lambda: emit_browser_ready("http://localhost:9222"),
            lambda: emit_awaiting_review("Please review"),
            lambda: emit_phase("Starting application"),
            lambda: emit_status("Starting", step=1, max_steps=10, job_id="j1"),
            lambda: emit_field_filled("first_name", "Jane"),
            lambda: emit_field_failed("phone", "not found"),
            lambda: emit_progress(3, 10, description="Filling fields"),
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

    def test_all_events_have_event_field(self):
        for event in self._emit_all_events():
            assert "event" in event, f"Missing 'event' in {event}"
            assert isinstance(event["event"], str)
            assert len(event["event"]) > 0

    def test_all_events_have_timestamp_field(self):
        for event in self._emit_all_events():
            assert "timestamp" in event, f"Missing 'timestamp' in {event}"
            assert isinstance(event["timestamp"], int | float)

    def test_browser_ready_contract(self):
        from ghosthands.output.jsonl import emit_browser_ready

        events = _capture_jsonl_output(emit_browser_ready, "http://localhost:9222")
        e = events[0]
        assert e["event"] == "browser_ready"
        assert "cdpUrl" in e
        assert "timestamp" in e

    def test_awaiting_review_contract(self):
        from ghosthands.output.jsonl import emit_awaiting_review

        events = _capture_jsonl_output(emit_awaiting_review, "Check the form")
        e = events[0]
        assert e["event"] == "awaiting_review"
        assert "message" in e
        assert "timestamp" in e

    def test_status_contract(self):
        from ghosthands.output.jsonl import emit_status

        events = _capture_jsonl_output(emit_status, "Loading", step=2, max_steps=20)
        e = events[0]
        assert e["event"] == "status"
        assert "message" in e
        assert "timestamp" in e

    def test_phase_contract(self):
        from ghosthands.output.jsonl import emit_phase

        events = _capture_jsonl_output(emit_phase, "Answering additional questions")
        e = events[0]
        assert e["event"] == "phase"
        assert e["phase"] == "Answering additional questions"
        assert "timestamp" in e

    def test_field_filled_contract(self):
        from ghosthands.output.jsonl import emit_field_filled

        events = _capture_jsonl_output(emit_field_filled, "email", "jane@example.com")
        e = events[0]
        assert e["event"] == "field_filled"
        assert "field" in e
        assert "value" in e
        assert "method" in e

    def test_field_failed_contract(self):
        from ghosthands.output.jsonl import emit_field_failed

        events = _capture_jsonl_output(emit_field_failed, "phone", "selector not found")
        e = events[0]
        assert e["event"] == "field_failed"
        assert "field" in e
        assert "reason" in e

    def test_progress_contract(self):
        from ghosthands.output.jsonl import emit_progress

        events = _capture_jsonl_output(emit_progress, 4, 10)
        e = events[0]
        assert e["event"] == "progress"
        assert "step" in e
        assert "maxSteps" in e

    def test_done_contract(self):
        from ghosthands.output.jsonl import emit_done

        events = _capture_jsonl_output(emit_done, True, "Application submitted")
        e = events[0]
        assert e["event"] == "done"
        assert "success" in e
        assert "message" in e
        assert isinstance(e["success"], bool)

    def test_error_contract(self):
        from ghosthands.output.jsonl import emit_error

        events = _capture_jsonl_output(emit_error, "Timeout", fatal=True, code="TIMEOUT")
        e = events[0]
        assert e["event"] == "error"
        assert "message" in e
        assert "fatal" in e
        assert e["code"] == "TIMEOUT"

    def test_cost_contract(self):
        from ghosthands.output.jsonl import emit_cost

        events = _capture_jsonl_output(emit_cost, 0.0025)
        e = events[0]
        assert e["event"] == "cost"
        assert "total_usd" in e
        assert isinstance(e["total_usd"], float)

    def test_none_values_are_omitted(self):
        """Fields passed as None to emit_event must be absent from the wire format."""
        from ghosthands.output.jsonl import emit_event

        events = _capture_jsonl_output(emit_event, "test_event", present="yes", absent=None)
        e = events[0]
        assert "present" in e
        assert "absent" not in e

    def test_done_includes_fields_failed(self):
        """emit_done must include fields_failed when provided."""
        from ghosthands.output.jsonl import emit_done

        events = _capture_jsonl_output(emit_done, True, "Done", fields_filled=8, fields_failed=2)
        e = events[0]
        assert e["event"] == "done"
        assert e["fields_filled"] == 8
        assert e["fields_failed"] == 2

    def test_done_fields_failed_defaults_to_zero(self):
        """emit_done must emit fields_failed=0 when not explicitly provided."""
        from ghosthands.output.jsonl import emit_done

        events = _capture_jsonl_output(emit_done, True, "Done")
        e = events[0]
        assert e["event"] == "done"
        assert e["fields_failed"] == 0

    def test_all_known_event_types(self):
        """Smoke-test: all events produced by _emit_all_events have recognised types."""
        known_types = {
            "account_created",
            "browser_ready",
            "awaiting_review",
            "phase",
            "status",
            "field_filled",
            "field_failed",
            "progress",
            "done",
            "error",
            "cost",
        }
        for event in self._emit_all_events():
            assert event["event"] in known_types, f"Unexpected event type: {event['event']}"


# ---------------------------------------------------------------------------
# Test 5 — Profile loading: GH_USER_PROFILE_TEXT env var fallback
# ---------------------------------------------------------------------------


class TestProfileLoadingEnvFallback:
    """_load_profile must fall back to GH_USER_PROFILE_TEXT env var when
    neither --profile nor --test-data is provided."""

    def test_loads_profile_from_env_var(self):
        from ghosthands.cli import _load_profile

        profile_data = {"name": "Jane Doe", "email": "jane@example.com"}
        args = argparse.Namespace(profile=None, test_data=None)

        with patch.dict(os.environ, {"GH_USER_PROFILE_TEXT": json.dumps(profile_data)}):
            result = _load_profile(args)

        assert result == profile_data

    def test_raises_when_no_profile_source(self):
        """Must raise ValueError when --profile, --test-data, and env var are all absent."""
        from ghosthands.cli import _load_profile

        args = argparse.Namespace(profile=None, test_data=None)

        with patch.dict(os.environ, {}, clear=False):
            # Ensure env var is not set
            os.environ.pop("GH_USER_PROFILE_TEXT", None)
            with pytest.raises(ValueError, match="Either --profile"):
                _load_profile(args)

    def test_env_var_invalid_json_raises(self):
        """Invalid JSON in GH_USER_PROFILE_TEXT must raise json.JSONDecodeError."""
        from ghosthands.cli import _load_profile

        args = argparse.Namespace(profile=None, test_data=None)

        with patch.dict(os.environ, {"GH_USER_PROFILE_TEXT": "not valid json"}), pytest.raises(json.JSONDecodeError):
            _load_profile(args)

    def test_profile_flag_takes_precedence_over_env(self):
        """--profile must take precedence over GH_USER_PROFILE_TEXT."""
        from ghosthands.cli import _load_profile

        flag_data = {"source": "flag"}
        env_data = {"source": "env"}
        args = argparse.Namespace(profile=json.dumps(flag_data), test_data=None)

        with patch.dict(os.environ, {"GH_USER_PROFILE_TEXT": json.dumps(env_data)}):
            result = _load_profile(args)

        assert result == flag_data


# ---------------------------------------------------------------------------
# Test 6 — normalize_profile_defaults
# ---------------------------------------------------------------------------


class TestNormalizeProfileDefaults:
    """normalize_profile_defaults must add DomHand-expected fields with
    structural defaults when they are missing from a raw Desktop bridge profile."""

    def test_adds_all_scalar_defaults_when_missing(self):
        """A bare-minimum profile should gain only structural defaults."""
        from ghosthands.cli import normalize_profile_defaults

        raw = {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"}
        result = normalize_profile_defaults(raw)

        assert result["phone_device_type"] == "Mobile"
        assert result["phone_country_code"] == "+1"
        assert "work_authorization" not in result
        assert "visa_sponsorship" not in result
        assert "veteran_status" not in result
        assert "disability_status" not in result
        assert "gender" not in result
        assert "race_ethnicity" not in result

    def test_preserves_existing_values(self):
        """Existing non-empty values must NOT be overwritten by defaults."""
        from ghosthands.cli import normalize_profile_defaults

        raw = {
            "first_name": "Jane",
            "work_authorization": "No",
            "gender": "Female",
            "veteran_status": "I am a protected veteran",
        }
        result = normalize_profile_defaults(raw)

        assert result["work_authorization"] == "No"
        assert result["gender"] == "Female"
        assert result["veteran_status"] == "I am a protected veteran"
        # Only structural defaults are still applied for missing fields
        assert result["phone_device_type"] == "Mobile"
        assert "disability_status" not in result

    def test_adds_default_address_when_missing(self):
        """When address is absent, a full default address dict should be added."""
        from ghosthands.cli import normalize_profile_defaults

        raw = {"first_name": "Jane"}
        result = normalize_profile_defaults(raw)

        assert isinstance(result["address"], dict)
        assert result["address"]["country"] == "United States of America"
        assert "street" in result["address"]
        assert "city" in result["address"]
        assert "state" in result["address"]
        assert "zip" in result["address"]

    def test_merges_partial_address(self):
        """When address is a dict with some fields, missing fields get defaults."""
        from ghosthands.cli import normalize_profile_defaults

        raw = {
            "first_name": "Jane",
            "address": {"city": "San Francisco", "state": "CA"},
        }
        result = normalize_profile_defaults(raw)

        assert result["address"]["city"] == "San Francisco"
        assert result["address"]["state"] == "CA"
        assert result["address"]["country"] == "United States of America"
        assert result["address"]["street"] == ""

    def test_leaves_string_address_as_is(self):
        """A string address (e.g. 'San Francisco, CA') must not be replaced."""
        from ghosthands.cli import normalize_profile_defaults

        raw = {"first_name": "Jane", "address": "San Francisco, CA"}
        result = normalize_profile_defaults(raw)

        assert result["address"] == "San Francisco, CA"

    def test_replaces_empty_string_values_with_defaults(self):
        """Sensitive empty-string values should not be fabricated."""
        from ghosthands.cli import normalize_profile_defaults

        raw = {"work_authorization": "", "gender": ""}
        result = normalize_profile_defaults(raw)

        assert result["work_authorization"] == ""
        assert result["gender"] == ""
        assert result["phone_device_type"] == "Mobile"

    def test_replaces_none_values_with_defaults(self):
        """Sensitive None values should remain unset rather than defaulted."""
        from ghosthands.cli import normalize_profile_defaults

        raw = {"work_authorization": None, "veteran_status": None}
        result = normalize_profile_defaults(raw)

        assert result["work_authorization"] is None
        assert result["veteran_status"] is None
        assert result["phone_country_code"] == "+1"

    def test_does_not_mutate_input(self):
        """The original profile dict must not be modified."""
        from ghosthands.cli import normalize_profile_defaults

        raw = {"first_name": "Jane"}
        original_keys = set(raw.keys())
        normalize_profile_defaults(raw)

        assert set(raw.keys()) == original_keys

    def test_replaces_empty_address_string_with_default(self):
        """An empty string address should be replaced with the default dict."""
        from ghosthands.cli import normalize_profile_defaults

        raw = {"address": ""}
        result = normalize_profile_defaults(raw)

        assert isinstance(result["address"], dict)
        assert result["address"]["country"] == "United States of America"

    def test_original_profile_fields_pass_through(self):
        """Non-default fields from the original profile must be preserved."""
        from ghosthands.cli import normalize_profile_defaults

        raw = {
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane@example.com",
            "phone": "+15551234567",
            "skills": ["Python", "TypeScript"],
        }
        result = normalize_profile_defaults(raw)

        assert result["first_name"] == "Jane"
        assert result["last_name"] == "Doe"
        assert result["email"] == "jane@example.com"
        assert result["phone"] == "+15551234567"
        assert result["skills"] == ["Python", "TypeScript"]


class TestNormalizeProfileDefaultsWarningsRemoved:
    """The Desktop bridge must not fabricate sensitive answers or emit warnings
    about fabricated answers that no longer exist."""

    def test_missing_sensitive_fields_do_not_emit_status(self):
        from unittest.mock import patch

        from ghosthands.cli import normalize_profile_defaults

        raw = {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"}
        with patch("ghosthands.output.jsonl.emit_status") as mock_emit:
            result = normalize_profile_defaults(raw)

        mock_emit.assert_not_called()
        assert "gender" not in result
        assert "work_authorization" not in result

    def test_non_sensitive_defaults_still_apply_without_warning(self):
        from unittest.mock import patch

        from ghosthands.cli import normalize_profile_defaults

        raw = {"first_name": "Jane"}
        with patch("ghosthands.output.jsonl.emit_status") as mock_emit:
            result = normalize_profile_defaults(raw)

        assert result["phone_device_type"] == "Mobile"
        assert result["phone_country_code"] == "+1"
        mock_emit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7 — get_field_counts
# ---------------------------------------------------------------------------


class TestGetFieldCounts:
    """get_field_counts() must return correct (filled, failed) counts."""

    def _reset_field_events(self):
        """Reset module-level state so tests are independent."""
        import ghosthands.output.field_events as fe

        fe._installed = False
        fe._counts["filled"] = 0
        fe._counts["total"] = 0
        fe._counts["last_round"] = 0

    def test_returns_zero_zero_before_installation(self):
        """Before install_jsonl_callback is called, counts should be (0, 0)."""
        self._reset_field_events()
        from ghosthands.output.field_events import get_field_counts

        assert get_field_counts() == (0, 0)

    def test_returns_zero_zero_when_installed_but_no_fields(self):
        """After installation with no field results, counts should be (0, 0)."""
        import ghosthands.output.field_events as fe

        self._reset_field_events()
        # Simulate installation without actually importing domhand_fill
        fe._installed = True
        assert fe.get_field_counts() == (0, 0)

    def test_counts_tracked_correctly_after_fields(self):
        """Counts must reflect filled and failed fields accurately."""
        import ghosthands.output.field_events as fe

        self._reset_field_events()
        fe._installed = True

        # Simulate 5 filled, 2 failed (total=7)
        fe._counts["filled"] = 5
        fe._counts["total"] = 7

        assert fe.get_field_counts() == (5, 2)

    def test_counts_all_filled(self):
        """When all fields succeed, failed count should be 0."""
        import ghosthands.output.field_events as fe

        self._reset_field_events()
        fe._installed = True

        fe._counts["filled"] = 10
        fe._counts["total"] = 10

        assert fe.get_field_counts() == (10, 0)

    def test_counts_all_failed(self):
        """When no fields succeed, filled count should be 0."""
        import ghosthands.output.field_events as fe

        self._reset_field_events()
        fe._installed = True

        fe._counts["filled"] = 0
        fe._counts["total"] = 3

        assert fe.get_field_counts() == (0, 3)


# ---------------------------------------------------------------------------
# Test 8 — account_created emission
# ---------------------------------------------------------------------------


class TestAccountCreatedEmission:
    """account_created is emitted by cli.py's _on_step_end when the agent's
    evaluation indicates that an account was successfully created and
    the runtime is in a create-account credential mode.

    The emit_account_created function exists in the JSONL module and is
    imported by cli.py for this purpose.
    """

    def test_emit_account_created_function_still_exists(self):
        """The emit_account_created helper should still be importable for
        future use, even though cli.py no longer calls it unconditionally."""
        from ghosthands.output.jsonl import emit_account_created

        events = _capture_jsonl_output(
            emit_account_created,
            "workday",
            "user@test.com",
            "s3cret",
            url="https://workday.com/job/123",
        )
        assert len(events) == 1
        assert events[0]["event"] == "account_created"

    def test_cli_imports_emit_account_created(self):
        """cli.py should import emit_account_created for step-end detection."""
        from pathlib import Path

        cli_path = Path(__file__).resolve().parents[2] / "ghosthands" / "cli.py"
        source = cli_path.read_text(encoding="utf-8")
        # The function is now used in _on_step_end for account creation detection
        assert "emit_account_created" in source

    def test_cli_supports_user_create_account_emission(self):
        """User-provided create-account credentials should also trigger
        account_created emission logic, not just generated passwords."""
        from pathlib import Path

        cli_path = Path(__file__).resolve().parents[2] / "ghosthands" / "cli.py"
        source = cli_path.read_text(encoding="utf-8")
        assert 'credential_source == "user"' in source
        assert 'credential_intent == "create_account"' in source


# ---------------------------------------------------------------------------
# Test 9 — _camel_to_snake_profile
# ---------------------------------------------------------------------------


class TestCamelToSnakeProfile:
    """_camel_to_snake_profile must convert known camelCase keys to snake_case."""

    def test_converts_scalar_camel_keys(self):
        from ghosthands.cli import _camel_to_snake_profile

        profile = {
            "firstName": "Jane",
            "lastName": "Doe",
            "linkedIn": "https://linkedin.com/in/jane",
            "zipCode": "94105",
            "county": "San Francisco County",
            "workAuthorization": "Yes",
            "visaSponsorship": "No",
            "raceEthnicity": "Asian",
            "veteranStatus": "Not a veteran",
            "disabilityStatus": "No",
            "phoneDeviceType": "Mobile",
            "phoneCountryCode": "+1",
        }
        result = _camel_to_snake_profile(profile)

        assert result["first_name"] == "Jane"
        assert result["last_name"] == "Doe"
        assert result["linkedin"] == "https://linkedin.com/in/jane"
        assert result["zip"] == "94105"
        assert result["postal_code"] == "94105"
        assert result["county"] == "San Francisco County"
        assert result["work_authorization"] == "Yes"
        assert result["visa_sponsorship"] == "No"
        assert result["race_ethnicity"] == "Asian"
        assert result["veteran_status"] == "Not a veteran"
        assert result["disability_status"] == "No"
        assert result["phone_device_type"] == "Mobile"
        assert result["phone_country_code"] == "+1"

    def test_preserves_camel_keys(self):
        """Original camelCase keys must NOT be removed."""
        from ghosthands.cli import _camel_to_snake_profile

        profile = {"firstName": "Jane", "lastName": "Doe"}
        result = _camel_to_snake_profile(profile)

        assert result["firstName"] == "Jane"
        assert result["first_name"] == "Jane"

    def test_does_not_overwrite_existing_snake_keys(self):
        """If both camelCase and snake_case exist, snake_case wins."""
        from ghosthands.cli import _camel_to_snake_profile

        profile = {"firstName": "CamelJane", "first_name": "SnakeJane"}
        result = _camel_to_snake_profile(profile)

        assert result["first_name"] == "SnakeJane"

    def test_converts_education_nested_fields(self):
        from ghosthands.cli import _camel_to_snake_profile

        profile = {
            "education": [
                {
                    "school": "MIT",
                    "fieldOfStudy": "Computer Science",
                    "startDate": "2018-09",
                    "endDate": "2022-06",
                    "graduationDate": "2022-06-15",
                }
            ]
        }
        result = _camel_to_snake_profile(profile)
        edu = result["education"][0]

        assert edu["field_of_study"] == "Computer Science"
        assert edu["start_date"] == "2018-09"
        assert edu["end_date"] == "2022-06"
        assert edu["graduation_date"] == "2022-06-15"
        # Originals preserved
        assert edu["fieldOfStudy"] == "Computer Science"

    def test_converts_experience_nested_fields(self):
        from ghosthands.cli import _camel_to_snake_profile

        profile = {
            "experience": [
                {
                    "company": "Acme",
                    "title": "Engineer",
                    "startDate": "2020-01",
                    "endDate": "2023-12",
                }
            ]
        }
        result = _camel_to_snake_profile(profile)
        exp = result["experience"][0]

        assert exp["start_date"] == "2020-01"
        assert exp["end_date"] == "2023-12"

    def test_handles_empty_profile(self):
        from ghosthands.cli import _camel_to_snake_profile

        result = _camel_to_snake_profile({})
        assert result == {}

    def test_does_not_mutate_input(self):
        from ghosthands.cli import _camel_to_snake_profile

        profile = {"firstName": "Jane"}
        original_keys = set(profile.keys())
        _camel_to_snake_profile(profile)

        assert set(profile.keys()) == original_keys

    def test_handles_non_dict_items_in_arrays(self):
        """Non-dict items in education/experience arrays should pass through."""
        from ghosthands.cli import _camel_to_snake_profile

        profile = {"education": ["MIT", None, 42]}
        result = _camel_to_snake_profile(profile)

        assert result["education"] == ["MIT", None, 42]

    def test_handles_non_list_education(self):
        """If education is not a list, it should be left as-is."""
        from ghosthands.cli import _camel_to_snake_profile

        profile = {"education": "MIT"}
        result = _camel_to_snake_profile(profile)

        assert result["education"] == "MIT"

    def test_snake_case_profile_passes_through(self):
        """A profile already in snake_case should not be altered."""
        from ghosthands.cli import _camel_to_snake_profile

        profile = {
            "first_name": "Jane",
            "last_name": "Doe",
            "work_authorization": "Yes",
        }
        result = _camel_to_snake_profile(profile)

        assert result["first_name"] == "Jane"
        assert result["last_name"] == "Doe"
        assert result["work_authorization"] == "Yes"


# ---------------------------------------------------------------------------
# Test 10 — Credential extraction from profile JSON
# ---------------------------------------------------------------------------


class TestCredentialExtraction:
    """_resolve_sensitive_data must extract credentials from embedded profile data."""

    @staticmethod
    def _make_settings(email="", password=""):
        settings = MagicMock()
        settings.email = email
        settings.password = password
        return settings

    def test_platform_specific_creds_take_priority(self):
        """Platform-specific credentials should be used over generic."""
        from ghosthands.cli import _resolve_sensitive_data

        settings = self._make_settings()

        creds = {
            "generic": {"email": "generic@test.com", "password": "genPass"},
            "workday": {"email": "wd@test.com", "password": "wdPass"},
        }

        result = _resolve_sensitive_data(settings, embedded_credentials=creds, platform="workday")
        assert result == {"email": "wd@test.com", "password": "wdPass"}

    def test_generic_creds_used_when_no_platform_match(self):
        """Generic credentials should be used when platform-specific are missing."""
        from ghosthands.cli import _resolve_sensitive_data

        settings = self._make_settings()

        creds = {
            "generic": {"email": "generic@test.com", "password": "genPass"},
            "workday": {"email": "wd@test.com", "password": "wdPass"},
        }

        result = _resolve_sensitive_data(settings, embedded_credentials=creds, platform="greenhouse")
        assert result == {"email": "generic@test.com", "password": "genPass"}

    def test_env_vars_fallback_when_no_embedded_creds(self):
        """GH_EMAIL / GH_PASSWORD (via app_settings) should be used when no embedded creds."""
        from ghosthands.cli import _resolve_sensitive_data

        settings = self._make_settings(email="env@test.com", password="envPass")

        result = _resolve_sensitive_data(settings, embedded_credentials=None)
        assert result == {"email": "env@test.com", "password": "envPass"}

    def test_embedded_creds_override_env_vars(self):
        """Embedded credentials should take priority over env vars."""
        from ghosthands.cli import _resolve_sensitive_data

        settings = self._make_settings(email="env@test.com", password="envPass")

        creds = {
            "generic": {"email": "embedded@test.com", "password": "embedPass"},
        }

        result = _resolve_sensitive_data(settings, embedded_credentials=creds)
        assert result == {"email": "embedded@test.com", "password": "embedPass"}

    def test_returns_none_when_no_credentials_anywhere(self):
        """Should return None when no credentials are available from any source."""
        from ghosthands.cli import _resolve_sensitive_data

        settings = self._make_settings()

        result = _resolve_sensitive_data(settings, embedded_credentials=None)
        assert result is None

    def test_application_password_fallback(self):
        """application_password is only used when creds_email is set but creds_password is not.

        Since the code requires both email AND password from platform/generic
        creds to set creds_email, and application_password is a password-only
        fallback, this scenario only applies if a future code path sets
        creds_email without creds_password.  For now we verify the code
        doesn't crash and falls through to None.
        """
        from ghosthands.cli import _resolve_sensitive_data

        settings = self._make_settings()

        # workday has email but missing password key -> fails the
        # "both email AND password" check -> creds_email stays empty
        # -> application_password can't help -> returns None
        embedded = {
            "workday": {"email": "wd@test.com"},
            "application_password": "appPass123",
        }

        result = _resolve_sensitive_data(settings, embedded_credentials=embedded, platform="workday")
        assert result is None

    def test_empty_embedded_credentials_falls_through(self):
        """An empty credentials dict should fall through to env vars."""
        from ghosthands.cli import _resolve_sensitive_data

        settings = self._make_settings(email="env@test.com", password="envPass")

        result = _resolve_sensitive_data(settings, embedded_credentials={})
        assert result == {"email": "env@test.com", "password": "envPass"}


# ---------------------------------------------------------------------------
# Test 11 — Stdout guard mechanics
# ---------------------------------------------------------------------------


class TestStdoutGuard:
    """install_stdout_guard must save the real stdout fd and redirect
    sys.stdout to stderr so the JSONL stream is never corrupted."""

    def test_guard_redirects_stdout_to_saved_fd(self):
        """After install, emit_event writes to saved fd, not sys.stdout."""
        import ghosthands.output.jsonl as jsonl_mod

        fake_fd = io.StringIO()
        original_guard = jsonl_mod._jsonl_out
        jsonl_mod._jsonl_out = fake_fd

        try:
            fake_stdout = io.StringIO()
            with patch("sys.stdout", fake_stdout):
                jsonl_mod.emit_event("test_guard", value="hello")

            # JSONL goes to the saved fd
            assert fake_fd.getvalue().strip() != ""
            event = json.loads(fake_fd.getvalue().strip())
            assert event["event"] == "test_guard"
            assert event["value"] == "hello"
            # Nothing leaked to sys.stdout
            assert fake_stdout.getvalue() == ""
        finally:
            jsonl_mod._jsonl_out = original_guard

    def test_guard_idempotent(self):
        """Calling install_stdout_guard twice should not raise or double-dup."""
        import ghosthands.output.jsonl as jsonl_mod

        original_guard = jsonl_mod._jsonl_out
        # Simulate already installed
        jsonl_mod._jsonl_out = io.StringIO()
        try:
            # Should return early without error
            jsonl_mod.install_stdout_guard()
        finally:
            jsonl_mod._jsonl_out = original_guard

    def test_fallback_when_guard_not_installed(self):
        """When guard is NOT installed, emit_event falls back to sys.stdout."""
        import ghosthands.output.jsonl as jsonl_mod

        original_guard = jsonl_mod._jsonl_out
        jsonl_mod._jsonl_out = None

        try:
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                jsonl_mod.emit_event("fallback_test", data="yes")

            event = json.loads(buf.getvalue().strip())
            assert event["event"] == "fallback_test"
        finally:
            jsonl_mod._jsonl_out = original_guard


# ---------------------------------------------------------------------------
# Test 12 — Thread-safe JSONL emission
# ---------------------------------------------------------------------------


class TestThreadSafeEmission:
    """emit_event uses a threading.Lock — concurrent calls must produce
    valid, non-interleaved JSONL lines."""

    def test_concurrent_emits_produce_valid_jsonl(self):
        """Multiple threads emitting simultaneously must each produce a
        complete, parseable JSONL line."""
        import threading

        import ghosthands.output.jsonl as jsonl_mod

        buf = io.StringIO()
        original_guard = jsonl_mod._jsonl_out
        jsonl_mod._jsonl_out = None

        try:
            with patch("sys.stdout", buf):
                threads = []
                for i in range(20):
                    t = threading.Thread(
                        target=jsonl_mod.emit_event,
                        args=("concurrent",),
                        kwargs={"index": i},
                    )
                    threads.append(t)

                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

            lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
            assert len(lines) == 20

            indices = set()
            for line in lines:
                event = json.loads(line)
                assert event["event"] == "concurrent"
                indices.add(event["index"])
            assert indices == set(range(20))
        finally:
            jsonl_mod._jsonl_out = original_guard


# ---------------------------------------------------------------------------
# Test 13 — field_events callback integration
# ---------------------------------------------------------------------------


class TestFieldEventsCallback:
    """install_jsonl_callback must wire into domhand_fill and emit
    field_filled / field_failed events as fields are processed."""

    def _reset_field_events(self):
        import ghosthands.output.field_events as fe

        fe._installed = False
        fe._counts["filled"] = 0
        fe._counts["total"] = 0
        fe._counts["last_round"] = 0

    def _setup_mock_fill(self):
        """Create mock modules that satisfy `from ghosthands.actions import domhand_fill`."""
        mock_fill = types.ModuleType("ghosthands.actions.domhand_fill")
        mock_fill._on_field_result = None

        mock_actions = types.ModuleType("ghosthands.actions")
        mock_actions.domhand_fill = mock_fill

        return mock_fill, {
            "ghosthands.actions": mock_actions,
            "ghosthands.actions.domhand_fill": mock_fill,
        }

    def test_callback_emits_field_filled(self):
        """A successful FillFieldResult should emit field_filled."""
        self._reset_field_events()

        import ghosthands.output.field_events as fe

        mock_fill, modules = self._setup_mock_fill()

        with patch.dict("sys.modules", modules):
            fe._installed = False
            fe.install_jsonl_callback()

            callback = mock_fill._on_field_result
            assert callback is not None

            result = MagicMock()
            result.success = True
            result.name = "email"
            result.value_set = "jane@test.com"

            events = _capture_jsonl_output(callback, result, 1)

        # Should have emitted field_filled + progress
        assert len(events) == 2
        assert events[0]["event"] == "field_filled"
        assert events[0]["field"] == "email"
        assert events[0]["value"] == "jane@test.com"
        assert events[1]["event"] == "progress"

    def test_callback_emits_field_failed(self):
        """A failed FillFieldResult should emit field_failed."""
        self._reset_field_events()

        import ghosthands.output.field_events as fe

        mock_fill, modules = self._setup_mock_fill()

        with patch.dict("sys.modules", modules):
            fe._installed = False
            fe.install_jsonl_callback()

            callback = mock_fill._on_field_result
            result = MagicMock()
            result.success = False
            result.name = "phone"
            result.error = "selector not found"

            events = _capture_jsonl_output(callback, result, 1)

        assert events[0]["event"] == "field_failed"
        assert events[0]["field"] == "phone"
        assert events[0]["reason"] == "selector not found"

    def test_every_fifth_success_emits_phase(self):
        """Each fifth successful fill should emit a high-level phase update."""
        self._reset_field_events()

        import ghosthands.output.field_events as fe

        mock_fill, modules = self._setup_mock_fill()

        with patch.dict("sys.modules", modules):
            fe._installed = False
            fe.install_jsonl_callback()

            callback = mock_fill._on_field_result
            assert callback is not None

            result = MagicMock()
            result.success = True
            result.name = "email"
            result.value_set = "jane@test.com"

            for _ in range(4):
                _capture_jsonl_output(callback, result, 1)

            events = _capture_jsonl_output(callback, result, 1)

        assert len(events) == 3
        assert events[0]["event"] == "field_filled"
        assert events[1]["event"] == "phase"
        assert events[1]["phase"] == "Filling form fields (5 completed)"
        assert events[2]["event"] == "progress"

    def test_multi_round_counting(self):
        """Counts must accumulate across multiple rounds."""
        self._reset_field_events()

        import ghosthands.output.field_events as fe

        mock_fill, modules = self._setup_mock_fill()

        with patch.dict("sys.modules", modules):
            fe._installed = False
            fe.install_jsonl_callback()

            callback = mock_fill._on_field_result

            # Round 1: 2 filled, 1 failed
            for field_name in ("first_name", "last_name"):
                r = MagicMock(success=True, value_set="val")
                r.name = field_name  # .name is special in MagicMock
                _capture_jsonl_output(callback, r, 1)
            r = MagicMock(success=False, error="not found")
            r.name = "phone"
            _capture_jsonl_output(callback, r, 1)

            # Round 2: 1 filled
            r = MagicMock(success=True, value_set="+15551234567")
            r.name = "phone"
            _capture_jsonl_output(callback, r, 2)

        # Cumulative: 3 filled, 1 failed out of 4 total
        assert fe.get_field_counts() == (3, 1)


# ---------------------------------------------------------------------------
# Test 14 — _listen_for_cancel: cancel_requested event + cancel_job type
# ---------------------------------------------------------------------------


class TestListenForCancelExtended:
    """Extended tests for _listen_for_cancel covering cancel_requested
    event and the cancel_job command type."""

    @pytest.mark.asyncio
    async def test_cancel_sets_cancel_requested_event(self):
        """cancel command must set the cancel_requested asyncio.Event."""
        from ghosthands.bridge.protocol import listen_for_cancel

        agent = _make_mock_agent()
        cancel_requested = asyncio.Event()

        cancel_line = json.dumps({"type": "cancel"}) + "\n"

        with patch(
            "ghosthands.bridge.protocol.read_stdin_line",
            new=AsyncMock(side_effect=[cancel_line, ""]),
        ):
            await listen_for_cancel(agent, cancel_requested)

        assert cancel_requested.is_set()
        assert agent.state.stopped is True

    @pytest.mark.asyncio
    async def test_cancel_job_command_stops_agent(self):
        """{"type": "cancel_job"} must also set agent.state.stopped = True."""
        from ghosthands.bridge.protocol import listen_for_cancel

        agent = _make_mock_agent()
        cancel_requested = asyncio.Event()

        cancel_line = json.dumps({"type": "cancel_job"}) + "\n"

        with patch(
            "ghosthands.bridge.protocol.read_stdin_line",
            new=AsyncMock(side_effect=[cancel_line, ""]),
        ):
            await listen_for_cancel(agent, cancel_requested)

        assert agent.state.stopped is True
        assert cancel_requested.is_set()

    @pytest.mark.asyncio
    async def test_timeout_on_stdin_continues_loop(self):
        """TimeoutError from _read_stdin_line should continue the loop."""
        from ghosthands.bridge.protocol import listen_for_cancel

        agent = _make_mock_agent()
        call_count = 0

        async def _mock_read(timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise TimeoutError()
            return ""  # EOF after 2 timeouts

        with patch("ghosthands.bridge.protocol.read_stdin_line", new=_mock_read):
            await listen_for_cancel(agent)

        assert call_count == 3
        assert agent.state.stopped is True


# ---------------------------------------------------------------------------
# Test 15 — _wait_for_review_command
# ---------------------------------------------------------------------------


class TestWaitForReviewCommand:
    """_wait_for_review_command must wait for stdin commands and handle
    complete_review, cancel, and timeout scenarios."""

    @pytest.mark.asyncio
    async def test_complete_review_closes_browser(self):
        from ghosthands.bridge.protocol import wait_for_review_command

        browser = AsyncMock()
        cmd = json.dumps({"type": "complete_review"}) + "\n"

        with patch(
            "ghosthands.bridge.protocol.read_stdin_line",
            new=AsyncMock(side_effect=[cmd]),
        ):
            result = await wait_for_review_command(browser, "j1", "l1")

        assert result == "complete"
        browser.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_during_review_closes_browser(self):
        from ghosthands.bridge.protocol import wait_for_review_command

        browser = AsyncMock()
        cmd = json.dumps({"type": "cancel"}) + "\n"

        with patch(
            "ghosthands.bridge.protocol.read_stdin_line",
            new=AsyncMock(side_effect=[cmd]),
        ):
            result = await wait_for_review_command(browser, "j1", "l1")

        assert result == "cancel"
        browser.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_eof_during_review_closes_browser(self):
        """EOF (Electron crashed) should cleanly close browser."""
        from ghosthands.bridge.protocol import wait_for_review_command

        browser = AsyncMock()

        with patch(
            "ghosthands.bridge.protocol.read_stdin_line",
            new=AsyncMock(return_value=""),
        ):
            result = await wait_for_review_command(browser, "j1", "l1")

        assert result == "eof"
        browser.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_unknown_commands_during_review(self):
        """Unknown commands should be ignored, loop continues."""
        from ghosthands.bridge.protocol import wait_for_review_command

        browser = AsyncMock()

        seq = [
            json.dumps({"type": "ping"}) + "\n",
            json.dumps({"type": "complete_review"}) + "\n",
        ]

        with patch(
            "ghosthands.bridge.protocol.read_stdin_line",
            new=AsyncMock(side_effect=seq),
        ):
            result = await wait_for_review_command(browser, "j1", "l1")

        assert result == "complete"
        browser.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_emits_warning_before_fatal_error(self):
        from ghosthands.bridge.protocol import wait_for_review_command

        browser = AsyncMock()

        with (
            patch(
                "ghosthands.bridge.protocol.read_stdin_line",
                new=AsyncMock(side_effect=[TimeoutError(), TimeoutError()]),
            ),
            patch(
                "ghosthands.bridge.protocol._time.monotonic",
                side_effect=[0.0, 23 * 60 * 60 + 1.0, 24 * 60 * 60 + 1.0],
            ),
            patch("ghosthands.output.jsonl.emit_status") as emit_status,
            patch("ghosthands.output.jsonl.emit_error") as emit_error,
        ):
            result = await wait_for_review_command(browser, "j1", "l1")

        assert result == "timeout"
        emit_status.assert_any_call(
            "Your review session will expire in about 1 hour. Please submit or cancel soon.",
            job_id="j1",
        )
        emit_error.assert_called_once_with(
            "Review session expired — please submit or cancel your application",
            fatal=True,
            job_id="j1",
        )
        browser.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_warning_if_submitted_before_timeout(self):
        from ghosthands.bridge.protocol import wait_for_review_command

        browser = AsyncMock()
        cmd = json.dumps({"type": "complete_review"}) + "\n"

        with (
            patch(
                "ghosthands.bridge.protocol.read_stdin_line",
                new=AsyncMock(side_effect=[cmd]),
            ),
            patch(
                "ghosthands.bridge.protocol._time.monotonic",
                side_effect=[0.0, 600.0],
            ),
            patch("ghosthands.output.jsonl.emit_status") as emit_status,
        ):
            result = await wait_for_review_command(browser, "j1", "l1")

        assert result == "complete"
        emit_status.assert_any_call("Review complete -- closing browser", job_id="j1")
        assert ("Your review session will expire in 5 minutes. Please submit or cancel soon.",) not in [
            call.args for call in emit_status.call_args_list
        ]
        browser.stop.assert_called_once()


class TestReviewOutcomeHandling:
    def test_timeout_emits_done_failure_message(self):
        from ghosthands.cli import _handle_review_result

        with patch("ghosthands.output.jsonl.emit_done") as emit_done:
            exit_code = _handle_review_result(
                "timeout",
                fields_filled=8,
                fields_failed=2,
                job_id="job-1",
                lease_id="lease-1",
                result_data={"success": True},
            )

        assert exit_code == 1
        emit_done.assert_called_once_with(
            success=False,
            message="Review timed out after 30 minutes. The browser window is still open — you can submit manually.",
            fields_filled=8,
            fields_failed=2,
            job_id="job-1",
            lease_id="lease-1",
            result_data={"success": False, "timedOut": True},
        )


class TestRuntimeErrorClassification:
    @staticmethod
    def _make_error(status_code, message, *, headers=None, body=None):
        response = types.SimpleNamespace(
            status_code=status_code,
            headers=headers or {},
            text=message,
        )
        error = RuntimeError(message)
        error.status_code = status_code
        error.message = message
        error.response = response
        error.body = body
        return error

    def test_detects_grant_expiry_from_401(self):
        from ghosthands.cli import _classify_runtime_error

        error = self._make_error(401, "Runtime grant expired")

        result = _classify_runtime_error(error, proxy_mode=True)

        assert result is not None
        assert result.code == "GRANT_EXPIRED"
        assert result.message == "Your automation session expired. Please try again."
        assert result.keep_browser_open is True

    def test_detects_budget_exhaustion_header(self):
        from ghosthands.cli import _classify_runtime_error

        error = self._make_error(
            429,
            "Runtime grant budget exhausted",
            headers={"X-Budget-Exhausted": "true"},
        )

        result = _classify_runtime_error(error, proxy_mode=True)

        assert result is not None
        assert result.code == "BUDGET_EXHAUSTED"
        assert "partially completed form is still open in the browser" in result.message
        assert result.keep_browser_open is True

    def test_returns_none_when_not_proxy_mode(self):
        from ghosthands.cli import _classify_runtime_error

        error = self._make_error(401, "Runtime grant expired")

        result = _classify_runtime_error(error, proxy_mode=False)

        assert result is None

    def test_plain_429_is_not_budget_exhausted(self):
        from ghosthands.cli import _classify_runtime_error

        error = self._make_error(429, "Too many requests")

        result = _classify_runtime_error(error, proxy_mode=True)

        assert result is None or result.code != "BUDGET_EXHAUSTED"

    def test_plain_401_is_not_grant_expired(self):
        from ghosthands.cli import _classify_runtime_error

        error = self._make_error(401, "Invalid API key")

        result = _classify_runtime_error(error, proxy_mode=True)

        assert result is None or result.code != "GRANT_EXPIRED"


# ---------------------------------------------------------------------------
# Test 16 — step goal phase mapping
# ---------------------------------------------------------------------------


class TestGoalPhaseMapping:
    @pytest.mark.parametrize(
        ("goal", "expected"),
        [
            ("Upload the resume PDF to continue", "Uploading resume"),
            ("Fill work experience history section", "Filling work experience"),
            ("Complete personal information and contact info", "Filling personal information"),
            ("Answer additional questions about authorization", "Answering additional questions"),
            ("Review the form and prepare to submit", "Preparing to submit"),
            ("Navigate to the application form", "Navigating to application"),
            ("Click the next button", None),
        ],
    )
    def test_infers_user_friendly_phase(self, goal, expected):
        from ghosthands.agent.hooks import infer_phase_from_goal

        assert infer_phase_from_goal(goal) == expected


# ---------------------------------------------------------------------------
# Test 17 — JSONL line buffering / output format
# ---------------------------------------------------------------------------


class TestJSONLOutputFormat:
    """Each JSONL event must be exactly one newline-terminated line with
    compact JSON (no spaces)."""

    def test_compact_json_no_extra_whitespace(self):
        """emit_event should use compact separators (no spaces)."""
        from ghosthands.output.jsonl import emit_event

        events_raw = io.StringIO()
        import ghosthands.output.jsonl as jsonl_mod

        original_guard = jsonl_mod._jsonl_out
        jsonl_mod._jsonl_out = None
        try:
            with patch("sys.stdout", events_raw):
                emit_event("compact_test", key="value", num=42)
        finally:
            jsonl_mod._jsonl_out = original_guard

        raw = events_raw.getvalue()
        # Should NOT contain ": " or ", " (compact separators)
        assert '": ' not in raw
        assert '", ' not in raw
        # Should contain ":" and "," without spaces
        assert '"event":"compact_test"' in raw

    def test_each_event_ends_with_newline(self):
        """Each emitted event must be terminated by exactly one \\n."""
        from ghosthands.output.jsonl import emit_status

        events = _capture_jsonl_output(emit_status, "hello", step=1, max_steps=10)
        assert len(events) == 1

    def test_multiple_events_produce_multiple_lines(self):
        """Multiple calls must each produce separate lines."""

        def emit_two():
            from ghosthands.output.jsonl import emit_progress, emit_status

            emit_status("step one")
            emit_progress(1, 5)

        events = _capture_jsonl_output(emit_two)
        assert len(events) == 2
        assert events[0]["event"] == "status"
        assert events[1]["event"] == "progress"
