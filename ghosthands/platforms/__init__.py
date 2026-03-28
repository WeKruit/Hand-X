"""Platforms module — ATS-specific guardrails and strategies (Workday, Greenhouse, Lever, etc.)."""

from urllib.parse import parse_qs, urlparse

from ghosthands.platforms.generic import GENERIC_CONFIG
from ghosthands.platforms.greenhouse import GREENHOUSE_CONFIG
from ghosthands.platforms.lever import LEVER_CONFIG
from ghosthands.platforms.oracle import ORACLE_CONFIG
from ghosthands.platforms.phenom import PHENOM_CONFIG
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
        PHENOM_CONFIG,
        SMARTRECRUITERS_CONFIG,
        ORACLE_CONFIG,
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
    # Phenom
    (
        [
            "phenom.com",
            "phenompeople.com",
        ],
        "phenom",
    ),
    # SmartRecruiters
    (
        [
            "jobs.smartrecruiters.com",
            "smartrecruiters.com",
        ],
        "smartrecruiters",
    ),
    # Oracle Cloud HCM
    (
        [
            "oraclecloud.com",
            "fa.ocs.oraclecloud.com",
        ],
        "oracle",
    ),
]

_HOSTED_GREENHOUSE_QUERY_KEYS = frozenset({"gh_jid", "gh_src"})
_HOSTED_GREENHOUSE_PATH_HINTS = (
    "/jobs/",
    "/job/",
    "/apply",
    "/openings/",
)
_HOSTED_GREENHOUSE_HOST_HINTS = (
    "careers",
    "jobs",
)


def _match_marker_hits(normalized_text: str, markers: list[str]) -> set[str]:
    """Return the subset of markers found in the provided page text."""
    return {marker for marker in markers if marker and marker.lower() in normalized_text}


def _looks_like_hosted_greenhouse(url: str) -> bool:
    """Detect high-confidence hosted Greenhouse URLs on custom domains."""
    try:
        parsed = urlparse(url.lower())
        hostname = parsed.hostname or ""
        path = parsed.path or ""
        query_keys = set(parse_qs(parsed.query))
    except Exception:
        return False

    if not (_HOSTED_GREENHOUSE_QUERY_KEYS & query_keys):
        return False

    if any(pattern in hostname for patterns, _ in _URL_PATTERNS for pattern in patterns):
        return False

    has_host_hint = any(
        hostname.startswith(f"{label}.") or f".{label}." in hostname
        for label in _HOSTED_GREENHOUSE_HOST_HINTS
    )
    has_path_hint = any(hint in path for hint in _HOSTED_GREENHOUSE_PATH_HINTS)
    return has_host_hint or has_path_hint


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
        strong_hits = _match_marker_hits(normalized_text, config.strong_content_markers)
        if strong_hits:
            return platform_name
        marker_hits = _match_marker_hits(normalized_text, config.content_markers)
        if len(marker_hits) >= max(1, config.content_marker_min_hits):
            return platform_name

    return url_guess


def detect_platform(url: str) -> str:
    """Detect ATS platform from URL.

    Returns: 'workday' | 'greenhouse' | 'lever' | 'smartrecruiters' | 'oracle' | 'generic'
    """
    normalized = url.lower()
    for patterns, platform_name in _URL_PATTERNS:
        for pattern in patterns:
            if pattern in normalized:
                return platform_name
    if _looks_like_hosted_greenhouse(normalized):
        return "greenhouse"
    return "generic"


def get_platform_config(url: str) -> PlatformConfig:
    """Detect platform from URL and return the corresponding PlatformConfig."""
    platform_name = detect_platform(url)
    return _PLATFORM_REGISTRY.get(platform_name, GENERIC_CONFIG)


def get_config_by_name(name: str) -> PlatformConfig:
    """Look up a PlatformConfig by name. Falls back to generic."""
    return _PLATFORM_REGISTRY.get(name, GENERIC_CONFIG)


def get_fill_strategy(url: str) -> str:
    """Return the preferred form-filling strategy for the platform at *url*."""
    return get_platform_config(url).form_strategy


def get_automation_id_map(url: str) -> dict[str, str]:
    """Return platform-specific automation-id selectors for the platform at *url*."""
    return get_platform_config(url).automation_id_map


def get_fill_overrides(url: str) -> dict[str, str]:
    """Return per-control-type fill strategy overrides for the platform at *url*."""
    return get_platform_config(url).fill_overrides


__all__ = [
    "GENERIC_CONFIG",
    "GREENHOUSE_CONFIG",
    "LEVER_CONFIG",
    "ORACLE_CONFIG",
    "SMARTRECRUITERS_CONFIG",
    "WORKDAY_CONFIG",
    "PlatformConfig",
    "detect_platform",
    "detect_platform_from_signals",
    "get_automation_id_map",
    "get_config_by_name",
    "get_fill_overrides",
    "get_fill_strategy",
    "get_platform_config",
]
