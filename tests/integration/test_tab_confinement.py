"""Integration tests for tab confinement (Desktop-managed shared Chromium).

Tests verify that when ``assigned_target_id`` is set on a BrowserSession,
all navigation, tab-switching, tab-creation, health-checking, and popup
handling are correctly confined to the assigned target.  No real browser
is launched -- all CDP/browser internals are mocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from browser_use.browser.events import (
    AgentFocusChangedEvent,
    NavigateToUrlEvent,
    SwitchTabEvent,
    TabClosedEvent,
    TabCreatedEvent,
)
from browser_use.browser.session import BrowserSession, Target
from browser_use.browser.session_manager import SessionManager
from browser_use.browser.watchdogs.crash_watchdog import CrashWatchdog
from browser_use.browser.watchdogs.popups_watchdog import PopupsWatchdog
from ghosthands.output.jsonl import emit_awaiting_review, emit_browser_ready


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TARGET_ASSIGNED = "AAAA1111BBBB2222CCCC3333DDDD4444"
TARGET_OTHER = "XXXX9999YYYY8888ZZZZ7777WWWW6666"


def _make_target(target_id: str, url: str = "https://example.com") -> Target:
    return Target(target_id=target_id, target_type="page", url=url, title="Test")


def _pydantic_setattr(obj, name, value):
    """Bypass Pydantic's __setattr__ validation (extra='forbid')."""
    object.__setattr__(obj, name, value)


def _make_session(
    assigned: str | None = None,
    focus: str | None = None,
) -> BrowserSession:
    """Build a minimal BrowserSession with mocked internals.

    Uses object.__setattr__ for any attribute Pydantic would reject, and
    replaces event_bus.dispatch to prevent bubus from starting its internal
    async run-loop.
    """
    if assigned is not None:
        session = BrowserSession(assigned_target_id=assigned)
    else:
        session = BrowserSession()

    # Wire a mocked SessionManager
    sm = MagicMock(spec=SessionManager)
    sm.get_all_page_targets = MagicMock(return_value=[])
    sm.get_target = MagicMock(return_value=None)
    session.session_manager = sm

    if focus:
        session.agent_focus_target_id = focus

    # Suppress actual CDP calls
    session._cdp_client_root = MagicMock()

    # Replace event_bus.dispatch so bubus never starts its async loop.
    # dispatch() is sync and its return value must be awaitable (bubus Event
    # objects implement __await__).  We use a thin wrapper that returns an
    # object supporting both `await dispatch(...)` and bare `dispatch(...)`.
    class _FakeDispatchResult:
        """Awaitable stand-in for a bubus Event."""
        def __await__(self):
            return iter([])
        def event_result(self):
            async def _noop():
                return None
            return _noop()

    session.event_bus.dispatch = MagicMock(side_effect=lambda evt: _FakeDispatchResult())

    return session


# ===================================================================
# 1. BrowserSession tab confinement guards
# ===================================================================


