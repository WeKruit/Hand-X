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
from dataclasses import dataclass
import inspect
import json
import logging
import os
import re
from datetime import date
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
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
    get_repeater_field_binding,
    get_learned_question_alias,
    get_interaction_recipe,
    get_cached_semantic_alias,
    get_domhand_failure_count,
    record_expected_field_value,
    record_repeater_field_binding,
    has_cached_semantic_alias,
    is_domhand_retry_capped,
    record_domhand_failure,
    record_interaction_recipe,
    stage_learned_question_alias,
)

logger = logging.getLogger(__name__)

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
_SOCIAL_OR_ID_NO_GUESS_RE = re.compile(
    r"\b(twitter|x(\.com)?\s*(handle|username|profile)?|github|gitlab|linkedin"
    r"|instagram|tiktok|facebook|social\s*(media|profile)?|handle|username|user\s*name"
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
_SKILL_FIELD_MAX_ITEMS = 10
_MULTI_SELECT_CHECKBOX_PROMPT_RE = re.compile(
    r"\b(select|check|choose|mark)\s+all\s+that\s+apply\b|\ball that apply\b",
    re.IGNORECASE,
)
_EXCLUSIVE_CHOICE_CHECKBOX_PROMPT_RE = re.compile(
    r"^(are|do|did|have|has|will|would|can|is|please\s+(select|choose|check)\s+one)\b",
    re.IGNORECASE,
)
_EXCLUSIVE_CHOICE_OPTION_PREFIXES = (
    "yes",
    "no",
    "i do not",
    "prefer not",
    "decline",
)
DOMHAND_RETRY_CAPPED = "domhand_retry_capped"
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

_MATCH_CONFIDENCE_RANKS = {
    "exact": 4,
    "strong": 3,
    "medium": 2,
    "weak": 1,
}

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


def _checkbox_group_mode(field: FormField) -> str:
    """Classify grouped checkboxes conservatively to preserve current multi-select behavior."""
    if field.field_type != "checkbox-group":
        return "multi_select"

    prompt = normalize_name(field.raw_label or field.name or "")
    if _MULTI_SELECT_CHECKBOX_PROMPT_RE.search(prompt):
        return "multi_select"

    choices = [normalize_name(choice) for choice in field.choices if normalize_name(choice)]
    if len(choices) < 2 or len(choices) > 4:
        return "multi_select"

    if all(any(choice.startswith(prefix) for prefix in _EXCLUSIVE_CHOICE_OPTION_PREFIXES) for choice in choices):
        return "exclusive_choice"

    if (
        len(choices) == 2
        and any(choice == "yes" or choice.startswith("yes ") for choice in choices)
        and any(choice == "no" or choice.startswith("no ") or choice.startswith("i do not ") for choice in choices)
    ):
        return "exclusive_choice"

    if _EXCLUSIVE_CHOICE_CHECKBOX_PROMPT_RE.search(prompt) and all(len(choice.split()) <= 8 for choice in choices):
        prefixed = [
            any(choice.startswith(prefix) for prefix in _EXCLUSIVE_CHOICE_OPTION_PREFIXES) for choice in choices
        ]
        if prefixed.count(True) >= 2:
            return "exclusive_choice"

    return "multi_select"


def _checkbox_group_is_exclusive_choice(field: FormField) -> bool:
    return _checkbox_group_mode(field) == "exclusive_choice"


def _domhand_retry_field_identity(field: FormField) -> str:
    return get_stable_field_key(field)


def _domhand_retry_message(field: FormField) -> str:
    return (
        f'"{_preferred_field_label(field)}" hit the DomHand retry cap after '
        f"{DOMHAND_RETRY_CAP} failed attempts. Use browser-use or one screenshot/vision fallback."
    )


def _should_trace_profile_label(label: str) -> bool:
    norm = normalize_name(label or "")
    return any(token in norm for token in _PROFILE_TRACE_LABEL_TOKENS)


def _trace_profile_resolution(event: str, *, field_label: str, **extra: Any) -> None:
    if not _profile_debug_enabled():
        return
    if not _should_trace_profile_label(field_label):
        return
    logger.info(
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


_PAGE_CONTEXT_SCAN_JS = r"""() => {
	const visible = (el) => {
		if (!el) return false;
		const style = window.getComputedStyle(el);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		const rect = el.getBoundingClientRect();
		return rect.width > 0 && rect.height > 0;
	};
	const normalize = (text) => (text || '').replace(/\s+/g, ' ').trim();
	const allText = Array.from(document.querySelectorAll('h1, h2, h3, button, a, [role="button"], label, p, span, div'))
		.filter((el) => visible(el))
		.map((el) => normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || ''))
		.filter(Boolean);
	const hasText = (patterns) => allText.some((text) => patterns.some((pattern) => pattern.test(text.toLowerCase())));
	const headingTexts = Array.from(document.querySelectorAll('h1, h2, h3, [role="heading"]'))
		.filter((el) => visible(el))
		.map((el) => normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || ''))
		.filter(Boolean)
		.slice(0, 5);
	const passwordCount = document.querySelectorAll('input[type="password"]').length;
	const emailCount = document.querySelectorAll('input[type="email"], input[name*="email" i], input[id*="email" i]').length;
	const confirmPasswordVisible =
		passwordCount >= 2 ||
		document.querySelector('[data-automation-id="verifyPassword"]') !== null;
	const createAccountSignals = hasText([/\bcreate account\b/, /\bregister\b/, /\bsign up\b/]);
	const signInSignals = hasText([/\bsign in\b/, /\blog in\b/, /\blogin\b/]);
	const startDialogSignals = hasText([/\bstart your application\b/, /\bautofill with resume\b/, /\bapply manually\b/, /\buse my last application\b/]);
	let pageMarker = '';
	if (confirmPasswordVisible || createAccountSignals) {
		pageMarker = 'auth create account';
	} else if (emailCount >= 1 && passwordCount >= 1 && signInSignals) {
		pageMarker = 'auth native login';
	} else if (startDialogSignals) {
		pageMarker = 'auth entry';
	} else if (headingTexts.length > 0) {
		pageMarker = headingTexts[0];
	}
	return JSON.stringify({
		page_marker: pageMarker,
		heading_texts: headingTexts,
	});
}"""


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

_EXTRACT_FIELDS_JS = r"""() => {
	var ff = window.__ff;
	if (!ff) return JSON.stringify([]);
	var seen = new Set();
	var out = [];

	var shouldSkip = function(el) {
		if (ff.closestCrossRoot(el, '[class*="select-dropdown"], [class*="select-option"]')) return true;
		if (ff.closestCrossRoot(el, '.iti__dropdown-content')) return true;
		if (ff.closestCrossRoot(el, '[data-automation-id="activeListContainer"]')) return true;
		if (ff.closestCrossRoot(el, '[role="listbox"], [role="menu"]')) return true;
		if (el.getAttribute('role') === 'listbox' && el.closest('[role="combobox"]')) return true;
		if (el.getAttribute('role') === 'listbox' && el.id) {
			var controller = ff.queryOne('[role="combobox"][aria-controls="' + el.id + '"]');
			if (controller) return true;
		}
		if (el.tagName === 'INPUT' && el.type === 'search' && ff.closestCrossRoot(el, '[class*="dropdown"], [role="dialog"]')) return true;
		if (el.tagName === 'INPUT' && (el.type === 'radio' || el.type === 'checkbox') && window.getComputedStyle(el).display === 'none') return true;
		return false;
	};

	var getOptionMainText = function(opt) {
		var clone = opt.cloneNode(true);
		clone.querySelectorAll('[class*="desc"], [class*="sub"], [class*="hint"], .option-desc, small').forEach(function(x) { x.remove(); });
		return clone.textContent ? clone.textContent.trim() : '';
	};

	var cleanControlText = function(node) {
		if (!node) return '';
		var clone = node.cloneNode(true);
		clone.querySelectorAll(
			'input, textarea, select, button, [role="radio"], [role="checkbox"], [role="switch"], [class*="desc"], [class*="sub"], [class*="hint"], .option-desc, small'
		).forEach(function(x) { x.remove(); });
		return clone.textContent ? clone.textContent.replace(/\s+/g, ' ').trim() : '';
	};

	var getGroupQuestionLabel = function(el, itemLabel) {
		var itemNorm = (itemLabel || '').replace(/\s+/g, ' ').trim().toLowerCase();
		var container = ff.closestCrossRoot(
			el,
			'fieldset, [role="radiogroup"], [role="group"], .radio-group, .checkbox-group, .radio-cards, [data-automation-id="formField"], [data-automation-id*="formField"]'
		) || ff.rootParent(el) || el;

		var candidates = [];
		var push = function(node) {
			if (!node) return;
			var text = cleanControlText(node);
			if (!text) return;
			var norm = text.toLowerCase();
			if (itemNorm && (norm === itemNorm || norm === ('* ' + itemNorm) || norm === (itemNorm + ' *'))) return;
			if (text.length > 200) return;
			candidates.push(text);
		};

		push(container.querySelector(':scope > legend'));
		push(container.querySelector(':scope > [data-automation-id="fieldLabel"]'));
		push(container.querySelector(':scope > [data-automation-id*="fieldLabel"]'));
		push(container.querySelector(':scope > label'));
		push(container.querySelector(':scope > [class*="question"]'));
		push(container.previousElementSibling);

		var formField = ff.closestCrossRoot(container, '[data-automation-id="formField"], [data-automation-id*="formField"]');
		if (formField && formField !== container) {
			push(formField.querySelector('legend'));
			push(formField.querySelector('[data-automation-id="fieldLabel"]'));
			push(formField.querySelector('[data-automation-id*="fieldLabel"]'));
			push(formField.querySelector('label'));
			push(formField.querySelector('[class*="question"]'));
			push(formField.previousElementSibling);
		}

		for (var i = 0; i < candidates.length; i++) {
			if (candidates[i]) return candidates[i];
		}
		return '';
	};

	var getFieldWrapperMeta = function(el) {
		var wrapper = ff.closestCrossRoot(
			el,
			'[data-automation-id="formField"], [data-automation-id*="formField"], fieldset, .form-group, .field'
		) || el.parentElement || el;
		var cleanText = function(node) {
			if (!node) return '';
			var clone = node.cloneNode(true);
			clone.querySelectorAll(
				'input, textarea, select, button, [role="radio"], [role="checkbox"], [role="switch"], [role="textbox"], [role="combobox"], [class*="desc"], [class*="sub"], [class*="hint"], .option-desc, small'
			).forEach(function(x) { x.remove(); });
			return clone.textContent ? clone.textContent.replace(/\s+/g, ' ').trim() : '';
		};
		var label = '';
		var candidates = [
			wrapper.querySelector(':scope > legend'),
			wrapper.querySelector(':scope > [data-automation-id="fieldLabel"]'),
			wrapper.querySelector(':scope > [data-automation-id*="fieldLabel"]'),
			wrapper.querySelector(':scope > label'),
			wrapper.querySelector(':scope > [class*="question"]')
		];
		for (var i = 0; i < candidates.length; i++) {
			var text = cleanText(candidates[i]);
			if (!text) continue;
			label = text;
			break;
		}
		var hasCalendarTrigger = !!wrapper.querySelector(
			'button[aria-label*="calendar" i], button[title*="calendar" i], [data-automation-id*="datePicker"], [data-automation-id*="dateIcon"], [data-automation-id*="dateTrigger"]'
		);
		var formatHint = '';
		var placeholders = [];
		var placeholderNodes = wrapper.querySelectorAll('input[placeholder]');
		for (var j = 0; j < placeholderNodes.length; j++) {
			var placeholder = (placeholderNodes[j].getAttribute('placeholder') || '').trim();
			if (!placeholder) continue;
			if (placeholders.indexOf(placeholder) === -1) placeholders.push(placeholder);
		}
		if (placeholders.length >= 2) {
			formatHint = placeholders.join('/');
		} else if (placeholders.length === 1) {
			formatHint = placeholders[0];
		}
		return {
			wrapperId: wrapper ? ff.tag(wrapper) : '',
			wrapperLabel: label,
			hasCalendarTrigger: hasCalendarTrigger,
			formatHint: formatHint
		};
	};

	ff.queryAll(ff.SELECTOR).forEach(function(el) {
		if (seen.has(el)) return;
		seen.add(el);
		if (shouldSkip(el)) return;

		var id = ff.tag(el);
		var type = (function() {
			var role = el.getAttribute('role');
			if (role === 'textbox' && el.getAttribute('aria-multiline') === 'true') return 'textarea';
			if (role === 'textbox') return 'text';
			if (role === 'combobox') return 'select';
			if (role === 'listbox') return 'select';
			if (el.getAttribute('data-uxi-widget-type') === 'selectinput') return 'select';
			if (el.getAttribute('aria-haspopup') === 'listbox') return 'select';
			if (role === 'radio') return 'radio';
			if (role === 'checkbox') return 'checkbox';
			if (role === 'spinbutton') return 'number';
			if (role === 'slider') return 'range';
			if (role === 'searchbox') return 'search';
			if (role === 'switch') return 'toggle';
			if (el.tagName === 'SELECT') return 'select';
			if (el.tagName === 'TEXTAREA') return 'textarea';
			var t = el.type || '';
			var typeMap = {text:'text', email:'email', tel:'tel', url:'url', number:'number', date:'date', file:'file', checkbox:'checkbox', radio:'radio', search:'search', password:'password'};
			return typeMap[t] || t || 'text';
		})();

		if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'image' || type === 'reset') return;

		var visible = (function() {
			if (type === 'file' && !ff.isVisible(el)) {
				var container = el.closest('[class*=upload], [class*=drop], .form-group, .field');
				return container ? ff.isVisible(container) : false;
			}
			return ff.isVisible(el);
		})();
		if (!visible) return;

		var isNative = el.tagName === 'SELECT';
		var isMultiSelect = type === 'select' && !isNative && !!(
			el.querySelector('[class*="multi"]') ||
			el.classList.toString().includes('multi') ||
			el.getAttribute('aria-multiselectable') === 'true'
		);

		var entry = {
			field_id: id,
			name: ff.getAccessibleName(el),
			field_type: type,
			section: ff.getSection(el),
			name_attr: el.getAttribute('name') || '',
			required: el.required || el.getAttribute('aria-required') === 'true' || el.dataset.required === 'true' || el.dataset.ffRequired === 'true',
			options: [],
			choices: [],
			accept: el.accept || null,
			is_native: isNative || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA',
			is_multi_select: isMultiSelect || el.multiple || el.getAttribute('aria-multiselectable') === 'true',
			visible: true,
			raw_label: ff.getAccessibleName(el),
			synthetic_label: false,
			field_fingerprint: null,
			current_value: ''
		};
		var isSelectLike = type === 'select';
		var looksLikeOpaqueValue = function(text) {
			var value = (text || '').replace(/\s+/g, ' ').trim();
			if (!value) return false;
			return /^(?:[0-9a-f]{16,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/i.test(value);
		};
		var isUnsetLike = function(text) {
			var value = (text || '').replace(/\s+/g, ' ').trim();
			if (!value) return true;
			if (/^(select one|choose one|please select)$/i.test(value)) return true;
			if (/\b(select one|choose one|please select)\b/i.test(value)) return true;
			return looksLikeOpaqueValue(value);
		};
		var readVisibleSelectValue = function(node) {
			if (!node) return '';
			var localVisibleText = function(target) {
				if (!target) return '';
				var style = window.getComputedStyle(target);
				if (!style || style.visibility === 'hidden' || style.display === 'none') return '';
				var rect = target.getBoundingClientRect();
				if (!rect || rect.width === 0 || rect.height === 0) return '';
				return (target.textContent || '').replace(/\s+/g, ' ').trim();
			};
			var ownText = localVisibleText(node);
			if (ownText && !isUnsetLike(ownText)) return ownText;
			var wrapper = ff.closestCrossRoot(
				node,
				'[data-automation-id="formField"], [data-automation-id*="formField"], .form-group, .field'
			) || node.parentElement || node;
			var tokenSelectors = [
				'[data-automation-id*="selected"]',
				'[data-automation-id*="Selected"]',
				'[data-automation-id*="token"]',
				'[data-automation-id*="Token"]',
				'[class*="token"]',
				'[class*="Token"]',
				'[class*="pill"]',
				'[class*="Pill"]',
				'[class*="chip"]',
				'[class*="Chip"]',
				'[class*="tag"]',
				'[class*="Tag"]'
			];
			for (var si = 0; si < tokenSelectors.length; si++) {
				var tokenNodes = wrapper.querySelectorAll(tokenSelectors[si]);
				for (var sj = 0; sj < tokenNodes.length; sj++) {
					var tokenText = localVisibleText(tokenNodes[sj]);
					if (!tokenText || isUnsetLike(tokenText)) continue;
					return tokenText;
				}
			}
			var wrapperText = localVisibleText(wrapper);
			if (wrapperText && !isUnsetLike(wrapperText) && wrapperText.length <= 120) {
				return wrapperText;
			}
			var ariaLabel = (node.getAttribute('aria-label') || '').trim();
			if (ariaLabel && !isUnsetLike(ariaLabel)) return ariaLabel;
			return '';
		};

		if (type === 'select') {
			var opts = [];
			if (el.tagName === 'SELECT') {
				opts = Array.from(el.options)
					.filter(function(o) { return o.value !== ''; })
					.map(function(o) { return o.textContent ? o.textContent.trim() : ''; })
					.filter(Boolean);
			} else {
				var ctrlId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
				var src = ctrlId ? ff.getByDomId(ctrlId) : null;
				if (!src && el.tagName === 'INPUT') {
					src = ff.closestCrossRoot(el, '[class*="select"], [class*="combobox"], .form-group, .field');
				}
				if (!src) src = el;
				if (src) {
					opts = Array.from(src.querySelectorAll('[role="option"], [role="menuitem"]'))
						.map(function(o) { return getOptionMainText(o); }).filter(Boolean);
				}
			}
			if (opts.length) entry.options = opts;
		}

		if (type === 'checkbox' || type === 'radio') {
			var labelEl = el.querySelector('[class*="label"], .rc-label');
			if (labelEl) {
				entry.itemLabel = labelEl.textContent ? labelEl.textContent.trim() : '';
			} else {
				var wrap = el.closest('label');
				if (wrap) {
					var c = wrap.cloneNode(true);
					c.querySelectorAll('input, [class*=desc], small').forEach(function(x) { x.remove(); });
					entry.itemLabel = c.textContent ? c.textContent.trim() : '';
				} else {
					entry.itemLabel = el.getAttribute('aria-label') || ff.getAccessibleName(el);
				}
			}
			entry.itemValue = el.value || (el.querySelector('input') ? el.querySelector('input').value : '') || '';
			entry.questionLabel = getGroupQuestionLabel(el, entry.itemLabel);
			entry.groupKey = el.name || el.getAttribute('name') || entry.questionLabel || entry.itemLabel || id;
			if (entry.questionLabel) {
				entry.name = entry.questionLabel;
				entry.raw_label = entry.questionLabel;
			}
		}

		if (el.tagName === 'SELECT') {
			var selOpt = el.options[el.selectedIndex];
			entry.current_value = selOpt ? selOpt.text.trim() : '';
		} else if (isSelectLike) {
			entry.current_value = readVisibleSelectValue(el);
		} else if (type === 'checkbox' || type === 'radio') {
			if (el.tagName === 'INPUT') entry.current_value = el.checked ? 'checked' : '';
			else entry.current_value = el.getAttribute('aria-checked') === 'true' ? 'checked' : '';
		} else if (el.getAttribute('role') === 'checkbox' || el.getAttribute('role') === 'switch') {
			entry.current_value = el.getAttribute('aria-checked') === 'true' ? 'checked' : '';
		} else {
			entry.current_value = el.value || (el.textContent ? el.textContent.trim() : '') || '';
		}

		var wrapperMeta = getFieldWrapperMeta(el);
		if (wrapperMeta.wrapperId) entry.wrapper_id = wrapperMeta.wrapperId;
		if (wrapperMeta.wrapperLabel) entry.wrapper_label = wrapperMeta.wrapperLabel;
		if (wrapperMeta.hasCalendarTrigger) entry.has_calendar_trigger = true;
		if (wrapperMeta.formatHint) entry.format_hint = wrapperMeta.formatHint;
		var labelNorm = (entry.name || '').replace(/\s+/g, ' ').trim().toLowerCase();
		if ((type === 'number' || type === 'text' || type === 'date') && (labelNorm === 'month' || labelNorm === 'day' || labelNorm === 'year')) {
			entry.date_component = labelNorm;
			entry.date_group_key = wrapperMeta.wrapperId || wrapperMeta.wrapperLabel || entry.section || '';
			if (wrapperMeta.wrapperLabel) entry.group_label = wrapperMeta.wrapperLabel;
		}

		out.push(entry);
	});
	return JSON.stringify(out);
}"""


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

_FILL_FIELD_JS = r"""(ffId, value, fieldType) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return JSON.stringify({success: false, error: 'Element not found'});

	try {
		if (fieldType === 'select' && el.tagName === 'SELECT') {
			var lowerValue = value.toLowerCase();
			var matched = false;
			for (var i = 0; i < el.options.length; i++) {
				var opt = el.options[i];
				var optText = (opt.text || '').trim().toLowerCase();
				var optVal = (opt.value || '').toLowerCase();
				if (optText === lowerValue || optVal === lowerValue) {
					el.value = opt.value; matched = true; break;
				}
			}
			if (!matched) {
				for (var j = 0; j < el.options.length; j++) {
					var o = el.options[j];
					var oText = (o.text || '').trim().toLowerCase();
					if (oText.includes(lowerValue) || lowerValue.includes(oText)) {
						el.value = o.value; matched = true; break;
					}
				}
			}
			if (!matched) return JSON.stringify({success: false, error: 'No matching option for: ' + value});
			el.dispatchEvent(new Event('change', {bubbles: true}));
			el.dispatchEvent(new Event('input', {bubbles: true}));
			return JSON.stringify({success: true});
		}

		if (fieldType === 'checkbox' || fieldType === 'radio' || fieldType === 'toggle') {
			var shouldCheck = /^(checked|true|yes|on|1)$/i.test(value);
			if (el.tagName === 'INPUT') {
				if (el.checked !== shouldCheck) el.click();
			} else {
				var current = el.getAttribute('aria-checked') === 'true';
				if (current !== shouldCheck) el.click();
			}
			return JSON.stringify({success: true});
		}

		var proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
		var nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value');
		if (nativeSetter && nativeSetter.set) {
			nativeSetter.set.call(el, value);
		} else {
			el.value = value;
		}
		el.dispatchEvent(new Event('input', {bubbles: true}));
		el.dispatchEvent(new Event('change', {bubbles: true}));
		el.dispatchEvent(new Event('blur', {bubbles: true}));
		return JSON.stringify({success: true});
	} catch (e) {
		return JSON.stringify({success: false, error: e.message});
	}
}"""

_FILL_CONTENTEDITABLE_JS = r"""(ffId, value) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return JSON.stringify({success: false, error: 'Element not found'});
	try {
		el.textContent = value;
		el.dispatchEvent(new Event('input', {bubbles: true}));
		el.dispatchEvent(new Event('change', {bubbles: true}));
		return JSON.stringify({success: true});
	} catch (e) {
		return JSON.stringify({success: false, error: e.message});
	}
}"""

_FILL_DATE_JS = r"""(ffId, value) => {
	var ff = window.__ff || {byId: function(id){ return document.querySelector('[data-ff-id="'+id+'"]'); }};
	var el = ff.byId(ffId);
	if (!el) return JSON.stringify({success: false, error: 'Element not found'});
	try {
		var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
		if (setter && setter.set) setter.set.call(el, value);
		else el.value = value;
		el.dispatchEvent(new Event('input', {bubbles: true}));
		el.dispatchEvent(new Event('change', {bubbles: true}));
		return JSON.stringify({success: true});
	} catch (e) {
		return JSON.stringify({success: false, error: e.message});
	}
}"""

_CLICK_RADIO_OPTION_JS = r"""(ffId, text) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({clicked: false, error: 'Element not found'});
	var group = ff.closestCrossRoot(
		el,
		'fieldset, [role="radiogroup"], [role="group"], .radio-cards, .radio-group, .checkbox-group, [data-automation-id="formField"], [data-automation-id*="formField"]'
	) || ff.rootParent(el) || el;
	var items = group.querySelectorAll(
		'[role="radio"], [role="checkbox"], [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"], label.radio-card, .radio-card, .radio-option, label.checkbox-card, .checkbox-card, .checkbox-option, [role="button"], [role="cell"], input[type="radio"], input[type="checkbox"]'
	);
	var lower = text.toLowerCase().trim();
	var getClickable = function(item) {
		return ff.closestCrossRoot(
			item,
			'button, label, [role="row"], [role="cell"], [role="button"], [role="radio"], [role="checkbox"], .radio-card, .radio-option, .checkbox-card, .checkbox-option, [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"], [data-automation-id*="radio"], [data-automation-id*="checkbox"]'
		) || item.parentElement || item;
	};
	var getText = function(item) {
		var labelEl = item.querySelector('[class*="label"], .rc-label');
		if (labelEl && labelEl.textContent) return labelEl.textContent.trim();
		var clickable = getClickable(item);
		return (clickable && clickable.textContent ? clickable.textContent : item.textContent || '').trim();
	};
	for (var i = 0; i < items.length; i++) {
		var item = items[i];
		var itemText = getText(item);
		var itemLower = itemText.toLowerCase();
		if (itemLower === lower || itemLower.includes(lower) || lower.includes(itemLower)) {
			var clickable = getClickable(item);
			if (clickable && clickable.scrollIntoView) {
				clickable.scrollIntoView({block: 'center', inline: 'center'});
			}
			clickable.click();
			return JSON.stringify({clicked: true, text: itemText});
		}
	}
	return JSON.stringify({clicked: false, error: 'No matching radio option'});
}"""

_CLICK_SINGLE_RADIO_JS = r"""(ffId, text) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({clicked: false});
	var normalize = function(v) { return v.trim().toLowerCase(); };
	var target = normalize(text);
	var ownInput = el.tagName === 'INPUT' ? el : el.querySelector('input[type="radio"]');
	var radios = [];
	if (ownInput && ownInput.name) {
		var root = ownInput.form || document;
		radios = Array.from(root.querySelectorAll('input[type="radio"][name="' + CSS.escape(ownInput.name) + '"]'));
		if (radios.length === 0) radios = ff.queryAll('input[type="radio"][name="' + CSS.escape(ownInput.name) + '"]') || [];
	} else {
		var group = ff.closestCrossRoot(el, '[role="radiogroup"], [role="group"], fieldset, .radio-group') || ff.rootParent(el) || el;
		radios = Array.from(group.querySelectorAll('input[type="radio"], [role="radio"]'));
	}
	if (radios.length === 0) radios = [el];
	var getLabel = function(node) {
		if (node.id) { var byFor = ff.queryOne('label[for="' + CSS.escape(node.id) + '"]'); if (byFor && byFor.textContent && byFor.textContent.trim()) return byFor.textContent.trim(); }
		var ariaLabel = node.getAttribute('aria-label'); if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
		var wrap = ff.closestCrossRoot(node, 'label, [role="radio"], .radio-card, .radio-option');
		return (wrap ? wrap.textContent : node.textContent || '').trim();
	};
	for (var i = 0; i < radios.length; i++) {
		var radio = radios[i];
		var label = normalize(getLabel(radio));
		if (!label) continue;
		if (label === target || label.includes(target) || target.includes(label)) {
			var isChecked = radio.checked || radio.getAttribute('aria-checked') === 'true';
			if (isChecked) return JSON.stringify({clicked: true, alreadyChecked: true});
			radio.click(); return JSON.stringify({clicked: true, alreadyChecked: false});
		}
	}
	return JSON.stringify({clicked: false});
}"""

_CLICK_CHECKBOX_GROUP_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({clicked: false});
	var group = ff.closestCrossRoot(el, '.checkbox-group, [role="group"]') || el;
	var cbs = Array.from(group.querySelectorAll('input[type="checkbox"], [role="checkbox"]'));
	if (cbs.length === 0) return JSON.stringify({clicked: false});
	for (var i = 0; i < cbs.length; i++) {
		if (cbs[i].checked || cbs[i].getAttribute('aria-checked') === 'true') return JSON.stringify({clicked: true, alreadyChecked: true});
	}
	cbs[0].click(); return JSON.stringify({clicked: true, alreadyChecked: false});
}"""

_READ_BINARY_STATE_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify(null);
	if (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) return JSON.stringify(el.checked);
	var ac = el.getAttribute('aria-checked');
	if (ac === 'true') return JSON.stringify(true);
	if (ac === 'false') return JSON.stringify(false);
	return JSON.stringify(null);
}"""

_CLICK_BINARY_FIELD_JS = r"""(ffId, desiredChecked) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({clicked: false, error: 'Element not found'});

	var getControl = function(node) {
		if (!node) return null;
		if (
			node.matches &&
			node.matches(
				'input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"], [role="switch"], [aria-checked], [aria-pressed], [aria-selected]'
			)
		) {
			return node;
		}
		return node.querySelector
			? node.querySelector(
				'input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"], [role="switch"], [aria-checked], [aria-pressed], [aria-selected]'
			)
			: null;
	};
	var getState = function(control) {
		if (!control) return null;
		if (control.tagName === 'INPUT' && (control.type === 'checkbox' || control.type === 'radio')) return control.checked;
		var ariaChecked = control.getAttribute('aria-checked');
		if (ariaChecked === 'true') return true;
		if (ariaChecked === 'false') return false;
		var ariaPressed = control.getAttribute('aria-pressed');
		if (ariaPressed === 'true') return true;
		if (ariaPressed === 'false') return false;
		var ariaSelected = control.getAttribute('aria-selected');
		if (ariaSelected === 'true') return true;
		if (ariaSelected === 'false') return false;
		return null;
	};
	var dispatchClickSequence = function(node) {
		if (!node) return;
		var mouseOpts = { bubbles: true, cancelable: true, view: window };
		node.dispatchEvent(new MouseEvent('mouseover', mouseOpts));
		node.dispatchEvent(new MouseEvent('mouseenter', mouseOpts));
		node.dispatchEvent(new MouseEvent('mousedown', mouseOpts));
		node.dispatchEvent(new MouseEvent('mouseup', mouseOpts));
		node.dispatchEvent(new MouseEvent('click', mouseOpts));
	};
	var control = getControl(el) || el;
	var wrapper = ff.closestCrossRoot(
		control,
		'label, [role="row"], [role="cell"], [role="button"], .checkbox-card, .checkbox-option, .radio-card, .radio-option, [data-automation-id*="checkbox"], [data-automation-id*="radio"], [data-automation-id*="promptOption"]'
	) || control.parentElement || control;

	var currentState = getState(control);
	if (currentState === desiredChecked) {
		return JSON.stringify({clicked: true, alreadyChecked: true});
	}

	if (wrapper && wrapper.scrollIntoView) {
		wrapper.scrollIntoView({block: 'center', behavior: 'instant'});
	}
	if (wrapper && wrapper !== control) {
		if (wrapper.focus) wrapper.focus();
		if (wrapper.click) wrapper.click();
		dispatchClickSequence(wrapper);
		if (getState(control) === desiredChecked) {
			return JSON.stringify({clicked: true, strategy: 'wrapper'});
		}
	}

	if (control.focus) control.focus();
	if (control.click) control.click();
	dispatchClickSequence(control);
	if (control.tagName === 'INPUT' && (control.type === 'checkbox' || control.type === 'radio')) {
		control.dispatchEvent(new Event('input', {bubbles: true}));
		control.dispatchEvent(new Event('change', {bubbles: true}));
	}
	if (getState(control) === desiredChecked) {
		return JSON.stringify({clicked: true, strategy: 'control'});
	}

	return JSON.stringify({clicked: true, strategy: 'unconfirmed'});
}"""

_GET_BINARY_CLICK_TARGET_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({found: false, error: 'Element not found'});

	var getControl = function(node) {
		if (!node) return null;
		if (
			node.matches &&
			node.matches(
				'input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"], [role="switch"], [aria-checked], [aria-pressed], [aria-selected]'
			)
		) {
			return node;
		}
		return node.querySelector
			? node.querySelector(
				'input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"], [role="switch"], [aria-checked], [aria-pressed], [aria-selected]'
			)
			: null;
	};
	var control = getControl(el) || el;
	var target = ff.closestCrossRoot(
		control,
		'label, [role="row"], [role="cell"], [role="button"], .checkbox-card, .checkbox-option, .radio-card, .radio-option, [data-automation-id*="checkbox"], [data-automation-id*="radio"], [data-automation-id*="promptOption"]'
	) || control.parentElement || control;
	if (target && target.scrollIntoView) {
		target.scrollIntoView({block: 'center', inline: 'center'});
	}
	var rect = target && target.getBoundingClientRect ? target.getBoundingClientRect() : null;
	if (!rect || rect.width === 0 || rect.height === 0) {
		if (control && control.scrollIntoView) {
			control.scrollIntoView({block: 'center', inline: 'center'});
		}
		rect = control && control.getBoundingClientRect ? control.getBoundingClientRect() : null;
		target = control;
	}
	if (!rect || rect.width === 0 || rect.height === 0) {
		return JSON.stringify({found: false, error: 'No visible click target'});
	}
	return JSON.stringify({
		found: true,
		text: ((target && target.textContent) || (control && control.getAttribute && control.getAttribute('aria-label')) || '').trim(),
		x: Math.round(rect.left + (rect.width / 2)),
		y: Math.round(rect.top + (rect.height / 2))
	});
}"""

_READ_GROUP_SELECTION_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({selected: ''});
	var group = ff.closestCrossRoot(
		el,
		'fieldset, [role="radiogroup"], [role="group"], .radio-group, .radio-cards, .checkbox-group, [data-automation-id="formField"], [data-automation-id*="formField"]'
	) || ff.rootParent(el) || el;
	var nodes = Array.from(
		group.querySelectorAll(
			'[role="radio"], [role="checkbox"], input[type="radio"], input[type="checkbox"], label.radio-card, .radio-card, .radio-option, label.checkbox-card, .checkbox-card, .checkbox-option, button, [role="button"], [role="cell"], [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"]'
		)
	);
	var seen = new Set();
	var filtered = [];
	for (var i = 0; i < nodes.length; i++) {
		var node = nodes[i];
		if (seen.has(node)) continue;
		seen.add(node);
		filtered.push(node);
	}
	var getLabel = function(node) {
		if (!node) return '';
		if (node.id) {
			var byFor = ff.queryOne('label[for="' + CSS.escape(node.id) + '"]');
			if (byFor && byFor.textContent && byFor.textContent.trim()) return byFor.textContent.trim();
		}
		var ariaLabel = node.getAttribute('aria-label');
		if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
		var wrap = ff.closestCrossRoot(
			node,
			'label, [role="radio"], [role="checkbox"], .radio-card, .radio-option, .checkbox-card, .checkbox-option, [role="button"], [role="cell"], [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"]'
		) || node;
		return (wrap.textContent || '').trim();
	};
	var isSelected = function(node) {
		if (!node) return false;
		if (node.matches && node.matches('input[type="radio"], input[type="checkbox"]') && node.checked) return true;
		var ariaChecked = node.getAttribute && node.getAttribute('aria-checked');
		if (ariaChecked === 'true') return true;
		var ariaPressed = node.getAttribute && node.getAttribute('aria-pressed');
		if (ariaPressed === 'true') return true;
		var ariaSelected = node.getAttribute && node.getAttribute('aria-selected');
		if (ariaSelected === 'true') return true;
		var nested = node.querySelector && node.querySelector('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]');
		if (nested) {
			if (nested.matches && nested.matches('input[type="radio"], input[type="checkbox"]') && nested.checked) return true;
			if (nested.getAttribute && nested.getAttribute('aria-checked') === 'true') return true;
		}
		var className = typeof node.className === 'string' ? node.className : '';
		return /\b(selected|checked|active)\b/i.test(className);
	};
	for (var j = 0; j < filtered.length; j++) {
		var candidate = filtered[j];
		if (isSelected(candidate)) return JSON.stringify({selected: getLabel(candidate)});
	}
	return JSON.stringify({selected: ''});
}"""

