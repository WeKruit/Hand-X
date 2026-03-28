"""Security watchdog for enforcing URL access policies."""

from typing import TYPE_CHECKING, ClassVar

from bubus import BaseEvent

from pydantic import PrivateAttr

from browser_use.browser.events import (
	AgentFocusChangedEvent,
	BrowserErrorEvent,
	NavigateToUrlEvent,
	NavigationCompleteEvent,
	TabCreatedEvent,
)
from browser_use.browser.watchdog_base import BaseWatchdog

if TYPE_CHECKING:
	pass

# Track if we've shown the glob warning
_GLOB_WARNING_SHOWN = False


class SecurityWatchdog(BaseWatchdog):
	"""Monitors and enforces security policies for URL access."""

	# Event contracts
	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [
		NavigateToUrlEvent,
		NavigationCompleteEvent,
		TabCreatedEvent,
		AgentFocusChangedEvent,
	]
	EMITS: ClassVar[list[type[BaseEvent]]] = [
		BrowserErrorEvent,
	]

	# Track pre-existing tabs discovered during BrowserSession initialization.
	# These belong to other jobs in shared-browser mode and must not be closed.
	_initial_target_ids: set[str] = PrivateAttr(default_factory=set)
	_initialization_complete: bool = PrivateAttr(default=False)

	async def on_NavigateToUrlEvent(self, event: NavigateToUrlEvent) -> None:
		"""Check if navigation URL is allowed before navigation starts."""
		# Security check BEFORE navigation
		if not self._is_url_allowed(event.url):
			self.logger.warning(f'⛔️ Blocking navigation to disallowed URL: {event.url}')
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='NavigationBlocked',
					message=f'Navigation blocked to disallowed URL: {event.url}',
					details={'url': event.url, 'reason': 'not_in_allowed_domains'},
				)
			)
			# Stop event propagation by raising exception
			raise ValueError(f'Navigation to {event.url} blocked by security policy')

	async def on_NavigationCompleteEvent(self, event: NavigationCompleteEvent) -> None:
		"""Check if navigated URL is allowed (catches redirects to blocked domains)."""
		# Check if the navigated URL is allowed (in case of redirects)
		if not self._is_url_allowed(event.url):
			self.logger.warning(f'⛔️ Navigation to non-allowed URL detected: {event.url}')
			message = (
				f'Navigation blocked to non-allowed URL: {event.url}'
				if 'maintenance' not in event.url.lower()
				else f'Navigation reached a maintenance/interstitial URL: {event.url}'
			)

			# Dispatch browser error
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='NavigationBlocked',
					message=message,
					details={'url': event.url, 'target_id': event.target_id, 'preserved_visible_page': True},
				)
			)
			# Preserve the visible blocker page instead of collapsing to about:blank.
			return

	async def on_AgentFocusChangedEvent(self, event: AgentFocusChangedEvent) -> None:
		"""Mark initialization complete after the first focus event.

		During BrowserSession.start(), TabCreatedEvents for all existing tabs are
		dispatched BEFORE AgentFocusChangedEvent. Once focus is set, any subsequent
		TabCreatedEvent represents a genuinely new tab (e.g. a popup).
		"""
		if not self._initialization_complete:
			self._initialization_complete = True

	async def on_TabCreatedEvent(self, event: TabCreatedEvent) -> None:
		"""Check if new tab URL is allowed.

		In shared-browser mode (GH_TARGET_ID set), pre-existing tabs from other
		jobs are preserved. Only the agent's own focus tab and genuinely new
		popups are subject to domain lockdown.
		"""
		agent_target = self.browser_session.agent_focus_target_id
		is_shared_browser = getattr(self.browser_session, '_initial_target_id', None) is not None

		# In shared-browser mode, skip security checks for tabs that belong
		# to other jobs (pre-existing tabs).
		if is_shared_browser and agent_target and event.target_id != agent_target:
			if not self._initialization_complete:
				# Still during init — this is a pre-existing tab from another job
				self._initial_target_ids.add(event.target_id)
				return
			if event.target_id in self._initial_target_ids:
				# Known pre-existing tab, leave it alone
				return
			# Genuinely new tab created during agent execution — enforce security below

		if not self._is_url_allowed(event.url):
			self.logger.warning(f'⛔️ New tab created with disallowed URL: {event.url}')

			# Dispatch error and try to close the tab
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='TabCreationBlocked',
					message=f'Tab created with non-allowed URL: {event.url}',
					details={'url': event.url, 'target_id': event.target_id},
				)
			)

			# Try to close the offending tab
			try:
				await self.browser_session._cdp_close_page(event.target_id)
				self.logger.info(f'⛔️ Closed new tab with non-allowed URL: {event.url}')
			except Exception as e:
				self.logger.error(f'⛔️ Failed to close new tab with non-allowed URL: {type(e).__name__} {e}')

	def _is_root_domain(self, domain: str) -> bool:
		"""True for simple registrable names like example.com (one dot); used for prohibited-domain www matching only."""
		if '*' in domain or '://' in domain:
			return False
		return domain.count('.') == 1

	def _log_glob_warning(self) -> None:
		"""Log a warning about glob patterns in allowed_domains."""
		global _GLOB_WARNING_SHOWN
		if not _GLOB_WARNING_SHOWN:
			_GLOB_WARNING_SHOWN = True
			self.logger.warning(
				'⚠️ Using glob patterns in allowed_domains. '
				'Note: Patterns like "*.example.com" will match both subdomains AND the main domain.'
			)

	def _get_domain_variants(self, host: str) -> tuple[str, str]:
		"""Get both variants of a domain (with and without www prefix).

		Args:
			host: The hostname to process

		Returns:
			Tuple of (original_host, variant_host)
			- If host starts with www., variant is without www.
			- Otherwise, variant is with www. prefix
		"""
		if host.startswith('www.'):
			return (host, host[4:])  # ('www.example.com', 'example.com')
		else:
			return (host, f'www.{host}')  # ('example.com', 'www.example.com')

	def _is_ip_address(self, host: str) -> bool:
		"""Check if a hostname is an IP address (IPv4 or IPv6).

		Args:
			host: The hostname to check

		Returns:
			True if the host is an IP address, False otherwise
		"""
		import ipaddress

		try:
			# Try to parse as IP address (handles both IPv4 and IPv6)
			ipaddress.ip_address(host)
			return True
		except ValueError:
			return False
		except Exception:
			return False

	def _is_url_allowed(self, url: str) -> bool:
		"""Check if a URL is allowed based on the allowed_domains configuration.

		Args:
			url: The URL to check

		Returns:
			True if the URL is allowed, False otherwise
		"""

		# Always allow internal browser targets (before any other checks)
		if url in ['about:blank', 'chrome://new-tab-page/', 'chrome://new-tab-page', 'chrome://newtab/']:
			return True

		# Parse the URL to extract components
		from urllib.parse import urlparse

		try:
			parsed = urlparse(url)
		except Exception:
			# Invalid URL
			return False

		# Allow blob: URLs (origin-bound, safe)
		if parsed.scheme == 'blob':
			return True

		# Allow data: URLs only for known-safe MIME types (images, fonts, audio, video).
		# Allowlist approach — blocks everything not explicitly safe, including
		# image/svg+xml, application/xhtml+xml, text/xml which can contain scripts.
		if parsed.scheme == 'data':
			data_header = url.split(',', 1)[0].lower()
			safe_prefixes = ('data:image/', 'data:font/', 'data:audio/', 'data:video/', 'data:application/font-', 'data:application/octet-stream')
			# SVG can execute scripts — explicitly exclude even though it starts with image/
			if 'image/svg' in data_header:
				return False
			if any(data_header.startswith(p) for p in safe_prefixes):
				return True
			return False

		# Get the actual host (domain)
		host = parsed.hostname
		if not host:
			return False

		# Check if IP addresses should be blocked (before domain checks)
		if self.browser_session.browser_profile.block_ip_addresses:
			if self._is_ip_address(host):
				return False

		# If no allowed_domains specified, allow all URLs
		if (
			not self.browser_session.browser_profile.allowed_domains
			and not self.browser_session.browser_profile.prohibited_domains
		):
			return True

		# Check allowed domains (fast path for sets, slow path for lists with patterns)
		if self.browser_session.browser_profile.allowed_domains:
			allowed_domains = self.browser_session.browser_profile.allowed_domains

			if isinstance(allowed_domains, set):
				# Fast path: O(1) exact hostname match - check both www and non-www variants
				host_variant, host_alt = self._get_domain_variants(host)
				return host_variant in allowed_domains or host_alt in allowed_domains
			else:
				# Slow path: O(n) pattern matching for lists
				for pattern in allowed_domains:
					if self._is_url_match(url, host, parsed.scheme, pattern):
						return True
				return False

		# Check prohibited domains (fast path for sets, slow path for lists with patterns)
		if self.browser_session.browser_profile.prohibited_domains:
			prohibited_domains = self.browser_session.browser_profile.prohibited_domains

			if isinstance(prohibited_domains, set):
				# Fast path: O(1) exact hostname match - check both www and non-www variants
				host_variant, host_alt = self._get_domain_variants(host)
				return host_variant not in prohibited_domains and host_alt not in prohibited_domains
			else:
				# Slow path: O(n) pattern matching for lists
				for pattern in prohibited_domains:
					if self._is_url_match(
						url, host, parsed.scheme, pattern, subdomain_suffix_for_plain_domain=False
					):
						return False
				return True

		return True

	def _is_url_match(
		self,
		url: str,
		host: str,
		scheme: str,
		pattern: str,
		*,
		subdomain_suffix_for_plain_domain: bool = True,
	) -> bool:
		"""Check if a URL matches a pattern.

		Allowlists use subdomain_suffix_for_plain_domain=True so ``oraclecloud.com`` matches
		``hdpc.fa.us2.oraclecloud.com`` (same as Hand-X DomainLockdown). Prohibited lists use
		False so blocking ``example.com`` does not block ``mail.example.com`` unless a glob is used.
		"""

		# Full URL for matching (scheme + host)
		full_url_pattern = f'{scheme}://{host}'

		# Handle glob patterns
		if '*' in pattern:
			self._log_glob_warning()
			import fnmatch

			# Check if pattern matches the host
			if pattern.startswith('*.'):
				# Pattern like *.example.com should match subdomains and main domain
				domain_part = pattern[2:]  # Remove *.
				if host == domain_part or host.endswith('.' + domain_part):
					# Only match http/https URLs for domain-only patterns
					if scheme in ['http', 'https']:
						return True
			elif pattern.endswith('/*'):
				# Pattern like brave://* or http*://example.com/*
				if fnmatch.fnmatch(url, pattern):
					return True
			else:
				# Use fnmatch for other glob patterns
				if fnmatch.fnmatch(
					full_url_pattern if '://' in pattern else host,
					pattern,
				):
					return True
		else:
			# Exact match
			if '://' in pattern:
				# Full URL pattern
				if url.startswith(pattern):
					return True
			else:
				hl, pl = host.lower(), pattern.lower()
				if subdomain_suffix_for_plain_domain:
					if hl == pl or hl.endswith('.' + pl):
						return True
				else:
					if hl == pl:
						return True
					if self._is_root_domain(pattern) and hl == f'www.{pl}':
						return True

		return False
