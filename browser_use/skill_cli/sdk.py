"""Browser-Use SDK client factory."""

from browser_use_sdk import BrowserUse

_client: BrowserUse | None = None


def get_sdk_client() -> BrowserUse:
	"""Get authenticated SDK client (singleton)."""
	global _client
	if _client is None:
		from browser_use.skill_cli.api_key import require_api_key

		api_key = require_api_key('Cloud API')
		_client = BrowserUse(api_key=api_key)
	return _client