_GET_GROUP_OPTION_TARGET_JS = r"""(ffId, text) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({found: false, error: 'Element not found'});
	var group = ff.closestCrossRoot(
		el,
		'fieldset, [role="radiogroup"], [role="group"], .radio-group, .radio-cards, .checkbox-group, [data-automation-id="formField"], [data-automation-id*="formField"]'
	) || ff.rootParent(el) || el;
	var nodes = Array.from(
		group.querySelectorAll(
			'[role="radio"], [role="checkbox"], input[type="radio"], input[type="checkbox"], label.radio-card, .radio-card, .radio-option, label.checkbox-card, .checkbox-card, .checkbox-option, button, [role="button"], [role="cell"], [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"]'
		)
	);
	var lower = text.toLowerCase().trim();
	var best = null;
	var getClickable = function(node) {
		return ff.closestCrossRoot(
			node,
			'button, label, [role="row"], [role="cell"], [role="button"], [role="radio"], [role="checkbox"], .radio-card, .radio-option, .checkbox-card, .checkbox-option, [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"], [data-automation-id*="radio"], [data-automation-id*="checkbox"]'
		) || node.parentElement || node;
	};
	var getLabel = function(node) {
		if (!node) return '';
		if (node.id) {
			var byFor = ff.queryOne('label[for="' + CSS.escape(node.id) + '"]');
			if (byFor && byFor.textContent && byFor.textContent.trim()) return byFor.textContent.trim();
		}
		var ariaLabel = node.getAttribute('aria-label');
		if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
		var wrap = getClickable(node);
		return (wrap.textContent || '').trim();
	};
	for (var i = 0; i < nodes.length; i++) {
		var node = nodes[i];
		var label = getLabel(node);
		if (!label) continue;
		var labelLower = label.toLowerCase().trim();
		var score = 0;
		if (labelLower === lower) score = 3;
		else if (labelLower.includes(lower) || lower.includes(labelLower)) score = 2;
		if (score === 0) continue;
		var clickable = getClickable(node);
		if (clickable && clickable.scrollIntoView) {
			clickable.scrollIntoView({block: 'center', inline: 'center'});
		}
		var rect = clickable.getBoundingClientRect();
		if (!rect || rect.width === 0 || rect.height === 0) continue;
		if (!best || score > best.score) {
			best = {
				found: true,
				score: score,
				text: label,
				x: Math.round(rect.left + (rect.width / 2)),
				y: Math.round(rect.top + (rect.height / 2)),
			};
		}
	}
	return JSON.stringify(best || {found: false, error: 'No matching option'});
}"""

_HAS_FIELD_VALIDATION_ERROR_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify(false);
	var nodes = [];
	var seen = new Set();
	var push = function(node) {
		if (!node || seen.has(node)) return;
		seen.add(node);
		nodes.push(node);
	};
	push(el);
	push(ff.closestCrossRoot(el, '[aria-invalid], [role="group"], [role="radiogroup"], fieldset, label, [role="row"], [role="cell"], .form-group, .field, [data-automation-id*="formField"]'));
	if (el.querySelector) {
		push(el.querySelector('[aria-invalid], input, textarea, select, [role="checkbox"], [role="radio"], [role="switch"], [role="textbox"], [role="combobox"]'));
	}
	for (var i = 0; i < nodes.length; i++) {
		var node = nodes[i];
		if (!node) continue;
		if (node.getAttribute && node.getAttribute('aria-invalid') === 'true') return JSON.stringify(true);
		if (node.querySelector && node.querySelector('[aria-invalid="true"]')) return JSON.stringify(true);
	}
	return JSON.stringify(false);
}"""

_READ_FIELD_VALUE_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify('');
	var looksLikeOpaqueValue = function(text) {
		var value = (text || '').replace(/\s+/g, ' ').trim();
		if (!value) return false;
		return /^(?:[0-9a-f]{16,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/i.test(value);
	};
	var isUnsetLike = function(text) {
		var value = (text || '').replace(/\s+/g, ' ').trim();
		if (!value) return true;
		if (/^(select one|choose one|please select)$/i.test(value)) return true;
		if (/\b(select one|choose one|please select)\b/i.test(value)) return true;
		return looksLikeOpaqueValue(value);
	};
	var visibleText = function(node) {
		if (!node) return '';
		var style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return '';
		var rect = node.getBoundingClientRect();
		if (!rect || rect.width === 0 || rect.height === 0) return '';
		return (node.textContent || '').replace(/\s+/g, ' ').trim();
	};
	var isSelectLike = el.getAttribute('role') === 'combobox'
		|| el.getAttribute('role') === 'listbox'
		|| el.getAttribute('data-uxi-widget-type') === 'selectinput'
		|| el.getAttribute('aria-haspopup') === 'listbox';
	if (el.tagName === 'SELECT') {
		var selOpt = el.options[el.selectedIndex];
		return JSON.stringify(selOpt ? (selOpt.textContent || '').trim() : '');
	}
	if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && !isSelectLike) {
		var directValue = (el.value || '').trim();
		if (directValue) return JSON.stringify(directValue);
	}
	var value = '';
	if (!isSelectLike && typeof el.value === 'string' && el.value.trim()) {
		value = el.value.trim();
	}
	if (!value && isSelectLike) {
		var ownText = visibleText(el);
		if (ownText && !isUnsetLike(ownText)) {
			value = ownText;
		}
		var wrapper = ff.closestCrossRoot(
			el,
			'[data-automation-id="formField"], [data-automation-id*="formField"], .form-group, .field'
		) || el.parentElement || el;
		var tokenSelectors = [
			'[data-automation-id*="selected"]',
			'[data-automation-id*="Selected"]',
			'[data-automation-id*="token"]',
			'[data-automation-id*="Token"]',
			'[class*="token"]',
			'[class*="Token"]',
			'[class*="pill"]',
			'[class*="Pill"]',
			'[class*="chip"]',
			'[class*="Chip"]',
			'[class*="tag"]',
			'[class*="Tag"]'
		];
		for (var i = 0; i < tokenSelectors.length && !value; i++) {
			var tokenNodes = wrapper.querySelectorAll(tokenSelectors[i]);
			for (var j = 0; j < tokenNodes.length; j++) {
				var tokenText = visibleText(tokenNodes[j]);
				if (!tokenText) continue;
				if (isUnsetLike(tokenText) || /^required$/i.test(tokenText)) continue;
				value = tokenText;
				break;
			}
		}
		if (!value) {
			var wrapperText = visibleText(wrapper);
			if (wrapperText && !isUnsetLike(wrapperText) && !/^required$/i.test(wrapperText) && wrapperText.length <= 120) {
				value = wrapperText;
			}
		}
	}
	if (!value) {
		var ariaLabel = el.getAttribute('aria-label');
		if (ariaLabel && ariaLabel.trim() && !isUnsetLike(ariaLabel)) {
			value = ariaLabel.trim();
		}
	}
	if (!value && el.textContent) {
		value = el.textContent.trim();
	}
	if (isUnsetLike(value)) value = '';
	return JSON.stringify(value || '');
}"""

_CLICK_BUTTON_GROUP_JS = r"""(ffId, text) => {
	var ff = window.__ff;
	var container = ff ? ff.byId(ffId) : null;
	if (!container) return JSON.stringify({clicked: false});
	var textLower = text.toLowerCase().trim();
	var btns = container.querySelectorAll('button, [role="button"]');
	for (var i = 0; i < btns.length; i++) { var bt = (btns[i].textContent || '').trim().toLowerCase(); if (bt === textLower) { btns[i].click(); return JSON.stringify({clicked: true}); } }
	for (var j = 0; j < btns.length; j++) { var btt = (btns[j].textContent || '').trim().toLowerCase(); if (btt.includes(textLower) || textLower.includes(btt)) { btns[j].click(); return JSON.stringify({clicked: true}); } }
	return JSON.stringify({clicked: false});
}"""

_IS_SEARCHABLE_DROPDOWN_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify(false);
	return JSON.stringify(
		el.getAttribute('role') === 'combobox' ||
		el.getAttribute('data-uxi-widget-type') === 'selectinput' ||
		el.getAttribute('data-automation-id') === 'searchBox' ||
		(el.getAttribute('autocomplete') === 'off' && el.getAttribute('aria-controls'))
	);
}"""

_CLICK_DROPDOWN_OPTION_JS = r"""(text) => {
	var lowerText = text.toLowerCase();
	function qAll(sel) {
		if (window.__ff && window.__ff.queryAll) return window.__ff.queryAll(sel);
		return Array.from(document.querySelectorAll(sel));
	}
	var roleEls = qAll('[role="option"], [role="menuitem"], [role="treeitem"], [data-automation-id*="promptOption"], [data-automation-id*="menuItem"]');
	for (var i = 0; i < roleEls.length; i++) {
		var o = roleEls[i]; var rect = o.getBoundingClientRect();
		if (rect.width === 0 || rect.height === 0) continue;
		var t = (o.textContent || '').trim().toLowerCase();
		if (t === lowerText || t.includes(lowerText)) { o.click(); return JSON.stringify({clicked: true, text: (o.textContent || '').trim()}); }
	}
	var allVisible = qAll('div[tabindex], div[data-automation-id], span[data-automation-id], li, a, button, [role="button"]');
	for (var j = 0; j < allVisible.length; j++) {
		var el = allVisible[j]; var r = el.getBoundingClientRect();
		if (r.width === 0 || r.height === 0) continue;
		var directText = '';
		for (var k = 0; k < el.childNodes.length; k++) { var n = el.childNodes[k]; directText += (n.textContent || ''); }
		directText = directText.trim().toLowerCase();
		if (directText && (directText === lowerText || directText.includes(lowerText))) { el.click(); return JSON.stringify({clicked: true, text: directText}); }
	}
	return JSON.stringify({clicked: false});
}"""

_ELEMENT_EXISTS_JS = r"""(ffId, fieldType) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify(false);
	if (ff.isVisible(el)) return JSON.stringify(true);
	if (fieldType === 'file') {
		var container = ff.closestCrossRoot(el, '[class*=upload], [class*=drop], .form-group, .field');
		return JSON.stringify(container ? ff.isVisible(container) : false);
	}
	return JSON.stringify(false);
}"""

_REVEAL_SECTIONS_JS = r"""() => {
	document.querySelectorAll('[data-section], .form-section, .form-step, .step-content, .tab-pane, .accordion-content, .panel-body, [role="tabpanel"]').forEach(function(el) {
		el.style.display = ''; el.classList.add('active'); el.removeAttribute('hidden'); el.setAttribute('aria-hidden', 'false');
	});
	return 'ok';
}"""

_DISMISS_DROPDOWN_JS = r"""() => {
	var active = document.activeElement;
	if (active) active.blur();
	document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
	document.body.dispatchEvent(new MouseEvent('click', {bubbles: true}));
	return 'ok';
}"""

_CLICK_OTHER_TEXTLIKE_FIELD_JS = r"""(ffId) => {
	var ff = window.__ff;
	var current = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	var isVisible = function(node) {
		if (!node) return false;
		var style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		var rect = node.getBoundingClientRect();
		return !!rect && rect.width > 0 && rect.height > 0;
	};
	var sameFamily = function(a, b) {
		if (!a || !b || !a.contains || !b.contains) return false;
		return a === b || a.contains(b) || b.contains(a);
	};
	var selectors = [
		'input:not([type="hidden"]):not([disabled])',
		'textarea:not([disabled])',
		'select:not([disabled])',
		'[role="textbox"]',
		'[role="combobox"]',
		'[contenteditable="true"]'
	].join(', ');
	var nodes = ff && ff.queryAll ? ff.queryAll(selectors) : Array.from(document.querySelectorAll(selectors));
	for (var i = 0; i < nodes.length; i++) {
		var node = nodes[i];
		if (!node || sameFamily(current, node) || !isVisible(node)) continue;
		try {
			if (node.scrollIntoView) node.scrollIntoView({block: 'center', inline: 'center'});
			node.click();
			return JSON.stringify({clicked: true, fieldId: node.getAttribute('data-ff-id') || ''});
		} catch (e) {}
	}
	if (current && current.blur) current.blur();
	document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
	document.body.dispatchEvent(new MouseEvent('click', {bubbles: true}));
	return JSON.stringify({clicked: false});
}"""

_FOCUS_AND_CLEAR_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return JSON.stringify({ok: false, error: 'not found'});
	el.focus();
	if (el.select) el.select();
	return JSON.stringify({ok: true});
}"""

