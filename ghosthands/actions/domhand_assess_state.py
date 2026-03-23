"""Runtime application-state assessment for browser-use job-application flows."""

import asyncio
import json
import logging
import os
import re
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from ghosthands.actions.domhand_fill import (
    _OPAQUE_WIDGET_VALUE_RE,
    _build_inject_helpers_js,
    _filter_fields_for_scope,
    extract_visible_form_fields,
    _field_has_validation_error,
    _field_value_matches_expected,
    _is_effectively_unset_field_value,
    _is_navigation_field,
    _preferred_field_label,
    _read_checkbox_group_value,
    _read_binary_state,
    _read_field_value,
    _read_field_value_for_field,
    _read_group_selection,
    _safe_page_url,
    _section_matches_scope,
    _get_page_context_key,
    _grouped_date_is_complete,
    _value_shape_is_compatible,
)
from ghosthands.actions.views import (
    ApplicationFieldIssue,
    ApplicationState,
    DomHandAssessStateParams,
    FormField,
    get_stable_field_key,
    is_placeholder_value,
    normalize_name,
)
from ghosthands.step_trace import publish_browser_session_trace
from ghosthands.platforms import detect_platform_from_signals, get_config_by_name
from ghosthands.runtime_learning import detect_host_from_url, get_expected_field_value

logger = logging.getLogger(__name__)


def _verification_attempt_count() -> int:
    effort = os.getenv("GH_VERIFICATION_EFFORT", "medium").strip().lower()
    if effort == "low":
        return 1
    if effort == "high":
        return 3
    return 2


def _field_current_value_is_opaque(field: FormField) -> bool:
    value = str(field.current_value or "").strip()
    if not value:
        return False
    if field.field_type == "select" and _OPAQUE_WIDGET_VALUE_RE.match(value):
        return True
    return bool(is_placeholder_value(value))


_SEMANTIC_VERIFICATION_HINTS = (
    "why",
    "want to work",
    "want this job",
    "desired start date",
    "start date",
    "notice",
    "relocation",
    "salary",
    "compensation",
    "how did you hear",
    "hear about us",
    "location to which you are willing to relocate",
)

_STRICT_VERIFICATION_HINTS = (
    "legal name",
    "first name",
    "last name",
    "email",
    "password",
    "address",
    "city",
    "postal code",
    "zip",
    "phone",
    "school",
    "university",
    "degree",
    "graduation",
    "country",
    "visa",
    "sponsorship",
    "authorized to work",
    "legally permitted to work",
)

_STRICT_VERIFICATION_WORD_BOUNDARY_HINTS = (
    "state",
)


def _label_has_strict_hint(label: str) -> bool:
    """Check if a normalized label contains a strict verification hint.

    Plain substring match for multi-word hints; word-boundary regex for
    single-word hints that are ambiguous (e.g. 'state' should match
    'state/province' but not 'please state your expectations').
    """
    if any(token in label for token in _STRICT_VERIFICATION_HINTS):
        return True
    for token in _STRICT_VERIFICATION_WORD_BOUNDARY_HINTS:
        if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", label):
            return True
    return False

_COMPANION_BOOLEAN_FIELD_TYPES = {
    "checkbox",
    "checkbox-group",
    "radio",
    "radio-group",
    "toggle",
    "button-group",
}


def _field_uses_semantic_verification(field: FormField) -> bool:
    if field.field_type not in {"text", "textarea"}:
        return False
    label = normalize_name(field.raw_label or _preferred_field_label(field) or "")
    if not label:
        return False
    if _label_has_strict_hint(label):
        return False
    return field.field_type == "textarea" or any(token in label for token in _SEMANTIC_VERIFICATION_HINTS)


def _field_type_family(field_type: str) -> str:
    normalized = normalize_name(field_type)
    if normalized in {"checkbox", "checkbox group", "radio", "radio group", "toggle", "button group"}:
        return "binary"
    if normalized in {"select", "combobox", "listbox"}:
        return "select"
    if normalized in {"text", "email", "tel", "url", "number", "search"}:
        return "text"
    if normalized in {"textarea"}:
        return "textarea"
    if normalized in {"date"}:
        return "date"
    return normalized or "unknown"


def _field_label_tokens(text: str | None) -> set[str]:
    return {token for token in normalize_name(text or "").split() if token}


def _expected_cluster_signature(label: str | None, field_type: str | None) -> tuple[str | None, str | None]:
    normalized_label = normalize_name(label or "")
    family = _field_type_family(field_type or "")
    is_binary = family == "binary"

    if any(token in normalized_label for token in ("relocation", "relocate")):
        if any(token in normalized_label for token in ("location to which", "preferred location", "where would you relocate")):
            return "relocation", "location_child"
        if is_binary or family == "select":
            return "relocation", "boolean_parent"
        return "relocation", "detail_child"

    if "visa" in normalized_label or "sponsorship" in normalized_label:
        if any(token in normalized_label for token in ("please specify", "type of visa", "what visa", "which visa")):
            return "visa_sponsorship", "detail_child"
        if is_binary or family == "select":
            return "visa_sponsorship", "boolean_parent"
        return "visa_sponsorship", "detail_child"

    if any(token in normalized_label for token in ("authorized to work", "legally permitted to work")):
        if any(token in normalized_label for token in ("please choose", "please specify", "which most accurately fits")):
            return "work_authorization", "detail_child"
        if is_binary or family == "select":
            return "work_authorization", "boolean_parent"
        return "work_authorization", "detail_child"

    if "salary" in normalized_label or "compensation" in normalized_label:
        return "salary", "detail_child"
    if "start date" in normalized_label or "availability" in normalized_label:
        return "availability_window", "detail_child"
    if "notice" in normalized_label:
        return "notice_period", "detail_child"
    return None, None


