"""Tests for the stealth JS injection anti-detection layer.

Validates that:
- Stealth scripts are syntactically valid JavaScript
- ``get_stealth_scripts()`` returns the correct scripts based on config
- Individual patches can be toggled on/off
- ``StealthConfig(enabled=False)`` produces no scripts
- ``StealthConfig`` is properly wired into ``BrowserProfile``
"""

import re

import pytest

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.stealth.config import StealthConfig
from browser_use.browser.stealth.scripts import (
	CHROME_RUNTIME_PATCH,
	IFRAME_CONTENTWINDOW_PATCH,
	LANGUAGES_PATCH,
	MEDIA_CODECS_PATCH,
	PERMISSIONS_PATCH,
	PLUGINS_PATCH,
	WEBDRIVER_PATCH,
	WEBGL_PATCH,
	_PATCHES,
	get_stealth_scripts,
)


# ---------------------------------------------------------------------------
# Config unit tests
# ---------------------------------------------------------------------------


class TestStealthConfig:
	"""Tests for StealthConfig pydantic model."""

	def test_default_disabled(self):
		"""StealthConfig defaults to enabled=False for safety."""
		cfg = StealthConfig()
		assert cfg.enabled is False

	def test_all_patches_default_true_when_enabled(self):
		"""When enabled=True, every individual patch flag should default to True (except disabled no-ops)."""
		cfg = StealthConfig(enabled=True)
		disabled_by_default = {'iframe_contentwindow_patch'}  # no-op, needs implementation
		for flag, _ in _PATCHES:
			if flag in disabled_by_default:
				assert getattr(cfg, flag) is False, f'{flag} should default to False (no-op)'
			else:
				assert getattr(cfg, flag) is True, f'{flag} should default to True'

	def test_individual_patch_disable(self):
		"""Individual patches can be disabled."""
		cfg = StealthConfig(enabled=True, webdriver_patch=False, webgl_patch=False)
		assert cfg.webdriver_patch is False
		assert cfg.webgl_patch is False
		# Others remain True
		assert cfg.chrome_runtime_patch is True
		assert cfg.plugins_patch is True


# ---------------------------------------------------------------------------
# Script selection tests
# ---------------------------------------------------------------------------


class TestGetStealthScripts:
	"""Tests for get_stealth_scripts() function."""

	def test_disabled_returns_empty(self):
		"""When enabled=False, no scripts should be returned."""
		cfg = StealthConfig(enabled=False)
		scripts = get_stealth_scripts(cfg)
		assert scripts == []

	def test_enabled_returns_active_by_default(self):
		"""When enabled=True with defaults, all active scripts are returned."""
		cfg = StealthConfig(enabled=True)
		scripts = get_stealth_scripts(cfg)
		# iframe_contentwindow_patch is disabled by default (no-op)
		active_patches = [(f, s) for f, s in _PATCHES if getattr(cfg, f)]
		assert len(scripts) == len(active_patches)

	def test_disabling_one_patch_reduces_count(self):
		"""Disabling a single patch should return one fewer script."""
		cfg_base = StealthConfig(enabled=True)
		base_count = len(get_stealth_scripts(cfg_base))
		cfg = StealthConfig(enabled=True, webdriver_patch=False)
		scripts = get_stealth_scripts(cfg)
		assert len(scripts) == base_count - 1
		assert WEBDRIVER_PATCH not in scripts

	def test_disabling_all_patches_returns_empty(self):
		"""Disabling every patch flag should return an empty list even when enabled."""
		kwargs = {flag: False for flag, _ in _PATCHES}
		cfg = StealthConfig(enabled=True, **kwargs)
		scripts = get_stealth_scripts(cfg)
		assert scripts == []

	def test_specific_patches_included(self):
		"""Verify that enabling only specific patches returns exactly those scripts."""
		cfg = StealthConfig(
			enabled=True,
			webdriver_patch=True,
			chrome_runtime_patch=False,
			plugins_patch=False,
			languages_patch=False,
			permissions_patch=False,
			webgl_patch=True,
			iframe_contentwindow_patch=False,
			media_codecs_patch=False,
		)
		scripts = get_stealth_scripts(cfg)
		assert len(scripts) == 2
		assert WEBDRIVER_PATCH in scripts
		assert WEBGL_PATCH in scripts

	def test_order_preserved(self):
		"""Scripts should be returned in the same order as _PATCHES (active only)."""
		cfg = StealthConfig(enabled=True)
		scripts = get_stealth_scripts(cfg)
		expected = [script for flag, script in _PATCHES if getattr(cfg, flag)]
		assert scripts == expected