_FOCUS_FIELD_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return JSON.stringify({ok: false, error: 'not found'});
	if (el.focus) el.focus();
	return JSON.stringify({ok: true});
}"""

_CLICK_ALTERNATE_FIELD_JS = r"""(ffId) => {
	var ff = window.__ff;
	var current = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!current) return JSON.stringify({clicked: false, error: 'current_not_found'});
	var isVisible = function(node) {
		if (!node) return false;
		var style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		var rect = node.getBoundingClientRect();
		return !!rect && rect.width > 0 && rect.height > 0;
	};
	var candidates = ff ? ff.queryAll(ff.SELECTOR) : Array.from(document.querySelectorAll('[data-ff-id]'));
	for (var i = 0; i < candidates.length; i++) {
		var node = candidates[i];
		if (!node || node === current) continue;
		var nodeId = (node.getAttribute && node.getAttribute('data-ff-id')) || '';
		if (nodeId && nodeId === ffId) continue;
		if (!isVisible(node)) continue;
		var clickable = (ff && ff.closestCrossRoot)
			? ff.closestCrossRoot(node, 'input, textarea, select, button, [role="textbox"], [role="combobox"], [role="spinbutton"]')
			: null;
		if (!clickable) clickable = node;
		if (!isVisible(clickable)) continue;
		if (clickable.scrollIntoView) {
			clickable.scrollIntoView({block: 'center', inline: 'center'});
		}
		if (clickable.click) clickable.click();
		if (clickable.focus) clickable.focus();
		return JSON.stringify({clicked: true, target_id: nodeId || '', target_tag: clickable.tagName || ''});
	}
	return JSON.stringify({clicked: false, error: 'no_alternate_visible_field'});
}"""

_OPEN_GROUPED_DATE_PICKER_JS = r"""(ffId) => {
	var ff = window.__ff;
	var container = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!container) return JSON.stringify({clicked: false, opened: false, error: 'container_not_found'});
	var all = Array.from(container.querySelectorAll('button, [role="button"], [aria-haspopup]'));
	var trigger = null;
	for (var i = 0; i < all.length; i++) {
		var node = all[i];
		var text = ((node.getAttribute('aria-label') || '') + ' ' + (node.getAttribute('title') || '') + ' ' + (node.textContent || '')).toLowerCase();
		if (text.indexOf('calendar') !== -1 || text.indexOf('date') !== -1) {
			trigger = node;
			break;
		}
	}
	if (!trigger) {
		trigger = container.querySelector('[data-automation-id*="datePicker"], [data-automation-id*="dateIcon"], [data-automation-id*="dateTrigger"]');
	}
	if (!trigger) return JSON.stringify({clicked: false, opened: false, error: 'trigger_not_found'});
	if (trigger.scrollIntoView) trigger.scrollIntoView({block: 'center', inline: 'center'});
	if (trigger.click) trigger.click();
	var dialog = document.querySelector('[role="dialog"], [data-automation-id*="datePicker"], [data-automation-id*="calendar"], [class*="calendar"]');
	return JSON.stringify({clicked: true, opened: !!dialog});
}"""

_SELECT_GROUPED_DATE_PICKER_VALUE_JS = r"""(monthName, dayValue, yearValue) => {
	var dialogs = Array.from(document.querySelectorAll('[role="dialog"], [data-automation-id*="datePicker"], [data-automation-id*="calendar"], [class*="calendar"]'));
	if (dialogs.length === 0) return JSON.stringify({selected: false, error: 'picker_not_open'});
	var roots = dialogs;
	var targetMonth = (monthName || '').toLowerCase();
	var targetDay = String(dayValue || '').trim();
	var targetYear = String(yearValue || '').trim();
	var matches = function(text) {
		var lower = (text || '').replace(/\s+/g, ' ').trim().toLowerCase();
		if (!lower) return false;
		if (targetMonth && lower.indexOf(targetMonth) === -1) return false;
		if (targetYear && lower.indexOf(targetYear) === -1) return false;
		return lower.indexOf(' ' + targetDay + ' ') !== -1
			|| lower.endsWith(' ' + targetDay)
			|| lower.indexOf('/' + targetDay + '/') !== -1
			|| lower.indexOf('-' + targetDay + '-') !== -1;
	};
	for (var i = 0; i < roots.length; i++) {
		var nodes = roots[i].querySelectorAll('button, [role="button"], [role="gridcell"], [role="cell"], td, div');
		for (var j = 0; j < nodes.length; j++) {
			var node = nodes[j];
			var text = (node.getAttribute && node.getAttribute('aria-label')) || node.textContent || '';
			if (!matches(text)) continue;
			if (node.scrollIntoView) node.scrollIntoView({block: 'center', inline: 'center'});
			if (node.click) node.click();
			return JSON.stringify({selected: true});
		}
	}
	return JSON.stringify({selected: false, error: 'target_not_found'});
}"""


# ── Profile evidence extraction ──────────────────────────────────────


def _parse_profile_evidence(profile_text: str) -> dict[str, str | None]:
    """Extract structured fields from profile text for direct field matching."""
    stripped = profile_text.strip()

    def _score_date(value: Any) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        parts = text.split("-")
        try:
            year = int(parts[0])
        except (TypeError, ValueError):
            return 0
        month = 1
        day = 1
        if len(parts) > 1:
            try:
                month = int(parts[1])
            except (TypeError, ValueError):
                month = 1
        if len(parts) > 2:
            try:
                day = int(parts[2])
            except (TypeError, ValueError):
                day = 1
        return (year * 10_000) + (month * 100) + day

    def _pick_latest_education_entry(raw_entries: Any) -> dict[str, Any] | None:
        if not isinstance(raw_entries, list):
            return None
        entries = [entry for entry in raw_entries if isinstance(entry, dict)]
        if not entries:
            return None
        ranked = sorted(
            entries,
            key=lambda entry: _score_date(
                entry.get("endDate")
                or entry.get("end_date")
                or entry.get("graduationDate")
                or entry.get("graduation_date")
                or entry.get("startDate")
                or entry.get("start_date")
            ),
            reverse=True,
        )
        return ranked[0] if ranked else None

    def _format_graduation_date(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        parts = text.split("-")
        if len(parts) < 2:
            return text
        try:
            month_index = int(parts[1])
        except (TypeError, ValueError):
            return text
        if month_index < 1 or month_index > 12:
            return text
        month_labels = [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
        return f"{month_labels[month_index - 1]} {parts[0]}"

    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):

            def _read_text(*keys: str) -> str | None:
                for key in keys:
                    text = _entry_text_value(data.get(key))
                    if text:
                        return text
                return None

            name = str(data.get("name") or "").strip() or None
            first_name = _read_text("first_name", "firstName")
            last_name = _read_text("last_name", "lastName")
            if name and not first_name:
                first_name = name.split()[0] if name.split() else None
            if name and not last_name and len(name.split()) > 1:
                last_name = " ".join(name.split()[1:])

            location = data.get("location")
            city = str(data.get("city") or "").strip() or None
            state = str(data.get("state") or data.get("province") or "").strip() or None
            zip_code = str(data.get("zip") or data.get("zip_code") or data.get("postal_code") or "").strip() or None
            county = str(data.get("county") or "").strip() or None
            if isinstance(location, str) and location.strip() and (not city or not state or not zip_code):
                parts = [p.strip() for p in location.split(",") if p.strip()]
                if len(parts) >= 2:
                    city = city or parts[0]
                    state_zip = parts[1].split()
                    state = state or (state_zip[0] if state_zip else None)
                    zip_code = zip_code or (state_zip[1] if len(state_zip) > 1 else None)

            latest_education = _pick_latest_education_entry(data.get("education"))
            latest_degree = _entry_text_value((latest_education or {}).get("degree")) or None
            latest_degree_type = _read_text("degree_type", "degreeType")
            latest_field_of_study = (
                _entry_text_value(
                    (latest_education or {}).get("field")
                    or (latest_education or {}).get("fieldOfStudy")
                    or (latest_education or {}).get("field_of_study")
                    or (latest_education or {}).get("major")
                    or (latest_education or {}).get("majors")
                    or (latest_education or {}).get("majorName")
                    or (latest_education or {}).get("majorNames")
                    or (latest_education or {}).get("major_name")
                    or (latest_education or {}).get("major_names")
                    or (latest_education or {}).get("area_of_study")
                    or (latest_education or {}).get("areaOfStudy")
                    or (latest_education or {}).get("discipline")
                    or ""
                )
                or None
            )
            latest_minor = _entry_text_value(
                (latest_education or {}).get("minor")
                or (latest_education or {}).get("minors")
                or (latest_education or {}).get("minorName")
                or (latest_education or {}).get("minorNames")
                or (latest_education or {}).get("minor_name")
                or (latest_education or {}).get("minor_names")
                or ""
            )
            latest_honors = _entry_text_value(
                (latest_education or {}).get("honors")
                or (latest_education or {}).get("honours")
                or (latest_education or {}).get("honor")
                or (latest_education or {}).get("honorsList")
                or (latest_education or {}).get("honoursList")
                or (latest_education or {}).get("honors_list")
                or (latest_education or {}).get("honours_list")
                or ""
            )
            latest_graduation_date = _format_graduation_date(
                (latest_education or {}).get("graduationDate")
                or (latest_education or {}).get("graduation_date")
                or (latest_education or {}).get("endDate")
                or (latest_education or {}).get("end_date")
            )

            github = str(data.get("github") or data.get("github_url") or "").strip() or None
            if not github:
                github_match = re.search(r"https?://(?:www\.)?github\.com/[^\s)]+", profile_text, re.IGNORECASE)
                github = github_match.group(0) if github_match else None

            twitter = (
                str(data.get("twitter") or data.get("twitter_url") or data.get("x") or data.get("x_url") or "").strip()
                or None
            )
            if not twitter:
                twitter_match = re.search(
                    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s)]+", profile_text, re.IGNORECASE
                )
                twitter = twitter_match.group(0) if twitter_match else None

            return {
                "first_name": first_name,
                "last_name": last_name,
                "email": str(data.get("email") or "").strip() or None,
                "phone": str(data.get("phone") or "").strip() or None,
                "address": str(data.get("address") or "").strip() or None,
                "address_line_2": str(data.get("address_line_2") or "").strip() or None,
                "city": city,
                "state": state,
                "zip": zip_code,
                "county": county,
                "country": _read_text("country"),
                "phone_device_type": _read_text("phone_device_type", "phone_type", "phoneDeviceType"),
                "phone_country_code": _read_text("phone_country_code", "phoneCountryCode"),
                "linkedin": _read_text("linkedin", "linkedin_url", "linkedIn"),
                "portfolio": str(
                    data.get("portfolio")
                    or data.get("website")
                    or data.get("personal_website")
                    or data.get("personalWebsite")
                    or ""
                ).strip()
                or None,
                "github": github,
                "twitter": twitter,
                # Workday-relevant fields
                "work_authorization": _read_text("work_authorization", "workAuthorization"),
                "available_start_date": _read_text("available_start_date", "availableStartDate"),
                "availability_window": _read_text("availability_window", "availabilityWindow"),
                "notice_period": _read_text("notice_period", "noticePeriod"),
                "salary_expectation": _read_text("salary_expectation", "salaryExpectation"),
                "current_school_year": _read_text("current_school_year", "currentSchoolYear"),
                "graduation_date": _read_text("graduation_date", "graduationDate") or latest_graduation_date,
                "degree_seeking": _read_text("degree_seeking", "degreeSeeking") or latest_degree,
                "degree_type": _read_text("degree_type", "degreeType") or latest_degree_type,
                "certifications_licenses": _read_text("certifications_licenses", "certificationsLicenses"),
                "field_of_study": latest_field_of_study,
                "minor": latest_minor,
                "honors": latest_honors,
                "spoken_languages": _read_text("spoken_languages", "spokenLanguages"),
                "english_proficiency": _read_text("english_proficiency", "englishProficiency"),
                "languages": data.get("languages") if isinstance(data.get("languages"), list) else None,
                "country_of_residence": _read_text("country_of_residence", "countryOfResidence"),
                "how_did_you_hear": _read_text("how_did_you_hear", "referral_source", "howDidYouHear"),
                "willing_to_relocate": _read_text("willing_to_relocate", "willingToRelocate"),
                "preferred_work_mode": _read_text("preferred_work_mode", "preferredWorkMode"),
                "preferred_locations": _read_text("preferred_locations", "preferredLocations"),
            }

    def read_line(label: str) -> str | None:
        m = re.search(rf"^\s*{re.escape(label)}:\s*(.+)$", profile_text, re.MULTILINE | re.IGNORECASE)
        val = m.group(1).strip() if m else None
        return val if val else None

    first_name = read_line("First name") or read_line("First Name")
    last_name = read_line("Last name") or read_line("Last Name")
    name = read_line("Full name") or read_line("Name")
    if name and not first_name:
        first_name = name.split()[0] if name.split() else None
    if name and not last_name and len(name.split()) > 1:
        last_name = " ".join(name.split()[1:])

    location = read_line("Location")
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    if location:
        parts = [p.strip() for p in location.split(",") if p.strip()]
        if len(parts) >= 2:
            city = parts[0]
            state_zip = parts[1].split()
            state = state_zip[0] if state_zip else None
            zip_code = state_zip[1] if len(state_zip) > 1 else None

    linkedin = read_line("LinkedIn")
    portfolio = read_line("Portfolio") or read_line("Website")
    github_match = re.search(r"https?://(?:www\.)?github\.com/[^\s)]+", profile_text, re.IGNORECASE)
    twitter_match = re.search(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s)]+", profile_text, re.IGNORECASE)

    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": read_line("Email"),
        "phone": read_line("Phone"),
        "address": read_line("Address"),
        "address_line_2": read_line("Address line 2") or read_line("Address Line 2"),
        "city": city,
        "state": state,
        "zip": zip_code,
        "county": read_line("County"),
        "country": read_line("Country"),
        "phone_device_type": read_line("Phone type") or read_line("Phone device type"),
        "phone_country_code": read_line("Phone country code") or read_line("Country phone code"),
        "linkedin": linkedin,
        "portfolio": portfolio,
        "github": github_match.group(0) if github_match else None,
        "twitter": twitter_match.group(0) if twitter_match else None,
        # Workday-relevant fields
        "work_authorization": read_line("Work authorization"),
        "available_start_date": read_line("Available start date"),
        "availability_window": read_line("Availability to start") or read_line("Earliest start date"),
        "notice_period": read_line("Notice period"),
        "salary_expectation": read_line("Salary expectation"),
        "current_school_year": read_line("Current year in school") or read_line("School year"),
        "graduation_date": read_line("Graduation date") or read_line("Estimated graduation date"),
        "degree_seeking": read_line("Degree seeking") or read_line("What degree are you seeking"),
        "certifications_licenses": read_line("Certifications or licenses")
        or read_line("Relevant certifications or licenses"),
        "field_of_study": read_line("Field of study") or read_line("Major") or read_line("Area of study"),
        "spoken_languages": read_line("Preferred spoken languages")
        or read_line("Spoken languages")
        or read_line("Languages spoken")
        or read_line("Language proficiency"),
        "english_proficiency": read_line("English proficiency")
        or read_line("Overall")
        or read_line("Reading")
        or read_line("Writing")
        or read_line("Speaking")
        or read_line("Comprehension"),
        "country_of_residence": read_line("Country of residence") or read_line("Country"),
        "how_did_you_hear": read_line("How did you hear about us"),
        "willing_to_relocate": read_line("Willing to relocate"),
        "preferred_work_mode": read_line("Preferred work setup"),
        "preferred_locations": read_line("Preferred locations"),
    }


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


def _coerce_answer_to_field(field: FormField, answer: str | None) -> str | None:
    """Map a profile answer onto the closest available field option when present."""
    if answer in (None, ""):
        return None
    text = str(answer).strip()
    if not text:
        return None

    choices = [str(choice).strip() for choice in (field.options or field.choices or []) if str(choice).strip()]
    if not choices:
        return text

    text_norm = normalize_name(text)
    for choice in choices:
        if normalize_name(choice) == text_norm:
            return choice

    boolish = _normalize_yes_no_answer(text)
    if boolish:
        for choice in choices:
            if normalize_name(choice) == normalize_name(boolish):
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
    return None


def _field_label_candidates(field: FormField) -> list[str]:
    """Return deduplicated field labels ordered from most to least descriptive."""
    seen: set[str] = set()
    candidates: list[str] = []
    for label in (field.raw_label, field.name):
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
    }
    return replacements.get(normalized, normalized)


_SECTION_SCOPE_CHILDREN: dict[str, set[str]] = {
    # Workday nests Education / Languages under the page-level "My Experience"
    # step. When domhand_fill targets "My Experience", it should treat those
    # subsections as part of the same fill scope instead of excluding them.
    "experience": {"education", "languages", "language", "skills", "certifications"},
    # Workday also nests address/phone/legal-name groups under the page-level
    # "My Information" step.
    "information": {"address", "phone", "legal name", "name", "contact", "contact information"},
    # Workday keeps the terms acknowledgment under Voluntary Disclosures.
    "voluntary disclosures": {"terms and conditions", "terms conditions"},
}


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


def _filter_fields_for_scope(
    fields: list[FormField],
    target_section: str | None = None,
    heading_boundary: str | None = None,
    focus_fields: list[str] | None = None,
    *,
    allow_all_visible_fallback: bool = True,
) -> list[FormField]:
    """Restrict fields to a section and/or repeater entry boundary."""
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
                    logger.info(
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
            logger.info(
                "DomHand scope found no live match for target section without fallback",
                extra={"target_section": target_section, "field_count": len(fields)},
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
    if not focus_fields:
        return FocusFieldSelection(fields=fields, ambiguous_labels={})

    normalized_pairs = [
        (label, _normalize_match_label(label)) for label in focus_fields if _normalize_match_label(label)
    ]
    if not normalized_pairs:
        return FocusFieldSelection(fields=fields, ambiguous_labels={})

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

    return FocusFieldSelection(fields=focused, ambiguous_labels=ambiguous_labels)


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
) -> tuple[list[FormField], bool]:
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
    setattr(browser_session, "_gh_page_token_costs", store)
    logger.info(
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


def _format_entry_profile_text(entry_data: dict[str, Any]) -> str:
    """Format a repeater entry into profile text for scoped LLM answer generation."""
    if not entry_data:
        return ""

    lines: list[str] = []
    used_keys: set[str] = set()
    label_map = [
        ("title", "Job Title"),
        ("company", "Company"),
        ("location", "Location"),
        ("school", "School"),
        ("degree", "Degree"),
        ("degree_type", "Degree Type"),
        ("field_of_study", "Field of Study"),
        ("major", "Major"),
        ("minor", "Minor"),
        ("honors", "Honors"),
        ("gpa", "GPA"),
        ("start_date", "Start Date"),
        ("end_date", "End Date"),
        ("end_date_type", "End Date Status"),
        ("description", "Description"),
    ]

    for key, label in label_map:
        value = _entry_text_value(entry_data.get(key))
        if key == "end_date" and value in (None, "", []):
            value = _entry_text_value(entry_data.get("graduation_date"))
        if value in (None, "", []):
            continue
        used_keys.add(key)
        lines.append(f"{label}: {value}")

    currently_work_here = entry_data.get("currently_work_here")
    if currently_work_here is None:
        currently_work_here = entry_data.get("currently_working")
    if currently_work_here is not None:
        used_keys.add("currently_work_here")
        lines.append("I currently work here: " + ("Yes" if bool(currently_work_here) else "No"))

    for key, value in entry_data.items():
        if key in used_keys or value in (None, "", []):
            continue
        text_value = _entry_text_value(value)
        if text_value is None:
            continue
        lines.append(f"{key.replace('_', ' ').title()}: {text_value}")

    return "\n".join(lines) if lines else json.dumps(entry_data, indent=2, sort_keys=True)


def _entry_text_value(value: Any) -> str | None:
    """Return deterministic text for structured education/profile values."""
    if value in (None, "", []):
        return None
    if isinstance(value, dict):
        for key in ("name", "label", "value", "title"):
            text = _entry_text_value(value.get(key))
            if text:
                return text
        return None
    if isinstance(value, set):
        parts = sorted(part for part in (_entry_text_value(item) for item in value) if part)
        return ", ".join(parts) if parts else None
    if isinstance(value, (list, tuple)):
        parts = [part for part in (_entry_text_value(item) for item in value) if part]
        return ", ".join(parts) if parts else None
    text = str(value).strip()
    return text or None


def _known_entry_value(field_name: str, entry_data: dict[str, Any] | None) -> str | None:
    """Return a scoped repeater-entry value when filling a single experience/education block."""
    if not entry_data:
        return None

    name = normalize_name(field_name)
    if not name:
        return None

    def _entry_string(key: str) -> str | None:
        value = entry_data.get(key)
        return _entry_text_value(value)

    def _entry_string_from_aliases(*keys: str) -> str | None:
        for key in keys:
            value = _entry_string(key)
            if value:
                return value
        return None

    def _entry_month_year_value(
        *,
        direct_keys: tuple[str, ...],
        pair_keys: tuple[tuple[str, str], ...],
    ) -> str | None:
        for key in direct_keys:
            value = _entry_string(key)
            if value:
                return value
        for year_key, month_key in pair_keys:
            year = _entry_string(year_key)
            month = _entry_string(month_key)
            if not year:
                continue
            if not month:
                return year
            month_digits = re.sub(r"\D+", "", month)
            if month_digits:
                return f"{year}-{int(month_digits):02d}"
            return year
        return None

    if any(kw in name for kw in ("job title", "title", "position", "role title")):
        return _entry_string("title")
    if any(kw in name for kw in ("company", "employer", "organization")):
        return _entry_string("company")
    if any(kw in name for kw in ("school", "university", "college", "institution")):
        return _entry_string_from_aliases("school", "institution")
    if any(kw in name for kw in ("degree type", "type of degree", "degree level")):
        return _entry_string_from_aliases("degree_type", "degreeType")
    if "degree" in name:
        return _entry_string_from_aliases("degree", "degree_type", "degreeType")
    if any(kw in name for kw in ("field of study", "major", "discipline", "area of study", "concentration")):
        return _entry_string_from_aliases(
            "field_of_study",
            "fieldOfStudy",
            "field",
            "major",
            "majors",
            "majorName",
            "majorNames",
            "major_name",
            "major_names",
            "area_of_study",
            "areaOfStudy",
            "discipline",
            "concentration",
            "specialization",
        )
    if any(kw in name for kw in ("minor", "minor field", "minor subject")):
        return _entry_string_from_aliases("minor", "minors", "minorName", "minorNames", "minor_name", "minor_names")
    if any(kw in name for kw in ("honors", "honours", "honour", "honor")):
        return _entry_string_from_aliases(
            "honors",
            "honours",
            "honor",
            "honorsList",
            "honoursList",
            "honors_list",
            "honours_list",
        )
    if "gpa" in name:
        return _entry_string("gpa")
    if any(kw in name for kw in ("location", "city")):
        return _entry_string("location")
    if any(kw in name for kw in ("currently work here", "currently employed", "currently working", "still employed")):
        current = entry_data.get("currently_work_here")
        if current is None:
            current = entry_data.get("currently_working")
        if current is None:
            return None
        return "checked" if bool(current) else "unchecked"
    if any(kw in name for kw in ("actual or expected", "actual/expected", "expected or actual", "expected/actual")):
        return _entry_string_from_aliases("end_date_type", "endDateType", "endDateKind")
    if name in {"from", "from date"} or any(
        kw in name for kw in ("start date", "from date", "date from", "begin date", "employment start")
    ):
        return _entry_month_year_value(
            direct_keys=("start_date", "startDate", "fromDate", "from_date"),
            pair_keys=(
                ("start_year", "start_month"),
                ("startYear", "startMonth"),
                ("from_year", "from_month"),
                ("fromYear", "fromMonth"),
            ),
        )
    if name in {"to", "to date"} or any(
        kw in name for kw in ("end date", "to date", "date to", "graduation date", "completion date")
    ):
        return _entry_month_year_value(
            direct_keys=("end_date", "endDate", "graduation_date", "graduationDate", "toDate", "to_date"),
            pair_keys=(
                ("end_year", "end_month"),
                ("endYear", "endMonth"),
                ("graduation_year", "graduation_month"),
                ("graduationYear", "graduationMonth"),
                ("to_year", "to_month"),
                ("toYear", "toMonth"),
            ),
        ) or _entry_string_from_aliases("expectedGraduation", "expected_graduation")
    if any(
        kw in name
        for kw in (
            "description",
            "summary",
            "responsibilities",
            "responsibility",
            "duties",
            "details",
            "accomplishments",
            "achievements",
        )
    ):
        return _entry_string("description")
    return None


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


def _known_entry_value_for_field(field: FormField, entry_data: dict[str, Any] | None) -> str | None:
    """Try scoped repeater entry matching against all known labels for a field."""
    for label in _field_label_candidates(field):
        value = _known_entry_value(label, entry_data)
        if value:
            return _coerce_answer_to_field(field, value)
    return _coerce_answer_to_field(field, _known_entry_value(field.name, entry_data))


def _field_section_name(field: FormField) -> str:
    return normalize_name(field.section or "")


def _is_education_like_section(value: str | None) -> bool:
    section_norm = normalize_name(value or "")
    return "education" in section_norm or any(token in section_norm for token in ("school", "university", "college"))


def _is_structured_education_field(field: FormField) -> bool:
    labels = [normalize_name(label) for label in _field_label_candidates(field)]
    section_norm = _field_section_name(field)
    if "education" not in section_norm and not any(
        token in section_norm for token in ("school", "university", "college")
    ):
        return False
    if any(label in {"month", "year"} for label in labels):
        return True
    return any(
        any(
            token in label
            for token in (
                "field of study",
                "major",
                "minor",
                "honor",
                "honour",
                "degree type",
                "discipline",
                "gpa",
                "actual or expected",
                "actual expected",
                "expected or actual",
                "from",
                "from date",
                "to",
                "to date",
                "start date",
                "end date",
                "graduation date",
                "completion date",
                "month",
                "year",
            )
        )
        for label in labels
    )


def _is_structured_education_candidate(field: FormField, visible_fields: list[FormField] | None = None) -> bool:
    if _is_structured_education_field(field):
        return True
    slot_name = _education_slot_name(field, visible_fields)
    if slot_name in {"field_of_study", "gpa", "end_date_type", "school", "degree", "degree_type", "minor", "honors"}:
        return True
    if slot_name not in {"start_date", "end_date"}:
        return False
    target_section = normalize_name(field.section or "")
    sibling_candidates = [
        candidate
        for candidate in (visible_fields or [])
        if candidate.field_id != field.field_id
        and (
            not target_section
            or not normalize_name(candidate.section or "")
            or normalize_name(candidate.section or "") == target_section
        )
    ]
    for candidate in sibling_candidates:
        if _is_structured_education_field(candidate):
            return True
        candidate_slot = _education_slot_name(candidate, visible_fields)
        if candidate_slot in {
            "school",
            "degree",
            "degree_type",
            "field_of_study",
            "minor",
            "honors",
            "gpa",
            "end_date_type",
        }:
            return True
    return False


def _language_entry_index_for_field(field: FormField) -> int | None:
    section = field.section or ""
    index = _parse_heading_index(section)
    if index is not None:
        return index - 1
    return None


def _field_binding_identity(field: FormField) -> str:
    return str(field.field_id or get_stable_field_key(field)).strip()


def _language_slot_name(field: FormField) -> str | None:
    for label in _field_label_candidates(field):
        name = normalize_name(label)
        if not name:
            continue
        if name == "language" or "preferred language" in name:
            return "language"
        if "i am fluent in this language" in name or name == "fluent" or name == "is fluent":
            return "is_fluent"
        if "reading" in name or "writing" in name:
            return "reading_writing"
        if "speaking" in name or "listening" in name:
            return "speaking_listening"
        if "overall" in name or "language proficiency" in name:
            return "overall_proficiency"
    return None


def _education_slot_name(field: FormField, visible_fields: list[FormField] | None = None) -> str | None:
    for label in _field_label_candidates(field):
        name = normalize_name(label)
        if not name:
            continue
        if any(token in name for token in ("school", "university", "college", "institution")):
            return "school"
        if any(token in name for token in ("degree type", "type of degree", "degree level")):
            return "degree_type"
        if "degree" in name:
            return "degree"
        if any(token in name for token in ("field of study", "major", "discipline", "area of study", "concentration")):
            return "field_of_study"
        if any(token in name for token in ("minor", "minor field", "minor subject")):
            return "minor"
        if any(token in name for token in ("honors", "honours", "honour", "honor")):
            return "honors"
        if "gpa" in name:
            return "gpa"
        if any(token in name for token in ("actual or expected", "actual expected", "expected or actual")):
            return "end_date_type"
        if name in {"from", "from date"} or any(token in name for token in ("start date", "date from", "begin date")):
            return "start_date"
        if name in {"to", "to date"} or any(
            token in name for token in ("end date", "date to", "graduation date", "completion date")
        ):
            return "end_date"
        if name in {"month", "year"} and visible_fields:
            target_section = normalize_name(field.section or "")
            generic_date_fields: list[FormField] = []
            for candidate in visible_fields:
                candidate_section = normalize_name(candidate.section or "")
                if target_section and candidate_section and candidate_section != target_section:
                    continue
                candidate_labels = [normalize_name(text) for text in _field_label_candidates(candidate)]
                if not any(text in {"month", "year"} for text in candidate_labels):
                    continue
                generic_date_fields.append(candidate)
            for index, candidate in enumerate(generic_date_fields):
                if candidate.field_id != field.field_id:
                    continue
                split_index = 1 if len(generic_date_fields) <= 2 else max(2, len(generic_date_fields) // 2)
                return "start_date" if index < split_index else "end_date"
    return None


def _extract_date_component(value: Any, component: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    if component == "year":
        year_match = re.search(r"\b(19|20)\d{2}\b", text)
        return year_match.group(0) if year_match else None

    normalized = text.replace("/", "-").replace(".", "-")
    parts = [part.strip() for part in normalized.split("-") if part.strip()]
    if len(parts) >= 2 and re.fullmatch(r"\d{4}", parts[0]) and re.fullmatch(r"\d{1,2}", parts[1]):
        return str(int(parts[1]))
    if len(parts) >= 2 and re.fullmatch(r"\d{1,2}", parts[0]) and re.fullmatch(r"\d{4}", parts[1]):
        return str(int(parts[0]))

    month_names = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }
    lowered = normalize_name(text)
    for token, month in month_names.items():
        if token in lowered:
            return str(month)
    return None


def _entry_text_and_source(entry: dict[str, Any], *keys: str) -> tuple[str | None, str | None]:
    for key in keys:
        value = entry.get(key)
        text = _entry_text_value(value)
        if text:
            return text, key
    return None, None


def _entry_date_text_and_source(
    entry: dict[str, Any],
    *,
    direct_keys: tuple[str, ...],
    pair_keys: tuple[tuple[str, str], ...] = (),
) -> tuple[str | None, str | None]:
    for key in direct_keys:
        value = entry.get(key)
        if isinstance(value, dict):
            year = str(value.get("year") or value.get("YYYY") or "").strip()
            month = str(value.get("month") or value.get("MM") or "").strip()
            if year and month:
                month_digits = re.sub(r"\D+", "", month)
                if month_digits:
                    return f"{year}-{int(month_digits):02d}", key
                return year, key
            if year:
                return year, key
        if value in (None, "", []):
            continue
        text = str(value).strip()
        if text:
            return text, key

    for year_key, month_key in pair_keys:
        year = str(entry.get(year_key) or "").strip()
        month = str(entry.get(month_key) or "").strip()
        if not year:
            continue
        if not month:
            return year, year_key
        month_digits = re.sub(r"\D+", "", month)
        if month_digits:
            return f"{year}-{int(month_digits):02d}", f"{year_key}+{month_key}"
        return year, year_key

    return None, None


def _structured_education_raw_value_from_entry(
    field: FormField,
    entry: dict[str, Any],
    visible_fields: list[FormField] | None = None,
) -> str | None:
    value, _ = _structured_education_raw_value_and_source_from_entry(field, entry, visible_fields)
    return value


def _structured_education_raw_value_and_source_from_entry(
    field: FormField,
    entry: dict[str, Any],
    visible_fields: list[FormField] | None = None,
) -> tuple[str | None, str | None]:
    slot_name = _education_slot_name(field, visible_fields)
    if not slot_name:
        return None, None

    label_norm = normalize_name(_preferred_field_label(field))
    if slot_name == "school":
        return _entry_text_and_source(entry, "school", "institution")
    if slot_name == "degree":
        return _entry_text_and_source(entry, "degree", "degreeType", "degree_type")
    if slot_name == "degree_type":
        return _entry_text_and_source(entry, "degree_type", "degreeType")
    if slot_name == "field_of_study":
        return _entry_text_and_source(
            entry,
            "field_of_study",
            "fieldOfStudy",
            "field",
            "major",
            "majors",
            "majorName",
            "majorNames",
            "major_name",
            "major_names",
            "area_of_study",
            "areaOfStudy",
            "discipline",
            "concentration",
            "specialization",
        )
    if slot_name == "minor":
        return _entry_text_and_source(entry, "minor", "minors", "minorName", "minorNames", "minor_name", "minor_names")
    if slot_name == "honors":
        return _entry_text_and_source(
            entry,
            "honors",
            "honours",
            "honor",
            "honorsList",
            "honoursList",
            "honors_list",
            "honours_list",
        )
    if slot_name == "gpa":
        return _entry_text_and_source(entry, "gpa")
    if slot_name == "end_date_type":
        return _entry_text_and_source(entry, "end_date_type", "endDateType", "endDateKind")

    if slot_name == "start_date":
        raw_value, source_key = _entry_date_text_and_source(
            entry,
            direct_keys=("start_date", "startDate", "fromDate", "from_date"),
            pair_keys=(
                ("start_year", "start_month"),
                ("startYear", "startMonth"),
                ("from_year", "from_month"),
                ("fromYear", "fromMonth"),
            ),
        )
    else:
        raw_value, source_key = _entry_date_text_and_source(
            entry,
            direct_keys=("end_date", "endDate", "graduation_date", "graduationDate", "toDate", "to_date"),
            pair_keys=(
                ("end_year", "end_month"),
                ("endYear", "endMonth"),
                ("graduation_year", "graduation_month"),
                ("graduationYear", "graduationMonth"),
                ("to_year", "to_month"),
                ("toYear", "toMonth"),
            ),
        )
        if not raw_value:
            raw_value, source_key = _entry_text_and_source(entry, "expectedGraduation", "expected_graduation")

    if label_norm == "year":
        return _extract_date_component(raw_value, "year"), source_key
    if label_norm == "month":
        return _extract_date_component(raw_value, "month"), source_key
    text = str(raw_value or "").strip()
    return (text or None), source_key


def _structured_education_value_from_entry(
    field: FormField,
    entry: dict[str, Any],
    visible_fields: list[FormField] | None = None,
) -> str | None:
    return _coerce_answer_to_field(
        field,
        _structured_education_raw_value_and_source_from_entry(field, entry, visible_fields)[0],
    )


def _match_entry_by_slot_value(
    *,
    field: FormField,
    slot_name: str,
    entries: list[dict[str, Any]],
    current_value: str,
) -> int | None:
    current_text = str(current_value or "").strip()
    if not current_text:
        return None
    matched: list[int] = []
    for index, entry in enumerate(entries):
        if slot_name == "language":
            expected = str(entry.get("language") or "").strip()
        elif slot_name == "is_fluent":
            bool_value = entry.get("isFluent")
            if not isinstance(bool_value, bool):
                bool_value = entry.get("is_fluent")
            expected = "Yes" if bool_value is True else "No" if bool_value is False else ""
        elif slot_name == "reading_writing":
            expected = str(entry.get("readingWriting") or entry.get("reading_writing") or "").strip()
        elif slot_name == "speaking_listening":
            expected = str(entry.get("speakingListening") or entry.get("speaking_listening") or "").strip()
        elif slot_name == "overall_proficiency":
            expected = str(entry.get("overallProficiency") or entry.get("overall_proficiency") or "").strip()
        elif slot_name == "school":
            expected = str(entry.get("school") or "").strip()
        elif slot_name == "degree":
            expected = _entry_text_value(entry.get("degree")) or ""
        elif slot_name == "degree_type":
            expected = _entry_text_value(entry.get("degree_type") or entry.get("degreeType")) or ""
        elif slot_name == "field_of_study":
            expected = (
                _entry_text_value(
                    entry.get("field_of_study")
                    or entry.get("fieldOfStudy")
                    or entry.get("field")
                    or entry.get("major")
                    or entry.get("majors")
                    or entry.get("majorName")
                    or entry.get("majorNames")
                    or entry.get("major_name")
                    or entry.get("major_names")
                    or entry.get("area_of_study")
                    or entry.get("areaOfStudy")
                    or entry.get("discipline")
                    or entry.get("concentration")
                )
                or ""
            )
        elif slot_name == "minor":
            expected = (
                _entry_text_value(
                    entry.get("minor")
                    or entry.get("minors")
                    or entry.get("minorName")
                    or entry.get("minorNames")
                    or entry.get("minor_name")
                    or entry.get("minor_names")
                )
                or ""
            )
        elif slot_name == "honors":
            expected = (
                _entry_text_value(
                    entry.get("honors")
                    or entry.get("honours")
                    or entry.get("honor")
                    or entry.get("honorsList")
                    or entry.get("honoursList")
                    or entry.get("honors_list")
                    or entry.get("honours_list")
                )
                or ""
            )
        elif slot_name == "gpa":
            expected = _entry_text_value(entry.get("gpa")) or ""
        elif slot_name == "start_date":
            expected, _ = _entry_date_text_and_source(
                entry,
                direct_keys=("start_date", "startDate", "fromDate", "from_date"),
                pair_keys=(
                    ("start_year", "start_month"),
                    ("startYear", "startMonth"),
                    ("from_year", "from_month"),
                    ("fromYear", "fromMonth"),
                ),
            )
            expected = str(expected or "").strip()
        elif slot_name == "end_date":
            expected, _ = _entry_date_text_and_source(
                entry,
                direct_keys=("end_date", "endDate", "graduation_date", "graduationDate", "toDate", "to_date"),
                pair_keys=(
                    ("end_year", "end_month"),
                    ("endYear", "endMonth"),
                    ("graduation_year", "graduation_month"),
                    ("graduationYear", "graduationMonth"),
                    ("to_year", "to_month"),
                    ("toYear", "toMonth"),
                ),
            )
            if not expected:
                expected = str(entry.get("expectedGraduation") or entry.get("expected_graduation") or "").strip()
        elif slot_name == "end_date_type":
            expected = str(
                entry.get("end_date_type") or entry.get("endDateKind") or entry.get("endDateType") or ""
            ).strip()
        else:
            expected = ""
        if expected and _field_value_matches_expected(current_text, expected):
            matched.append(index)
    return matched[0] if len(matched) == 1 else None


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


def _resolve_repeater_binding(
    *,
    host: str,
    repeater_group: str,
    field: FormField,
    visible_fields: list[FormField],
    entries: list[dict[str, Any]],
    numeric_index: int | None,
    slot_name: str | None,
    current_value: str,
    slot_resolver,
) -> ResolvedFieldBinding | None:
    if not entries:
        return None
    if len(entries) == 1:
        return ResolvedFieldBinding(
            entry_index=0,
            binding_mode="exact",
            binding_confidence="high",
            best_effort_guess=False,
        )

    field_binding_key = _field_binding_identity(field)
    cached = get_repeater_field_binding(
        host=host,
        repeater_group=repeater_group,
        field_binding_key=field_binding_key,
    )
    if cached and 0 <= cached.entry_index < len(entries):
        return ResolvedFieldBinding(
            entry_index=cached.entry_index,
            binding_mode=cached.binding_mode,
            binding_confidence=cached.binding_confidence,
            best_effort_guess=cached.best_effort_guess,
        )

    if numeric_index is not None and 0 <= numeric_index < len(entries):
        binding = ResolvedFieldBinding(
            entry_index=numeric_index,
            binding_mode="exact",
            binding_confidence="high",
            best_effort_guess=False,
        )
        record_repeater_field_binding(
            host=host,
            repeater_group=repeater_group,
            field_binding_key=field_binding_key,
            entry_index=binding.entry_index,
            binding_mode="exact",
            binding_confidence="high",
            best_effort_guess=False,
        )
        return binding

    section_hint = normalize_name(field.section or "")
    if section_hint:
        section_matches: list[int] = []
        for index, entry in enumerate(entries):
            candidate_texts: list[str] = []
            if repeater_group == "languages":
                candidate_texts.append(str(entry.get("language") or "").strip())
            elif repeater_group == "education":
                candidate_texts.extend(
                    [
                        _entry_text_value(entry.get("school")) or "",
                        _entry_text_value(entry.get("degree")) or "",
                        _entry_text_value(
                            entry.get("field_of_study")
                            or entry.get("field")
                            or entry.get("major")
                            or entry.get("majors")
                            or entry.get("majorName")
                            or entry.get("majorNames")
                            or entry.get("major_name")
                            or entry.get("major_names")
                        )
                        or "",
                        _entry_text_value(entry.get("degree_type") or entry.get("degreeType")) or "",
                        _entry_text_value(
                            entry.get("minor")
                            or entry.get("minors")
                            or entry.get("minorName")
                            or entry.get("minor_name")
                            or entry.get("minor_names")
                        )
                        or "",
                        _entry_text_value(
                            entry.get("honors")
                            or entry.get("honours")
                            or entry.get("honor")
                            or entry.get("honorsList")
                            or entry.get("honoursList")
                            or entry.get("honors_list")
                            or entry.get("honours_list")
                        )
                        or "",
                    ]
                )
            if any(text and normalize_name(text) and normalize_name(text) in section_hint for text in candidate_texts):
                section_matches.append(index)
        if len(section_matches) == 1:
            binding = ResolvedFieldBinding(
                entry_index=section_matches[0],
                binding_mode="similarity",
                binding_confidence="medium",
                best_effort_guess=False,
            )
            record_repeater_field_binding(
                host=host,
                repeater_group=repeater_group,
                field_binding_key=field_binding_key,
                entry_index=binding.entry_index,
                binding_mode="similarity",
                binding_confidence="medium",
                best_effort_guess=False,
            )
            return binding

    if slot_name:
        similarity_index = _match_entry_by_slot_value(
            field=field,
            slot_name=slot_name,
            entries=entries,
            current_value=current_value,
        )
        if similarity_index is not None:
            binding = ResolvedFieldBinding(
                entry_index=similarity_index,
                binding_mode="similarity",
                binding_confidence="medium",
                best_effort_guess=False,
            )
            record_repeater_field_binding(
                host=host,
                repeater_group=repeater_group,
                field_binding_key=field_binding_key,
                entry_index=binding.entry_index,
                binding_mode="similarity",
                binding_confidence="medium",
                best_effort_guess=False,
            )
            return binding

        row_order_index = _row_order_binding_index(
            field=field,
            visible_fields=visible_fields,
            slot_name=slot_name,
            slot_resolver=slot_resolver,
        )
        if row_order_index is not None and 0 <= row_order_index < len(entries):
            binding = ResolvedFieldBinding(
                entry_index=row_order_index,
                binding_mode="row_order",
                binding_confidence="low",
                best_effort_guess=True,
            )
            record_repeater_field_binding(
                host=host,
                repeater_group=repeater_group,
                field_binding_key=field_binding_key,
                entry_index=binding.entry_index,
                binding_mode="row_order",
                binding_confidence="low",
                best_effort_guess=True,
            )
            return binding

    return None


def _structured_language_raw_value_from_entry(field: FormField, entry: dict[str, Any]) -> str | None:
    value, _ = _structured_language_raw_value_and_source_from_entry(field, entry)
    return value


def _structured_language_raw_value_and_source_from_entry(
    field: FormField,
    entry: dict[str, Any],
) -> tuple[str | None, str | None]:
    for label in _field_label_candidates(field):
        name = normalize_name(label)
        if not name:
            continue
        if name == "language" or "preferred language" in name:
            return _entry_text_and_source(entry, "language")
        if "i am fluent in this language" in name or name == "fluent" or name == "is fluent":
            if isinstance(entry.get("isFluent"), bool):
                return ("Yes" if bool(entry.get("isFluent")) else "No"), "isFluent"
            if isinstance(entry.get("is_fluent"), bool):
                return ("Yes" if bool(entry.get("is_fluent")) else "No"), "is_fluent"
        if "reading" in name or "writing" in name:
            return _entry_text_and_source(entry, "readingWriting", "reading_writing")
        if "speaking" in name or "listening" in name:
            return _entry_text_and_source(entry, "speakingListening", "speaking_listening")
        if "overall" in name or "language proficiency" in name:
            return _entry_text_and_source(entry, "overallProficiency", "overall_proficiency")
    return None, None


def _structured_language_value_from_entry(field: FormField, entry: dict[str, Any]) -> str | None:
    return _coerce_answer_to_field(field, _structured_language_raw_value_and_source_from_entry(field, entry)[0])


def _resolve_structured_language_value(
    field: FormField,
    profile_data: dict[str, Any] | None,
) -> str | None:
    if not profile_data:
        return None
    raw_languages = profile_data.get("languages")
    if not isinstance(raw_languages, list) or not raw_languages:
        return None
    index = _language_entry_index_for_field(field)
    if index is None or not (0 <= index < len(raw_languages)):
        return None
    entry = raw_languages[index]
    if not isinstance(entry, dict):
        return None
    return _structured_language_value_from_entry(field, entry)


def _is_structured_language_field(field: FormField) -> bool:
    section_norm = _field_section_name(field)
    if "language" not in section_norm:
        return False
    labels = [normalize_name(label) for label in _field_label_candidates(field)]
    return any(
        any(
            token in label
            for token in (
                "language",
                "i am fluent in this language",
                "reading",
                "writing",
                "speaking",
                "listening",
                "overall",
                "language proficiency",
            )
        )
        for label in labels
    )


def _structured_field_missing_reason(field: FormField) -> str:
    label = _preferred_field_label(field)
    return f"Structured profile data is required for {label} and no exact value was available."


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


def _infer_entry_data_from_scope(
    profile_data: dict[str, Any],
    heading_boundary: str | None,
    target_section: str | None,
) -> dict[str, Any] | None:
    """Infer repeater entry data from the full profile when entry_data is omitted."""
    if not profile_data:
        return None

    scope_norm = normalize_name(heading_boundary or target_section or "")
    if not scope_norm:
        return None

    if "education" in scope_norm:
        entries = profile_data.get("education")
    elif any(token in scope_norm for token in ("work experience", "experience", "employment")):
        entries = profile_data.get("experience")
    else:
        return None

    if not isinstance(entries, list) or len(entries) == 0:
        return None
    indexed_heading = _parse_heading_index(heading_boundary or target_section)
    if indexed_heading is not None:
        entry_index = indexed_heading - 1
    elif len(entries) == 1:
        entry_index = 0
    else:
        return None
    if not (0 <= entry_index < len(entries)):
        return None
    entry = entries[entry_index]
    return entry if isinstance(entry, dict) and entry else None


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


def _get_nested_profile_value(profile_data: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    """Return the first nested profile value found across the candidate paths."""
    for path in paths:
        current: Any = profile_data
        found = True
        for key in path:
            if not isinstance(current, dict) or key not in current:
                found = False
                break
            current = current[key]
        if found:
            return current
    return None


def _normalize_qa_text(text: str) -> str:
    """Normalize question text for fuzzy Q&A matching."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)  # Remove punctuation
    text = re.sub(r"\s+", " ", text)  # Collapse whitespace
    return text