def _expected_binding_is_compatible(field: FormField, expected: Any) -> bool:
    expected_label = str(getattr(expected, "field_label", "") or "").strip()
    expected_type = str(getattr(expected, "field_type", "") or "").strip()
    expected_section = str(getattr(expected, "field_section", "") or "").strip()
    expected_fingerprint = normalize_name(str(getattr(expected, "field_fingerprint", "") or ""))
    current_fingerprint = normalize_name(field.field_fingerprint or "")

    if expected_fingerprint and current_fingerprint and expected_fingerprint != current_fingerprint:
        return False

    current_family = _field_type_family(field.field_type)
    expected_family = _field_type_family(expected_type) if expected_type else ""
    if expected_family and expected_family != "unknown" and current_family != expected_family:
        return False

    if expected_section:
        current_section = field.section or ""
        if current_section and not (
            _section_matches_scope(current_section, expected_section)
            or _section_matches_scope(expected_section, current_section)
        ):
            return False

    current_cluster, current_role = _expected_cluster_signature(_preferred_field_label(field), field.field_type)
    expected_cluster, expected_role = _expected_cluster_signature(expected_label, expected_type)
    if current_cluster and expected_cluster and current_cluster != expected_cluster:
        return False
    if current_cluster and expected_cluster and current_role and expected_role and current_role != expected_role:
        return False

    current_tokens = _field_label_tokens(_preferred_field_label(field))
    expected_tokens = _field_label_tokens(expected_label)
    if expected_tokens and current_tokens:
        overlap = current_tokens & expected_tokens
        if not overlap:
            return False
        smaller = min(len(current_tokens), len(expected_tokens))
        if smaller >= 2 and (len(overlap) / smaller) < 0.5:
            return False

    return True


def _field_companion_identity(field: FormField) -> tuple[str, str] | None:
    label = normalize_name(field.raw_label or _preferred_field_label(field) or "")
    if not label:
        return None
    return (normalize_name(field.section or ""), label)


def _companion_duplicate_field_ids(fields: list[FormField]) -> set[str]:
    groups: dict[tuple[str, str], list[FormField]] = {}
    for field in fields:
        identity = _field_companion_identity(field)
        if identity is None:
            continue
        groups.setdefault(identity, []).append(field)

    duplicate_ids: set[str] = set()
    for group in groups.values():
        has_data_field = any(field.field_type not in _COMPANION_BOOLEAN_FIELD_TYPES for field in group)
        if not has_data_field:
            continue
        for field in group:
            if field.field_type in _COMPANION_BOOLEAN_FIELD_TYPES:
                duplicate_ids.add(field.field_id)
    return duplicate_ids


