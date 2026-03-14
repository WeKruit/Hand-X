"""Provider registry for browser engine lookup."""

from __future__ import annotations

from browser_use.browser.providers.base import BrowserProvider
from browser_use.browser.providers.camoufox import CamoufoxProvider
from browser_use.browser.providers.chromium import ChromiumProvider


class ProviderRegistry:
	"""Maps engine names to provider classes.

	Usage:
		provider_cls = ProviderRegistry.get('chromium')
		provider = provider_cls()
		cdp_url, pid = await provider.launch(profile)

	New engines are registered via:
		ProviderRegistry.register('firefox', CamoufoxProvider)

	Tests should call ``ProviderRegistry.reset()`` in teardown to restore
	the built-in providers and prevent test pollution.
	"""

	_default_providers: dict[str, type[BrowserProvider]] = {
		'chromium': ChromiumProvider,
		'firefox': CamoufoxProvider,
	}
	_providers: dict[str, type[BrowserProvider]] = dict(_default_providers)

	@classmethod
	def register(cls, name: str, provider_class: type[BrowserProvider]) -> None:
		"""Register a browser provider class under the given name.

		Args:
			name: Engine name (e.g., 'chromium', 'firefox').
			provider_class: The provider class to register.
		"""
		cls._providers[name] = provider_class

	@classmethod
	def get(cls, name: str) -> type[BrowserProvider]:
		"""Look up a registered provider class by engine name.

		Args:
			name: Engine name to look up.

		Returns:
			The registered provider class.

		Raises:
			NotImplementedError: If no provider is registered for the given name.
		"""
		if name not in cls._providers:
			raise NotImplementedError(f"Browser provider '{name}' not registered")
		return cls._providers[name]

	@classmethod
	def available(cls) -> list[str]:
		"""Return list of registered engine names."""
		return list(cls._providers.keys())

	@classmethod
	def reset(cls) -> None:
		"""Reset the registry to built-in providers only.

		Call this in test teardown to prevent test pollution from
		custom provider registrations leaking across test cases.
		"""
		cls._providers = dict(cls._default_providers)
