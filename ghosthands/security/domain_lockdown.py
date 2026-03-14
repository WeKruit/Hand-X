"""Domain lockdown — URL allowlist enforcement for prompt injection mitigation.

Ported from GHOST-HANDS domainLockdown.ts (security report S4).

After initial navigation to a job URL, all subsequent navigations are
restricted to the same domain and known ATS subdomains. This prevents
a malicious job listing from tricking the LLM agent into navigating to
an attacker-controlled URL for data exfiltration.

Usage:
	lockdown = DomainLockdown(job_url="https://company.myworkdayjobs.com/...", platform="workday")
	if lockdown.is_allowed("https://wd5.myworkday.com/login"):
		# safe to navigate
	else:
		# blocked — do not navigate
"""

from __future__ import annotations

import logging
from collections import deque
from urllib.parse import urlparse

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-platform domain allowlists
# ---------------------------------------------------------------------------

PLATFORM_ALLOWLISTS: dict[str, list[str]] = {
	"workday": [
		"myworkdayjobs.com",
		"myworkday.com",
		"workday.com",
		"wd1.myworkdayjobs.com",
		"wd2.myworkdayjobs.com",
		"wd3.myworkdayjobs.com",
		"wd4.myworkdayjobs.com",
		"wd5.myworkdayjobs.com",
		"wd1.myworkday.com",
		"wd2.myworkday.com",
		"wd3.myworkday.com",
		"wd4.myworkday.com",
		"wd5.myworkday.com",
		"wd5.myworkdaysite.com",
	],
	"linkedin": [
		"linkedin.com",
		"www.linkedin.com",
		"licdn.com",
	],
	"greenhouse": [
		"greenhouse.io",
		"boards.greenhouse.io",
		"job-boards.greenhouse.io",
	],
	"lever": [
		"lever.co",
		"jobs.lever.co",
	],
	"ashby": [
		"ashbyhq.com",
		"jobs.ashbyhq.com",
	],
	"icims": [
		"icims.com",
		"careers-page.icims.com",
	],
	"smartrecruiters": [
		"smartrecruiters.com",
		"jobs.smartrecruiters.com",
	],
	"amazon": [
		"amazon.jobs",
		"www.amazon.jobs",
		"amazon.com",
		"www.amazon.com",
	],
}


# Common CDN/resource domains that should always be allowed for page rendering.
RESOURCE_ALLOWLIST: list[str] = [
	"fonts.googleapis.com",
	"fonts.gstatic.com",
	"cdn.jsdelivr.net",
	"cdnjs.cloudflare.com",
	"unpkg.com",
	"googletagmanager.com",
	"google-analytics.com",
	"google.com",
	"gstatic.com",
	"recaptcha.net",
	"hcaptcha.com",
]


# Resource types that are safe to load from any domain (images, fonts, etc.)
SAFE_RESOURCE_TYPES: frozenset[str] = frozenset([
	"image",
	"font",
	"media",
	"stylesheet",
])


# ---------------------------------------------------------------------------
# Domain matching helpers
# ---------------------------------------------------------------------------

def _extract_domain(url: str) -> str | None:
	"""Extract the hostname from a URL, lowercased."""
	try:
		parsed = urlparse(url)
		hostname = parsed.hostname
		return hostname.lower() if hostname else None
	except Exception:
		return None


def _domain_matches(hostname: str, allowed_domain: str) -> bool:
	"""Check if hostname matches an allowed domain (exact or subdomain)."""
	h = hostname.lower()
	a = allowed_domain.lower()
	return h == a or h.endswith("." + a)


# ---------------------------------------------------------------------------
# Lockdown stats
# ---------------------------------------------------------------------------

class LockdownStats(BaseModel):
	"""Statistics for a DomainLockdown instance."""
	total_intercepted: int = 0
	allowed: int = 0
	blocked: int = 0
	blocked_urls: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# DomainLockdown
# ---------------------------------------------------------------------------