def _semantic_tokens(text: str) -> set[str]:
    stop_words = {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "for",
        "from",
        "i",
        "in",
        "is",
        "my",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
    return {
        token
        for token in normalize_name(text).split()
        if len(token) > 2 and token not in stop_words
    }


def _semantic_text_values_match(current: str, expected: str) -> bool:
    if _field_value_matches_expected(current, expected):
        return True

    current_numbers = [int(match) for match in re.findall(r"\d+", current or "")]
    expected_numbers = [int(match) for match in re.findall(r"\d+", expected or "")]
    if current_numbers and expected_numbers:
        if set(current_numbers) & set(expected_numbers):
            return True
        if len(expected_numbers) >= 2:
            low = min(expected_numbers[0], expected_numbers[1])
            high = max(expected_numbers[0], expected_numbers[1])
            if any(low <= number <= high for number in current_numbers):
                return True

    current_tokens = _semantic_tokens(current)
    expected_tokens = _semantic_tokens(expected)
    if current_tokens and expected_tokens:
        overlap = current_tokens & expected_tokens
        smaller = min(len(current_tokens), len(expected_tokens))
        if overlap and (len(overlap) >= min(2, smaller) or (len(overlap) / max(smaller, 1)) >= 0.5):
            return True
    return False


def _verification_issue_for_field(
    field: FormField,
    *,
    reason: str,
    relative_position: str,
    expected_value: str,
) -> ApplicationFieldIssue:
    return ApplicationFieldIssue(
        field_id=field.field_id,
        name=_preferred_field_label(field),
        field_type=field.field_type,
        section=field.section or "",
        section_path=field.section or "",
        required=field.required,
        reason=reason,
        relative_position=relative_position,  # type: ignore[arg-type]
        takeover_suggestion="browser_use_takeover",
        question_text=(field.raw_label or _preferred_field_label(field) or "").strip() or None,
        current_value=(field.current_value or "").strip(),
        visible_error=f"Expected value: {expected_value}",
        widget_kind=None,
        options=[
            str(option).strip()
            for option in (field.options or field.choices or [])
            if str(option).strip()
        ],
    )


def _assess_debug_enabled() -> bool:
    return os.getenv("GH_DEBUG_PROFILE_PASS_THROUGH") == "1"


def _field_log_snapshot(fields: list[FormField], target_section: str | None = None) -> list[dict[str, Any]]:
    """Return a compact snapshot of extracted fields for diagnostics."""
    scoped = _filter_fields_for_scope(
        fields,
        target_section=target_section,
        allow_all_visible_fallback=False,
    )
    snapshot: list[dict[str, Any]] = []
    for field in scoped[:20]:
        snapshot.append(
            {
                "field_id": field.field_id,
                "label": _preferred_field_label(field),
                "field_type": field.field_type,
                "section": field.section,
                "required": field.required,
                "current_value": field.current_value,
                "choices": (field.options or field.choices or [])[:6],
            }
        )
    return snapshot


def _is_meaningful_section_label(section: str | None, field_label: str | None = None) -> bool:
    text = str(section or "").strip()
    if not text:
        return False
    if field_label and normalize_name(text) == normalize_name(field_label):
        return False
    if len(text) > 80:
        return False
    if "?" in text:
        return False
    return True


_FIELD_LAYOUT_JS = r"""(fieldIds) => {
	const ff = window.__ff;
	if (!ff) return JSON.stringify({});
	const out = {};
	for (const fieldId of fieldIds || []) {
		const node = ff.queryOne('[data-ff-id="' + fieldId + '"]');
		if (!node) continue;
		const rect = node.getBoundingClientRect();
		out[fieldId] = {
			top: rect.top,
			bottom: rect.bottom,
			in_view: rect.bottom >= 0 && rect.top <= window.innerHeight,
		};
	}
	return JSON.stringify(out);
}"""

_FIELD_CONTEXT_JS = r"""(ffId) => {
	const ff = window.__ff;
	const el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({error_text: "", widget_kind: ""});

	const visible = (node) => {
		if (!node) return false;
		const style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		const rect = node.getBoundingClientRect();
		return rect.width > 0 && rect.height > 0;
	};
	const norm = (text) => (text || '').replace(/\s+/g, ' ').trim();
	const looksLikeErrorText = (text) => /(?:error|required|invalid|must|please|select|enter|choose|provide|complete)/i.test(text || '');
	const gather = [];
	const seen = new Set();
	const push = (node) => {
		if (!node || seen.has(node)) return;
		seen.add(node);
		gather.push(node);
	};
	push(el);
	push(ff ? ff.closestCrossRoot(el, '[aria-invalid], [role="group"], [role="radiogroup"], fieldset, label, [role="row"], [role="cell"], .form-group, .field, [data-automation-id*="formField"]') : null);
	if (el.querySelector) {
		push(el.querySelector('[aria-invalid], input, textarea, select, [role="checkbox"], [role="radio"], [role="switch"], [role="textbox"], [role="combobox"]'));
	}

	let errorText = "";
	const errorSelectors = '[role="alert"], [aria-live="assertive"], .error, .errors, .invalid, .field-error, [data-error], [class*="error"]';
	for (const node of gather) {
		if (!node) continue;
		const direct = [];
		if (node.matches && node.matches(errorSelectors)) direct.push(node);
		if (node.querySelectorAll) {
			for (const err of Array.from(node.querySelectorAll(errorSelectors))) direct.push(err);
		}
		for (const err of direct) {
			if (!visible(err)) continue;
			const text = norm(err.innerText || err.getAttribute('aria-label') || err.getAttribute('data-error') || '');
			if (text && looksLikeErrorText(text)) {
				errorText = text;
				break;
			}
		}
		if (errorText) break;
	}

	let widgetKind = '';
	if (el.tagName === 'SELECT') widgetKind = 'native_select';
	else if (el.tagName === 'TEXTAREA') widgetKind = 'textarea';
	else if (el.tagName === 'INPUT') widgetKind = (el.getAttribute('type') || 'text').toLowerCase();
	else if (el.getAttribute('role') === 'combobox' || el.getAttribute('data-uxi-widget-type') === 'selectinput') widgetKind = 'custom_combobox';
	else if (el.getAttribute('role') === 'listbox') widgetKind = 'listbox';
	else if (el.getAttribute('role') === 'radio') widgetKind = 'radio';
	else if (el.getAttribute('role') === 'checkbox') widgetKind = 'checkbox';
	else if (el.getAttribute('role') === 'switch') widgetKind = 'switch';
	else widgetKind = 'custom_widget';

	return JSON.stringify({
		error_text: errorText,
		widget_kind: widgetKind,
	});
}"""

_SCAN_PAGE_STATE_JS = r"""() => {
	const visible = (el) => {
		if (!el) return false;
		const style = window.getComputedStyle(el);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		const rect = el.getBoundingClientRect();
		return rect.width > 0 && rect.height > 0;
	};
	const norm = (text) => (text || '').replace(/\s+/g, ' ').trim();
	const looksLikeErrorText = (text) => /(?:error|required|invalid|must|please|select|enter|choose|provide|complete)/i.test(text || '');
	const bodyText = norm(document.body ? document.body.innerText : '');
	const headingTexts = Array.from(document.querySelectorAll('h1, h2, h3, [role="heading"]'))
		.filter((el) => visible(el))
		.map((el) => norm(el.innerText || el.getAttribute('aria-label') || ''))
		.filter((text) => Boolean(text))
		.slice(0, 8);
	const buttonTexts = [];
	const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], a, [role="button"]'))
		.filter((el) => visible(el))
		.map((el) => {
			const text = norm(el.innerText || el.value || el.getAttribute('aria-label') || '');
			const disabled = !!(el.disabled || el.getAttribute('aria-disabled') === 'true');
			const lower = text.toLowerCase();
			return { text, lower, disabled };
		})
		.filter((item) => item.text);
	for (const item of buttons) buttonTexts.push(item.text);

	const isAdvanceControl = (lower) => {
		if (!lower) return false;
		if (/\bcontinue with\b/.test(lower)) return false;
		if (/\b(save and continue later|save & continue later|continue later)\b/.test(lower)) return false;
		if (/^(next|next step|continue|continue application|continue to review)$/.test(lower)) return true;
		if (/\b(save and continue|save & continue)\b/.test(lower)) return true;
		return false;
	};

	const submitButtons = buttons.filter((item) => /\b(submit|finish application|send application)\b/.test(item.lower));
	const advanceButtons = buttons.filter((item) => isAdvanceControl(item.lower));

	const markerNodes = Array.from(document.querySelectorAll('[id], [class], script[src]')).slice(0, 300);
	const markers = [];
	for (const node of markerNodes) {
		if (node.id) markers.push(node.id);
		if (typeof node.className === 'string' && node.className) markers.push(node.className);
		if (node.tagName === 'SCRIPT' && node.getAttribute('src')) markers.push(node.getAttribute('src'));
	}

	const errorTexts = Array.from(document.querySelectorAll(
		'[role="alert"], [aria-live="assertive"], [aria-invalid="true"], .error, .errors, .invalid, .field-error, [data-error], [class*="error"]'
	))
		.filter((el) => visible(el))
		.map((el) => norm(el.innerText || el.getAttribute('aria-label') || el.getAttribute('data-error') || ''))
		.filter((text) => Boolean(text) && looksLikeErrorText(text))
		.slice(0, 12);

	return JSON.stringify({
		body_text: bodyText,
		heading_texts: headingTexts,
		button_texts: buttonTexts,
		submit_visible: submitButtons.length > 0,
		submit_disabled: submitButtons.length > 0 && submitButtons.every((item) => item.disabled),
		advance_visible: advanceButtons.some((item) => !/\bsubmit\b/.test(item.lower)),
		advance_disabled: advanceButtons.length > 0 && advanceButtons.every((item) => item.disabled),
		error_texts: errorTexts,
		markers,
	});
}"""


def _group_form_fields(raw_fields: list[dict[str, Any]], button_groups: list[dict[str, Any]]) -> list[FormField]:
    fields: list[FormField] = []
    grouped_names: set[str] = set()
    seen_ids: set[str] = set()

    for f_data in raw_fields:
        fid = f_data.get("field_id", "")
        if not fid or fid in seen_ids:
            continue
        ftype = f_data.get("field_type", "text")
        fname = f_data.get("name", "")

        if ftype in ("checkbox", "radio"):
            group_key = f"group:{fname}:{f_data.get('section', '')}"
            if group_key in grouped_names:
                continue
            siblings = [
                r
                for r in raw_fields
                if r.get("field_type") in ("checkbox", "radio")
                and r.get("name") == fname
                and r.get("section", "") == f_data.get("section", "")
            ]
            if len(siblings) > 1:
                grouped_names.add(group_key)
                for sibling in siblings:
                    seen_ids.add(sibling.get("field_id", ""))
                selected_choice = ""
                for sibling in siblings:
                    if sibling.get("current_value"):
                        selected_choice = sibling.get("itemLabel", sibling.get("name", "")) or ""
                        break
                fields.append(
                    FormField(
                        field_id=fid,
                        name=fname,
                        field_type=f"{ftype}-group",
                        section=f_data.get("section", ""),
                        required=f_data.get("required", False),
                        options=[],
                        choices=[s.get("itemLabel", s.get("name", "")) for s in siblings],
                        is_native=False,
                        visible=True,
                        raw_label=f_data.get("raw_label"),
                        current_value=selected_choice,
                    )
                )
                continue

        seen_ids.add(fid)
        fields.append(FormField.model_validate(f_data))

    for bg in button_groups:
        bg_id = bg.get("field_id", "")
        if bg_id and bg_id not in seen_ids:
            seen_ids.add(bg_id)
            fields.append(FormField.model_validate(bg))

    return fields


def _field_is_empty(field: FormField) -> bool:
    if field.field_type in {"checkbox", "checkbox-group", "radio", "radio-group", "toggle", "button-group"}:
        return not bool((field.current_value or "").strip())
    if (field.widget_kind or "") == "grouped_date":
        return not _grouped_date_is_complete(field.current_value)
    return _is_effectively_unset_field_value(field.current_value)


def _maybe_suppress_custom_select_readback_false_positives(
    unresolved_required: list[ApplicationFieldIssue],
    fields: list[FormField],
    *,
    page_host: str,
    page_context_key: str,
) -> list[ApplicationFieldIssue]:
    """Remove custom-select \"empty\" blockers when DomHand recorded an expected or intended value.

    React-select / Greenhouse often shows a selected pill while DOM readback still returns empty,
    which spams false ``required_missing_value`` issues after a successful ``domhand_fill`` prefill
    and steers the agent into useless DomHand retry loops. If we have a non-empty recorded value for
    that stable field key (settled or ``domhand_unverified`` from a successful fill), treat the
    blocker as readback noise and drop it.
    """
    if not unresolved_required:
        return unresolved_required
    field_by_id = {f.field_id: f for f in fields}
    kept: list[ApplicationFieldIssue] = []
    dropped = 0
    for issue in unresolved_required:
        field = field_by_id.get(issue.field_id)
        if (
            issue.reason == "required_missing_value"
            and issue.field_type == "select"
            and field is not None
            and not field.is_native
            and not (issue.visible_error or "").strip()
            and not (issue.current_value or "").strip()
        ):
            fk = get_stable_field_key(field)
            expected = get_expected_field_value(
                host=page_host,
                page_context_key=page_context_key,
                field_key=fk,
            )
            ev = str(getattr(expected, "expected_value", None) or "").strip()
            if expected is not None and ev:
                dropped += 1
                continue
        kept.append(issue)
    if dropped:
        logger.info(
            "domhand.assess_state.suppressed_readback_false_positives",
            extra={"dropped": dropped, "host": page_host},
        )
    return kept


def _widget_kind_for_field(field: FormField, browser_context: dict[str, Any] | None = None) -> str:
    if field.widget_kind:
        return field.widget_kind
    if browser_context and isinstance(browser_context.get("widget_kind"), str) and browser_context["widget_kind"].strip():
        return str(browser_context["widget_kind"]).strip()
    if field.field_type == "select":
        return "native_select" if field.is_native else "custom_select"
    if field.field_type in {"radio-group", "radio"}:
        return "radio_group" if field.field_type == "radio-group" else "radio"
    if field.field_type in {"checkbox-group", "checkbox"}:
        return "checkbox_group" if field.field_type == "checkbox-group" else "checkbox"
    if field.field_type in {"button-group"}:
        return "button_group"
    if field.field_type in {"search"}:
        return "search_input"
    if field.field_type in {"textarea"}:
        return "textarea"
    if field.field_type in {"date"}:
        return "date_input"
    return "text_input" if field.is_native else "custom_widget"


def _is_noise_visible_error(text: str, unresolved_required: list[ApplicationFieldIssue]) -> bool:
    normalized = normalize_name(text)
    if not normalized:
        return True
    if len(normalized.split()) <= 3:
        option_words: set[str] = set()
        for issue in unresolved_required:
            for option in issue.options:
                option_words.update(normalize_name(option).split())
        if option_words and set(normalized.split()).issubset(option_words):
            return True
    return False


def _clean_visible_errors(
    visible_errors: list[str],
    unresolved_required: list[ApplicationFieldIssue],
) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_text in visible_errors:
        candidate = " ".join(str(raw_text or "").split()).strip()
        if not candidate:
            continue
        for issue in unresolved_required:
            option_suffix = " ".join(option for option in issue.options if option).strip()
            if option_suffix and candidate.endswith(option_suffix):
                candidate = candidate[: -len(option_suffix)].strip(" :-")
            visible_error = str(issue.visible_error or "").strip()
            if visible_error and candidate.endswith(visible_error):
                prefix = candidate[: -len(visible_error)].strip(" :-")
                if prefix and normalize_name(prefix) == normalize_name(issue.name):
                    candidate = visible_error
        normalized = normalize_name(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(candidate)
    return cleaned


def _classify_terminal_state(
    platform: str,
    has_editable_fields: bool,
    submit_visible: bool,
    submit_disabled: bool,
    advance_visible: bool,
    advance_disabled: bool,
    unresolved_required: list[ApplicationFieldIssue],
    visible_errors: list[str],
    body_text: str,
) -> str:
    body_norm = normalize_name(body_text)
    if re.search(
        r"\b(thank you for applying|application submitted|application received|successfully submitted)\b", body_norm
    ):
        return "confirmation"
    if advance_visible and not advance_disabled:
        return "advanceable"
    if not has_editable_fields and submit_visible:
        return "review"
    if (
        get_config_by_name(platform).single_page_presubmit_allowed
        and submit_visible
        and not submit_disabled
        and not unresolved_required
        and not visible_errors
    ):
        return "presubmit_single_page"
    return "advanceable"


async def domhand_assess_state(params: DomHandAssessStateParams, browser_session: BrowserSession) -> ActionResult:
    """Assess runtime application state for scrolling, advancing, or stopping."""
    page = await browser_session.get_current_page()
    if not page:
        return ActionResult(error="No active page found in browser session")

    try:
        from ghosthands.dom.shadow_helpers import ensure_helpers

        await ensure_helpers(page)
    except Exception:
        pass

    await page.evaluate(_build_inject_helpers_js())

    fields = await extract_visible_form_fields(page)
    logger.info(
        "domhand.assess_state.extracted "
        f"target_section={params.target_section or ''!r} "
        f"field_count={len(fields)} "
        f"required_field_count={sum(1 for field in fields if field.required)}"
    )
    logger.debug(
        "domhand.assess_state.extracted.snapshot "
        f"snapshot={json.dumps(_field_log_snapshot(fields, params.target_section), ensure_ascii=True)}"
    )
    if _assess_debug_enabled():
        logger.debug(
            "domhand.assess_state.fields",
            extra={
                "target_section": params.target_section,
                "fields": [
                    {
                        "field_id": field.field_id,
                        "label": _preferred_field_label(field),
                        "field_type": field.field_type,
                        "section": field.section,
                        "required": field.required,
                        "current_value": field.current_value,
                        "choices": (field.options or field.choices or [])[:6],
                    }
                    for field in fields[:30]
                ],
            },
        )

    page_scan_raw = await page.evaluate(_SCAN_PAGE_STATE_JS)
    page_scan = json.loads(page_scan_raw) if isinstance(page_scan_raw, str) else page_scan_raw or {}

    field_ids = [field.field_id for field in fields]
    layout_raw = await page.evaluate(_FIELD_LAYOUT_JS, field_ids)
    layout = json.loads(layout_raw) if isinstance(layout_raw, str) else layout_raw or {}

    button_texts = page_scan.get("button_texts", [])
    body_text = page_scan.get("body_text", "")
    current_url = await _safe_page_url(page)
    page_host = detect_host_from_url(current_url)
    platform_hint = detect_platform_from_signals(
        str(current_url or ""),
        page_text=" ".join(filter(None, [body_text, " ".join(button_texts)])),
        markers=page_scan.get("markers") or [],
    )

    scoped_fields = _filter_fields_for_scope(
        fields,
        target_section=params.target_section,
        allow_all_visible_fallback=False,
    )
    if params.target_section and not scoped_fields:
        visible_sections = [
            section
            for section in dict.fromkeys(
                str(field.section or "").strip()
                for field in fields
                if _is_meaningful_section_label(field.section, _preferred_field_label(field))
            )
            if section
        ]
        logger.info(
            "domhand.assess_state.target_section_not_live",
            extra={
                "target_section": params.target_section,
                "visible_sections": visible_sections[:8],
                "field_count": len(fields),
            },
        )
    companion_duplicate_ids = _companion_duplicate_field_ids(scoped_fields)
    unresolved_required: list[ApplicationFieldIssue] = []
    unresolved_optional: list[ApplicationFieldIssue] = []
    mismatched_fields: list[ApplicationFieldIssue] = []
    opaque_fields: list[ApplicationFieldIssue] = []
    unverified_fields: list[ApplicationFieldIssue] = []
    sections_in_view: list[str] = []
    field_context_by_id: dict[str, dict[str, Any]] = {}
    verification_attempts = _verification_attempt_count()

    for field in scoped_fields:
        if field.field_type == "file" or _is_navigation_field(field):
            continue
        if field.field_id in companion_duplicate_ids:
            continue

        field_layout = layout.get(field.field_id, {})
        relative_position = "unknown"
        if field_layout:
            top = float(field_layout.get("top", 0))
            bottom = float(field_layout.get("bottom", 0))
            in_view = bool(field_layout.get("in_view"))
            if in_view:
                relative_position = "in_view"
            elif bottom < 0:
                relative_position = "above"
            elif top > 0:
                relative_position = "below"
        if _is_meaningful_section_label(field.section, _preferred_field_label(field)) and relative_position == "in_view":
            sections_in_view.append(field.section)

        if field.field_type == "checkbox-group" and not field.current_value:
            field.current_value = await _read_checkbox_group_value(page, field)
        elif field.field_type in {"radio-group", "button-group"} and not field.current_value:
            field.current_value = await _read_group_selection(page, field.field_id)
        elif field.field_type == "select":
            observed_value = (
                await _read_field_value_for_field(page, field)
                if (field.widget_kind or "") == "grouped_date"
                else await _read_field_value(page, field.field_id)
            )
            if observed_value or not field.current_value:
                field.current_value = observed_value or field.current_value
        elif field.field_type in {"checkbox", "toggle"} and not field.current_value:
            binary_state = await _read_binary_state(page, field.field_id)
            field.current_value = "checked" if binary_state else ""
        else:
            observed_value = (
                await _read_field_value_for_field(page, field)
                if (field.widget_kind or "") == "grouped_date"
                else await _read_field_value(page, field.field_id)
            )
            if observed_value or not field.current_value:
                field.current_value = observed_value or field.current_value

        has_error = await _field_has_validation_error(page, field.field_id)
        if field.field_id not in field_context_by_id:
            try:
                raw_context = await page.evaluate(_FIELD_CONTEXT_JS, field.field_id)
                field_context_by_id[field.field_id] = (
                    json.loads(raw_context) if isinstance(raw_context, str) else raw_context or {}
                )
            except Exception:
                field_context_by_id[field.field_id] = {}
        is_empty = _field_is_empty(field)
        if not field.required and not has_error:
            continue
        if not field.required and is_empty:
            continue
        if not is_empty and not has_error:
            continue

        reason = "validation_error" if has_error else "required_missing_value"
        field_context = field_context_by_id.get(field.field_id) or {}
        issue = ApplicationFieldIssue(
            field_id=field.field_id,
            name=_preferred_field_label(field),
            field_type=field.field_type,
            section=field.section or "",
            section_path=field.section or "",
            required=field.required,
            reason=reason,
            relative_position=relative_position,
            takeover_suggestion="browser_use_takeover",
            question_text=(field.raw_label or _preferred_field_label(field) or "").strip() or None,
            current_value=(field.current_value or "").strip(),
            visible_error=str(field_context.get("error_text") or "").strip() or None,
            widget_kind=_widget_kind_for_field(field, field_context),
            options=[
                str(option).strip()
                for option in (field.options or field.choices or [])
                if str(option).strip()
            ],
        )
        if field.required:
            unresolved_required.append(issue)
        else:
            unresolved_optional.append(issue)

    page_context_key = await _get_page_context_key(page, fields=fields, fallback_marker=params.target_section)
    unresolved_required = _maybe_suppress_custom_select_readback_false_positives(
        unresolved_required,
        fields,
        page_host=page_host,
        page_context_key=page_context_key,
    )

    verification_failures: tuple[list[ApplicationFieldIssue], list[ApplicationFieldIssue], list[ApplicationFieldIssue]] = (
        [],
        [],
        [],
    )
    verification_companion_duplicate_ids = _companion_duplicate_field_ids(fields)
    verification_scoped_fields = [
        field
        for field in fields
        if field.field_type != "file"
        and not _is_navigation_field(field)
        and field.field_id not in verification_companion_duplicate_ids
    ]
    for attempt_index in range(verification_attempts):
        mismatched_attempt: list[ApplicationFieldIssue] = []
        opaque_attempt: list[ApplicationFieldIssue] = []
        unverified_attempt: list[ApplicationFieldIssue] = []
        for field in verification_scoped_fields:
            field_key = get_stable_field_key(field)
            expected = get_expected_field_value(
                host=page_host,
                page_context_key=page_context_key,
                field_key=field_key,
            )
            if not expected:
                continue
            if not _expected_binding_is_compatible(field, expected):
                logger.info(
                    "domhand.assess_state.stale_expected_binding_ignored",
                    extra={
                        "field_id": field.field_id,
                        "field_key": field_key,
                        "field_label": _preferred_field_label(field),
                        "field_type": field.field_type,
                        "field_section": field.section or "",
                        "field_fingerprint": field.field_fingerprint or "",
                        "expected_label": expected.field_label,
                        "expected_type": getattr(expected, "field_type", ""),
                        "expected_section": getattr(expected, "field_section", ""),
                        "expected_fingerprint": getattr(expected, "field_fingerprint", ""),
                        "expected_value": expected.expected_value,
                        "expected_source": expected.source,
                    },
                )
                continue
            field_layout = layout.get(field.field_id, {})
            relative_position = "unknown"
            if field_layout:
                if bool(field_layout.get("in_view")):
                    relative_position = "in_view"
                elif float(field_layout.get("bottom", 0)) < 0:
                    relative_position = "above"
                elif float(field_layout.get("top", 0)) > 0:
                    relative_position = "below"

            if field.field_type == "checkbox-group":
                field.current_value = await _read_checkbox_group_value(page, field)
            elif field.field_type in {"radio-group", "button-group"}:
                field.current_value = await _read_group_selection(page, field.field_id)
            elif field.field_type == "select":
                field.current_value = (
                    await _read_field_value_for_field(page, field)
                    if (field.widget_kind or "") == "grouped_date"
                    else await _read_field_value(page, field.field_id)
                )
            elif field.field_type in {"checkbox", "toggle"}:
                binary_state = await _read_binary_state(page, field.field_id)
                field.current_value = "checked" if binary_state else ""
            else:
                observed_value = (
                    await _read_field_value_for_field(page, field)
                    if (field.widget_kind or "") == "grouped_date"
                    else await _read_field_value(page, field.field_id)
                )
                if observed_value or not field.current_value:
                    field.current_value = observed_value or field.current_value

            if not str(field.current_value or "").strip():
                unverified_attempt.append(
                    _verification_issue_for_field(
                        field,
                        reason="unverified_value",
                        relative_position=relative_position,
                        expected_value=expected.expected_value,
                    ),
                )
                continue

            if not _value_shape_is_compatible(field, expected.expected_value):
                if _assess_debug_enabled():
                    logger.info(
                        "domhand.assess_state.skip_incompatible_expected",
                        extra={
                            "field_id": field.field_id,
                            "field_label": _preferred_field_label(field),
                            "field_type": field.field_type,
                            "current_value": field.current_value,
                            "expected_value": expected.expected_value,
                            "source": expected.source,
                        },
                    )
                continue

            if _field_current_value_is_opaque(field):
                opaque_attempt.append(
                    _verification_issue_for_field(
                        field,
                        reason="opaque_value",
                        relative_position=relative_position,
                        expected_value=expected.expected_value,
                    ),
                )
                continue

            if _field_uses_semantic_verification(field):
                has_error = await _field_has_validation_error(page, field.field_id)
                if not has_error and (
                    field.field_type == "textarea"
                    or _semantic_text_values_match(field.current_value, expected.expected_value)
                ):
                    continue

            if not _field_value_matches_expected(field.current_value, expected.expected_value):
                mismatched_attempt.append(
                    _verification_issue_for_field(
                        field,
                        reason="mismatched_value",
                        relative_position=relative_position,
                        expected_value=expected.expected_value,
                    ),
                )

        verification_failures = (mismatched_attempt, opaque_attempt, unverified_attempt)
        if not mismatched_attempt and not opaque_attempt and not unverified_attempt:
            break
        if attempt_index < verification_attempts - 1:
            await asyncio.sleep(0.2)

    mismatched_fields, opaque_fields, unverified_fields = verification_failures
    optional_validation_blockers = [
        issue
        for issue in unresolved_optional
        if issue.reason == "validation_error"
    ]

    scroll_bias = "none"
    if any(issue.relative_position == "in_view" for issue in unresolved_required):
        scroll_bias = "stay"
    elif any(issue.relative_position == "below" for issue in unresolved_required) and not any(
        issue.relative_position == "above" for issue in unresolved_required
    ):
        scroll_bias = "down"
    elif any(issue.relative_position == "above" for issue in unresolved_required) and not any(
        issue.relative_position == "below" for issue in unresolved_required
    ):
        scroll_bias = "up"

    target_section_has_live_match = bool(
        params.target_section
        and any(
            _section_matches_scope(field.section, params.target_section)
            and _is_meaningful_section_label(field.section, _preferred_field_label(field))
            for field in fields
        )
    )
    heading_texts = [
        str(text).strip()
        for text in (page_scan.get("heading_texts") or [])
        if str(text).strip()
    ]
    current_section = params.target_section if target_section_has_live_match else ""
    if not current_section:
        for issue in unresolved_required:
            if _is_meaningful_section_label(issue.section, issue.name) and issue.relative_position == "in_view":
                current_section = issue.section
                break
    if not current_section:
        for issue in unresolved_required:
            if _is_meaningful_section_label(issue.section, issue.name):
                current_section = issue.section
                break
    if not current_section and sections_in_view:
        current_section = sections_in_view[0]
    if not current_section and heading_texts:
        current_section = heading_texts[0]

    visible_errors = [
        text
        for text in (page_scan.get("error_texts") or [])
        if text and not _is_noise_visible_error(text, unresolved_required)
    ][:8]
    visible_errors = _clean_visible_errors(visible_errors, unresolved_required)
    has_editable_fields = any(field.field_type != "file" and not _is_navigation_field(field) for field in fields)
    terminal_state = _classify_terminal_state(
        platform_hint,
        has_editable_fields,
        bool(page_scan.get("submit_visible")),
        bool(page_scan.get("submit_disabled")),
        bool(page_scan.get("advance_visible")),
        bool(page_scan.get("advance_disabled")),
        unresolved_required,
        visible_errors,
        body_text,
    )
    application_state = ApplicationState(
        terminal_state=terminal_state,
        current_section=current_section,
        unresolved_required_fields=unresolved_required,
        unresolved_optional_fields=unresolved_optional,
        mismatched_fields=mismatched_fields,
        opaque_fields=opaque_fields,
        unverified_fields=unverified_fields,
        visible_errors=visible_errors,
        scroll_bias=scroll_bias,
        submit_visible=bool(page_scan.get("submit_visible")),
        submit_disabled=bool(page_scan.get("submit_disabled")),
        advance_visible=bool(page_scan.get("advance_visible")),
        advance_disabled=bool(page_scan.get("advance_disabled")),
        advance_allowed=(
            not unresolved_required
            and not optional_validation_blockers
            and not visible_errors
            and not mismatched_fields
            and not opaque_fields
            and not unverified_fields
            and not (bool(page_scan.get("advance_visible")) and bool(page_scan.get("advance_disabled")))
        ),
        platform_hint=platform_hint,
    )
    field_by_id = {field.field_id: field for field in fields}
    active_blocker_issues = (
        application_state.unresolved_required_fields
        + optional_validation_blockers
        + application_state.mismatched_fields
        + application_state.opaque_fields
        + application_state.unverified_fields
    )
    blocker_states: dict[str, dict[str, str]] = {}
    for issue in active_blocker_issues:
        field = field_by_id.get(issue.field_id)
        blocker_key = get_stable_field_key(field) if field else normalize_name(issue.field_id)
        blocker_states[blocker_key] = {
            "field_id": issue.field_id,
            "field_label": issue.name,
            "field_type": issue.field_type,
            "field_section": issue.section or "",
            "field_fingerprint": field.field_fingerprint if field else "",
            "reason": issue.reason,
            "current_value": issue.current_value or "",
            "visible_error": issue.visible_error or "",
        }

    previous_state = getattr(browser_session, "_gh_last_application_state", None)
    previous_blocker_states = (
        previous_state.get("blocking_field_states", {})
        if isinstance(previous_state, dict)
        and previous_state.get("page_context_key") == page_context_key
        and previous_state.get("page_url") == current_url
        else {}
    )
    blocker_state_changes: dict[str, str] = {}
    for blocker_key, state in blocker_states.items():
        previous = previous_blocker_states.get(blocker_key)
        if not previous:
            blocker_state_changes[blocker_key] = "new"
            continue
        blocker_state_changes[blocker_key] = (
            "no_state_change"
            if previous == state
            else "changed"
        )

    blocker_signature = json.dumps(blocker_states, sort_keys=True, ensure_ascii=True)
    previous_signature = previous_state.get("blocking_signature") if isinstance(previous_state, dict) else None
    previous_repeat_count = int(previous_state.get("same_blocker_signature_count") or 0) if isinstance(previous_state, dict) else 0
    same_blocker_signature_count = (
        previous_repeat_count + 1
        if previous_signature == blocker_signature and previous_state.get("page_context_key") == page_context_key
        else 0
    )

    _STALE_MISMATCH_THRESHOLD = 3
    only_soft_blockers = (
        not unresolved_required
        and not visible_errors
        and not optional_validation_blockers
    )
    if (
        same_blocker_signature_count >= _STALE_MISMATCH_THRESHOLD
        and only_soft_blockers
        and (mismatched_fields or opaque_fields or unverified_fields)
    ):
        stale_ids = [f.field_id for f in mismatched_fields + opaque_fields + unverified_fields]
        logger.warning(
            "domhand.assess_state.stale_blocker_override",
            extra={
                "same_blocker_signature_count": same_blocker_signature_count,
                "cleared_field_ids": stale_ids,
                "reason": "Blockers unchanged for multiple assessments; likely false positives. Allowing advancement.",
            },
        )
        application_state = ApplicationState(
            terminal_state=application_state.terminal_state,
            current_section=application_state.current_section,
            unresolved_required_fields=application_state.unresolved_required_fields,
            unresolved_optional_fields=application_state.unresolved_optional_fields,
            mismatched_fields=[],
            opaque_fields=[],
            unverified_fields=[],
            visible_errors=application_state.visible_errors,
            scroll_bias=application_state.scroll_bias,
            submit_visible=application_state.submit_visible,
            submit_disabled=application_state.submit_disabled,
            advance_visible=application_state.advance_visible,
            advance_disabled=application_state.advance_disabled,
            advance_allowed=True,
            platform_hint=application_state.platform_hint,
        )
        active_blocker_issues = []
        blocker_states = {}
        blocker_state_changes = {}
        blocker_signature = "{}"

    setattr(
        browser_session,
        "_gh_last_application_state",
        {
            "page_context_key": page_context_key,
            "page_url": current_url,
            "current_section": application_state.current_section,
            "advance_allowed": application_state.advance_allowed,
            "advance_visible": application_state.advance_visible,
            "advance_disabled": application_state.advance_disabled,
            "unresolved_required_count": len(application_state.unresolved_required_fields),
            "optional_validation_count": len(optional_validation_blockers),
            "mismatched_count": len(application_state.mismatched_fields),
            "opaque_count": len(application_state.opaque_fields),
            "unverified_count": len(application_state.unverified_fields),
            "blocking_signature": blocker_signature,
            "same_blocker_signature_count": same_blocker_signature_count,
            "blocking_field_ids": sorted(
                {
                    issue.field_id
                    for issue in active_blocker_issues
                    if issue.field_id
                }
            ),
            "blocking_field_keys": sorted(blocker_states.keys()),
            "blocking_field_reasons": {
                blocker_key: state.get("reason", "")
                for blocker_key, state in blocker_states.items()
            },
            "blocking_field_labels": sorted(
                {
                    str(issue.name).strip()
                    for issue in active_blocker_issues
                    if issue.name
                }
            ),
            "blocking_field_states": blocker_states,
            "blocking_field_state_changes": blocker_state_changes,
            "single_active_blocker": (
                {
                    "field_key": next(iter(blocker_states.keys())),
                    **next(iter(blocker_states.values())),
                }
                if len(blocker_states) == 1
                else None
            ),
        },
    )
    await publish_browser_session_trace(
        browser_session,
        "assessment_snapshot",
        {
            "page_context_key": page_context_key,
            "page_url": current_url,
            "current_section": application_state.current_section,
            "blocking_field_ids": sorted(
                {
                    issue.field_id
                    for issue in active_blocker_issues
                    if issue.field_id
                }
            ),
            "blocking_field_keys": sorted(blocker_states.keys()),
            "blocking_field_reasons": {
                blocker_key: state.get("reason", "")
                for blocker_key, state in blocker_states.items()
            },
            "blocking_field_state_changes": blocker_state_changes,
            "single_active_blocker": (
                {
                    "field_key": next(iter(blocker_states.keys())),
                    **next(iter(blocker_states.values())),
                }
                if len(blocker_states) == 1
                else None
            ),
            "advance_allowed": application_state.advance_allowed,
            "advance_disabled": application_state.advance_disabled,
            "unresolved_required_count": len(application_state.unresolved_required_fields),
            "optional_validation_count": len(optional_validation_blockers),
            "mismatched_count": len(application_state.mismatched_fields),
            "opaque_count": len(application_state.opaque_fields),
            "unverified_count": len(application_state.unverified_fields),
        },
    )
    logger.info(
        "domhand.assess_state.summary "
        f"target_section={params.target_section or ''!r} "
        f"current_section={application_state.current_section!r} "
        f"terminal_state={application_state.terminal_state} "
        f"unresolved_required_count={len(application_state.unresolved_required_fields)} "
        f"optional_validation_count={len(optional_validation_blockers)} "
        f"mismatched_count={len(application_state.mismatched_fields)} "
        f"opaque_count={len(application_state.opaque_fields)} "
        f"unverified_count={len(application_state.unverified_fields)} "
        f"same_blocker_signature_count={same_blocker_signature_count}"
    )
    logger.debug(
        "domhand.assess_state.summary.details "
        f"unresolved_required_fields={json.dumps([{'field_id': issue.field_id, 'label': issue.name, 'field_type': issue.field_type, 'section': issue.section, 'reason': issue.reason, 'current_value': issue.current_value, 'visible_error': issue.visible_error, 'widget_kind': issue.widget_kind} for issue in application_state.unresolved_required_fields[:10]], ensure_ascii=True)} "
        f"optional_validation_fields={json.dumps([{'field_id': issue.field_id, 'label': issue.name, 'field_type': issue.field_type, 'section': issue.section, 'reason': issue.reason, 'current_value': issue.current_value, 'visible_error': issue.visible_error} for issue in optional_validation_blockers[:10]], ensure_ascii=True)} "
        f"visible_errors={json.dumps(application_state.visible_errors[:8], ensure_ascii=True)} "
        f"snapshot={json.dumps(_field_log_snapshot(fields, params.target_section), ensure_ascii=True)}"
    )

    summary_lines = [
        f"Application state: {application_state.terminal_state}",
        f"Current section: {application_state.current_section or '(unknown)'}",
        f"Unresolved required fields: {len(application_state.unresolved_required_fields)}",
        f"Optional validation blockers: {len(optional_validation_blockers)}",
        f"Mismatched fields: {len(application_state.mismatched_fields)}",
        f"Opaque fields: {len(application_state.opaque_fields)}",
        f"Unverified fields: {len(application_state.unverified_fields)}",
        f"Visible errors: {len(application_state.visible_errors)}",
        f"Scroll bias: {application_state.scroll_bias}",
        f"Advance disabled: {'Yes' if application_state.advance_disabled else 'No'}",
        f"Advance allowed: {'Yes' if application_state.advance_allowed else 'No'}",
    ]
    if application_state.platform_hint:
        summary_lines.append(f"Platform hint: {application_state.platform_hint}")
    if application_state.advance_allowed:
        summary_lines.append("Next action: All visible blockers are clear on this page. Do not refill fields; click Next/Continue/Save now.")
    if application_state.unresolved_required_fields:
        summary_lines.append("Required field issues:")
        for issue in application_state.unresolved_required_fields[:10]:
            location = issue.relative_position.replace("_", " ")
            section = f" [{issue.section}]" if issue.section else ""
            extra = []
            if issue.current_value:
                extra.append(f'current="{issue.current_value}"')
            if issue.visible_error:
                extra.append(f'error="{issue.visible_error}"')
            if issue.widget_kind:
                extra.append(f"widget={issue.widget_kind}")
            extras = f" | {'; '.join(extra)}" if extra else ""
            summary_lines.append(f"  - {issue.name}{section} ({issue.reason}, {location}){extras}")
    if application_state.visible_errors:
        summary_lines.append("Visible errors:")
        for error_text in application_state.visible_errors[:6]:
            summary_lines.append(f"  - {error_text}")
    if optional_validation_blockers:
        summary_lines.append("Optional validation blockers:")
        for issue in optional_validation_blockers[:10]:
            location = issue.relative_position.replace("_", " ")
            section_suffix = f" [{issue.section}]" if issue.section else ""
            current_suffix = f' | current="{issue.current_value}"' if issue.current_value else ""
            error_suffix = f'; error="{issue.visible_error}"' if issue.visible_error else ""
            summary_lines.append(
                f"  - {issue.name}"
                f"{section_suffix}"
                f" ({issue.reason}, {location})"
                f"{current_suffix}"
                f"{error_suffix}"
            )
    summary_lines.append("APPLICATION_STATE_JSON:")
    summary_lines.append(application_state.model_dump_json())
    summary = "\n".join(summary_lines)

    logger.debug(summary)
    return ActionResult(
        extracted_content=summary,
        include_extracted_content_only_once=False,
    )
