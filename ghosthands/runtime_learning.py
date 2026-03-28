"""Runtime-learned semantic aliases and interaction recipes for Hand-X."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

SemanticQuestionIntent = Literal[
    "work_authorization",
    "visa_sponsorship",
    "how_did_you_hear",
    "willing_to_relocate",
    "salary_expectation",
    "current_school_year",
    "graduation_date",
    "degree_seeking",
    "certifications_licenses",
    "spoken_languages",
    "english_proficiency",
    "country_of_residence",
    "preferred_work_mode",
    "preferred_locations",
    "availability_window",
    "notice_period",
    "gender",
    "race_ethnicity",
    "veteran_status",
    "disability_status",
    "employer_history",
]


class LearnedQuestionAlias(BaseModel):
    model_config = ConfigDict(extra="ignore")

    normalized_label: str
    intent: SemanticQuestionIntent
    source: str = "semantic_fallback"
    confidence: str = "high"


class LearnedInteractionRecipe(BaseModel):
    model_config = ConfigDict(extra="ignore")

    platform: str
    host: str
    normalized_label: str
    widget_signature: str
    preferred_action_chain: list[str] = Field(default_factory=list)
    source: str = "visual_fallback"


class ExpectedFieldValue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str
    page_context_key: str
    field_key: str
    field_label: str
    field_type: str = ""
    field_section: str = ""
    field_fingerprint: str = ""
    expected_value: str
    source: Literal[
        "exact_profile",
        "derived_profile",
        "manual_recovery",
        "domhand_unverified",
    ]


class RepeaterFieldBinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str
    repeater_group: str
    field_binding_key: str
    entry_index: int
    binding_mode: Literal["exact", "similarity", "row_order", "manual_recovery"]
    binding_confidence: Literal["high", "medium", "low"]
    best_effort_guess: bool = False


_loaded = False
_loaded_aliases: dict[str, LearnedQuestionAlias] = {}
_pending_aliases: dict[str, LearnedQuestionAlias] = {}
_confirmed_aliases: dict[str, LearnedQuestionAlias] = {}
_loaded_recipes: dict[tuple[str, str, str, str], LearnedInteractionRecipe] = {}
_confirmed_recipes: dict[tuple[str, str, str, str], LearnedInteractionRecipe] = {}
_semantic_cache: dict[str, LearnedQuestionAlias | None] = {}
_domhand_failure_counts: dict[tuple[str, str, str], int] = {}
_domhand_retry_capped: set[tuple[str, str, str]] = set()
_expected_field_values: dict[tuple[str, str, str], ExpectedFieldValue] = {}
_repeater_field_bindings: dict[tuple[str, str, str], RepeaterFieldBinding] = {}
_active_page_context_by_host: dict[str, str] = {}

DOMHAND_RETRY_CAP = 2


def reset_runtime_learning_state() -> None:
    """Reset in-process learning state between runs/tests."""
    global _loaded
    _loaded = False
    _loaded_aliases.clear()
    _pending_aliases.clear()
    _confirmed_aliases.clear()
    _loaded_recipes.clear()
    _confirmed_recipes.clear()
    _semantic_cache.clear()
    _domhand_failure_counts.clear()
    _domhand_retry_capped.clear()
    _expected_field_values.clear()
    _repeater_field_bindings.clear()
    _active_page_context_by_host.clear()


def normalize_runtime_label(text: str | None) -> str:
    """Normalize a question label for matching and persistence."""
    if not text:
        return ""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.strip().lower())
    return " ".join(cleaned.split())


def detect_platform_from_url(url: str | None) -> str:
    """Best-effort ATS platform detection for runtime learning scope."""
    host = detect_host_from_url(url)
    if "workday" in host:
        return "workday"
    if "greenhouse" in host:
        return "greenhouse"
    if "lever" in host:
        return "lever"
    if "smartrecruiters" in host:
        return "smartrecruiters"
    if "oraclecloud" in host:
        return "oracle"
    if "ashby" in host:
        return "ashby"
    if "bamboohr" in host:
        return "bamboohr"
    return "other"


def detect_host_from_url(url: str | None) -> str:
    """Return the lowercase hostname for a URL."""
    if not url:
        return ""
    try:
        return urlparse(url).hostname.lower() if urlparse(url).hostname else ""
    except Exception:
        return ""


def build_domhand_retry_key(
    *,
    host: str,
    field_key: str,
    desired_value: str,
) -> tuple[str, str, str] | None:
    """Build a stable per-run retry key for a DomHand field/value pair."""
    normalized_field_key = normalize_runtime_label(field_key)
    normalized_value = normalize_runtime_label(desired_value)
    normalized_host = str(host or "").strip().lower()
    if not normalized_field_key or not normalized_value:
        return None
    return (normalized_host, normalized_field_key, normalized_value)


def build_expected_field_key(
    *,
    host: str,
    page_context_key: str,
    field_key: str,
) -> tuple[str, str, str] | None:
    normalized_field_key = normalize_runtime_label(field_key)
    normalized_host = str(host or "").strip().lower()
    normalized_page_context = normalize_runtime_label(page_context_key)
    if not normalized_field_key or not normalized_page_context:
        return None
    return (normalized_host, normalized_page_context, normalized_field_key)


def build_page_context_key(*, url: str | None, page_marker: str | None) -> str:
    """Build a stable page-scoped context key for expected-value tracking."""
    normalized_marker = normalize_runtime_label(page_marker)
    path = ""
    if url:
        try:
            parsed = urlparse(url)
            path = normalize_runtime_label(parsed.path or "/")
        except Exception:
            path = ""
    if not path:
        path = "root"
    if not normalized_marker:
        normalized_marker = "page"
    return f"{path}::{normalized_marker}"


def activate_page_context(*, host: str, page_context_key: str) -> None:
    """Switch expected-value tracking to the current page context for a host."""
    normalized_host = str(host or "").strip().lower()
    normalized_page_context = normalize_runtime_label(page_context_key)
    if not normalized_host or not normalized_page_context:
        return

    previous = _active_page_context_by_host.get(normalized_host)
    if previous == normalized_page_context:
        return

    _active_page_context_by_host[normalized_host] = normalized_page_context
    stale_keys = [
        key
        for key in _expected_field_values
        if key[0] == normalized_host and key[1] != normalized_page_context
    ]
    for key in stale_keys:
        _expected_field_values.pop(key, None)


def build_repeater_field_binding_key(
    *,
    host: str,
    repeater_group: str,
    field_binding_key: str,
) -> tuple[str, str, str] | None:
    normalized_host = str(host or "").strip().lower()
    normalized_group = normalize_runtime_label(repeater_group)
    normalized_key = normalize_runtime_label(field_binding_key)
    if not normalized_group or not normalized_key:
        return None
    return (normalized_host, normalized_group, normalized_key)


def record_expected_field_value(
    *,
    host: str,
    page_context_key: str,
    field_key: str,
    field_label: str,
    field_type: str = "",
    field_section: str = "",
    field_fingerprint: str = "",
    expected_value: str,
    source: Literal[
        "exact_profile",
        "derived_profile",
        "manual_recovery",
        "domhand_unverified",
    ],
) -> None:
    activate_page_context(host=host, page_context_key=page_context_key)
    key = build_expected_field_key(host=host, page_context_key=page_context_key, field_key=field_key)
    normalized_expected = str(expected_value or "").strip()
    if key is None or not normalized_expected:
        return
    _expected_field_values[key] = ExpectedFieldValue(
        host=key[0],
        page_context_key=key[1],
        field_key=key[2],
        field_label=str(field_label or "").strip(),
        field_type=str(field_type or "").strip(),
        field_section=str(field_section or "").strip(),
        field_fingerprint=str(field_fingerprint or "").strip(),
        expected_value=normalized_expected,
        source=source,
    )


def get_expected_field_value(
    *,
    host: str,
    page_context_key: str,
    field_key: str,
) -> ExpectedFieldValue | None:
    activate_page_context(host=host, page_context_key=page_context_key)
    key = build_expected_field_key(host=host, page_context_key=page_context_key, field_key=field_key)
    if key is None:
        return None
    return _expected_field_values.get(key)


def record_repeater_field_binding(
    *,
    host: str,
    repeater_group: str,
    field_binding_key: str,
    entry_index: int,
    binding_mode: Literal["exact", "similarity", "row_order", "manual_recovery"],
    binding_confidence: Literal["high", "medium", "low"],
    best_effort_guess: bool = False,
) -> None:
    key = build_repeater_field_binding_key(
        host=host,
        repeater_group=repeater_group,
        field_binding_key=field_binding_key,
    )
    if key is None or entry_index < 0:
        return
    _repeater_field_bindings[key] = RepeaterFieldBinding(
        host=key[0],
        repeater_group=key[1],
        field_binding_key=key[2],
        entry_index=entry_index,
        binding_mode=binding_mode,
        binding_confidence=binding_confidence,
        best_effort_guess=best_effort_guess,
    )


def get_repeater_field_binding(
    *,
    host: str,
    repeater_group: str,
    field_binding_key: str,
) -> RepeaterFieldBinding | None:
    key = build_repeater_field_binding_key(
        host=host,
        repeater_group=repeater_group,
        field_binding_key=field_binding_key,
    )
    if key is None:
        return None
    return _repeater_field_bindings.get(key)


def get_domhand_failure_count(
    *,
    host: str,
    field_key: str,
    desired_value: str,
) -> int:
    """Return the in-run DomHand failure count for a field/value pair."""
    key = build_domhand_retry_key(host=host, field_key=field_key, desired_value=desired_value)
    if key is None:
        return 0
    return _domhand_failure_counts.get(key, 0)


def is_domhand_retry_capped(
    *,
    host: str,
    field_key: str,
    desired_value: str,
) -> bool:
    """Return True when DomHand should stop touching this field/value in the current run."""
    key = build_domhand_retry_key(host=host, field_key=field_key, desired_value=desired_value)
    if key is None:
        return False
    return key in _domhand_retry_capped


def record_domhand_failure(
    *,
    host: str,
    field_key: str,
    desired_value: str,
) -> int:
    """Increment the in-run DomHand failure count and cap when the threshold is reached."""
    key = build_domhand_retry_key(host=host, field_key=field_key, desired_value=desired_value)
    if key is None:
        return 0
    count = _domhand_failure_counts.get(key, 0) + 1
    _domhand_failure_counts[key] = count
    if count >= DOMHAND_RETRY_CAP:
        _domhand_retry_capped.add(key)
    return count


def clear_domhand_failure(
    *,
    host: str,
    field_key: str,
    desired_value: str,
) -> None:
    """Clear an in-run DomHand failure count unless the field/value is already capped."""
    key = build_domhand_retry_key(host=host, field_key=field_key, desired_value=desired_value)
    if key is None or key in _domhand_retry_capped:
        return
    _domhand_failure_counts.pop(key, None)


def _alias_from_raw(raw: Any) -> LearnedQuestionAlias | None:
    if not isinstance(raw, dict):
        return None
    normalized_label = normalize_runtime_label(
        str(raw.get("normalized_label") or raw.get("normalizedLabel") or ""),
    )
    intent = str(raw.get("intent") or "").strip()
    if not normalized_label or not intent:
        return None
    try:
        return LearnedQuestionAlias(
            normalized_label=normalized_label,
            intent=intent,  # type: ignore[arg-type]
            source=str(raw.get("source") or "semantic_fallback").strip() or "semantic_fallback",
            confidence=str(raw.get("confidence") or "high").strip() or "high",
        )
    except Exception:
        return None


def _recipe_from_raw(raw: Any) -> LearnedInteractionRecipe | None:
    if not isinstance(raw, dict):
        return None
    platform = str(raw.get("platform") or "").strip().lower()
    host = str(raw.get("host") or "").strip().lower()
    normalized_label = normalize_runtime_label(
        str(raw.get("normalized_label") or raw.get("normalizedLabel") or ""),
    )
    widget_signature = str(raw.get("widget_signature") or raw.get("widgetSignature") or "").strip()
    preferred_action_chain = raw.get("preferred_action_chain") or raw.get("preferredActionChain") or []
    if not isinstance(preferred_action_chain, list):
        return None
    action_chain = [
        str(step).strip() for step in preferred_action_chain if isinstance(step, str) and step.strip()
    ]
    if not platform or not host or not normalized_label or not widget_signature or not action_chain:
        return None
    return LearnedInteractionRecipe(
        platform=platform,
        host=host,
        normalized_label=normalized_label,
        widget_signature=widget_signature,
        preferred_action_chain=action_chain,
        source=str(raw.get("source") or "visual_fallback").strip() or "visual_fallback",
    )


def ensure_runtime_learning_loaded(profile_data: dict[str, Any] | None) -> None:
    """Hydrate persisted learning from the runtime profile payload once."""
    global _loaded
    if _loaded:
        return

    profile = profile_data or {}
    raw_aliases = profile.get("learned_question_aliases") or profile.get("learnedQuestionAliases") or []
    raw_recipes = profile.get("learned_interaction_recipes") or profile.get("learnedInteractionRecipes") or []

    for raw in raw_aliases:
        alias = _alias_from_raw(raw)
        if alias is not None:
            _loaded_aliases[alias.normalized_label] = alias

    for raw in raw_recipes:
        recipe = _recipe_from_raw(raw)
        if recipe is not None:
            _loaded_recipes[
                (recipe.platform, recipe.host, recipe.normalized_label, recipe.widget_signature)
            ] = recipe

    _loaded = True


def get_learned_question_alias(
    label: str,
    profile_data: dict[str, Any] | None = None,
) -> LearnedQuestionAlias | None:
    """Return a persisted or in-run learned alias for a normalized label."""
    ensure_runtime_learning_loaded(profile_data)
    normalized = normalize_runtime_label(label)
    if not normalized:
        return None
    return _confirmed_aliases.get(normalized) or _pending_aliases.get(normalized) or _loaded_aliases.get(normalized)


def get_cached_semantic_alias(label: str) -> LearnedQuestionAlias | None:
    """Return an in-run semantic classification cache hit when present."""
    return _semantic_cache.get(normalize_runtime_label(label))


def has_cached_semantic_alias(label: str) -> bool:
    """Return True when a semantic classification result was cached for the label."""
    return normalize_runtime_label(label) in _semantic_cache


def cache_semantic_alias(label: str, alias: LearnedQuestionAlias | None) -> None:
    """Cache the semantic classification result for a normalized label."""
    _semantic_cache[normalize_runtime_label(label)] = alias


def stage_learned_question_alias(
    label: str,
    intent: SemanticQuestionIntent,
    *,
    source: str = "semantic_fallback",
    confidence: str = "high",
    profile_data: dict[str, Any] | None = None,
) -> None:
    """Stage a high-confidence semantic alias until the field actually fills."""
    ensure_runtime_learning_loaded(profile_data)
    normalized = normalize_runtime_label(label)
    if not normalized:
        return
    loaded = _loaded_aliases.get(normalized)
    if loaded and loaded.intent == intent:
        return
    _pending_aliases[normalized] = LearnedQuestionAlias(
        normalized_label=normalized,
        intent=intent,
        source=source,
        confidence=confidence,
    )


def confirm_learned_question_alias(label: str) -> None:
    """Promote a staged alias once the field really filled successfully."""
    normalized = normalize_runtime_label(label)
    pending = _pending_aliases.pop(normalized, None)
    if pending is None:
        return
    loaded = _loaded_aliases.get(normalized)
    if loaded and loaded.intent == pending.intent:
        return
    _confirmed_aliases[normalized] = pending


def get_interaction_recipe(
    *,
    platform: str,
    host: str,
    label: str,
    widget_signature: str,
    profile_data: dict[str, Any] | None = None,
) -> LearnedInteractionRecipe | None:
    """Return a persisted or newly learned interaction recipe when available."""
    ensure_runtime_learning_loaded(profile_data)
    key = (platform.lower(), host.lower(), normalize_runtime_label(label), widget_signature.strip())
    return _confirmed_recipes.get(key) or _loaded_recipes.get(key)


def record_interaction_recipe(
    *,
    platform: str,
    host: str,
    label: str,
    widget_signature: str,
    preferred_action_chain: list[str],
    source: str = "visual_fallback",
    profile_data: dict[str, Any] | None = None,
) -> None:
    """Persist an in-run interaction recipe after a verified widget success."""
    ensure_runtime_learning_loaded(profile_data)
    normalized = normalize_runtime_label(label)
    if not platform or not host or not normalized or not widget_signature or not preferred_action_chain:
        return
    recipe = LearnedInteractionRecipe(
        platform=platform.lower(),
        host=host.lower(),
        normalized_label=normalized,
        widget_signature=widget_signature.strip(),
        preferred_action_chain=[step for step in preferred_action_chain if step],
        source=source,
    )
    key = (recipe.platform, recipe.host, recipe.normalized_label, recipe.widget_signature)
    loaded = _loaded_recipes.get(key)
    if loaded and loaded.preferred_action_chain == recipe.preferred_action_chain:
        return
    _confirmed_recipes[key] = recipe


def export_runtime_learning_payload() -> dict[str, list[dict[str, Any]]]:
    """Return the newly learned artifacts for final CLI result_data emission."""
    return {
        "learned_question_aliases": [
            alias.model_dump(mode="json") for alias in _confirmed_aliases.values()
        ],
        "learned_interaction_recipes": [
            recipe.model_dump(mode="json") for recipe in _confirmed_recipes.values()
        ],
    }
