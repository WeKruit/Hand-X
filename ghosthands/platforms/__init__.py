"""Platforms module — ATS-specific guardrails and strategies (Workday, Greenhouse, Lever, etc.)."""

from urllib.parse import parse_qs, urlparse

from ghosthands.platforms.generic import GENERIC_CONFIG
from ghosthands.platforms.greenhouse import GREENHOUSE_CONFIG
from ghosthands.platforms.lever import LEVER_CONFIG
from ghosthands.platforms.smartrecruiters import SMARTRECRUITERS_CONFIG
from ghosthands.platforms.views import PlatformConfig
from ghosthands.platforms.workday import WORKDAY_CONFIG

# ---------------------------------------------------------------------------
# Platform Registry
# ---------------------------------------------------------------------------

_PLATFORM_REGISTRY: dict[str, PlatformConfig] = {
    config.name: config
    for config in [
        WORKDAY_CONFIG,
        GREENHOUSE_CONFIG,
        LEVER_CONFIG,
        SMARTRECRUITERS_CONFIG,
        GENERIC_CONFIG,
    ]
}

# URL patterns for platform detection — ordered from most-specific to least-specific
_URL_PATTERNS: list[tuple[list[str], str]] = [
    # Workday
    (
        [
            "myworkdayjobs.com",
            "myworkday.com",
            "wd1.myworkdayjobs.com",
            "wd3.myworkdayjobs.com",
            "wd5.myworkdayjobs.com",
            "wd5.myworkdaysite.com",
            "workday.com",
        ],
        "workday",
    ),
    # Greenhouse
    (
        [
            "boards.greenhouse.io",
            "job-boards.greenhouse.io",
            "greenhouse.io",
        ],
        "greenhouse",
    ),
    # Lever
    (
        [
            "jobs.lever.co",
            "lever.co",
        ],
        "lever",
    ),
    # SmartRecruiters
    (
        [
            "jobs.smartrecruiters.com",
            "smartrecruiters.com",
        ],
        "smartrecruiters",
    ),
]


def detect_platform_from_signals(
    url: str,
    page_text: str = "",
    markers: list[str] | None = None,
) -> str:
    """Detect platform from URL plus optional DOM/text signals.

    This keeps URL detection as the primary signal but lets hosted or
    white-label pages hint their underlying ATS through stable DOM markers.
    """
    url_guess = detect_platform(url)
    if url_guess != "generic":
        return url_guess

    normalized_text = page_text.lower()
    if markers:
        marker_blob = " ".join(markers).lower()
        if marker_blob:
            normalized_text = f"{normalized_text} {marker_blob}".strip()

    if not normalized_text:
        return url_guess

    for platform_name, config in _PLATFORM_REGISTRY.items():
        if platform_name == "generic":
            continue
        if not config.content_markers:
            continue
        if any(marker.lower() in normalized_text for marker in config.content_markers):
            return platform_name

    return url_guess


def detect_platform(url: str) -> str:
    """Detect ATS platform from URL.

    Returns: 'workday' | 'greenhouse' | 'lever' | 'smartrecruiters' | 'generic'
    """
    normalized = url.lower()
    try:
        parsed = urlparse(normalized)
        query = parse_qs(parsed.query)
        if "gh_jid" in query or "gh_src" in query:
            return "greenhouse"
    except Exception:
        pass
    for patterns, platform_name in _URL_PATTERNS:
        for pattern in patterns:
            if pattern in normalized:
                return platform_name
    return "generic"


def get_platform_config(url: str) -> PlatformConfig:
    """Detect platform from URL and return the corresponding PlatformConfig."""
    platform_name = detect_platform(url)
    return _PLATFORM_REGISTRY.get(platform_name, GENERIC_CONFIG)


def get_config_by_name(name: str) -> PlatformConfig:
    """Look up a PlatformConfig by name. Falls back to generic."""
    return _PLATFORM_REGISTRY.get(name, GENERIC_CONFIG)


__all__ = [
    "GENERIC_CONFIG",
    "GREENHOUSE_CONFIG",
    "LEVER_CONFIG",
    "SMARTRECRUITERS_CONFIG",
    "WORKDAY_CONFIG",
    "PlatformConfig",
    "detect_platform",
    "detect_platform_from_signals",
    "get_config_by_name",
    "get_platform_config",
]