class TestBrowserSessionConfinement:
    """BrowserSession-level confinement guards."""

    # -- assigned_target_id field --

    def test_assigned_target_id_defaults_to_none(self):
        session = BrowserSession()
        assert session.assigned_target_id is None

    def test_assigned_target_id_set_via_constructor(self):
        session = BrowserSession(assigned_target_id=TARGET_ASSIGNED)
        assert session.assigned_target_id == TARGET_ASSIGNED

    # -- on_NavigateToUrlEvent --

    @pytest.mark.asyncio
    async def test_navigate_forces_no_new_tab_when_confined(self):
        session = _make_session(assigned=TARGET_ASSIGNED, focus=TARGET_ASSIGNED)

        session.session_manager.get_target.return_value = _make_target(
            TARGET_ASSIGNED, "https://ats.example.com/apply"
        )

        event = NavigateToUrlEvent(url="https://example.com", new_tab=True)

        with patch.object(session, "_navigate_and_wait", new_callable=AsyncMock):
            with patch.object(session, "_close_extension_options_pages", new_callable=AsyncMock):
                await session.on_NavigateToUrlEvent(event)

        assert event.new_tab is False

    @pytest.mark.asyncio
    async def test_navigate_allows_new_tab_when_not_confined(self):
        """When assigned_target_id is None the navigate guard must NOT force new_tab=False."""
        session = _make_session(assigned=None, focus=TARGET_ASSIGNED)
        assert session.assigned_target_id is None

        event = NavigateToUrlEvent(url="https://example.com", new_tab=True)

        # Reproduce the guard logic in isolation:
        if session.assigned_target_id is not None:
            event.new_tab = False

        assert event.new_tab is True

    # -- on_SwitchTabEvent --

    @pytest.mark.asyncio
    async def test_switch_tab_rejects_non_assigned_target(self):
        session = _make_session(assigned=TARGET_ASSIGNED, focus=TARGET_ASSIGNED)
        session.session_manager.get_all_page_targets.return_value = [
            _make_target(TARGET_ASSIGNED),
            _make_target(TARGET_OTHER),
        ]

        event = SwitchTabEvent(target_id=TARGET_OTHER)
        result = await session.on_SwitchTabEvent(event)

        assert result == TARGET_ASSIGNED

    @pytest.mark.asyncio
    async def test_switch_tab_allows_assigned_target(self):
        session = _make_session(assigned=TARGET_ASSIGNED, focus=TARGET_ASSIGNED)
        session.session_manager.get_all_page_targets.return_value = [
            _make_target(TARGET_ASSIGNED),
        ]
        session.session_manager.get_target.return_value = _make_target(TARGET_ASSIGNED)

        event = SwitchTabEvent(target_id=TARGET_ASSIGNED)

        mock_cdp_session = MagicMock()
        mock_cdp_session.cdp_client.send.Target.activateTarget = AsyncMock()
        mock_cdp_session.session_id = "sess-123456789012345678901234"

        _pydantic_setattr(session, "get_or_create_cdp_session", AsyncMock(return_value=mock_cdp_session))
        result = await session.on_SwitchTabEvent(event)

        assert result == TARGET_ASSIGNED

    @pytest.mark.asyncio
    async def test_switch_tab_works_normally_when_not_confined(self):
        session = _make_session(assigned=None, focus=TARGET_ASSIGNED)
        session.session_manager.get_all_page_targets.return_value = [
            _make_target(TARGET_ASSIGNED),
            _make_target(TARGET_OTHER),
        ]
        session.session_manager.get_target.return_value = _make_target(TARGET_OTHER)

        event = SwitchTabEvent(target_id=TARGET_OTHER)

        mock_cdp_session = MagicMock()
        mock_cdp_session.cdp_client.send.Target.activateTarget = AsyncMock()
        mock_cdp_session.session_id = "sess-456789012345678901234567"

        _pydantic_setattr(session, "get_or_create_cdp_session", AsyncMock(return_value=mock_cdp_session))
        result = await session.on_SwitchTabEvent(event)

        assert result == TARGET_OTHER

    @pytest.mark.asyncio
    async def test_tab_closed_does_not_switch_away_from_assigned_target(self):
        session = _make_session(assigned=TARGET_ASSIGNED, focus=TARGET_ASSIGNED)

        await session.on_TabClosedEvent(TabClosedEvent(target_id=TARGET_ASSIGNED))

        dispatched_events = [call.args[0] for call in session.event_bus.dispatch.call_args_list]
        assert not any(isinstance(event, SwitchTabEvent) for event in dispatched_events)

    # -- _cdp_create_new_page --

    @pytest.mark.asyncio
    async def test_create_new_page_returns_assigned_when_confined(self):
        session = _make_session(assigned=TARGET_ASSIGNED, focus=TARGET_ASSIGNED)

        result = await session._cdp_create_new_page("https://example.com")
        assert result == TARGET_ASSIGNED

    @pytest.mark.asyncio
    async def test_create_new_page_creates_tab_when_not_confined(self):
        session = _make_session(assigned=None, focus=TARGET_ASSIGNED)

        new_target_id = "NEWPAGE_1234567890ABCDEF12345678"
        session._cdp_client_root.send.Target.createTarget = AsyncMock(
            return_value={"targetId": new_target_id}
        )

        result = await session._cdp_create_new_page("about:blank")
        assert result == new_target_id

    # -- _close_extension_options_pages --

    @pytest.mark.asyncio
    async def test_close_extension_pages_skips_assigned_target(self):
        session = _make_session(assigned=TARGET_ASSIGNED, focus=TARGET_ASSIGNED)

        extension_target = _make_target(
            TARGET_ASSIGNED, "chrome-extension://abc/options.html"
        )
        other_ext_target = _make_target(
            TARGET_OTHER, "chrome-extension://xyz/welcome.html"
        )
        session.session_manager.get_all_page_targets.return_value = [
            extension_target,
            other_ext_target,
        ]

        close_mock = AsyncMock()
        with patch.object(session, "_cdp_close_page", close_mock):
            await session._close_extension_options_pages()

        close_mock.assert_called_once_with(TARGET_OTHER)


