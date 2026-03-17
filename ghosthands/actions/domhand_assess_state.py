"""Runtime application-state assessment for browser-use job-application flows."""

import json
import logging
import re
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from ghosthands.actions.domhand_fill import (
    _EXTRACT_BUTTON_GROUPS_JS,
    _EXTRACT_FIELDS_JS,
    _build_inject_helpers_js,
    _field_has_validation_error,
    _is_navigation_field,
    _preferred_field_label,
    _read_binary_state,
    _read_group_selection,
)
from ghosthands.actions.views import (
    ApplicationFieldIssue,
    ApplicationState,
    DomHandAssessStateParams,
    FormField,
    is_placeholder_value,
    normalize_name,
)
from ghosthands.platforms import detect_platform_from_signals, get_config_by_name

logger = logging.getLogger(__name__)


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
			if (text) {
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
	const bodyText = norm(document.body ? document.body.innerText : '');
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
		.filter(Boolean)
		.slice(0, 12);

	return JSON.stringify({
		body_text: bodyText,
		button_texts: buttonTexts,
		submit_visible: submitButtons.length > 0,
		submit_disabled: submitButtons.length > 0 && submitButtons.every((item) => item.disabled),
		advance_visible: advanceButtons.some((item) => !/\bsubmit\b/.test(item.lower)),
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
    return not bool((field.current_value or "").strip()) or is_placeholder_value(field.current_value)


def _widget_kind_for_field(field: FormField, browser_context: dict[str, Any] | None = None) -> str:
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


def _classify_terminal_state(
    platform: str,
    has_editable_fields: bool,
    submit_visible: bool,
    submit_disabled: bool,
    advance_visible: bool,
    unresolved_required: list[ApplicationFieldIssue],
    visible_errors: list[str],
    body_text: str,
) -> str:
    body_norm = normalize_name(body_text)
    if re.search(
        r"\b(thank you for applying|application submitted|application received|successfully submitted)\b", body_norm
    ):
        return "confirmation"
    if advance_visible:
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

    raw_fields_json = await page.evaluate(_EXTRACT_FIELDS_JS)
    raw_fields = json.loads(raw_fields_json) if isinstance(raw_fields_json, str) else raw_fields_json or []
    button_groups_json = await page.evaluate(_EXTRACT_BUTTON_GROUPS_JS)
    button_groups = json.loads(button_groups_json) if isinstance(button_groups_json, str) else button_groups_json or []
    fields = _group_form_fields(raw_fields, button_groups)

    page_scan_raw = await page.evaluate(_SCAN_PAGE_STATE_JS)
    page_scan = json.loads(page_scan_raw) if isinstance(page_scan_raw, str) else page_scan_raw or {}

    field_ids = [field.field_id for field in fields]
    layout_raw = await page.evaluate(_FIELD_LAYOUT_JS, field_ids)
    layout = json.loads(layout_raw) if isinstance(layout_raw, str) else layout_raw or {}

    button_texts = page_scan.get("button_texts", [])
    body_text = page_scan.get("body_text", "")
    current_url = await page.evaluate("() => window.location.href")
    platform_hint = detect_platform_from_signals(
        str(current_url or ""),
        page_text=" ".join(filter(None, [body_text, " ".join(button_texts)])),
        markers=page_scan.get("markers") or [],
    )

    unresolved_required: list[ApplicationFieldIssue] = []
    unresolved_optional: list[ApplicationFieldIssue] = []
    sections_in_view: list[str] = []
    field_context_by_id: dict[str, dict[str, Any]] = {}

    for field in fields:
        if field.field_type == "file" or _is_navigation_field(field):
            continue
        if params.target_section and normalize_name(params.target_section) not in normalize_name(field.section or ""):
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
        if field.section and relative_position == "in_view":
            sections_in_view.append(field.section)

        if field.field_type in {"radio-group", "button-group"} and not field.current_value:
            field.current_value = await _read_group_selection(page, field.field_id)
        elif field.field_type in {"checkbox", "toggle"} and not field.current_value:
            binary_state = await _read_binary_state(page, field.field_id)
            field.current_value = "checked" if binary_state else ""

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

    current_section = params.target_section or ""
    if not current_section:
        for issue in unresolved_required:
            if issue.section and issue.relative_position == "in_view":
                current_section = issue.section
                break
    if not current_section:
        for issue in unresolved_required:
            if issue.section:
                current_section = issue.section
                break
    if not current_section and sections_in_view:
        current_section = sections_in_view[0]

    visible_errors = [text for text in (page_scan.get("error_texts") or []) if text][:8]
    has_editable_fields = any(field.field_type != "file" and not _is_navigation_field(field) for field in fields)
    terminal_state = _classify_terminal_state(
        platform_hint,
        has_editable_fields,
        bool(page_scan.get("submit_visible")),
        bool(page_scan.get("submit_disabled")),
        bool(page_scan.get("advance_visible")),
        unresolved_required,
        visible_errors,
        body_text,
    )
    application_state = ApplicationState(
        terminal_state=terminal_state,
        current_section=current_section,
        unresolved_required_fields=unresolved_required,
        unresolved_optional_fields=unresolved_optional,
        visible_errors=visible_errors,
        scroll_bias=scroll_bias,
        submit_visible=bool(page_scan.get("submit_visible")),
        submit_disabled=bool(page_scan.get("submit_disabled")),
        advance_visible=bool(page_scan.get("advance_visible")),
        platform_hint=platform_hint,
    )

    summary_lines = [
        f"Application state: {application_state.terminal_state}",
        f"Current section: {application_state.current_section or '(unknown)'}",
        f"Unresolved required fields: {len(application_state.unresolved_required_fields)}",
        f"Visible errors: {len(application_state.visible_errors)}",
        f"Scroll bias: {application_state.scroll_bias}",
    ]
    if application_state.platform_hint:
        summary_lines.append(f"Platform hint: {application_state.platform_hint}")
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
    summary_lines.append("APPLICATION_STATE_JSON:")
    summary_lines.append(application_state.model_dump_json())
    summary = "\n".join(summary_lines)

    logger.info(summary)
    return ActionResult(
        extracted_content=summary,
        include_extracted_content_only_once=False,
    )
