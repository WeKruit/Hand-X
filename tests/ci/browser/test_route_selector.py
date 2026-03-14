"""Tests for RouteSelector URL-to-engine routing (Stream 6).

Validates:
- Workday URLs route to 'firefox'
- Greenhouse, Lever, iCIMS URLs route to 'chromium'
- Unknown URLs default to 'chromium'
- Empty/invalid URLs default to 'chromium'
- Platform hint overrides URL matching
- Domain extraction handles edge cases
"""

import pytest

from browser_use.browser.providers.route_selector import RouteSelector


class TestFirefoxPreferred:
	"""Workday and other Firefox-preferred sites must route to firefox."""

	def test_workday_myworkdayjobs(self):
		"""myworkdayjobs.com -> firefox."""
		assert RouteSelector.select_engine('https://company.myworkdayjobs.com/en-US/jobs') == 'firefox'

	def test_workday_myworkday(self):
		"""myworkday.com -> firefox."""
		assert RouteSelector.select_engine('https://app.myworkday.com/login') == 'firefox'

	def test_workday_main_domain(self):
		"""workday.com -> firefox."""
		assert RouteSelector.select_engine('https://www.workday.com/careers') == 'firefox'

	def test_workday_subdomain(self):
		"""Subdomains of workday.com -> firefox."""
		assert RouteSelector.select_engine('https://jobs.workday.com/posting/12345') == 'firefox'

	def test_workday_with_path(self):
		"""Full Workday URL with path components -> firefox."""
		url = 'https://acme.myworkdayjobs.com/en-US/External/job/San-Francisco/Software-Engineer_R123456'
		assert RouteSelector.select_engine(url) == 'firefox'


class TestChromiumPreferred:
	"""Greenhouse, Lever, and other Chromium-preferred sites."""

	def test_greenhouse_boards(self):
		"""boards.greenhouse.io -> chromium."""
		assert RouteSelector.select_engine('https://boards.greenhouse.io/company/jobs/12345') == 'chromium'

	def test_greenhouse_main(self):
		"""greenhouse.io -> chromium."""
		assert RouteSelector.select_engine('https://greenhouse.io/something') == 'chromium'

	def test_lever_jobs(self):
		"""jobs.lever.co -> chromium."""
		assert RouteSelector.select_engine('https://jobs.lever.co/company/12345') == 'chromium'

	def test_lever_main(self):
		"""lever.co -> chromium."""
		assert RouteSelector.select_engine('https://lever.co/careers') == 'chromium'

	def test_icims(self):
		"""icims.com -> chromium."""
		assert RouteSelector.select_engine('https://careers.icims.com/jobs/12345') == 'chromium'

	def test_taleo(self):
		"""taleo.net -> chromium."""
		assert RouteSelector.select_engine('https://company.taleo.net/careersection/') == 'chromium'

	def test_successfactors(self):
		"""successfactors.com -> chromium."""
		assert RouteSelector.select_engine('https://company.successfactors.com/career') == 'chromium'


class TestDefaultBehavior:
	"""Unknown and edge-case URLs default to chromium."""

	def test_unknown_domain(self):
		"""Unknown domain -> chromium (default)."""
		assert RouteSelector.select_engine('https://example.com/careers') == 'chromium'

	def test_empty_url(self):
		"""Empty URL -> chromium (default)."""
		assert RouteSelector.select_engine('') == 'chromium'

	def test_none_like_empty_string(self):
		"""Empty string -> chromium (default)."""
		assert RouteSelector.select_engine('') == 'chromium'

	def test_url_without_scheme(self):
		"""URL without scheme is handled gracefully."""
		assert RouteSelector.select_engine('company.myworkdayjobs.com/jobs') == 'firefox'

	def test_random_ats_site(self):
		"""Non-listed ATS -> chromium (default)."""
		assert RouteSelector.select_engine('https://apply.smartrecruiters.com/') == 'chromium'

	def test_localhost(self):
		"""localhost URL -> chromium (default)."""
		assert RouteSelector.select_engine('http://localhost:3000/jobs') == 'chromium'

	def test_ip_address_url(self):
		"""IP address URL -> chromium (default)."""
		assert RouteSelector.select_engine('http://192.168.1.1:8080/careers') == 'chromium'


class TestPlatformHint:
	"""Platform hint should take precedence over URL matching."""

	def test_platform_workday_overrides(self):
		"""platform='workday' -> firefox, even with non-matching URL."""
		assert RouteSelector.select_engine('https://example.com', platform='workday') == 'firefox'

	def test_platform_greenhouse_overrides(self):
		"""platform='greenhouse' -> chromium."""
		assert RouteSelector.select_engine('https://example.com', platform='greenhouse') == 'chromium'

	def test_platform_unknown_falls_to_url(self):
		"""Unknown platform falls back to URL matching."""
		assert RouteSelector.select_engine('https://company.myworkdayjobs.com', platform='unknown') == 'firefox'

	def test_platform_case_insensitive(self):
		"""Platform hint is case-insensitive."""
		assert RouteSelector.select_engine('https://example.com', platform='Workday') == 'firefox'
		assert RouteSelector.select_engine('https://example.com', platform='WORKDAY') == 'firefox'

	def test_empty_platform_ignored(self):
		"""Empty platform string is ignored."""
		assert RouteSelector.select_engine('https://company.myworkdayjobs.com', platform='') == 'firefox'


class TestGetPlatformForUrl:
	"""Tests for RouteSelector.get_platform_for_url()."""

	def test_workday_detected(self):
		"""Workday URL returns 'workday' platform."""
		assert RouteSelector.get_platform_for_url('https://company.myworkdayjobs.com/jobs') == 'workday'

	def test_greenhouse_detected(self):
		"""Greenhouse URL returns 'greenhouse' platform."""
		assert RouteSelector.get_platform_for_url('https://boards.greenhouse.io/company') == 'greenhouse'

	def test_lever_detected(self):
		"""Lever URL returns 'lever' platform."""
		assert RouteSelector.get_platform_for_url('https://jobs.lever.co/company') == 'lever'

	def test_unknown_returns_none(self):
		"""Unknown URL returns None."""
		assert RouteSelector.get_platform_for_url('https://example.com') is None

	def test_empty_url_returns_none(self):
		"""Empty URL returns None."""
		assert RouteSelector.get_platform_for_url('') is None


class TestDomainExtraction:
	"""Tests for RouteSelector._extract_domain() edge cases."""

	def test_https_url(self):
		"""Standard HTTPS URL."""
		assert RouteSelector._extract_domain('https://example.com/path') == 'example.com'

	def test_http_url(self):
		"""Standard HTTP URL."""
		assert RouteSelector._extract_domain('http://example.com') == 'example.com'

	def test_url_with_port(self):
		"""URL with port number."""
		assert RouteSelector._extract_domain('https://example.com:8080/path') == 'example.com'

	def test_url_without_scheme(self):
		"""URL without scheme gets https:// prepended."""
		assert RouteSelector._extract_domain('example.com/path') == 'example.com'

	def test_subdomain_preserved(self):
		"""Subdomains are preserved."""
		assert RouteSelector._extract_domain('https://sub.example.com') == 'sub.example.com'

	def test_empty_string(self):
		"""Empty string returns empty string."""
		assert RouteSelector._extract_domain('') == ''

	def test_uppercase_normalized(self):
		"""Domain is lowercased."""
		assert RouteSelector._extract_domain('https://EXAMPLE.COM') == 'example.com'

	def test_protocol_relative_url(self):
		"""Protocol-relative URL (//example.com)."""
		assert RouteSelector._extract_domain('//example.com/path') == 'example.com'
