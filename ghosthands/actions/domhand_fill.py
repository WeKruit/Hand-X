"""DomHand Fill — the core action that extracts form fields, generates answers via
a single cheap LLM call, and fills everything via CDP DOM manipulation.

Ported from GHOST-HANDS formFiller.ts.  This is the primary workhorse action for
job application form filling.  It:

1. Injects browser-side helper library (``__ff``) into the page
2. Extracts ALL visible form fields including radio/checkbox groups and button groups
3. Makes a SINGLE cheap LLM call (Haiku) with resume profile + all fields -> answer map
4. Fills each field via the appropriate strategy:
   - Native selects      -> CDP ``HTMLSelectElement.value`` + change event
   - Custom dropdowns     -> click trigger, discover options, click match
   - Searchable combos    -> type to filter, click matching option
   - Radio/checkbox groups -> click the matching item
   - Button groups        -> click the button whose text matches
   - Text/email/tel/etc.  -> native setter + input/change/blur events
   - Textareas            -> same, or ``textContent`` for contenteditable
   - Date fields          -> direct set or Workday-style keyboard fill
   - Checkboxes/toggles   -> click to toggle state
5. Re-extracts to verify fills and catch newly revealed conditional fields
6. Repeats for up to ``MAX_FILL_ROUNDS`` rounds
7. Returns ``ActionResult`` with filled/failed/unfilled counts
"""

import asyncio
import contextlib
import inspect
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from ghosthands.actions.combobox_toggle import (
    CLICK_COMBOBOX_TOGGLE_BY_FFID_JS,
    CLICK_INPUT_BY_FFID_JS,
    combobox_toggle_clicked,
)
from ghosthands.actions.views import (
    DomHandFillParams,
    FillFieldResult,
    FormField,
    generate_dropdown_search_terms,
    get_stable_field_key,
    is_placeholder_value,
    normalize_name,
    split_dropdown_value_hierarchy,
)
from ghosthands.bridge.profile_adapter import camel_to_snake_profile
from ghosthands.dom.dropdown_fill import DropdownFillResult, fill_interactive_dropdown
from ghosthands.dom.dropdown_match import (
    CLICK_DROPDOWN_OPTION_ENHANCED_JS,
    SCAN_VISIBLE_OPTIONS_JS,
    match_dropdown_option,
    synonym_groups_for_js,
)
from ghosthands.dom.dropdown_verify import selection_matches_desired
from ghosthands.dom.fill_profile_resolver import (
    _available_semantic_intent_answers,
    _build_profile_answer_map,
    _cap_qa_entries,
    _classify_known_intent_for_field,
    _coerce_answer_if_compatible,
    _default_answer_mode_for_field,
    _default_screening_answer,
    _default_value,
    _entry_text_value,
    _extract_named_employer_from_question,
    _field_conditional_cluster,
    _field_option_norms,
    _field_section_name,
    _field_widget_kind_for_debug,
    _find_best_profile_answer,
    _format_entry_profile_text,
    _get_nested_profile_value,
    _is_binary_value_text,
    _is_detail_child_prompt,
    _is_employer_history_boolean_prompt,
    _is_employer_history_screening_question,
    _is_required_custom_widget_boolean_select,
    _known_entry_value,
    _known_entry_value_for_field,
    _known_profile_value,
    _known_profile_value_for_field,
    _label_starts_boolean_question,
    _log_answer_resolution,
    _log_boolean_widget_classification,
    _match_confidence_score,
    _match_qa_answer,
    _normalize_qa_text,
    _normalized_employer_tokens,
    _parse_profile_evidence,
    _profile_has_employer_history,
    _resolve_known_profile_value_for_field,
    _resolve_semantic_intent_answer,
    _resolved_field_value,
    _resolved_field_value_if_compatible,
    _semantic_profile_value_for_field,
    _value_shape_is_compatible,
)
from ghosthands.dom.fill_label_match import (
    _GENERIC_SINGLE_WORD_LABELS,
    _MATCH_CONFIDENCE_RANKS,
    _SECTION_SCOPE_CHILDREN,
    _active_blocker_focus_fields,
    _answer_is_phone_line_type_token,
    _canonical_section_name,
    _choice_words,
    _coerce_answer_to_field,
    _coerce_proficiency_choice,
    _field_accepts_phone_digits_not_line_type,
    _field_label_candidates,
    _field_matches_focus_label,
    _filter_fields_for_focus,
    _filter_fields_for_scope,
    _focus_field_priority,
    _humanize_name_attr,
    _label_match_confidence,
    _label_match_words,
    _meets_match_confidence,
    _merge_focus_matched_fields_across_sections,
    _normalize_binary_match_value,
    _normalize_bool_text,
    _normalize_match_label,
    _normalize_yes_no_answer,
    _preferred_field_label,
    _proficiency_rank,
    _prune_focus_companion_controls,
    _resolve_focus_fields,
    _section_matches_scope,
    _select_extractions_look_like_pre_open_noise,
    _stem_word,
)
from ghosthands.dom.fill_executor import (
    _EXCLUSIVE_CHOICE_CHECKBOX_PROMPT_RE,
    _EXCLUSIVE_CHOICE_OPTION_PREFIXES,
    _MULTI_SELECT_CHECKBOX_PROMPT_RE,
    _SKILL_FIELD_MAX_ITEMS,
    _checkbox_group_is_exclusive_choice,
    _checkbox_group_mode,
    _click_away_from_text_like_field,
    _click_binary_with_gui,
    _click_dropdown_option,
    _click_group_option_with_gui,
    _clear_dropdown_search,
    _coerce_salary_numeric_candidate,
    _confirm_text_like_value,
    _field_has_effective_value,
    _field_has_validation_error,
    _field_needs_blur_revalidation,
    _field_needs_enter_commit,
    _field_value_matches_expected,
    _fill_checkbox,
    _fill_checkbox_group,
    _fill_button_group,
    _fill_custom_dropdown,
    _fill_custom_dropdown_cdp_first,
    _fill_date_field,
    _fill_grouped_date_field,
    _fill_multi_select,
    _fill_radio_group,
    _fill_searchable_dropdown,
    _fill_select_field,
    _fill_single_field,
    _fill_single_radio,
    _fill_text_field,
    _fill_text_like_with_keyboard,
    _fill_textarea_field,
    _fill_toggle,
    _find_dom_tree_node_by_ff_id,
    _get_binary_click_target,
    _grouped_date_is_complete,
    _is_date_component_field,
    _is_grouped_date_field,
    _is_salary_like_field,
    _is_self_identify_date_field,
    _load_field_interaction_recipe,
    _open_grouped_date_picker,
    _parse_dropdown_click_result,
    _parse_full_date_value,
    _poll_click_dropdown_option,
    _read_binary_state,
    _read_checkbox_group_value,
    _read_field_value,
    _read_field_value_for_field,
    _read_group_selection,
    _record_field_interaction_recipe,
    _refresh_binary_field,
    _reset_group_selection_with_gui,
    _scan_visible_dropdown_options,
    _select_grouped_date_from_picker,
    _settle_dropdown_selection,
    _text_fill_attempt_values,
    _try_open_combobox_menu,
    _type_and_click_dropdown_option,
    _visible_field_id_snapshot,
    _wait_for_field_value,
)
from ghosthands.dom.fill_browser_scripts import (
    _CLICK_ALTERNATE_FIELD_JS,
    _CLICK_BINARY_FIELD_JS,
    _CLICK_BUTTON_GROUP_JS,
    _CLICK_CHECKBOX_GROUP_JS,
    _CLICK_DROPDOWN_OPTION_JS,
    _CLICK_OTHER_TEXTLIKE_FIELD_JS,
    _CLICK_RADIO_OPTION_JS,
    _CLICK_SINGLE_RADIO_JS,
    _DISMISS_DROPDOWN_JS,
    _ELEMENT_EXISTS_JS,
    _EXTRACT_FIELDS_JS,
    _FILL_CONTENTEDITABLE_JS,
    _FILL_DATE_JS,
    _FILL_FIELD_JS,
    _FOCUS_AND_CLEAR_JS,
    _FOCUS_FIELD_JS,
    _GET_BINARY_CLICK_TARGET_JS,
    _GET_GROUP_OPTION_TARGET_JS,
    _HAS_FIELD_VALIDATION_ERROR_JS,
    _IS_SEARCHABLE_DROPDOWN_JS,
    _OPEN_GROUPED_DATE_PICKER_JS,
    _PAGE_CONTEXT_SCAN_JS,
    _READ_BINARY_STATE_JS,
    _READ_FIELD_VALUE_JS,
    _READ_GROUP_SELECTION_JS,
    _REVEAL_SECTIONS_JS,
    _SELECT_GROUPED_DATE_PICKER_VALUE_JS,
)
from ghosthands.dom.fill_resolution import (
    _education_slot_name,
    _entry_date_text_and_source,
    _entry_text_and_source,
    _extract_date_component,
    _field_binding_identity,
    _infer_entry_data_from_scope,
    _is_education_like_section,
    _is_structured_education_candidate,
    _is_structured_education_field,
    _is_structured_language_field,
    _language_entry_index_for_field,
    _language_slot_name,
    _match_entry_by_slot_value,
    _resolve_repeater_binding,
    _resolve_structured_language_value,
    _structured_education_raw_value_and_source_from_entry,
    _structured_education_raw_value_from_entry,
    _structured_education_value_from_entry,
    _structured_field_missing_reason,
    _structured_language_raw_value_and_source_from_entry,
    _structured_language_raw_value_from_entry,
    _structured_language_value_from_entry,
)
from ghosthands.dom.label_resolver import generate_field_fingerprint
from ghosthands.profile.canonical import build_canonical_profile
from ghosthands.runtime_learning import (
    DOMHAND_RETRY_CAP,
    SemanticQuestionIntent,
    build_page_context_key,
    cache_semantic_alias,
    clear_domhand_failure,
    confirm_learned_question_alias,
    detect_host_from_url,
    detect_platform_from_url,
    get_cached_semantic_alias,
    get_domhand_failure_count,
    get_interaction_recipe,
    get_learned_question_alias,
    get_repeater_field_binding,
    has_cached_semantic_alias,
    is_domhand_retry_capped,
    record_domhand_failure,
    record_expected_field_value,
    record_interaction_recipe,
    record_repeater_field_binding,
    stage_learned_question_alias,
)

logger = structlog.get_logger(__name__)

# ── Field event callback (set by CLI for JSONL emission) ─────────────
# When set, called with each FillFieldResult as it is created.
# Signature: (result: FillFieldResult, round_num: int) -> None
_on_field_result: Any = None  # Callable[[FillFieldResult, int], None] | None


@dataclass(frozen=True)
class ResolvedFieldValue:
    value: str
    source: str
    answer_mode: str | None
    confidence: float
    state: str = "filled"


@dataclass(frozen=True)
class ResolvedFieldBinding:
    entry_index: int
    binding_mode: str
    binding_confidence: str
    best_effort_guess: bool = False


@dataclass(frozen=True)
class FocusFieldSelection:
    fields: list[FormField]
    ambiguous_labels: dict[str, list[FormField]]


@dataclass
class StructuredRepeaterDiagnostic:
    repeater_group: str
    field_id: str
    field_label: str
    section: str
    slot_name: str | None = None
    numeric_index: int | None = None
    section_binding_reused: bool = False
    binding_mode: str | None = None
    binding_confidence: str | None = None
    entry_index: int | None = None
    current_value: str = ""
    resolved_value_preview: str = "EMPTY"
    resolved_source_key: str | None = None
    failure_stage: str = ""


def _field_name_attr_hint(raw_field: dict[str, Any]) -> str:
    return str(
        raw_field.get("name_attr")
        or raw_field.get("groupKey")
        or raw_field.get("questionLabel")
        or raw_field.get("itemValue")
        or ""
    ).strip()


def _ensure_field_fingerprint(field: FormField, *, name_attr_hint: str = "") -> FormField:
    if str(field.field_fingerprint or "").strip():
        return field
    label = _preferred_field_label(field) or field.name or field.raw_label or field.field_id
    field.field_fingerprint = generate_field_fingerprint(
        field.field_type or "unknown",
        label,
        field.section or "",
        name_attr_hint,
    )
    return field


# ── Constants ────────────────────────────────────────────────────────

MAX_FILL_ROUNDS = 3
MAX_CONDITIONAL_PASSES = 3
DEFAULT_HITL_TIMEOUT_SECONDS = int(os.getenv("GH_OPEN_QUESTION_TIMEOUT_SECONDS", "5400"))