def _cap_qa_entries(
    qa_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cap Q&A bank at MAX_QA_ENTRIES entries, prioritized by usage mode,
    times_used descending, then confidence (exact > inferred > learned)."""
    if len(qa_entries) <= MAX_QA_ENTRIES:
        return qa_entries
    qa_entries.sort(
        key=lambda e: (
            e.get("usageMode", e.get("usage_mode")) != "always_use",  # always_use first
            -int(e.get("timesUsed", e.get("times_used", 0)) or 0),  # most used first
            _QA_CONFIDENCE_RANKS.get(e.get("confidence", "learned"), 3),  # exact > inferred > learned
        )
    )
    return qa_entries[:MAX_QA_ENTRIES]


def _match_qa_answer(field_label: str, qa_entries: list[dict[str, Any]]) -> str | None:
    """Try to match a form field label against Q&A bank entries with fuzzy matching.

    Checks exact normalized match first, then synonym-based matching.
    Returns the answer string or None if no match found.
    """
    if not qa_entries or not field_label:
        return None

    normalized_label = _normalize_qa_text(field_label)
    if not normalized_label:
        return None

    for entry in qa_entries:
        stored_q = _normalize_qa_text(entry.get("question", ""))
        if not stored_q:
            continue

        # Exact normalized match
        if normalized_label == stored_q:
            answer = entry.get("answer", "")
            if answer:
                return answer

        # Substring containment (short label inside long stored question or vice versa)
        shorter = min(normalized_label, stored_q, key=len)
        if len(shorter) >= 8 and (shorter in normalized_label and shorter in stored_q):
            answer = entry.get("answer", "")
            if answer:
                return answer

    # Synonym-based matching: both the field label and stored question
    # must match the same canonical synonym group.
    for canonical, synonyms in _QA_QUESTION_SYNONYMS.items():
        all_variants = [canonical, *synonyms]
        label_matches = any(v in normalized_label for v in all_variants)
        if not label_matches:
            continue
        for entry in qa_entries:
            stored_q = _normalize_qa_text(entry.get("question", ""))
            if not stored_q:
                continue
            stored_matches = any(v in stored_q for v in all_variants)
            if stored_matches:
                answer = entry.get("answer", "")
                if answer:
                    return answer

    return None


def _build_profile_answer_map(
    profile_data: dict[str, Any],
    evidence: dict[str, str | None],
) -> dict[str, str]:
    """Build a generic question/answer map from structured profile data."""
    canonical = build_canonical_profile(profile_data, evidence)

    answer_map: dict[str, str] = {}

    def add(value: Any, *labels: str) -> None:
        text = _normalize_bool_text(value)
        if text is None:
            return
        for label in labels:
            answer_map[label] = text

    add(
        canonical.get("gender"),
        "Gender",
        "Please select your gender.",
    )
    add(
        canonical.get("race_ethnicity"),
        "Race/Ethnicity",
        "Race",
        "Ethnicity",
        "Please select the ethnicity (or ethnicities) which most accurately describe(s) how you identify yourself.",
    )
    add(
        canonical.get("veteran_status"),
        "Veteran Status",
        "Are you a protected veteran",
        "Please select the veteran status which most accurately describes your status.",
    )
    add(
        canonical.get("disability_status"),
        "Disability",
        "Disability Status",
        "Please indicate if you have a disability",
        "Please check one of the boxes below:",
    )
    add(canonical.get("country"), "Country", "Country/Territory", "Country/Region")
    add(canonical.get("phone_device_type"), "Phone Device Type", "Phone Type")
    add(canonical.get("phone_country_code"), "Country Phone Code", "Phone Country Code")
    add(
        canonical.get("full_name"),
        "Please enter your name",
        "Enter your name",
        "Your name",
        "Full name",
        "Name",
        "Signature",
    )
    add(canonical.get("preferred_name"), "Preferred name", "Nickname")
    add(
        canonical.get("address"),
        "Address",
        "Address Line 1",
        "Address 1",
        "Street",
        "Street Address",
        "Street Line 1",
        "Mailing Address",
    )
    add(
        canonical.get("address_line_2"),
        "Address Line 2",
        "Address 2",
        "Apartment / Unit",
        "Apartment",
        "Suite / Apartment",
        "Suite",
        "Unit",
        "Street Line 2",
        "Mailing Address Line 2",
    )
    add(canonical.get("city"), "City", "Town")
    add(canonical.get("state"), "State", "State/Province", "State / Province", "Province", "Region")
    add(canonical.get("postal_code"), "Postal Code", "Postal/Zip Code", "ZIP", "ZIP Code", "Zip/Postal Code")
    add(canonical.get("county"), "County", "County / Parish / Borough", "Parish", "Borough")
    # Compose location from city + state for combined location fields
    _city = canonical.get("city") or ""
    _state = canonical.get("state") or ""
    _location_val = canonical.get("location") or (f"{_city}, {_state}" if _city and _state else _city or _state)
    if _location_val:
        add(_location_val, "Location", "City/State", "City, State")
    add(
        canonical.get("how_did_you_hear"),
        "How Did You Hear About Us?",
        "How did you hear about this position?",
        "How did you learn about us?",
        "Referral Source",
        "Source",
        "Source of Referral",
    )
    add(canonical.get("linkedin"), "LinkedIn", "LinkedIn URL", "LinkedIn Profile")
    add(
        canonical.get("spoken_languages"),
        "Languages spoken",
        "Spoken languages",
        "Preferred spoken languages",
        "Preferred language",
    )
    add(
        canonical.get("english_proficiency"),
        "English proficiency",
        "English language proficiency",
    )
    add(
        canonical.get("country_of_residence"),
        "Country of residence",
        "Country/Region",
        "Country region",
    )
    add(
        canonical.get("preferred_work_mode"),
        "Preferred work setup",
        "Preferred work arrangement",
        "Work arrangement",
        "Remote/Hybrid preference",
    )
    add(
        canonical.get("preferred_locations"),
        "Preferred locations",
        "Preferred work locations",
        "Preferred city",
        "Preferred office location",
    )
    add(
        canonical.get("availability_window"),
        "Availability to start",
        "Earliest start date",
        "Available start date",
        "Start date",
    )
    add(canonical.get("notice_period"), "Notice period")
    add(
        canonical.get("salary_expectation"),
        "Expected annual salary",
        "Expected salary range",
        "Expected compensation",
        "Compensation expectation",
        "Hourly compensation requirements",
        "Hourly compensation",
    )
    add(
        canonical.get("current_school_year"),
        "Current year in school",
        "What is your current year in school?",
        "School year",
        "Academic standing",
        "Class standing",
    )
    add(
        canonical.get("graduation_date"),
        "Estimated graduation date",
        "Expected graduation date",
        "Graduation date",
        "Already graduated date",
        "Degree completion date",
    )
    add(
        canonical.get("degree_seeking"),
        "What degree are you seeking?",
        "Degree seeking",
        "Degree sought",
        "Degree pursuing",
    )
    add(
        canonical.get("degree_type"),
        "Degree Type",
        "Type of Degree",
        "Degree level",
    )
    add(
        canonical.get("field_of_study"),
        "Field of study",
        "Area of study",
        "Major",
        "Majors",
        "Major(s)",
        "Please list your area of study (major).",
    )
    add(
        canonical.get("minor"),
        "Minor",
        "Minors",
        "Minor(s)",
    )
    add(
        canonical.get("honors"),
        "Honors",
        "Honours",
        "Honors / Awards",
    )
    add(
        canonical.get("certifications_licenses", allow_policy=True) or "None",
        "Certifications or licenses",
        "Relevant certifications or licenses",
        "Relevant certifications",
        "Licenses",
    )
    add(
        canonical.get("portfolio"),
        "Website",
        "Website URL",
        "Portfolio",
        "Portfolio URL",
        "Personal Website",
        "Personal Site",
        "Blog",
    )
    add(canonical.get("github"), "GitHub", "GitHub URL", "GitHub Profile")
    add(canonical.get("work_authorization"), "Work Authorization")
    add(canonical.get("willing_to_relocate"), "Willing to relocate", "Relocation")
    add(
        canonical.get("sponsorship_needed"),
        "Visa Sponsorship",
        "Sponsorship needed",
        "Require sponsorship",
        "Need sponsorship",
        "Will you now or in the future require visa sponsorship by an employer?",
    )
    add(
        canonical.get("authorized_to_work"),
        "Authorized to work",
        "Legally authorized to work",
        "Are you legally permitted to work in the country where this job is located?",
        "Are you legally authorized to work in the country in which this job is located?",
    )

    age_value = profile_data.get("age")
    if age_value not in (None, ""):
        try:
            if int(str(age_value).strip()) >= 18:
                add("Yes", "Are you at least 18 years old?", "Are you 18 years of age or older?")
        except ValueError:
            pass

    skills = _profile_skill_values(profile_data)
    if skills:
        add(
            ", ".join(skills),
            "Skills",
            "Skill",
            "Technical skills",
            "Technologies",
            "Type to Add Skills",
        )

    # ── Inject Q&A bank entries into the answer map ──────────────────
    # The answer bank arrives from Desktop/VALET as profile_data["answerBank"]
    # or profile_data["answer_bank"]. Each entry has {question, answer,
    # intentTag?, usageMode?}. Cap at MAX_QA_ENTRIES and add each entry's
    # question as a key in the answer map (profile values take precedence).
    raw_bank = profile_data.get("answerBank") or profile_data.get("answer_bank")
    if isinstance(raw_bank, list) and raw_bank:
        capped = _cap_qa_entries(list(raw_bank))
        for entry in capped:
            if not isinstance(entry, dict):
                continue
            question = str(entry.get("question", "")).strip()
            answer = str(entry.get("answer", "")).strip()
            if question and answer and question not in answer_map:
                answer_map[question] = answer

    return answer_map


def _find_best_profile_answer(
    label: str,
    answer_map: dict[str, str],
    minimum_confidence: str = "medium",
) -> str | None:
    """Find the closest structured-profile answer for a field label."""
    if not label or not answer_map:
        return None

    best_answer: str | None = None
    best_rank = 0
    for question, answer in answer_map.items():
        confidence = _label_match_confidence(label, question)
        if not _meets_match_confidence(confidence, minimum_confidence):
            continue
        rank = _MATCH_CONFIDENCE_RANKS.get(confidence or "", 0)
        if rank > best_rank:
            best_rank = rank
            best_answer = answer

    if best_answer is None:
        return None
    return best_answer


def _is_employer_history_screening_question(norm: str) -> bool:
    """Return True for low-risk yes/no questions about prior employment with an employer."""
    direct_phrases = (
        "previously worked",
        "previously employed",
        "worked for this organization",
        "worked for this company",
        "worked here before",
        "prior employment",
        "prior employer",
        "former employee",
        "current or previous employee",
        "current or former employee",
        "previous employee",
        "government employee",
        "government employment",
        "worked for the government",
        "worked in government",
    )
    if any(phrase in norm for phrase in direct_phrases):
        return True

    if any(
        token in norm
        for token in (
            "years",
            "year",
            "months",
            "month",
            "experience",
            "experienced",
            "skill",
            "skills",
            "technology",
            "technologies",
        )
    ):
        return False

    employer_question_prefixes = (
        "have you worked at ",
        "have you ever worked at ",
        "have you previously worked at ",
        "have you worked for ",
        "have you ever worked for ",
        "have you previously worked for ",
        "have you been employed by ",
        "have you ever been employed by ",
        "have you previously been employed by ",
        "were you previously employed by ",
        "are you a current or former employee",
        "are you a current or previous employee",
        "are you a former employee",
    )
    return any(norm.startswith(prefix) for prefix in employer_question_prefixes)


def _extract_named_employer_from_question(norm: str) -> str | None:
    """Extract a concrete employer name from a screening question when present."""
    patterns = (
        r"have you (?:ever |previously )?worked (?:at|for) (?P<employer>.+)",
        r"have you (?:ever |previously )?been employed by (?P<employer>.+)",
        r"were you previously employed by (?P<employer>.+)",
        r"are you a current or former employee of (?P<employer>.+)",
        r"are you a current or previous employee of (?P<employer>.+)",
        r"are you a former employee of (?P<employer>.+)",
    )
    for pattern in patterns:
        match = re.match(pattern, norm)
        if not match:
            continue
        employer = re.sub(
            r"\b(before|previously|currently|now|today|already|or its subsidiaries|or its affiliates)\b$",
            "",
            match.group("employer").strip(),
        ).strip(" ?.:;!,")
        if employer in {
            "",
            "this company",
            "this organization",
            "this employer",
            "the company",
            "the organization",
            "the employer",
            "here",
        }:
            return None
        return employer
    return None


def _normalized_employer_tokens(text: str) -> set[str]:
    """Normalize an employer name into comparable tokens."""
    ignored = {
        "the",
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "lp",
        "llp",
        "gmbh",
        "ag",
        "sa",
        "pte",
        "pty",
    }
    return {token for token in normalize_name(text).split() if token and token not in ignored}


def _profile_has_employer_history(profile_data: dict[str, Any], employer_name: str) -> bool:
    """Return True when the named employer appears in the applicant's work history."""
    target_tokens = _normalized_employer_tokens(employer_name)
    if not target_tokens:
        return False

    employers: list[str] = []
    experience_entries = profile_data.get("experience")
    if isinstance(experience_entries, list):
        for entry in experience_entries:
            if not isinstance(entry, dict):
                continue
            for key in ("company", "employer", "organization", "company_name"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    employers.append(value.strip())

    for key in ("current_company", "company", "employer", "organization"):
        value = profile_data.get(key)
        if isinstance(value, str) and value.strip():
            employers.append(value.strip())

    for employer in employers:
        employer_tokens = _normalized_employer_tokens(employer)
        if employer_tokens and (target_tokens <= employer_tokens or employer_tokens <= target_tokens):
            return True
    return False


def _default_screening_answer(field: FormField, profile_data: dict[str, Any]) -> str | None:
    """Return an answer only when the profile explicitly supports it."""
    label = _preferred_field_label(field)
    norm = normalize_name(label)
    options = [normalize_name(choice) for choice in (field.options or field.choices or [])]
    if options and not ({"yes", "no"} & set(options)):
        return None

    named_employer = _extract_named_employer_from_question(norm)
    if named_employer:
        answer = "Yes" if _profile_has_employer_history(profile_data, named_employer) else "No"
        return _coerce_answer_to_field(field, answer)

    if _is_employer_history_screening_question(norm):
        return _coerce_answer_to_field(field, "No")

    sponsorship_value = profile_data.get("sponsorship_needed")
    if sponsorship_value is None:
        sponsorship_value = profile_data.get("visa_sponsorship")
    if sponsorship_value is not None and any(
        phrase in norm for phrase in ("sponsorship", "visa sponsorship", "require sponsorship", "need sponsorship")
    ):
        return _coerce_answer_to_field(field, _normalize_bool_text(sponsorship_value))

    authorized_value = profile_data.get("authorized_to_work")
    if authorized_value is None:
        authorized_value = profile_data.get("US_citizen")
    if authorized_value is not None and any(
        phrase in norm
        for phrase in ("authorized to work", "legally authorized", "legally permitted to work", "eligible to work")
    ):
        return _coerce_answer_to_field(field, _normalize_bool_text(authorized_value))

    age_value = profile_data.get("age")
    if age_value not in (None, "") and any(phrase in norm for phrase in ("at least 18", "18 years of age or older")):
        try:
            return _coerce_answer_to_field(field, "Yes" if int(str(age_value).strip()) >= 18 else "No")
        except ValueError:
            return None

    return None


_SEMANTIC_INTENT_DESCRIPTIONS: dict[SemanticQuestionIntent, str] = {
    "work_authorization": "Legal authorization or eligibility to work.",
    "visa_sponsorship": "Need for current or future visa / immigration sponsorship.",
    "how_did_you_hear": "Referral source or how the applicant heard about the role/company.",
    "willing_to_relocate": "Willingness or openness to relocate or move.",
    "salary_expectation": "Expected compensation or salary expectation.",
    "current_school_year": "Current year in school or class standing.",
    "graduation_date": "Estimated, expected, or completed graduation date.",
    "degree_seeking": "Degree sought or currently being pursued.",
    "certifications_licenses": "Relevant certifications or licenses held by the applicant.",
    "spoken_languages": "Languages the applicant speaks or prefers.",
    "english_proficiency": "English or language rubric proficiency such as overall/reading/writing/speaking.",
    "country_of_residence": "Current country or region of residence.",
    "preferred_work_mode": "Remote, hybrid, onsite, or work arrangement preference.",
    "preferred_locations": "Preferred city, office, or work location.",
    "availability_window": "Availability or earliest possible start window/date.",
    "notice_period": "Notice period before the applicant can start.",
    "gender": "Gender self-identification question.",
    "race_ethnicity": "Race or ethnicity self-identification question.",
    "veteran_status": "Veteran self-identification question.",
    "disability_status": "Disability self-identification question.",
    "employer_history": "Whether the applicant previously worked for the named employer or a government organization.",
}


def _resolve_semantic_intent_answer(
    field: FormField,
    intent: SemanticQuestionIntent,
    profile_data: dict[str, Any] | None,
    evidence: dict[str, str | None],
) -> str | None:
    """Resolve a classified semantic intent into an explicit saved answer."""
    canonical = build_canonical_profile(profile_data or {}, evidence)

    if intent == "work_authorization":
        return _coerce_answer_to_field(
            field,
            canonical.get("work_authorization") or canonical.get("authorized_to_work"),
        )
    if intent == "visa_sponsorship":
        return _coerce_answer_to_field(
            field,
            canonical.get("sponsorship_needed"),
        )
    if intent == "how_did_you_hear":
        return _coerce_answer_to_field(field, canonical.get("how_did_you_hear"))
    if intent == "willing_to_relocate":
        return _coerce_answer_to_field(field, canonical.get("willing_to_relocate"))
    if intent == "salary_expectation":
        return _coerce_answer_to_field(field, canonical.get("salary_expectation"))
    if intent == "current_school_year":
        return _coerce_answer_to_field(field, canonical.get("current_school_year"))
    if intent == "graduation_date":
        return _coerce_answer_to_field(field, canonical.get("graduation_date"))
    if intent == "degree_seeking":
        return _coerce_answer_to_field(field, canonical.get("degree_seeking"))
    if intent == "certifications_licenses":
        return _coerce_answer_to_field(
            field,
            canonical.get("certifications_licenses", allow_policy=True) or "None",
        )
    if intent == "spoken_languages":
        return _coerce_answer_to_field(field, canonical.get("spoken_languages"))
    if intent == "english_proficiency":
        return _coerce_answer_to_field(field, canonical.get("english_proficiency"))
    if intent == "country_of_residence":
        return _coerce_answer_to_field(
            field,
            canonical.get("country_of_residence") or canonical.get("country"),
        )
    if intent == "preferred_work_mode":
        return _coerce_answer_to_field(field, canonical.get("preferred_work_mode"))
    if intent == "preferred_locations":
        return _coerce_answer_to_field(field, canonical.get("preferred_locations"))
    if intent == "availability_window":
        return _coerce_answer_to_field(
            field,
            canonical.get("availability_window") or canonical.get("available_start_date"),
        )
    if intent == "notice_period":
        return _coerce_answer_to_field(field, canonical.get("notice_period"))
    if intent == "gender":
        return _coerce_answer_to_field(field, canonical.get("gender"))
    if intent == "race_ethnicity":
        return _coerce_answer_to_field(field, canonical.get("race_ethnicity"))
    if intent == "veteran_status":
        return _coerce_answer_to_field(field, canonical.get("veteran_status"))
    if intent == "disability_status":
        return _coerce_answer_to_field(field, canonical.get("disability_status"))
    if intent == "employer_history":
        return _default_screening_answer(field, profile_data or {})
    return None


def _available_semantic_intent_answers(
    field: FormField,
    profile_data: dict[str, Any] | None,
    evidence: dict[str, str | None],
) -> dict[SemanticQuestionIntent, str]:
    """Return the known semantic intents that have an explicit answer right now."""
    available: dict[SemanticQuestionIntent, str] = {}
    for intent in _SEMANTIC_INTENT_DESCRIPTIONS:
        answer = _resolve_semantic_intent_answer(field, intent, profile_data, evidence)
        if answer:
            available[intent] = answer
    return available


def _field_option_norms(field: FormField) -> set[str]:
    return {normalize_name(str(option)) for option in (field.options or field.choices or []) if str(option).strip()}


_BOOLEAN_QUESTION_PREFIXES = (
    "are you",
    "do you",
    "did you",
    "have you",
    "has the applicant",
    "will you",
    "would you",
    "can you",
    "is the applicant",
)

_DETAIL_CHILD_PROMPT_FRAGMENTS = (
    "please specify",
    "type of visa",
    "what visa",
    "which visa",
    "location to which",
    "where would you relocate",
    "preferred location",
    "which most accurately fits",
    "answer below",
    "choose the answer below",
)


def _field_widget_kind_for_debug(field: FormField) -> str:
    if field.field_type == "select":
        return "native_select" if field.is_native else "custom_widget"
    if field.field_type in {"radio", "radio-group"}:
        return "radio_group"
    if field.field_type in {"checkbox", "checkbox-group"}:
        return "checkbox_group"
    if field.field_type == "button-group":
        return "button_group"
    if field.field_type == "textarea":
        return "textarea"
    return "text_input" if field.is_native else "custom_widget"


def _label_starts_boolean_question(label: str) -> bool:
    return any(label.startswith(prefix) for prefix in _BOOLEAN_QUESTION_PREFIXES)


def _is_detail_child_prompt(label: str) -> bool:
    return any(fragment in label for fragment in _DETAIL_CHILD_PROMPT_FRAGMENTS)


def _is_employer_history_boolean_prompt(label: str) -> bool:
    return _is_employer_history_screening_question(label)


def _log_boolean_widget_classification(field: FormField, cluster: str | None, role: str | None) -> None:
    if cluster not in {"relocation", "visa_sponsorship", "work_authorization", "employer_history"}:
        return
    logger.info(
        "domhand.boolean_widget_classification",
        extra={
            "field_id": field.field_id,
            "field_label": _preferred_field_label(field),
            "field_type": field.field_type,
            "widget_kind": _field_widget_kind_for_debug(field),
            "visible_options_count": len(field.options or field.choices or []),
            "cluster": cluster,
            "inferred_role": role or "",
        },
    )


def _field_conditional_cluster(field: FormField) -> tuple[str | None, str | None]:
    label = normalize_name(_preferred_field_label(field))
    option_norms = _field_option_norms(field)
    is_binary = bool(option_norms & {"yes", "no"}) or field.field_type in {
        "checkbox",
        "checkbox-group",
        "radio",
        "radio-group",
        "toggle",
        "button-group",
    }
    question_stem = _label_starts_boolean_question(label)

    if any(token in label for token in ("relocation", "relocate")):
        if any(token in label for token in ("location to which", "where would you relocate", "preferred location")):
            cluster, role = "relocation", "location_child"
            _log_boolean_widget_classification(field, cluster, role)
            return cluster, role
        if is_binary or question_stem or any(token in label for token in ("willing to relocate", "open to relocation")):
            cluster, role = "relocation", "boolean_parent"
            _log_boolean_widget_classification(field, cluster, role)
            return cluster, role
        cluster, role = "relocation", "detail_child"
        _log_boolean_widget_classification(field, cluster, role)
        return cluster, role

    if "visa" in label or "sponsorship" in label:
        if _is_detail_child_prompt(label):
            cluster, role = "visa_sponsorship", "detail_child"
            _log_boolean_widget_classification(field, cluster, role)
            return cluster, role
        if (
            is_binary
            or question_stem
            or any(token in label for token in ("require sponsorship", "need sponsorship", "visa sponsorship"))
        ):
            cluster, role = "visa_sponsorship", "boolean_parent"
            _log_boolean_widget_classification(field, cluster, role)
            return cluster, role
        cluster, role = "visa_sponsorship", "detail_child"
        _log_boolean_widget_classification(field, cluster, role)
        return cluster, role

    if any(token in label for token in ("authorized to work", "legally permitted to work")):
        if _is_detail_child_prompt(label):
            cluster, role = "work_authorization", "detail_child"
            _log_boolean_widget_classification(field, cluster, role)
            return cluster, role
        if is_binary or question_stem:
            cluster, role = "work_authorization", "boolean_parent"
            _log_boolean_widget_classification(field, cluster, role)
            return cluster, role
        cluster, role = "work_authorization", "detail_child"
        _log_boolean_widget_classification(field, cluster, role)
        return cluster, role

    if _is_employer_history_boolean_prompt(label):
        cluster, role = "employer_history", "boolean_parent"
        _log_boolean_widget_classification(field, cluster, role)
        return cluster, role

    if "salary" in label or "compensation" in label:
        return "salary", "detail_child"
    if "start date" in label or "availability" in label:
        return "availability_window", "detail_child"
    if "notice" in label:
        return "notice_period", "detail_child"
    if any(token in label for token in ("from date", "start date", "date from")) and "education" in normalize_name(
        field.section or ""
    ):
        return "education_dates", "start_child"
    if any(
        token in label for token in ("to date", "end date", "graduation date", "completion date")
    ) and "education" in normalize_name(field.section or ""):
        return "education_dates", "end_child"
    return None, None


def _is_binary_value_text(value: str | None) -> bool:
    if not value:
        return False
    normalized = _normalize_binary_match_value(value)
    if normalized in {"Yes", "No"}:
        return True
    return normalize_name(value) in {"checked", "unchecked", "true", "false"}


def _value_shape_is_compatible(field: FormField, value: str | None) -> bool:
    if value in (None, ""):
        return False

    _, role = _field_conditional_cluster(field)
    option_norms = _field_option_norms(field)
    expects_binary = bool(option_norms & {"yes", "no"}) or role == "boolean_parent"
    if expects_binary:
        return _is_binary_value_text(value)

    if role in {"location_child", "detail_child", "start_child", "end_child"} and _is_binary_value_text(value):
        return False

    if field.field_type in {"text", "textarea", "search"} and _is_binary_value_text(value):
        label = normalize_name(_preferred_field_label(field))
        if any(token in label for token in ("please specify", "location to which", "type of visa", "what visa")):
            return False

    return True


def _log_answer_resolution(
    field: FormField,
    *,
    source_candidate: str,
    raw_answer: Any,
    coerced_answer: str | None,
    shape_compatible: bool,
    rejection_reason: str | None,
) -> None:
    logger.info(
        "domhand.answer_resolution",
        extra={
            "field_id": field.field_id,
            "field_label": _preferred_field_label(field),
            "field_type": field.field_type,
            "widget_kind": _field_widget_kind_for_debug(field),
            "source_candidate": source_candidate,
            "raw_answer": _profile_debug_preview(raw_answer),
            "coerced_answer": _profile_debug_preview(coerced_answer),
            "shape_compatible": shape_compatible,
            "rejection_reason": rejection_reason or "",
        },
    )


def _coerce_answer_if_compatible(
    field: FormField,
    raw_answer: Any,
    *,
    source_candidate: str,
) -> str | None:
    coerced = _coerce_answer_to_field(field, raw_answer)
    if not coerced:
        _log_answer_resolution(
            field,
            source_candidate=source_candidate,
            raw_answer=raw_answer,
            coerced_answer=coerced,
            shape_compatible=False,
            rejection_reason="coercion_failed",
        )
        return None

    compatible = _value_shape_is_compatible(field, coerced)
    _log_answer_resolution(
        field,
        source_candidate=source_candidate,
        raw_answer=raw_answer,
        coerced_answer=coerced,
        shape_compatible=compatible,
        rejection_reason=None if compatible else "shape_incompatible",
    )
    return coerced if compatible else None


def _is_required_custom_widget_boolean_select(field: FormField) -> bool:
    _, role = _field_conditional_cluster(field)
    return field.required and field.field_type == "select" and not field.is_native and role == "boolean_parent"


async def _classify_known_intent_for_field(
    field: FormField,
    profile_data: dict[str, Any] | None,
    evidence: dict[str, str | None],
) -> tuple[SemanticQuestionIntent | None, str | None]:
    """Classify a blocking field into a known intent using a cheap text model."""
    if not field.required:
        return None, None

    label = _preferred_field_label(field)
    if has_cached_semantic_alias(label):
        cached = get_cached_semantic_alias(label)
        return (cached.intent, cached.confidence) if cached else (None, None)

    available = _available_semantic_intent_answers(field, profile_data, evidence)
    if not available:
        cache_semantic_alias(label, None)
        return None, None

    try:
        from browser_use.llm.messages import UserMessage
        from ghosthands.config.settings import settings as _settings
        from ghosthands.llm.client import get_chat_model
    except ImportError:
        return None, None

    model_id = _settings.semantic_match_model or _settings.domhand_model
    llm = get_chat_model(model=model_id)
    allowed_intents = [
        {
            "intent": intent,
            "description": _SEMANTIC_INTENT_DESCRIPTIONS[intent],
        }
        for intent in available.keys()
    ]
    prompt = (
        "You classify one job-application field into a known saved-profile intent.\n"
        "Return ONLY valid JSON with keys intent and confidence.\n"
        'confidence must be one of "high", "medium", or "low".\n'
        "Do not generate answer text.\n\n"
        f"Field label: {label}\n"
        f"Question text: {field.raw_label or field.name}\n"
        f"Section: {field.section or ''}\n"
        f"Field type: {field.field_type}\n"
        f"Options: {json.dumps((field.options or field.choices or [])[:20], ensure_ascii=True)}\n"
        f"Allowed intents: {json.dumps(allowed_intents, ensure_ascii=True)}\n"
    )

    try:
        response = await llm.ainvoke([UserMessage(content=prompt)], max_tokens=200)
        text = response.completion if isinstance(response.completion, str) else ""
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.MULTILINE)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned, flags=re.MULTILINE).strip()
        parsed = json.loads(cleaned)
        intent = parsed.get("intent")
        confidence = str(parsed.get("confidence") or "").strip().lower()
        if intent not in available or confidence != "high":
            _trace_profile_resolution(
                "domhand.profile_semantic_classifier_rejected",
                field_label=label,
                available_intents=",".join(sorted(available.keys())),
                proposed_intent=str(intent or ""),
                confidence=confidence or "missing",
            )
            cache_semantic_alias(label, None)
            return None, None
        alias = get_learned_question_alias(label, profile_data)
        if alias is None or alias.intent != intent:
            stage_learned_question_alias(
                label,
                intent,  # type: ignore[arg-type]
                source="semantic_fallback",
                confidence=confidence,
                profile_data=profile_data,
            )
        cache_semantic_alias(
            label,
            get_learned_question_alias(label, profile_data),
        )
        _trace_profile_resolution(
            "domhand.profile_semantic_classifier_resolved",
            field_label=label,
            available_intents=",".join(sorted(available.keys())),
            intent=intent,
            confidence=confidence,
            model=model_id,
        )
        return intent, confidence
    except Exception as exc:
        _trace_profile_resolution(
            "domhand.profile_semantic_classifier_failed",
            field_label=label,
            available_intents=",".join(sorted(available.keys())),
            error=str(exc)[:160],
            model=model_id,
        )
        cache_semantic_alias(label, None)
        return None, None