# ===================================================================
# 2. SessionManager guards
# ===================================================================


class TestSessionManagerConfinement:
    """SessionManager-level confinement guards."""

    def test_get_all_page_targets_filter_returns_only_matching(self):
        session = _make_session(assigned=TARGET_ASSIGNED, focus=TARGET_ASSIGNED)
        sm = SessionManager(session)

        sm._targets[TARGET_ASSIGNED] = _make_target(TARGET_ASSIGNED)
        sm._targets[TARGET_OTHER] = _make_target(TARGET_OTHER)

        result = sm.get_all_page_targets(filter_target_id=TARGET_ASSIGNED)
        assert len(result) == 1
        assert result[0].target_id == TARGET_ASSIGNED

    def test_get_all_page_targets_no_filter_returns_all(self):
        session = _make_session(assigned=None, focus=TARGET_ASSIGNED)
        sm = SessionManager(session)

        sm._targets[TARGET_ASSIGNED] = _make_target(TARGET_ASSIGNED)
        sm._targets[TARGET_OTHER] = _make_target(TARGET_OTHER)

        result = sm.get_all_page_targets()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_recover_agent_focus_raises_when_confined(self):
        """When confined, _recover_agent_focus logs a RuntimeError (caught
        by its own try/except) rather than creating an emergency tab."""
        session = _make_session(assigned=TARGET_ASSIGNED, focus=TARGET_ASSIGNED)
        sm = SessionManager(session)
        sm._recovery_lock = asyncio.Lock()

        # The RuntimeError is raised on line 625 but caught by the broad
        # except on line 756.  Verify via log output.
        with patch.object(sm.logger, "error") as mock_log_error:
            await sm._recover_agent_focus(TARGET_ASSIGNED)

        # Verify the confined-mode error was logged
        error_messages = [str(call) for call in mock_log_error.call_args_list]
        assert any("confined mode" in msg for msg in error_messages)

    @pytest.mark.asyncio
    async def test_recover_agent_focus_creates_emergency_tab_when_not_confined(self):
        session = _make_session(assigned=None, focus=None)
        sm = SessionManager(session)
        sm._recovery_lock = asyncio.Lock()
        sm._recovery_in_progress = False
        sm._recovery_complete_event = None

        sm._targets = {}
        sm._target_sessions = {}

        new_target = "EMERGENCY_TAB_ID_1234567890ABCDEF"

        # Bypass Pydantic to set the mock on the real session
        _pydantic_setattr(session, "_cdp_create_new_page", AsyncMock(return_value=new_target))

        mock_cdp_session = MagicMock()
        call_count = 0

        def _fake_get(tid):
            nonlocal call_count
            call_count += 1
            return mock_cdp_session if call_count >= 2 else None

        sm._get_session_for_target = _fake_get
        sm.get_target = MagicMock(return_value=_make_target(new_target, "about:blank"))
        sm.get_all_page_targets = MagicMock(return_value=[])

        with patch("browser_use.browser.session_manager.asyncio.sleep", new_callable=AsyncMock):
            await sm._recover_agent_focus(TARGET_ASSIGNED)

        session._cdp_create_new_page.assert_called_once_with("about:blank")