# Selector for all interactive form elements (matches GH formFiller.ts).
INTERACTIVE_SELECTOR = ", ".join(
    [
        "input",
        "select",
        "textarea",
        '[role="textbox"]',
        '[role="combobox"]',
        '[role="listbox"]',
        '[role="checkbox"]',
        '[role="radio"]',
        '[role="switch"]',
        '[role="spinbutton"]',
        '[role="slider"]',
        '[role="searchbox"]',
        '[data-uxi-widget-type="selectinput"]',
        '[aria-haspopup="listbox"]',
    ]
)

# Regex for fields whose values should never be fabricated.
# Note: avoid matching bare "social" — company names like "Social Scientific Solutions"
# caused false positives and wiped legitimate Yes/No answers from screening dropdowns.
_SOCIAL_OR_ID_NO_GUESS_RE = re.compile(
    r"\b(twitter|x(\.com)?\s*(handle|username|profile)?|github|gitlab|linkedin"
    r"|instagram|tiktok|facebook|social\s+(media|network|profile)\b|handle|username|user\s*name"
    r"|passport|driver'?s?\s*license|license\s*number|national\s*id|id\s*number"
    r"|tax\s*id|itin|ein|ssn|social security)\b",
    re.IGNORECASE,
)
_OPAQUE_WIDGET_VALUE_RE = re.compile(
    r"^(?:[0-9a-f]{16,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$",
    re.IGNORECASE,
)
_SELECT_PLACEHOLDER_FRAGMENT_RE = re.compile(r"\b(select one|choose one|please select)\b", re.IGNORECASE)
_NAME_FRAGMENT_NO_GUESS_RE = re.compile(r"\b(suffix|preferred name|nickname)\b", re.IGNORECASE)
DOMHAND_RETRY_CAPPED_ERROR = (
    f"DomHand retry cap reached after {DOMHAND_RETRY_CAP} failed attempts. "
    "Use browser-use or one screenshot/vision fallback for this exact field."
)

# Navigation-like button labels to skip when detecting button groups.
_NAV_BUTTON_LABELS = frozenset(
    [
        "save and continue",
        "next",
        "continue",
        "submit",
        "submit application",
        "apply",
        "add",
        "add another",
        "replace",
        "upload",
        "browse",
        "remove",
        "delete",
        "cancel",
        "back",
        "previous",
        "close",
        "save",
        "select one",
        "choose file",
    ]
)

_PROFILE_TRACE_LABEL_TOKENS = (
    "salary",
    "compensation",
    "pay expectation",
    "how did you hear",
    "heard about",
    "referral source",
    "source of referral",
    "language",
    "reading",
    "writing",
    "speaking",
    "comprehension",
    "overall",
    "authorization",
    "authorized",
    "eligible to work",
    "sponsorship",
    "relocate",
    "country of residence",
    "notice period",
    "availability",
    "veteran",
    "disability",
    "gender",
    "race",
    "ethnicity",
    "worked at",
    "worked for",
    "employed by",
    "former employee",
    "government employee",
    "linkedin",
)


def _profile_debug_enabled() -> bool:
    return os.getenv("GH_DEBUG_PROFILE_PASS_THROUGH") == "1"


def _profile_debug_preview(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "EMPTY"
    if len(text) <= 96:
        return text
    return f"{text[:93]}..."


def _structured_repeater_debug_enabled() -> bool:
    return _profile_debug_enabled()


def _trace_structured_repeater_resolution(
    field: FormField,
    diagnostic: StructuredRepeaterDiagnostic | None,
) -> None:
    if not diagnostic or not _structured_repeater_debug_enabled():
        return
    logger.info(
        "domhand.structured_repeater_resolution",
        extra={
            "repeater_group": diagnostic.repeater_group,
            "field_id": diagnostic.field_id,
            "field_label": diagnostic.field_label,
            "section": diagnostic.section,
            "slot_name": diagnostic.slot_name,
            "numeric_index": diagnostic.numeric_index,
            "section_binding_reused": diagnostic.section_binding_reused,
            "binding_mode": diagnostic.binding_mode,
            "binding_confidence": diagnostic.binding_confidence,
            "entry_index": diagnostic.entry_index,
            "current_value": _profile_debug_preview(diagnostic.current_value),
            "resolved_value_preview": diagnostic.resolved_value_preview,
            "resolved_source_key": diagnostic.resolved_source_key,
            "failure_stage": diagnostic.failure_stage,
            "field_type": field.field_type,
        },
    )


def _structured_repeater_failure_reason(stage: str) -> str:
    return {
        "slot_unresolved": "structured_slot_unresolved",
        "binding_unresolved": "structured_binding_unresolved",
        "entry_value_missing": "structured_entry_value_missing",
        "value_coercion_empty": "structured_value_coercion_empty",
    }.get(stage, "missing_profile_data")


def _structured_repeater_takeover_suggestion(repeater_group: str) -> str:
    if repeater_group == "languages":
        return "Pause for user data instead of guessing this structured language field."
    if repeater_group == "education":
        return "Pause for user data instead of guessing this structured education field."
    return "Pause for user data instead of guessing this structured repeater field."


def _structured_repeater_fill_result(
    field: FormField,
    diagnostic: StructuredRepeaterDiagnostic,
) -> FillFieldResult:
    error_msg = _structured_field_missing_reason(field)
    return FillFieldResult(
        field_id=field.field_id,
        name=_preferred_field_label(field),
        success=False,
        actor="skipped",
        error=error_msg if not field.required else f"REQUIRED — {error_msg}",
        required=field.required,
        control_kind=field.field_type,
        section=field.section or "",
        state="failed",
        failure_reason=_structured_repeater_failure_reason(diagnostic.failure_stage),
        takeover_suggestion=_structured_repeater_takeover_suggestion(diagnostic.repeater_group),
        binding_mode=diagnostic.binding_mode,
        binding_confidence=diagnostic.binding_confidence,
        repeater_group=diagnostic.repeater_group,
        slot_name=diagnostic.slot_name,
        diagnostic_stage=diagnostic.failure_stage or None,
    )


def _fill_result_summary_entry(result: FillFieldResult) -> dict[str, Any]:
    entry = {
        "field_id": result.field_id,
        "name": result.name,
        "control_kind": result.control_kind,
        "section": result.section,
        "failure_reason": result.failure_reason,
        "takeover_suggestion": result.takeover_suggestion,
    }
    if result.repeater_group:
        entry["repeater_group"] = result.repeater_group
    if result.slot_name:
        entry["slot_name"] = result.slot_name
    if result.diagnostic_stage:
        entry["diagnostic_stage"] = result.diagnostic_stage
    if result.binding_mode:
        entry["binding_mode"] = result.binding_mode
    if result.binding_confidence:
        entry["binding_confidence"] = result.binding_confidence
    return entry


# Agent-facing fill summary: keep tool output small for planner token cost.
_AGENT_FILL_NAME_MAX_LEN = 100
_AGENT_FILL_SECTION_MAX_LEN = 80
_AGENT_FILL_MAX_FAILED_FIELDS = 35


def _truncate_agent_fill_text(text: str | None, max_len: int) -> str:
    t = " ".join(str(text or "").split()).strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _fill_result_summary_entry_for_agent(result: FillFieldResult) -> dict[str, Any]:
    entry = _fill_result_summary_entry(result)
    entry["name"] = _truncate_agent_fill_text(entry.get("name"), _AGENT_FILL_NAME_MAX_LEN)
    entry["section"] = _truncate_agent_fill_text(entry.get("section"), _AGENT_FILL_SECTION_MAX_LEN)
    return entry


def _set_structured_repeater_binding(
    diagnostic: StructuredRepeaterDiagnostic | None,
    binding: ResolvedFieldBinding | None,
) -> None:
    if not diagnostic or not binding:
        return
    diagnostic.binding_mode = binding.binding_mode
    diagnostic.binding_confidence = binding.binding_confidence
    diagnostic.entry_index = binding.entry_index


def _set_structured_repeater_resolved_value(
    diagnostic: StructuredRepeaterDiagnostic | None,
    value: str | None,
    *,
    source_key: str | None = None,
) -> None:
    if not diagnostic:
        return
    diagnostic.failure_stage = "resolved"
    diagnostic.resolved_value_preview = _profile_debug_preview(value)
    diagnostic.resolved_source_key = source_key


def _looks_like_internal_widget_value(value: str | None) -> bool:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return False
    return bool(_OPAQUE_WIDGET_VALUE_RE.fullmatch(text))


def _is_effectively_unset_field_value(value: str | None) -> bool:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return True
    if is_placeholder_value(text):
        return True
    if _looks_like_internal_widget_value(text):
        return True
    if _SELECT_PLACEHOLDER_FRAGMENT_RE.search(text):
        return True
    return False


def _is_non_guess_name_fragment(field_name: str | None) -> bool:
    norm = normalize_name(field_name or "")
    if not norm:
        return False
    if _NAME_FRAGMENT_NO_GUESS_RE.search(norm):
        return True
    if norm == "name":
        return True
    return False


def _strip_required_marker(label: str | None) -> str:
    text = str(label or "").strip()
    if not text:
        return ""
    return re.sub(r"\s*[*\uFF0A]+\s*$", "", text).strip()


def _profile_skill_values(profile_data: dict[str, Any] | None) -> list[str]:
    raw_values = (profile_data or {}).get("skills")
    if not isinstance(raw_values, list):
        return []

    seen: set[str] = set()
    ordered: list[str] = []
    for entry in raw_values:
        text = str(entry or "").strip()
        if not text:
            continue
        key = normalize_name(text)
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(text)
        if len(ordered) >= _SKILL_FIELD_MAX_ITEMS:
            break
    return ordered



def _should_trace_profile_label(label: str) -> bool:
    norm = normalize_name(label or "")
    return any(token in norm for token in _PROFILE_TRACE_LABEL_TOKENS)


def _trace_profile_resolution(event: str, *, field_label: str, **extra: Any) -> None:
    if not _profile_debug_enabled():
        return
    if not _should_trace_profile_label(field_label):
        return
    logger.debug(
        event,
        extra={
            "field_label": field_label,
            **extra,
        },
    )


async def _safe_page_url(page: Any) -> str:
    """Best-effort page URL extraction across Playwright/CDP page wrappers."""
    if not page:
        return ""

    try:
        url_attr = getattr(page, "url", None)
        if callable(url_attr):
            value = url_attr()
            if inspect.isawaitable(value):
                value = await value
        else:
            value = url_attr
        if isinstance(value, str):
            return value
    except Exception:
        pass

    try:
        get_url = getattr(page, "get_url", None)
        if callable(get_url):
            value = get_url()
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, str):
                return value
    except Exception:
        pass

    return ""


async def _read_page_context_snapshot(page: Any) -> dict[str, Any]:
    """Return lightweight page-context signals for expected-value scoping."""
    try:
        raw = await page.evaluate(_PAGE_CONTEXT_SCAN_JS)
    except Exception:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _first_meaningful_section(fields: list[FormField] | None) -> str:
    if not fields:
        return ""
    for field in fields:
        section = str(field.section or "").strip()
        if section and len(section) <= 80 and "?" not in section:
            return section
    return ""


async def _get_page_context_key(
    page: Any,
    *,
    fields: list[FormField] | None = None,
    fallback_marker: str | None = None,
) -> str:
    """Build a stable page-context key shared by fill, recovery, and assessment."""
    page_url = await _safe_page_url(page)
    snapshot = await _read_page_context_snapshot(page)
    marker = (
        str(snapshot.get("page_marker") or "").strip()
        or str(fallback_marker or "").strip()
        or _first_meaningful_section(fields)
    )
    heading_texts = snapshot.get("heading_texts") or []
    if not marker and heading_texts:
        marker = str(heading_texts[0] or "").strip()
    return build_page_context_key(url=page_url, page_marker=marker)


# ── Q&A bank constants ───────────────────────────────────────────────

# Maximum number of Q&A bank entries to keep in LLM context.
MAX_QA_ENTRIES = 20

# Confidence ranking for Q&A bank entries.
_QA_CONFIDENCE_RANKS: dict[str, int] = {
    "exact": 0,
    "inferred": 1,
    "learned": 2,
}