class DomainLockdown:
	"""URL allowlist enforcement for a single job application session.

	After construction, call `is_allowed(url)` to check any URL before
	navigating. The lockdown is configured with:
	  - The initial job URL's domain (auto-added)
	  - Platform-specific ATS domains
	  - Additional user-specified domains
	  - CDN/resource domains (always allowed)

	Args:
		job_url: The initial job URL. Its domain becomes the primary allowed domain.
		platform: ATS platform name (for loading platform-specific allowlist).
		additional_domains: Extra domains to allow (e.g. company-specific SSO).
		allow_cross_origin_resources: Whether to allow resource loads (images, CSS, fonts)
			from any domain. Defaults to True.
		on_blocked: Callback when a navigation is blocked. Receives (url, reason).
	"""

	def __init__(
		self,
		job_url: str,
		platform: str = "",
		additional_domains: list[str] | None = None,
		allow_cross_origin_resources: bool = True,
		on_blocked: callable | None = None,
	) -> None:
		self._allowed_domains: set[str] = set()
		self._stats = LockdownStats()
		self._blocked_urls: deque[str] = deque(maxlen=20)
		self._allow_cross_origin_resources = allow_cross_origin_resources
		self._on_blocked = on_blocked
		self._frozen = False

		# Add the job URL's domain
		job_domain = _extract_domain(job_url)
		if job_domain:
			self._allowed_domains.add(job_domain)

		# Add platform-specific domains
		if platform:
			platform_domains = PLATFORM_ALLOWLISTS.get(platform.lower(), [])
			for d in platform_domains:
				self._allowed_domains.add(d.lower())

		# Add additional domains
		if additional_domains:
			for d in additional_domains:
				self._allowed_domains.add(d.lower())

		# Always allow resource CDNs
		for d in RESOURCE_ALLOWLIST:
			self._allowed_domains.add(d.lower())

	def is_allowed(self, url: str) -> bool:
		"""Check if a URL is allowed by the lockdown policy.

		Args:
			url: The URL to check.

		Returns:
			True if navigation to this URL is allowed.
		"""
		hostname = _extract_domain(url)
		if not hostname:
			return False

		for allowed in self._allowed_domains:
			if _domain_matches(hostname, allowed):
				return True
		return False

	def check_and_record(self, url: str, resource_type: str = "document") -> bool:
		"""Check if a URL is allowed and record the result in stats.

		Args:
			url: The URL to check.
			resource_type: The resource type (e.g. 'document', 'image', 'font').

		Returns:
			True if allowed, False if blocked.
		"""
		self._stats.total_intercepted += 1

		# Allow safe resource types from any domain if configured
		if self._allow_cross_origin_resources and resource_type in SAFE_RESOURCE_TYPES:
			self._stats.allowed += 1
			return True

		if self.is_allowed(url):
			self._stats.allowed += 1
			return True

		# Block
		reason = f"Blocked {resource_type} request to non-allowed domain"
		self._record_blocked(url, reason)
		return False

	def freeze(self) -> None:
		"""Prevent further domain additions after initialization."""
		self._frozen = True

	def add_allowed_domain(self, domain: str) -> None:
		"""Add a domain to the allowlist at runtime."""
		if self._frozen:
			raise RuntimeError("Cannot add domains after lockdown is frozen")
		self._allowed_domains.add(domain.lower())

	def get_allowed_domains(self) -> list[str]:
		"""Get all currently allowed domains."""
		return sorted(self._allowed_domains)

	def get_stats(self) -> LockdownStats:
		"""Get lockdown statistics."""
		return LockdownStats(
			total_intercepted=self._stats.total_intercepted,
			allowed=self._stats.allowed,
			blocked=self._stats.blocked,
			blocked_urls=list(self._blocked_urls),
		)

	def _record_blocked(self, url: str, reason: str) -> None:
		"""Record a blocked URL."""
		self._stats.blocked += 1
		self._blocked_urls.append(url)

		logger.warning("Domain lockdown blocked navigation", extra={
			"url": url,
			"reason": reason,
			"blocked_count": self._stats.blocked,
		})

		if self._on_blocked:
			try:
				self._on_blocked(url, reason)
			except Exception:
				pass


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def is_url_allowed(url: str, platform: str = "generic") -> bool:
	"""Check if a URL is allowed for the given platform.

	This is a stateless convenience function. For full stats tracking,
	use a DomainLockdown instance instead.

	Args:
		url: The URL to check.
		platform: Platform name ('workday', 'greenhouse', etc.) or 'generic'.

	Returns:
		True if the URL's domain is in the platform's allowlist or CDN list.
	"""
	hostname = _extract_domain(url)
	if not hostname:
		return False

	# Check platform-specific domains
	platform_domains = PLATFORM_ALLOWLISTS.get(platform.lower(), [])
	for allowed in platform_domains:
		if _domain_matches(hostname, allowed):
			return True

	# Check CDN/resource allowlist
	for allowed in RESOURCE_ALLOWLIST:
		if _domain_matches(hostname, allowed):
			return True

	# For generic platform, check all platform allowlists
	if platform.lower() == "generic":
		for _platform_name, domains in PLATFORM_ALLOWLISTS.items():
			for allowed in domains:
				if _domain_matches(hostname, allowed):
					return True

	return False


def create_lockdown_for_platform(
	job_url: str,
	platform: str,
	additional_domains: list[str] | None = None,
) -> DomainLockdown:
	"""Create a DomainLockdown configured for a specific ATS platform.

	Args:
		job_url: The initial job URL.
		platform: ATS platform name.
		additional_domains: Extra domains to allow.

	Returns:
		A configured DomainLockdown instance.
	"""
	return DomainLockdown(
		job_url=job_url,
		platform=platform,
		additional_domains=additional_domains,
	)
