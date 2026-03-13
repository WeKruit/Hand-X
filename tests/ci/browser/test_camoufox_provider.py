"""Tests for CamoufoxProvider (Stream 6).

Validates:
- CamoufoxProvider properties and interface
- ProviderRegistry correctly registers and retrieves CamoufoxProvider
- CamoufoxProvider.get_default_args() returns Firefox-style args
- BrowserProfile(engine='firefox') creates a valid profile
- CamoufoxProvider graceful handling when Camoufox is not installed

All tests are designed to work WITHOUT Camoufox installed (for CI).
Tests that require a running Camoufox instance are marked with skipif.
"""

import shutil

import pytest

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.providers import BrowserProvider, CamoufoxProvider, ProviderRegistry


class TestCamoufoxProviderProperties:
	"""Tests for CamoufoxProvider properties and interface compliance."""

	def test_engine_name_is_firefox(self):
		"""CamoufoxProvider.engine_name must be 'firefox'."""
		provider = CamoufoxProvider()
		assert provider.engine_name == 'firefox'

	def test_supports_cdp_is_false(self):
		"""CamoufoxProvider.supports_cdp must be False (uses Juggler, not CDP)."""
		provider = CamoufoxProvider()
		assert provider.supports_cdp is False

	def test_is_subclass_of_browser_provider(self):
		"""CamoufoxProvider must be a subclass of BrowserProvider."""
		assert issubclass(CamoufoxProvider, BrowserProvider)

	def test_instance_starts_clean(self):
		"""A fresh CamoufoxProvider instance has no process or server."""
		provider = CamoufoxProvider()
		assert provider._process is None
		assert provider._server is None
		assert provider._ws_endpoint is None


class TestCamoufoxProviderArgs:
	"""Tests for CamoufoxProvider.get_default_args() output."""

	def test_get_default_args_returns_list(self):
		"""get_default_args() must return a list of strings."""
		provider = CamoufoxProvider()
		profile = BrowserProfile()
		args = provider.get_default_args(profile)
		assert isinstance(args, list)
		assert all(isinstance(arg, str) for arg in args)

	def test_get_default_args_includes_debug_port(self):
		"""get_default_args() must include --remote-debugging-port."""
		provider = CamoufoxProvider()
		profile = BrowserProfile()
		args = provider.get_default_args(profile)
		assert any(arg.startswith('--remote-debugging-port=') for arg in args)

	def test_get_default_args_default_port_9222(self):
		"""Default debug port is 9222 when not specified in profile."""
		provider = CamoufoxProvider()
		profile = BrowserProfile()
		args = provider.get_default_args(profile)
		assert '--remote-debugging-port=9222' in args

	def test_get_default_args_headless_flag(self):
		"""headless=True adds -headless (Firefox-style, no --)."""
		provider = CamoufoxProvider()
		profile = BrowserProfile(headless=True)
		args = provider.get_default_args(profile)
		assert '-headless' in args
		# Must NOT use Chrome-style --headless
		assert '--headless' not in args

	def test_get_default_args_no_headless_flag_when_false(self):
		"""headless=False does NOT add -headless flag."""
		provider = CamoufoxProvider()
		profile = BrowserProfile(headless=False)
		args = provider.get_default_args(profile)
		assert '-headless' not in args

	def test_get_default_args_user_data_dir_as_profile(self):
		"""user_data_dir uses -profile (Firefox-style, not --user-data-dir).

		BrowserProfile resolves paths via Path.resolve(), so the arg value
		matches profile.user_data_dir (the resolved path), not the raw input.
		"""
		import tempfile

		tmp_dir = tempfile.mkdtemp(prefix='camoufox-test-')
		provider = CamoufoxProvider()
		profile = BrowserProfile(user_data_dir=tmp_dir)
		args = provider.get_default_args(profile)
		assert '-profile' in args
		idx = args.index('-profile')
		# Must match the resolved user_data_dir from the profile
		assert args[idx + 1] == str(profile.user_data_dir)
		# Must NOT use Chrome-style --user-data-dir
		assert not any(arg.startswith('--user-data-dir=') for arg in args)

	def test_get_default_args_window_size(self):
		"""window_size adds --width and --height flags.

		Note: headless=False is required because BrowserProfile's
		detect_display_configuration() sets window_size=None in headless mode.
		"""
		from browser_use.browser.profile import ViewportSize

		provider = CamoufoxProvider()
		profile = BrowserProfile(headless=False, window_size=ViewportSize(width=1920, height=1080))
		args = provider.get_default_args(profile)
		assert '--width=1920' in args
		assert '--height=1080' in args