# Canonical question synonyms for fuzzy Q&A matching.
# Each canonical key maps to a list of alternative phrasings.
_QA_QUESTION_SYNONYMS: dict[str, list[str]] = {
    "how did you hear about us": [
        "how did you hear",
        "how did you learn",
        "referral source",
        "source of application",
        "where did you hear",
        "source",
        "how did you find this job",
        "how did you find us",
        "how did you find this position",
        "heard about us",
        "how did you hear about this position",
        "how did you learn about this job",
        "source of referral",
    ],
    "work authorization": [
        "authorized to work",
        "are you authorized",
        "legally authorized",
        "work permit",
        "right to work",
        "employment eligibility",
        "us citizen",
        "citizen or permanent resident",
        "are you legally authorized to work",
    ],
    "visa sponsorship": [
        "sponsorship",
        "require sponsorship",
        "need sponsorship",
        "immigration sponsorship",
        "visa support",
        "sponsorship needed",
    ],
    "willing to relocate": [
        "relocation",
        "open to relocation",
        "willing to move",
        "relocate",
        "can you relocate",
        "willingness to relocate",
    ],
    "salary expectation": [
        "salary",
        "compensation",
        "total compensation",
        "expectations on compensation",
        "desired salary",
        "pay expectation",
        "salary requirement",
        "expected compensation",
        "desired compensation",
        "compensation range",
        "salary range",
    ],
    "start date": [
        "available start date",
        "when can you start",
        "availability",
        "earliest start",
        "start date availability",
    ],
    "gender": [
        "what is your gender",
        "gender identity",
    ],
    "race ethnicity": [
        "race",
        "ethnicity",
        "racial background",
    ],
    "veteran status": [
        "are you a protected veteran",
        "veteran",
    ],
    "disability status": [
        "disability",
        "do you have a disability",
        "please indicate if you have a disability",
    ],
}

# ── Browser-side helper injection ────────────────────────────────────


def _build_inject_helpers_js() -> str:
    """Return the JS that installs ``window.__ff`` — the browser-side helper
    library used by every subsequent ``page.evaluate()`` call.

    Ported 1:1 from GH formFiller.ts ``injectHelpers()``.
    """
    selector_json = json.dumps(INTERACTIVE_SELECTOR)
    return f"""() => {{
	if (typeof globalThis.__name === 'undefined') {{
		globalThis.__name = function(fn) {{ return fn; }};
	}}
	var _prevNextId = (window.__ff && window.__ff.nextId) || 0;
	window.__ff = {{
		SELECTOR: {selector_json},

		rootParent: function(node) {{
			if (!node) return null;
			if (node.parentElement) return node.parentElement;
			var root = node.getRootNode ? node.getRootNode() : null;
			if (root && root.host) return root.host;
			return null;
		}},

		allRoots: function() {{
			var roots = [document];
			var seen = new Set([document]);
			for (var i = 0; i < roots.length; i++) {{
				var root = roots[i];
				if (!root.querySelectorAll) continue;
				root.querySelectorAll('*').forEach(function(el) {{
					if (el.shadowRoot && !seen.has(el.shadowRoot)) {{
						seen.add(el.shadowRoot);
						roots.push(el.shadowRoot);
					}}
				}});
			}}
			return roots;
		}},

		queryAll: function(selector) {{
			var results = [];
			var seen = new Set();
			window.__ff.allRoots().forEach(function(root) {{
				if (!root.querySelectorAll) return;
				root.querySelectorAll(selector).forEach(function(el) {{
					if (seen.has(el)) return;
					seen.add(el);
					results.push(el);
				}});
			}});
			return results;
		}},

		queryOne: function(selector) {{
			var hits = window.__ff.queryAll(selector);
			return hits.length > 0 ? hits[0] : null;
		}},

		byId: function(id) {{
			return window.__ff.queryOne('[data-ff-id="' + id + '"]');
		}},

		getByDomId: function(id) {{
			if (!id) return null;
			var escapedId = String(id).replace(/"/g, '\\\\"');
			var roots = window.__ff.allRoots();
			for (var i = 0; i < roots.length; i++) {{
				var root = roots[i];
				if (root.getElementById) {{
					var direct = root.getElementById(id);
					if (direct) return direct;
				}}
				if (root.querySelector) {{
					var queried = root.querySelector('[id="' + escapedId + '"]');
					if (queried) return queried;
				}}
			}}
			return null;
		}},

		closestCrossRoot: function(el, selector) {{
			var node = el;
			while (node) {{
				if (node.matches && node.matches(selector)) return node;
				node = window.__ff.rootParent(node);
			}}
			return null;
		}},

		getAccessibleName: function(el) {{
			var lblBy = el.getAttribute('aria-labelledby');
			if (lblBy) {{
				var uxiC = window.__ff.closestCrossRoot(el, '[data-uxi-widget-type]') || window.__ff.closestCrossRoot(el, '[role="combobox"]');
				var t = lblBy.split(/\\s+/)
					.map(function(id) {{
						var r = window.__ff.getByDomId(id);
						if (!r) return '';
						if (uxiC && uxiC.contains(r)) return '';
						if (el.contains(r)) return '';
						return r.textContent.trim();
					}})
					.filter(Boolean).join(' ');
				if (t) return t;
			}}
			var elType = el.type || el.getAttribute('role') || '';
			var al = el.getAttribute('aria-label');
			if (al && elType !== 'radio' && elType !== 'checkbox') {{
				al = al.trim();
				if (el.getAttribute('aria-haspopup') === 'listbox' && el.textContent) {{
					var val = el.textContent.trim();
					if (val && al.includes(val)) {{
						al = al.replace(val, '');
						if (/\\bRequired\\b/i.test(al)) {{
							el.dataset.ffRequired = 'true';
							al = al.replace(/\\s*Required\\s*/gi, ' ');
						}}
						al = al.replace(/\\s+/g, ' ').trim();
					}}
				}}
				if (al) return al;
			}}
			if (el.id) {{
				var lbl = window.__ff.queryOne('label[for="' + el.id + '"]');
				if (lbl) {{
					var c = lbl.cloneNode(true);
					c.querySelectorAll('input, .required, span[aria-hidden]').forEach(function(x) {{ x.remove(); }});
					var tx = c.textContent.trim();
					if (tx) return tx;
				}}
			}}
			var from = el;
			var tp = el.type || el.getAttribute('role') || '';
			if (tp === 'checkbox' || tp === 'radio') {{
				var grp = window.__ff.closestCrossRoot(el, '.checkbox-group, .radio-group, [role=group], [role=radiogroup]');
				var grpParent = grp ? window.__ff.rootParent(grp) : null;
				if (grp && grpParent) from = grpParent;
			}}
			var group = window.__ff.closestCrossRoot(from, '.form-group, .field, .form-field, fieldset') || from;
			var lbl2 = group.querySelector(':scope > label, :scope > legend');
			if (lbl2) {{
				var c2 = lbl2.cloneNode(true);
				c2.querySelectorAll('input, .required, span[aria-hidden]').forEach(function(x) {{ x.remove(); }});
				var tx2 = c2.textContent.trim();
				if (tx2) return tx2;
			}}
			if (el.type === 'file') {{
				var card = window.__ff.closestCrossRoot(el, '.card, .section, [class*="upload"], [class*="drop"]');
				if (card) {{
					var parent = window.__ff.closestCrossRoot(card, '.card, .section') || card;
					var hdr = parent.querySelector('h1, h2, h3, h4, legend, [class*="heading"], [class*="title"]');
					if (hdr) {{
						var ht = hdr.textContent.trim();
						if (ht) return ht;
					}}
				}}
			}}
			return el.placeholder || el.getAttribute('title') || '';
		}},

		isVisible: function(el) {{
			var n = el;
			while (n && n !== document.body) {{
				var s = window.getComputedStyle(n);
				if (s.display === 'none' || s.visibility === 'hidden') return false;
				if (n.getAttribute && n.getAttribute('aria-hidden') === 'true') return false;
				n = window.__ff.rootParent(n);
			}}
			return true;
		}},

		getSection: function(el) {{
			var n = window.__ff.rootParent(el);
			while (n) {{
				var heading = n.querySelector(':scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > [data-automation-id="pageHeader"], :scope > [data-automation-id*="pageHeader"], :scope > [data-automation-id*="sectionTitle"], :scope > [data-automation-id*="stepTitle"]');
				if (heading) return heading.textContent.trim();
				var isFieldWrapper =
					n.matches &&
					n.matches('fieldset, [data-automation-id="formField"], [data-automation-id*="formField"], .form-group, .field, .form-field');
				if (!isFieldWrapper) {{
					var localLabel = n.querySelector(':scope > legend, :scope > [data-automation-id="fieldLabel"], :scope > [data-automation-id*="fieldLabel"], :scope > label');
					if (localLabel) return localLabel.textContent.trim();
				}}
				n = window.__ff.rootParent(n);
			}}
			return '';
		}},

		nextId: _prevNextId,
		tag: function(el) {{
			if (!el.hasAttribute('data-ff-id')) {{
				el.setAttribute('data-ff-id', 'ff-' + (window.__ff.nextId++));
			}}
			return el.getAttribute('data-ff-id');
		}}
	}};
	return 'ok';
}}"""


# ── Field extraction JS ──────────────────────────────────────────────

# ── Button group extraction JS ───────────────────────────────────────

_EXTRACT_BUTTON_GROUPS_JS = (
    r"""() => {
	var ff = window.__ff;
	if (!ff) return JSON.stringify([]);
	var results = [];
	var allBtnEls = document.querySelectorAll(
		'button, [role="button"], [role="radio"], [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"], [data-automation-id*="radio"], [data-automation-id*="Radio"]'
	);
	var parentMap = {};

	var navLabels = new Set("""
    + json.dumps(list(_NAV_BUTTON_LABELS))
    + r""");

	for (var i = 0; i < allBtnEls.length; i++) {
		var btn = allBtnEls[i];
		if (!ff.isVisible(btn)) continue;
		if (btn.disabled) continue;
		if (btn.closest('nav, header, [role="navigation"], [role="menubar"], [role="menu"], [role="toolbar"]')) continue;
		if (btn.tagName === 'A' || btn.closest('a[href]')) continue;
		if (btn.getAttribute('role') === 'combobox') continue;
		if (btn.getAttribute('aria-haspopup') === 'listbox') continue;
		if (btn.tagName.toLowerCase() === 'input') continue;

		var btnText = (btn.textContent || '').trim();
		if (!btnText || btnText.length > 30) continue;
		if (navLabels.has(btnText.toLowerCase()) || btnText.toLowerCase().startsWith('add ') || btnText.toLowerCase().includes('save & continue')) continue;

		var parent = btn.parentElement;
		for (var pu = 0; pu < 3 && parent; pu++) {
			var childBtns = parent.querySelectorAll(
				'button, [role="button"], [role="radio"], [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"], [data-automation-id*="radio"], [data-automation-id*="Radio"]'
			);
			var visibleCount = 0;
			for (var vc = 0; vc < childBtns.length; vc++) {
				if (ff.isVisible(childBtns[vc])) visibleCount++;
			}
			if (visibleCount >= 2 && visibleCount <= 4) break;
			parent = parent.parentElement;
		}
		if (!parent) continue;

		var parentKey = parent.getAttribute('data-ff-btn-group') || ('btngrp-' + i);
		parent.setAttribute('data-ff-btn-group', parentKey);

		if (!parentMap[parentKey]) {
			parentMap[parentKey] = { parent: parent, buttons: [] };
		}
		var already = false;
		for (var j = 0; j < parentMap[parentKey].buttons.length; j++) {
			if (parentMap[parentKey].buttons[j].text === btnText) { already = true; break; }
		}
		if (!already) {
			parentMap[parentKey].buttons.push({ text: btnText, ffId: ff.tag(btn) });
		}
	}

	for (var groupKey in parentMap) {
		var group = parentMap[groupKey];
		if (group.buttons.length < 2 || group.buttons.length > 4) continue;
		if (group.buttons.some(function(entry) { return entry.text.length > 30; })) continue;

		var container = group.parent;
		var questionLabel = '';
		var prevSib = container.previousElementSibling;
		if (prevSib) {
			var prevText = (prevSib.textContent || '').trim();
			if (prevText.length > 0 && prevText.length < 200) questionLabel = prevText;
		}
		if (!questionLabel) {
			var parentEl = container.parentElement;
			if (parentEl) {
				var labelEl = parentEl.querySelector('[data-automation-id="fieldLabel"], [data-automation-id*="fieldLabel"], label, .label, h3, h4, legend, [class*="question"]');
				if (labelEl) questionLabel = (labelEl.textContent || '').trim();
			}
		}
		if (!questionLabel) questionLabel = 'Button group choice';

		var ffId = ff.tag(container);
		results.push({
			field_id: ffId,
			name: questionLabel.replace(/\*\s*$/, '').trim(),
			field_type: 'button-group',
			section: ff.getSection(container),
			name_attr: container.getAttribute('name') || '',
			required: false,
			options: [],
			choices: group.buttons.map(function(b) { return b.text; }),
			accept: null,
			is_native: false,
			is_multi_select: false,
			visible: true,
			raw_label: questionLabel,
			synthetic_label: false,
			field_fingerprint: null,
			current_value: '',
			btn_ids: group.buttons.map(function(b) { return b.ffId; })
		});
	}
	return JSON.stringify(results);
}"""
)