async def _semantic_profile_value_for_field(
    field: FormField,
    evidence: dict[str, str | None],
    profile_data: dict[str, Any] | None = None,
) -> str | None:
    """Resolve a field via learned aliases or classification-only semantic fallback."""
    for label in _field_label_candidates(field):
        alias = get_learned_question_alias(label, profile_data)
        if alias is None:
            continue
        answer = _resolve_semantic_intent_answer(field, alias.intent, profile_data, evidence)
        coerced = _coerce_answer_if_compatible(
            field,
            answer,
            source_candidate="semantic",
        )
        if coerced:
            _trace_profile_resolution(
                "domhand.profile_learned_alias_match",
                field_label=_preferred_field_label(field),
                source_label=label,
                intent=alias.intent,
                coerced_value=_profile_debug_preview(coerced),
            )
            return coerced

    intent, confidence = await _classify_known_intent_for_field(field, profile_data, evidence)
    if intent is None:
        return None
    answer = _resolve_semantic_intent_answer(field, intent, profile_data, evidence)
    coerced = _coerce_answer_if_compatible(
        field,
        answer,
        source_candidate="semantic",
    )
    if coerced:
        _trace_profile_resolution(
            "domhand.profile_semantic_intent_match",
            field_label=_preferred_field_label(field),
            intent=intent,
            confidence=confidence,
            coerced_value=_profile_debug_preview(coerced),
        )
        return coerced
    return None


def _parse_dropdown_click_result(raw_result: Any) -> dict[str, Any]:
    """Normalize dropdown click helper results into a dict."""
    if isinstance(raw_result, str):
        try:
            parsed = json.loads(raw_result)
        except json.JSONDecodeError:
            return {"clicked": False}
        return parsed if isinstance(parsed, dict) else {"clicked": False}
    return raw_result if isinstance(raw_result, dict) else {"clicked": False}


def _field_value_matches_expected(current: str, expected: str) -> bool:
    """Return True when the visible field value reflects the intended selection."""
    current_text = (current or "").strip()
    expected_text = (expected or "").strip()
    if not current_text or not expected_text:
        return False
    if _is_effectively_unset_field_value(current_text):
        return False

    current_norm = normalize_name(current_text)
    expected_norm = normalize_name(expected_text)
    if not current_norm or not expected_norm:
        return False
    current_binary = _normalize_binary_match_value(current_text)
    expected_binary = _normalize_binary_match_value(expected_text)
    if current_binary and expected_binary:
        return current_binary == expected_binary
    parsed_current_date = _parse_full_date_value(current_text)
    parsed_expected_date = _parse_full_date_value(expected_text)
    if parsed_current_date and parsed_expected_date and parsed_current_date == parsed_expected_date:
        return True
    if expected_norm in current_norm or current_norm in expected_norm:
        return True

    segments = split_dropdown_value_hierarchy(expected_text)
    if not segments:
        return False
    final_segment = normalize_name(segments[-1])
    return bool(final_segment and final_segment in current_norm)


async def _read_field_value(page: Any, field_id: str) -> str:
    """Read the current visible value for a field."""
    try:
        raw_value = await page.evaluate(_READ_FIELD_VALUE_JS, field_id)
    except Exception:
        return ""
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return raw_value.strip()
        return str(parsed or "").strip()
    return str(raw_value or "").strip()


