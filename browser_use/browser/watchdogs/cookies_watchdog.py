"""Cookies watchdog for direct cookie manipulation."""

from typing import Any, ClassVar

from bubus import BaseEvent
from cdp_use.cdp.network import Cookie

from browser_use.browser.events import (
	ClearCookiesEvent,
	GetCookiesEvent,
	SetCookieEvent,
)
from browser_use.browser.watchdog_base import BaseWatchdog


class CookiesWatchdog(BaseWatchdog):
	"""Handles direct cookie get/set/clear operations."""

	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [
		GetCookiesEvent,
		SetCookieEvent,
		ClearCookiesEvent,
	]
	EMITS: ClassVar[list[type[BaseEvent]]] = []

	async def on_GetCookiesEvent(self, event: GetCookiesEvent) -> list[dict[str, Any]]:
		"""Get browser cookies, optionally filtered by URL."""
		try:
			cookies = await self.browser_session._cdp_get_cookies()
			# Convert Cookie objects to dicts for the result
			cookie_list: list[dict[str, Any]] = []
			for c in cookies:
				cookie_dict: dict[str, Any] = {
					'name': c.get('name', ''),
					'value': c.get('value', ''),
					'domain': c.get('domain', ''),
					'path': c.get('path', '/'),
					'secure': c.get('secure', False),
					'httpOnly': c.get('httpOnly', False),
				}
				if 'sameSite' in c:
					cookie_dict['sameSite'] = c.get('sameSite')
				if 'expires' in c:
					cookie_dict['expires'] = c.get('expires')
				cookie_list.append(cookie_dict)

			# Filter by URL if provided
			if event.url:
				from urllib.parse import urlparse

				parsed = urlparse(event.url)
				domain = parsed.netloc
				# Filter cookies that match the domain
				cookie_list = [
					c for c in cookie_list
					if domain.endswith(str(c.get('domain', '')).lstrip('.'))
					or str(c.get('domain', '')).lstrip('.').endswith(domain)
				]

			return cookie_list
		except Exception as e:
			self.logger.error(f'[CookiesWatchdog] Failed to get cookies: {e}')
			return []

	async def on_SetCookieEvent(self, event: SetCookieEvent) -> bool:
		"""Set a browser cookie."""
		try:
			cookie_dict: dict[str, Any] = {
				'name': event.name,
				'value': event.value,
				'path': event.path,
				'secure': event.secure,
				'httpOnly': event.http_only,
			}

			if event.domain:
				cookie_dict['domain'] = event.domain
			if event.same_site:
				cookie_dict['sameSite'] = event.same_site
			if event.expires:
				cookie_dict['expires'] = event.expires

			# If no domain specified, we need to get current URL's domain
			if not event.domain:
				cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None)
				if cdp_session:
					# Get current URL to extract domain
					result = await cdp_session.cdp_client.send.Runtime.evaluate(
						params={'expression': 'window.location.hostname', 'returnByValue': True},
						session_id=cdp_session.session_id,
					)
					hostname = result.get('result', {}).get('value', '')
					if hostname:
						cookie_dict['domain'] = hostname

			# Convert dict to Cookie object
			cookie_obj = Cookie(**cookie_dict)
			await self.browser_session._cdp_set_cookies([cookie_obj])
			self.logger.debug(f'[CookiesWatchdog] Set cookie: {event.name}')
			return True
		except Exception as e:
			self.logger.error(f'[CookiesWatchdog] Failed to set cookie: {e}')
			return False

	async def on_ClearCookiesEvent(self, event: ClearCookiesEvent) -> None:
		"""Clear browser cookies."""
		try:
			if event.url:
				# Clear cookies only for specific URL
				# First get all cookies, then delete ones matching the URL
				cookies = await self.browser_session._cdp_get_cookies()
				from urllib.parse import urlparse

				parsed = urlparse(event.url)
				domain = parsed.netloc

				cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None)
				if cdp_session:
					for cookie in cookies:
						cookie_domain = str(cookie.get('domain', '')).lstrip('.')
						if domain.endswith(cookie_domain) or cookie_domain.endswith(domain):
							await cdp_session.cdp_client.send.Network.deleteCookies(
								params={
									'name': cookie.get('name', ''),
									'domain': cookie.get('domain'),
									'path': cookie.get('path', '/'),
								},
								session_id=cdp_session.session_id,
							)
			else:
				# Clear all cookies
				await self.browser_session._cdp_clear_cookies()

			self.logger.debug(f'[CookiesWatchdog] Cleared cookies' + (f' for {event.url}' if event.url else ''))
		except Exception as e:
			self.logger.error(f'[CookiesWatchdog] Failed to clear cookies: {e}')