# ── Single-field JS helpers ──────────────────────────────────────────

# ── Profile evidence extraction ──────────────────────────────────────


def _record_page_token_cost(
    browser_session: BrowserSession,
    *,
    page_context_key: str,
    target_section: str | None,
    field_count: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
    if input_tokens <= 0 and output_tokens <= 0:
        return
    store = getattr(browser_session, "_gh_page_token_costs", None)
    if not isinstance(store, dict):
        store = {}
    entry_key = page_context_key or (target_section or "(unknown)")
    entry = store.get(entry_key)
    if not isinstance(entry, dict):
        entry = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
    entry["calls"] = int(entry.get("calls") or 0) + 1
    entry["input_tokens"] = int(entry.get("input_tokens") or 0) + int(input_tokens)
    entry["output_tokens"] = int(entry.get("output_tokens") or 0) + int(output_tokens)
    entry["field_count"] = field_count
    entry["target_section"] = target_section or ""
    store[entry_key] = entry
    browser_session._gh_page_token_costs = store
    logger.debug(
        "domhand.page_token_cost",
        extra={
            "page_context_key": page_context_key,
            "target_section": target_section or "",
            "field_count": field_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cumulative_calls": entry["calls"],
            "cumulative_input_tokens": entry["input_tokens"],
            "cumulative_output_tokens": entry["output_tokens"],
        },
    )


def _log_loop_blocker_state(
    browser_session: BrowserSession,
    *,
    fields: list[FormField],
    chosen_next_strategy: str,
) -> None:
    last_state = getattr(browser_session, "_gh_last_application_state", None)
    if not isinstance(last_state, dict):
        return
    same_blocker_signature_count = int(last_state.get("same_blocker_signature_count") or 0)
    blocker_signature = str(last_state.get("blocking_signature") or "")
    field_keys = [get_stable_field_key(field) for field in fields]
    no_progress_counts = {key: same_blocker_signature_count + 1 for key in field_keys}
    logger.info(
        "domhand.loop_blocker_state",
        extra={
            "blocker_signature": blocker_signature,
            "same_blocker_signature_count": same_blocker_signature_count,
            "field_keys": field_keys,
            "field_labels": [_preferred_field_label(field) for field in fields],
            "field_no_progress_count": no_progress_counts,
            "chosen_next_strategy": chosen_next_strategy,
        },
    )


def _get_auth_override_data(enabled: bool) -> dict[str, str] | None:
    """Load auth credentials from GH_* env vars when auth-mode fills are enabled."""
    if not enabled:
        return None

    email = (os.environ.get("GH_EMAIL") or "").strip()
    password = (os.environ.get("GH_PASSWORD") or "").strip()
    if not email and not password:
        return None

    overrides: dict[str, str] = {}
    if email:
        overrides["email"] = email
    if password:
        overrides["password"] = password
        overrides["confirm_password"] = password
    return overrides or None


def _is_auth_like_field(field: FormField) -> bool:
    """Return True for auth fields that should prefer credential overrides."""
    if field.field_type == "password":
        return True

    for label in _field_label_candidates(field):
        name = normalize_name(label)
        if not name:
            continue
        if any(token in name for token in ("email", "e-mail", "username", "user name", "login")):
            return True
    return False


def _known_auth_override_for_field(field: FormField, auth_overrides: dict[str, str] | None) -> str | None:
    """Match auth-like fields to GH_EMAIL/GH_PASSWORD values."""
    if not auth_overrides:
        return None

    password = auth_overrides.get("password")
    confirm_password = auth_overrides.get("confirm_password") or password
    email = auth_overrides.get("email")

    for label in _field_label_candidates(field):
        name = normalize_name(label)
        if not name:
            continue

        if "password" in name:
            if any(token in name for token in ("confirm", "re-enter", "reenter", "repeat", "again")):
                return confirm_password
            return password

        if any(token in name for token in ("email", "e-mail", "username", "user name", "login")):
            return email

    if field.field_type == "password":
        return password
    if field.field_type == "email":
        return email
    return None


def _row_order_binding_index(
    *,
    field: FormField,
    visible_fields: list[FormField],
    slot_name: str,
    slot_resolver,
) -> int | None:
    occurrence = 0
    target_id = field.field_id
    target_section = normalize_name(field.section or "")
    for candidate in visible_fields:
        if slot_resolver(candidate) != slot_name:
            continue
        candidate_section = normalize_name(candidate.section or "")
        if target_section and candidate_section and candidate_section != target_section:
            continue
        if candidate.field_id == target_id:
            return occurrence
        occurrence += 1
    return None


def _parse_heading_index(scope: str | None) -> int | None:
    """Extract a 1-based repeater index from headings like 'Education 1'."""
    if not scope:
        return None
    match = re.search(r"(\d+)(?!.*\d)", scope)
    if not match:
        return None
    try:
        return max(1, int(match.group(1)))
    except ValueError:
        return None


def _normalized_raw_date_component(raw_field: dict[str, Any]) -> str:
    return normalize_name(raw_field.get("date_component") or "")


def _clean_grouped_date_component(value: str | None, component: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = normalize_name(text)
    if normalized in {component, "mm", "dd", "yyyy"}:
        return ""
    return text


def _compose_grouped_date_value(month: str | None, day: str | None, year: str | None) -> str:
    normalized_month = _clean_grouped_date_component(month, "month")
    normalized_day = _clean_grouped_date_component(day, "day")
    normalized_year = _clean_grouped_date_component(year, "year")
    if not any((normalized_month, normalized_day, normalized_year)):
        return ""
    return "/".join(
        [
            normalized_month or "MM",
            normalized_day or "DD",
            normalized_year or "YYYY",
        ]
    )


def _raw_grouped_date_fields(
    raw_fields: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    groups: dict[str, dict[str, Any]] = {}
    grouped_component_ids: set[str] = set()

    for index, raw_field in enumerate(raw_fields):
        component = _normalized_raw_date_component(raw_field)
        if component not in {"month", "day", "year"}:
            continue
        group_key = str(raw_field.get("date_group_key") or "").strip()
        if not group_key:
            continue
        group = groups.setdefault(
            group_key,
            {
                "order": index,
                "components": {},
                "wrapper_id": str(raw_field.get("wrapper_id") or "").strip(),
                "wrapper_label": str(raw_field.get("group_label") or raw_field.get("wrapper_label") or "").strip(),
                "section": str(raw_field.get("section") or "").strip(),
                "required": False,
                "has_calendar_trigger": False,
                "format_hint": str(raw_field.get("format_hint") or "").strip(),
            },
        )
        group["components"][component] = raw_field
        group["required"] = bool(group["required"] or raw_field.get("required"))
        group["has_calendar_trigger"] = bool(group["has_calendar_trigger"] or raw_field.get("has_calendar_trigger"))
        if not group["format_hint"] and raw_field.get("format_hint"):
            group["format_hint"] = str(raw_field.get("format_hint") or "").strip()
        if not group["wrapper_label"] and raw_field.get("wrapper_label"):
            group["wrapper_label"] = str(raw_field.get("wrapper_label") or "").strip()
        if not group["section"] and raw_field.get("section"):
            group["section"] = str(raw_field.get("section") or "").strip()

    grouped_fields: dict[str, dict[str, Any]] = {}
    for group_key, group in groups.items():
        components = group["components"]
        if not {"month", "day", "year"} <= set(components):
            continue
        section_text = str(group.get("section") or "")
        label_text = str(group.get("wrapper_label") or "")
        if _is_education_like_section(section_text) or _is_education_like_section(label_text):
            continue
        month_field = components["month"]
        day_field = components["day"]
        year_field = components["year"]
        field_id = str(group.get("wrapper_id") or month_field.get("field_id") or "").strip()
        if not field_id:
            continue
        grouped_component_ids.update(
            {
                str(month_field.get("field_id") or "").strip(),
                str(day_field.get("field_id") or "").strip(),
                str(year_field.get("field_id") or "").strip(),
            }
        )
        grouped_fields[group_key] = {
            "field_id": field_id,
            "name": label_text or "Date",
            "field_type": "date",
            "section": section_text,
            "required": bool(group.get("required")) or "*" in (label_text or ""),
            "options": [],
            "choices": [],
            "accept": None,
            "is_native": False,
            "is_multi_select": False,
            "visible": True,
            "raw_label": label_text or "Date",
            "synthetic_label": bool(not label_text),
            "field_fingerprint": None,
            "current_value": _compose_grouped_date_value(
                month_field.get("current_value"),
                day_field.get("current_value"),
                year_field.get("current_value"),
            ),
            "widget_kind": "grouped_date",
            "component_field_ids": [
                str(month_field.get("field_id") or "").strip(),
                str(day_field.get("field_id") or "").strip(),
                str(year_field.get("field_id") or "").strip(),
            ],
            "has_calendar_trigger": bool(group.get("has_calendar_trigger")),
            "format_hint": str(group.get("format_hint") or "MM/DD/YYYY"),
            "group_order": int(group.get("order") or 0),
        }
    return grouped_fields, grouped_component_ids



from ghosthands.dom.fill_verify import (
    DOMHAND_RETRY_CAPPED,
    _attempt_domhand_fill_with_retry_cap,
    _clear_domhand_failure_for_field,
    _domhand_retry_field_identity,
    _domhand_retry_message,
    _field_already_matches,
    _is_domhand_retry_capped_for_field,
    _read_observed_field_value,
    _record_domhand_failure_for_field,
    _record_expected_value_if_settled,
    _verify_fill_observable,
)



def _record_unverified_custom_select_intent(
    *,
    host: str,
    page_context_key: str,
    field: FormField,
    field_key: str,
    intended_value: str,
) -> None:
    """Persist intended value when DomHand reports fill success but settle verification never passed.

    Custom selects (e.g. react-select on Greenhouse) often keep empty DOM readback while the UI
    shows a chip. ``domhand_assess_state`` uses this alongside ``get_expected_field_value`` to drop
    false ``required_missing_value`` blockers after prefill.
    """
    intended = str(intended_value or "").strip()
    if not intended:
        return
    if field.field_type != "select" or field.is_native:
        return
    record_expected_field_value(
        host=host,
        page_context_key=page_context_key,
        field_key=field_key,
        field_label=_preferred_field_label(field),
        field_type=field.field_type,
        field_section=field.section or "",
        field_fingerprint=field.field_fingerprint or "",
        expected_value=intended,
        source="domhand_unverified",
    )



from ghosthands.dom.fill_llm_answers import (
    _disambiguated_field_names,
    _generate_answers,
    _is_explicit_false,
    _parse_llm_json_answer_object,
    _repair_invalid_json_string_escapes,
    _resolve_llm_answer_for_field,
    _resolve_llm_answer_via_batch_key,
    _sanitize_no_guess_answer,
    _takeover_suggestion_for_field,
    infer_answers_for_fields,
)



def _build_field_description(field: FormField, display_name: str) -> str:
    type_label = "multi-select" if field.is_multi_select else field.field_type
    req_marker = " *" if field.required else ""
    desc = f'- "{display_name}"{req_marker} (type: {type_label})'
    if field.options:
        desc += f" options: [{', '.join(field.options[:50])}]"
    if field.choices:
        desc += f" choices: [{', '.join(field.choices[:30])}]"
    if field.section:
        desc += f" [section: {field.section}]"
    if field.raw_label and normalize_name(field.raw_label) != normalize_name(display_name):
        desc += f' [question: "{field.raw_label}"]'
    if field.current_value:
        desc += f' [current: "{field.current_value}"]'
    return desc


def _replace_placeholder_answers(
    parsed: dict[str, Any],
    fields: list[FormField],
    disambiguated_names: list[str],
) -> None:
    placeholder_re = re.compile(
        r"^(select one|choose one|please select|-- ?select ?--|— ?select ?—|\(select\)|select\.{0,3})$",
        re.IGNORECASE,
    )
    decline_patterns = [
        re.compile(p, re.IGNORECASE)
        for p in [
            r"not declared",
            r"prefer not",
            r"decline",
            r"do not wish",
            r"choose not",
            r"rather not",
            r"not specified",
            r"not applicable",
            r"n/?a",
        ]
    ]
    for key, val in list(parsed.items()):
        if not isinstance(val, str) or not placeholder_re.match(val.strip()):
            continue
        idx = disambiguated_names.index(key) if key in disambiguated_names else -1
        field = fields[idx] if idx >= 0 else None
        if field and not field.required:
            parsed[key] = ""
            continue
        options = (field.options or field.choices or []) if field else []
        neutral = next((o for o in options if any(p.search(o) for p in decline_patterns)), None)
        if neutral:
            logger.info(f'Replaced placeholder "{val}" -> "{neutral}" for field "{key}"')
            parsed[key] = neutral
        elif options:
            non_placeholder = [o for o in options if not placeholder_re.match(o.strip())]
            if non_placeholder:
                parsed[key] = non_placeholder[-1]


# ── Field-answer matching ────────────────────────────────────────────

_AUTHORITATIVE_SELECT_KEYS: dict[str, list[str]] = {
    "country phone code": ["Country Phone Code", "Phone Country Code"],
    "phone country code": ["Phone Country Code", "Country Phone Code"],
    "phone device type": ["Phone Device Type", "Phone Type"],
    "phone type": ["Phone Type", "Phone Device Type"],
}
_AUTHORITATIVE_SELECT_DEFAULTS: dict[str, str] = {
    "country phone code": "+1",
    "phone country code": "+1",
    "phone device type": "Mobile",
    "phone type": "Mobile",
    # Common non-personal fields — safe defaults that should never trigger HITL
    "how did you hear": "LinkedIn",
    "how did you hear about us": "LinkedIn",
    "how did you hear about this position": "LinkedIn",
    "how did you learn about this job": "LinkedIn",
    "referral source": "LinkedIn",
    "source": "LinkedIn",
    "where did you hear": "LinkedIn",
    "country": "United States",
    "country of residence": "United States",
    "worked for this company": "No",
    "worked for this organization": "No",
    "worked here before": "No",
    "previously worked": "No",
    "previously employed": "No",
    "prior employment": "No",
    "previous employee": "No",
    "preferred language": "English",
    "language": "English",
    "overall": "Native / bilingual",
    "overall proficiency": "Native / bilingual",
    "reading": "Native / bilingual",
    "reading proficiency": "Native / bilingual",
    "writing": "Native / bilingual",
    "writing proficiency": "Native / bilingual",
    "speaking": "Native / bilingual",
    "speaking proficiency": "Native / bilingual",
    "comprehension": "Native / bilingual",
    "language proficiency": "Native / bilingual",
    "willing to relocate": "Yes",
    "willingness to relocate": "Yes",
    "relocation": "Yes",
}

_AUTHORITATIVE_TEXT_DEFAULTS: dict[str, str] = {
    "how did you hear about this position": "LinkedIn",
    "how did you hear about us": "LinkedIn",
    "referral source": "LinkedIn",
    "source of application": "LinkedIn",
}

# EEO / demographic fields — "decline to self-identify" defaults when profile
# data is empty.  Prevents required EEO fields from triggering HITL.
_EEO_DECLINE_DEFAULTS: dict[str, str] = {
    "gender": "I decline to self-identify",
    "race": "I decline to self-identify",
    "race ethnicity": "I decline to self-identify",
    "ethnicity": "I decline to self-identify",
    "veteran status": "I am not a protected veteran",
    "veteran": "I am not a protected veteran",
    "disability": "I do not wish to answer",
    "disability status": "I do not wish to answer",
    "sexual orientation": "I decline to self-identify",
    "lgbtq": "I decline to self-identify",
}


def _match_answer(
    field: FormField,
    answers: dict[str, str],
    evidence: dict[str, str | None],
    profile_data: dict[str, Any] | None = None,
) -> str | None:
    label_candidates = _field_label_candidates(field) or [field.name]
    candidate_norms = [_normalize_match_label(label) for label in label_candidates if _normalize_match_label(label)]
    minimum_confidence = "medium" if field.required else "strong"

    if field.field_type == "select":
        for norm_name in candidate_norms:
            if norm_name in _AUTHORITATIVE_SELECT_KEYS:
                for ck in _AUTHORITATIVE_SELECT_KEYS[norm_name]:
                    if ck in answers:
                        return answers[ck]
                    for key, val in answers.items():
                        if normalize_name(key) == normalize_name(ck):
                            return val
                if norm_name in _AUTHORITATIVE_SELECT_DEFAULTS:
                    return _AUTHORITATIVE_SELECT_DEFAULTS[norm_name]

    profile_val = _known_profile_value_for_field(field, evidence, profile_data, minimum_confidence=minimum_confidence)
    if profile_val:
        return profile_val

    if not candidate_norms:
        return None

    best_val: str | None = None
    best_rank = 0
    for key, val in answers.items():
        for candidate in label_candidates:
            confidence = _label_match_confidence(candidate, key)
            if not _meets_match_confidence(confidence, minimum_confidence):
                continue
            rank = _MATCH_CONFIDENCE_RANKS.get(confidence or "", 0)
            if rank > best_rank:
                best_rank = rank
                best_val = _coerce_answer_to_field(field, val)
                if rank == _MATCH_CONFIDENCE_RANKS["exact"]:
                    return best_val

    if best_val is not None:
        return best_val

    # ── Authoritative defaults for non-personal fields ────────────
    # Check select defaults for ANY field type (some "select" fields render
    # as text inputs, button-groups, or radios).
    for norm_name in candidate_norms:
        if norm_name in _AUTHORITATIVE_SELECT_DEFAULTS:
            return _AUTHORITATIVE_SELECT_DEFAULTS[norm_name]

    # Check text defaults for text/textarea fields.
    if field.field_type in {"text", "textarea", "search"}:
        for norm_name in candidate_norms:
            if norm_name in _AUTHORITATIVE_TEXT_DEFAULTS:
                return _AUTHORITATIVE_TEXT_DEFAULTS[norm_name]

    # ── EEO "decline" defaults — only for required fields with no profile data ──
    if field.required:
        for norm_name in candidate_norms:
            if norm_name in _EEO_DECLINE_DEFAULTS:
                return _EEO_DECLINE_DEFAULTS[norm_name]

    return None


def _is_skill_like(field_name: str) -> bool:
    n = normalize_name(field_name)
    return bool(re.search(r"\bskills?\b", n) or re.search(r"\btechnolog(y|ies)\b", n))


def _is_navigation_field(field: FormField) -> bool:
    if field.field_type != "button-group":
        return False
    choices_lower = [c.lower() for c in (field.choices or [])]
    nav_keywords = {"next", "continue", "back", "previous", "save", "cancel", "submit"}
    return any(c in nav_keywords for c in choices_lower)


# ── Core action function ─────────────────────────────────────────────


async def extract_visible_form_fields(page: Any) -> list[FormField]:
    """Extract visible form fields and synthetic button groups using DomHand helpers."""
    raw_json = await page.evaluate(_EXTRACT_FIELDS_JS)
    raw_fields: list[dict[str, Any]] = json.loads(raw_json) if isinstance(raw_json, str) else raw_json or []
    grouped_date_raw_fields, grouped_date_component_ids = _raw_grouped_date_fields(raw_fields)
    raw_field_hints = {
        str(raw.get("field_id", "")).strip(): _field_name_attr_hint(raw)
        for raw in raw_fields
        if str(raw.get("field_id", "")).strip()
    }
    for raw in grouped_date_raw_fields.values():
        raw_field_hints[str(raw.get("field_id", "")).strip()] = str(
            raw.get("name") or raw.get("raw_label") or raw.get("field_id") or ""
        ).strip()

    fields: list[FormField] = []
    grouped_names: set[str] = set()
    seen_ids: set[str] = set()
    emitted_grouped_date_keys: set[str] = set()

    for f_data in raw_fields:
        fid = f_data.get("field_id", "")
        if not fid or fid in seen_ids:
            continue
        date_group_key = str(f_data.get("date_group_key") or "").strip()
        if date_group_key and date_group_key in grouped_date_raw_fields:
            if date_group_key in emitted_grouped_date_keys:
                seen_ids.add(fid)
                continue
            emitted_grouped_date_keys.add(date_group_key)
            grouped_field = grouped_date_raw_fields[date_group_key]
            seen_ids.update(
                {
                    str(component_id).strip()
                    for component_id in grouped_field.get("component_field_ids", [])
                    if str(component_id).strip()
                }
            )
            grouped_field_id = str(grouped_field.get("field_id", "")).strip()
            if grouped_field_id:
                seen_ids.add(grouped_field_id)
            fields.append(
                _ensure_field_fingerprint(
                    FormField.model_validate(grouped_field),
                    name_attr_hint=raw_field_hints.get(grouped_field_id, ""),
                )
            )
            continue
        if fid in grouped_date_component_ids:
            seen_ids.add(fid)
            continue
        ftype = f_data.get("field_type", "text")
        fname = f_data.get("name", "")
        group_key = f_data.get("groupKey") or f_data.get("questionLabel") or fname or fid

        if ftype in ("checkbox", "radio"):
            normalized_group_key = normalize_name(str(group_key or "")) or str(group_key or "")
            if normalized_group_key in grouped_names:
                continue
            seen_ids.add(fid)
            siblings = [
                r
                for r in raw_fields
                if r.get("field_type") in ("checkbox", "radio")
                and (normalize_name(str(r.get("groupKey") or r.get("questionLabel") or r.get("name", ""))) or "")
                == normalized_group_key
            ]
            if len(siblings) > 1:
                grouped_names.add(normalized_group_key)
                for s in siblings:
                    seen_ids.add(s.get("field_id", ""))
                selected_choice = ""
                for s in siblings:
                    if s.get("current_value"):
                        selected_choice = s.get("itemLabel", s.get("name", "")) or ""
                        break
                field_label = f_data.get("questionLabel") or f_data.get("raw_label") or f_data.get("name") or fname
                choice_labels = [
                    str(s.get("itemLabel", s.get("name", "")) or "").strip()
                    for s in siblings
                    if str(s.get("itemLabel", s.get("name", "")) or "").strip()
                ]
                choice_norms = {normalize_name(choice) for choice in choice_labels if normalize_name(choice)}
                label_norm = normalize_name(str(field_label or ""))
                sibling_sections = []
                for sibling in siblings:
                    section_text = str(sibling.get("section", "") or "").strip()
                    if not section_text:
                        continue
                    section_norm = normalize_name(section_text)
                    if choice_norms and section_norm in choice_norms:
                        continue
                    if label_norm and section_norm == label_norm:
                        continue
                    sibling_sections.append(section_text)
                group_section = sibling_sections[0] if sibling_sections else ""
                inferred_required = any(bool(s.get("required")) for s in siblings)
                if not inferred_required and "*" in str(field_label or ""):
                    inferred_required = True
                fields.append(
                    _ensure_field_fingerprint(
                        FormField(
                            field_id=fid,
                            name=str(field_label or "").strip(),
                            field_type=f"{ftype}-group",
                            section=group_section,
                            required=inferred_required,
                            options=[],
                            choices=choice_labels,
                            is_native=False,
                            visible=True,
                            raw_label=str(field_label or "").strip() or None,
                            current_value=selected_choice,
                        ),
                        name_attr_hint=normalized_group_key,
                    )
                )
            else:
                field_label = f_data.get("questionLabel") or f_data.get("raw_label") or f_data.get("itemLabel") or fname
                field_section = str(f_data.get("section", "") or "").strip()
                item_label = str(f_data.get("itemLabel", "") or "").strip()
                if item_label and normalize_name(field_section) == normalize_name(item_label):
                    field_section = ""
                fields.append(
                    _ensure_field_fingerprint(
                        FormField(
                            field_id=fid,
                            name=str(field_label or "").strip(),
                            field_type=ftype,
                            section=field_section,
                            required=bool(f_data.get("required", False)) or "*" in str(field_label or ""),
                            is_native=False,
                            visible=True,
                            raw_label=str(field_label or "").strip() or None,
                            current_value=f_data.get("current_value", ""),
                        ),
                        name_attr_hint=_field_name_attr_hint(f_data),
                    )
                )
        else:
            seen_ids.add(fid)
            fields.append(
                _ensure_field_fingerprint(
                    FormField.model_validate(f_data),
                    name_attr_hint=_field_name_attr_hint(f_data),
                )
            )

    try:
        btn_json = await page.evaluate(_EXTRACT_BUTTON_GROUPS_JS)
        btn_groups: list[dict[str, Any]] = json.loads(btn_json) if isinstance(btn_json, str) else btn_json
        for bg in btn_groups:
            bg_id = bg.get("field_id", "")
            if bg_id and bg_id not in seen_ids:
                seen_ids.add(bg_id)
                raw_field_hints[str(bg_id).strip()] = _field_name_attr_hint(bg)
                fields.append(
                    _ensure_field_fingerprint(
                        FormField.model_validate(bg),
                        name_attr_hint=_field_name_attr_hint(bg),
                    )
                )
    except Exception as e:
        logger.debug(f"Button group extraction failed: {e}")

    for field in fields:
        _ensure_field_fingerprint(field, name_attr_hint=raw_field_hints.get(field.field_id, ""))

    return fields


async def _rescan_for_conditional_fields(
    page: Any,
    known_field_ids: set[str],
    *,
    target_section: str | None = None,
    heading_boundary: str | None = None,
    focus_fields: list[str] | None = None,
) -> list[FormField]:
    """Re-extract visible fields and return only newly revealed ones.

    After filling conditional trigger fields (e.g. "Do you require sponsorship? Yes"),
    the ATS may reveal follow-up fields.  This function re-scans the DOM and returns
    only fields whose IDs weren't present in *known_field_ids*.

    Called within a fill round to catch chained conditionals without waiting for
    the next full round.
    """
    try:
        fields = await extract_visible_form_fields(page)
    except Exception:
        return []
    fields = _filter_fields_for_scope(
        fields,
        target_section=target_section,
        heading_boundary=heading_boundary,
        focus_fields=focus_fields,
    )
    new_fields = [f for f in fields if f.field_id and f.field_id not in known_field_ids]
    if new_fields:
        logger.info(
            "domhand.conditional_rescan.new_fields",
            count=len(new_fields),
            labels=[f.name or f.field_id for f in new_fields[:5]],
        )
    return new_fields


async def _stagehand_observe_cross_reference(
    browser_session: BrowserSession,
    known_field_ids: set[str],
    all_results: list[FillFieldResult],
) -> None:
    """Cross-reference DOM extraction with Stagehand observation.

    Called once after the first fill round to log any interactive elements that
    Stagehand sees but DOM extraction missed.  This is informational — the data
    helps improve the DOM scanner over time without blocking the fill pipeline.
    """
    try:
        from ghosthands.stagehand.compat import ensure_stagehand_for_session

        layer = await ensure_stagehand_for_session(browser_session)
        if not layer.is_available:
            return

        elements = await layer.observe("Find all unfilled or empty form fields on this page")
        if not elements:
            return

        filled_labels = {r.name.lower() for r in all_results if r.success}
        missed = [
            el for el in elements
            if el.description and el.description.lower() not in filled_labels
        ]
        if missed:
            logger.info(
                "stagehand.observe_cross_ref",
                total_observed=len(elements),
                potentially_missed=len(missed),
                missed_labels=[el.description[:60] for el in missed[:10]],
            )
    except Exception as exc:
        logger.debug("stagehand.observe_cross_ref.error", error=str(exc))


async def domhand_fill(params: DomHandFillParams, browser_session: BrowserSession) -> ActionResult:
    """Fill all visible form fields using fast DOM manipulation."""
    page = await browser_session.get_current_page()
    if not page:
        return ActionResult(error="No active page found in browser session")

    base_profile_text = _get_profile_text()
    if not base_profile_text:
        return ActionResult(
            error="No user profile text found. Set GH_USER_PROFILE_TEXT or GH_USER_PROFILE_PATH env var."
        )

    profile_data = _get_profile_data()
    auth_overrides = _get_auth_override_data(params.use_auth_credentials)
    entry_data = params.entry_data if isinstance(params.entry_data, dict) and params.entry_data else None
    if not entry_data:
        entry_data = _infer_entry_data_from_scope(profile_data, params.heading_boundary, params.target_section)
    profile_text = _format_entry_profile_text(entry_data) if entry_data else base_profile_text
    evidence = _parse_profile_evidence(profile_text)
    page_url = await _safe_page_url(page)
    page_host = detect_host_from_url(page_url)
    page_context_key = await _get_page_context_key(
        page,
        fallback_marker=params.target_section or params.heading_boundary,
    )
    all_results: list[FillFieldResult] = []
    total_step_cost = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    llm_calls = 0
    model_name: str | None = None
    fields_seen: set[str] = set()
    fields_skipped: set[str] = set()  # Fields with no profile data — don't retry
    fields_capped: set[str] = set()
    settled_fields: dict[str, float] = {}  # field_key -> fill_confidence (>= 0.8 means done)

    for round_num in range(1, MAX_FILL_ROUNDS + 1):
        logger.info(f"DomHand fill round {round_num}/{MAX_FILL_ROUNDS}")

        try:
            await page.evaluate(_build_inject_helpers_js())
        except Exception as e:
            logger.warning(f"Helper injection failed (round {round_num}): {e}")

        if round_num == 1:
            try:
                await page.evaluate(_REVEAL_SECTIONS_JS)
            except Exception:
                pass

        try:
            fields = await extract_visible_form_fields(page)
        except Exception as e:
            logger.error(f"Field extraction failed: {e}")
            return ActionResult(error=f"Failed to extract form fields: {e}")

        fields = _filter_fields_for_scope(
            fields,
            target_section=params.target_section,
            heading_boundary=params.heading_boundary,
            focus_fields=params.focus_fields,
        )
        focus_selection = _resolve_focus_fields(fields, params.focus_fields)
        if params.focus_fields and focus_selection.ambiguous_labels:
            details = ", ".join(
                f"{label}: {[f'{field.field_id} ({field.field_type})' for field in matches]}"
                for label, matches in focus_selection.ambiguous_labels.items()
            )
            return ActionResult(
                error=(
                    "Multiple visible fields matched the requested focus_fields. "
                    f"Disambiguate with the exact blocker field_id before retrying. {details}"
                ),
            )
        fields = focus_selection.fields if params.focus_fields else fields
        fields, blockers_unchanged = _active_blocker_focus_fields(
            browser_session,
            fields=fields,
            page_context_key=page_context_key,
            page_url=page_url,
        )
        if blockers_unchanged:
            if fields:
                chosen_next_strategy = (
                    "direct_widget_interaction"
                    if all(_is_required_custom_widget_boolean_select(field) for field in fields)
                    else "change_strategy"
                )
                _log_loop_blocker_state(
                    browser_session,
                    fields=fields,
                    chosen_next_strategy=chosen_next_strategy,
                )
            if fields and all(_is_required_custom_widget_boolean_select(field) for field in fields):
                return ActionResult(
                    error=(
                        "Latest domhand_assess_state still shows the same required custom-widget boolean blocker(s) on this page context. "
                        "Do NOT call domhand_fill again for these fields. Open one widget, click the visible Yes/No option directly, then reassess."
                    ),
                )
            return ActionResult(
                error=(
                    "Latest domhand_assess_state shows the same blocker state on this page context. "
                    "Do not retry domhand_fill on these blockers again. Switch to domhand_interact_control, "
                    "domhand_select, or browser-use/manual for one current blocker."
                ),
            )

        if params.heading_boundary and not fields:
            return ActionResult(
                error=(
                    f'No visible fields matched heading boundary "{params.heading_boundary}". '
                    "Verify the entry heading is visible before calling domhand_fill."
                ),
            )
        if params.focus_fields and not fields:
            return ActionResult(
                error=(
                    "No visible blocker fields matched the requested focus_fields on the current page context. "
                    "Use domhand_assess_state again before retrying another blocker."
                ),
            )

        fillable_fields: list[FormField] = []
        for f in fields:
            if f.field_type == "file":
                continue
            key = get_stable_field_key(f)
            if key in fields_skipped:
                continue
            if key in fields_capped:
                continue
            if settled_fields.get(key, 0.0) >= 0.8:
                continue  # Already filled with high confidence — anti-loop gate
            if key in fields_seen and _field_has_effective_value(f):
                continue
            if _is_navigation_field(f):
                continue
            fillable_fields.append(f)

        if not fillable_fields:
            if round_num == 1:
                return ActionResult(
                    extracted_content="No fillable form fields found on the page.",
                    include_extracted_content_only_once=True,
                    metadata={
                        "step_cost": total_step_cost,
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                        "model": model_name,
                        "domhand_llm_calls": llm_calls,
                    },
                )
            break

        logger.info(f"Round {round_num}: {len(fillable_fields)} fillable fields found")

        needs_llm: list[FormField] = []
        direct_fills: dict[str, str] = {}
        resolved_values: dict[str, ResolvedFieldValue] = {}
        resolved_bindings: dict[str, ResolvedFieldBinding] = {}
        fillable_field_map = {field.field_id: field for field in fillable_fields}
        # When focus_fields is set, the agent explicitly targets these fields
        # (usually because of a validation error despite the field appearing filled).
        # Do NOT skip them — the value may be in the DOM but not registered by the
        # ATS framework (e.g. Workday React state).
        _force_refill = bool(params.focus_fields)
        for f in fillable_fields:
            if _field_has_effective_value(f) and not _force_refill:
                fields_seen.add(get_stable_field_key(f))
                continue
            auth_val = _known_auth_override_for_field(f, auth_overrides)
            if auth_val:
                direct_fills[f.field_id] = auth_val
                resolved_values[f.field_id] = _resolved_field_value(
                    auth_val,
                    source="exact_profile",
                    answer_mode="profile_backed",
                    confidence=1.0,
                )
                continue
            if auth_overrides and _is_auth_like_field(f):
                fr = FillFieldResult(
                    field_id=f.field_id,
                    name=_preferred_field_label(f),
                    success=False,
                    actor="skipped",
                    error="Missing auth override for auth field",
                    value_set=None,
                    required=f.required,
                    control_kind=f.field_type,
                    section=f.section or "",
                    source="dom",
                    confidence=1.0,
                    state="failed",
                    failure_reason="auth_override_missing",
                    takeover_suggestion=(
                        "Retry domhand_fill with use_auth_credentials=true or use a targeted "
                        "browser-use input action for this auth field only."
                    ),
                )
                all_results.append(fr)
                if _on_field_result:
                    _on_field_result(fr, round_num)
                continue
            is_structured_language = _is_structured_language_field(f)
            structured_language_diag = (
                StructuredRepeaterDiagnostic(
                    repeater_group="languages",
                    field_id=f.field_id,
                    field_label=_preferred_field_label(f),
                    section=f.section or "",
                    slot_name=_language_slot_name(f),
                    numeric_index=_language_entry_index_for_field(f),
                    current_value=str(f.current_value or "").strip(),
                )
                if is_structured_language
                else None
            )
            structured_language_val = None
            if is_structured_language and profile_data:
                raw_languages = [entry for entry in (profile_data.get("languages") or []) if isinstance(entry, dict)]
                if structured_language_diag and not structured_language_diag.slot_name:
                    structured_language_diag.failure_stage = "slot_unresolved"
                structured_language_binding = _resolve_repeater_binding(
                    host=page_host,
                    repeater_group="languages",
                    field=f,
                    visible_fields=fillable_fields,
                    entries=raw_languages,
                    numeric_index=structured_language_diag.numeric_index
                    if structured_language_diag
                    else _language_entry_index_for_field(f),
                    slot_name=structured_language_diag.slot_name
                    if structured_language_diag
                    else _language_slot_name(f),
                    current_value=f.current_value,
                    slot_resolver=_language_slot_name,
                )
                if structured_language_binding:
                    _set_structured_repeater_binding(structured_language_diag, structured_language_binding)
                    raw_structured_language_val, raw_structured_language_source = (
                        _structured_language_raw_value_and_source_from_entry(
                            f,
                            raw_languages[structured_language_binding.entry_index],
                        )
                    )
                    if raw_structured_language_val:
                        structured_language_val = _coerce_answer_to_field(f, raw_structured_language_val)
                        if structured_language_val:
                            _set_structured_repeater_resolved_value(
                                structured_language_diag,
                                structured_language_val,
                                source_key=raw_structured_language_source,
                            )
                        elif structured_language_diag:
                            structured_language_diag.failure_stage = "value_coercion_empty"
                    elif structured_language_diag:
                        structured_language_diag.failure_stage = "entry_value_missing"
                    if structured_language_val:
                        resolved_bindings[f.field_id] = structured_language_binding
                elif structured_language_diag:
                    structured_language_diag.failure_stage = (
                        "binding_unresolved" if len(raw_languages) > 1 else "entry_value_missing"
                    )
            if not structured_language_val:
                structured_language_val = _resolve_structured_language_value(f, profile_data)
                if structured_language_val:
                    _set_structured_repeater_resolved_value(
                        structured_language_diag,
                        structured_language_val,
                        source_key="fallback_structured_language",
                    )
                elif structured_language_diag and not structured_language_diag.failure_stage:
                    structured_language_diag.failure_stage = (
                        "slot_unresolved" if not structured_language_diag.slot_name else "entry_value_missing"
                    )
            if structured_language_val:
                _trace_structured_repeater_resolution(f, structured_language_diag)
                direct_fills[f.field_id] = structured_language_val
                resolved_values[f.field_id] = _resolved_field_value(
                    structured_language_val,
                    source="exact_profile",
                    answer_mode="profile_backed",
                    confidence=1.0,
                )
                continue
            if is_structured_language:
                if structured_language_diag and not structured_language_diag.failure_stage:
                    structured_language_diag.failure_stage = (
                        "slot_unresolved" if not structured_language_diag.slot_name else "entry_value_missing"
                    )
                _trace_structured_repeater_resolution(f, structured_language_diag)
                fr = _structured_repeater_fill_result(
                    f,
                    structured_language_diag
                    or StructuredRepeaterDiagnostic(
                        repeater_group="languages",
                        field_id=f.field_id,
                        field_label=_preferred_field_label(f),
                        section=f.section or "",
                        current_value=str(f.current_value or "").strip(),
                        failure_stage="entry_value_missing",
                    ),
                )
                all_results.append(fr)
                if _on_field_result:
                    _on_field_result(fr, round_num)
                fields_seen.add(key)
                fields_skipped.add(key)
                continue
            entry_val = _known_entry_value_for_field(f, entry_data)
            is_structured_education_candidate = _is_structured_education_candidate(f, fillable_fields)
            structured_education_diag = (
                StructuredRepeaterDiagnostic(
                    repeater_group="education",
                    field_id=f.field_id,
                    field_label=_preferred_field_label(f),
                    section=f.section or "",
                    slot_name=_education_slot_name(f, fillable_fields),
                    numeric_index=(_parse_heading_index(f.section) - 1)
                    if _parse_heading_index(f.section) is not None
                    else None,
                    current_value=str(f.current_value or "").strip(),
                )
                if is_structured_education_candidate
                else None
            )
            if structured_education_diag and entry_val:
                _set_structured_repeater_resolved_value(structured_education_diag, entry_val)
            if not entry_val and is_structured_education_candidate and profile_data:
                raw_education = [entry for entry in (profile_data.get("education") or []) if isinstance(entry, dict)]
                section_binding = next(
                    (
                        binding
                        for bound_field_id, binding in resolved_bindings.items()
                        if bound_field_id != f.field_id
                        and bound_field_id in fillable_field_map
                        and _is_structured_education_candidate(fillable_field_map[bound_field_id], fillable_fields)
                        and normalize_name(fillable_field_map[bound_field_id].section or "")
                        == normalize_name(f.section or "")
                    ),
                    None,
                )
                if structured_education_diag:
                    structured_education_diag.section_binding_reused = section_binding is not None
                    if not structured_education_diag.slot_name:
                        structured_education_diag.failure_stage = "slot_unresolved"

                def education_slot_resolver(candidate: FormField) -> str | None:
                    return _education_slot_name(candidate, fillable_fields)

                structured_education_binding = section_binding or _resolve_repeater_binding(
                    host=page_host,
                    repeater_group="education",
                    field=f,
                    visible_fields=fillable_fields,
                    entries=raw_education,
                    numeric_index=structured_education_diag.numeric_index
                    if structured_education_diag
                    else (_parse_heading_index(f.section) - 1)
                    if _parse_heading_index(f.section) is not None
                    else None,
                    slot_name=structured_education_diag.slot_name
                    if structured_education_diag
                    else _education_slot_name(f, fillable_fields),
                    current_value=f.current_value,
                    slot_resolver=education_slot_resolver,
                )
                if structured_education_binding:
                    _set_structured_repeater_binding(structured_education_diag, structured_education_binding)
                    raw_structured_education_val, raw_structured_education_source = (
                        _structured_education_raw_value_and_source_from_entry(
                            f,
                            raw_education[structured_education_binding.entry_index],
                            fillable_fields,
                        )
                    )
                    if raw_structured_education_val:
                        entry_val = _coerce_answer_to_field(f, raw_structured_education_val)
                        if entry_val:
                            _set_structured_repeater_resolved_value(
                                structured_education_diag,
                                entry_val,
                                source_key=raw_structured_education_source,
                            )
                        elif structured_education_diag:
                            structured_education_diag.failure_stage = "value_coercion_empty"
                    elif structured_education_diag:
                        structured_education_diag.failure_stage = "entry_value_missing"
                    if entry_val:
                        resolved_bindings[f.field_id] = structured_education_binding
                elif structured_education_diag:
                    structured_education_diag.failure_stage = (
                        "binding_unresolved" if len(raw_education) > 1 else "entry_value_missing"
                    )
            if entry_val:
                coerced_entry_val = _coerce_answer_to_field(f, entry_val)
                if coerced_entry_val:
                    if structured_education_diag and structured_education_diag.failure_stage != "resolved":
                        _set_structured_repeater_resolved_value(
                            structured_education_diag,
                            coerced_entry_val,
                            source_key=structured_education_diag.resolved_source_key,
                        )
                    _trace_structured_repeater_resolution(f, structured_education_diag)
                    direct_fills[f.field_id] = coerced_entry_val
                    resolved_values[f.field_id] = _resolved_field_value(
                        coerced_entry_val,
                        source="exact_profile",
                        answer_mode="profile_backed",
                        confidence=0.99,
                    )
                    continue
                if structured_education_diag and not structured_education_diag.failure_stage:
                    structured_education_diag.failure_stage = "value_coercion_empty"
            if is_structured_education_candidate:
                if structured_education_diag and not structured_education_diag.failure_stage:
                    structured_education_diag.failure_stage = (
                        "slot_unresolved" if not structured_education_diag.slot_name else "entry_value_missing"
                    )
                _trace_structured_repeater_resolution(f, structured_education_diag)
                fr = _structured_repeater_fill_result(
                    f,
                    structured_education_diag
                    or StructuredRepeaterDiagnostic(
                        repeater_group="education",
                        field_id=f.field_id,
                        field_label=_preferred_field_label(f),
                        section=f.section or "",
                        current_value=str(f.current_value or "").strip(),
                        failure_stage="entry_value_missing",
                    ),
                )
                all_results.append(fr)
                if _on_field_result:
                    _on_field_result(fr, round_num)
                fields_seen.add(key)
                fields_skipped.add(key)
                continue
            known_resolution = _resolve_known_profile_value_for_field(
                f,
                evidence,
                profile_data,
                minimum_confidence="medium" if f.required else "strong",
            )
            if known_resolution:
                direct_fills[f.field_id] = known_resolution.value
                resolved_values[f.field_id] = known_resolution
                continue
            semantic_profile_val = await _semantic_profile_value_for_field(
                f,
                evidence,
                profile_data,
            )
            if semantic_profile_val:
                direct_fills[f.field_id] = semantic_profile_val
                resolved_values[f.field_id] = _resolved_field_value(
                    semantic_profile_val,
                    source="derived_profile",
                    answer_mode="profile_backed",
                    confidence=0.78,
                )
                continue
            needs_llm.append(f)

        answers: dict[str, str] = {}
        if needs_llm:
            llm_answers, in_tok, out_tok, step_cost, llm_model_name = await _generate_answers(
                needs_llm,
                profile_text,
                profile_data=profile_data,
            )
            _record_page_token_cost(
                browser_session,
                page_context_key=page_context_key,
                target_section=params.target_section,
                field_count=len(needs_llm),
                input_tokens=in_tok,
                output_tokens=out_tok,
            )
            answers = llm_answers
            total_step_cost += step_cost
            total_input_tokens += in_tok
            total_output_tokens += out_tok
            llm_calls += 1
            if llm_model_name:
                model_name = llm_model_name

        _llm_keys = list(answers.keys())[:20] if answers else []
        _llm_sample = (
            {k: (v[:60] if isinstance(v, str) else v) for k, v in list(answers.items())[:5]} if answers else {}
        )
        logger.warning(
            "domhand.fill_round_triage",
            direct_fills=len(direct_fills),
            needs_llm=len(needs_llm),
            llm_answer_keys=_llm_keys,
            llm_answer_sample=_llm_sample,
        )

        round_filled = 0
        round_failed = 0

        for f in fillable_fields:
            key = get_stable_field_key(f)
            if f.field_id in direct_fills:
                value = direct_fills[f.field_id]
                resolved_value = resolved_values.get(
                    f.field_id,
                    _resolved_field_value(
                        value,
                        source="dom",
                        answer_mode="profile_backed",
                        confidence=0.95,
                    ),
                )
                success, field_error, failure_reason, fc = await _attempt_domhand_fill_with_retry_cap(
                    page,
                    host=page_host,
                    field=f,
                    desired_value=value,
                    tool_name="domhand_fill",
                    browser_session=browser_session,
                )
                fr = FillFieldResult(
                    field_id=f.field_id,
                    name=_preferred_field_label(f),
                    success=success,
                    actor="dom",
                    value_set=value if success else None,
                    error=None if success else field_error,
                    required=f.required,
                    control_kind=f.field_type,
                    section=f.section or "",
                    source=resolved_value.source,
                    answer_mode=resolved_value.answer_mode,
                    confidence=resolved_value.confidence,
                    fill_confidence=fc,
                    state=resolved_value.state if success else "failed",
                    failure_reason=None if success else failure_reason,
                    takeover_suggestion=_takeover_suggestion_for_field(
                        f,
                        success,
                        "dom",
                        None if success else field_error,
                    ),
                    binding_mode=resolved_bindings.get(f.field_id).binding_mode
                    if resolved_bindings.get(f.field_id)
                    else None,
                    binding_confidence=resolved_bindings.get(f.field_id).binding_confidence
                    if resolved_bindings.get(f.field_id)
                    else None,
                    best_effort_guess=resolved_bindings.get(f.field_id).best_effort_guess
                    if resolved_bindings.get(f.field_id)
                    else False,
                )
                if success:
                    confirm_learned_question_alias(_preferred_field_label(f))
                    settled = await _record_expected_value_if_settled(
                        page=page,
                        host=page_host,
                        page_context_key=page_context_key,
                        field=f,
                        field_key=key,
                        expected_value=value,
                        source="exact_profile" if resolved_value.source == "exact_profile" else "derived_profile",
                        log_context="domhand.fill",
                    )
                    if not settled:
                        _record_unverified_custom_select_intent(
                            host=page_host,
                            page_context_key=page_context_key,
                            field=f,
                            field_key=key,
                            intended_value=value,
                    )
                elif failure_reason == DOMHAND_RETRY_CAPPED:
                    fields_capped.add(key)
                all_results.append(fr)
                if _on_field_result:
                    _on_field_result(fr, round_num)
                fields_seen.add(key)
                round_filled += 1 if success else 0
                round_failed += 0 if success else 1

        _needs_llm_keys = _disambiguated_field_names(needs_llm)
        for _nli, f in enumerate(needs_llm):
            key = get_stable_field_key(f)
            batch_llm_key = _needs_llm_keys[_nli]
            resolved_value = _resolve_llm_answer_via_batch_key(f, batch_llm_key, answers)
            if resolved_value is None:
            resolved_value = _resolve_llm_answer_for_field(f, answers, evidence, profile_data)
            _rejection_reason = ""
            if resolved_value and re.match(
                r"^(n/?a|na|none|not applicable|unknown|placeholder)$",
                resolved_value.value.strip(),
                re.IGNORECASE,
            ):
                _rejection_reason = f"na_filter:{resolved_value.value[:40]}"
                resolved_value = None
            if resolved_value and "[NEEDS_USER_INPUT]" in resolved_value.value:
                _rejection_reason = "needs_user_input"
                resolved_value = None
            if not resolved_value or not resolved_value.value:
                if not _rejection_reason:
                    _rejection_reason = "resolve_returned_none"
                _label_cands = _field_label_candidates(f)
                logger.warning(
                    "domhand.fill_field_skipped",
                    label=_preferred_field_label(f),
                    name=f.name,
                    field_type=f.field_type,
                    required=f.required,
                    candidates=_label_cands,
                    rejection=_rejection_reason,
                    is_non_guess=any(_is_non_guess_name_fragment(l) for l in _label_cands),
                    is_skill_like=_is_skill_like(f.name),
                )
                error_msg = "No confident profile match for this field"
                if f.required:
                    error_msg = "REQUIRED — could not fill automatically"
                fr = FillFieldResult(
                    field_id=f.field_id,
                    name=_preferred_field_label(f),
                    success=False,
                    actor="skipped",
                    error=error_msg,
                    required=f.required,
                    control_kind=f.field_type,
                    section=f.section or "",
                    state="failed",
                    failure_reason="missing_profile_data" if not f.required else "required_missing_profile_data",
                    takeover_suggestion=_takeover_suggestion_for_field(
                        f,
                        False,
                        "skipped",
                        error_msg,
                    ),
                )
                all_results.append(fr)
                if _on_field_result:
                    _on_field_result(fr, round_num)
                fields_seen.add(key)
                fields_skipped.add(key)
                continue
            matched_answer = resolved_value.value
            success, field_error, failure_reason, fc = await _attempt_domhand_fill_with_retry_cap(
                page,
                host=page_host,
                field=f,
                desired_value=matched_answer,
                tool_name="domhand_fill",
                browser_session=browser_session,
            )
            fr = FillFieldResult(
                field_id=f.field_id,
                name=_preferred_field_label(f),
                success=success,
                actor="dom",
                value_set=matched_answer if success else None,
                error=None if success else field_error,
                required=f.required,
                control_kind=f.field_type,
                section=f.section or "",
                source=resolved_value.source,
                answer_mode=resolved_value.answer_mode,
                confidence=resolved_value.confidence,
                fill_confidence=fc,
                state=resolved_value.state if success else "failed",
                failure_reason=None if success else failure_reason,
                takeover_suggestion=_takeover_suggestion_for_field(
                    f,
                    success,
                    "dom",
                    None if success else field_error,
                ),
                binding_mode=resolved_bindings.get(f.field_id).binding_mode
                if resolved_bindings.get(f.field_id)
                else None,
                binding_confidence=resolved_bindings.get(f.field_id).binding_confidence
                if resolved_bindings.get(f.field_id)
                else None,
                best_effort_guess=resolved_bindings.get(f.field_id).best_effort_guess
                if resolved_bindings.get(f.field_id)
                else False,
            )
            if success:
                confirm_learned_question_alias(_preferred_field_label(f))
                settled = await _record_expected_value_if_settled(
                    page=page,
                    host=page_host,
                    page_context_key=page_context_key,
                    field=f,
                    field_key=key,
                    expected_value=matched_answer,
                    source="exact_profile" if resolved_value.source == "exact_profile" else "derived_profile",
                    log_context="domhand.fill",
                )
                if not settled:
                    _record_unverified_custom_select_intent(
                        host=page_host,
                        page_context_key=page_context_key,
                        field=f,
                        field_key=key,
                        intended_value=matched_answer,
                )
            elif failure_reason == DOMHAND_RETRY_CAPPED:
                fields_capped.add(key)
            all_results.append(fr)
            if _on_field_result:
                _on_field_result(fr, round_num)
            fields_seen.add(key)
            round_filled += 1 if success else 0
            round_failed += 0 if success else 1

        logger.info(f"Round {round_num}: filled={round_filled}, failed={round_failed}")
        if round_filled == 0:
            break

        known_ids = {f.field_id for f in fillable_fields if f.field_id}

        if browser_session and round_num == 1:
            await _stagehand_observe_cross_reference(
                browser_session, known_ids | fields_seen, all_results
            )

        for cond_pass in range(1, MAX_CONDITIONAL_PASSES + 1):
            await asyncio.sleep(0.3)
            new_fields = await _rescan_for_conditional_fields(
                page,
                known_ids | fields_seen,
                target_section=params.target_section,
                heading_boundary=params.heading_boundary,
                focus_fields=params.focus_fields,
            )
            if not new_fields:
                break
            logger.info(
                f"Round {round_num} conditional pass {cond_pass}: "
                f"{len(new_fields)} new fields revealed"
            )
            cond_filled = 0
            for f in new_fields:
                key = get_stable_field_key(f)
                if key in fields_seen or key in fields_skipped or key in fields_capped:
                    continue
                known_ids.add(f.field_id)
                matched_answer = direct_fills.get(f.field_id) if direct_fills else None
                if not matched_answer and answers:
                    matched_answer = answers.get(f.field_id)
                if not matched_answer:
                    fields_skipped.add(key)
                    continue
                success, failure_msg, failure_reason, fc = await _attempt_domhand_fill_with_retry_cap(
                    page,
                    host=page_host,
                    field=f,
                    desired_value=matched_answer,
                    tool_name="domhand_fill",
                    browser_session=browser_session,
                )
                fr = FillFieldResult(
                    field_id=f.field_id,
                    control_kind=f.field_type,
                    name=_preferred_field_label(f),
                    section=f.section or "",
                    success=success,
                    actor="dom",
                    error=failure_msg if not success else None,
                    failure_reason=failure_reason if not success else None,
                    value_set=matched_answer if success else None,
                    answer_mode="conditional_reveal",
                    fill_confidence=fc,
                )
                all_results.append(fr)
                if _on_field_result:
                    _on_field_result(fr, round_num)
                fields_seen.add(key)
                cond_filled += 1 if success else 0
            if cond_filled == 0:
                break

        for r in all_results:
            rkey = r.field_id
            if r.fill_confidence > settled_fields.get(rkey, 0.0):
                settled_fields[rkey] = r.fill_confidence

        await asyncio.sleep(0.5)

    filled_count = sum(1 for r in all_results if r.success)
    failed_count = sum(1 for r in all_results if not r.success and r.actor == "dom")
    skipped_count = sum(1 for r in all_results if r.actor == "skipped")
    unfilled_count = sum(1 for r in all_results if r.actor == "unfilled")
    best_effort_results = [r for r in all_results if r.success and r.answer_mode == "best_effort_guess" and r.value_set]
    best_effort_binding_results = [r for r in all_results if r.success and r.best_effort_guess]
    required_skipped = [
        f'  - "{r.name}" (REQUIRED — needs attention)'
        for r in all_results
        if r.actor == "skipped" and r.error and "REQUIRED" in r.error
    ]
    optional_skipped = [
        f'  - "{r.name}" ({r.error or "no confident profile match"})'
        for r in all_results
        if r.actor == "skipped" and (not r.error or "REQUIRED" not in r.error)
    ]
    failed_descriptions = [
        f'  - "{r.name}" ({r.error or "DOM fill failed"})' for r in all_results if not r.success and r.actor == "dom"
    ]
    summary_lines = [
        f"DomHand fill complete: {filled_count} filled, {failed_count} DOM failures, {skipped_count} skipped (no data), {unfilled_count} unfilled.",
        f"LLM calls: {llm_calls} (input: {total_input_tokens} tokens, output: {total_output_tokens} tokens)",
    ]
    if required_skipped:
        summary_lines.append("REQUIRED fields that need attention (fill these using click/select):")
        summary_lines.extend(required_skipped[:20])
    if optional_skipped:
        summary_lines.append("Skipped optional fields (no confident profile match):")
        summary_lines.extend(optional_skipped[:20])
        if len(optional_skipped) > 20:
            summary_lines.append(f"  ... and {len(optional_skipped) - 20} more")
    if failed_descriptions:
        summary_lines.append("Failed fields (retry even if optional when profile data exists):")
        summary_lines.extend(failed_descriptions[:20])
    if best_effort_results:
        summary_lines.append("Best-effort guesses used (review these answers before submit):")
        summary_lines.extend(
            [f'  - "{r.name}"' + (f" [{r.section}]" if r.section else "") for r in best_effort_results[:20]]
        )
    if best_effort_binding_results:
        summary_lines.append("Best-effort repeater bindings used (review these answers before submit):")
        summary_lines.extend(
            [
                f'  - "{r.name}"'
                + (f" [{r.section}]" if r.section else "")
                + (f" via {r.binding_mode}" if r.binding_mode else "")
                for r in best_effort_binding_results[:20]
            ]
        )

    confident_fields = [
        r for r in all_results if r.success and r.fill_confidence >= 0.8
    ]
    if confident_fields:
        summary_lines.append(
            f"Confidently filled fields ({len(confident_fields)}) — DO NOT re-fill or re-verify these:"
        )
        summary_lines.extend(
            [f'  - "{r.name}"' + (f" [{r.section}]" if r.section else "") for r in confident_fields[:30]]
        )

    low_confidence_fields = [
        r for r in all_results if not r.success or (r.success and r.fill_confidence < 0.4)
    ]
    if low_confidence_fields:
        summary_lines.append(
            f"Low-confidence fields ({len(low_confidence_fields)}) — may need Stagehand or manual intervention:"
        )
        summary_lines.extend(
            [
                f'  - "{r.name}" (confidence={r.fill_confidence:.1f}, error={r.error or "none"})'
                for r in low_confidence_fields[:15]
            ]
        )

    _failed_all = [r for r in all_results if not r.success]
    structured_summary_full = {
        "filled_count": filled_count,
        "dom_failure_count": failed_count,
        "skipped_count": skipped_count,
        "unfilled_count": unfilled_count,
        "best_effort_guess_count": len(best_effort_results),
        "best_effort_binding_count": len(best_effort_binding_results),
        "best_effort_guess_fields": [
            {
                "field_id": r.field_id,
                "prompt_text": r.name,
                "section_label": r.section or None,
                "required": r.required,
            }
            for r in best_effort_results
        ],
        "best_effort_binding_fields": [
            {
                "field_id": r.field_id,
                "prompt_text": r.name,
                "section_label": r.section or None,
                "binding_mode": r.binding_mode,
                "binding_confidence": r.binding_confidence,
                "best_effort_guess": r.best_effort_guess,
            }
            for r in best_effort_binding_results
        ],
        "unresolved_required_fields": [
            _fill_result_summary_entry(r) for r in all_results if not r.success and r.required
        ],
        "failed_fields": [_fill_result_summary_entry(r) for r in _failed_all],
    }
    logger.debug(
        "domhand.fill.full_structured_summary %s",
        json.dumps(structured_summary_full, ensure_ascii=True),
    )

    _agent_failed = [_fill_result_summary_entry_for_agent(r) for r in _failed_all][:_AGENT_FILL_MAX_FAILED_FIELDS]
    structured_summary_agent = {
        "filled_count": filled_count,
        "dom_failure_count": failed_count,
        "skipped_count": skipped_count,
        "unfilled_count": unfilled_count,
        "best_effort_guess_count": len(best_effort_results),
        "best_effort_binding_count": len(best_effort_binding_results),
        "best_effort_guess_fields": [
            {
                "field_id": r.field_id,
                "prompt_text": _truncate_agent_fill_text(r.name, _AGENT_FILL_NAME_MAX_LEN),
                "section_label": _truncate_agent_fill_text(r.section, _AGENT_FILL_SECTION_MAX_LEN) or None,
                "required": r.required,
            }
            for r in best_effort_results[:25]
        ],
        "best_effort_binding_fields": [
            {
                "field_id": r.field_id,
                "prompt_text": _truncate_agent_fill_text(r.name, _AGENT_FILL_NAME_MAX_LEN),
                "section_label": _truncate_agent_fill_text(r.section, _AGENT_FILL_SECTION_MAX_LEN) or None,
                "binding_mode": r.binding_mode,
                "binding_confidence": r.binding_confidence,
                "best_effort_guess": r.best_effort_guess,
            }
            for r in best_effort_binding_results[:25]
        ],
        "unresolved_required_fields": [
            _fill_result_summary_entry_for_agent(r) for r in all_results if not r.success and r.required
        ][:25],
        "failed_fields": _agent_failed,
        "failed_fields_total": len(_failed_all),
    }
    if len(_failed_all) > _AGENT_FILL_MAX_FAILED_FIELDS:
        structured_summary_agent["failed_fields_truncated"] = len(_failed_all) - _AGENT_FILL_MAX_FAILED_FIELDS

    summary_lines.append("DOMHAND_FILL_JSON:")
    summary_lines.append(json.dumps(structured_summary_agent, ensure_ascii=True))

    summary = "\n".join(summary_lines)
    logger.info(summary)
    return ActionResult(
        extracted_content=summary,
        include_extracted_content_only_once=False,
        metadata={
            "step_cost": total_step_cost,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "model": model_name,
            "domhand_llm_calls": llm_calls,
        },
    )


# ── Per-field fill dispatch ──────────────────────────────────────────



def _get_profile_text() -> str | None:
    # Prefer file-based path (secure, avoids /proc/pid/environ exposure)
    path = os.environ.get("GH_USER_PROFILE_PATH", "")
    if path:
        try:
            import pathlib

            p = pathlib.Path(path)
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning(f"Failed to read profile from {path}: {e}")
    # Fallback to env var for backwards compat (desktop bridge)
    text = os.environ.get("GH_USER_PROFILE_TEXT", "")
    if text.strip():
        return text.strip()
    return None


def _get_profile_data() -> dict[str, Any]:
    """Return structured applicant profile data when available."""

    def _normalize(parsed: dict[str, Any]) -> dict[str, Any]:
        return camel_to_snake_profile(parsed)

    # Prefer file-based path (secure, avoids /proc/pid/environ exposure)
    path = os.environ.get("GH_USER_PROFILE_PATH", "")
    if path:
        try:
            import pathlib

            p = pathlib.Path(path)
            if p.is_file():
                parsed = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    return _normalize(parsed)
        except Exception as e:
            logger.warning(f"Failed to parse profile JSON from {path}: {e}")

    # Fallback to env vars for backwards compat
    raw_json = os.environ.get("GH_USER_PROFILE_JSON", "")
    if raw_json.strip():
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                return _normalize(parsed)
        except Exception as e:
            logger.warning(f"Failed to parse GH_USER_PROFILE_JSON: {e}")

    text = os.environ.get("GH_USER_PROFILE_TEXT", "")
    if text.strip():
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return _normalize(parsed)
        except Exception:
            pass

    return {}