# ---------------------------------------------------------------------------
# JS syntax validation
# ---------------------------------------------------------------------------


class TestStealthScriptsSyntax:
	"""Validate that each stealth script is syntactically valid JavaScript."""

	ALL_SCRIPTS = [
		('WEBDRIVER_PATCH', WEBDRIVER_PATCH),
		('CHROME_RUNTIME_PATCH', CHROME_RUNTIME_PATCH),
		('PLUGINS_PATCH', PLUGINS_PATCH),
		('LANGUAGES_PATCH', LANGUAGES_PATCH),
		('PERMISSIONS_PATCH', PERMISSIONS_PATCH),
		('WEBGL_PATCH', WEBGL_PATCH),
		('IFRAME_CONTENTWINDOW_PATCH', IFRAME_CONTENTWINDOW_PATCH),
		('MEDIA_CODECS_PATCH', MEDIA_CODECS_PATCH),
	]

	@pytest.mark.parametrize('name,script', ALL_SCRIPTS, ids=[n for n, _ in ALL_SCRIPTS])
	def test_script_is_valid_js(self, name: str, script: str):
		"""Each stealth script must be a well-formed JS snippet.

		Performs Python-based structural checks (no Node.js dependency):
		- Non-empty content
		- Balanced braces, parens, and brackets
		- No obvious syntax errors (stray tokens, empty blocks)
		"""
		assert script.strip(), f'{name} is empty'
		# Check balanced braces/parens/brackets
		assert script.count('(') == script.count(')'), f'{name} has unbalanced parentheses'
		assert script.count('{') == script.count('}'), f'{name} has unbalanced braces'
		assert script.count('[') == script.count(']'), f'{name} has unbalanced brackets'
		# No stray triple-quote (Python leaking) or obvious non-JS tokens
		assert '"""' not in script, f'{name} contains Python triple-quotes'
		assert "'''" not in script, f'{name} contains Python triple-quotes'
		# Should contain at least one JS keyword or expression
		js_keywords = re.compile(r'\b(function|const|let|var|return|if|Object|get|set|value)\b')
		assert js_keywords.search(script), f'{name} does not look like JavaScript'

	def test_all_scripts_are_nonempty_strings(self):
		"""Every script constant must be a non-empty string."""
		for name, script in self.ALL_SCRIPTS:
			assert isinstance(script, str), f'{name} is not a string'
			assert len(script.strip()) > 0, f'{name} is empty'

	def test_scripts_are_iife(self):
		"""Each script should be wrapped in an IIFE to avoid polluting global scope."""
		for name, script in self.ALL_SCRIPTS:
			stripped = script.strip()
			assert stripped.startswith('('), f'{name} does not start with IIFE pattern'
			assert stripped.endswith('})();') or stripped.endswith('})();\n'), (
				f'{name} does not end with IIFE closing pattern'
			)


# ---------------------------------------------------------------------------
# BrowserProfile integration
# ---------------------------------------------------------------------------


