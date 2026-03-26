"""Label matching, field scope filtering, section handling, and answer coercion.

Extracted from ``ghosthands.actions.domhand_fill``.  This module owns:
- Label normalization and matching confidence scoring
- Section name canonicalization and scope filtering
- Focus field resolution
- Answer-to-field coercion (proficiency, phone, binary, etc.)
"""

from __future__ import annotations

import re
from typing import Any, TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from ghosthands.actions.domhand_fill import FocusFieldSelection

from ghosthands.actions.views import (
    FormField,
    get_stable_field_key,
    is_placeholder_value,
    normalize_name,
)

logger = structlog.get_logger(__name__)

_MATCH_CONFIDENCE_RANKS = {
    "exact": 4,
    "strong": 3,
    "medium": 2,
    "weak": 1,
}

_GENERIC_SINGLE_WORD_LABELS = frozenset(
    {
        "source",
        "type",
        "status",
        "name",
        "date",
        "number",
        "code",
        "title",
    }
)


# ── Late-import delegates ────────────────────────────────────────────────

def _is_effectively_unset_field_value(value: str | None) -> bool:
    from ghosthands.actions.domhand_fill import _is_effectively_unset_field_value as _impl
    return _impl(value)


def _looks_like_internal_widget_value(value: str | None) -> bool:
    from ghosthands.actions.domhand_fill import _looks_like_internal_widget_value as _impl
    return _impl(value)


def _is_non_guess_name_fragment(field_name: str | None) -> bool:
    from ghosthands.actions.domhand_fill import _is_non_guess_name_fragment as _impl
    return _impl(field_name)


def _strip_required_marker(label: str | None) -> str:
    from ghosthands.actions.domhand_fill import _strip_required_marker as _impl
    return _impl(label)


def _field_has_effective_value(field: FormField) -> bool:
    from ghosthands.dom.fill_executor import _field_has_effective_value as _impl
    return _impl(field)


def _is_binary_value_text(value: str | None) -> bool:
    from ghosthands.actions.domhand_fill import _is_binary_value_text as _impl
    return _impl(value)


def _trace_profile_resolution(event: str, *, field_label: str, **extra: Any) -> None:
    from ghosthands.actions.domhand_fill import _trace_profile_resolution as _impl
    return _impl(event, field_label=field_label, **extra)


def _SELECT_PLACEHOLDER_FRAGMENT_RE_getter():
    from ghosthands.actions.domhand_fill import _SELECT_PLACEHOLDER_FRAGMENT_RE
    return _SELECT_PLACEHOLDER_FRAGMENT_RE


def _profile_debug_preview(value: Any) -> str:
    from ghosthands.actions.domhand_fill import _profile_debug_preview as _impl
    return _impl(value)


def _get_FocusFieldSelection():
    from ghosthands.actions.domhand_fill import FocusFieldSelection
    return FocusFieldSelection


