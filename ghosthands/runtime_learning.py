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


_loaded = False
_loaded_aliases: dict[str, LearnedQuestionAlias] = {}
_pending_aliases: dict[str, LearnedQuestionAlias] = {}
_confirmed_aliases: dict[str, LearnedQuestionAlias] = {}
_loaded_recipes: dict[tuple[str, str, str, str], LearnedInteractionRecipe] = {}
_confirmed_recipes: dict[tuple[str, str, str, str], LearnedInteractionRecipe] = {}
_semantic_cache: dict[str, LearnedQuestionAlias | None] = {}


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
