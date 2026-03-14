"""Stealth watchdog -- injects anti-detection JS on browser connection."""

from typing import ClassVar

from bubus import BaseEvent

from browser_use.browser.events import BrowserConnectedEvent
from browser_use.browser.stealth.scripts import get_stealth_scripts
from browser_use.browser.watchdog_base import BaseWatchdog


class StealthWatchdog(BaseWatchdog):
	"""Injects anti-detection JavaScript into every page via CDP on browser connection.

	Listens for ``BrowserConnectedEvent`` and, when the stealth layer is enabled
	in the browser profile, calls ``_cdp_add_init_script`` for each enabled patch.
	Scripts are registered with ``Page.addScriptToEvaluateOnNewDocument`` so they
	survive page navigations and new iframe creation.
	"""

	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [BrowserConnectedEvent]
	EMITS: ClassVar[list[type[BaseEvent]]] = []

	async def on_BrowserConnectedEvent(self, event: BrowserConnectedEvent) -> None:
		"""Inject stealth scripts when the browser connects."""
		stealth_config = self.browser_session.browser_profile.stealth
		if not stealth_config.enabled:
			return

		scripts = get_stealth_scripts(stealth_config)
		if not scripts:
			return

		self.logger.debug(f'Injecting {len(scripts)} stealth script(s)')
		for script in scripts:
			await self.browser_session._cdp_add_init_script(script)
		self.logger.debug('Stealth scripts injected successfully')