def _normalize_bool_text(value: Any) -> str | None:
    """Convert bool-like values to a stable Yes/No string when possible."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    norm = normalize_name(text)
    if norm in {"yes", "y", "true", "checked", "1"}:
        return "Yes"
    if norm in {"no", "n", "false", "unchecked", "0"}:
        return "No"
    return text


def _normalize_yes_no_answer(answer: str | None) -> str | None:
    """Collapse affirmative/negative answer variants to Yes/No when possible."""
    if not answer:
        return None
    norm = normalize_name(answer)
    if not norm:
        return None
    if re.search(r"\b(no|not|false|unchecked|decline|never|none)\b", norm):
        return "No"
    if re.search(r"\b(yes|true|checked|citizen|authorized|eligible|available)\b", norm):
        return "Yes"
    return None


def _normalize_binary_match_value(value: str | None) -> str | None:
    """Normalize strict binary control values to Yes/No for verification."""
    if value in (None, ""):
        return None
    norm = normalize_name(value)
    if not norm:
        return None
    if norm in {"yes", "y", "true", "checked", "on", "1"}:
        return "Yes"
    if norm in {"no", "n", "false", "unchecked", "off", "0"}:
        return "No"

    if re.search(r"\b(do not|dont|don t|disagree|decline|reject)\b", norm):
        return "No"
    if re.search(r"\b(acknowledge|agree|accept|consent|confirm)\b", norm):
        return "Yes"
    return None


def _choice_words(text: str) -> set[str]:
    """Return a normalized word set for fuzzy option matching."""
    stop_words = {"the", "a", "an", "of", "for", "in", "to", "and", "or", "your", "my"}
    return {word for word in normalize_name(text).split() if len(word) > 2 and word not in stop_words}


def _stem_word(word: str) -> str:
    """Apply a lightweight stemmer for fuzzy question/choice matching."""
    return re.sub(
        r"(ating|ting|ing|tion|sion|m" r"ent|ness|able|ible|ed|ly|er|est|ies|es|s)$",
        "",
        word,
        flags=re.IGNORECASE,
    )


def _normalize_match_label(text: str) -> str:
    """Normalize a field label for confidence scoring and answer lookup."""
    raw = normalize_name(text or "")
    raw = re.sub(r"\s+#\d+\s*$", "", raw)
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _label_match_words(text: str) -> set[str]:
    """Return normalized label words including short domain words like ZIP."""
    return {
        word
        for word in _normalize_match_label(text).split()
        if word and word not in {"the", "a", "an", "of", "for", "in", "to", "and", "or", "your", "my"}
    }


def _label_match_confidence(label: str, candidate: str) -> str | None:
    """Classify how confidently two field labels refer to the same concept."""
    label_norm = _normalize_match_label(label)
    candidate_norm = _normalize_match_label(candidate)
    if not label_norm or not candidate_norm:
        return None
    if label_norm == candidate_norm:
        return "exact"

    label_words = _label_match_words(label)
    candidate_words = _label_match_words(candidate)
    if not label_words or not candidate_words:
        return None

    overlap_words = label_words & candidate_words
    smaller_size = min(len(label_words), len(candidate_words))
    overlap_ratio = len(overlap_words) / smaller_size if smaller_size else 0.0
    if smaller_size >= 2 and overlap_ratio >= 1.0:
        return "strong"
    if smaller_size >= 3 and overlap_ratio >= 0.75:
        return "strong"

    if smaller_size == 1 and overlap_ratio >= 1.0:
        single_word = next(iter(overlap_words))
        if len(single_word) >= 4 and single_word not in _GENERIC_SINGLE_WORD_LABELS:
            if max(len(label_words), len(candidate_words)) <= 2:
                return "strong"
            return "medium"

    if smaller_size >= 2 and overlap_ratio >= 0.6:
        return "medium"

    label_stems = {_stem_word(word) for word in label_words}
    candidate_stems = {_stem_word(word) for word in candidate_words}
    stem_overlap = label_stems & candidate_stems
    stem_ratio = (
        len(stem_overlap) / min(len(label_stems), len(candidate_stems)) if label_stems and candidate_stems else 0.0
    )
    if len(stem_overlap) >= 2 and stem_ratio >= 0.75:
        return "medium"
    if len(stem_overlap) >= 2:
        return "weak"

    if label_norm in candidate_norm or candidate_norm in label_norm:
        shorter = min(label_norm, candidate_norm, key=len)
        if len(shorter) >= 8:
            return "medium"
        return "weak"

    return None


def _meets_match_confidence(confidence: str | None, minimum_confidence: str) -> bool:
    """Return True when the detected match confidence clears the required bar."""
    if not confidence:
        return False
    return _MATCH_CONFIDENCE_RANKS.get(confidence, 0) >= _MATCH_CONFIDENCE_RANKS.get(minimum_confidence, 0)


def _proficiency_rank(text: str) -> int | None:
    norm = normalize_name(text)
    if not norm:
        return None
    if any(token in norm for token in ("native", "bilingual", "mother tongue", "expert", "master")):
        return 6
    if any(token in norm for token in ("fluent", "full professional", "professional working")):
        return 5
    if any(
        token in norm
        for token in (
            "advanced",
            "proficient",
        )
    ):
        return 4
    if any(token in norm for token in ("intermediate", "conversational", "working knowledge", "working")):
        return 3
    if any(token in norm for token in ("elementary", "basic", "beginner", "novice", "limited")):
        return 1
    return None


def _coerce_proficiency_choice(choices: list[str], answer: str) -> str | None:
    answer_rank = _proficiency_rank(answer)
    if answer_rank is None:
        return None

    ranked_choices = [(choice, _proficiency_rank(choice)) for choice in choices]
    ranked_choices = [(choice, rank) for choice, rank in ranked_choices if rank is not None]
    if not ranked_choices:
        return None

    best_choice: str | None = None
    best_distance: int | None = None
    best_rank = -1
    for choice, rank in ranked_choices:
        distance = abs(answer_rank - rank)
        if best_distance is None or distance < best_distance or (distance == best_distance and rank > best_rank):
            best_choice = choice
            best_distance = distance
            best_rank = rank

    return best_choice


def _select_extractions_look_like_pre_open_noise(field: FormField, choices: list[str]) -> bool:
    """True only when a single extracted option is clearly not a real multi-option menu yet.

    All usual coercion paths (exact match, Yes/No option match, substring, word overlap) run
    first; this is an **append-only** fallback for React-select / Greenhouse-style extractions
    that list the question (or a placeholder) as the sole row before the menu opens.
    """
    if len(choices) != 1:
        return False
    only = (choices[0] or "").strip()
    if not only:
        return True
    if is_placeholder_value(only):
        return True
    if _SELECT_PLACEHOLDER_FRAGMENT_RE_getter().search(only):
        return True
    for cand in _field_label_candidates(field):
        c_norm = normalize_name(cand)
        o_norm = normalize_name(only)
        if c_norm and o_norm and (c_norm == o_norm or c_norm in o_norm or o_norm in c_norm):
            return True
    return False


_PHONE_LINE_TYPE_TOKENS_NORM: frozenset[str] = frozenset(
    {
        "mobile",
        "home",
        "work",
        "cell",
        "office",
        "business",
        "landline",
        "fax",
        "other",
        "mobile phone",
        "cell phone",
        "home phone",
        "work phone",
    }
)


def _answer_is_phone_line_type_token(text: str) -> bool:
    """True when the answer is only a phone *line/device* label, not a numeric phone number."""
    t = normalize_name(text)
    return bool(t and t in _PHONE_LINE_TYPE_TOKENS_NORM)


def _field_accepts_phone_digits_not_line_type(field: FormField) -> bool:
    """True for <tel> and plain phone-number text fields — not device-type / country-code pickers."""
    if field.field_type == "tel":
        return True
    if field.field_type not in {"text", "search"}:
        return False
    lab = normalize_name(" ".join(_field_label_candidates(field)))
    if any(
        p in lab
        for p in (
            "phone device",
            "phone type",
            "device type",
            "line type",
            "method of contact",
        )
    ):
        return False
    if "country" in lab and "phone" in lab:
        return False
    return bool(
        "phone" in lab
        or "mobile number" in lab
        or "cell number" in lab
        or "telephone number" in lab
        or "contact number" in lab
    )


def _normalize_degree_family_answer(answer: str | None) -> str | None:
    norm = normalize_name(answer or "")
    if not norm:
        return None
    compact = re.sub(r"[^a-z0-9]+", "", norm)
    if any(token in norm for token in ("associate", "associates")):
        return "Associates"
    if any(
        token in norm
        for token in (
            "bachelor",
            "bachelors",
            "bs",
            "b s",
            "ba",
            "b a",
            "bsc",
            "b sc",
            "bachelor of science",
            "bachelor of arts",
        )
    ) or compact in {"bs", "ba", "bsc"}:
        return "Bachelors"
    if any(
        token in norm
        for token in (
            "master",
            "masters",
            "ms",
            "m s",
            "ma",
            "m a",
            "msc",
            "m sc",
            "mba",
            "master of science",
            "master of arts",
        )
    ) or compact in {"ms", "ma", "msc", "mba"}:
        return "Masters"
    if any(
        token in norm
        for token in (
            "doctor",
            "doctorate",
            "doctoral",
            "phd",
            "ph d",
            "dphil",
            "d phil",
            "jd",
            "j d",
            "md",
            "m d",
        )
    ) or compact in {"phd", "dphil", "jd", "md"}:
        return "Doctorate"
    return None


def _matches_degree_family_choice(choice: str, degree_family_answer: str | None) -> bool:
    """Return True when a select option belongs to the requested degree family."""
    if not choice or not degree_family_answer:
        return False

    choice_norm = normalize_name(choice)
    family_norm = normalize_name(degree_family_answer)
    if not choice_norm or not family_norm:
        return False

    if choice_norm == family_norm or family_norm in choice_norm or choice_norm in family_norm:
        return True

    choice_compact = re.sub(r"[^a-z0-9]+", "", choice_norm)
    family_compact = re.sub(r"[^a-z0-9]+", "", family_norm)
    if not choice_compact or not family_compact:
        return False

    if (
        choice_compact == family_compact
        or family_compact in choice_compact
        or choice_compact in family_compact
    ):
        return True

    family_word_markers = {
        "associates": {"associate", "associates"},
        "bachelors": {"bachelor", "bachelors"},
        "masters": {"master", "masters"},
        "doctorate": {"doctor", "doctorate", "doctoral"},
    }
    if any(marker in choice_norm for marker in family_word_markers.get(family_compact, set())):
        return True

    family_exact_aliases = {
        "associates": {"aa", "as", "aas"},
        "bachelors": {"ba", "bs", "bsc", "beng", "be"},
        "masters": {"ma", "ms", "msc", "meng", "mba", "me"},
        "doctorate": {"phd", "dphil", "jd", "md"},
    }
    return choice_compact in family_exact_aliases.get(family_compact, set())


def _coerce_answer_to_field(field: FormField, answer: str | None) -> str | None:
    """Map a profile answer onto the closest available field option when present."""
    if answer in (None, ""):
        return None
    text = str(answer).strip()
    if not text:
        return None

    # LLM/prompt often says default *device type* to "Mobile"; that must never become the <tel> value.
    # Also react-select noise lists include "Mobile" as an option — do not snap the number field to it.
    if _field_accepts_phone_digits_not_line_type(field) and _answer_is_phone_line_type_token(text):
        return None

    degree_like_label = any(
        normalize_name(candidate) in {"degree", "degree type", "type of degree", "degree level"}
        for candidate in _field_label_candidates(field)
    )
    degree_family_answer = _normalize_degree_family_answer(text) if degree_like_label else None

    # Workday skills/selectinput widgets intentionally accept comma-joined
    # multi-value answers and split them later inside ``_fill_multi_select``.
    # Treating them like a single-option select drops the answer before fill.
    from ghosthands.actions.domhand_fill import _is_skill_like as _is_skill_like_impl

    if _is_skill_like_impl(_preferred_field_label(field) or field.name or ""):
        return text

    latest_employer_label = any(
        any(
            token in normalize_name(candidate)
            for token in (
                "latest employer",
                "current employer",
                "most recent employer",
                "name of latest employer",
                "name of current employer",
                "name of most recent employer",
            )
        )
        for candidate in _field_label_candidates(field)
    )

    choices = [str(choice).strip() for choice in (field.options or field.choices or []) if str(choice).strip()]
    if not choices:
        if field.field_type == "select" and latest_employer_label:
            return None
        if field.field_type == "select" and degree_family_answer:
            return degree_family_answer
        return text

    text_norm = normalize_name(text)
    for choice in choices:
        if normalize_name(choice) == text_norm:
            return choice

    if degree_family_answer:
        for choice in choices:
            if _matches_degree_family_choice(choice, degree_family_answer):
                return choice

    boolish = _normalize_yes_no_answer(text)
    if boolish:
        for choice in choices:
            if normalize_name(choice) == normalize_name(boolish):
                return choice
        for choice in choices:
            if _normalize_binary_match_value(choice) == boolish:
                return choice

    proficiency_choice = _coerce_proficiency_choice(choices, text)
    if proficiency_choice:
        _trace_profile_resolution(
            "domhand.profile_proficiency_coerced",
            field_label=field.name or field.raw_label or "",
            requested=_profile_debug_preview(text),
            selected=_profile_debug_preview(proficiency_choice),
        )
        return proficiency_choice

    for choice in choices:
        choice_norm = normalize_name(choice)
        if choice_norm and (choice_norm in text_norm or text_norm in choice_norm):
            return choice

    text_words = _choice_words(text)
    text_stems = {_stem_word(word) for word in text_words}
    best_choice: str | None = None
    best_score = 0
    for choice in choices:
        choice_words = _choice_words(choice)
        score = len(text_words & choice_words) * 2
        score += len(text_stems & {_stem_word(word) for word in choice_words})
        if score > best_score:
            best_score = score
            best_choice = choice

    if best_choice and best_score > 0:
        return best_choice

    # Lever / modern ATS UIs often attach noisy option lists to plain inputs (placeholders,
    # autocomplete shards, etc.). For free-text controls, keep the model/profile answer.
    if field.field_type in {
        "text",
        "email",
        "tel",
        "url",
        "number",
        "password",
        "search",
        "textarea",
        "date",
    }:
        return text

    # Radio / checkbox groups: Yes/No passes through — DOM fill can click the right control.
    if field.field_type in {"radio-group", "radio", "checkbox-group", "checkbox", "toggle"} and _is_binary_value_text(
        text
    ):
        return text

    # Custom/React <select>: append-only fallback when extraction is pre-open noise (never for
    # a real single-option or multi-option semantic list — those are handled above).
    if (
        field.field_type == "select"
        and _is_binary_value_text(text)
        and _select_extractions_look_like_pre_open_noise(field, choices)
    ):
        return text

    return None


def _humanize_name_attr(name_attr: str) -> str:
    """Turn camelCase/snake HTML names into spaced words for label matching (e.g. addressLine1 -> address Line 1)."""
    raw = str(name_attr or "").strip()
    if not raw:
        return ""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw.replace("_", " "))
    spaced = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", spaced)
    return re.sub(r"\s+", " ", spaced).strip()


def _field_label_candidates(field: FormField) -> list[str]:
    """Return deduplicated field labels ordered from most to least descriptive."""
    seen: set[str] = set()
    candidates: list[str] = []
    humanized = _humanize_name_attr(field.name_attr or "")
    for label in (field.raw_label, field.name, humanized if humanized else None):
        if label is None:
            continue
        cleaned = str(label or "").strip()
        variants = [cleaned]
        stripped = _strip_required_marker(cleaned)
        if stripped and stripped != cleaned:
            variants.append(stripped)
        for variant in variants:
            key = normalize_name(variant)
            if not variant or not key or key in seen:
                continue
            seen.add(key)
            candidates.append(variant)
    return candidates


def _preferred_field_label(field: FormField) -> str:
    """Choose the best human-readable label for prompts and matching."""
    candidates = _field_label_candidates(field)
    if candidates:
        return candidates[0]
    return (field.name or field.raw_label or "").strip()


def _canonical_section_name(value: str | None) -> str:
    normalized = normalize_name(value or "")
    # Normalize `/` separators (Oracle uses "College / University")
    normalized = normalized.replace(" / ", " ").replace("/", " ")
    normalized = " ".join(normalized.split())
    replacements = {
        "my information": "information",
        "personal information": "information",
        "my experience": "experience",
        "work experience": "experience",
        "professional experience": "experience",
        "my education": "education",
        "education history": "education",
        "self identify": "self identify",
        "self identification": "self identify",
        "self-identification": "self identify",
        "voluntary self identification": "self identify",
        "voluntary self-identification": "self identify",
        "voluntary self identification of disability": "self identify",
        "voluntary self-identification of disability": "self identify",
        # Oracle HCM repeater section headings
        "college university": "education",
        "college and university": "education",
        "technical skills": "skills",
        "language skills": "languages",
        "licenses and certificates": "licenses",
        "licenses certificates": "licenses",
        "certifications and licenses": "licenses",
        "work history": "experience",
    }
    return replacements.get(normalized, normalized)


_SECTION_SCOPE_CHILDREN: dict[str, set[str]] = {
    # Workday nests Education / Languages under the page-level "My Experience"
    # step. Oracle HCM similarly nests "College / University", "Technical Skills",
    # "Language Skills" etc. under the page-level "Experience" step.
    "experience": {
        "education",
        "languages",
        "language",
        "skills",
        "certifications",
        "licenses",
        # Oracle HCM sub-section names (post-canonicalization)
        "college university",
        "technical skills",
        "language skills",
        "licenses and certificates",
        "licenses certificates",
        "work history",
    },
    # Workday also nests address/phone/legal-name groups under the page-level
    # "My Information" step.
    "information": {
        "address",
        "phone",
        "legal name",
        "name",
        "contact",
        "contact information",
        "how did you hear",
        "referral source",
        "source",
    },
    # Workday keeps the terms acknowledgment under Voluntary Disclosures.
    "voluntary disclosures": {"terms and conditions", "terms conditions"},
    # Oracle HCM parent scopes
    "education": {"college university", "college and university"},
    "skills": {"technical skills"},
    "languages": {"language skills"},
    "licenses": {"licenses and certificates", "licenses certificates", "certifications and licenses"},
}


def _normalize_separator(value: str) -> str:
    """Normalize `/` separators and collapse whitespace (pre-alias step)."""
    return " ".join(normalize_name(value or "").replace(" / ", " ").replace("/", " ").split())


def _section_matches_scope(section: str | None, scope: str | None) -> bool:
    """Return True when a field section matches a requested scope/boundary."""
    section_norm = _canonical_section_name(section)
    scope_norm = _canonical_section_name(scope)
    if not scope_norm:
        return True
    if not section_norm:
        return False
    if section_norm == scope_norm or scope_norm in section_norm or section_norm in scope_norm:
        return True
    child_sections = _SECTION_SCOPE_CHILDREN.get(scope_norm)
    if child_sections and section_norm in child_sections:
        return True
    # Also check the pre-alias `/`-normalized form against child sets
    section_raw_norm = _normalize_separator(section)
    if child_sections and section_raw_norm and section_raw_norm in child_sections:
        return True
    section_tokens = {token for token in section_norm.split() if token not in {"my", "work", "personal"}}
    scope_tokens = {token for token in scope_norm.split() if token not in {"my", "work", "personal"}}
    section_numbers = {token for token in section_tokens if token.isdigit()}
    scope_numbers = {token for token in scope_tokens if token.isdigit()}
    if section_numbers and scope_numbers and section_numbers != scope_numbers:
        return False
    overlap = section_tokens & scope_tokens
    if not overlap:
        return False
    smaller = min(len(section_tokens), len(scope_tokens))
    if smaller <= 1:
        return True
    return len(overlap) >= 2


def _merge_focus_matched_fields_across_sections(
    filtered: list[FormField],
    all_fields: list[FormField],
    *,
    target_section: str | None,
    focus_fields: list[str] | None,
) -> list[FormField]:
    """When focus_fields target specific labels, include matching fields even if their section disagrees with target_section.

    Oracle HCM / standard apply flow often sets a page-level section (e.g. \"Job application form\") on some fields
    while address/contact blocks keep a distinct section (\"Contact\"). A strict section filter would drop those
    fields before focus resolution, so domhand_fill(..., focus_fields=[\"Address Line 1\"]) could not see them.
    """
    if not target_section or not focus_fields:
        return filtered
    normalized_focus = [_normalize_match_label(label) for label in focus_fields if _normalize_match_label(label)]
    if not normalized_focus:
        return filtered
    existing_ids = {f.field_id for f in filtered}
    extras: list[FormField] = []
    for field in all_fields:
        if field.field_id in existing_ids:
            continue
        if not any(_field_matches_focus_label(field, fl) for fl in normalized_focus):
            continue
        extras.append(field)
        existing_ids.add(field.field_id)
    if not extras:
        return filtered
    logger.debug(
        "DomHand scope: merged focus-matched fields outside target_section",
        extra={
            "target_section": target_section,
            "focus_fields": focus_fields,
            "merged_field_ids": [f.field_id for f in extras],
        },
    )
    return [*filtered, *extras]


def _filter_fields_for_scope(
    fields: list[FormField],
    target_section: str | None = None,
    heading_boundary: str | None = None,
    focus_fields: list[str] | None = None,
    *,
    allow_all_visible_fallback: bool = True,
) -> list[FormField]:
    """Restrict fields to a section and/or repeater entry boundary."""
    original_fields = fields
    filtered = fields
    if target_section:
        section_filtered = [f for f in filtered if _section_matches_scope(f.section, target_section)]
        if section_filtered:
            if not heading_boundary:
                blank_section_fields = [f for f in filtered if not normalize_name(f.section or "")]
                if blank_section_fields:
                    normalized_focus = [
                        _normalize_match_label(label) for label in (focus_fields or []) if _normalize_match_label(label)
                    ]
                    if normalized_focus:
                        blank_section_fields = [
                            field
                            for field in blank_section_fields
                            if any(_field_matches_focus_label(field, focus_label) for focus_label in normalized_focus)
                        ]
                    merged: list[FormField] = []
                    seen_ids: set[str] = set()
                    for field in [*section_filtered, *blank_section_fields]:
                        if field.field_id in seen_ids:
                            continue
                        seen_ids.add(field.field_id)
                        merged.append(field)
                    filtered = merged
                    logger.debug(
                        "DomHand scope kept blank-section fields with matching target section",
                        extra={
                            "target_section": target_section,
                            "matched_count": len(section_filtered),
                            "blank_section_count": len(blank_section_fields),
                            "result_count": len(filtered),
                        },
                    )
                else:
                    filtered = section_filtered
            else:
                filtered = section_filtered
        elif not heading_boundary and allow_all_visible_fallback:
            logger.info(
                "DomHand scope fallback: no fields matched target section, using all visible fields",
                extra={"target_section": target_section, "field_count": len(fields)},
            )
        elif not heading_boundary:
            filtered = []
            logger.debug(
                "DomHand scope found no live match for target section without fallback",
                extra={"target_section": target_section, "field_count": len(fields)},
            )
    filtered = _merge_focus_matched_fields_across_sections(
        filtered,
        original_fields,
        target_section=target_section,
        focus_fields=focus_fields,
    )
    if heading_boundary:
        filtered = [f for f in filtered if _section_matches_scope(f.section, heading_boundary)]
    return filtered


_FOCUS_BOOLEAN_FIELD_TYPES = {
    "checkbox",
    "checkbox-group",
    "radio",
    "radio-group",
    "toggle",
    "button-group",
}
_FOCUS_DATA_FIELD_TYPES = {
    "text",
    "textarea",
    "email",
    "password",
    "tel",
    "search",
    "number",
    "date",
    "select",
}


def _field_matches_focus_label(field: FormField, focus_label: str) -> bool:
    labels = _field_label_candidates(field) or [field.name]
    for candidate in labels:
        confidence = _label_match_confidence(candidate, focus_label)
        if _meets_match_confidence(confidence, "medium"):
            return True
        reverse_confidence = _label_match_confidence(focus_label, candidate)
        if _meets_match_confidence(reverse_confidence, "medium"):
            return True
    return False


def _focus_field_priority(field: FormField) -> int:
    if field.field_type in _FOCUS_DATA_FIELD_TYPES:
        return 30
    if field.field_type in {"radio-group", "button-group"}:
        return 20
    if field.field_type in {"checkbox-group"}:
        return 15
    if field.field_type in _FOCUS_BOOLEAN_FIELD_TYPES:
        return 10
    return 5


def _prune_focus_companion_controls(fields: list[FormField]) -> list[FormField]:
    has_data_field = any(field.field_type in _FOCUS_DATA_FIELD_TYPES for field in fields)
    if not has_data_field:
        return fields
    pruned = [field for field in fields if field.field_type not in _FOCUS_BOOLEAN_FIELD_TYPES]
    return pruned or fields


def _resolve_focus_fields(
    fields: list[FormField],
    focus_fields: list[str] | None = None,
) -> FocusFieldSelection:
    _FFS = _get_FocusFieldSelection()
    if not focus_fields:
        return _FFS(fields=fields, ambiguous_labels={})

    normalized_pairs = [
        (label, _normalize_match_label(label)) for label in focus_fields if _normalize_match_label(label)
    ]
    if not normalized_pairs:
        return _FFS(fields=fields, ambiguous_labels={})

    focused: list[FormField] = []
    seen_ids: set[str] = set()
    ambiguous_labels: dict[str, list[FormField]] = {}

    for original_label, normalized_label in normalized_pairs:
        matches = [field for field in fields if _field_matches_focus_label(field, normalized_label)]
        if not matches:
            continue
        matches = _prune_focus_companion_controls(matches)
        ranked = sorted(matches, key=_focus_field_priority, reverse=True)
        if len(ranked) == 1:
            if ranked[0].field_id not in seen_ids:
                seen_ids.add(ranked[0].field_id)
                focused.append(ranked[0])
            continue

        top_priority = _focus_field_priority(ranked[0])
        top_matches = [field for field in ranked if _focus_field_priority(field) == top_priority]
        if len(top_matches) > 1:
            ambiguous_labels[original_label] = top_matches
            continue

        winner = top_matches[0]
        if winner.field_id not in seen_ids:
            seen_ids.add(winner.field_id)
            focused.append(winner)

    return _FFS(fields=focused, ambiguous_labels=ambiguous_labels)


def _filter_fields_for_focus(fields: list[FormField], focus_fields: list[str] | None = None) -> list[FormField]:
    """Restrict fields to explicit blocker labels when the agent knows them."""
    resolved = _resolve_focus_fields(fields, focus_fields)
    if resolved.fields:
        return resolved.fields

    logger.info(
        "DomHand focus mismatch: no fields matched focus_fields",
        extra={
            "focus_fields": focus_fields,
            "field_count": len(fields),
            "available_fields": [
                {
                    "field_id": field.field_id,
                    "label": _preferred_field_label(field),
                    "field_type": field.field_type,
                    "section": field.section,
                    "current_value": field.current_value,
                }
                for field in fields[:20]
            ],
        },
    )
    return []


def _active_blocker_focus_fields(
    browser_session: BrowserSession,
    *,
    fields: list[FormField],
    page_context_key: str,
    page_url: str,
    focus_fields: list[str] | None = None,
) -> tuple[list[FormField], bool]:
    if focus_fields:
        return fields, False

    last_state = getattr(browser_session, "_gh_last_application_state", None)
    if not isinstance(last_state, dict):
        return fields, False
    if last_state.get("page_context_key") and last_state.get("page_context_key") != page_context_key:
        return fields, False
    if page_url and last_state.get("page_url") and last_state.get("page_url") != page_url:
        return fields, False

    blocker_ids = {str(value).strip() for value in (last_state.get("blocking_field_ids") or []) if str(value).strip()}
    blocker_keys = {str(value).strip() for value in (last_state.get("blocking_field_keys") or []) if str(value).strip()}
    blocker_labels = {
        normalize_name(str(value)) for value in (last_state.get("blocking_field_labels") or []) if str(value).strip()
    }
    if not blocker_ids and not blocker_keys and not blocker_labels:
        return fields, False

    filtered = [
        field
        for field in fields
        if field.field_id in blocker_ids
        or get_stable_field_key(field) in blocker_keys
        or normalize_name(_preferred_field_label(field)) in blocker_labels
    ]
    if not filtered:
        return [], False

    blocker_state_changes = {
        str(key).strip(): str(value).strip()
        for key, value in (last_state.get("blocking_field_state_changes") or {}).items()
        if str(key).strip()
    }
    filtered_keys = [get_stable_field_key(field) for field in filtered]
    all_no_state_change = bool(filtered_keys) and all(
        blocker_state_changes.get(key) == "no_state_change" for key in filtered_keys
    )
    return filtered, all_no_state_change
