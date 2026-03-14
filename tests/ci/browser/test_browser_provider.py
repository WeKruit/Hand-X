"""Tests for BrowserProvider abstraction (Stream 5).

Validates:
- ProviderRegistry correctly registers and retrieves providers
- ChromiumProvider properties and interface
- BrowserProfile engine field accepts valid values
- No regressions in existing BrowserProfile behavior
"""

import tempfile

import pytest

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.providers import BrowserProvider, ChromiumProvider, ProviderRegistry


class TestProviderRegistry:
	"""Tests for ProviderRegistry lookup and registration."""

	def test_get_chromium_returns_chromium_provider(self):
		"""ProviderRegistry.get('chromium') must return ChromiumProvider."""
		provider_cls = ProviderRegistry.get('chromium')
		assert provider_cls is ChromiumProvider

	def test_get_firefox_returns_camoufox_provider(self):
		"""ProviderRegistry.get('firefox') must return CamoufoxProvider (registered in S6)."""
		from browser_use.browser.providers.camoufox import CamoufoxProvider

		provider_cls = ProviderRegistry.get('firefox')
		assert provider_cls is CamoufoxProvider

	def test_get_unknown_raises_not_implemented(self):
		"""ProviderRegistry.get with an unknown name raises NotImplementedError."""
		with pytest.raises(NotImplementedError, match="Browser provider 'unknown_engine' not registered"):
			ProviderRegistry.get('unknown_engine')

	def test_register_and_retrieve_custom_provider(self):
		"""Custom providers can be registered and retrieved."""

		class FakeProvider(BrowserProvider):
			@property
			def engine_name(self) -> str:
				return 'fake'

			@property
			def supports_cdp(self) -> bool:
				return False

			async def launch(self, profile):
				return ('ws://fake:1234', None)

			async def kill(self):
				pass

			def get_default_args(self, profile):
				return []

		ProviderRegistry.register('fake', FakeProvider)
		try:
			assert ProviderRegistry.get('fake') is FakeProvider
		finally:
			# Clean up to avoid polluting other tests
			ProviderRegistry.reset()

	def test_available_includes_chromium(self):
		"""ProviderRegistry.available() includes 'chromium'."""
		available = ProviderRegistry.available()
		assert 'chromium' in available


class TestChromiumProvider:
	"""Tests for ChromiumProvider properties and interface."""

	def test_engine_name_is_chromium(self):
		"""ChromiumProvider.engine_name must be 'chromium'."""
		provider = ChromiumProvider()
		assert provider.engine_name == 'chromium'

	def test_supports_cdp_is_true(self):
		"""ChromiumProvider.supports_cdp must be True."""
		provider = ChromiumProvider()
		assert provider.supports_cdp is True

	def test_get_default_args_returns_list(self):
		"""ChromiumProvider.get_default_args() must return a list of strings."""
		provider = ChromiumProvider()
		# get_args() requires user_data_dir to be set
		tmp_dir = tempfile.mkdtemp(prefix='browseruse-test-')
		profile = BrowserProfile(user_data_dir=tmp_dir)
		args = provider.get_default_args(profile)
		assert isinstance(args, list)
		assert len(args) > 0
		assert all(isinstance(arg, str) for arg in args)

	def test_get_default_args_contains_user_data_dir(self):
		"""get_default_args() must include --user-data-dir (required for CDP attach)."""
		provider = ChromiumProvider()
		tmp_dir = tempfile.mkdtemp(prefix='browseruse-test-')
		profile = BrowserProfile(user_data_dir=tmp_dir)
		args = provider.get_default_args(profile)
		assert any(arg.startswith('--user-data-dir=') for arg in args)

	def test_is_subclass_of_browser_provider(self):
		"""ChromiumProvider must be a subclass of BrowserProvider."""
		assert issubclass(ChromiumProvider, BrowserProvider)

	def test_instance_starts_with_no_process(self):
		"""A fresh ChromiumProvider instance has no process."""
		provider = ChromiumProvider()
		assert provider._process is None
		assert provider._temp_dirs == []


class TestBrowserProfileEngine:
	"""Tests for BrowserProfile.engine field."""

	def test_default_engine_is_chromium(self):
		"""BrowserProfile default engine is 'chromium'."""
		profile = BrowserProfile()
		assert profile.engine == 'chromium'

	def test_engine_chromium_creates_valid_profile(self):
		"""BrowserProfile(engine='chromium') creates a valid profile."""
		profile = BrowserProfile(engine='chromium')
		assert profile.engine == 'chromium'

	def test_engine_auto_creates_valid_profile(self):
		"""BrowserProfile(engine='auto') creates a valid profile."""
		profile = BrowserProfile(engine='auto')
		assert profile.engine == 'auto'

	def test_engine_firefox_creates_valid_profile(self):
		"""BrowserProfile(engine='firefox') creates a valid profile."""
		profile = BrowserProfile(engine='firefox')
		assert profile.engine == 'firefox'

	def test_engine_invalid_value_raises(self):
		"""BrowserProfile with invalid engine value raises ValidationError."""
		with pytest.raises(Exception):
			BrowserProfile(engine='safari')  # type: ignore[arg-type]

	def test_engine_does_not_affect_get_args(self):
		"""Setting engine does not change the output of get_args() (no behavior change yet)."""
		tmp_dir = tempfile.mkdtemp(prefix='browseruse-test-')
		profile_default = BrowserProfile(user_data_dir=tmp_dir)
		profile_chromium = BrowserProfile(engine='chromium', user_data_dir=tmp_dir)
		# Both should produce the same args since engine doesn't affect args yet
		assert profile_default.get_args() == profile_chromium.get_args()
