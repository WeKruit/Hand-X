"""Browser engine provider abstraction.

Exports:
	BrowserProvider: Abstract base class for browser engines.
	ChromiumProvider: Chromium/Chrome engine provider.
	ProviderRegistry: Engine name -> provider class lookup.
"""

from browser_use.browser.providers.base import BrowserProvider
from browser_use.browser.providers.chromium import ChromiumProvider
from browser_use.browser.providers.registry import ProviderRegistry

__all__ = ['BrowserProvider', 'ChromiumProvider', 'ProviderRegistry']