class TestBrowserProfileStealthIntegration:
	"""Test that StealthConfig is properly wired into BrowserProfile."""

	def test_browser_profile_has_stealth_field(self):
		"""BrowserProfile must expose a stealth field."""
		profile = BrowserProfile(headless=True, user_data_dir=None)
		assert hasattr(profile, 'stealth')
		assert isinstance(profile.stealth, StealthConfig)

	def test_browser_profile_stealth_default_disabled(self):
		"""BrowserProfile.stealth defaults to disabled."""
		profile = BrowserProfile(headless=True, user_data_dir=None)
		assert profile.stealth.enabled is False

	def test_browser_profile_stealth_enabled(self):
		"""BrowserProfile can be created with stealth enabled."""
		profile = BrowserProfile(
			headless=True,
			user_data_dir=None,
			stealth=StealthConfig(enabled=True),
		)
		assert profile.stealth.enabled is True

	def test_browser_profile_stealth_custom_patches(self):
		"""BrowserProfile can be created with custom stealth patch flags."""
		profile = BrowserProfile(
			headless=True,
			user_data_dir=None,
			stealth=StealthConfig(enabled=True, webgl_patch=False),
		)
		assert profile.stealth.enabled is True
		assert profile.stealth.webgl_patch is False
		assert profile.stealth.webdriver_patch is True


# ---------------------------------------------------------------------------
# Watchdog registration test
# ---------------------------------------------------------------------------


class TestStealthWatchdogRegistration:
	"""Test that StealthWatchdog can be instantiated and has the correct event contracts."""

	def test_watchdog_listens_to_browser_connected(self):
		"""StealthWatchdog must declare BrowserConnectedEvent in LISTENS_TO."""
		from browser_use.browser.events import BrowserConnectedEvent
		from browser_use.browser.watchdogs.stealth_watchdog import StealthWatchdog

		assert BrowserConnectedEvent in StealthWatchdog.LISTENS_TO

	def test_watchdog_instantiation(self):
		"""StealthWatchdog can be instantiated with a BrowserSession."""
		from bubus import EventBus

		from browser_use.browser.session import BrowserSession
		from browser_use.browser.watchdogs.stealth_watchdog import StealthWatchdog

		profile = BrowserProfile(headless=True, user_data_dir=None, stealth=StealthConfig(enabled=True))
		session = BrowserSession(browser_profile=profile)
		bus = EventBus()
		watchdog = StealthWatchdog(event_bus=bus, browser_session=session)
		assert watchdog is not None
		assert watchdog.browser_session is session

	def test_watchdog_has_handler_method(self):
		"""StealthWatchdog must have an on_BrowserConnectedEvent handler."""
		from browser_use.browser.watchdogs.stealth_watchdog import StealthWatchdog

		assert hasattr(StealthWatchdog, 'on_BrowserConnectedEvent')
		assert callable(getattr(StealthWatchdog, 'on_BrowserConnectedEvent'))


class TestStealthFirefoxGuard:
	"""Tests for Firefox/Camoufox stealth interaction (M-6)."""

	def test_firefox_stealth_scripts_available(self):
		"""get_firefox_stealth_scripts() returns non-empty list when enabled."""
		from browser_use.browser.stealth.firefox_scripts import get_firefox_stealth_scripts

		scripts = get_firefox_stealth_scripts(enabled=True)
		assert len(scripts) >= 2, 'Expected at least Permissions + MediaCodecs patches'
		for script in scripts:
			assert isinstance(script, str)
			assert len(script) > 10

	def test_firefox_stealth_scripts_disabled(self):
		"""get_firefox_stealth_scripts(enabled=False) returns empty list."""
		from browser_use.browser.stealth.firefox_scripts import get_firefox_stealth_scripts

		assert get_firefox_stealth_scripts(enabled=False) == []

	def test_is_firefox_engine_detection(self):
		"""is_firefox_engine identifies Firefox and Camoufox engines."""
		from browser_use.browser.stealth.firefox_scripts import is_firefox_engine

		assert is_firefox_engine('firefox') is True
		assert is_firefox_engine('camoufox') is True
		assert is_firefox_engine('Firefox') is True
		assert is_firefox_engine('chromium') is False
		assert is_firefox_engine('chrome') is False

	def test_iframe_contentwindow_patch_disabled_by_default(self):
		"""IFRAME_CONTENTWINDOW_PATCH defaults to disabled (no-op guard)."""
		config = StealthConfig(enabled=True)
		assert config.iframe_contentwindow_patch is False