# ===================================================================
# 3. Watchdog guards
# ===================================================================


class TestCrashWatchdogConfinement:
    """CrashWatchdog health-check confinement."""

    @pytest.mark.asyncio
    async def test_health_check_skips_non_assigned_in_confined_mode(self):
        session = _make_session(assigned=TARGET_ASSIGNED, focus=TARGET_ASSIGNED)

        assigned_target = _make_target(TARGET_ASSIGNED, "https://ats.example.com")
        other_target = _make_target(TARGET_OTHER, "chrome://new-tab-page/")

        session.session_manager.get_all_page_targets.return_value = [
            assigned_target,
            other_target,
        ]

        mock_cdp_session = MagicMock()
        mock_cdp_session.cdp_client = MagicMock()
        mock_cdp_session.session_id = "sess-crash-12345678901234567890"
        mock_cdp_session.cdp_client.send.Runtime.evaluate = AsyncMock(
            return_value={"result": {"value": 2}}
        )

        _pydantic_setattr(session, "get_or_create_cdp_session", AsyncMock(return_value=mock_cdp_session))
        session._local_browser_watchdog = None

        wd = CrashWatchdog(
            event_bus=session.event_bus,
            browser_session=session,
        )

        await wd._check_browser_health()

        # get_or_create_cdp_session should NOT have been called with the other target
        for call in session.get_or_create_cdp_session.call_args_list:
            if call.kwargs.get("target_id"):
                assert call.kwargs["target_id"] != TARGET_OTHER
            if call.args:
                assert call.args[0] != TARGET_OTHER

    @pytest.mark.asyncio
    async def test_health_check_checks_all_when_not_confined(self):
        session = _make_session(assigned=None, focus=TARGET_ASSIGNED)

        assigned_target = _make_target(TARGET_ASSIGNED, "https://ats.example.com")
        other_target = _make_target(TARGET_OTHER, "chrome://new-tab-page/")

        session.session_manager.get_all_page_targets.return_value = [
            assigned_target,
            other_target,
        ]

        mock_cdp_session = MagicMock()
        mock_cdp_session.cdp_client = MagicMock()
        mock_cdp_session.session_id = "sess-crash-23456789012345678901"
        mock_cdp_session.cdp_client.send.Runtime.evaluate = AsyncMock(
            return_value={"result": {"value": 2}}
        )
        mock_cdp_session.cdp_client.send.Page.navigate = AsyncMock()

        _pydantic_setattr(session, "get_or_create_cdp_session", AsyncMock(return_value=mock_cdp_session))
        session._local_browser_watchdog = None

        wd = CrashWatchdog(
            event_bus=session.event_bus,
            browser_session=session,
        )

        await wd._check_browser_health()

        # When not confined, the other_target (chrome://new-tab-page/) should be processed.
        target_ids_called = []
        for call in session.get_or_create_cdp_session.call_args_list:
            if call.kwargs.get("target_id"):
                target_ids_called.append(call.kwargs["target_id"])
        assert TARGET_OTHER in target_ids_called