async def _wait_for_field_value(
    page: Any,
    field: FormField,
    expected: str,
    timeout: float = 2.4,
    poll_interval: float = 0.25,
) -> str:
    """Wait briefly for a field's visible value to reflect the intended selection."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_value = ""
    while True:
        current = await _read_field_value_for_field(page, field)
        if current:
            last_value = current
        if _field_value_matches_expected(current, expected):
            return current
        if loop.time() >= deadline:
            return last_value
        await asyncio.sleep(poll_interval)


async def _read_group_selection(page: Any, field_id: str) -> str:
    """Read the currently selected label for a radio/button-style group."""
    try:
        raw = await page.evaluate(_READ_GROUP_SELECTION_JS, field_id)
    except Exception:
        return ""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return ""
    if isinstance(parsed, dict):
        return str(parsed.get("selected") or "").strip()
    return ""


async def _read_checkbox_group_value(page: Any, field: FormField) -> str:
    """Read a grouped checkbox value without forcing exclusive-choice semantics globally."""
    if _checkbox_group_is_exclusive_choice(field):
        return await _read_group_selection(page, field.field_id)
    state = await _read_binary_state(page, field.field_id)
    return "checked" if state else ""


async def _get_group_option_target(page: Any, field_id: str, text: str) -> dict[str, Any]:
    """Get clickable coordinates for a choice inside a custom group control."""
    try:
        raw = await page.evaluate(_GET_GROUP_OPTION_TARGET_JS, field_id, text)
    except Exception:
        return {"found": False}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {"found": False}
    return parsed if isinstance(parsed, dict) else {"found": False}


async def _read_binary_state(page: Any, field_id: str) -> bool | None:
    """Read the checked/pressed state of a checkbox/radio/toggle-like control."""
    try:
        raw = await page.evaluate(_READ_BINARY_STATE_JS, field_id)
    except Exception:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, bool) or parsed is None:
        return parsed
    return None


async def _get_binary_click_target(page: Any, field_id: str) -> dict[str, Any]:
    """Get visible click coordinates for a checkbox/radio/toggle control."""
    try:
        raw = await page.evaluate(_GET_BINARY_CLICK_TARGET_JS, field_id)
    except Exception:
        return {"found": False}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {"found": False}
    return parsed if isinstance(parsed, dict) else {"found": False}


async def _field_has_validation_error(page: Any, field_id: str) -> bool:
    """Check whether the field or its wrapper still exposes an invalid state."""
    try:
        raw = await page.evaluate(_HAS_FIELD_VALIDATION_ERROR_JS, field_id)
    except Exception:
        return False
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return False
    return bool(parsed)


async def _click_binary_with_gui(page: Any, field: FormField, tag: str, desired_checked: bool) -> bool:
    """Use a real trusted click on checkbox/radio/toggle-like controls."""
    target = await _get_binary_click_target(page, field.field_id)
    if not target.get("found"):
        return False
    try:
        mouse = await page.mouse
        await mouse.click(int(target["x"]), int(target["y"]))
        await asyncio.sleep(0.25)
    except Exception as exc:
        logger.debug(f"gui click {tag} failed: {str(exc)[:60]}")
        return False

    current = await _read_binary_state(page, field.field_id)
    if current is desired_checked:
        logger.debug(f'gui-check {tag} -> "{target.get("text", field.name)}"')
        return True
    return False


def _field_needs_enter_commit(field: FormField) -> bool:
    """Return True for fields that often need a real Enter to commit the value."""
    label_norm = normalize_name(_preferred_field_label(field))
    section_norm = normalize_name(field.section or "")
    if field.field_type in {"search", "date"}:
        return True
    if label_norm in {"name", "date", "month", "day", "year"}:
        return True
    return label_norm == "name" and any(token in section_norm for token in ("self identify", "voluntary disclosure"))


def _is_self_identify_date_field(field: FormField) -> bool:
    label_norm = normalize_name(_preferred_field_label(field))
    section_norm = normalize_name(field.section or "")
    return label_norm == "date" and any(token in section_norm for token in ("self identify", "disability"))


def _is_salary_like_field(field: FormField) -> bool:
    label_norm = normalize_name(_preferred_field_label(field))
    return any(token in label_norm for token in ("salary", "compensation", "pay expectation", "pay requirement"))


def _field_needs_blur_revalidation(field: FormField) -> bool:
    return _is_salary_like_field(field)


def _coerce_salary_numeric_candidate(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    compact = text.replace(",", "")
    k_match = re.search(r"(\d+(?:\.\d+)?)\s*[kK]\b", compact)
    if k_match:
        return str(int(float(k_match.group(1)) * 1000))
    numeric_tokens = re.findall(r"\d+(?:\.\d+)?", compact)
    if not numeric_tokens:
        return None
    for token in numeric_tokens:
        whole = token.split(".", 1)[0]
        if len(whole) >= 4:
            return whole
    return numeric_tokens[0].split(".", 1)[0]


def _is_date_component_field(field: FormField) -> bool:
    label_norm = normalize_name(_preferred_field_label(field))
    return field.field_type in {"text", "number"} and label_norm in {"month", "day", "year"}


def _is_grouped_date_field(field: FormField) -> bool:
    return (
        field.field_type == "date"
        and (field.widget_kind or "") == "grouped_date"
        and len(field.component_field_ids) >= 3
    )


def _parse_full_date_value(value: str | None) -> tuple[int, int, int] | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = re.sub(r"[.\-]", "/", text)
    parts = [part.strip() for part in normalized.split("/") if part.strip()]
    if len(parts) != 3:
        return None
    try:
        if len(parts[0]) == 4:
            year = int(parts[0])
            month = int(parts[1])
            day = int(parts[2])
        elif len(parts[2]) == 4:
            month = int(parts[0])
            day = int(parts[1])
            year = int(parts[2])
        else:
            return None
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 9999):
        return None
    return (year, month, day)


def _grouped_date_is_complete(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = normalize_name(text)
    if any(token in normalized for token in {"mm", "dd", "yyyy", "month", "day", "year"}):
        return False
    return _parse_full_date_value(text) is not None


def _field_has_effective_value(field: FormField) -> bool:
    current_value = str(field.current_value or "").strip()
    if not current_value or _is_effectively_unset_field_value(current_value):
        return False
    if _is_grouped_date_field(field):
        return _grouped_date_is_complete(current_value)
    return True


async def _read_field_value_for_field(page: Any, field: FormField) -> str:
    if not _is_grouped_date_field(field):
        return await _read_field_value(page, field.field_id)
    component_ids = list(field.component_field_ids)
    if len(component_ids) < 3:
        return await _read_field_value(page, field.field_id)
    month_value = await _read_field_value(page, component_ids[0])
    day_value = await _read_field_value(page, component_ids[1])
    year_value = await _read_field_value(page, component_ids[2])
    return _compose_grouped_date_value(month_value, day_value, year_value)


def _text_fill_attempt_values(field: FormField, value: str) -> list[str]:
    attempts = [str(value)]
    if _is_date_component_field(field):
        label_norm = normalize_name(_preferred_field_label(field))
        digits = re.sub(r"\D", "", attempts[0])
        if label_norm in {"month", "day"} and len(digits) == 1:
            padded = digits.zfill(2)
            if padded not in attempts:
                attempts.append(padded)
        if label_norm in {"month", "day"} and len(digits) == 2 and digits.startswith("0"):
            unpadded = str(int(digits))
            if unpadded not in attempts:
                attempts.append(unpadded)
    if _is_salary_like_field(field):
        numeric_candidate = _coerce_salary_numeric_candidate(value)
        if numeric_candidate and numeric_candidate not in attempts:
            attempts.append(numeric_candidate)
    return attempts


async def _click_away_from_text_like_field(page: Any, field_id: str) -> bool:
    try:
        raw = await page.evaluate(_CLICK_OTHER_TEXTLIKE_FIELD_JS, field_id)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return bool(isinstance(parsed, dict) and parsed.get("clicked"))
    except Exception:
        try:
            await page.evaluate(_DISMISS_DROPDOWN_JS)
        except Exception:
            return False
        return False


async def _confirm_text_like_value(page: Any, field: FormField, value: str, tag: str) -> bool:
    """Verify a text-like field and use a narrow commit sequence when needed."""
    current = await _wait_for_field_value(page, field, value, timeout=0.9, poll_interval=0.15)
    if not _field_value_matches_expected(current, value):
        return False
    needs_enter_commit = _field_needs_enter_commit(field)
    needs_blur_revalidation = _field_needs_blur_revalidation(field)
    if (
        not needs_enter_commit
        and not needs_blur_revalidation
        and not await _field_has_validation_error(page, field.field_id)
    ):
        return True
    selector = f'[data-ff-id="{field.field_id}"]'
    try:
        await page.evaluate(_FOCUS_FIELD_JS, field.field_id)
        await asyncio.sleep(0.05)
        locator = page.locator(selector).first
        await locator.click(timeout=500)
        await asyncio.sleep(0.05)
        if needs_enter_commit:
            await locator.press("Enter")
            await asyncio.sleep(0.15)
        if needs_blur_revalidation:
            await page.evaluate(_DISMISS_DROPDOWN_JS)
            await asyncio.sleep(0.15)
        else:
            await locator.press("Tab")
            await asyncio.sleep(0.1)
    except Exception:
        try:
            await page.evaluate(_FOCUS_FIELD_JS, field.field_id)
            await asyncio.sleep(0.05)
            if needs_enter_commit:
                await page.keyboard.press("Enter")
                await asyncio.sleep(0.15)
            if needs_blur_revalidation:
                await page.evaluate(_DISMISS_DROPDOWN_JS)
                await asyncio.sleep(0.15)
            else:
                await page.keyboard.press("Tab")
                await asyncio.sleep(0.1)
        except Exception:
            return _field_value_matches_expected(current, value) and not await _field_has_validation_error(
                page, field.field_id
            )
    confirmed = await _wait_for_field_value(page, field, value, timeout=1.1, poll_interval=0.15)
    if _field_value_matches_expected(confirmed, value) and not await _field_has_validation_error(page, field.field_id):
        logger.debug(f"confirm {tag} -> enter/tab commit")
        return True
    if _field_value_matches_expected(confirmed, value):
        await _click_away_from_text_like_field(page, field.field_id)
        await asyncio.sleep(0.15)
        confirmed = await _wait_for_field_value(page, field, value, timeout=1.1, poll_interval=0.15)
        if _field_value_matches_expected(confirmed, value) and not await _field_has_validation_error(
            page, field.field_id
        ):
            logger.debug(f"confirm {tag} -> click-away commit")
            return True
    return False


async def _fill_text_like_with_keyboard(page: Any, field: FormField, value: str, tag: str) -> bool:
    """Type into a field with browser-use actor events, then commit with Enter/Tab."""
    try:
        selector = f'[data-ff-id="{field.field_id}"]'
        elements = await page.get_elements_by_css_selector(selector)
        if not elements:
            return False
        await elements[0].fill(value, clear=True)
        await asyncio.sleep(0.35)
        if _field_needs_enter_commit(field):
            await page.press("Enter")
            await asyncio.sleep(0.15)
        await page.press("Tab")
        await asyncio.sleep(0.1)
        return await _confirm_text_like_value(page, field, value, tag)
    except Exception:
        return False


async def _click_group_option_with_gui(page: Any, field: FormField, value: str, tag: str) -> bool:
    """Use a real mouse click on the visible option when DOM clicks do not stick."""
    target = await _get_group_option_target(page, field.field_id, value)
    if not target.get("found"):
        return False
    try:
        mouse = await page.mouse
        await mouse.click(int(target["x"]), int(target["y"]))
        await asyncio.sleep(0.25)
    except Exception as exc:
        logger.debug(f"gui click {tag} failed: {str(exc)[:60]}")
        return False

    current = await _read_group_selection(page, field.field_id)
    if _field_value_matches_expected(current, value):
        logger.debug(f'gui-select {tag} -> "{target.get("text", value)}"')
        return True
    return False


async def _reset_group_selection_with_gui(
    page: Any,
    field: FormField,
    current_value: str,
    desired_value: str,
    tag: str,
) -> bool:
    """Late fallback for sticky custom groups: clear current selection, then reselect."""
    if not current_value or _field_value_matches_expected(current_value, desired_value):
        return False
    target = await _get_group_option_target(page, field.field_id, current_value)
    if not target.get("found"):
        return False
    try:
        mouse = await page.mouse
        await mouse.click(int(target["x"]), int(target["y"]))
        await asyncio.sleep(0.25)
    except Exception as exc:
        logger.debug(f"gui reset {tag} failed: {str(exc)[:60]}")
        return False
    if await _click_group_option_with_gui(page, field, desired_value, tag):
        logger.debug(f'group-reset {tag} -> "{desired_value}"')
        return True
    return False


async def _refresh_binary_field(page: Any, field: FormField, tag: str, desired_checked: bool) -> bool:
    """Late fallback for sticky checkboxes/toggles: clear and re-apply the target state."""
    try:
        result_json = await page.evaluate(_CLICK_BINARY_FIELD_JS, field.field_id, not desired_checked)
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
        if isinstance(result, dict) and result.get("clicked"):
            await asyncio.sleep(0.2)
    except Exception:
        pass
    if await _click_binary_with_gui(page, field, tag, desired_checked):
        logger.debug(f"binary-refresh {tag}")
        return True
    return False


async def _load_field_interaction_recipe(page: Any, field: FormField) -> dict[str, Any] | None:
    """Load a host-scoped interaction recipe for a non-select field when available."""
    page_url = await _safe_page_url(page)
    if not page_url:
        return None

    profile_data = _get_profile_data()
    label = _preferred_field_label(field)
    widget_signature = field.field_type or "unknown"
    recipe = get_interaction_recipe(
        platform=detect_platform_from_url(page_url),
        host=detect_host_from_url(page_url),
        label=label,
        widget_signature=widget_signature,
        profile_data=profile_data,
    )
    if recipe is not None:
        _trace_profile_resolution(
            "domhand.field_recipe_loaded",
            field_label=label,
            widget_signature=widget_signature,
            preferred_action_chain=",".join(recipe.preferred_action_chain),
        )
    return {
        "page_url": page_url,
        "profile_data": profile_data,
        "label": label,
        "widget_signature": widget_signature,
        "recipe": recipe,
    }


def _record_field_interaction_recipe(context: dict[str, Any] | None, action_chain: list[str]) -> None:
    """Persist a successful GUI/reset recovery as a learned interaction recipe."""
    if not context or not action_chain:
        return

    page_url = str(context.get("page_url") or "")
    label = str(context.get("label") or "")
    widget_signature = str(context.get("widget_signature") or "")
    if not page_url or not label or not widget_signature:
        return

    record_interaction_recipe(
        platform=detect_platform_from_url(page_url),
        host=detect_host_from_url(page_url),
        label=label,
        widget_signature=widget_signature,
        preferred_action_chain=action_chain,
        source="visual_fallback",
        profile_data=context.get("profile_data"),
    )
    _trace_profile_resolution(
        "domhand.field_recipe_recorded",
        field_label=label,
        widget_signature=widget_signature,
        preferred_action_chain=",".join(action_chain),
    )


def _is_domhand_retry_capped_for_field(host: str, field: FormField, desired_value: str) -> bool:
    return is_domhand_retry_capped(
        host=host,
        field_key=_domhand_retry_field_identity(field),
        desired_value=desired_value,
    )


def _record_domhand_failure_for_field(
    host: str, field: FormField, desired_value: str, tool_name: str
) -> tuple[int, bool]:
    count = record_domhand_failure(
        host=host,
        field_key=_domhand_retry_field_identity(field),
        desired_value=desired_value,
    )
    capped = count >= DOMHAND_RETRY_CAP
    logger.info(
        "domhand.field_retry_state",
        extra={
            "tool": tool_name,
            "field_label": _preferred_field_label(field),
            "field_key": _domhand_retry_field_identity(field),
            "desired_value": desired_value,
            "host": host,
            "failure_count": count,
            "retry_capped": capped,
        },
    )
    return count, capped


def _clear_domhand_failure_for_field(host: str, field: FormField, desired_value: str) -> None:
    clear_domhand_failure(
        host=host,
        field_key=_domhand_retry_field_identity(field),
        desired_value=desired_value,
    )


async def _attempt_domhand_fill_with_retry_cap(
    page: Any,
    *,
    host: str,
    field: FormField,
    desired_value: str,
    tool_name: str,
) -> tuple[bool, str | None, str | None]:
    """Attempt a DomHand field fill while enforcing the generic per-field retry cap."""
    if await _field_already_matches(page, field, desired_value):
        _clear_domhand_failure_for_field(host, field, desired_value)
        return True, None, None

    if _is_domhand_retry_capped_for_field(host, field, desired_value):
        logger.info(
            "domhand.field_retry_capped",
            extra={
                "tool": tool_name,
                "field_label": _preferred_field_label(field),
                "field_key": _domhand_retry_field_identity(field),
                "desired_value": desired_value,
                "host": host,
                "failure_count": get_domhand_failure_count(
                    host=host,
                    field_key=_domhand_retry_field_identity(field),
                    desired_value=desired_value,
                ),
            },
        )
        return False, _domhand_retry_message(field), DOMHAND_RETRY_CAPPED

    success = await _fill_single_field(page, field, desired_value)
    if success:
        _clear_domhand_failure_for_field(host, field, desired_value)
        return True, None, None

    _, capped = _record_domhand_failure_for_field(host, field, desired_value, tool_name)
    if capped:
        return False, _domhand_retry_message(field), DOMHAND_RETRY_CAPPED
    return False, "DOM fill failed", "dom_fill_failed"


async def _field_already_matches(page: Any, field: FormField, value: str | None) -> bool:
    """Live DOM check to avoid re-filling a field that already settled correctly."""
    if not value:
        return False
    if field.field_type == "checkbox-group":
        if _checkbox_group_is_exclusive_choice(field):
            current = await _read_group_selection(page, field.field_id)
            return _field_value_matches_expected(current, value) and not await _field_has_validation_error(
                page, field.field_id
            )
        desired_checked = not _is_explicit_false(value)
        state = await _read_binary_state(page, field.field_id)
        return state is desired_checked and not await _field_has_validation_error(page, field.field_id)
    if field.field_type in {"checkbox", "toggle"}:
        desired_checked = not _is_explicit_false(value)
        state = await _read_binary_state(page, field.field_id)
        return state is desired_checked and not await _field_has_validation_error(page, field.field_id)
    if field.field_type in {"radio-group", "radio", "button-group"}:
        current = await _read_group_selection(page, field.field_id)
        return _field_value_matches_expected(current, value) and not await _field_has_validation_error(
            page, field.field_id
        )
    current = await _read_field_value_for_field(page, field)
    return _field_value_matches_expected(current, value) and not await _field_has_validation_error(page, field.field_id)


async def _read_observed_field_value(page: Any, field: FormField) -> str:
    """Read the visible value of a field using the correct control-specific path."""
    if field.field_type == "checkbox-group":
        if _checkbox_group_is_exclusive_choice(field):
            return await _read_group_selection(page, field.field_id)
        state = await _read_binary_state(page, field.field_id)
        return "checked" if state else ""
    if field.field_type in {"checkbox", "toggle"}:
        state = await _read_binary_state(page, field.field_id)
        return "checked" if state else ""
    if field.field_type in {"radio-group", "radio", "button-group"}:
        return await _read_group_selection(page, field.field_id)
    return await _read_field_value_for_field(page, field)


async def _record_expected_value_if_settled(
    *,
    page: Any,
    host: str,
    page_context_key: str,
    field: FormField,
    field_key: str,
    expected_value: str,
    source: str,
    log_context: str,
) -> bool:
    """Persist an expected value only after the field visibly settled and validated."""
    observed_value = await _read_observed_field_value(page, field)
    has_validation_error = await _field_has_validation_error(page, field.field_id)
    if not await _field_already_matches(page, field, expected_value):
        logger.info(
            f"{log_context}.skip_record_expected_value",
            extra={
                "field_id": field.field_id,
                "field_key": field_key,
                "field_label": _preferred_field_label(field),
                "field_type": field.field_type,
                "field_section": field.section or "",
                "field_fingerprint": field.field_fingerprint or "",
                "expected_value": expected_value,
                "observed_value": observed_value,
                "validation_cleared": not has_validation_error,
                "reason": "value_not_settled",
            },
        )
        return False
    if has_validation_error:
        logger.info(
            f"{log_context}.skip_record_expected_value",
            extra={
                "field_id": field.field_id,
                "field_key": field_key,
                "field_label": _preferred_field_label(field),
                "field_type": field.field_type,
                "field_section": field.section or "",
                "field_fingerprint": field.field_fingerprint or "",
                "expected_value": expected_value,
                "observed_value": observed_value,
                "validation_cleared": False,
                "reason": "validation_error",
            },
        )
        return False

    record_expected_field_value(
        host=host,
        page_context_key=page_context_key,
        field_key=field_key,
        field_label=_preferred_field_label(field),
        field_type=field.field_type,
        field_section=field.section or "",
        field_fingerprint=field.field_fingerprint or "",
        expected_value=expected_value,
        source=source,
    )
    logger.info(
        f"{log_context}.record_expected_value",
        extra={
            "field_id": field.field_id,
            "field_key": field_key,
            "field_label": _preferred_field_label(field),
            "field_type": field.field_type,
            "field_section": field.section or "",
            "field_fingerprint": field.field_fingerprint or "",
            "expected_value": expected_value,
            "observed_value": observed_value,
            "validation_cleared": True,
            "source": source,
        },
    )
    return True


def _known_profile_value(
    field_name: str,
    evidence: dict[str, str | None],
    profile_data: dict[str, Any] | None = None,
) -> str | None:
    """Return a profile value if the field name matches a known personal field."""
    name = normalize_name(field_name)
    if not name:
        return None
    if _is_skill_like(field_name):
        skills = _profile_skill_values(profile_data)
        if skills:
            return ", ".join(skills)
    if "suffix" in name:
        return None
    if "preferred name" in name or "nickname" in name:
        canonical = build_canonical_profile(profile_data or {}, evidence)
        return canonical.get("preferred_name")
    if "first name" in name and evidence.get("first_name"):
        return evidence["first_name"]
    if "last name" in name and evidence.get("last_name"):
        return evidence["last_name"]
    if "full name" in name:
        first = evidence.get("first_name", "")
        last = evidence.get("last_name", "")
        if first or last:
            return f"{first} {last}".strip()
    if "email" in name and evidence.get("email"):
        return evidence["email"]
    if "phone extension" in name:
        return None
    if any(kw in name for kw in ("phone device", "phone type")):
        return evidence.get("phone_device_type")
    if any(kw in name for kw in ("country phone code", "phone country code", "country code")):
        return evidence.get("phone_country_code")
    if any(kw in name for kw in ("phone", "mobile", "telephone")) and evidence.get("phone"):
        return evidence["phone"]
    if any(
        kw in name
        for kw in (
            "address line 2",
            "address 2",
            "street line 2",
            "apartment",
            "apt",
            "suite",
            "unit",
            "mailing address line 2",
        )
    ):
        return evidence.get("address_line_2")
    if any(
        kw in name
        for kw in ("address", "street address", "address line 1", "address 1", "street line 1", "mailing address")
    ):
        return evidence.get("address")
    if name == "city" or " city" in name:
        return evidence.get("city")
    if (
        name == "state"
        or "state/province" in name
        or "state / province" in name
        or "province" in name
        or name == "region"
    ):
        return evidence.get("state")
    if any(kw in name for kw in ("county", "parish", "borough")):
        return evidence.get("county")
    if "postal" in name or "zip" in name:
        return evidence.get("zip")
    if any(kw in name for kw in ("country/region", "country region", "country")):
        return evidence.get("country")
    # Combined location fields (e.g. "Location", "City, State")
    if "location" in name:
        city = evidence.get("city", "")
        state = evidence.get("state", "")
        if city and state:
            return f"{city}, {state}"
        return city or state or evidence.get("location", "") or None
    if "linkedin" in name:
        return evidence.get("linkedin")
    if "github" in name:
        return evidence.get("github")
    if any(
        kw in name
        for kw in ("portfolio", "website", "website url", "personal site", "personal website", "blog", "homepage")
    ):
        return evidence.get("portfolio")
    if "twitter" in name or "x handle" in name:
        return evidence.get("twitter")
    # Workday-specific field matching
    if any(
        kw in name for kw in ("how did you hear", "learn about us", "referral source", "source of referral", "source")
    ):
        return evidence.get("how_did_you_hear")
    if any(
        kw in name
        for kw in ("work authorization", "authorized to work", "legally authorized", "legally permitted to work")
    ):
        return evidence.get("work_authorization")
    if any(kw in name for kw in ("start date", "earliest start", "available date", "availability")):
        return evidence.get("availability_window") or evidence.get("available_start_date")
    if "notice period" in name:
        return evidence.get("notice_period")
    if any(kw in name for kw in ("salary", "compensation", "pay expectation")):
        return evidence.get("salary_expectation")
    if any(kw in name for kw in ("current year in school", "school year", "academic standing", "class standing")):
        return evidence.get("current_school_year")
    if any(kw in name for kw in ("graduation date", "graduated date", "completion date")):
        return evidence.get("graduation_date")
    if any(kw in name for kw in ("degree seeking", "degree are you seeking", "degree sought", "degree pursuing")):
        return evidence.get("degree_seeking")
    if any(kw in name for kw in ("field of study", "area of study", "major", "discipline")):
        return evidence.get("field_of_study")
    if "certification" in name or "relevant license" in name or ("licenses" in name and "relevant" in name):
        return evidence.get("certifications_licenses") or "None"
    if any(kw in name for kw in ("languages spoken", "spoken languages", "preferred spoken languages")):
        return evidence.get("spoken_languages")
    if "english proficiency" in name:
        return evidence.get("english_proficiency")
    if any(kw in name for kw in ("country of residence", "country/region", "country region", "country")):
        return evidence.get("country_of_residence") or evidence.get("country")
    if any(kw in name for kw in ("willing to relocate", "relocation")):
        return evidence.get("willing_to_relocate")
    if any(
        kw in name
        for kw in (
            "preferred work",
            "work setup",
            "work arrangement",
            "remote",
            "hybrid",
            "onsite",
            "on site",
            "location preference",
            "preferred location",
            "preferred office",
        )
    ):
        return evidence.get("preferred_work_mode") or evidence.get("preferred_locations")
    # Auto-check agreement/consent checkboxes — always agree on behalf of applicant
    if any(
        kw in name
        for kw in (
            "i agree",
            "i accept",
            "i understand",
            "i acknowledge",
            "i consent",
            "i certify",
            "privacy policy",
            "terms of service",
            "terms and conditions",
            "candidate consent",
            "agree to",
        )
    ):
        return "checked"
    return None


def _known_profile_value_for_field(
    field: FormField,
    evidence: dict[str, str | None],
    profile_data: dict[str, Any] | None = None,
    minimum_confidence: str = "medium",
) -> str | None:
    """Try direct profile matching against all known labels for a field."""
    field_label = _preferred_field_label(field)
    profile_answer_map = _build_profile_answer_map(profile_data or {}, evidence)
    for label in _field_label_candidates(field):
        value = _find_best_profile_answer(label, profile_answer_map, minimum_confidence=minimum_confidence)
        if value:
            coerced = _coerce_answer_to_field(field, value)
            _trace_profile_resolution(
                "domhand.profile_answer_map_match",
                field_label=field_label,
                source_label=label,
                minimum_confidence=minimum_confidence,
                raw_value=_profile_debug_preview(value),
                coerced_value=_profile_debug_preview(coerced),
            )
            return coerced
    if _MATCH_CONFIDENCE_RANKS.get(minimum_confidence, 0) >= _MATCH_CONFIDENCE_RANKS["strong"]:
        _trace_profile_resolution(
            "domhand.profile_lookup_miss",
            field_label=field_label,
            minimum_confidence=minimum_confidence,
            reason="strong_match_required",
        )
        return None
    if _is_structured_education_field(field):
        _trace_profile_resolution(
            "domhand.profile_lookup_miss",
            field_label=field_label,
            minimum_confidence=minimum_confidence,
            reason="structured_education_exact_only",
        )
        return None
    for label in _field_label_candidates(field):
        value = _known_profile_value(label, evidence, profile_data)
        if value:
            coerced = _coerce_answer_to_field(field, value)
            _trace_profile_resolution(
                "domhand.profile_keyword_match",
                field_label=field_label,
                source_label=label,
                raw_value=_profile_debug_preview(value),
                coerced_value=_profile_debug_preview(coerced),
            )
            return coerced

    # ── Q&A bank fuzzy matching (synonym-based) ──────────────────────
    # If the answer bank has an entry whose question is a synonym of the
    # field label, use it. This catches cases like "How did you hear about
    # this position?" matching a stored answer for "How did you hear about us?"
    raw_bank = (profile_data or {}).get("answerBank") or (profile_data or {}).get("answer_bank")
    if isinstance(raw_bank, list) and raw_bank:
        capped = _cap_qa_entries(list(raw_bank))
        for label in _field_label_candidates(field):
            qa_val = _match_qa_answer(label, capped)
            if qa_val:
                coerced = _coerce_answer_to_field(field, qa_val)
                _trace_profile_resolution(
                    "domhand.profile_answer_bank_match",
                    field_label=field_label,
                    source_label=label,
                    raw_value=_profile_debug_preview(qa_val),
                    coerced_value=_profile_debug_preview(coerced),
                    answer_bank_count=len(capped),
                )
                return coerced

    default_answer = _default_screening_answer(field, profile_data or {})
    if default_answer:
        _trace_profile_resolution(
            "domhand.profile_default_answer",
            field_label=field_label,
            raw_value=_profile_debug_preview(default_answer),
        )
        return default_answer
    fallback = _coerce_answer_to_field(field, _known_profile_value(field.name, evidence, profile_data))
    _trace_profile_resolution(
        "domhand.profile_fallback_lookup",
        field_label=field_label,
        raw_value=_profile_debug_preview(_known_profile_value(field.name, evidence, profile_data)),
        coerced_value=_profile_debug_preview(fallback),
    )
    return fallback


def _resolved_field_value(
    value: str,
    *,
    source: str,
    answer_mode: str | None,
    confidence: float,
    state: str = "filled",
) -> ResolvedFieldValue:
    return ResolvedFieldValue(
        value=value,
        source=source,
        answer_mode=answer_mode,
        confidence=max(0.0, min(confidence, 1.0)),
        state=state,
    )


def _resolved_field_value_if_compatible(
    field: FormField,
    value: str | None,
    *,
    source: str,
    source_candidate: str | None = None,
    answer_mode: str | None,
    confidence: float,
    state: str = "filled",
) -> ResolvedFieldValue | None:
    coerced = _coerce_answer_if_compatible(
        field,
        value,
        source_candidate=source_candidate or source,
    )
    if not coerced:
        return None
    return _resolved_field_value(
        coerced,
        source=source,
        answer_mode=answer_mode,
        confidence=confidence,
        state=state,
    )


def _default_answer_mode_for_field(field: FormField, value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    norm = _normalize_match_label(field.name or "")
    if field.required and norm in _EEO_DECLINE_DEFAULTS and text == _EEO_DECLINE_DEFAULTS[norm]:
        return "default_decline"
    return None


def _match_confidence_score(confidence: str | None) -> float:
    return {
        "exact": 0.95,
        "strong": 0.85,
        "medium": 0.72,
        "weak": 0.58,
    }.get((confidence or "").strip().lower(), 0.55)


def _resolve_known_profile_value_for_field(
    field: FormField,
    evidence: dict[str, str | None],
    profile_data: dict[str, Any] | None = None,
    minimum_confidence: str = "medium",
) -> ResolvedFieldValue | None:
    field_label = _preferred_field_label(field)
    if _is_structured_education_field(field):
        _trace_profile_resolution(
            "domhand.profile_lookup_miss",
            field_label=field_label,
            minimum_confidence=minimum_confidence,
            reason="structured_education_exact_only",
        )
        return None
    profile_answer_map = _build_profile_answer_map(profile_data or {}, evidence)
    for label in _field_label_candidates(field):
        confidence = _label_match_confidence(label, label)
        value = _find_best_profile_answer(label, profile_answer_map, minimum_confidence=minimum_confidence)
        if value:
            coerced = _coerce_answer_if_compatible(
                field,
                value,
                source_candidate="profile",
            )
            if not coerced:
                continue
            _trace_profile_resolution(
                "domhand.profile_answer_map_match",
                field_label=field_label,
                source_label=label,
                minimum_confidence=minimum_confidence,
                raw_value=_profile_debug_preview(value),
                coerced_value=_profile_debug_preview(coerced),
            )
            return _resolved_field_value(
                coerced,
                source="derived_profile",
                answer_mode="profile_backed",
                confidence=_match_confidence_score(confidence),
            )
    if _MATCH_CONFIDENCE_RANKS.get(minimum_confidence, 0) >= _MATCH_CONFIDENCE_RANKS["strong"]:
        _trace_profile_resolution(
            "domhand.profile_lookup_miss",
            field_label=field_label,
            minimum_confidence=minimum_confidence,
            reason="strong_match_required",
        )
        return None
    for label in _field_label_candidates(field):
        value = _known_profile_value(label, evidence, profile_data)
        if value:
            coerced = _coerce_answer_if_compatible(
                field,
                value,
                source_candidate="profile",
            )
            if not coerced:
                continue
            _trace_profile_resolution(
                "domhand.profile_keyword_match",
                field_label=field_label,
                source_label=label,
                raw_value=_profile_debug_preview(value),
                coerced_value=_profile_debug_preview(coerced),
            )
            return _resolved_field_value(
                coerced,
                source="derived_profile",
                answer_mode="profile_backed",
                confidence=0.82,
            )

    raw_bank = (profile_data or {}).get("answerBank") or (profile_data or {}).get("answer_bank")
    if isinstance(raw_bank, list) and raw_bank:
        capped = _cap_qa_entries(list(raw_bank))
        for label in _field_label_candidates(field):
            qa_val = _match_qa_answer(label, capped)
            if qa_val:
                coerced = _coerce_answer_if_compatible(
                    field,
                    qa_val,
                    source_candidate="profile",
                )
                if not coerced:
                    continue
                _trace_profile_resolution(
                    "domhand.profile_answer_bank_match",
                    field_label=field_label,
                    source_label=label,
                    raw_value=_profile_debug_preview(qa_val),
                    coerced_value=_profile_debug_preview(coerced),
                    answer_bank_count=len(capped),
                )
                return _resolved_field_value(
                    coerced,
                    source="derived_profile",
                    answer_mode="profile_backed",
                    confidence=0.8,
                )

    default_answer = _default_screening_answer(field, profile_data or {})
    if default_answer:
        _trace_profile_resolution(
            "domhand.profile_default_answer",
            field_label=field_label,
            raw_value=_profile_debug_preview(default_answer),
        )
        return _resolved_field_value_if_compatible(
            field,
            default_answer,
            source="dom",
            source_candidate="default",
            answer_mode=_default_answer_mode_for_field(field, default_answer),
            confidence=0.68,
        )

    fallback = _coerce_answer_if_compatible(
        field,
        _known_profile_value(field.name, evidence, profile_data),
        source_candidate="profile",
    )
    _trace_profile_resolution(
        "domhand.profile_fallback_lookup",
        field_label=field_label,
        raw_value=_profile_debug_preview(_known_profile_value(field.name, evidence, profile_data)),
        coerced_value=_profile_debug_preview(fallback),
    )
    if not fallback:
        return None
    return _resolved_field_value(
        fallback,
        source="dom",
        answer_mode="profile_backed",
        confidence=0.75,
    )


def _default_value(field: FormField) -> str:
    """Fallback values allowed by strict-provenance policy only."""
    name_lower = normalize_name(field.name or "")
    if any(token in name_lower for token in ("signature date", "today", "current date")):
        return date.today().isoformat()
    if _is_self_identify_date_field(field):
        return date.today().isoformat()

    # EEO / demographic decline defaults — last resort for required fields
    # that were not matched by _match_answer.
    if field.required:
        norm = _normalize_match_label(field.name or "")
        if norm in _EEO_DECLINE_DEFAULTS:
            return _EEO_DECLINE_DEFAULTS[norm]
        if "certification" in norm or "relevant license" in norm or ("licenses" in norm and "relevant" in norm):
            return "None"

    return ""


def _resolve_llm_answer_for_field(
    field: FormField,
    answers: dict[str, str],
    evidence: dict[str, str | None],
    profile_data: dict[str, Any] | None = None,
) -> ResolvedFieldValue | None:
    if _is_skill_like(field.name):
        return None
    label_candidates = _field_label_candidates(field) or [field.name]
    if any(_is_non_guess_name_fragment(label) for label in label_candidates):
        return None
    candidate_norms = [_normalize_match_label(label) for label in label_candidates if _normalize_match_label(label)]
    minimum_confidence = "medium" if field.required else "strong"

    if field.field_type == "select":
        for norm_name in candidate_norms:
            if norm_name in _AUTHORITATIVE_SELECT_KEYS:
                for ck in _AUTHORITATIVE_SELECT_KEYS[norm_name]:
                    if ck in answers:
                        coerced = _coerce_answer_if_compatible(
                            field,
                            answers[ck],
                            source_candidate="llm",
                        )
                        if coerced:
                            return _resolved_field_value(
                                coerced,
                                source="llm",
                                answer_mode="best_effort_guess",
                                confidence=0.64,
                            )
                    for key, val in answers.items():
                        if normalize_name(key) == normalize_name(ck):
                            coerced = _coerce_answer_if_compatible(
                                field,
                                val,
                                source_candidate="llm",
                            )
                            if coerced:
                                return _resolved_field_value(
                                    coerced,
                                    source="llm",
                                    answer_mode="best_effort_guess",
                                    confidence=0.6,
                                )
                if norm_name in _AUTHORITATIVE_SELECT_DEFAULTS:
                    default_value = _AUTHORITATIVE_SELECT_DEFAULTS[norm_name]
                    return _resolved_field_value_if_compatible(
                        field,
                        default_value,
                        source="dom",
                        source_candidate="default",
                        answer_mode=_default_answer_mode_for_field(field, default_value),
                        confidence=0.66,
                    )

    best_resolution: ResolvedFieldValue | None = None
    best_rank = 0
    for key, val in answers.items():
        for candidate in label_candidates:
            confidence = _label_match_confidence(candidate, key)
            if not _meets_match_confidence(confidence, minimum_confidence):
                continue
            rank = _MATCH_CONFIDENCE_RANKS.get(confidence or "", 0)
            if rank <= best_rank:
                continue
            coerced = _coerce_answer_if_compatible(
                field,
                val,
                source_candidate="llm",
            )
            if not coerced:
                continue
            best_rank = rank
            best_resolution = _resolved_field_value(
                coerced,
                source="llm",
                answer_mode="best_effort_guess",
                confidence=_match_confidence_score(confidence),
            )
            if rank == _MATCH_CONFIDENCE_RANKS["exact"]:
                return best_resolution

    if best_resolution is not None:
        return best_resolution

    for norm_name in candidate_norms:
        if norm_name in _AUTHORITATIVE_SELECT_DEFAULTS:
            default_value = _AUTHORITATIVE_SELECT_DEFAULTS[norm_name]
            return _resolved_field_value_if_compatible(
                field,
                default_value,
                source="dom",
                source_candidate="default",
                answer_mode=_default_answer_mode_for_field(field, default_value),
                confidence=0.66,
            )

    if field.field_type in {"text", "textarea", "search"}:
        for norm_name in candidate_norms:
            if norm_name in _AUTHORITATIVE_TEXT_DEFAULTS:
                default_value = _AUTHORITATIVE_TEXT_DEFAULTS[norm_name]
                return _resolved_field_value_if_compatible(
                    field,
                    default_value,
                    source="dom",
                    source_candidate="default",
                    answer_mode=_default_answer_mode_for_field(field, default_value),
                    confidence=0.66,
                )

    if field.required:
        for norm_name in candidate_norms:
            if norm_name in _EEO_DECLINE_DEFAULTS:
                default_value = _EEO_DECLINE_DEFAULTS[norm_name]
                return _resolved_field_value_if_compatible(
                    field,
                    default_value,
                    source="dom",
                    source_candidate="default",
                    answer_mode="default_decline",
                    confidence=0.72,
                )

    default_value = _default_value(field)
    if not default_value:
        return None
    return _resolved_field_value_if_compatible(
        field,
        default_value,
        source="dom",
        source_candidate="default",
        answer_mode=_default_answer_mode_for_field(field, default_value),
        confidence=0.65,
    )


def _is_explicit_false(val: str | None) -> bool:
    """Return True if the value explicitly indicates unchecked/off/no."""
    if not val:
        return False
    return bool(re.match(r"^(unchecked|false|no|off|0)$", val.strip(), re.IGNORECASE))


def _takeover_suggestion_for_field(field: FormField, success: bool, actor: str, error: str | None) -> str | None:
    """Return a high-level takeover hint for browser-use after DomHand acts."""
    if success:
        return None
    if actor == "skipped":
        return "leave_blank" if not field.required else "browser_use_takeover"
    if field.field_type in {"select", "radio-group", "checkbox-group", "button-group", "radio", "checkbox", "toggle"}:
        return "browser_use_takeover"
    if field.field_type in {"text", "email", "tel", "url", "number", "password", "search", "date", "textarea"}:
        return "browser_use_takeover" if field.required else "retry_with_commit"
    if error and "REQUIRED" in error:
        return "browser_use_takeover"
    return "browser_use_takeover"


# ── LLM answer generation ───────────────────────────────────────────


def _sanitize_no_guess_answer(
    field_name: str,
    required: bool,
    answer: str | None,
    evidence: dict[str, str | None],
    *,
    field_type: str = "",
    question_text: str = "",
) -> str:
    """Prevent fabrication of sensitive identity fields not in profile.

    Apply flows do not pause for HITL. If the model still emits the legacy
    ``[NEEDS_USER_INPUT]`` marker, suppress it and fall back to saved/best-effort
    answers instead.
    """
    proposed = (answer or "").strip()
    known = _known_profile_value(field_name, evidence)
    if _is_non_guess_name_fragment(question_text or field_name):
        return known or ""

    # ── [NEEDS_USER_INPUT] passthrough ────────────────────────────────
    if proposed and "[NEEDS_USER_INPUT]" in proposed.upper():
        if known:
            _trace_profile_resolution(
                "domhand.profile_needs_input_overridden",
                field_label=field_name,
                proposed_marker="[NEEDS_USER_INPUT]",
                recovered_value=_profile_debug_preview(known),
            )
            return known
        best_effort = _known_profile_value(question_text or field_name, evidence)
        if best_effort:
            _trace_profile_resolution(
                "domhand.profile_needs_input_best_effort",
                field_label=field_name,
                proposed_marker="[NEEDS_USER_INPUT]",
                recovered_value=_profile_debug_preview(best_effort),
            )
            return best_effort
        # No-HITL apply flows never surface the marker. Required fields are
        # retried through best-effort inference and remain unresolved only if
        # DomHand still cannot infer a value.
        _trace_profile_resolution(
            "domhand.profile_needs_input_suppressed",
            field_label=field_name,
            proposed_marker="[NEEDS_USER_INPUT]",
            recovered_value="EMPTY",
        )
        return ""

    if known:
        return known
    if not _SOCIAL_OR_ID_NO_GUESS_RE.search(field_name or ""):
        return proposed
    if not proposed:
        return ""
    if is_placeholder_value(proposed) or re.match(
        r"^(n/a|na|none|unknown|not applicable|prefer not|decline)", proposed, re.IGNORECASE
    ):
        return ""
    return ""


def _disambiguated_field_names(fields: list[FormField]) -> list[str]:
    """Build deterministic display names for batched LLM answer generation."""
    name_counts: dict[str, int] = {}
    disambiguated_names: list[str] = []
    for i, field in enumerate(fields):
        base_name = _preferred_field_label(field) or f"Field {i + 1}"
        norm = normalize_name(base_name) or f"field-{i + 1}"
        count = name_counts.get(norm, 0) + 1
        name_counts[norm] = count
        disambiguated_names.append(f"{base_name} #{count}" if count > 1 else base_name)
    return disambiguated_names


async def _generate_answers(
    fields: list[FormField],
    profile_text: str,
    profile_data: dict[str, Any] | None = None,
) -> tuple[dict[str, str], int, int, float, str | None]:
    """Call the configured DomHand model to generate answers for all fields in a single batch."""
    try:
        from browser_use.llm.messages import UserMessage
        from ghosthands.config.models import estimate_cost
        from ghosthands.config.settings import settings as _settings
        from ghosthands.llm.client import get_chat_model
    except ImportError:
        logger.error("ghosthands.llm.client not available — cannot generate answers")
        return {}, 0, 0, 0.0, None

    evidence = _parse_profile_evidence(profile_text)
    model_id = _settings.domhand_model
    llm = get_chat_model(model=model_id)
    input_tokens = 0
    output_tokens = 0
    step_cost = 0.0

    disambiguated_names = _disambiguated_field_names(fields)

    field_descriptions = "\n".join(
        _build_field_description(field, disambiguated_names[i]) for i, field in enumerate(fields)
    )

    today = date.today().isoformat()
    prompt = f"""You are filling out a job application form on behalf of an applicant. Today's date is {today}.

