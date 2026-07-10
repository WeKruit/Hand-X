"""Focused tests for the Hand-X boundary consumed by the Go VALET runtime."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _parse(monkeypatch: pytest.MonkeyPatch, *args: str) -> argparse.Namespace:
    from ghosthands.cli import parse_args

    monkeypatch.setattr(sys, "argv", ["hand-x", *args])
    return parse_args()


def _capture_events(callback) -> list[dict]:
    output = io.StringIO()
    with patch("ghosthands.output.jsonl._get_output", return_value=output):
        callback()
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_cli_accepts_go_cdp_target_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _parse(
        monkeypatch,
        "--job-url",
        "https://example.com/job",
        "--cdp-url",
        "http://127.0.0.1:49321",
        "--cdp-target-id",
        "target-a",
    )

    assert args.cdp_target_id == "target-a"


@pytest.mark.asyncio
async def test_missing_requested_chrome_target_fails_without_creating_a_tab() -> None:
    from browser_use.browser.session import BrowserSession

    session = BrowserSession(
        cdp_url="ws://127.0.0.1:9222/devtools/browser/shared",
        target_id="target-missing",
    )
    cdp_client = AsyncMock()
    cdp_client.send = MagicMock()
    cdp_client.send.Target = MagicMock()
    cdp_client.send.Target.setAutoAttach = AsyncMock()
    cdp_client.send.Target.createTarget = AsyncMock(return_value={"targetId": "replacement"})
    manager = MagicMock()
    manager.start_monitoring = AsyncMock()
    manager.get_all_page_targets.return_value = []
    manager.clear = AsyncMock()

    with (
        patch("browser_use.browser.session.CDPClient", return_value=cdp_client),
        patch("browser_use.browser.session_manager.SessionManager", return_value=manager),
        pytest.raises(RuntimeError, match="target-missing"),
    ):
        await session.connect()

    cdp_client.send.Target.createTarget.assert_not_awaited()


def test_public_tui_always_forces_review_intent() -> None:
    from ghosthands.tui import build_engine_argv

    args = argparse.Namespace(
        job_url="https://example.com/job",
        profile='{"first_name":"Jane"}',
        test_data=None,
        user_id=None,
        resume_id=None,
        resume=None,
        job_id="job-1",
        lease_id="lease-1",
        model=None,
        max_steps=20,
        max_budget=0.5,
        submit_intent="submit",
        proxy_url=None,
        runtime_grant=None,
        allowed_domains=None,
        browsers_path=None,
        cdp_url=None,
        cdp_target_id=None,
        engine="auto",
        headless=False,
        output_format="tui",
    )

    argv = build_engine_argv(args, executable=["hand-x"])

    index = argv.index("--submit-intent")
    assert argv[index + 1] == "review"
    assert "submit" not in argv


def test_tui_accepts_canonical_go_review_event() -> None:
    from ghosthands.tui import TuiRunState, parse_jsonl_event

    event = parse_jsonl_event(
        '{"type":"review_ready","status":"review_ready","version":8,'
        '"message":"Review the completed form","targetId":"target-a"}'
    )

    assert event is not None
    state = TuiRunState()
    state.apply_event(event)
    assert state.review_ready is True
    assert state.phase == "Review"


def test_go_jsonl_contract_is_versioned_and_never_reports_submission() -> None:
    from ghosthands.output.jsonl import (
        emit_cost,
        emit_handshake,
        emit_needs_answer,
        emit_review_ready,
    )

    def emit() -> None:
        emit_handshake()
        emit_cost(0.12, cost_summary={"total_tracked_cost_usd": 0.12})
        emit_needs_answer(
            field_id="field-1",
            label="Authorized to work?",
            field_type="choice",
            options=["Yes", "No"],
            required=True,
            section="Eligibility",
        )
        emit_review_ready(
            cdp_url="http://127.0.0.1:49321",
            target_id="target-a",
            page_url="https://example.com/job",
            cost_summary={"total_tracked_cost_usd": 0.12},
        )

    events = _capture_events(emit)

    assert [event["version"] for event in events] == list(range(1, len(events) + 1))
    assert all(event["type"] == event["event"] for event in events)
    assert events[1]["type"] == "cost"
    assert events[1]["costSummary"]["actualCostCents"] == 12
    assert events[2]["status"] == "waiting_human"
    assert events[2]["questions"] == [
        {
            "fieldId": "field-1",
            "fieldLabel": "Authorized to work?",
            "fieldType": "choice",
            "options": ["Yes", "No"],
            "required": True,
            "section": "Eligibility",
        }
    ]
    assert events[3]["status"] == "review_ready"
    encoded = json.dumps(events).lower()
    assert '"status":"submitted"' not in encoded
    assert '"status":"applied"' not in encoded


class _PauseableAgent:
    def __init__(self) -> None:
        self.state = SimpleNamespace(stopped=False, paused=False)
        self.browser_session = SimpleNamespace(
            cdp_url="http://127.0.0.1:49321",
            agent_focus_target_id="target-a",
        )
        self._external_pause_event = asyncio.Event()
        self._external_pause_event.set()
        self.pause_calls = 0
        self.resume_calls = 0
        self.stop_calls = 0

    def pause(self) -> None:
        self.pause_calls += 1
        self.state.paused = True
        self._external_pause_event.clear()

    def resume(self) -> None:
        self.resume_calls += 1
        self.state.paused = False
        self._external_pause_event.set()

    def stop(self) -> None:
        self.stop_calls += 1
        self.state.stopped = True
        self._external_pause_event.set()


@pytest.mark.asyncio
async def test_pause_resume_commands_gate_actions_without_browser_cleanup() -> None:
    from ghosthands.bridge import protocol

    protocol.reset_hitl_state()
    agent = _PauseableAgent()
    commands: asyncio.Queue[str] = asyncio.Queue()

    async def read_command(timeout: float | None = None) -> str:
        return await asyncio.wait_for(commands.get(), timeout=timeout)

    async def next_action() -> None:
        assert await protocol.wait_for_run_resume() is True

    with (
        patch("ghosthands.bridge.protocol.read_stdin_line", side_effect=read_command),
        patch("ghosthands.output.jsonl.emit_paused") as emit_paused,
        patch("ghosthands.output.jsonl.emit_resumed") as emit_resumed,
    ):
        listener = asyncio.create_task(protocol.listen_for_cancel(agent, job_id="job-1"))
        await commands.put('{"type":"pause_job"}\n')
        async with asyncio.timeout(1):
            while not agent.state.paused:
                await asyncio.sleep(0)

        action = asyncio.create_task(next_action())
        await asyncio.sleep(0)
        assert action.done() is False

        await commands.put('{"type":"resume_job"}\n')
        await asyncio.wait_for(action, timeout=1)
        await commands.put('{"type":"cancel_job"}\n')
        await asyncio.wait_for(listener, timeout=1)

    assert agent.pause_calls == 1
    assert agent.resume_calls == 1
    assert agent.stop_calls == 1
    assert protocol.is_hitl_available() is False
    assert await protocol.wait_for_run_resume() is False
    protocol.reset_hitl_state()
    assert await protocol.wait_for_run_resume() is True
    emit_paused.assert_called_once()
    emit_resumed.assert_called_once()

    review_browser = SimpleNamespace(
        agent_focus_target_id="target-a",
        detach_keep_alive=AsyncMock(),
    )
    review_commands = [
        '{"type":"pause_job"}\n',
        '{"type":"resume_job"}\n',
        '{"type":"complete_review"}\n',
    ]
    with (
        patch("ghosthands.bridge.protocol.read_stdin_line", new=AsyncMock(side_effect=review_commands)),
        patch("ghosthands.output.jsonl.emit_paused") as review_paused,
        patch("ghosthands.output.jsonl.emit_resumed") as review_resumed,
        patch("ghosthands.output.jsonl.emit_run_state"),
        patch("ghosthands.output.jsonl.emit_status"),
    ):
        result = await protocol.wait_for_review_command(review_browser, "job-1", "lease-1")

    assert result == "complete"
    review_paused.assert_called_once()
    review_resumed.assert_called_once()
    review_browser.detach_keep_alive.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_guard_init_failure_is_fatal() -> None:
    hooks = importlib.import_module("ghosthands.agent.hooks")

    browser = MagicMock()
    browser._cdp_add_init_script = AsyncMock(side_effect=RuntimeError("CDP rejected script"))
    browser.get_current_page = AsyncMock()
    hooks._FINAL_SUBMIT_GUARD_INSTALLED.discard(id(browser))

    with pytest.raises(RuntimeError, match="CDP rejected script"):
        await hooks.install_final_submit_guard(SimpleNamespace(browser_session=browser), allow_submit=False)

    browser.get_current_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_guard_current_page_failure_is_fatal() -> None:
    hooks = importlib.import_module("ghosthands.agent.hooks")

    page = MagicMock()
    page.evaluate = AsyncMock(side_effect=RuntimeError("page rejected script"))
    browser = MagicMock()
    browser._cdp_add_init_script = AsyncMock()
    browser.get_current_page = AsyncMock(return_value=page)
    hooks._FINAL_SUBMIT_GUARD_INSTALLED.discard(id(browser))

    with pytest.raises(RuntimeError, match="page rejected script"):
        await hooks.install_final_submit_guard(SimpleNamespace(browser_session=browser), allow_submit=False)


def test_completed_review_result_never_claims_submitted_or_applied() -> None:
    from ghosthands.cli import _handle_review_result

    with patch("ghosthands.output.jsonl_terminal.emit_run_terminal") as emit_terminal:
        result = _handle_review_result(
            "complete",
            fields_filled=3,
            fields_failed=0,
            job_id="job-1",
            lease_id="lease-1",
            result_data={"success": True},
            cost_summary={},
            total_cost_usd=0.0,
        )

    assert result is None
    payload = emit_terminal.call_args.kwargs
    encoded = json.dumps(payload).lower()
    assert "submitted" not in encoded
    assert "applied" not in encoded
