"""Route-based browser engine selection.

Maps target URL patterns to the optimal browser engine based on known
anti-bot detection characteristics of various ATS platforms.

This is a starting point -- routing rules will be refined based on
real-world detection data from production runs.
"""

from __future__ import annotations

from urllib.parse import urlparse


class RouteSelector:
	"""Select browser engine based on target URL pattern.

	Sites with aggressive Chromium bot detection route to Firefox/Camoufox.
	Sites known to work well with Chromium stay on the default engine.
	Unknown sites default to Chromium (widest compatibility).

	Usage:
		engine = RouteSelector.select_engine("https://company.myworkdayjobs.com/...")
		# Returns "firefox" for Workday

		engine = RouteSelector.select_engine("https://boards.greenhouse.io/...")
		# Returns "chromium" for Greenhouse
	"""

	# Sites with aggressive Chromium bot detection -> prefer Firefox/Camoufox
	FIREFOX_PREFERRED: dict[str, list[str]] = {
		'workday': ['myworkdayjobs.com', 'myworkday.com', 'workday.com'],
	}

	# Sites that work better with Chromium (or have no special detection)
	CHROMIUM_PREFERRED: dict[str, list[str]] = {
		'greenhouse': ['greenhouse.io', 'boards.greenhouse.io'],
		'lever': ['lever.co', 'jobs.lever.co'],
		'icims': ['icims.com'],
		'taleo': ['taleo.net'],
		'successfactors': ['successfactors.com', 'successfactors.eu'],
	}

	@classmethod
	def select_engine(cls, url: str, platform: str = '') -> str:
		"""Return 'chromium' or 'firefox' based on URL and/or platform hint.

		Resolution order:
		1. If ``platform`` matches a known Firefox-preferred platform, return 'firefox'.
		2. If URL domain matches a Firefox-preferred pattern, return 'firefox'.
		3. If URL domain matches a Chromium-preferred pattern, return 'chromium'.
		4. Default: 'chromium'.

		Args:
			url: Target URL to analyze.
			platform: Optional platform hint (e.g., 'workday', 'greenhouse').

		Returns:
			Engine name: 'chromium' or 'firefox'.
		"""
		# Platform hint takes precedence
		if platform:
			platform_lower = platform.lower()
			if platform_lower in cls.FIREFOX_PREFERRED:
				return 'firefox'
			if platform_lower in cls.CHROMIUM_PREFERRED:
				return 'chromium'

		# Extract domain from URL
		domain = cls._extract_domain(url)
		if not domain:
			return 'chromium'

		# Check Firefox-preferred domains
		for _platform, domains in cls.FIREFOX_PREFERRED.items():
			if any(domain.endswith(d) for d in domains):
				return 'firefox'

		# Check Chromium-preferred domains (explicit match, not strictly needed
		# since chromium is the default, but useful for documentation and logging)
		for _platform, domains in cls.CHROMIUM_PREFERRED.items():
			if any(domain.endswith(d) for d in domains):
				return 'chromium'

		# Default to chromium (widest compatibility)
		return 'chromium'

	@classmethod
	def get_platform_for_url(cls, url: str) -> str | None:
		"""Identify the ATS platform for a given URL.

		Args:
			url: Target URL to analyze.

		Returns:
			Platform name (e.g., 'workday', 'greenhouse') or None.
		"""
		domain = cls._extract_domain(url)
		if not domain:
			return None

		for platform, domains in {**cls.FIREFOX_PREFERRED, **cls.CHROMIUM_PREFERRED}.items():
			if any(domain.endswith(d) for d in domains):
				return platform

		return None

	@classmethod
	def _extract_domain(cls, url: str) -> str:
		"""Extract and normalize the domain from a URL.

		Handles URLs with and without scheme. Returns empty string
		for invalid/empty URLs.

		Args:
			url: URL to parse.

		Returns:
			Lowercase domain string, or empty string.
		"""
		if not url:
			return ''

		# urlparse needs a scheme to parse correctly
		if not url.startswith(('http://', 'https://', '//')):
			url = 'https://' + url

		try:
			hostname = urlparse(url).hostname
			return (hostname or '').lower()
		except Exception:
			return ''
