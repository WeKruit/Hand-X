"""Regression tests for ghosthands.security.domain_lockdown.

Tests cover:
- DomainLockdown.is_allowed() — URL allowlist enforcement
- Platform-specific domain lists (Workday, Greenhouse, Lever, etc.)
- check_and_record() — stats tracking and cross-origin resource handling
- Convenience functions: is_url_allowed(), create_lockdown_for_platform()
- S4 FIXED: Verifies cli.py NOW wires DomainLockdown — BrowserProfile
  is created with allowed_domains in both JSONL and human paths.
"""

import inspect

import pytest

from ghosthands.security.domain_lockdown import (
    PLATFORM_ALLOWLISTS,
    RESOURCE_ALLOWLIST,
    SAFE_RESOURCE_TYPES,
    DomainLockdown,
    LockdownStats,
    create_lockdown_for_platform,
    is_url_allowed,
)


# ---------------------------------------------------------------------------
# DomainLockdown basic functionality
# ---------------------------------------------------------------------------


def test_lockdown_allows_job_url_domain():
    """BASELINE: The job URL's domain is automatically added to the allowlist."""
    lockdown = DomainLockdown(job_url="https://company.myworkdayjobs.com/jobs/123")

    assert lockdown.is_allowed("https://company.myworkdayjobs.com/jobs/456")


def test_lockdown_allows_subdomain_of_job_url():
    """BASELINE: Subdomains of the job URL domain are allowed."""
    lockdown = DomainLockdown(job_url="https://careers.example.com/apply")

    assert lockdown.is_allowed("https://careers.example.com/other-page")


def test_lockdown_blocks_unrelated_domain():
    """BASELINE: Unrelated domains are blocked."""
    lockdown = DomainLockdown(job_url="https://company.myworkdayjobs.com/jobs/123")

    assert not lockdown.is_allowed("https://evil-attacker.com/steal-data")


def test_lockdown_blocks_similar_but_different_domain():
    """BASELINE: Domains that look similar but aren't subdomains are blocked."""
    lockdown = DomainLockdown(
        job_url="https://company.myworkdayjobs.com/jobs/123",
        platform="workday",
    )

    assert not lockdown.is_allowed("https://notmyworkdayjobs.com/phishing")


def test_lockdown_returns_false_for_invalid_url():
    """BASELINE: Invalid URLs (no hostname extractable) return False."""
    lockdown = DomainLockdown(job_url="https://example.com")

    assert not lockdown.is_allowed("not-a-url")
    assert not lockdown.is_allowed("")


def test_lockdown_case_insensitive():
    """BASELINE: Domain matching is case-insensitive."""
    lockdown = DomainLockdown(job_url="https://Company.Example.COM/apply")

    assert lockdown.is_allowed("https://company.example.com/page")
    assert lockdown.is_allowed("https://COMPANY.EXAMPLE.COM/page")


# ---------------------------------------------------------------------------
# Platform-specific domain lists
# ---------------------------------------------------------------------------


def test_workday_platform_domains():
    """BASELINE: Workday platform includes myworkdayjobs.com, myworkday.com, wd* variants."""
    lockdown = DomainLockdown(
        job_url="https://company.myworkdayjobs.com/jobs/123",
        platform="workday",
    )

    # BASELINE: Workday-specific domains are allowed
    assert lockdown.is_allowed("https://wd5.myworkday.com/login")
    assert lockdown.is_allowed("https://myworkday.com/page")
    assert lockdown.is_allowed("https://wd1.myworkdayjobs.com/page")
    assert lockdown.is_allowed("https://workday.com/page")


def test_greenhouse_platform_domains():
    """BASELINE: Greenhouse platform includes greenhouse.io and boards.greenhouse.io."""
    lockdown = DomainLockdown(
        job_url="https://boards.greenhouse.io/company/jobs/123",
        platform="greenhouse",
    )

    assert lockdown.is_allowed("https://boards.greenhouse.io/other")
    assert lockdown.is_allowed("https://job-boards.greenhouse.io/page")
    assert lockdown.is_allowed("https://greenhouse.io/page")


def test_lever_platform_domains():
    """BASELINE: Lever platform includes lever.co and jobs.lever.co."""
    lockdown = DomainLockdown(
        job_url="https://jobs.lever.co/company/12345",
        platform="lever",
    )

    assert lockdown.is_allowed("https://lever.co/page")
    assert lockdown.is_allowed("https://jobs.lever.co/other-company")


