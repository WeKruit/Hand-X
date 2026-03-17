"""Flow tests for ghosthands.cli.run_agent_jsonl.

Tests cover the four main outcomes of the 362-line async function:
1. Success path  -> emit_awaiting_review -> wait_for_review_command
2. Failure path  -> emit_done(success=False) -> cleanup -> exit(1)
3. Cancel path   -> emit_done(cancelled=True) -> cleanup -> exit(1)
4. Exception path -> emit_error -> cleanup -> exit(1)

All heavy dependencies (browser-use, Agent, BrowserSession, LLM) are
mocked so these tests run without playwright or API keys.

Strategy: install stub modules into sys.modules at import time so the
``from X import Y`` local imports inside run_agent_jsonl resolve to our
mocks.  A session-scoped fixture saves and restores the original modules
to prevent pollution of downstream test files.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module-level setup: stub ALL the modules run_agent_jsonl imports from.
# We track which modules we installed so we can restore them later.
# ---------------------------------------------------------------------------

_INSTALLED_STUBS: dict[str, types.ModuleType | None] = {}
"""Maps module name -> original module (or None if we created it)."""

# Module-level mocks for assertions
_m_emit_status = MagicMock()
_m_emit_phase = MagicMock()
_m_emit_cost = MagicMock()
_m_emit_browser_ready = MagicMock()
_m_emit_error = MagicMock()
_m_emit_done = MagicMock()
_m_emit_awaiting_review = MagicMock()
_m_emit_account_created = MagicMock()
_m_cleanup_browser = AsyncMock()


def _install_stub(name: str, mod: types.ModuleType) -> None:
    """Install a module stub, recording the original for cleanup."""
    if name not in _INSTALLED_STUBS:
        _INSTALLED_STUBS[name] = sys.modules.get(name)
    sys.modules[name] = mod


def _restore_stubs() -> None:
    """Restore all modules to their pre-stub state."""
    for name, original in _INSTALLED_STUBS.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    _INSTALLED_STUBS.clear()


def _reset_mocks() -> None:
    """Reset all module-level mocks."""
    for m in [_m_emit_status, _m_emit_phase, _m_emit_cost,
              _m_emit_browser_ready, _m_emit_error, _m_emit_done,
              _m_emit_awaiting_review, _m_emit_account_created, _m_cleanup_browser]:
        m.reset_mock()


def _stub_all():
    """Install stubs for every module run_agent_jsonl imports from."""

    # ---- browser_use / cdp_use stubs ----
    # Always create FRESH stub modules (never mutate real modules)
    for name in [
        "cdp_use",
        "browser_use",
        "browser_use.agent",
        "browser_use.agent.service",
        "browser_use.browser",
        "browser_use.browser.session",
        "browser_use.browser.providers",
        "browser_use.browser.providers.route_selector",
        "browser_use.tools",
        "browser_use.tools.service",
    ]:
        _install_stub(name, types.ModuleType(name))

    bu = sys.modules["browser_use"]
    bu.Agent = MagicMock
    bu.BrowserProfile = MagicMock
    bu.BrowserSession = MagicMock
    bu.Tools = MagicMock

    rs_mod = sys.modules["browser_use.browser.providers.route_selector"]
    rs_mod.RouteSelector = type("RouteSelector", (), {
        "select_engine": staticmethod(lambda *a, **kw: "chromium"),
    })

    # ---- browser_use.llm.anthropic ----
    _chat_key = "browser_use.llm.anthropic.chat"
    if _chat_key not in sys.modules:
        _install_stub("browser_use.llm.anthropic", types.ModuleType("browser_use.llm.anthropic"))
        chat_mod = types.ModuleType(_chat_key)
        chat_mod.ChatAnthropic = type("ChatAnthropic", (), {})
        _install_stub(_chat_key, chat_mod)
        ser_key = "browser_use.llm.anthropic.serializer"
        ser_mod = types.ModuleType(ser_key)
        ser_mod.AnthropicMessageSerializer = type("AnthropicMessageSerializer", (), {})
        _install_stub(ser_key, ser_mod)

    # ---- ghosthands.agent.* ----
    ga = types.ModuleType("ghosthands.agent")
    _install_stub("ghosthands.agent", ga)

    ga_factory = types.ModuleType("ghosthands.agent.factory")
    _install_stub("ghosthands.agent.factory", ga_factory)

    ga_hooks = types.ModuleType("ghosthands.agent.hooks")
    ga_hooks.install_same_tab_guard = AsyncMock()
    ga_hooks.infer_phase_from_goal = MagicMock(return_value=None)
    _install_stub("ghosthands.agent.hooks", ga_hooks)

    ga_prompts = types.ModuleType("ghosthands.agent.prompts")
    ga_prompts.build_system_prompt = MagicMock(return_value="mock system prompt")
    ga_prompts.build_task_prompt = MagicMock(return_value="Go to https://... and fill")
    ga_prompts.build_completion_detection_text = MagicMock(return_value="Completion text")
    ga_prompts.FAIL_OVER_NATIVE_SELECT = "FAIL_OVER_NATIVE_SELECT"
    ga_prompts.FAIL_OVER_CUSTOM_WIDGET = "FAIL_OVER_CUSTOM_WIDGET"
    _install_stub("ghosthands.agent.prompts", ga_prompts)

    # ---- ghosthands.output.* ----
    go = types.ModuleType("ghosthands.output")
    _install_stub("ghosthands.output", go)

    go_jsonl = types.ModuleType("ghosthands.output.jsonl")
    go_jsonl.emit_status = _m_emit_status
    go_jsonl.emit_phase = _m_emit_phase
    go_jsonl.emit_cost = _m_emit_cost
    go_jsonl.emit_browser_ready = _m_emit_browser_ready
    go_jsonl.emit_error = _m_emit_error
    go_jsonl.emit_done = _m_emit_done
    go_jsonl.emit_awaiting_review = _m_emit_awaiting_review
    go_jsonl.emit_account_created = _m_emit_account_created
    _install_stub("ghosthands.output.jsonl", go_jsonl)

    go_fe = types.ModuleType("ghosthands.output.field_events")
    go_fe.install_jsonl_callback = MagicMock()
    go_fe.get_field_counts = MagicMock(return_value=(10, 2))
    _install_stub("ghosthands.output.field_events", go_fe)
    go.field_events = go_fe

    # ---- ghosthands.llm.client ----
    _install_stub("ghosthands.llm", types.ModuleType("ghosthands.llm"))
    glc = types.ModuleType("ghosthands.llm.client")
    glc.get_chat_model = MagicMock(return_value=MagicMock())
    _install_stub("ghosthands.llm.client", glc)

    # ---- ghosthands.actions ----
    ga_act = types.ModuleType("ghosthands.actions")
    ga_act.register_domhand_actions = MagicMock()
    _install_stub("ghosthands.actions", ga_act)

    # ---- ghosthands.platforms ----
    gp = types.ModuleType("ghosthands.platforms")
    gp.detect_platform = MagicMock(return_value="greenhouse")
    _install_stub("ghosthands.platforms", gp)

    # ---- ghosthands.security.domain_lockdown ----
    _install_stub("ghosthands.security", types.ModuleType("ghosthands.security"))
    gsdl = types.ModuleType("ghosthands.security.domain_lockdown")
    mock_lockdown = MagicMock()
    mock_lockdown.get_allowed_domains.return_value = ["greenhouse.io"]
    gsdl.create_lockdown_for_platform = MagicMock(return_value=mock_lockdown)
    gsdl.DomainLockdown = MagicMock(return_value=mock_lockdown)
    _install_stub("ghosthands.security.domain_lockdown", gsdl)


_stub_all()

from ghosthands.cli import run_agent_jsonl  # noqa: E402

# After importing, restore modules so later test files get real ones
_restore_stubs()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(**overrides) -> argparse.Namespace:
    defaults = {
        "job_url": "https://boards.greenhouse.io/acme/jobs/123",
        "profile": '{"name": "Test User", "email": "test@example.com"}',
        "test_data": None,
        "resume": None,
        "job_id": "job-test-001",
        "lease_id": "lease-test-001",
        "model": None,
        "max_steps": 10,
        "max_budget": 0.50,
        "headless": True,
        "output_format": "jsonl",
        "proxy_url": None,
        "runtime_grant": None,
        "allowed_domains": None,
        "browsers_path": None,
        "cdp_url": None,
        "engine": "chromium",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _make_mock_history(*, is_done=True, final_result="Application submitted"):
    history = MagicMock()
    history.is_done.return_value = is_done
    history.final_result.return_value = final_result
    history.history = [MagicMock()] * 5
    usage = MagicMock()
    usage.total_cost = 0.12
    usage.total_prompt_tokens = 5000
    usage.total_completion_tokens = 1500
    history.usage = usage
    return history


def _make_mock_browser(*, cdp_url="ws://127.0.0.1:9222/devtools/browser/abc"):
    browser = AsyncMock()
    browser.cdp_url = cdp_url
    browser.start = AsyncMock()
    browser.stop = AsyncMock()
    browser.close = AsyncMock()
    browser.kill = AsyncMock()
    browser.get_current_page_url = AsyncMock(return_value="https://boards.greenhouse.io/acme/jobs/123")
    return browser


@contextlib.contextmanager
def _apply_stubs_and_patches(mock_browser, mock_agent, extra_patches=None):
    """Install stub modules and apply patches for a single test.

    Stubs are installed into sys.modules for the duration of the test,
    then restored when the context manager exits.
    """
    _stub_all()
    _reset_mocks()

    mock_settings = MagicMock()
    mock_settings.job_id = ""
    mock_settings.lease_id = ""
    mock_settings.llm_proxy_url = None

    patches = [
        patch("ghosthands.cli._load_profile", return_value={"name": "Test User", "email": "test@example.com"}),
        patch("ghosthands.cli._apply_runtime_env", return_value="/tmp/resume.pdf"),
        patch("ghosthands.cli._load_runtime_settings", return_value=mock_settings),
        patch("ghosthands.cli._warn_if_proxy_overrides_direct_keys"),
        patch("ghosthands.cli._resolve_sensitive_data", return_value=None),
        patch("ghosthands.cli._classify_runtime_error", return_value=None),
        patch("ghosthands.cli._cleanup_browser", _m_cleanup_browser),
        patch("browser_use.Agent", return_value=mock_agent),
        patch("browser_use.BrowserSession", return_value=mock_browser),
        patch("browser_use.BrowserProfile", MagicMock()),
        patch("browser_use.Tools", return_value=MagicMock()),
    ]
    if extra_patches:
        patches.extend(extra_patches)

    try:
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            yield
    finally:
        _restore_stubs()


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


class TestRunAgentJsonlFlow:
    """Flow tests for the run_agent_jsonl function."""

    # ------------------------------------------------------------------
    # Scenario 1: Success path
    # ------------------------------------------------------------------

    async def test_success_path_emits_awaiting_review(self):
        """When agent.run() succeeds, emit_awaiting_review is called
        before wait_for_review_command."""
        mock_history = _make_mock_history(is_done=True, final_result="Application submitted")
        mock_browser = _make_mock_browser()

        mock_agent = AsyncMock()
        mock_agent.run = AsyncMock(return_value=mock_history)
        mock_agent.state = MagicMock(n_steps=5, last_model_output=None, stopped=False)

        call_order = []
        _m_emit_awaiting_review.side_effect = lambda **kw: call_order.append("awaiting_review")
        mock_wait = AsyncMock(side_effect=lambda *a, **kw: call_order.append("wait_for_review") or "complete")

        extra = [
            patch("ghosthands.cli.wait_for_review_command", mock_wait),
            patch("ghosthands.cli._handle_review_result", return_value=None),
        ]

        with _apply_stubs_and_patches(mock_browser, mock_agent, extra):
            await run_agent_jsonl(_make_args())

        _m_emit_awaiting_review.assert_called_once()
        assert call_order.index("awaiting_review") < call_order.index("wait_for_review")

    # ------------------------------------------------------------------
    # Scenario 2: Failure path
    # ------------------------------------------------------------------

    async def test_failure_path_emits_done_false(self):
        """When is_done=False, emit_done(success=False) + sys.exit(1)."""
        mock_history = _make_mock_history(is_done=False, final_result=None)
        mock_browser = _make_mock_browser()

        mock_agent = AsyncMock()
        mock_agent.run = AsyncMock(return_value=mock_history)
        mock_agent.state = MagicMock(n_steps=10, last_model_output=None, stopped=False)

        with _apply_stubs_and_patches(mock_browser, mock_agent), \
             pytest.raises(SystemExit) as exc_info:
            await run_agent_jsonl(_make_args())

        assert exc_info.value.code == 1
        _m_emit_done.assert_called_once()
        _, kwargs = _m_emit_done.call_args
        assert kwargs["success"] is False
        _m_cleanup_browser.assert_called_once()

    # ------------------------------------------------------------------
    # Scenario 3: Cancel path
    # ------------------------------------------------------------------

    async def test_cancel_path_exits_with_1(self):
        """When cancel_requested is set, emit_done with cancelled=True + exit(1)."""
        mock_history = _make_mock_history(is_done=True, final_result="Done")
        mock_browser = _make_mock_browser()

        mock_agent = AsyncMock()
        mock_agent.state = MagicMock(n_steps=3, last_model_output=None, stopped=False)

        async def _fake_listen_for_cancel(agent, cancel_requested):
            cancel_requested.set()

        async def _run_that_yields(**kwargs):
            await asyncio.sleep(0)
            return mock_history

        mock_agent.run = AsyncMock(side_effect=_run_that_yields)

        extra = [patch("ghosthands.cli.listen_for_cancel", _fake_listen_for_cancel)]

        with _apply_stubs_and_patches(mock_browser, mock_agent, extra), \
             pytest.raises(SystemExit) as exc_info:
            await run_agent_jsonl(_make_args())

        assert exc_info.value.code == 1
        _m_emit_done.assert_called_once()
        kwargs = _m_emit_done.call_args.kwargs
        assert kwargs["success"] is False
        result_data = kwargs.get("result_data") or {}
        assert result_data.get("cancelled") is True

    # ------------------------------------------------------------------
    # Scenario 4: Exception path
    # ------------------------------------------------------------------

    async def test_exception_path_emits_error(self):
        """When agent.run() raises, emit_error + cleanup + exit(1)."""
        mock_browser = _make_mock_browser()

        mock_agent = AsyncMock()
        mock_agent.run = AsyncMock(side_effect=RuntimeError("LLM connection timeout"))
        mock_agent.state = MagicMock(n_steps=0, last_model_output=None, stopped=False)

        with _apply_stubs_and_patches(mock_browser, mock_agent), \
             pytest.raises(SystemExit) as exc_info:
            await run_agent_jsonl(_make_args())

        assert exc_info.value.code == 1
        _m_emit_error.assert_called()
        first_arg = _m_emit_error.call_args[0][0]
        assert "unexpected error" in first_arg.lower()
        _m_cleanup_browser.assert_called()

    # ------------------------------------------------------------------
    # Scenario 5: Success + complete review exits cleanly
    # ------------------------------------------------------------------

    async def test_success_complete_review_no_exit(self):
        """When review returns 'complete' and _handle_review_result returns None,
        run_agent_jsonl finishes without sys.exit."""
        mock_history = _make_mock_history(is_done=True, final_result="Application submitted")
        mock_browser = _make_mock_browser()

        mock_agent = AsyncMock()
        mock_agent.run = AsyncMock(return_value=mock_history)
        mock_agent.state = MagicMock(n_steps=5, last_model_output=None, stopped=False)

        extra = [
            patch("ghosthands.cli.wait_for_review_command", AsyncMock(return_value="complete")),
            patch("ghosthands.cli._handle_review_result", return_value=None),
        ]

        with _apply_stubs_and_patches(mock_browser, mock_agent, extra):
            await run_agent_jsonl(_make_args())

        _m_cleanup_browser.assert_not_called()

    # ------------------------------------------------------------------
    # Scenario 6: Blocker in final_result
    # ------------------------------------------------------------------

    async def test_blocker_in_result_marks_failure(self):
        """When final_result contains 'blocker:', success=False."""
        mock_history = _make_mock_history(
            is_done=True,
            final_result="Blocker: CAPTCHA detected, cannot proceed",
        )
        mock_browser = _make_mock_browser()

        mock_agent = AsyncMock()
        mock_agent.run = AsyncMock(return_value=mock_history)
        mock_agent.state = MagicMock(n_steps=8, last_model_output=None, stopped=False)

        with _apply_stubs_and_patches(mock_browser, mock_agent), \
             pytest.raises(SystemExit) as exc_info:
            await run_agent_jsonl(_make_args())

        assert exc_info.value.code == 1
        _m_emit_done.assert_called_once()
        _, kwargs = _m_emit_done.call_args
        assert kwargs["success"] is False
        assert "CAPTCHA" in (kwargs.get("message") or "")

    async def test_answerable_blocker_uses_browser_issues_to_resume(self):
        """Browser-exposed unresolved required fields should trigger HITL recovery even with vague blocker text."""
        mock_history_blocked = _make_mock_history(
            is_done=True,
            final_result="blocker: still stuck after clicking Save and Continue",
        )
        mock_history_success = _make_mock_history(
            is_done=True,
            final_result="Application submitted",
        )
        mock_browser = _make_mock_browser()

        mock_agent = AsyncMock()
        mock_agent.run = AsyncMock(side_effect=[mock_history_blocked, mock_history_success])
        mock_agent.state = MagicMock(n_steps=8, last_model_output=None, stopped=False)

        issue = MagicMock()
        issue.field_label = "Expectations on Compensation"
        issue.field_id = "compensation"
        issue.field_type = "text"
        issue.question_text = "Expectations on Compensation"
        issue.section = "Application Questions"

        extra = [
            patch("ghosthands.cli.is_hitl_available", return_value=True),
            patch(
                "ghosthands.cli._collect_open_question_issues_from_browser",
                AsyncMock(return_value=[issue]),
            ),
            patch(
                "ghosthands.cli._request_open_question_answers",
                AsyncMock(return_value=({"Expectations on Compensation": "$30/hour"}, False)),
            ),
            patch("ghosthands.cli.wait_for_review_command", AsyncMock(return_value="complete")),
            patch("ghosthands.cli._handle_review_result", return_value=None),
        ]

        with _apply_stubs_and_patches(mock_browser, mock_agent, extra):
            await run_agent_jsonl(_make_args())

        assert mock_agent.run.await_count == 2

    # ------------------------------------------------------------------
    # Scenario 7: Cost event emitted
    # ------------------------------------------------------------------

    async def test_cost_emitted_after_agent_run(self):
        """After agent.run() completes, a final cost event is emitted."""
        mock_history = _make_mock_history(is_done=False, final_result=None)
        mock_browser = _make_mock_browser()

        mock_agent = AsyncMock()
        mock_agent.run = AsyncMock(return_value=mock_history)
        mock_agent.state = MagicMock(n_steps=5, last_model_output=None, stopped=False)

        with _apply_stubs_and_patches(mock_browser, mock_agent), \
             pytest.raises(SystemExit):
            await run_agent_jsonl(_make_args())

        _m_emit_cost.assert_called()
        _, kwargs = _m_emit_cost.call_args
        assert kwargs.get("total_usd") == 0.12