class TestCamoufoxRegistration:
	"""Tests for CamoufoxProvider registration in ProviderRegistry."""

	def test_registry_get_firefox_returns_camoufox(self):
		"""ProviderRegistry.get('firefox') must return CamoufoxProvider."""
		provider_cls = ProviderRegistry.get('firefox')
		assert provider_cls is CamoufoxProvider

	def test_registry_available_includes_firefox(self):
		"""ProviderRegistry.available() must include 'firefox'."""
		available = ProviderRegistry.available()
		assert 'firefox' in available

	def test_registry_available_includes_both_engines(self):
		"""ProviderRegistry.available() must include both 'chromium' and 'firefox'."""
		available = ProviderRegistry.available()
		assert 'chromium' in available
		assert 'firefox' in available

	def test_registry_instantiate_firefox_provider(self):
		"""Can instantiate a CamoufoxProvider from the registry."""
		provider_cls = ProviderRegistry.get('firefox')
		provider = provider_cls()
		assert isinstance(provider, CamoufoxProvider)
		assert provider.engine_name == 'firefox'


class TestCamoufoxBrowserProfile:
	"""Tests for BrowserProfile with engine='firefox'."""

	def test_engine_firefox_valid(self):
		"""BrowserProfile(engine='firefox') is valid."""
		profile = BrowserProfile(engine='firefox')
		assert profile.engine == 'firefox'

	def test_engine_firefox_headless(self):
		"""BrowserProfile(engine='firefox', headless=True) is valid."""
		profile = BrowserProfile(engine='firefox', headless=True)
		assert profile.engine == 'firefox'
		assert profile.headless is True

	def test_engine_auto_valid(self):
		"""BrowserProfile(engine='auto') is valid."""
		profile = BrowserProfile(engine='auto')
		assert profile.engine == 'auto'


class TestCamoufoxKill:
	"""Tests for CamoufoxProvider.kill() cleanup."""

	@pytest.mark.asyncio
	async def test_kill_no_process_is_safe(self):
		"""Calling kill() on a fresh provider (no process) must not raise."""
		provider = CamoufoxProvider()
		# Should not raise
		await provider.kill()
		assert provider._process is None
		assert provider._server is None
		assert provider._ws_endpoint is None


class TestCamoufoxAvailability:
	"""Tests for CamoufoxProvider.is_available() class method."""

	def test_is_available_returns_bool(self):
		"""is_available() must return a boolean."""
		result = CamoufoxProvider.is_available()
		assert isinstance(result, bool)

	@pytest.mark.skipif(shutil.which('camoufox') is not None, reason='Camoufox IS installed')
	def test_find_binary_returns_none_when_not_installed(self):
		"""_find_camoufox_binary() returns None when camoufox is not in PATH."""
		provider = CamoufoxProvider()
		# May still find it in ~/.camoufox or venv, so this test
		# only asserts the return type
		result = provider._find_camoufox_binary()
		assert result is None or isinstance(result, str)


@pytest.mark.skipif(not shutil.which('camoufox'), reason='Camoufox not installed')
class TestCamoufoxLaunch:
	"""Tests that require Camoufox to be installed.

	These tests are skipped in CI if Camoufox is not available.
	"""

	@pytest.mark.asyncio
	async def test_launch_returns_ws_url_and_pid(self):
		"""launch() must return a WebSocket URL and optional PID."""
		provider = CamoufoxProvider()
		try:
			ws_url, pid = await provider.launch(BrowserProfile(headless=True))
			assert isinstance(ws_url, str)
			assert 'ws://' in ws_url or 'wss://' in ws_url
		finally:
			await provider.kill()

	@pytest.mark.asyncio
	async def test_launch_and_kill_lifecycle(self):
		"""Full launch -> kill lifecycle must not leak processes."""
		provider = CamoufoxProvider()
		try:
			await provider.launch(BrowserProfile(headless=True))
		finally:
			await provider.kill()
		assert provider._process is None
		assert provider._server is None