def test_platform_domains_are_case_insensitive():
    """BASELINE: Platform name is lowercased before lookup."""
    lockdown = DomainLockdown(
        job_url="https://jobs.lever.co/company",
        platform="Lever",
    )

    assert lockdown.is_allowed("https://lever.co/page")


def test_unknown_platform_only_gets_job_domain_and_resources():
    """BASELINE: Unknown platform gets no platform-specific domains, only job domain + CDN."""
    lockdown = DomainLockdown(
        job_url="https://custom-ats.example.com/apply",
        platform="unknown-ats",
    )

    assert lockdown.is_allowed("https://custom-ats.example.com/page")
    assert lockdown.is_allowed("https://fonts.googleapis.com/css")  # CDN
    assert not lockdown.is_allowed("https://greenhouse.io/page")  # Not added


# ---------------------------------------------------------------------------
# Additional domains
# ---------------------------------------------------------------------------


def test_additional_domains_are_allowed():
    """BASELINE: additional_domains parameter adds extra domains to the allowlist."""
    lockdown = DomainLockdown(
        job_url="https://company.myworkdayjobs.com/jobs/123",
        additional_domains=["sso.company.com", "auth.company.com"],
    )

    assert lockdown.is_allowed("https://sso.company.com/login")
    assert lockdown.is_allowed("https://auth.company.com/oauth")


def test_add_allowed_domain_at_runtime():
    """BASELINE: add_allowed_domain() adds a domain after construction."""
    lockdown = DomainLockdown(job_url="https://example.com")

    assert not lockdown.is_allowed("https://new-domain.com/page")

    lockdown.add_allowed_domain("new-domain.com")

    assert lockdown.is_allowed("https://new-domain.com/page")


def test_get_allowed_domains_returns_sorted_list():
    """BASELINE: get_allowed_domains() returns a sorted list of all allowed domains."""
    lockdown = DomainLockdown(
        job_url="https://example.com",
        additional_domains=["zzz.com", "aaa.com"],
    )

    domains = lockdown.get_allowed_domains()

    assert isinstance(domains, list)
    assert domains == sorted(domains)
    assert "example.com" in domains
    assert "zzz.com" in domains
    assert "aaa.com" in domains


# ---------------------------------------------------------------------------
# CDN / resource allowlist
# ---------------------------------------------------------------------------


def test_cdn_domains_always_allowed():
    """BASELINE: CDN domains from RESOURCE_ALLOWLIST are always allowed."""
    lockdown = DomainLockdown(job_url="https://company.com/apply")

    # BASELINE: These CDN domains should always be in the allowlist
    assert lockdown.is_allowed("https://fonts.googleapis.com/css?family=Roboto")
    assert lockdown.is_allowed("https://fonts.gstatic.com/s/roboto/v30/font.woff2")
    assert lockdown.is_allowed("https://cdn.jsdelivr.net/npm/lib")
    assert lockdown.is_allowed("https://recaptcha.net/recaptcha/api.js")
    assert lockdown.is_allowed("https://hcaptcha.com/1/api.js")


