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
6. Repeats for up to ``MAX_FILL_ROUNDS`` rounds (extra round for conditional reveals)
7. Returns ``ActionResult`` with filled/failed/unfilled counts
"""

import asyncio
import contextlib
import inspect
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import structlog

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from ghosthands.actions.combobox_toggle import (  # noqa: F401
    CLICK_COMBOBOX_TOGGLE_BY_FFID_JS,
    CLICK_INPUT_BY_FFID_JS,
    combobox_toggle_clicked,
)
from ghosthands.actions.views import (
    DomHandFillParams,
    FillFieldResult,
    FormField,
    get_stable_field_key,
    is_placeholder_value,
    normalize_name,
)
from ghosthands.bridge.profile_adapter import camel_to_snake_profile
from ghosthands.dom.dropdown_fill import DropdownFillResult, fill_interactive_dropdown  # noqa: F401
from ghosthands.dom.dropdown_match import (  # noqa: F401
    CLICK_DROPDOWN_OPTION_ENHANCED_JS,
    SCAN_VISIBLE_OPTIONS_JS,
    match_dropdown_option,
    synonym_groups_for_js,
)
from ghosthands.dom.dropdown_verify import selection_matches_desired  # noqa: F401
from ghosthands.dom.fill_browser_scripts import (
    _CLICK_BINARY_FIELD_JS,
    _CLICK_RADIO_OPTION_JS,
    _DISMISS_DROPDOWN_SOFT_JS,
    _EXTRACT_FIELDS_JS,
    _PAGE_CONTEXT_SCAN_JS,
    _REVEAL_SECTIONS_JS,
)
from ghosthands.dom.fill_executor import (  # noqa: F401
    _EXCLUSIVE_CHOICE_CHECKBOX_PROMPT_RE,
    _EXCLUSIVE_CHOICE_OPTION_PREFIXES,
    _MULTI_SELECT_CHECKBOX_PROMPT_RE,
    _checkbox_group_is_exclusive_choice,
    _checkbox_group_mode,
    _clear_dropdown_search,
    _click_away_from_text_like_field,
    _click_binary_with_gui,
    _click_dropdown_option,
    _click_group_option_with_gui,
    _coerce_salary_numeric_candidate,
    _confirm_text_like_value,
    _field_has_effective_value,
    _field_has_validation_error,
    _field_needs_blur_revalidation,
    _field_needs_enter_commit,
    _field_value_matches_expected,
    _fill_button_group,
    _fill_checkbox,
    _fill_checkbox_group,
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
    _type_text_compat,
    _type_and_click_dropdown_option,
    _visible_field_id_snapshot,
    _wait_for_field_value,
)
from ghosthands.dom.fill_label_match import (  # noqa: F401
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
from ghosthands.dom.fill_profile_resolver import (  # noqa: F401
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
from ghosthands.dom.fill_resolution import (  # noqa: F401
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
from ghosthands.profile.canonical import build_canonical_profile  # noqa: F401
from ghosthands.runtime_learning import (
    DOMHAND_RETRY_CAP,
    build_page_context_key,
    confirm_learned_question_alias,
    detect_host_from_url,
    record_expected_field_value,
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
PRE_LLM_OPTION_ENRICHMENT_POLLS = 12

PRE_LLM_OPTION_ENRICHMENT_POLL_SECONDS = 0.22
PRE_LLM_OPTION_ENRICHMENT_SETTLE_MATCHES = 2
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
_EMPTY_MULTISELECT_FRAGMENT_RE = re.compile(
    r"^(0\s+items?\s+selected|no\s+items?\s+selected)$",
    re.IGNORECASE,
)
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
        field_key=get_stable_field_key(field),
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


async def _refresh_live_field_state(page: Any, field: FormField) -> tuple[str, bool]:
    if field.field_type == "checkbox-group":
        field.current_value = await _read_checkbox_group_value(page, field)
    elif field.field_type in {"radio-group", "button-group"}:
        observed_value = await _read_group_selection(page, field.field_id)
        if observed_value or not field.current_value:
            field.current_value = observed_value or field.current_value
    elif field.field_type in {"checkbox", "toggle"}:
        binary_state = await _read_binary_state(page, field.field_id)
        field.current_value = "checked" if binary_state else ""
    else:
        observed_value = await _read_field_value_for_field(page, field)
        if observed_value or not field.current_value:
            field.current_value = observed_value or field.current_value
    has_error = await _field_has_validation_error(page, field.field_id)
    return str(field.current_value or "").strip(), has_error


async def _poll_live_field_state_until_settled(
    page: Any,
    field: FormField,
    *,
    attempts: int = 4,
    wait_s: float = 0.4,
) -> tuple[str, bool]:
    current_value = ""
    has_error = False
    for attempt in range(max(attempts, 1)):
        current_value, has_error = await _refresh_live_field_state(page, field)
        if _field_has_effective_value(field) and not has_error:
            return current_value, has_error
        if attempt < max(attempts, 1) - 1:
            await asyncio.sleep(wait_s)
    return current_value, has_error


async def _reconcile_fill_results_against_live_dom(
    page: Any,
    results: list[FillFieldResult],
    *,
    target_section: str | None = None,
    heading_boundary: str | None = None,
    focus_fields: list[str] | None = None,
) -> tuple[list[FillFieldResult], list[dict[str, str]]]:
    try:
        fields = await extract_visible_form_fields(page)
    except Exception:
        return results, []

    fields = _filter_fields_for_scope(
        fields,
        target_section=target_section,
        heading_boundary=heading_boundary,
        focus_fields=focus_fields,
    )
    focus_selection = _resolve_focus_fields(fields, focus_fields)
    scoped_fields = focus_selection.fields if focus_fields else fields
    visible_by_id = {field.field_id: field for field in scoped_fields if str(field.field_id or "").strip()}
    visible_by_descriptor: dict[tuple[str, str, str], list[FormField]] = {}
    for field in scoped_fields:
        descriptor = (
            normalize_name(_preferred_field_label(field)),
            normalize_name(field.field_type),
            normalize_name(field.section or ""),
        )
        visible_by_descriptor.setdefault(descriptor, []).append(field)

    reconciled_results: list[FillFieldResult] = []
    cleared_results: list[dict[str, str]] = []
    for result in results:
        if result.success or not str(result.field_id or "").strip():
            reconciled_results.append(result)
            continue
        live_field = visible_by_id.get(result.field_id)
        settled_field: FormField | None = None
        current_value = ""
        has_error = False
        if live_field is not None:
            current_value, has_error = await _poll_live_field_state_until_settled(
                page,
                live_field,
                attempts=4 if result.required else 2,
                wait_s=0.4,
            )
            if _field_has_effective_value(live_field) and not has_error:
                settled_field = live_field
        if settled_field is None:
            descriptor = (
                normalize_name(result.name),
                normalize_name(result.control_kind),
                normalize_name(result.section or ""),
            )
            descriptor_candidates = visible_by_descriptor.get(descriptor, [])
            settled_candidates: list[tuple[FormField, str]] = []
            for candidate in descriptor_candidates:
                candidate_value, candidate_has_error = await _poll_live_field_state_until_settled(
                    page,
                    candidate,
                    attempts=4 if result.required else 2,
                    wait_s=0.4,
                )
                if _field_has_effective_value(candidate) and not candidate_has_error:
                    settled_candidates.append((candidate, candidate_value))
            if len(settled_candidates) == 1:
                settled_field, current_value = settled_candidates[0]
                has_error = False
        if settled_field is None:
            reconciled_results.append(result)
            continue
        cleared_results.append(
            {
                "field_id": result.field_id,
                "name": result.name,
                "section": result.section,
                "current_value": current_value,
                "previous_actor": result.actor,
            }
        )
        reconciled_results.append(
            result.model_copy(
                update={
                    "success": True,
                    "actor": "reconciled",
                    "error": None,
                    "value_set": current_value,
                    "fill_confidence": max(float(result.fill_confidence or 0.0), 0.9),
                    "state": "filled",
                    "failure_reason": None,
                    "takeover_suggestion": None,
                    "source": result.source or "live_dom_reconciled",
                    "answer_mode": result.answer_mode or "live_dom_reconciled",
                }
            )
        )

    return reconciled_results, cleared_results


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


def _structured_education_oracle_combobox_skip_dropdown_coercion(
    f: FormField,
    *,
    is_structured_education_candidate: bool,
    structured_education_diag: StructuredRepeaterDiagnostic | None,
) -> bool:
    """Non-native Oracle school/field comboboxes often expose only an alphabetical option slice.

    ``match_dropdown_option`` pass 5 (word overlap) can map e.g. UCLA to the first
    option sharing the token "University" (e.g. "9 Eylul University"). Skip coercion
    so triage routes to ``needs_llm`` and the executor types the real profile value.
    """
    if not is_structured_education_candidate or not structured_education_diag:
        return False
    if structured_education_diag.slot_name not in {"school", "field_of_study"}:
        return False
    if f.is_native:
        return False
    if f.field_type != "select":
        return False
    return True


def _looks_like_internal_widget_value(value: str | None) -> bool:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return False
    if text in {"▾", "▼", "▿", "⌄"}:
        return True
    return bool(_OPAQUE_WIDGET_VALUE_RE.fullmatch(text))


def _is_effectively_unset_field_value(value: str | None) -> bool:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return True
    if _EMPTY_MULTISELECT_FRAGMENT_RE.fullmatch(text):
        return True
    if is_placeholder_value(text):
        return True
    if _looks_like_internal_widget_value(text):
        return True
    if _SELECT_PLACEHOLDER_FRAGMENT_RE.search(text):
        return True
    return False


def _should_treat_llm_answer_as_na_placeholder(field: FormField, value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if not re.match(r"^(n/?a|na|none|not applicable|unknown|placeholder)$", text, re.IGNORECASE):
        return False
    choice_norms = {
        normalize_name(str(choice)) for choice in (field.options or field.choices or []) if str(choice or "").strip()
    }
    if choice_norms and normalize_name(text) in choice_norms:
        return False
    return True


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
    return ordered


def _profile_has_repeater_data(profile_data: dict[str, Any] | None, repeater_group: str) -> bool:
    data = profile_data or {}
    if repeater_group in {"experience", "education", "languages"}:
        entries = data.get(repeater_group)
        return isinstance(entries, list) and any(entry not in (None, "", {}) for entry in entries)
    if repeater_group == "skills":
        return bool(_profile_skill_values(data))
    if repeater_group == "licenses":
        for key in (
            "certifications",
            "licenses",
            "certifications_licenses",
            "licenses_certifications",
        ):
            value = data.get(key)
            if isinstance(value, list) and any(item not in (None, "", {}) for item in value):
                return True
            if isinstance(value, str) and value.strip():
                return True
        return False
    return False


def _candidate_auto_expand_sections(
    profile_data: dict[str, Any] | None,
    target_section: str | None = None,
) -> list[str]:
    requested_scope = normalize_name(target_section or "")
    group_candidates: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("experience", ("Work Experience", "Experience")),
        ("education", ("College / University", "Education")),
        ("languages", ("Language Skills", "Languages")),
        ("skills", ("Technical Skills", "Skills")),
        ("licenses", ("Licenses and Certificates", "Certifications", "Licenses")),
    )

    sections: list[str] = []
    seen: set[str] = set()
    for repeater_group, candidate_sections in group_candidates:
        if not _profile_has_repeater_data(profile_data, repeater_group):
            continue
        for section in candidate_sections:
            if requested_scope and not (
                _section_matches_scope(section, target_section) or _section_matches_scope(target_section, section)
            ):
                continue
            section_key = normalize_name(section)
            if not section_key or section_key in seen:
                continue
            seen.add(section_key)
            sections.append(section)
    return sections


# Normalized section names successfully auto-expanded in this browser session (avoid duplicate Add clicks).
_DOMHAND_AUTO_EXPAND_PROFILE_SECTIONS_ATTR = "_gh_domhand_auto_expanded_profile_sections"


_OPEN_PROFILE_INLINE_FORM_COUNT_JS = """() => {
    const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    return Array.from(document.querySelectorAll('.profile-inline-form'))
        .filter((el) => visible(el))
        .length;
}"""


async def _visible_open_profile_inline_form_count(page: Any) -> int:
    try:
        raw = await page.evaluate(_OPEN_PROFILE_INLINE_FORM_COUNT_JS)
    except Exception:
        return 0
    try:
        return max(0, int(raw or 0))
    except Exception:
        return 0


async def _maybe_auto_expand_profile_repeaters(
    *,
    browser_session: BrowserSession,
    profile_data: dict[str, Any] | None,
    target_section: str | None,
    heading_boundary: str | None,
    focus_fields: list[str] | None,
) -> bool:
    if heading_boundary or focus_fields:
        return False

    page = await browser_session.get_current_page()
    if page is not None and await _visible_open_profile_inline_form_count(page) > 0:
        logger.info(
            "domhand.auto_expand_profile_repeater.blocked_open_inline_editor",
            extra={"target_section": target_section or ""},
        )
        return False

    candidate_sections = _candidate_auto_expand_sections(profile_data, target_section=target_section)
    if not candidate_sections:
        return False

    attempted: set[str] | None = getattr(
        browser_session, _DOMHAND_AUTO_EXPAND_PROFILE_SECTIONS_ATTR, None
    )
    if attempted is None:
        attempted = set()
        setattr(browser_session, _DOMHAND_AUTO_EXPAND_PROFILE_SECTIONS_ATTR, attempted)

    from ghosthands.actions.domhand_expand import domhand_expand
    from ghosthands.actions.views import DomHandExpandParams

    for section in candidate_sections:
        section_key = normalize_name(section)
        if section_key and section_key in attempted:
            logger.debug(
                "domhand.auto_expand_profile_repeater.already_attempted",
                extra={"section": section, "target_section": target_section or ""},
            )
            continue
        result = await domhand_expand(DomHandExpandParams(section=section), browser_session)
        if getattr(result, "error", None):
            logger.debug(
                "domhand.auto_expand_profile_repeater.skipped",
                extra={"section": section, "error": result.error},
            )
            continue
        if section_key:
            attempted.add(section_key)
        logger.info(
            "domhand.auto_expand_profile_repeater",
            extra={"section": section, "target_section": target_section or ""},
        )
        return True
    return False


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


_GENERIC_PAGE_MARKERS: set[str] = {
    "job application form",
    "application",
    "apply",
    "application form",
    "job application",
    "apply for job",
    "submit application",
    "candidate application",
    "careers",
}


def _is_generic_marker(marker: str) -> bool:
    """Return True when the JS-detected page marker is too generic to differentiate SPA steps."""
    if not marker:
        return True
    normalized = marker.strip().lower()
    return normalized in _GENERIC_PAGE_MARKERS


async def _get_page_context_key(
    page: Any,
    *,
    fields: list[FormField] | None = None,
    fallback_marker: str | None = None,
) -> str:
    """Build a stable page-context key shared by fill, recovery, and assessment."""
    page_url = await _safe_page_url(page)
    snapshot = await _read_page_context_snapshot(page)
    js_marker = str(snapshot.get("page_marker") or "").strip()
    fb_marker = str(fallback_marker or "").strip()

    if js_marker and not _is_generic_marker(js_marker):
        marker = js_marker
    elif fb_marker:
        marker = fb_marker
    elif js_marker:
        marker = js_marker
    else:
        marker = _first_meaningful_section(fields)

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
			// Oracle HCM repeater container detection — check before generic walk
			var oracleContainerSels = [
				'.profile-inline-form',
				'.apply-flow-profile-item-section',
				'.apply-flow-block',
			];
			for (var oi = 0; oi < oracleContainerSels.length; oi++) {{
				var oc = el.closest ? el.closest(oracleContainerSels[oi]) : null;
				if (oc) {{
					var oTitle = oc.querySelector(
						'.apply-flow-profile-item-section__title, .apply-flow-block__title, ' +
						':scope > h3, :scope > h4, :scope > h2'
					);
					if (oTitle) {{
						var oText = oTitle.textContent.trim();
						if (oText && oText.length <= 80) return oText;
					}}
					var oPrev = oc.previousElementSibling;
					while (oPrev) {{
						if (oPrev.matches && oPrev.matches('h2, h3, h4')) {{
							var opText = oPrev.textContent.trim();
							if (opText && opText.length <= 80) return opText;
						}}
						oPrev = oPrev.previousElementSibling;
					}}
				}}
			}}

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
	var buttonSelector =
		'button, [role="button"], [role="radio"], [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"], [data-automation-id*="radio"], [data-automation-id*="Radio"]';
	var groups = {};

	var navLabels = new Set("""
    + json.dumps(list(_NAV_BUTTON_LABELS))
    + r""");

	var normalize = function(text) {
		return (text || '').replace(/\s+/g, ' ').trim();
	};

	var cleanQuestionText = function(node) {
		if (!node) return '';
		var clone = node.cloneNode(true);
		clone.querySelectorAll(
			buttonSelector + ', input, textarea, select, ul, ol, li, [role="radio"], [role="button"], [role="checkbox"], [role="switch"], [class*="hint"], [class*="desc"], [class*="sub"], small'
		).forEach(function(x) { x.remove(); });
		return normalize(clone.textContent || '');
	};

	var isGenericQuestionText = function(text) {
		return /^(button group choice|please provide a response|select one|choose one)$/i.test(normalize(text));
	};

	var getButtonGroupContainer = function(btn) {
		return ff.closestCrossRoot(
			btn,
			'ul.cx-select-pills-container, [role="radiogroup"], fieldset, [role="group"], .radio-group, .checkbox-group, [data-automation-id="formField"], [data-automation-id*="formField"], .input-row__control-container'
		);
	};

	var getOwningControlContainer = function(groupContainer) {
		return ff.closestCrossRoot(
			groupContainer,
			'.input-row__control-container, [role="radiogroup"], fieldset, [role="group"], [data-automation-id="formField"], [data-automation-id*="formField"], .radio-group, .checkbox-group, .form-group, .field'
		) || groupContainer;
	};

	var getOwningRow = function(controlContainer) {
		return ff.closestCrossRoot(
			controlContainer,
			'.input-row, [data-automation-id="formField"], [data-automation-id*="formField"], fieldset, .form-group, .field, form-builder, .apply-flow-block__form-list'
		) || controlContainer.parentElement || controlContainer;
	};

	var isOptionOnlyText = function(text, optionNorms) {
		var norm = normalize(text).toLowerCase();
		if (!norm) return true;
		return optionNorms.has(norm);
	};

	var pushCandidate = function(candidates, text, optionNorms) {
		var clean = normalize(text);
		if (!clean || clean.length > 2000) return;
		if (isOptionOnlyText(clean, optionNorms)) return;
		candidates.push(clean);
	};

	var pushBestPrecedingSiblingText = function(candidates, startNode, stopNode, optionNorms) {
		var cursor = startNode;
		while (cursor && cursor !== stopNode) {
			var sibling = cursor.previousElementSibling;
			while (sibling) {
				pushCandidate(candidates, cleanQuestionText(sibling), optionNorms);
				if (candidates.length && !isGenericQuestionText(candidates[candidates.length - 1])) {
					return candidates[candidates.length - 1];
				}
				sibling = sibling.previousElementSibling;
			}
			cursor = cursor.parentElement;
		}
		return '';
	};

	var getQuestionLabel = function(groupContainer, controlContainer, row, optionTexts) {
		var optionNorms = new Set(
			(optionTexts || []).map(function(text) { return normalize(text).toLowerCase(); }).filter(Boolean)
		);
		var candidates = [];

		var innerLabel = pushBestPrecedingSiblingText(candidates, groupContainer, controlContainer, optionNorms);
		if (innerLabel && !isGenericQuestionText(innerLabel)) {
			return innerLabel;
		}

		var prevSibling = controlContainer ? controlContainer.previousElementSibling : null;
		while (prevSibling) {
			pushCandidate(candidates, cleanQuestionText(prevSibling), optionNorms);
			if (candidates.length && !isGenericQuestionText(candidates[candidates.length - 1])) {
				return candidates[candidates.length - 1];
			}
			prevSibling = prevSibling.previousElementSibling;
		}

		if (row && row !== controlContainer) {
			var child = row.firstElementChild;
			while (child && child !== controlContainer) {
				pushCandidate(candidates, cleanQuestionText(child), optionNorms);
				if (candidates.length && !isGenericQuestionText(candidates[candidates.length - 1])) {
					return candidates[candidates.length - 1];
				}
				child = child.nextElementSibling;
			}

			var rowLabel = row.querySelector(
				':scope > legend, :scope > label, :scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > [class*="question"], :scope > [data-automation-id="fieldLabel"], :scope > [data-automation-id*="fieldLabel"]'
			);
			pushCandidate(candidates, cleanQuestionText(rowLabel), optionNorms);
			if (candidates.length && !isGenericQuestionText(candidates[candidates.length - 1])) {
				return candidates[candidates.length - 1];
			}
		}

		var ancestor = row || controlContainer;
		for (var depth = 0; depth < 3 && ancestor; depth++) {
			var sibling = ancestor.previousElementSibling;
			while (sibling) {
				pushCandidate(candidates, cleanQuestionText(sibling), optionNorms);
				if (candidates.length && !isGenericQuestionText(candidates[candidates.length - 1])) {
					return candidates[candidates.length - 1];
				}
				sibling = sibling.previousElementSibling;
			}
			ancestor = ancestor.parentElement;
		}

		for (var i = 0; i < candidates.length; i++) {
			if (!isGenericQuestionText(candidates[i])) return candidates[i];
		}
		return candidates[0] || '';
	};

	var getCurrentValue = function(buttonEntries) {
		for (var i = 0; i < buttonEntries.length; i++) {
			var btn = buttonEntries[i].node;
			if (!btn) continue;
			var ownClass = String(btn.className || '').toLowerCase();
			var parentClass = btn.parentElement ? String(btn.parentElement.className || '').toLowerCase() : '';
			var pressed = btn.getAttribute('aria-pressed') === 'true' || btn.getAttribute('aria-checked') === 'true';
			var selected = /\b(selected|active|checked|chosen)\b/.test(ownClass) || /\b(selected|active|checked|chosen)\b/.test(parentClass);
			if (pressed || selected) return buttonEntries[i].text;
		}
		return '';
	};

	var allBtnEls = document.querySelectorAll(buttonSelector);
	for (var i = 0; i < allBtnEls.length; i++) {
		var btn = allBtnEls[i];
		if (!ff.isVisible(btn)) continue;
		if (btn.disabled) continue;
		if (btn.closest('nav, header, [role="navigation"], [role="menubar"], [role="menu"], [role="toolbar"]')) continue;
		if (btn.tagName === 'A' || btn.closest('a[href]')) continue;
		if (btn.getAttribute('role') === 'combobox') continue;
		if (btn.getAttribute('aria-haspopup') === 'listbox') continue;
		if (btn.tagName.toLowerCase() === 'input') continue;
		if (btn.closest('[data-automation-id="selectedItemList"], [data-automation-id="selectedItems"], [data-automation-id="multiSelectContainer"], [data-uxi-widget-type="multiselect"]')) continue;

		var btnText = normalize(btn.textContent || '');
		if (!btnText || btnText.length > 160) continue;
		if (navLabels.has(btnText.toLowerCase()) || btnText.toLowerCase().startsWith('add ') || btnText.toLowerCase().includes('save & continue')) continue;

		var groupContainer = getButtonGroupContainer(btn);
		if (!groupContainer) continue;
		var controlContainer = getOwningControlContainer(groupContainer);
		if (!controlContainer) continue;
		var groupKey = ff.tag(controlContainer);
		if (!groups[groupKey]) {
			groups[groupKey] = {
				controlContainer: controlContainer,
				buttons: []
			};
		}
		var btnId = ff.tag(btn);
		var already = false;
		for (var j = 0; j < groups[groupKey].buttons.length; j++) {
			if (groups[groupKey].buttons[j].ffId === btnId) {
				already = true;
				break;
			}
		}
		if (!already) groups[groupKey].buttons.push({ text: btnText, ffId: btnId, node: btn });
	}

	for (var groupKey in groups) {
		var group = groups[groupKey];
		if (group.buttons.length < 2 || group.buttons.length > 10) continue;
		if (group.buttons.some(function(entry) { return entry.text.length > 200; })) continue;

			var controlContainer = group.controlContainer;
			var row = getOwningRow(controlContainer);
			var choiceTexts = group.buttons.map(function(entry) { return entry.text; });
			var firstButtonNode = group.buttons.length ? group.buttons[0].node : null;
			var groupContainer = firstButtonNode ? getButtonGroupContainer(firstButtonNode) : null;
			var questionLabel = getQuestionLabel(groupContainer, controlContainer, row, choiceTexts) || 'Button group choice';
			var normalizedLabel = normalize(questionLabel.replace(/\*\s*$/, ''));
			var currentValue = getCurrentValue(group.buttons);

			var detectRequired = function(label, gc, cc, rw) {
				if (/\*/.test(label)) return true;
				var containers = [gc, cc, rw].filter(Boolean);
				for (var ci = 0; ci < containers.length; ci++) {
					var c = containers[ci];
					if (c.getAttribute && c.getAttribute('aria-required') === 'true') return true;
					if (c.getAttribute && /\brequired\b/i.test(c.getAttribute('class') || '')) return true;
					var reqIcon = c.querySelector && c.querySelector('[class*="required-icon"], [class*="required_icon"], [class*="requiredIcon"], .oj-required-inline-icon, abbr[title="required"], span.required');
					if (reqIcon) return true;
					var labels = c.querySelectorAll ? c.querySelectorAll('label, legend, [class*="label"], [class*="question"]') : [];
					for (var li = 0; li < labels.length; li++) {
						if (/\*/.test(labels[li].textContent || '')) return true;
					}
				}
				return false;
			};

		results.push({
			field_id: groupKey,
			name: normalizedLabel || 'Button group choice',
			field_type: 'button-group',
			section: ff.getSection(controlContainer),
			name_attr: normalizedLabel || controlContainer.getAttribute('name') || '',
			required: detectRequired(questionLabel, groupContainer, controlContainer, row),
			options: [],
			choices: choiceTexts,
			accept: null,
			is_native: false,
			is_multi_select: false,
			visible: true,
			raw_label: questionLabel,
			synthetic_label: false,
			field_fingerprint: null,
			current_value: currentValue,
			btn_ids: group.buttons.map(function(b) { return b.ffId; }),
			questionLabel: questionLabel,
			groupKey: normalizedLabel || questionLabel
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


def _last_assess_state_is_cleanly_advanceable(last_state: dict[str, Any] | None) -> bool:
    if not isinstance(last_state, dict):
        return False
    if not bool(last_state.get("advance_allowed")):
        return False
    clean_counts = (
        "unresolved_required_count",
        "optional_validation_count",
        "visible_error_count",
        "mismatched_count",
        "opaque_count",
        "unverified_count",
    )
    return all(int(last_state.get(key) or 0) == 0 for key in clean_counts)


def _last_assess_state_has_no_hard_blockers(last_state: dict[str, Any] | None) -> bool:
    if not isinstance(last_state, dict):
        return False
    if not bool(last_state.get("advance_allowed")):
        return False
    hard_blocker_counts = (
        "unresolved_required_count",
        "optional_validation_count",
        "visible_error_count",
        "opaque_count",
    )
    return all(int(last_state.get(key) or 0) == 0 for key in hard_blocker_counts)


def _same_page_advance_guard_error(
    browser_session: BrowserSession,
    *,
    page_context_key: str,
    page_url: str,
    current_field_ids: set[str] | None = None,
) -> str | None:
    """Block same-page DomHand refill after assess_state already marked the page advanceable.

    SPA-aware: also compares current field IDs against the last fill's fields.
    If < 30% overlap, the page changed even if URL/context key didn't.
    """
    last_state = getattr(browser_session, "_gh_last_application_state", None)
    if not isinstance(last_state, dict):
        return None
    if str(last_state.get("page_context_key") or "") != page_context_key:
        return None
    if str(last_state.get("page_url") or "") != page_url:
        return None
    if not _last_assess_state_has_no_hard_blockers(last_state):
        return None
    # SPA guard: if current fields are mostly different from last fill, it's a new page
    if current_field_ids:
        last_fill = getattr(browser_session, "_gh_last_domhand_fill", None)
        if isinstance(last_fill, dict):
            last_field_ids = set(last_fill.get("field_ids") or [])
            if last_field_ids:
                overlap = current_field_ids & last_field_ids
                total = max(len(current_field_ids), len(last_field_ids))
                if total > 0 and len(overlap) / total < 0.3:
                    return None  # Different fields — SPA page transition, allow fill
    return "DomHand: page already assessed as advance_allowed=yes; broad fill already completed."


def _same_page_fill_guard_error(
    browser_session: BrowserSession,
    *,
    page_context_key: str,
    page_url: str,
    heading_boundary: str | None,
    focus_fields: list[str] | None,
    entry_data: dict[str, Any] | None,
    current_field_ids: set[str] | None = None,
) -> str | None:
    """Block repeated broad same-page domhand_fill. One fill pass per page.

    SPA-aware: also compares current field IDs against the last fill's fields.
    If < 30% overlap, the page changed even if the URL didn't (common in SPAs
    like Goldman Sachs, Greenhouse, etc.).
    """
    if heading_boundary or focus_fields or entry_data:
        return None
    last_fill = getattr(browser_session, "_gh_last_domhand_fill", None)
    if not isinstance(last_fill, dict):
        return None
    if str(last_fill.get("page_context_key") or "") != page_context_key:
        return None
    if str(last_fill.get("page_url") or "") != page_url:
        return None
    if not bool(last_fill.get("broad_fill_completed")):
        return None
    # SPA guard: if current fields are mostly different from last fill, it's a new page
    if current_field_ids:
        last_field_ids = set(last_fill.get("field_ids") or [])
        if last_field_ids:
            overlap = current_field_ids & last_field_ids
            total = max(len(current_field_ids), len(last_field_ids))
            if total > 0 and len(overlap) / total < 0.3:
                return None  # Different fields — SPA page transition, allow fill
    return "DomHand: broad fill already completed on this page."


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
    _field_already_matches,
    _is_explicit_false,
    _record_expected_value_if_settled,
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
    _resolve_llm_answer_for_field,
    _resolve_llm_answer_via_batch_key,
    _takeover_suggestion_for_field,
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
    # Education/experience location hint: tell the LLM this is the entity location
    _label_lc = normalize_name(display_name)
    _section_lc = normalize_name(field.section or "")
    if (
        _label_lc in {"country", "state", "city", "province", "region"}
        and any(tok in _section_lc for tok in ("education", "college", "university", "experience", "work", "employment"))
    ):
        desc += " [CRITICAL: This field is the SCHOOL/EMPLOYER location — NOT the applicant's home address. Look at the School or Company field in THIS SAME section and return THAT institution's city/state/country. NEVER use the applicant's residential address here.]"
    return desc


def _is_latest_employer_search_field(field: FormField) -> bool:
    label = normalize_name(_preferred_field_label(field) or field.name or "")
    if field.field_type != "select":
        return False
    return any(
        token in label
        for token in (
            "latest employer",
            "current employer",
            "most recent employer",
            "name of latest employer",
            "name of current employer",
            "name of most recent employer",
        )
    )


def _is_searchable_combobox_on_oracle(field: FormField, *, page_host: str) -> bool:
    """True on Oracle Fusion FA hosts for non-native ``select`` (HCM type-to-search combobox)."""
    host = (page_host or "").strip().lower()
    if not host:
        return False
    if field.field_type != "select" or field.is_native:
        return False
    return ".fa." in host and "oraclecloud.com" in host


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

# Neutral / decline patterns for EEO button-group choices. Ordered by
# preference — first match wins.  Used when the hardcoded EEO default text
# doesn't exactly match any choice on the page.
_NEUTRAL_EEO_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:i\s+)?prefer\s+not\s+(?:to\s+)?(?:say|answer|disclose|respond)", re.IGNORECASE),
    re.compile(r"(?:i\s+)?decline\s+(?:to\s+)?(?:self[- ]?identify|answer|disclose)", re.IGNORECASE),
    re.compile(r"(?:i\s+)?(?:do\s+not|don'?t)\s+wish\s+to\s+answer", re.IGNORECASE),
    re.compile(r"(?:i\s+)?choose\s+not\s+to\s+(?:disclose|answer|respond)", re.IGNORECASE),
    re.compile(r"^prefer\s+not\s+to\s+say$", re.IGNORECASE),
    re.compile(r"^not\s+(?:applicable|specified)$", re.IGNORECASE),
]


def _find_neutral_eeo_choice(field: FormField) -> str | None:
    """Find the most neutral/decline choice from a constrained-choice EEO field."""
    choices = [str(c).strip() for c in (field.options or field.choices or []) if str(c).strip()]
    if not choices:
        return None
    for pattern in _NEUTRAL_EEO_PATTERNS:
        for choice in choices:
            if pattern.search(choice):
                return choice
    return None


# EEO / demographic fields — "decline to self-identify" defaults when profile
# data is empty.  Prevents required EEO fields from triggering HITL.
_EEO_DECLINE_DEFAULTS: dict[str, str] = {
    "gender": "I decline to self-identify",
    "gender identity": "I decline to self-identify",
    "transgender": "I prefer not to say",
    "race": "I decline to self-identify",
    "race ethnicity": "I decline to self-identify",
    "ethnicity": "I decline to self-identify",
    "pronouns": "Prefer not to say",
    "veteran status": "I am not a protected veteran",
    "veteran": "I am not a protected veteran",
    "disability": "I do not wish to answer",
    "disability status": "I do not wish to answer",
    "sexual orientation": "I decline to self-identify",
    "lgbtq": "I decline to self-identify",
    "hispanic": "Prefer not to say",
    "latino": "Prefer not to say",
    "hispanic or latino": "Prefer not to say",
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

    # ── EEO "decline" defaults — for required fields or constrained-choice
    # controls (button-group, radio-group) where we can safely pick a
    # "decline" option rather than leaving the field empty and forcing
    # the agent to waste steps filling them one at a time.
    _has_constrained_choices = bool(field.choices or field.options) and field.field_type in {
        "button-group", "radio-group", "radio", "checkbox-group",
    }
    if field.required or _has_constrained_choices:
        for norm_name in candidate_norms:
            if norm_name in _EEO_DECLINE_DEFAULTS:
                eeo_default = _EEO_DECLINE_DEFAULTS[norm_name]
                if _has_constrained_choices:
                    eeo_default = _coerce_answer_to_field(field, eeo_default)
                    if not eeo_default:
                        eeo_default = _find_neutral_eeo_choice(field)
                    if not eeo_default:
                        continue
                return eeo_default
            for eeo_key, eeo_value in sorted(
                _EEO_DECLINE_DEFAULTS.items(),
                key=lambda item: len(item[0]),
                reverse=True,
            ):
                if eeo_key and eeo_key in norm_name:
                    if _has_constrained_choices:
                        eeo_value = _coerce_answer_to_field(field, eeo_value)
                        if not eeo_value:
                            eeo_value = _find_neutral_eeo_choice(field)
                        if not eeo_value:
                            continue
                    return eeo_value

    return None


def _is_skill_like(field_name: str) -> bool:
    n = normalize_name(field_name)
    if n == "skill type":
        return False
    return bool(re.search(r"\bskills?\b", n) or re.search(r"\btechnolog(y|ies)\b", n))


def _oracle_skill_dependency_rank(field: FormField) -> int:
    name = normalize_name(_preferred_field_label(field))
    if name == "skill type":
        return 0
    if name in {"skill", "skill name"}:
        return 1
    return 2


def _prioritize_fillable_fields(fields: list[FormField], *, page_host: str) -> list[FormField]:
    """Keep Workday skills at the front of the first-pass conquer order.

    The user wants Workday ``Skills`` handled before the rest of the page so the
    searchable multiselect is committed while context is still clean.
    """
    if any(normalize_name(_preferred_field_label(field)) == "skill type" for field in fields):
        return sorted(
            fields,
            key=lambda field: (
                _oracle_skill_dependency_rank(field),
                0 if field.required else 1,
            ),
        )
    if "myworkdayjobs.com" not in (page_host or "").lower():
        return fields
    return sorted(
        fields,
        key=lambda field: (
            0 if _is_skill_like(_preferred_field_label(field)) else 1,
            0 if field.required else 1,
        ),
    )


def _is_navigation_field(field: FormField) -> bool:
    if field.field_type != "button-group":
        return False
    choices_lower = [c.lower() for c in (field.choices or [])]
    nav_keywords = {"next", "continue", "back", "previous", "save", "cancel", "submit"}
    return any(c in nav_keywords for c in choices_lower)


def _is_upload_like_field(field: FormField) -> bool:
    """True for file-upload controls that must be handled via domhand_upload, not clicks."""
    if field.field_type == "file":
        return True
    if getattr(field, "accept", None):
        return True
    if field.field_type != "button-group":
        return False

    texts = [
        _preferred_field_label(field),
        field.name or "",
        field.raw_label or "",
        field.section or "",
        *(field.choices or []),
        *(field.options or []),
    ]
    normalized = [normalize_name(str(text)) for text in texts if str(text).strip()]
    joined = " | ".join(normalized)

    upload_keywords = (
        "cover letter",
        "resume",
        "curriculum vitae",
        "cv",
        "upload",
        "attach",
        "attachment",
        "browse",
        "choose file",
        "select file",
        "drop file",
        "drag and drop",
        "accepted file types",
    )
    if any(keyword in joined for keyword in upload_keywords):
        return True

    choice_set = {choice for choice in normalized if choice}
    if {"attach", "enter manually"} <= choice_set:
        return True
    if {"upload", "enter manually"} <= choice_set:
        return True
    return False


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


def _should_enrich_select_options_for_llm(field: FormField) -> bool:
    """Return True for custom single-select fields that still lack visible options.

    Keep this intentionally narrow to avoid perturbing existing fill behavior:
    only non-native, non-multi-select dropdowns with no extracted options/choices
    are enriched before the LLM answer pass.
    """
    return bool(
        field.field_id
        and field.field_type == "select"
        and not field.is_native
        and not field.is_multi_select
        and not field.options
        and not field.choices
    )


def _dedupe_option_texts(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


async def _enrich_missing_select_options_for_llm(
    page: Any,
    fields: list[FormField],
    *,
    page_host: str = "",
    combobox_search_hints: dict[str, str] | None = None,
) -> None:
    """Populate missing options for custom dropdowns before batch LLM answer generation.

    This is a read-only enrichment pass: open the combobox, scan visible options,
    then dismiss. If discovery fails, leave the field untouched.

    For Oracle FA school/field-of-study rows, ``combobox_search_hints`` triggers
    type-to-search so the LLM sees filtered options instead of the alphabetical slice.
    """
    enriched_labels: list[str] = []
    hints = combobox_search_hints or {}
    for field in fields:
        hint = hints.get(field.field_id)
        if hint and _is_searchable_combobox_on_oracle(field, page_host=page_host):
            field.options = []
            field.choices = []
            discovered = await _enrich_combobox_via_search(page, field, hint)
            if discovered:
                field.options = discovered
                enriched_labels.append(_preferred_field_label(field))
            continue
        if not _should_enrich_select_options_for_llm(field):
            continue

        tag = f"pre-llm option enrichment [{field.field_id}]"
        discovered: list[str] = []
        last_valid_scan: list[str] = []
        best_valid_scan: list[str] = []
        stable_matches = 0
        try:
            await _try_open_combobox_menu(page, field.field_id, tag=tag)
            for _ in range(PRE_LLM_OPTION_ENRICHMENT_POLLS):
                current_scan: list[str] = []
                with contextlib.suppress(Exception):
                    current_scan = _dedupe_option_texts(
                        await _scan_visible_dropdown_options(page, field_id=field.field_id)
                    )
                if current_scan and not _select_extractions_look_like_pre_open_noise(field, current_scan):
                    if len(current_scan) > len(best_valid_scan):
                        best_valid_scan = list(current_scan)
                    if current_scan == last_valid_scan:
                        stable_matches += 1
                    else:
                        last_valid_scan = list(current_scan)
                        stable_matches = 1
                    if stable_matches >= PRE_LLM_OPTION_ENRICHMENT_SETTLE_MATCHES:
                        discovered = list(current_scan)
                        break
                else:
                    last_valid_scan = []
                    stable_matches = 0
                await asyncio.sleep(PRE_LLM_OPTION_ENRICHMENT_POLL_SECONDS)
            else:
                discovered = list(best_valid_scan)
        except Exception:
            discovered = []
        finally:
            with contextlib.suppress(Exception):
                await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
            await asyncio.sleep(0.08)

        if not discovered:
            continue
        field.options = discovered
        enriched_labels.append(_preferred_field_label(field))

    if enriched_labels:
        logger.info(
            "domhand.pre_llm_option_enrichment",
            count=len(enriched_labels),
            labels=enriched_labels[:10],
        )


async def _enrich_combobox_via_search(page: Any, field: FormField, search_hint: str) -> list[str]:
    """Type ``search_hint`` into an Oracle-style combobox, read filtered options, then clear the input.

    Used by CI fixtures and flows where the default open→scan slice is alphabetical and wrong.
    """
    ff_id = field.field_id
    tag = f"combobox type-search enrich [{ff_id}]"
    discovered: list[str] = []
    try:
        with contextlib.suppress(Exception):
            await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
        await asyncio.sleep(0.06)
        await _try_open_combobox_menu(page, ff_id, tag=tag)
        await asyncio.sleep(0.1)
        await _clear_dropdown_search(page, ff_id)
        await asyncio.sleep(0.08)
        await _type_text_compat(page, search_hint, delay=35)
        await asyncio.sleep(0.5)
        last_valid: list[str] = []
        stable = 0
        best_batch: list[str] = []
        for _ in range(30):
            raw = await _scan_visible_dropdown_options(page, field_id=ff_id)
            batch = _dedupe_option_texts(raw)
            if len(batch) > len(best_batch):
                best_batch = list(batch)
            if batch and not _select_extractions_look_like_pre_open_noise(field, batch):
                if batch == last_valid:
                    stable += 1
                else:
                    last_valid = list(batch)
                    stable = 1
                if stable >= 2 or (len(batch) >= 3 and stable >= 1):
                    discovered = list(batch)
                    break
            else:
                last_valid = []
                stable = 0
            await asyncio.sleep(0.08)
        if not discovered:
            discovered = list(last_valid) if last_valid else list(best_batch)
        if not discovered:
            try:
                await _try_open_combobox_menu(page, ff_id, tag=f"{tag}-list-fallback")
                await asyncio.sleep(0.2)
                raw_fb = await page.evaluate(
                    r"""(fid) => {
					const lb = document.getElementById(fid + '-list');
					if (!lb) return [];
					if (!lb.classList.contains('open')) return [];
					return Array.from(lb.querySelectorAll('.option'))
						.map((n) => (n.textContent || '').replace(/\s+/g, ' ').trim())
						.filter(Boolean);
				}""",
                    ff_id,
                )
                if isinstance(raw_fb, list) and raw_fb:
                    discovered = _dedupe_option_texts([str(x) for x in raw_fb])
            except Exception:
                pass
    except Exception:
        discovered = []
    finally:
        with contextlib.suppress(Exception):
            await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
        await asyncio.sleep(0.06)
        for _ in range(2):
            with contextlib.suppress(Exception):
                await _clear_dropdown_search(page, ff_id)
        with contextlib.suppress(Exception):
            await page.evaluate(
                r"""(ffId) => {
				var el = document.getElementById(ffId);
				if (!el && window.__ff && window.__ff.byId) el = window.__ff.byId(ffId);
				if (el && 'value' in el) {
					el.value = '';
					el.dispatchEvent(new Event('input', { bubbles: true }));
					el.dispatchEvent(new Event('change', { bubbles: true }));
				}
			}""",
                ff_id,
            )
        await asyncio.sleep(0.05)
    return discovered


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
        from ghosthands.cost_summary import mark_stagehand_usage
        from ghosthands.stagehand.compat import ensure_stagehand_for_session

        layer = await ensure_stagehand_for_session(browser_session)
        if not layer.is_available:
            return

        mark_stagehand_usage(browser_session, source="stagehand_cross_reference")
        elements = await layer.observe("Find all unfilled or empty form fields on this page")
        if not elements:
            return

        filled_labels = {r.name.lower() for r in all_results if r.success}
        missed = [el for el in elements if el.description and el.description.lower() not in filled_labels]
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
    logger.info(
        "domhand.fill.ENTER",
        extra={
            "page_context_key": page_context_key,
            "page_url": page_url[:120],
            "target_section": params.target_section or "",
            "heading_boundary": params.heading_boundary or "",
            "focus_fields": list(params.focus_fields or [])[:5],
            "is_scoped": bool(params.heading_boundary or params.entry_data or params.focus_fields),
        },
    )
    # Scoped fills (heading_boundary, entry_data, focus_fields) bypass the
    # advance guard — repeater sections are expanded AFTER the initial
    # assessment and need a fresh fill pass.
    is_scoped_fill = bool(params.heading_boundary or params.entry_data or params.focus_fields)

    completed_scoped: dict[tuple[str, str], dict] = getattr(browser_session, "_gh_completed_scoped_fills", {})
    completed_scoped_page: str = getattr(browser_session, "_gh_completed_scoped_page", "")
    if completed_scoped_page and completed_scoped_page != page_context_key:
        completed_scoped = {}
    browser_session._gh_completed_scoped_fills = completed_scoped
    browser_session._gh_completed_scoped_page = page_context_key

    if is_scoped_fill and params.heading_boundary and not params.focus_fields:
        _scope_key = (page_context_key, (params.heading_boundary or "").strip().lower())
        prev = completed_scoped.get(_scope_key)
        if prev:
            _scoped_msg = (
                f"DomHand: section {params.heading_boundary!r} already filled "
                f"({prev.get('filled_count', 0)} fields) on this page."
            )
            return ActionResult(
                extracted_content=_scoped_msg,
                long_term_memory=_scoped_msg,
                include_extracted_content_only_once=True,
                metadata={
                    "tool": "domhand_fill",
                    "scoped_dedup_guard": True,
                    "page_context_key": page_context_key,
                    "heading_boundary": params.heading_boundary,
                },
            )

    # Quick field ID snapshot for SPA-aware guard (detects page transitions
    # when URL stays the same but form content changes, e.g. Goldman Sachs).
    # Must run BEFORE both guards so they can use it for SPA detection.
    # Inject __ff first so extract_visible_form_fields works on SPA page 2.
    _guard_field_ids: set[str] | None = None
    try:
        with contextlib.suppress(Exception):
            await page.evaluate(_build_inject_helpers_js())
        _guard_snapshot = await _visible_field_id_snapshot(page)
        _guard_field_ids = set(_guard_snapshot) if _guard_snapshot else None
    except Exception:
        pass
    same_page_advance_guard = (
        None
        if is_scoped_fill
        else _same_page_advance_guard_error(
            browser_session,
            page_context_key=page_context_key,
            page_url=page_url,
            current_field_ids=_guard_field_ids,
        )
    )
    if same_page_advance_guard:
        logger.info(
            "domhand.fill.advance_guard_blocked",
            extra={
                "page_context_key": page_context_key,
                "page_url": page_url,
                "guard_field_count": len(_guard_field_ids) if _guard_field_ids else 0,
                "last_fill_field_count": len((getattr(browser_session, "_gh_last_domhand_fill", {}) or {}).get("field_ids", [])),
                "message": same_page_advance_guard,
            },
        )
        return ActionResult(
            extracted_content=same_page_advance_guard,
            long_term_memory=same_page_advance_guard,
            include_extracted_content_only_once=True,
            metadata={
                "tool": "domhand_fill",
                "same_page_advance_guard": True,
                "recommended_next_action": "review_page_visually",
                "page_context_key": page_context_key,
                "page_url": page_url,
            },
        )
    same_page_guard = _same_page_fill_guard_error(
        browser_session,
        page_context_key=page_context_key,
        page_url=page_url,
        heading_boundary=params.heading_boundary,
        focus_fields=params.focus_fields,
        entry_data=entry_data,
        current_field_ids=_guard_field_ids,
    )
    if same_page_guard:
        logger.info(
            "domhand.fill.same_page_guard_blocked",
            extra={
                "page_context_key": page_context_key,
                "page_url": page_url,
                "guard_field_count": len(_guard_field_ids) if _guard_field_ids else 0,
                "last_fill_field_count": len((getattr(browser_session, "_gh_last_domhand_fill", {}) or {}).get("field_ids", [])),
                "guard_message": same_page_guard,
            },
        )
        return ActionResult(
            extracted_content=same_page_guard,
            long_term_memory=same_page_guard,
            include_extracted_content_only_once=True,
            metadata={
                "tool": "domhand_fill",
                "same_page_fill_guard": True,
                "page_context_key": page_context_key,
                "page_url": page_url,
            },
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
    total_already_filled_count = 0
    _cached_school_location: dict[str, str] = {}  # Persists across rounds for State deferred to round 3

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
            allow_all_visible_fallback=not params.strict_scope,
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
            focus_fields=params.focus_fields,
        )
        if blockers_unchanged:
            if fields:
                chosen_next_strategy = "continue_domhand_fill"
                _log_loop_blocker_state(
                    browser_session,
                    fields=fields,
                    chosen_next_strategy=chosen_next_strategy,
                )
                logger.info(
                    "domhand.fill.stale_blocker_set_observed",
                    extra={
                        "page_context_key": page_context_key,
                        "field_labels": [_preferred_field_label(field) for field in fields],
                    },
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
                    "No visible fields matched the requested focus_fields on the current page context. "
                    "Use the live visible label or field_id for the retry."
                ),
            )

        fillable_fields: list[FormField] = []
        for f in fields:
            if f.field_type == "file":
                continue
            if _is_upload_like_field(f):
                continue
            key = get_stable_field_key(f)
            if key in fields_skipped:
                continue
            if key in fields_capped:
                continue
            if settled_fields.get(key, 0.0) >= 0.8:
                continue  # Already filled with high confidence — anti-loop gate
            has_effective_value = _field_has_effective_value(f)
            has_validation_error = False
            if has_effective_value:
                has_validation_error = await _field_has_validation_error(page, f.field_id)
            if key in fields_seen and has_effective_value and not has_validation_error:
                continue
            if _is_navigation_field(f):
                continue
            fillable_fields.append(f)
        fillable_fields = _prioritize_fillable_fields(fillable_fields, page_host=page_host)
        logger.info(
            "domhand.fill.field_triage",
            extra={
                "round": round_num,
                "extracted_total": len(fields),
                "fillable_count": len(fillable_fields),
                "fillable_types": dict(
                    __import__("collections").Counter(f.field_type for f in fillable_fields)
                ) if fillable_fields else {},
                "page_context_key": page_context_key,
            },
        )

        if not fillable_fields:
            open_inline_forms = await _visible_open_profile_inline_form_count(page)
            if open_inline_forms > 0 and round_num == 1 and not all_results:
                return ActionResult(
                    error=(
                        "A profile inline editor is already open. Finish that SAME entry first: "
                        "fill any required date fields such as Start Date Month/Year, click the visible "
                        "commit button, and wait for the saved tile before opening another section."
                    ),
                    include_extracted_content_only_once=True,
                    metadata={
                        "step_cost": total_step_cost,
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                        "model": model_name,
                        "domhand_llm_calls": llm_calls,
                        "open_inline_form_count": open_inline_forms,
                    },
                )
            if round_num == 1 and not params.strict_scope:
                # strict_scope means domhand_fill_repeaters handles its own expand
                auto_expanded = await _maybe_auto_expand_profile_repeaters(
                    browser_session=browser_session,
                    profile_data=profile_data,
                    target_section=params.target_section,
                    heading_boundary=params.heading_boundary,
                    focus_fields=params.focus_fields,
                )
                if auto_expanded:
                    continue
            if round_num == 1:
                _empty_fields_msg = "No fillable form fields found on the page."
                return ActionResult(
                    extracted_content=_empty_fields_msg,
                    long_term_memory=_empty_fields_msg,
                    include_extracted_content_only_once=True,
                    metadata={
                        "tool": "domhand_fill",
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
        oracle_combobox_search_hints: dict[str, str] = {}
        direct_fills: dict[str, str] = {}
        resolved_values: dict[str, ResolvedFieldValue] = {}
        resolved_bindings: dict[str, ResolvedFieldBinding] = {}
        fillable_field_map = {field.field_id: field for field in fillable_fields}
        # When focus_fields is set, the agent explicitly targets these fields
        # (usually because of a validation error despite the field appearing filled).
        # Do NOT skip them — the value may be in the DOM but not registered by the
        # ATS framework (e.g. Workday React state).
        _force_refill = bool(params.focus_fields)
        already_filled_count = 0
        for f in fillable_fields:
            has_effective_value = _field_has_effective_value(f)
            has_validation_error = False
            if has_effective_value:
                has_validation_error = await _field_has_validation_error(page, f.field_id)
            if has_effective_value and not has_validation_error and not _force_refill:
                already_filled_count += 1
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
            if _is_skill_like(_preferred_field_label(f)):
                # When called from repeater with entry_data, use the single skill
                single_skill = (
                    (entry_data.get("skill_name") or entry_data.get("skill") or "").strip()
                    if isinstance(entry_data, dict) else ""
                )
                if single_skill:
                    direct_fills[f.field_id] = single_skill
                    resolved_values[f.field_id] = _resolved_field_value(
                        single_skill,
                        source="entry_data",
                        answer_mode="profile_backed",
                        confidence=1.0,
                    )
                    continue
                skills = _profile_skill_values(profile_data)
                if skills:
                    # Oracle combobox selects need one skill at a time
                    # (handled by domhand_fill_repeaters). Skip comma-join
                    # only on Oracle FA hosts. Workday multi-select widgets
                    # NEED the comma-join — the executor splits and fills
                    # each skill via _fill_multi_select.
                    if _is_searchable_combobox_on_oracle(f, page_host=page_host):
                        continue
                    skill_value = ", ".join(skills)
                    direct_fills[f.field_id] = skill_value
                    resolved_values[f.field_id] = _resolved_field_value(
                        skill_value,
                        source="exact_profile",
                        answer_mode="profile_backed",
                        confidence=1.0,
                    )
                    continue
            if auth_overrides and _is_auth_like_field(f):
                fr = FillFieldResult(
                    field_id=f.field_id,
                    field_key=key,
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
            if (
                is_structured_language
                and structured_language_diag
                and structured_language_diag.slot_name == "language"
                and _field_has_effective_value(f)
                and not _force_refill
            ):
                # Workday language repeaters often mark the whole row invalid while
                # sibling proficiency dropdowns are still empty. Do not turn the
                # already-populated language selector itself into a blocker.
                fields_seen.add(get_stable_field_key(f))
                continue
            structured_language_val = None
            structured_language_entry_val = _known_entry_value_for_field(f, entry_data)
            if structured_language_entry_val:
                coerced_structured_language_entry_val = _coerce_answer_to_field(f, structured_language_entry_val)
                if coerced_structured_language_entry_val:
                    _set_structured_repeater_resolved_value(
                        structured_language_diag,
                        coerced_structured_language_entry_val,
                        source_key="entry_data",
                    )
                    _trace_structured_repeater_resolution(f, structured_language_diag)
                    direct_fills[f.field_id] = coerced_structured_language_entry_val
                    resolved_values[f.field_id] = _resolved_field_value(
                        coerced_structured_language_entry_val,
                        source="exact_profile",
                        answer_mode="profile_backed",
                        confidence=0.99,
                    )
                    continue
                if structured_language_diag and not structured_language_diag.failure_stage:
                    structured_language_diag.failure_stage = "value_coercion_empty"
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
            _edu_slot = _education_slot_name(f, fillable_fields)
            if _edu_slot in {"school", "field_of_study"}:
                logger.info(
                    "domhand.education_triage",
                    field_label=_preferred_field_label(f),
                    field_type=f.field_type,
                    is_native=f.is_native,
                    is_structured_edu=is_structured_education_candidate,
                    slot_name=_edu_slot,
                    entry_val=(str(entry_val)[:40] if entry_val else None),
                    section=f.section or "",
                )
            structured_education_diag = (
                StructuredRepeaterDiagnostic(
                    repeater_group="education",
                    field_id=f.field_id,
                    field_label=_preferred_field_label(f),
                    section=f.section or "",
                    slot_name=_edu_slot,
                    numeric_index=(_parse_heading_index(f.section) - 1)
                    if _parse_heading_index(f.section) is not None
                    else None,
                    current_value=str(f.current_value or "").strip(),
                )
                if is_structured_education_candidate
                else None
            )
            skip_oracle_education_combobox_coercion = (
                _structured_education_oracle_combobox_skip_dropdown_coercion(
                    f,
                    is_structured_education_candidate=is_structured_education_candidate,
                    structured_education_diag=structured_education_diag,
                )
            )
            # Fallback guard: Oracle FA school combobox should always go to needs_llm
            # even if structured education gate didn't fire (section mismatch, etc.)
            _label_norm_guard = normalize_name(_preferred_field_label(f))
            _is_school_label = any(tok in _label_norm_guard for tok in ("school", "university", "college", "institution"))
            if _is_school_label and f.field_type == "select" and not f.is_native:
                logger.info(
                    "domhand.oracle_school_guard_trace",
                    field_label=_preferred_field_label(f),
                    skip_already=skip_oracle_education_combobox_coercion,
                    page_host=page_host[:60] if page_host else "EMPTY",
                    is_oracle_fa=_is_searchable_combobox_on_oracle(f, page_host=page_host),
                    is_structured_edu=is_structured_education_candidate,
                    slot_name=(_edu_slot or "None"),
                    entry_val=(str(entry_val)[:40] if entry_val else "None"),
                )
            if (
                not skip_oracle_education_combobox_coercion
                and not f.is_native
                and f.field_type == "select"
                and _is_school_label
            ):
                # Force the skip for school on ANY Oracle page, not just FA hosts
                logger.info(
                    "domhand.oracle_school_fallback_guard",
                    field_label=_preferred_field_label(f),
                    page_host=page_host[:60] if page_host else "EMPTY",
                )
                skip_oracle_education_combobox_coercion = True
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
                        if skip_oracle_education_combobox_coercion:
                            entry_val = str(raw_structured_education_val).strip()
                            if entry_val:
                                _set_structured_repeater_resolved_value(
                                    structured_education_diag,
                                    entry_val,
                                    source_key=raw_structured_education_source,
                                )
                            elif structured_education_diag:
                                structured_education_diag.failure_stage = "entry_value_missing"
                        else:
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
            if (
                skip_oracle_education_combobox_coercion
                and entry_val
                and str(entry_val).strip()
            ):
                hint = str(entry_val).strip()
                f_llm = f.model_copy(update={"oracle_freeform_combobox_answer": True})
                oracle_combobox_search_hints[f_llm.field_id] = hint
                _trace_structured_repeater_resolution(f, structured_education_diag)
                needs_llm.append(f_llm)
                continue
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
            # Education location fields (Country/State/City) without entry data:
            # route to needs_llm so the LLM infers from the school name, not the user's
            # home address which the general profile resolver would return.
            if (
                is_structured_education_candidate
                and structured_education_diag
                and structured_education_diag.slot_name in {"school_country", "school_state", "school_city"}
                and not entry_val
            ):
                needs_llm.append(f)
                continue
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
            if _is_latest_employer_search_field(f):
                error_msg = "REQUIRED — latest employer search unresolved; use browser/manual fallback"
                if not f.required:
                    error_msg = "Latest employer search unresolved; use browser/manual fallback"
                fr = FillFieldResult(
                    field_id=f.field_id,
                    field_key=key,
                    name=_preferred_field_label(f),
                    success=False,
                    actor="skipped",
                    error=error_msg,
                    required=f.required,
                    control_kind=f.field_type,
                    section=f.section or "",
                    state="failed",
                    failure_reason="required_missing_profile_data" if f.required else "missing_profile_data",
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
            # Entity location guard: Country/State/City inside Education or Experience
            # sections refer to the school/company location, NOT the user's home address.
            # Route to needs_llm so the LLM infers from the entity in the profile text.
            _section_norm = normalize_name(f.section or "")
            _label_norm_loc = normalize_name(_preferred_field_label(f))
            if (
                _label_norm_loc in {"country", "state", "city", "province", "region"}
                and any(
                    tok in _section_norm
                    for tok in ("education", "experience", "work", "employment", "college", "university")
                )
            ):
                needs_llm.append(f)
                continue
            _has_constrained_choices = bool(f.choices or f.options) and f.field_type in {
                "button-group", "radio-group", "radio", "checkbox-group",
            }
            minimum_confidence = "medium" if (f.required or _has_constrained_choices) else "strong"
            resolved_profile_value = _resolve_known_profile_value_for_field(
                f,
                evidence,
                profile_data,
                minimum_confidence=minimum_confidence,
            )
            if resolved_profile_value and resolved_profile_value.value:
                direct_fills[f.field_id] = resolved_profile_value.value
                resolved_values[f.field_id] = resolved_profile_value
                continue
            needs_llm.append(f)

        total_already_filled_count += already_filled_count
        logger.info(
            "domhand.fill.round_triage",
            extra={
                "round": round_num,
                "direct_fills": len(direct_fills),
                "needs_llm": len(needs_llm),
                "needs_llm_types": dict(
                    __import__("collections").Counter(f.field_type for f in needs_llm)
                ) if needs_llm else {},
                "already_filled": already_filled_count,
                "skipped": len(fields_skipped),
            },
        )

        answers: dict[str, str] = {}
        if needs_llm:
            await _enrich_missing_select_options_for_llm(
                page,
                needs_llm,
                page_host=page_host,
                combobox_search_hints=oracle_combobox_search_hints,
            )
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

        # Override education location fields with school-specific LLM lookup.
        # Haiku returns the user's home address; GPT-5.4-nano knows school locations.
        if answers:
            _school_answer = None
            _edu_location_keys: dict[str, str] = {}  # answer_key → slot (city/state/country)
            for f in needs_llm:
                _f_label = _preferred_field_label(f)
                _f_norm = normalize_name(_f_label)
                _f_section = normalize_name(f.section or "")
                if _f_norm in {"school", "university", "institution", "college"} or "school" in _f_norm:
                    _school_answer = answers.get(_f_label, "")
                if (
                    _f_norm in {"country", "state", "city", "province", "region"}
                    and any(tok in _f_section for tok in ("education", "college", "university"))
                ):
                    _edu_location_keys[_f_label] = _f_norm
            # Use cached location from previous round, or look up fresh
            if not _school_answer and _edu_location_keys and _cached_school_location:
                for _key, _slot in _edu_location_keys.items():
                    _loc_val = _cached_school_location.get(_slot, "")
                    if _loc_val:
                        answers[_key] = _loc_val
            elif _school_answer and _edu_location_keys:
                try:
                    from ghosthands.dom.oracle_combobox_llm import oracle_school_location_llm
                    _location = await oracle_school_location_llm(_school_answer)
                    if _location:
                        _cached_school_location.update(_location)
                        for _key, _slot in _edu_location_keys.items():
                            _loc_val = _location.get(_slot, "")
                            if _loc_val:
                                answers[_key] = _loc_val
                except Exception:
                    pass

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

                if _force_refill and _field_has_effective_value(f):
                    current_val = str(f.current_value or "").strip()
                    if current_val and current_val.lower() == value.strip().lower():
                        has_val_error = await _field_has_validation_error(page, f.field_id)
                        if not has_val_error:
                            already_filled_count += 1
                            total_already_filled_count += 1
                            fields_seen.add(key)
                            continue

                resolved_value = resolved_values.get(
                    f.field_id,
                    _resolved_field_value(
                        value,
                        source="dom",
                        answer_mode="profile_backed",
                        confidence=0.95,
                    ),
                )
                success, field_error, failure_reason, fc, settled_value = await _attempt_domhand_fill_with_retry_cap(
                    page,
                    host=page_host,
                    field=f,
                    desired_value=value,
                    tool_name="domhand_fill",
                    browser_session=browser_session,
                )
                fr = FillFieldResult(
                    field_id=f.field_id,
                    field_key=key,
                    name=_preferred_field_label(f),
                    success=success,
                    actor="dom",
                    value_set=settled_value if success else None,
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
                        expected_value=settled_value,
                        source="exact_profile" if resolved_value.source == "exact_profile" else "derived_profile",
                        log_context="domhand.fill",
                    )
                    if not settled:
                        _record_unverified_custom_select_intent(
                            host=page_host,
                            page_context_key=page_context_key,
                            field=f,
                            field_key=key,
                            intended_value=settled_value,
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
            if resolved_value and _should_treat_llm_answer_as_na_placeholder(f, resolved_value.value):
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
                    field_key=key,
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
            success, field_error, failure_reason, fc, settled_value = await _attempt_domhand_fill_with_retry_cap(
                page,
                host=page_host,
                field=f,
                desired_value=matched_answer,
                tool_name="domhand_fill",
                browser_session=browser_session,
            )
            fr = FillFieldResult(
                field_id=f.field_id,
                field_key=key,
                name=_preferred_field_label(f),
                success=success,
                actor="dom",
                value_set=settled_value if success else None,
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
                    expected_value=settled_value,
                    source="exact_profile" if resolved_value.source == "exact_profile" else "derived_profile",
                    log_context="domhand.fill",
                )
                if not settled:
                    _record_unverified_custom_select_intent(
                        host=page_host,
                        page_context_key=page_context_key,
                        field=f,
                        field_key=key,
                        intended_value=settled_value,
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

        # Skip stagehand observe when called from repeaters (entry_data present).
        # Repeater fills are single-field, cross-ref is wasteful (~12s + cost per call).
        if browser_session and round_num == 1 and not params.entry_data:
            await _stagehand_observe_cross_reference(browser_session, known_ids | fields_seen, all_results)

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
            logger.info(f"Round {round_num} conditional pass {cond_pass}: {len(new_fields)} new fields revealed")
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
                    logger.info(
                        "domhand.conditional_rescan.deferred_to_next_round",
                        field_id=f.field_id,
                        field_label=_preferred_field_label(f),
                        field_type=f.field_type,
                    )
                    continue
                success, failure_msg, failure_reason, fc, settled_value = await _attempt_domhand_fill_with_retry_cap(
                    page,
                    host=page_host,
                    field=f,
                    desired_value=matched_answer,
                    tool_name="domhand_fill",
                    browser_session=browser_session,
                )
                fr = FillFieldResult(
                    field_id=f.field_id,
                    field_key=key,
                    control_kind=f.field_type,
                    name=_preferred_field_label(f),
                    section=f.section or "",
                    success=success,
                    actor="dom",
                    error=failure_msg if not success else None,
                    failure_reason=failure_reason if not success else None,
                    value_set=settled_value if success else None,
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
            rkey = r.field_key or r.field_id
            if r.fill_confidence > settled_fields.get(rkey, 0.0):
                settled_fields[rkey] = r.fill_confidence

        await asyncio.sleep(0.5)

    reconciled_results, cleared_stale_results = await _reconcile_fill_results_against_live_dom(
        page,
        all_results,
        target_section=params.target_section,
        heading_boundary=params.heading_boundary,
        focus_fields=params.focus_fields,
    )

    filled_count = sum(1 for r in reconciled_results if r.success)
    failed_count = sum(1 for r in reconciled_results if not r.success and r.actor == "dom")
    skipped_count = sum(1 for r in reconciled_results if r.actor == "skipped")
    unfilled_count = sum(1 for r in reconciled_results if r.actor == "unfilled")
    best_effort_results = [
        r for r in reconciled_results if r.success and r.answer_mode == "best_effort_guess" and r.value_set
    ]
    best_effort_binding_results = [r for r in reconciled_results if r.success and r.best_effort_guess]
    required_skipped = [
        f'  - "{r.name}" (REQUIRED — needs attention)'
        for r in reconciled_results
        if r.actor == "skipped" and r.error and "REQUIRED" in r.error
    ]
    optional_skipped = [
        f'  - "{r.name}" ({r.error or "no confident profile match"})'
        for r in reconciled_results
        if r.actor == "skipped" and (not r.error or "REQUIRED" not in r.error)
    ]
    failed_descriptions = [
        f'  - "{r.name}" ({r.error or "DOM fill failed"})'
        for r in reconciled_results
        if not r.success and r.actor == "dom"
    ]
    # ── Log-only summary (verbose, for debugging; never shown to the agent) ──
    summary_lines: list[str] = [
        f"DomHand fill complete: {filled_count} filled, {failed_count} DOM failures, "
        f"{total_already_filled_count} already correct, {skipped_count} skipped (no data), {unfilled_count} unfilled.",
        f"LLM calls: {llm_calls} (input: {total_input_tokens} tokens, output: {total_output_tokens} tokens)",
    ]
    if failed_descriptions:
        summary_lines.append("Failed fields (log only):")
        summary_lines.extend(failed_descriptions[:20])
    if required_skipped:
        summary_lines.append("Required skipped (log only):")
        summary_lines.extend(required_skipped[:20])

    _failed_all = [r for r in reconciled_results if not r.success]
    structured_summary_full = {
        "filled_count": filled_count,
        "already_filled_count": total_already_filled_count,
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
        "reconciled_settled_fields": cleared_stale_results,
        "unresolved_required_fields": [
            _fill_result_summary_entry(r) for r in reconciled_results if not r.success and r.required
        ],
        "failed_fields": [_fill_result_summary_entry(r) for r in _failed_all],
    }
    logger.debug(
        "domhand.fill.full_structured_summary %s",
        json.dumps(structured_summary_full, ensure_ascii=True),
    )

    structured_summary_agent = {
        "filled_count": filled_count,
        "already_filled_count": total_already_filled_count,
        "dom_failure_count": failed_count,
        "unfilled_count": unfilled_count,
    }

    summary = "\n".join(summary_lines)
    logger.info(summary)

    # ── Verification engine review (v3 parity) ───────────────────────
    #
    # Build structured per-field review using the unified verification engine.
    # Two-axis contract: execution_status x review_status.
    # The 0.55 readback_unverified path becomes review_status=unreadable.
    from ghosthands.dom.verification_engine import (
        FieldReviewResult,
        build_agent_digest,
        build_agent_prose,
        build_review_summary,
    )

    _fill_confidence_to_review: list[FieldReviewResult] = []
    for r in reconciled_results:
        if r.actor == "skipped":
            _fill_confidence_to_review.append(
                FieldReviewResult(
                    field_id=r.field_id,
                    label=(r.name or "")[:50],
                    field_type=r.control_kind or "",
                    required=r.required,
                    execution_status="not_attempted",
                    review_status="unsupported",
                    reason=r.error or "skipped (no profile data)",
                )
            )
        elif r.actor == "unfilled":
            _fill_confidence_to_review.append(
                FieldReviewResult(
                    field_id=r.field_id,
                    label=(r.name or "")[:50],
                    field_type=r.control_kind or "",
                    required=r.required,
                    execution_status="not_attempted",
                    review_status="unsupported",
                    reason=r.error or "unfilled",
                )
            )
        elif not r.success:
            _exec = "retry_capped" if r.failure_reason == "domhand_retry_capped" else "execution_failed"
            _fill_confidence_to_review.append(
                FieldReviewResult(
                    field_id=r.field_id,
                    label=(r.name or "")[:50],
                    field_type=r.control_kind or "",
                    required=r.required,
                    execution_status=_exec,
                    review_status="mismatch",
                    reason=r.error or "fill failed",
                )
            )
        elif r.fill_confidence >= 0.9:
            # DOM verified or reconciliation verified
            _fill_confidence_to_review.append(
                FieldReviewResult(
                    field_id=r.field_id,
                    label=(r.name or "")[:50],
                    field_type=r.control_kind or "",
                    required=r.required,
                    execution_status="executed",
                    review_status="verified",
                    reason="DOM readback matches expected",
                )
            )
        elif abs(r.fill_confidence - 0.55) < 0.01:
            # THE FIX: readback_unverified → review_status=unreadable, NEVER verified
            _fill_confidence_to_review.append(
                FieldReviewResult(
                    field_id=r.field_id,
                    label=(r.name or "")[:50],
                    field_type=r.control_kind or "",
                    required=r.required,
                    execution_status="executed",
                    review_status="unreadable",
                    reason="executor succeeded but DOM readback did not match within poll window",
                )
            )
        else:
            # 0.6 (Stagehand) or 0.8 (LLM) — treat as verified (escalation succeeded)
            _fill_confidence_to_review.append(
                FieldReviewResult(
                    field_id=r.field_id,
                    label=(r.name or "")[:50],
                    field_type=r.control_kind or "",
                    required=r.required,
                    execution_status="executed",
                    review_status="verified",
                    reason=f"verified via escalation (confidence={r.fill_confidence})",
                )
            )

    # Add already-filled fields as already_settled
    for _ in range(total_already_filled_count):
        _fill_confidence_to_review.append(
            FieldReviewResult(
                field_id="",
                label="",
                field_type="",
                required=False,
                execution_status="already_settled",
                review_status="verified",
                reason="field already had correct value",
            )
        )

    _review_summary = build_review_summary(_fill_confidence_to_review)
    _agent_digest_json = build_agent_digest(_review_summary)
    _agent_prose = build_agent_prose(_review_summary)

    # ── Agent-facing text summary ─────────────────────────────────────
    #
    # Use the verification engine prose as primary summary.
    # Append filled field labels for agent context.
    filled_labels = ", ".join(
        _truncate_agent_fill_text(r.name, 40) for r in reconciled_results[:15] if r.success and r.name.strip()
    )
    agent_summary = _agent_prose
    if filled_labels:
        agent_summary += f" Fields: {filled_labels}."
    if browser_session is not None:
        setattr(
            browser_session,
            "_gh_last_domhand_fill",
            {
                "page_context_key": page_context_key,
                "page_url": page_url,
                "target_section": params.target_section or "",
                "heading_boundary": params.heading_boundary or "",
                "focus_fields": list(params.focus_fields or []),
                "broad_fill_completed": not bool(params.heading_boundary or params.focus_fields or entry_data),
                "field_ids": [r.field_id for r in reconciled_results if r.field_id][:50],
            },
        )
        if params.heading_boundary and (filled_count > 0 or total_already_filled_count > 0):
            _scope_key = (page_context_key, (params.heading_boundary or "").strip().lower())
            completed_scoped[_scope_key] = {
                "filled_count": filled_count,
                "already_filled_count": total_already_filled_count,
            }

    # long_term_memory: prose + capped JSON digest (≤1500 chars, PII-redacted)
    _long_term = f"{agent_summary}\n{_agent_digest_json}"

    logger.info(
        "domhand.fill.EXIT",
        extra={
            "page_context_key": page_context_key,
            "filled_count": filled_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "already_filled_count": total_already_filled_count,
            "total_results": len(reconciled_results),
            "llm_calls": llm_calls,
        },
    )

    # Clear stale assess_state so the watchdog won't block Next clicks
    # based on outdated unresolved_required data from before this fill.
    if failed_count == 0 and hasattr(browser_session, "_gh_last_application_state"):
        try:
            delattr(browser_session, "_gh_last_application_state")
        except AttributeError:
            pass

    return ActionResult(
        extracted_content=agent_summary,
        long_term_memory=_long_term,
        include_extracted_content_only_once=True,
        metadata={
            "tool": "domhand_fill",
            "step_cost": total_step_cost,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "model": model_name,
            "domhand_llm_calls": llm_calls,
            "domhand_fill_json": structured_summary_agent,
            "domhand_fill_full_json": structured_summary_full,
            "domhand_fill_agent_summary": agent_summary,
            "domhand_fill_log_summary": summary,
            "domhand_fill_review": [
                {
                    "field_id": r.field_id,
                    "label": r.label,
                    "field_type": r.field_type,
                    "required": r.required,
                    "execution_status": r.execution_status,
                    "review_status": r.review_status,
                    "reason": r.reason,
                    "actual_read": r.actual_read,
                    "has_validation_error": r.has_validation_error,
                }
                for r in _fill_confidence_to_review
                if r.field_id  # skip the placeholder already_settled entries
            ],
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
