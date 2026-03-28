"""Flow tests for ghosthands.cli.run_agent_human."""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ghosthands.cli import run_agent_human


def _make_args(**overrides) -> argparse.Namespace:
    defaults = {
        "job_url": "https://boards.greenhouse.io/acme/jobs/123",
        "profile": '{"name": "Test User", "email": "test@example.com"}',
        "test_data": None,
        "resume": None,
        "job_id": "",
        "lease_id": "",
        "model": None,
        "max_steps": 10,
        "max_budget": 0.50,
        "headless": True,
        "output_format": "human",
        "proxy_url": None,
        "runtime_grant": None,
        "allowed_domains": None,
        "browsers_path": None,
        "cdp_url": None,
        "engine": "chromium",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _make_mock_history(*, is_done: bool = False, final_result: str | None = None) -> MagicMock:
    history = MagicMock()
    history.is_done.return_value = is_done
    history.final_result.return_value = final_result
    history.history = [MagicMock()] * 3
    usage = MagicMock()
    usage.total_cost = 0.12
    usage.total_prompt_tokens = 5000
    usage.total_completion_tokens = 1500
    history.usage = usage
    return history


def _make_mock_browser() -> AsyncMock:
    browser = AsyncMock()
    browser.cdp_url = None
    return browser


@pytest.mark.asyncio
async def test_run_agent_human_prints_cost_on_keyboard_interrupt(capsys: pytest.CaptureFixture[str]) -> None:
    """Ctrl+C during agent.run still prints the partial cost summary."""
    mock_history = _make_mock_history(is_done=False, final_result=None)
    mock_browser = _make_mock_browser()
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(side_effect=KeyboardInterrupt())
    mock_agent.history = mock_history

    mock_settings = MagicMock()
    mock_settings.credential_source = "generated"
    mock_settings.credential_intent = "signin"
    mock_settings.submit_intent = "review"
    mock_settings.llm_proxy_url = None

    mock_lockdown = MagicMock()
    mock_lockdown.freeze.return_value = None
    mock_lockdown.get_allowed_domains.return_value = ["boards.greenhouse.io"]

    cleanup_browser = AsyncMock()

    with (
        patch("ghosthands.cli._load_profile_async", AsyncMock(return_value={"name": "Test User"})),
        patch("ghosthands.cli._apply_runtime_env", return_value="/tmp/resume.pdf"),
        patch("ghosthands.cli._load_runtime_settings", return_value=mock_settings),
        patch("ghosthands.cli._warn_if_proxy_overrides_direct_keys"),
        patch("ghosthands.cli._resolve_sensitive_data", return_value=None),
        patch("ghosthands.cli._log_auth_debug_credentials"),
        patch("ghosthands.cli._cleanup_browser", cleanup_browser),
        patch("browser_use.Agent", return_value=mock_agent),
        patch("browser_use.BrowserSession", return_value=mock_browser),
        patch("browser_use.BrowserProfile", MagicMock()),
        patch("browser_use.Tools", return_value=MagicMock()),
        patch("ghosthands.llm.client.get_chat_model", return_value=MagicMock(model="test-model")),
        patch("ghosthands.actions.register_domhand_actions"),
        patch("ghosthands.platforms.detect_platform", return_value="greenhouse"),
        patch("ghosthands.agent.prompts.build_system_prompt", return_value="system"),
        patch("ghosthands.agent.prompts.build_task_prompt", return_value="task"),
        patch("ghosthands.security.domain_lockdown.DomainLockdown", return_value=mock_lockdown),
        pytest.raises(KeyboardInterrupt),
    ):
        await run_agent_human(_make_args())

    output = capsys.readouterr().out
    assert "RESULT (interrupted)" in output
    assert "Cost:" in output
    cleanup_browser.assert_awaited_once_with(mock_browser, False)
