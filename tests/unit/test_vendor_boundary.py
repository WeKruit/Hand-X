from __future__ import annotations

from pathlib import Path

from bubus import EventBus

from browser_use.browser.watchdogs.downloads_watchdog import DownloadsWatchdog
from browser_use.tools.registry.views import SpecialActionParameters
from ghosthands.browser.session import BrowserSession as HandXBrowserSession


def test_browser_use_tree_has_no_ghosthands_runtime_coupling():
    browser_use_root = Path(__file__).resolve().parents[2] / "browser_use"
    checked_suffixes = {".py", ".md"}

    for path in browser_use_root.rglob("*"):
        if not path.is_file() or path.suffix not in checked_suffixes:
            continue
        source = path.read_text(encoding="utf-8")
        assert "ghosthands" not in source, f"ghosthands reference leaked into vendored file: {path}"
        assert "_gh_last_application_state" not in source, f"Hand-X runtime state leaked into vendored file: {path}"


def test_vendor_models_accept_handx_browser_session_adapter():
    session = HandXBrowserSession(headless=True)

    watchdog = DownloadsWatchdog(event_bus=EventBus(), browser_session=session)
    assert watchdog.browser_session is session

    special_params = SpecialActionParameters(browser_session=session)
    assert special_params.browser_session is session