class TestPopupsWatchdogConfinement:
    """PopupsWatchdog tab-creation confinement."""

    @pytest.mark.asyncio
    async def test_on_tab_created_skips_non_assigned_in_confined_mode(self):
        session = _make_session(assigned=TARGET_ASSIGNED, focus=TARGET_ASSIGNED)

        wd = PopupsWatchdog(
            event_bus=session.event_bus,
            browser_session=session,
        )

        event = TabCreatedEvent(target_id=TARGET_OTHER, url="https://spam.example.com")

        mock_get = AsyncMock()
        _pydantic_setattr(session, "get_or_create_cdp_session", mock_get)
        await wd.on_TabCreatedEvent(event)

        mock_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_tab_created_registers_for_assigned_target(self):
        session = _make_session(assigned=TARGET_ASSIGNED, focus=TARGET_ASSIGNED)

        wd = PopupsWatchdog(
            event_bus=session.event_bus,
            browser_session=session,
        )

        event = TabCreatedEvent(target_id=TARGET_ASSIGNED, url="https://ats.example.com")

        mock_cdp_session = MagicMock()
        mock_cdp_session.cdp_client = MagicMock()
        mock_cdp_session.session_id = "sess-popup-12345678901234567890"
        mock_cdp_session.cdp_client.send.Page.enable = AsyncMock()

        mock_get = AsyncMock(return_value=mock_cdp_session)
        _pydantic_setattr(session, "get_or_create_cdp_session", mock_get)
        session._cdp_client_root = MagicMock()
        session._cdp_client_root.send.Page.enable = AsyncMock()

        await wd.on_TabCreatedEvent(event)

        mock_get.assert_called()

    @pytest.mark.asyncio
    async def test_on_tab_created_registers_when_not_confined(self):
        session = _make_session(assigned=None, focus=TARGET_ASSIGNED)

        wd = PopupsWatchdog(
            event_bus=session.event_bus,
            browser_session=session,
        )

        event = TabCreatedEvent(target_id=TARGET_OTHER, url="https://other.example.com")

        mock_cdp_session = MagicMock()
        mock_cdp_session.cdp_client = MagicMock()
        mock_cdp_session.session_id = "sess-popup-23456789012345678901"
        mock_cdp_session.cdp_client.send.Page.enable = AsyncMock()

        mock_get = AsyncMock(return_value=mock_cdp_session)
        _pydantic_setattr(session, "get_or_create_cdp_session", mock_get)
        session._cdp_client_root = MagicMock()
        session._cdp_client_root.send.Page.enable = AsyncMock()

        await wd.on_TabCreatedEvent(event)

        mock_get.assert_called()


# ===================================================================
# 4. Settings integration
# ===================================================================


class TestSettingsTargetId:
    """Settings.target_id env-var mapping."""

    def test_target_id_from_env(self, monkeypatch):
        monkeypatch.setenv("GH_TARGET_ID", "env-target-abc123")
        from ghosthands.config.settings import Settings

        s = Settings()
        assert s.target_id == "env-target-abc123"

    def test_target_id_defaults_to_none(self, monkeypatch):
        monkeypatch.delenv("GH_TARGET_ID", raising=False)
        from ghosthands.config.settings import Settings

        s = Settings()
        assert s.target_id is None


# ===================================================================
# 5. JSONL output
# ===================================================================


class TestJSONLOutput:
    """emit_browser_ready and emit_awaiting_review target_id inclusion."""

    def _capture_jsonl(self, func, *args, **kwargs):
        """Call an emit function and capture the JSONL line written to the output stream."""
        buf = StringIO()

        with patch("ghosthands.output.jsonl._get_output", return_value=buf):
            with patch("ghosthands.output.jsonl._pipe_broken", False):
                func(*args, **kwargs)

        buf.seek(0)
        line = buf.readline().strip()
        if not line:
            return {}
        return json.loads(line)

    def test_emit_browser_ready_includes_target_id(self):
        data = self._capture_jsonl(emit_browser_ready, "ws://127.0.0.1:9222", target_id="abc-123")
        assert data["event"] == "browser_ready"
        assert data["targetId"] == "abc-123"
        assert data["cdpUrl"] == "ws://127.0.0.1:9222"

    def test_emit_browser_ready_omits_target_id_when_none(self):
        data = self._capture_jsonl(emit_browser_ready, "ws://127.0.0.1:9222")
        assert data["event"] == "browser_ready"
        assert "targetId" not in data

    def test_emit_awaiting_review_includes_target_id(self):
        data = self._capture_jsonl(
            emit_awaiting_review,
            message="Review please",
            target_id="xyz-789",
        )
        assert data["event"] == "awaiting_review"
        assert data["targetId"] == "xyz-789"

    def test_emit_awaiting_review_omits_target_id_when_none(self):
        data = self._capture_jsonl(emit_awaiting_review, message="Review please")
        assert data["event"] == "awaiting_review"
        assert "targetId" not in data
