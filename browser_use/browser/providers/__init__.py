"""Browser engine provider abstraction.

Exports:
	BrowserProvider: Abstract base class for browser engines.
	CamoufoxProvider: Camoufox (anti-detect Firefox) engine provider.
	ChromiumProvider: Chromium/Chrome engine provider.
	ProviderRegistry: Engine name -> provider class lookup.
	RouteSelector: URL-based engine selection.
"""

from browser_use.browser.providers.base import BrowserProvider
from browser_use.browser.providers.camoufox import CamoufoxProvider
from browser_use.browser.providers.chromium import ChromiumProvider
from browser_use.browser.providers.registry import ProviderRegistry
from browser_use.browser.providers.route_selector import RouteSelector

__all__ = ['BrowserProvider', 'CamoufoxProvider', 'ChromiumProvider', 'ProviderRegistry', 'RouteSelector']
