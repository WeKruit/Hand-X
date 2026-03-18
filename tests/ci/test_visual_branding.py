"""Regression tests for VALET loading branding and Hand-X interaction visuals."""

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.watchdogs.aboutblank_watchdog import _build_loading_overlay_script
from browser_use.tools.service import Tools
from ghosthands.actions import register_domhand_actions
from ghosthands.actions._highlight import DEFAULT_COLOR as DOMHAND_HIGHLIGHT_COLOR
from ghosthands.visuals.cursor import _INJECT_CURSOR_JS
from ghosthands.visuals.patch import _INJECT_CURSOR_EXPR


def test_handx_highlight_defaults_are_blue():
    """Browser-use and DomHand highlights should share the Hand-X blue theme."""
    profile = BrowserProfile(headless=True)

    assert profile.interaction_highlight_color == "rgb(37, 99, 235)"
    assert DOMHAND_HIGHLIGHT_COLOR == "rgb(37, 99, 235)"


def test_aboutblank_logo_is_enabled_by_default_and_uses_handx_asset():
    """The loading overlay should use the VALET branded logo and copy by default."""
    profile = BrowserProfile(headless=True)

    assert profile.aboutblank_loading_logo_enabled is True
    assert profile.aboutblank_loading_min_display_seconds == 2.0

    script_without_logo = _build_loading_overlay_script("test-session", show_logo=False)
    script_with_logo = _build_loading_overlay_script("test-session", show_logo=True)

    assert "VALET" in script_without_logo
    assert "WeKruit - VALET" in script_without_logo
    assert "https://cf.browser-use.com/logo.svg" not in script_without_logo
    assert "https://cf.browser-use.com/logo.svg" not in script_with_logo
    assert "Hand-X" not in script_with_logo
    assert "data:image/svg+xml" in script_with_logo
    assert "Loading secure browser session..." in script_with_logo


def test_cursor_visuals_use_handx_palette():
    """The injected cursor visuals should use the Hand-X palette, not orange defaults."""
    assert "#2563eb" in _INJECT_CURSOR_JS
    assert "#2563eb" in _INJECT_CURSOR_EXPR
    assert "rgba(96, 165, 250, 0.72)" in _INJECT_CURSOR_JS
    assert "rgba(96, 165, 250, 0.72)" in _INJECT_CURSOR_EXPR


def test_register_domhand_actions_keeps_assess_state_and_popup_tools_available():
    """A single action registration problem must not disable the whole DomHand bundle."""
    tools = Tools()

    register_domhand_actions(tools)

    registered = set(tools.registry.registry.actions)
    assert "domhand_fill" in registered
    assert "domhand_interact_control" in registered
    assert "domhand_assess_state" in registered
    assert "domhand_close_popup" in registered
