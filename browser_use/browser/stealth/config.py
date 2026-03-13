"""Configuration model for browser stealth/anti-detection patches."""

from pydantic import BaseModel


class StealthConfig(BaseModel):
	"""Controls which anti-detection JS patches are injected into browser pages.

	Each flag corresponds to a specific stealth script that patches a browser API
	commonly used by bot-detection systems. Set ``enabled=True`` to activate the
	stealth layer, then toggle individual patches as needed.

	When ``enabled=False`` (the default), no scripts are injected regardless of
	the individual patch flags.
	"""

	enabled: bool = False  # Master switch -- default OFF for safety
	webdriver_patch: bool = True
	chrome_runtime_patch: bool = True
	plugins_patch: bool = True
	languages_patch: bool = True
	permissions_patch: bool = True
	webgl_patch: bool = True
	iframe_contentwindow_patch: bool = True
	media_codecs_patch: bool = True