Here is their profile:

{profile_text}

Here are the form fields to fill:

{field_descriptions}

Rules:
- For each field, decide what value to put based on the profile.
- For substantive applicant fields, use ONLY the applicant's actual profile data. Do not invent salary, start dates, work history, education, essays, addresses, or personal identifiers.
- If the profile has NO relevant data for an OPTIONAL field, return "" (empty string). NEVER make up data or use placeholder values like "N/A", "None", "Not applicable", etc.
- If the profile has NO direct answer for a REQUIRED field: first use a saved survey/profile answer, then a semantically equivalent QA-bank answer, then the closest structured education/experience value, then a deterministic default when one exists, and finally your best-effort guess. NEVER return "[NEEDS_USER_INPUT]".
- NEVER fabricate personal identifiers or social handles/URLs not explicitly in the profile. If missing: return "" (empty string) for optional fields, and omit the field if you still cannot infer a safe answer.
- For dropdowns/radio groups with listed options, pick the EXACT text of one of the available options.
- For hierarchical dropdown options (format "Category > SubOption"), pick the EXACT full path including the " > " separator.
- Use the section, current field value, sibling field names, and listed options together to infer meaning for short or generic labels.
- If a label is generic (for example "Overall", "Type", "Status", or "Source"), do NOT rely on label matching alone. Use section context plus the available options to choose the best answer.
- For dropdowns WITHOUT listed options, provide the value from the profile if available. If the field name closely matches a profile entry, use that value.
- For low-risk standardized screening fields, prefer a best-effort answer instead of "[NEEDS_USER_INPUT]". This includes referral/source fields, phone type, country/country of residence, work arrangement, relocation, and demographic/EEO fields.
- For low-risk standardized screening dropdowns/radios, use the saved profile/default answer and choose the closest matching option text if the wording differs slightly.
- For "How did you hear about us?" or similar source/referral fields: use the applicant profile value if available. If the profile has no source, default to "LinkedIn" (or the closest matching option like "Job Board", "Online Job Board", "Internet"). NEVER return "[NEEDS_USER_INPUT]" for referral source fields — they always have a safe default.
- For "Phone Device Type" or similar phone type fields: default to "Mobile" if the profile has no phone type. NEVER return "[NEEDS_USER_INPUT]" for phone type fields.
- For structured education fields (GPA, field of study, actual vs expected end date, scoped education start/end dates) and structured language fields ("Language", fluent, reading/writing, speaking/listening, overall proficiency), NEVER guess. Only answer when the exact structured profile value is present.
- When the profile wording and the site wording differ, map to the semantically closest visible option instead of escalating. Example: "Native / bilingual" may map to the top proficiency tier such as "Expert", "Advanced", or "Fluent".
- For work-setup/location preference fields, use the saved preferred work mode / preferred locations if present. If the field is broad and only asks for an arrangement, prefer the work-mode answer over escalating.
- For relocation fields, use the applicant's saved relocation preference; default to "Yes" when the profile has no contrary preference.
- For skill typeahead fields, return an ARRAY of relevant skills from the applicant profile.
- For multi-select fields, return a JSON array of ALL matching options (e.g., ["Python", "Java"]).
- For checkboxes/toggles, respond with "checked" or "unchecked".
- IMPORTANT: For agreement/consent checkboxes (e.g., "I agree", "I accept", "I understand", privacy policy, terms of service, candidate consent), ALWAYS respond with "checked". The applicant consents to standard application agreements.
- For file upload fields, skip them (don't include in output).
- For textarea fields, use an explicit open-ended answer from the applicant profile when available. If the profile does not contain that answer, use a semantically matched survey/profile value, structured education/experience fallback, deterministic default, or best-effort guess for required fields.
- For demographic/EEO fields (gender, race, ethnicity, veteran status, disability status, sexual orientation), use the applicant's actual info if provided. If no info is provided in the profile, use a neutral decline option: "I decline to self-identify", "I am not a protected veteran", or "I do not wish to answer" (pick whichever matches the available options). NEVER return "[NEEDS_USER_INPUT]" for EEO fields — always use a decline default.
- NEVER select a default placeholder value like "Select One", "Please select", etc.
- NEVER use placeholder strings like "N/A", "NA", "Not applicable", or "Unknown". Use the literal string "None" only when the question is asking about certifications/licenses and the applicant has none.
- For salary or compensation fields, use the saved salary expectation first. If the profile does not contain one, make a best-effort guess instead of returning "[NEEDS_USER_INPUT]".
- Use the EXACT field names shown above (including any "#N" suffix) as JSON keys.
- Only include fields you have a real answer for. For REQUIRED fields you must provide your best answer and never emit "[NEEDS_USER_INPUT]".
- Respond with ONLY a valid JSON object. No explanation, no markdown fences.

Example: {{"First Name": "Alex", "Cover Letter": "I am excited to apply because..."}}"""

    # Scale max_tokens based on field count — forms with many fields (e.g. 60+
    # on SmartRecruiters with 5 experience entries) need more output budget to
    # avoid truncation of long descriptions and other text fields.
    scaled_max_tokens = max(4096, min(len(fields) * 128, 16384))
    try:
        response = await llm.ainvoke(
            [UserMessage(content=prompt)],
            max_tokens=scaled_max_tokens,
        )
        text = response.completion if isinstance(response.completion, str) else ""
        input_tokens = response.usage.prompt_tokens if response.usage else 0
        output_tokens = response.usage.completion_tokens if response.usage else 0
        try:
            step_cost = estimate_cost(model_id, input_tokens, output_tokens)
        except Exception as e:
            logger.warning(f'Failed to estimate LLM cost for model "{model_id}": {e}')
            step_cost = 0.0
        if response.stop_reason == "max_tokens":
            logger.warning("LLM response was truncated (hit max_tokens).")
        logger.info(f"LLM answer response: {text[:200]}{'...' if len(text) > 200 else ''}")

        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.MULTILINE)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned, flags=re.MULTILINE).strip()
        parsed: dict[str, Any] = json.loads(cleaned)

        for k, v in list(parsed.items()):
            if isinstance(v, list):
                parsed[k] = ",".join(str(item) for item in v)
            elif isinstance(v, (int, float)):
                parsed[k] = str(v)

        _replace_placeholder_answers(parsed, fields, disambiguated_names)

        for i, field in enumerate(fields):
            key = disambiguated_names[i]
            if key in parsed and isinstance(parsed[key], str):
                parsed[key] = _sanitize_no_guess_answer(
                    field.name,
                    field.required,
                    parsed[key],
                    evidence,
                    field_type=field.field_type,
                    question_text=field.raw_label or field.name,
                )

        return parsed, input_tokens, output_tokens, step_cost, model_id
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM response as JSON, using empty answers")
        return {}, input_tokens, output_tokens, step_cost, model_id
    except Exception as e:
        logger.error(f"LLM answer generation failed: {e}")
        return {}, input_tokens, output_tokens, step_cost, model_id


async def infer_answers_for_fields(
    fields: list[FormField],
    *,
    profile_text: str | None = None,
    profile_data: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Infer answers for an arbitrary field set using DomHand's option-aware LLM path."""
    if not fields:
        return {}

    effective_profile_text = profile_text or _get_profile_text() or ""
    if not effective_profile_text and profile_data:
        effective_profile_text = json.dumps(profile_data)
    if not effective_profile_text:
        return {}

    answers, *_ = await _generate_answers(
        fields,
        effective_profile_text,
        profile_data=profile_data,
    )
    evidence = _parse_profile_evidence(effective_profile_text)
    display_names = _disambiguated_field_names(fields)
    resolved: dict[str, str] = {}

    for field, display_name in zip(fields, display_names, strict=False):
        proposed = answers.get(display_name)
        if proposed is None:
            proposed = _match_answer(field, answers, evidence, profile_data)
        coerced = _coerce_answer_to_field(field, str(proposed).strip() if proposed is not None else None)
        if not coerced or "[NEEDS_USER_INPUT]" in coerced.upper():
            continue
        resolved[field.field_id] = coerced

    return resolved


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
                continue  # Already determined no profile data — don't retry
            if key in fields_capped:
                continue
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
        for f in fillable_fields:
            if _field_has_effective_value(f):
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
                success, field_error, failure_reason = await _attempt_domhand_fill_with_retry_cap(
                    page,
                    host=page_host,
                    field=f,
                    desired_value=value,
                    tool_name="domhand_fill",
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
                    await _record_expected_value_if_settled(
                        page=page,
                        host=page_host,
                        page_context_key=page_context_key,
                        field=f,
                        field_key=key,
                        expected_value=value,
                        source="exact_profile" if resolved_value.source == "exact_profile" else "derived_profile",
                        log_context="domhand.fill",
                    )
                elif failure_reason == DOMHAND_RETRY_CAPPED:
                    fields_capped.add(key)
                all_results.append(fr)
                if _on_field_result:
                    _on_field_result(fr, round_num)
                fields_seen.add(key)
                round_filled += 1 if success else 0
                round_failed += 0 if success else 1

        for f in needs_llm:
            key = get_stable_field_key(f)
            resolved_value = _resolve_llm_answer_for_field(f, answers, evidence, profile_data)
            if resolved_value and re.match(
                r"^(n/?a|na|none|not applicable|unknown|placeholder)$",
                resolved_value.value.strip(),
                re.IGNORECASE,
            ):
                resolved_value = None
            if resolved_value and "[NEEDS_USER_INPUT]" in resolved_value.value:
                resolved_value = None
            if not resolved_value or not resolved_value.value:
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
            success, field_error, failure_reason = await _attempt_domhand_fill_with_retry_cap(
                page,
                host=page_host,
                field=f,
                desired_value=matched_answer,
                tool_name="domhand_fill",
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
                await _record_expected_value_if_settled(
                    page=page,
                    host=page_host,
                    page_context_key=page_context_key,
                    field=f,
                    field_key=key,
                    expected_value=matched_answer,
                    source="exact_profile" if resolved_value.source == "exact_profile" else "derived_profile",
                    log_context="domhand.fill",
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

    structured_summary = {
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
        "failed_fields": [_fill_result_summary_entry(r) for r in all_results if not r.success],
    }
    summary_lines.append("DOMHAND_FILL_JSON:")
    summary_lines.append(json.dumps(structured_summary, ensure_ascii=True))

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


async def _fill_single_field(page: Any, field: FormField, value: str) -> bool:
    ff_id = field.field_id
    tag = f"[{field.name or field.field_type}]"

    try:
        exists_json = await page.evaluate(_ELEMENT_EXISTS_JS, ff_id, field.field_type)
        if not json.loads(exists_json):
            logger.debug(f"skip {tag} (not visible)")
            return False
    except Exception:
        pass

    match field.field_type:
        case "text" | "email" | "tel" | "url" | "number" | "password" | "search":
            return await _fill_text_field(page, field, value, tag)
        case "date":
            return await _fill_date_field(page, field, value, tag)
        case "textarea":
            return await _fill_textarea_field(page, field, value, tag)
        case "select":
            return await _fill_select_field(page, field, value, tag)
        case "radio-group":
            return await _fill_radio_group(page, field, value, tag)
        case "radio":
            return await _fill_single_radio(page, field, value, tag)
        case "button-group":
            return await _fill_button_group(page, field, value, tag)
        case "checkbox-group":
            return await _fill_checkbox_group(page, field, value, tag)
        case "checkbox":
            return await _fill_checkbox(page, field, value, tag)
        case "toggle":
            return await _fill_toggle(page, field, value, tag)
        case _:
            return await _fill_text_field(page, field, value, tag)


async def _fill_text_field(page: Any, field: FormField, value: str, tag: str) -> bool:
    ff_id = field.field_id
    try:
        is_search_json = await page.evaluate(_IS_SEARCHABLE_DROPDOWN_JS, ff_id)
        if json.loads(is_search_json):
            return await _fill_searchable_dropdown(page, field, value, tag)
    except Exception:
        pass

    if not value:
        logger.debug(f"skip {tag} (no value)")
        return False

    for attempt_value in _text_fill_attempt_values(field, value):
        if _field_needs_enter_commit(field) and await _fill_text_like_with_keyboard(page, field, attempt_value, tag):
            logger.debug(
                f'fill {tag} = "{attempt_value[:80]}{"..." if len(attempt_value) > 80 else ""}" (keyboard-first)'
            )
            return True

        try:
            result_json = await page.evaluate(_FILL_FIELD_JS, ff_id, attempt_value, field.field_type)
            result = json.loads(result_json) if isinstance(result_json, str) else result_json
            if (
                isinstance(result, dict)
                and result.get("success")
                and await _confirm_text_like_value(page, field, attempt_value, tag)
            ):
                logger.debug(f'fill {tag} = "{attempt_value[:80]}{"..." if len(attempt_value) > 80 else ""}"')
                return True
        except Exception:
            pass

        try:
            if await _fill_text_like_with_keyboard(page, field, attempt_value, tag):
                logger.debug(f'fill {tag} = "{attempt_value[:80]}..." (keyboard)')
                return True
        except Exception:
            pass
    logger.debug(f"skip {tag} (not fillable)")
    return False


async def _fill_searchable_dropdown(page: Any, field: FormField, value: str, tag: str) -> bool:
    ff_id = field.field_id
    if not value:
        logger.debug(f"skip {tag} (searchable dropdown, no answer)")
        return False

    # Generate fallback search terms: "United States of America" → "United States" → "US"
    search_terms = generate_dropdown_search_terms(value)
    if not search_terms:
        search_terms = [value]

    for term_idx, search_term in enumerate(search_terms):
        try:
            await page.evaluate(
                r"""(ffId) => {
				var el = window.__ff ? window.__ff.byId(ffId) : null;
				if (el) el.click();
				return 'ok';
			}""",
                ff_id,
            )
            await asyncio.sleep(0.4)

            await page.evaluate(_FOCUS_AND_CLEAR_JS, ff_id)
            await asyncio.sleep(0.1)
            await page.evaluate(_FILL_FIELD_JS, ff_id, search_term, "text")
            await page.evaluate(
                r"""(ffId) => {
				var el = window.__ff ? window.__ff.byId(ffId) : null;
				if (el) { el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('keyup', {bubbles: true})); }
				return 'ok';
			}""",
                ff_id,
            )
            # Wait longer on first attempt, shorter on retries
            await asyncio.sleep(2.0 if term_idx == 0 else 1.5)

            clicked_json = await page.evaluate(_CLICK_DROPDOWN_OPTION_JS, value)
            clicked = json.loads(clicked_json)
            if clicked.get("clicked"):
                logger.debug(f'search-select {tag} -> "{clicked.get("text", value)}" (term: "{search_term}")')
                await _settle_dropdown_selection(page)
                return True

            # Also try clicking by the search term itself (may differ from value)
            if search_term != value:
                clicked_json = await page.evaluate(_CLICK_DROPDOWN_OPTION_JS, search_term)
                clicked = json.loads(clicked_json)
                if clicked.get("clicked"):
                    logger.debug(f'search-select {tag} -> "{clicked.get("text", search_term)}" (alt term)')
                    await _settle_dropdown_selection(page)
                    return True

            if term_idx < len(search_terms) - 1:
                logger.debug(f'search-select {tag}: "{search_term}" no match, trying next term')
                continue

            # Last resort: ArrowDown + Enter on the final term
            await page.press("ArrowDown")
            await asyncio.sleep(0.2)
            await page.press("Enter")
            logger.debug(f'search-select {tag} -> first result (keyboard, term: "{search_term}")')
            await _settle_dropdown_selection(page)
            return True
        except Exception as e:
            logger.debug(f'search-select {tag}: term "{search_term}" failed: {str(e)[:60]}')
            if term_idx < len(search_terms) - 1:
                continue
            return False

    return False


async def _open_grouped_date_picker(page: Any, field: FormField) -> bool:
    if not _is_grouped_date_field(field) or not field.has_calendar_trigger:
        return False
    try:
        raw = await page.evaluate(_OPEN_GROUPED_DATE_PICKER_JS, field.field_id)
    except Exception:
        return False
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return False
    return bool(isinstance(parsed, dict) and parsed.get("clicked") and parsed.get("opened"))


async def _select_grouped_date_from_picker(page: Any, field: FormField, year: int, month: int, day: int) -> bool:
    if not _is_grouped_date_field(field):
        return False
    month_name = date(year, month, min(day, 28)).strftime("%B")
    try:
        raw = await page.evaluate(_SELECT_GROUPED_DATE_PICKER_VALUE_JS, month_name, str(day), str(year))
    except Exception:
        return False
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return False
    return bool(isinstance(parsed, dict) and parsed.get("selected"))


async def _fill_grouped_date_field(page: Any, field: FormField, value: str, tag: str) -> bool:
    parsed = _parse_full_date_value(value)
    if not parsed or not _is_grouped_date_field(field):
        return False
    year, month, day = parsed
    if await _open_grouped_date_picker(page, field):
        if await _select_grouped_date_from_picker(page, field, year, month, day):
            expected_display = f"{month:02d}/{day:02d}/{year}"
            if await _confirm_text_like_value(page, field, expected_display, tag):
                logger.debug(f'fill {tag} = "{expected_display}" (picker)')
                return True
        try:
            selector = f'[data-ff-id="{field.field_id}"]'
            await page.press(selector, "Escape")
            await asyncio.sleep(0.15)
        except Exception:
            pass

    component_ids = list(field.component_field_ids)
    component_values = [str(month).zfill(2), str(day).zfill(2), str(year)]
    for component_id, component_value in zip(component_ids[:3], component_values, strict=False):
        try:
            result_json = await page.evaluate(_FILL_FIELD_JS, component_id, component_value, "text")
            result = json.loads(result_json) if isinstance(result_json, str) else result_json
            if not isinstance(result, dict) or not result.get("success"):
                return False
            await asyncio.sleep(0.08)
        except Exception:
            return False

    expected_display = f"{month:02d}/{day:02d}/{year}"
    last_component_id = component_ids[2]
    try:
        selector = f'[data-ff-id="{last_component_id}"]'
        await page.press(selector, "Tab")
        await asyncio.sleep(0.2)
    except Exception:
        pass
    await _click_away_from_text_like_field(page, last_component_id)
    await asyncio.sleep(0.15)
    if await _confirm_text_like_value(page, field, expected_display, tag):
        logger.debug(f'fill {tag} = "{expected_display}" (grouped)')
        return True
    return False


async def _fill_date_field(page: Any, field: FormField, value: str, tag: str) -> bool:
    val = (value or "").strip()
    if not val:
        logger.debug(f"skip {tag} (date, no value)")
        return False

    if _is_grouped_date_field(field):
        if await _fill_grouped_date_field(page, field, val, tag):
            return True
        logger.debug(f"skip {tag} (grouped date not fillable)")
        return False

    # Try multiple date format variations for resilience
    date_variants = [val]
    # If value looks like YYYY-MM, also try MM/YYYY
    if re.match(r"^\d{4}-\d{2}$", val):
        parts = val.split("-")
        date_variants.append(f"{parts[1]}/{parts[0]}")  # MM/YYYY
    # If value looks like MM/YYYY, also try YYYY-MM
    elif re.match(r"^\d{2}/\d{4}$", val):
        parts = val.split("/")
        date_variants.append(f"{parts[1]}-{parts[0]}")  # YYYY-MM

    for attempt_val in date_variants:
        if await _fill_text_like_with_keyboard(page, field, attempt_val, tag):
            # Dismiss any calendar popup (Escape) then commit the value (Tab)
            try:
                selector = f'[data-ff-id="{field.field_id}"]'
                await page.press(selector, "Escape")
                await asyncio.sleep(0.15)
                await page.press(selector, "Tab")
                await asyncio.sleep(0.3)
            except Exception:
                pass
            logger.debug(f'fill {tag} = "{attempt_val}" (keyboard-first)')
            return True
        try:
            result_json = await page.evaluate(_FILL_FIELD_JS, field.field_id, attempt_val, "text")
            result = json.loads(result_json) if isinstance(result_json, str) else result_json
            if (
                isinstance(result, dict)
                and result.get("success")
                and await _confirm_text_like_value(page, field, attempt_val, tag)
            ):
                logger.debug(f'fill {tag} = "{attempt_val}"')
                return True
        except Exception:
            pass

    # Final attempt: special date JS fill with original value
    try:
        result_json = await page.evaluate(_FILL_DATE_JS, field.field_id, val)
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
        if isinstance(result, dict) and result.get("success") and await _confirm_text_like_value(page, field, val, tag):
            logger.debug(f'fill {tag} = "{val}" (direct)')
            return True
    except Exception:
        pass
    logger.debug(f"skip {tag} (date not fillable)")
    return False


async def _fill_textarea_field(page: Any, field: FormField, value: str, tag: str) -> bool:
    if not value:
        logger.debug(f"skip {tag} (no value)")
        return False
    try:
        result_json = await page.evaluate(_FILL_FIELD_JS, field.field_id, value, "textarea")
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
        if isinstance(result, dict) and result.get("success"):
            logger.debug(f'fill {tag} = "{value[:80]}{"..." if len(value) > 80 else ""}"')
            return True
    except Exception:
        pass
    try:
        result_json = await page.evaluate(_FILL_CONTENTEDITABLE_JS, field.field_id, value)
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
        if isinstance(result, dict) and result.get("success"):
            logger.debug(f'fill {tag} = "{value[:80]}..." (contenteditable)')
            return True
    except Exception:
        pass
    logger.debug(f"skip {tag} (textarea not fillable)")
    return False


async def _fill_select_field(page: Any, field: FormField, value: str, tag: str) -> bool:
    if not value:
        logger.debug(f"skip {tag} (no value)")
        return False
    if field.is_native:
        try:
            result_json = await page.evaluate(_FILL_FIELD_JS, field.field_id, value, "select")
            result = json.loads(result_json) if isinstance(result_json, str) else result_json
            if isinstance(result, dict) and result.get("success"):
                logger.debug(f'select {tag} -> "{value}"')
                return True
        except Exception:
            pass
        logger.debug(f"skip {tag} (native select failed)")
        return False

    is_skill = _is_skill_like(field.name)
    all_values = [v.strip() for v in value.split(",") if v.strip()]
    values = all_values[:_SKILL_FIELD_MAX_ITEMS] if is_skill else all_values
    if len(values) > 1 or is_skill:
        return await _fill_multi_select(page, field, values, tag)
    return await _fill_custom_dropdown(page, field, value, tag)


async def _fill_multi_select(page: Any, field: FormField, values: list[str], tag: str) -> bool:
    ff_id = field.field_id
    try:
        await page.evaluate(
            r"""(ffId) => {
			var ff = window.__ff; var el = ff ? ff.byId(ffId) : null;
			if (el) el.click(); return 'ok';
		}""",
            ff_id,
        )
        await asyncio.sleep(0.6)

        picked_count = 0
        for val in values:
            await page.evaluate(_FILL_FIELD_JS, ff_id, val, "text")
            await asyncio.sleep(0.3)
            try:
                clicked_json = await page.evaluate(_CLICK_DROPDOWN_OPTION_JS, val)
                clicked = json.loads(clicked_json)
                if clicked.get("clicked"):
                    picked_count += 1
                    await asyncio.sleep(0.2)
                    continue
            except Exception:
                pass
            await page.press("Enter")
            await asyncio.sleep(0.3)
            picked_count += 1

        try:
            await page.evaluate(_DISMISS_DROPDOWN_JS)
        except Exception:
            pass
        if picked_count > 0:
            logger.debug(f"multi-select {tag} -> {picked_count}/{len(values)} options")
            return True
    except Exception as e:
        logger.debug(f"multi-select {tag} failed: {str(e)[:60]}")
    return False


async def _click_dropdown_option(page: Any, text: str) -> dict[str, Any]:
    """Click a visible dropdown option by text."""
    try:
        raw_result = await page.evaluate(_CLICK_DROPDOWN_OPTION_JS, text)
    except Exception:
        return {"clicked": False}
    return _parse_dropdown_click_result(raw_result)


async def _clear_dropdown_search(page: Any) -> None:
    """Clear the current searchable dropdown query if one is focused."""
    for shortcut in ("Meta+A", "Control+A"):
        try:
            await page.keyboard.press(shortcut)
        except Exception:
            pass
    try:
        await page.keyboard.press("Backspace")
    except Exception:
        pass
    await asyncio.sleep(0.15)


async def _settle_dropdown_selection(page: Any, delay: float = 0.45) -> None:
    """Dismiss an open dropdown and give the UI time to commit the selection."""
    try:
        await page.evaluate(_DISMISS_DROPDOWN_JS)
    except Exception:
        pass
    await asyncio.sleep(delay)


async def _visible_field_id_snapshot(page: Any) -> set[str]:
    try:
        fields = await extract_visible_form_fields(page)
    except Exception:
        return set()
    return {str(field.field_id).strip() for field in fields if str(field.field_id).strip()}


async def _type_and_click_dropdown_option(page: Any, value: str, tag: str) -> dict[str, Any]:
    """Type search terms into an open dropdown and click the best visible match."""
    for idx, term in enumerate(generate_dropdown_search_terms(value)):
        try:
            if idx > 0:
                await _clear_dropdown_search(page)
            await page.keyboard.type(term, delay=45)
            await asyncio.sleep(0.3)
            clicked = await _click_dropdown_option(page, value)
            if clicked.get("clicked"):
                logger.debug(f'select {tag} -> "{clicked.get("text", value)}" (typed search)')
                return clicked
            await page.keyboard.press("Enter")
            await asyncio.sleep(1.1)
            clicked = await _click_dropdown_option(page, value)
            if clicked.get("clicked"):
                logger.debug(f'select {tag} -> "{clicked.get("text", value)}" (typed search)')
                return clicked
            clicked = await _click_dropdown_option(page, term)
            if clicked.get("clicked"):
                logger.debug(f'select {tag} -> "{clicked.get("text", term)}" (typed search)')
                return clicked
        except Exception as e:
            logger.debug(f'dropdown search {tag} term "{term}" failed: {str(e)[:60]}')
    return {"clicked": False}


async def _fill_custom_dropdown(page: Any, field: FormField, value: str, tag: str) -> bool:
    ff_id = field.field_id
    pre_value = await _read_field_value_for_field(page, field)
    visible_before = await _visible_field_id_snapshot(page)
    open_succeeded = False
    selection_stuck = False
    post_value = pre_value
    follow_up_appeared = False
    try:
        await page.evaluate(
            r"""(ffId) => {
			var ff = window.__ff; var el = ff ? ff.byId(ffId) : null;
			if (el) el.click(); return 'ok';
		}""",
            ff_id,
        )
        open_succeeded = True
        await asyncio.sleep(0.6)

        clicked = await _click_dropdown_option(page, value)
        if clicked.get("clicked"):
            current = await _wait_for_field_value(page, field, value)
            if _field_value_matches_expected(current, value):
                post_value = current
                selection_stuck = True
                logger.debug(f'select {tag} -> "{clicked.get("text", value)}"')
                await _settle_dropdown_selection(page)
                return True

        segments = split_dropdown_value_hierarchy(value)
        if len(segments) > 1:
            for idx, segment in enumerate(segments):
                clicked = await _click_dropdown_option(page, segment)
                if not clicked.get("clicked"):
                    clicked = await _type_and_click_dropdown_option(page, segment, tag)
                if not clicked.get("clicked"):
                    raise RuntimeError(f'No hierarchical dropdown match for "{segment}"')
                await asyncio.sleep(0.8 if idx < len(segments) - 1 else 0.45)
            current = await _wait_for_field_value(page, field, value, timeout=2.8)
            if _field_value_matches_expected(current, value):
                post_value = current
                selection_stuck = True
                logger.debug(f'select {tag} -> "{value}" (hierarchy)')
                await _settle_dropdown_selection(page, delay=0.6)
                return True

        clicked = await _type_and_click_dropdown_option(page, value, tag)
        if clicked.get("clicked"):
            current = await _wait_for_field_value(page, field, value)
            if _field_value_matches_expected(current, value):
                post_value = current
                selection_stuck = True
                await _settle_dropdown_selection(page)
                return True

        await page.press("ArrowDown")
        await asyncio.sleep(0.25)
        await page.press("Enter")
        current = await _wait_for_field_value(page, field, value, timeout=1.4)
        if _field_value_matches_expected(current, value):
            post_value = current
            selection_stuck = True
            logger.debug(f"select {tag} -> first option (keyboard)")
            await _settle_dropdown_selection(page)
            return True
        raise RuntimeError(f'Dropdown value did not settle to "{value}"')
    except Exception as e:
        try:
            await page.evaluate(_DISMISS_DROPDOWN_JS)
        except Exception:
            pass
        logger.debug(f"skip {tag} (custom dropdown failed: {str(e)[:60]})")
        return False
    finally:
        if not selection_stuck:
            post_value = await _read_field_value_for_field(page, field)
        visible_after = await _visible_field_id_snapshot(page)
        follow_up_appeared = bool(visible_after - visible_before - {field.field_id})
        logger.info(
            "domhand.custom_widget_attempt",
            extra={
                "field_id": field.field_id,
                "field_label": _preferred_field_label(field),
                "desired_value": value,
                "pre_value": pre_value,
                "post_value": post_value,
                "open_succeeded": open_succeeded,
                "selection_stuck": selection_stuck,
                "follow_up_appeared": follow_up_appeared,
                "widget_kind": _field_widget_kind_for_debug(field),
            },
        )


async def _fill_radio_group(page: Any, field: FormField, value: str, tag: str) -> bool:
    choice = value or (field.choices[0] if field.choices else "")
    if not choice:
        logger.debug(f"skip {tag} (radio-group, no answer)")
        return False
    recipe_context = await _load_field_interaction_recipe(page, field)
    current = await _read_group_selection(page, field.field_id)
    if _field_value_matches_expected(current, choice) and not await _field_has_validation_error(page, field.field_id):
        logger.debug(f"skip {tag} (already selected)")
        return True
    recipe = recipe_context.get("recipe") if recipe_context else None
    if recipe is not None:
        if "group_option_reset" in recipe.preferred_action_chain and current:
            if await _reset_group_selection_with_gui(page, field, current, choice, tag):
                _trace_profile_resolution(
                    "domhand.field_recipe_applied",
                    field_label=_preferred_field_label(field),
                    widget_signature=field.field_type,
                    preferred_action_chain="group_option_reset,group_option_gui_click",
                )
                return True
        if "group_option_gui_click" in recipe.preferred_action_chain:
            if await _click_group_option_with_gui(page, field, choice, tag) and not await _field_has_validation_error(
                page,
                field.field_id,
            ):
                _trace_profile_resolution(
                    "domhand.field_recipe_applied",
                    field_label=_preferred_field_label(field),
                    widget_signature=field.field_type,
                    preferred_action_chain="group_option_gui_click",
                )
                return True
    try:
        result_json = await page.evaluate(_CLICK_RADIO_OPTION_JS, field.field_id, choice)
        result = json.loads(result_json)
        if result.get("clicked"):
            current = await _read_group_selection(page, field.field_id)
            if _field_value_matches_expected(current, choice) and not await _field_has_validation_error(
                page, field.field_id
            ):
                logger.debug(f'radio {tag} -> "{choice}"')
                return True
    except Exception:
        pass
    try:
        result_json = await page.evaluate(_CLICK_RADIO_OPTION_JS, field.field_id, choice)
        result = json.loads(result_json)
        if result.get("clicked"):
            current = await _read_group_selection(page, field.field_id)
            if _field_value_matches_expected(current, choice) and not await _field_has_validation_error(
                page, field.field_id
            ):
                logger.debug(f'radio {tag} -> "{choice}" (retry)')
                return True
    except Exception:
        pass
    if await _click_group_option_with_gui(page, field, choice, tag) and not await _field_has_validation_error(
        page, field.field_id
    ):
        _record_field_interaction_recipe(recipe_context, ["group_option_gui_click"])
        return True
    current = await _read_group_selection(page, field.field_id)
    if (_field_value_matches_expected(current, choice) and await _field_has_validation_error(page, field.field_id)) or (
        current and not _field_value_matches_expected(current, choice)
    ):
        if await _reset_group_selection_with_gui(page, field, current, choice, tag):
            _record_field_interaction_recipe(
                recipe_context,
                ["group_option_reset", "group_option_gui_click"],
            )
            return True
    logger.debug(f"skip {tag} (no matching radio option)")
    return False


async def _fill_single_radio(page: Any, field: FormField, value: str, tag: str) -> bool:
    if not value:
        logger.debug(f"skip {tag} (radio, no answer)")
        return False
    recipe_context = await _load_field_interaction_recipe(page, field)
    current = await _read_group_selection(page, field.field_id)
    if _field_value_matches_expected(current, value) and not await _field_has_validation_error(page, field.field_id):
        logger.debug(f"skip {tag} (already selected)")
        return True
    recipe = recipe_context.get("recipe") if recipe_context else None
    if recipe is not None:
        if "group_option_reset" in recipe.preferred_action_chain and current:
            if await _reset_group_selection_with_gui(page, field, current, value, tag):
                _trace_profile_resolution(
                    "domhand.field_recipe_applied",
                    field_label=_preferred_field_label(field),
                    widget_signature=field.field_type,
                    preferred_action_chain="group_option_reset,group_option_gui_click",
                )
                return True
        if "group_option_gui_click" in recipe.preferred_action_chain:
            if await _click_group_option_with_gui(page, field, value, tag) and not await _field_has_validation_error(
                page,
                field.field_id,
            ):
                _trace_profile_resolution(
                    "domhand.field_recipe_applied",
                    field_label=_preferred_field_label(field),
                    widget_signature=field.field_type,
                    preferred_action_chain="group_option_gui_click",
                )
                return True
    try:
        result_json = await page.evaluate(_CLICK_SINGLE_RADIO_JS, field.field_id, value)
        result = json.loads(result_json)
        if result.get("clicked"):
            if result.get("alreadyChecked"):
                if not await _field_has_validation_error(page, field.field_id):
                    logger.debug(f"skip {tag} (already selected)")
                    return True
            current = await _read_group_selection(page, field.field_id)
            if _field_value_matches_expected(current, value) and not await _field_has_validation_error(
                page, field.field_id
            ):
                logger.debug(f'radio {tag} -> "{value}"')
                return True
    except Exception:
        pass
    if await _click_group_option_with_gui(page, field, value, tag) and not await _field_has_validation_error(
        page, field.field_id
    ):
        _record_field_interaction_recipe(recipe_context, ["group_option_gui_click"])
        return True
    current = await _read_group_selection(page, field.field_id)
    if (_field_value_matches_expected(current, value) and await _field_has_validation_error(page, field.field_id)) or (
        current and not _field_value_matches_expected(current, value)
    ):
        if await _reset_group_selection_with_gui(page, field, current, value, tag):
            _record_field_interaction_recipe(
                recipe_context,
                ["group_option_reset", "group_option_gui_click"],
            )
            return True
    logger.debug(f'skip {tag} (no matching radio for "{value}")')
    return False


async def _fill_button_group(page: Any, field: FormField, value: str, tag: str) -> bool:
    choice = value or (field.choices[0] if field.choices else "")
    if not choice:
        logger.debug(f"skip {tag} (button-group, no answer)")
        return False
    recipe_context = await _load_field_interaction_recipe(page, field)
    current = await _read_group_selection(page, field.field_id)
    if _field_value_matches_expected(current, choice) and not await _field_has_validation_error(page, field.field_id):
        logger.debug(f"skip {tag} (already selected)")
        return True
    recipe = recipe_context.get("recipe") if recipe_context else None
    if recipe is not None:
        if "group_option_reset" in recipe.preferred_action_chain and current:
            if await _reset_group_selection_with_gui(page, field, current, choice, tag):
                _trace_profile_resolution(
                    "domhand.field_recipe_applied",
                    field_label=_preferred_field_label(field),
                    widget_signature=field.field_type,
                    preferred_action_chain="group_option_reset,group_option_gui_click",
                )
                return True
        if "group_option_gui_click" in recipe.preferred_action_chain:
            if await _click_group_option_with_gui(page, field, choice, tag) and not await _field_has_validation_error(
                page,
                field.field_id,
            ):
                _trace_profile_resolution(
                    "domhand.field_recipe_applied",
                    field_label=_preferred_field_label(field),
                    widget_signature=field.field_type,
                    preferred_action_chain="group_option_gui_click",
                )
                return True
    try:
        result_json = await page.evaluate(_CLICK_BUTTON_GROUP_JS, field.field_id, choice)
        result = json.loads(result_json)
        if result.get("clicked"):
            current = await _read_group_selection(page, field.field_id)
            if _field_value_matches_expected(current, choice) and not await _field_has_validation_error(
                page, field.field_id
            ):
                logger.debug(f'button-group {tag} -> "{choice}"')
                return True
    except Exception:
        pass
    try:
        result_json = await page.evaluate(_CLICK_BUTTON_GROUP_JS, field.field_id, choice)
        result = json.loads(result_json)
        if result.get("clicked"):
            current = await _read_group_selection(page, field.field_id)
            if _field_value_matches_expected(current, choice) and not await _field_has_validation_error(
                page, field.field_id
            ):
                logger.debug(f'button-group {tag} -> "{choice}" (retry)')
                return True
    except Exception:
        pass
    if await _click_group_option_with_gui(page, field, choice, tag) and not await _field_has_validation_error(
        page, field.field_id
    ):
        _record_field_interaction_recipe(recipe_context, ["group_option_gui_click"])
        return True
    current = await _read_group_selection(page, field.field_id)
    if (_field_value_matches_expected(current, choice) and await _field_has_validation_error(page, field.field_id)) or (
        current and not _field_value_matches_expected(current, choice)
    ):
        if await _reset_group_selection_with_gui(page, field, current, choice, tag):
            _record_field_interaction_recipe(
                recipe_context,
                ["group_option_reset", "group_option_gui_click"],
            )
            return True
    logger.debug(f"skip {tag} (button-group, no matching button)")
    return False


async def _fill_checkbox_group(page: Any, field: FormField, value: str, tag: str) -> bool:
    if not _checkbox_group_is_exclusive_choice(field):
        if _is_explicit_false(value):
            logger.debug(f"check {tag} -> skip (answer=unchecked)")
            return True
        recipe_context = await _load_field_interaction_recipe(page, field)
        recipe = recipe_context.get("recipe") if recipe_context else None
        if recipe is not None:
            if "binary_refresh" in recipe.preferred_action_chain:
                if await _refresh_binary_field(page, field, tag, True) and not await _field_has_validation_error(
                    page,
                    field.field_id,
                ):
                    _trace_profile_resolution(
                        "domhand.field_recipe_applied",
                        field_label=_preferred_field_label(field),
                        widget_signature=field.field_type,
                        preferred_action_chain="binary_refresh,binary_gui_click",
                    )
                    return True
            if "binary_gui_click" in recipe.preferred_action_chain:
                if await _click_binary_with_gui(page, field, tag, True) and not await _field_has_validation_error(
                    page,
                    field.field_id,
                ):
                    _trace_profile_resolution(
                        "domhand.field_recipe_applied",
                        field_label=_preferred_field_label(field),
                        widget_signature=field.field_type,
                        preferred_action_chain="binary_gui_click",
                    )
                    return True
        try:
            result_json = await page.evaluate(_CLICK_CHECKBOX_GROUP_JS, field.field_id)
            result = json.loads(result_json)
            if result.get("clicked"):
                current = await _read_binary_state(page, field.field_id)
                if result.get("alreadyChecked") and not await _field_has_validation_error(page, field.field_id):
                    logger.debug(f"skip {tag} (already checked)")
                    return True
                if current is True and not await _field_has_validation_error(page, field.field_id):
                    logger.debug(f"check {tag} -> first")
                    return True
                if current is True and await _field_has_validation_error(page, field.field_id):
                    if await _refresh_binary_field(page, field, tag, True):
                        _record_field_interaction_recipe(recipe_context, ["binary_refresh", "binary_gui_click"])
                        return True
                if await _click_binary_with_gui(page, field, tag, True):
                    _record_field_interaction_recipe(recipe_context, ["binary_gui_click"])
                    return True
        except Exception:
            pass
        logger.debug(f"skip {tag} (checkbox-group)")
        return False

    choice = value or (field.choices[0] if field.choices else "")
    if not choice:
        logger.debug(f"skip {tag} (checkbox-group, no answer)")
        return False
    recipe_context = await _load_field_interaction_recipe(page, field)
    current = await _read_group_selection(page, field.field_id)
    if _field_value_matches_expected(current, choice) and not await _field_has_validation_error(page, field.field_id):
        logger.debug(f"skip {tag} (already selected)")
        return True
    recipe = recipe_context.get("recipe") if recipe_context else None
    if recipe is not None:
        if "group_option_reset" in recipe.preferred_action_chain and current:
            if await _reset_group_selection_with_gui(page, field, current, choice, tag):
                _trace_profile_resolution(
                    "domhand.field_recipe_applied",
                    field_label=_preferred_field_label(field),
                    widget_signature=field.field_type,
                    preferred_action_chain="group_option_reset,group_option_gui_click",
                )
                return True
        if "group_option_gui_click" in recipe.preferred_action_chain:
            if await _click_group_option_with_gui(page, field, choice, tag) and not await _field_has_validation_error(
                page,
                field.field_id,
            ):
                _trace_profile_resolution(
                    "domhand.field_recipe_applied",
                    field_label=_preferred_field_label(field),
                    widget_signature=field.field_type,
                    preferred_action_chain="group_option_gui_click",
                )
                return True
    try:
        result_json = await page.evaluate(_CLICK_RADIO_OPTION_JS, field.field_id, choice)
        result = json.loads(result_json)
        if result.get("clicked"):
            current = await _read_group_selection(page, field.field_id)
            if _field_value_matches_expected(current, choice) and not await _field_has_validation_error(
                page, field.field_id
            ):
                logger.debug(f'exclusive-checkbox-group {tag} -> "{choice}"')
                return True
    except Exception:
        pass
    if await _click_group_option_with_gui(page, field, choice, tag) and not await _field_has_validation_error(
        page, field.field_id
    ):
        _record_field_interaction_recipe(recipe_context, ["group_option_gui_click"])
        return True
    current = await _read_group_selection(page, field.field_id)
    if (_field_value_matches_expected(current, choice) and await _field_has_validation_error(page, field.field_id)) or (
        current and not _field_value_matches_expected(current, choice)
    ):
        if await _reset_group_selection_with_gui(page, field, current, choice, tag):
            _record_field_interaction_recipe(
                recipe_context,
                ["group_option_reset", "group_option_gui_click"],
            )
            return True
    logger.debug(f"skip {tag} (exclusive checkbox-group)")
    return False


async def _fill_checkbox(page: Any, field: FormField, value: str, tag: str) -> bool:
    desired_checked = not _is_explicit_false(value)
    if not desired_checked:
        logger.debug(f"check {tag} -> skip (answer=unchecked)")
        return True
    recipe_context = await _load_field_interaction_recipe(page, field)
    state = await _read_binary_state(page, field.field_id)
    if state is True and not await _field_has_validation_error(page, field.field_id):
        logger.debug(f"skip {tag} (already checked)")
        return True
    recipe = recipe_context.get("recipe") if recipe_context else None
    if recipe is not None:
        if "binary_refresh" in recipe.preferred_action_chain:
            if await _refresh_binary_field(page, field, tag, desired_checked) and not await _field_has_validation_error(
                page,
                field.field_id,
            ):
                _trace_profile_resolution(
                    "domhand.field_recipe_applied",
                    field_label=_preferred_field_label(field),
                    widget_signature=field.field_type,
                    preferred_action_chain="binary_refresh,binary_gui_click",
                )
                return True
        if "binary_gui_click" in recipe.preferred_action_chain:
            if await _click_binary_with_gui(
                page, field, tag, desired_checked
            ) and not await _field_has_validation_error(
                page,
                field.field_id,
            ):
                _trace_profile_resolution(
                    "domhand.field_recipe_applied",
                    field_label=_preferred_field_label(field),
                    widget_signature=field.field_type,
                    preferred_action_chain="binary_gui_click",
                )
                return True
    for attempt in range(2):
        try:
            result_json = await page.evaluate(_CLICK_BINARY_FIELD_JS, field.field_id, desired_checked)
            result = json.loads(result_json) if isinstance(result_json, str) else result_json
            if isinstance(result, dict) and result.get("clicked"):
                await asyncio.sleep(0.25)
                state = await _read_binary_state(page, field.field_id)
                if state is desired_checked and not await _field_has_validation_error(page, field.field_id):
                    logger.debug(f"check {tag}{' (retry)' if attempt else ''}")
                    return True
        except Exception:
            pass
    if await _click_binary_with_gui(page, field, tag, desired_checked) and not await _field_has_validation_error(
        page, field.field_id
    ):
        _record_field_interaction_recipe(recipe_context, ["binary_gui_click"])
        return True
    if await _refresh_binary_field(page, field, tag, desired_checked) and not await _field_has_validation_error(
        page, field.field_id
    ):
        _record_field_interaction_recipe(recipe_context, ["binary_refresh", "binary_gui_click"])
        return True
    logger.debug(f"skip {tag} (did not remain checked)")
    return False


async def _fill_toggle(page: Any, field: FormField, value: str, tag: str) -> bool:
    desired_on = not _is_explicit_false(value)
    if not desired_on:
        logger.debug(f"toggle {tag} -> skip (answer=off)")
        return True
    recipe_context = await _load_field_interaction_recipe(page, field)
    state = await _read_binary_state(page, field.field_id)
    if state is True and not await _field_has_validation_error(page, field.field_id):
        logger.debug(f"skip {tag} (already on)")
        return True
    recipe = recipe_context.get("recipe") if recipe_context else None
    if recipe is not None:
        if "binary_refresh" in recipe.preferred_action_chain:
            if await _refresh_binary_field(page, field, tag, desired_on) and not await _field_has_validation_error(
                page,
                field.field_id,
            ):
                _trace_profile_resolution(
                    "domhand.field_recipe_applied",
                    field_label=_preferred_field_label(field),
                    widget_signature=field.field_type,
                    preferred_action_chain="binary_refresh,binary_gui_click",
                )
                return True
        if "binary_gui_click" in recipe.preferred_action_chain:
            if await _click_binary_with_gui(page, field, tag, desired_on) and not await _field_has_validation_error(
                page,
                field.field_id,
            ):
                _trace_profile_resolution(
                    "domhand.field_recipe_applied",
                    field_label=_preferred_field_label(field),
                    widget_signature=field.field_type,
                    preferred_action_chain="binary_gui_click",
                )
                return True
    for attempt in range(2):
        try:
            result_json = await page.evaluate(_CLICK_BINARY_FIELD_JS, field.field_id, desired_on)
            result = json.loads(result_json) if isinstance(result_json, str) else result_json
            if isinstance(result, dict) and result.get("clicked"):
                await asyncio.sleep(0.25)
                state = await _read_binary_state(page, field.field_id)
                if state is desired_on and not await _field_has_validation_error(page, field.field_id):
                    logger.debug(f"toggle {tag} -> on{' (retry)' if attempt else ''}")
                    return True
        except Exception:
            pass
    if await _click_binary_with_gui(page, field, tag, desired_on) and not await _field_has_validation_error(
        page, field.field_id
    ):
        _record_field_interaction_recipe(recipe_context, ["binary_gui_click"])
        return True
    if await _refresh_binary_field(page, field, tag, desired_on) and not await _field_has_validation_error(
        page, field.field_id
    ):
        _record_field_interaction_recipe(recipe_context, ["binary_refresh", "binary_gui_click"])
        return True
    logger.debug(f"skip {tag} (did not remain on)")
    return False


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
