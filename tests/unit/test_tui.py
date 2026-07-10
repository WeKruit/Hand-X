"""Tests for the terminal UI JSONL adapter."""

from __future__ import annotations

import argparse
import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from ghosthands.tui import TuiRunState, _render_state, build_engine_argv, parse_jsonl_event


def _args(**overrides):
    values = {
        "job_url": "https://example.com/job",
        "profile": '{"first_name":"Jane"}',
        "test_data": None,
        "user_id": None,
        "resume_id": None,
        "resume": "/tmp/resume.pdf",
        "job_id": "",
        "lease_id": "",
        "model": None,
        "max_steps": 50,
        "max_budget": 0.5,
        "submit_intent": "review",
        "proxy_url": None,
        "runtime_grant": None,
        "allowed_domains": None,
        "browsers_path": None,
        "cdp_url": None,
        "engine": "auto",
        "headless": False,
        "output_format": "tui",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_parse_jsonl_event_rejects_non_events():
    assert parse_jsonl_event("not-json") is None
    assert parse_jsonl_event("[]") is None
    assert parse_jsonl_event('{"timestamp":1}') is None


def test_tui_state_tracks_core_events():
    state = TuiRunState()
    for line in (
        '{"event":"handshake","protocol_version":1}',
        '{"event":"phase","phase":"Starting application"}',
        '{"event":"status","message":"Step 2","step":2,"maxSteps":50}',
        '{"event":"field_filled","field":"email","value":"jane@example.com"}',
        '{"event":"cost","total_usd":0.0123,"prompt_tokens":10,"completion_tokens":5}',
        '{"event":"awaiting_review","message":"Review now","pageUrl":"https://example.com/review"}',
    ):
        event = parse_jsonl_event(line)
        assert event is not None
        state.apply_event(event)

    assert state.phase == "Review"
    assert state.step == 2
    assert state.max_steps == 50
    assert state.fields_filled == 1
    assert state.cost_usd == 0.0123
    assert state.awaiting_review is True
    assert state.page_url == "https://example.com/review"


def test_tui_state_tracks_sync_events():
    state = TuiRunState(job_id="job-1", lease_id="lease-1", sync_status="VALET sync pending")

    for line in (
        '{"event":"lease_acquired","leaseId":"lease-1","jobId":"job-1"}',
        '{"event":"lease_heartbeat","leaseId":"lease-1"}',
        '{"event":"lease_released","leaseId":"lease-1","reason":"completed"}',
    ):
        event = parse_jsonl_event(line)
        assert event is not None
        state.apply_event(event)

    assert state.job_id == "job-1"
    assert state.lease_id == "lease-1"
    assert state.sync_status == "VALET released (completed)"
    assert state.logs[-1] == ("sync", "VALET released (completed)")


def test_tui_state_redacts_password_values():
    state = TuiRunState()
    event = parse_jsonl_event('{"event":"field_filled","field":"password","value":"secret"}')
    assert event is not None

    state.apply_event(event)

    assert state.logs[-1] == ("filled", "password: [redacted]")


@pytest.mark.asyncio
async def test_tui_checkbox_prompt_sends_saved_multi_answer(monkeypatch):
    from argparse import Namespace

    from ghosthands.tui import TuiRunState, _handle_hitl_prompt

    state = TuiRunState(
        pending_question={
            "fieldId": "skills",
            "fieldLabel": "Skills",
            "fieldType": "checkbox",
            "options": ["Python", "Go"],
        }
    )
    proc = Namespace(stdin=AsyncMock())
    sent: list[dict] = []
    monkeypatch.setattr("ghosthands.tui.Confirm.ask", MagicMock(side_effect=[True, True, True]))
    monkeypatch.setattr(
        "ghosthands.tui._send_command", AsyncMock(side_effect=lambda _proc, command: sent.append(command))
    )

    await _handle_hitl_prompt(proc, state, MagicMock())

    assert sent == [
        {
            "type": "save_answer",
            "field_id": "skills",
            "field_label": "Skills",
            "answer": ["Python", "Go"],
        }
    ]


def test_tui_render_treats_log_messages_as_plain_text():
    state = TuiRunState()
    event = parse_jsonl_event('{"event":"field_filled","field":"password","value":"secret"}')
    assert event is not None
    state.apply_event(event)
    output = io.StringIO()
    console = Console(file=output, width=100, force_terminal=False)

    console.print(_render_state(state))

    assert "password: [redacted]" in output.getvalue()


def test_tui_render_shows_sync_status():
    state = TuiRunState(job_id="job-1", lease_id="lease-1", sync_status="VALET sync active")
    output = io.StringIO()
    console = Console(file=output, width=120, force_terminal=False)

    console.print(_render_state(state))

    text = output.getvalue()
    assert "VALET sync active" in text
    assert "job-1" in text
    assert "lease-1" in text


def test_build_engine_argv_forces_jsonl_child_mode():
    argv = build_engine_argv(_args(), executable=["hand-x"])

    assert argv[:1] == ["hand-x"]
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "jsonl"
    assert "tui" not in argv
    assert "--job-url" in argv
    assert argv[argv.index("--job-url") + 1] == "https://example.com/job"


def test_build_engine_argv_passes_headless_flag_only_when_enabled():
    argv = build_engine_argv(_args(headless=True), executable=["hand-x"])

    assert "--headless" in argv