def test_resource_allowlist_contents():
    """BASELINE: RESOURCE_ALLOWLIST contains expected CDN/analytics domains."""
    expected_domains = [
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
    for domain in expected_domains:
        assert domain in RESOURCE_ALLOWLIST, f"{domain} missing from RESOURCE_ALLOWLIST"


def test_platform_allowlists_contain_expected_platforms():
    """BASELINE: PLATFORM_ALLOWLISTS has entries for all known ATS platforms."""
    expected_platforms = [
        "workday",
        "linkedin",
        "greenhouse",
        "lever",
        "ashby",
        "icims",
        "smartrecruiters",
        "amazon",
    ]
    for platform in expected_platforms:
        assert platform in PLATFORM_ALLOWLISTS, f"{platform} missing from PLATFORM_ALLOWLISTS"


# ---------------------------------------------------------------------------
# check_and_record — stats tracking
# ---------------------------------------------------------------------------


def test_check_and_record_tracks_allowed():
    """BASELINE: check_and_record increments allowed count."""
    lockdown = DomainLockdown(job_url="https://example.com")

    result = lockdown.check_and_record("https://example.com/page")

    assert result is True
    stats = lockdown.get_stats()
    assert stats.total_intercepted == 1
    assert stats.allowed == 1
    assert stats.blocked == 0


def test_check_and_record_tracks_blocked():
    """BASELINE: check_and_record increments blocked count and records URL."""
    lockdown = DomainLockdown(job_url="https://example.com")

    result = lockdown.check_and_record("https://evil.com/steal")

    assert result is False
    stats = lockdown.get_stats()
    assert stats.total_intercepted == 1
    assert stats.allowed == 0
    assert stats.blocked == 1
    assert "https://evil.com/steal" in stats.blocked_urls


def test_check_and_record_allows_safe_resource_types_cross_origin():
    """BASELINE: Safe resource types (image, font, etc.) are allowed from any domain
    when allow_cross_origin_resources=True (the default)."""
    lockdown = DomainLockdown(job_url="https://example.com")

    # Image from unrelated domain should be allowed
    result = lockdown.check_and_record("https://cdn.unrelated.com/image.png", resource_type="image")

    assert result is True


def test_check_and_record_blocks_document_cross_origin():
    """BASELINE: Document navigations to non-allowed domains are blocked."""
    lockdown = DomainLockdown(job_url="https://example.com")

    result = lockdown.check_and_record("https://evil.com/phishing", resource_type="document")

    assert result is False


def test_check_and_record_blocks_safe_resources_when_disabled():
    """BASELINE: Safe resource types are blocked when allow_cross_origin_resources=False."""
    lockdown = DomainLockdown(
        job_url="https://example.com",
        allow_cross_origin_resources=False,
    )

    result = lockdown.check_and_record("https://cdn.unrelated.com/image.png", resource_type="image")

    # With cross-origin resources disabled and domain not allowed, should block
    assert result is False


def test_safe_resource_types_set():
    """BASELINE: SAFE_RESOURCE_TYPES contains image, font, media, stylesheet."""
    assert "image" in SAFE_RESOURCE_TYPES
    assert "font" in SAFE_RESOURCE_TYPES
    assert "media" in SAFE_RESOURCE_TYPES
    assert "stylesheet" in SAFE_RESOURCE_TYPES
    # BASELINE: 'document' and 'script' are NOT safe resource types
    assert "document" not in SAFE_RESOURCE_TYPES
    assert "script" not in SAFE_RESOURCE_TYPES


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_get_stats_returns_copy():
    """BASELINE: get_stats() returns a new LockdownStats, not a reference to internal state."""
    lockdown = DomainLockdown(job_url="https://example.com")
    lockdown.check_and_record("https://example.com/page")

    stats1 = lockdown.get_stats()
    lockdown.check_and_record("https://example.com/page2")
    stats2 = lockdown.get_stats()

    # stats1 should not have been mutated by the second check
    assert stats1.total_intercepted == 1
    assert stats2.total_intercepted == 2


def test_blocked_urls_limited_to_20():
    """BASELINE: Blocked URLs are stored in a deque(maxlen=20), oldest dropped."""
    lockdown = DomainLockdown(job_url="https://example.com")

    for i in range(25):
        lockdown.check_and_record(f"https://evil-{i}.com/steal", resource_type="document")

    stats = lockdown.get_stats()
    assert stats.blocked == 25
    # Only the last 20 URLs are kept
    assert len(stats.blocked_urls) == 20
    # Oldest (0-4) should have been dropped
    assert "https://evil-0.com/steal" not in stats.blocked_urls
    assert "https://evil-24.com/steal" in stats.blocked_urls


# ---------------------------------------------------------------------------
# on_blocked callback
# ---------------------------------------------------------------------------


def test_on_blocked_callback_is_called():
    """BASELINE: on_blocked callback is invoked when a URL is blocked."""
    blocked_calls = []

    def on_blocked(url, reason):
        blocked_calls.append((url, reason))

    lockdown = DomainLockdown(
        job_url="https://example.com",
        on_blocked=on_blocked,
    )

    lockdown.check_and_record("https://evil.com/steal", resource_type="document")

    assert len(blocked_calls) == 1
    assert blocked_calls[0][0] == "https://evil.com/steal"
    assert "non-allowed domain" in blocked_calls[0][1].lower()


def test_on_blocked_callback_exception_is_swallowed():
    """BASELINE: If on_blocked raises, the exception is silently caught."""
    def on_blocked(url, reason):
        raise RuntimeError("callback failed")

    lockdown = DomainLockdown(
        job_url="https://example.com",
        on_blocked=on_blocked,
    )

    # Should not raise
    result = lockdown.check_and_record("https://evil.com/steal", resource_type="document")
    assert result is False


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


def test_is_url_allowed_workday():
    """BASELINE: is_url_allowed checks platform-specific domains."""
    assert is_url_allowed("https://company.myworkdayjobs.com/jobs", "workday")
    assert is_url_allowed("https://wd5.myworkday.com/login", "workday")
    assert not is_url_allowed("https://evil.com/steal", "workday")


def test_is_url_allowed_generic_checks_all_platforms():
    """BASELINE: 'generic' platform checks ALL platform allowlists."""
    # A Lever URL should be allowed even with platform='generic'
    assert is_url_allowed("https://jobs.lever.co/company", "generic")
    # A Workday URL should also be allowed with platform='generic'
    assert is_url_allowed("https://myworkdayjobs.com/jobs", "generic")
    # CDN is always allowed
    assert is_url_allowed("https://fonts.googleapis.com/css", "generic")
    # Random domain is not allowed
    assert not is_url_allowed("https://evil.com/steal", "generic")


def test_is_url_allowed_returns_false_for_invalid_url():
    """BASELINE: is_url_allowed returns False for non-parseable URLs."""
    assert not is_url_allowed("not-a-url", "workday")
    assert not is_url_allowed("", "generic")


def test_create_lockdown_for_platform():
    """BASELINE: create_lockdown_for_platform returns a configured DomainLockdown."""
    lockdown = create_lockdown_for_platform(
        job_url="https://jobs.lever.co/company/123",
        platform="lever",
        additional_domains=["sso.company.com"],
    )

    assert isinstance(lockdown, DomainLockdown)
    assert lockdown.is_allowed("https://jobs.lever.co/other")
    assert lockdown.is_allowed("https://lever.co/page")
    assert lockdown.is_allowed("https://sso.company.com/login")
    assert not lockdown.is_allowed("https://evil.com")


# ---------------------------------------------------------------------------
# CLI path DOES wire domain lockdown (S4 fixed the security gap)
# ---------------------------------------------------------------------------


def test_cli_path_uses_domain_lockdown():
    """S4 FIXED: CLI creates BrowserProfile WITH allowed_domains.

    Both run_agent_jsonl and run_agent_human construct BrowserProfile with
    allowed_domains via create_lockdown_for_platform(), matching the security
    boundary that already existed in factory.py's create_job_agent."""
    import ast

    from ghosthands import cli

    source = inspect.getsource(cli)
    tree = ast.parse(source)

    browser_profile_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            func_name = None
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr

            if func_name == "BrowserProfile":
                kwarg_names = [kw.arg for kw in node.keywords if kw.arg is not None]
                browser_profile_calls.append(kwarg_names)

    # There should be BrowserProfile calls in cli.py (jsonl + human)
    assert len(browser_profile_calls) >= 2, (
        f"Expected at least 2 BrowserProfile calls in cli.py (jsonl + human), "
        f"found {len(browser_profile_calls)}"
    )

    # S4 FIXED: ALL BrowserProfile calls in cli.py now include 'allowed_domains'
    for i, kwargs in enumerate(browser_profile_calls):
        assert "allowed_domains" in kwargs, (
            f"BrowserProfile call #{i + 1} in cli.py is missing 'allowed_domains' — "
            f"S4 domain lockdown wiring is incomplete"
        )


def test_factory_path_does_use_domain_lockdown():
    """BASELINE: create_job_agent in factory.py DOES pass allowed_domains to BrowserProfile.
    This confirms the security boundary exists in the worker path as well as CLI."""
    import ast
    from pathlib import Path

    # Read factory.py by file path instead of inspect.getsource() to avoid
    # failures when another test has stubbed ghosthands.agent.factory in sys.modules.
    factory_path = Path(__file__).resolve().parent.parent.parent / "ghosthands" / "agent" / "factory.py"
    source = factory_path.read_text()
    tree = ast.parse(source)

    browser_profile_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            func_name = None
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr

            if func_name == "BrowserProfile":
                kwarg_names = [kw.arg for kw in node.keywords if kw.arg is not None]
                browser_profile_calls.append(kwarg_names)

    # BASELINE: factory.py should have exactly 1 BrowserProfile call
    assert len(browser_profile_calls) == 1

    # BASELINE: factory.py's BrowserProfile call INCLUDES allowed_domains
    assert "allowed_domains" in browser_profile_calls[0], (
        "Expected factory.py to pass allowed_domains to BrowserProfile"
    )
