"""Platforms module — ATS-specific guardrails and strategies (Workday, Greenhouse, Lever, etc.)."""

from ghosthands.platforms.views import PlatformConfig
from ghosthands.platforms.workday import WORKDAY_CONFIG
from ghosthands.platforms.greenhouse import GREENHOUSE_CONFIG
from ghosthands.platforms.lever import LEVER_CONFIG
from ghosthands.platforms.smartrecruiters import SMARTRECRUITERS_CONFIG
from ghosthands.platforms.generic import GENERIC_CONFIG


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


def detect_platform(url: str) -> str:
	"""Detect ATS platform from URL.

	Returns: 'workday' | 'greenhouse' | 'lever' | 'smartrecruiters' | 'generic'
	"""
	normalized = url.lower()
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
	"detect_platform",
	"get_platform_config",
	"get_config_by_name",
	"PlatformConfig",
	"WORKDAY_CONFIG",
	"GREENHOUSE_CONFIG",
	"LEVER_CONFIG",
	"SMARTRECRUITERS_CONFIG",
	"GENERIC_CONFIG",
]
