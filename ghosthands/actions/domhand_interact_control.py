"""DomHand exact-field interaction for stateful controls.

This action is an additive fallback for stubborn non-text controls where the
agent already knows the exact question label and desired answer. It keeps the
interaction inside DomHand instead of falling back to blind browser-use clicks.
"""

import logging
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession

from ghosthands.actions.domhand_fill import (
    _CLICK_BINARY_FIELD_JS,
    _CLICK_RADIO_OPTION_JS,
    DOMHAND_RETRY_CAPPED,
    _attempt_domhand_fill_with_retry_cap,
    _build_inject_helpers_js,
    _click_binary_with_gui,
    _click_group_option_with_gui,
    _field_already_matches,
    _field_has_validation_error,
    _field_matches_focus_label,
    _filter_fields_for_scope,
    _fill_single_field,
    _get_page_context_key,
    _is_explicit_false,
    _normalize_match_label,
    _preferred_field_label,
    _read_checkbox_group_value,
    _read_field_value_for_field,
    _read_group_selection,
    _record_expected_value_if_settled,
    _reset_group_selection_with_gui,
    _safe_page_url,
    extract_visible_form_fields,
)
from ghosthands.step_trace import publish_browser_session_trace, update_blocker_attempt_state
from ghosthands.actions.views import DomHandInteractControlParams, FormField, get_stable_field_key
from ghosthands.runtime_learning import detect_host_from_url

logger = logging.getLogger(__name__)

_SUPPORTED_FIELD_TYPES = {
    "select",
    "radio-group",
    "radio",
    "button-group",
    "checkbox-group",
    "checkbox",
    "toggle",
    "text",
    "email",
    "tel",
    "url",
    "number",
    "password",
    "search",
    "date",
    "textarea",
}


def _match_exact_control(
    fields: list[FormField],
    *,
    field_id: str | None,
    field_type: str | None,
) -> FormField | None:
    requested_id = str(field_id or "").strip()
    requested_type = str(field_type or "").strip().lower()
    if not requested_id and not requested_type:
        return None
    for field in fields:
        if requested_id and field.field_id != requested_id:
            continue
        if requested_type and field.field_type.lower() != requested_type:
            continue
        return field
    return None


def _control_debug_enabled() -> bool:
    return os.getenv("GH_DEBUG_PROFILE_PASS_THROUGH") == "1"


def _summarize_controls(fields: list[FormField]) -> list[dict[str, object]]:
    """Return a compact log-friendly snapshot of visible interactive controls."""
    snapshot: list[dict[str, object]] = []
    for field in fields[:20]:
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


def _normalize_control_text(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _candidate_score(field: FormField, desired_value: str, requested_label: str) -> tuple[int, int, int]:
    score = 0
    label = _preferred_field_label(field)
    normalized_label = _normalize_control_text(label)
    normalized_requested = _normalize_control_text(requested_label)
    if normalized_label and normalized_requested:
        if normalized_label == normalized_requested:
            score += 100
        elif normalized_requested in normalized_label or normalized_label in normalized_requested:
            score += 50
    if label:
        score += 3
    options = field.options or field.choices or []
    if desired_value and any(desired_value.strip().lower() in str(option).strip().lower() for option in options):
        score += 2
    if field.required:
        score += 1
    return score, len(options), len(label or "")


async def _read_control_value(page, field: FormField) -> str:
    if field.field_type == "checkbox-group":
        return await _read_checkbox_group_value(page, field)
    if field.field_type in {"radio-group", "radio", "button-group"}:
        return await _read_group_selection(page, field.field_id)
    return await _read_field_value_for_field(page, field)


async def _capture_control_screenshot(browser_session: BrowserSession, field_label: str) -> str | None:
    try:
        slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in field_label).strip("-")[:48] or "control"
        path = Path(tempfile.gettempdir()) / f"domhand-control-{slug}-{uuid4().hex[:8]}.png"
        await browser_session.take_screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return None


async def _attempt_exact_control_recovery(page, field: FormField, desired_value: str) -> tuple[bool, str]:
    """Use a live field-bound recovery path for capped or stubborn non-text controls."""
    tag = f"[{_preferred_field_label(field) or field.field_id}]"
    if field.field_type in {"radio-group", "radio", "button-group"}:
        current_value = await _read_group_selection(page, field.field_id)
        if current_value and not _is_explicit_false(current_value):
            if await _reset_group_selection_with_gui(page, field, current_value, desired_value, tag):
                return True, "exact_group_reset"
        if await _click_group_option_with_gui(page, field, desired_value, tag):
            return True, "exact_group_gui"
        try:
            await page.evaluate(_CLICK_RADIO_OPTION_JS, field.field_id, desired_value)
            if await _field_already_matches(page, field, desired_value):
                return True, "exact_group_dom"
        except Exception:
            pass
        return False, "exact_group_failed"
    if field.field_type in {"checkbox", "toggle"}:
        desired_checked = not _is_explicit_false(desired_value)
        if await _click_binary_with_gui(page, field, tag, desired_checked):
            return True, "exact_binary_gui"
        try:
            await page.evaluate(_CLICK_BINARY_FIELD_JS, field.field_id, desired_checked)
            if await _field_already_matches(page, field, desired_value):
                return True, "exact_binary_dom"
        except Exception:
            pass
        return False, "exact_binary_failed"
    if field.field_type == "checkbox-group":
        if await _click_group_option_with_gui(page, field, desired_value, tag):
            return True, "exact_checkbox_group_gui"
        try:
            await page.evaluate(_CLICK_RADIO_OPTION_JS, field.field_id, desired_value)
            if await _field_already_matches(page, field, desired_value):
                return True, "exact_checkbox_group_dom"
        except Exception:
            pass
    if field.field_type in {"text", "email", "tel", "url", "number", "password", "search", "date", "textarea"}:
        try:
            if await _fill_single_field(page, field, desired_value) and await _field_already_matches(page, field, desired_value):
                return True, "exact_text_like_fill"
        except Exception:
            pass
    return False, "unsupported_exact_recovery"


async def domhand_interact_control(
    params: DomHandInteractControlParams,
    browser_session: BrowserSession,
) -> ActionResult:
    """Resolve and interact with one exact stateful control by label."""
    page = await browser_session.get_current_page()
    if not page:
        return ActionResult(error="No active page found in browser session")

    try:
        from ghosthands.dom.shadow_helpers import ensure_helpers

        await ensure_helpers(page)
    except Exception:
        pass

    await page.evaluate(_build_inject_helpers_js())

    try:
        fields = await extract_visible_form_fields(page)
    except Exception as exc:
        return ActionResult(error=f"Failed to extract controls: {exc}")

    extracted_supported_fields = [
        field
        for field in fields
        if field.field_type in _SUPPORTED_FIELD_TYPES
    ]
    scoped_supported_fields = _filter_fields_for_scope(
        extracted_supported_fields,
        target_section=params.target_section,
        heading_boundary=params.heading_boundary,
        focus_fields=[params.field_label],
    )
    logger.info(
        "domhand.interact_control.candidates "
        f"requested_label={params.field_label!r} "
        f"desired_value={params.desired_value!r} "
        f"target_section={params.target_section or ''!r} "
        f"heading_boundary={params.heading_boundary or ''!r} "
        f"visible_field_count={len(fields)} "
        f"total_supported_field_count={len(extracted_supported_fields)} "
        f"scoped_supported_field_count={len(scoped_supported_fields)} "
        f"scoped_candidates={_summarize_controls(scoped_supported_fields)} "
        f"unscoped_candidates={_summarize_controls(extracted_supported_fields)}"
    )
    target = _match_exact_control(
        scoped_supported_fields,
        field_id=params.field_id,
        field_type=params.field_type,
    )
    if params.field_id and target is None:
        return ActionResult(
            error=(
                f'No visible interactive control matched field_id="{params.field_id}"'
                + (f' and field_type="{params.field_type}"' if params.field_type else "")
                + ". Refusing to fall back to label-only matching."
            ),
        )
    focused: list[FormField]
    if target is None:
        normalized_requested_label = _normalize_match_label(params.field_label)
        focused = [
            field
            for field in scoped_supported_fields
            if normalized_requested_label and _field_matches_focus_label(field, normalized_requested_label)
        ]
    else:
        focused = [target]

    if not focused:
        available_labels = sorted({_preferred_field_label(field) for field in scoped_supported_fields if _preferred_field_label(field)})
        screenshot_path = await _capture_control_screenshot(browser_session, params.field_label)
        logger.warning(
            "domhand.interact_control.no_match "
            f"requested_label={params.field_label!r} "
            f"desired_value={params.desired_value!r} "
            f"target_section={params.target_section or ''!r} "
            f"heading_boundary={params.heading_boundary or ''!r} "
            f"visible_field_count={len(fields)} "
            f"total_supported_field_count={len(extracted_supported_fields)} "
            f"scoped_supported_field_count={len(scoped_supported_fields)} "
            f"available_labels={available_labels[:20]} "
            f"scoped_candidates={_summarize_controls(scoped_supported_fields)} "
            f"unscoped_candidates={_summarize_controls(extracted_supported_fields)} "
            f"screenshot_path={screenshot_path or ''!r}"
        )
        details = (
            f'No visible supported field matched "{params.field_label}". '
            f"Available fields: {available_labels[:12]}"
        )
        if screenshot_path:
            details += f" Screenshot captured: {screenshot_path}."
        return ActionResult(
            error=details
        )

    target = sorted(
        focused,
        key=lambda field: _candidate_score(field, params.desired_value, params.field_label),
        reverse=True,
    )[0]
    page_host = detect_host_from_url(await _safe_page_url(page))
    page_context_key = await _get_page_context_key(
        page,
        fallback_marker=params.target_section or params.heading_boundary,
    )

    logger.info(
        "domhand.interact_control.start",
        extra={
            "field_label": _preferred_field_label(target),
            "requested_label": params.field_label,
            "desired_value": params.desired_value,
            "field_type": target.field_type,
            "section": target.section,
        },
    )
    await publish_browser_session_trace(
        browser_session,
        "tool_attempt",
        {
            "tool": "domhand_interact_control",
            "field_id": target.field_id,
            "field_key": get_stable_field_key(target),
            "field_label": _preferred_field_label(target),
            "field_type": target.field_type,
            "desired_value": params.desired_value,
            "target_section": params.target_section or "",
            "heading_boundary": params.heading_boundary or "",
        },
    )

    if await _field_already_matches(page, target, params.desired_value):
        current_value = await _read_control_value(page, target)
        has_error = await _field_has_validation_error(page, target.field_id)
        await _record_expected_value_if_settled(
            page=page,
            host=page_host,
            page_context_key=page_context_key,
            field=target,
            field_key=get_stable_field_key(target),
            expected_value=params.desired_value,
            source="derived_profile",
            log_context="domhand.interact_control",
        )
        logger.info(
            "domhand.interact_control.already_matched",
            extra={
                "field_label": _preferred_field_label(target),
                "requested_label": params.field_label,
                "desired_value": params.desired_value,
                "current_value": current_value,
                "field_type": target.field_type,
                "has_error": has_error,
                "section": target.section,
            },
        )
        await publish_browser_session_trace(
            browser_session,
            "tool_result",
            {
                "tool": "domhand_interact_control",
                "field_id": target.field_id,
                "field_key": get_stable_field_key(target),
                "field_label": _preferred_field_label(target),
                "field_type": target.field_type,
                "desired_value": params.desired_value,
                "observed_value": current_value,
                "visible_error": str(has_error),
                "strategy": "already_matched",
                "retry_capped": False,
                "state_change": "unchanged",
                "recommended_next_action": "call domhand_assess_state",
            },
        )
        return ActionResult(
            extracted_content=(
                f'Control "{_preferred_field_label(target)}" already matched "{params.desired_value}".'
            ),
            include_extracted_content_only_once=False,
            metadata={
                "tool": "domhand_interact_control",
                "field_id": target.field_id,
                "field_key": get_stable_field_key(target),
                "strategy": "already_matched",
                "state_change": "unchanged",
                "retry_capped": False,
                "recommended_next_action": "call domhand_assess_state",
            },
        )

    success, failure_error, failure_reason, _fc = await _attempt_domhand_fill_with_retry_cap(
        page,
        host=page_host,
        field=target,
        desired_value=params.desired_value,
        tool_name="domhand_interact_control",
    )
    current_value = await _read_control_value(page, target)
    has_error = await _field_has_validation_error(page, target.field_id)
    strategy = "domhand_interact_control"

    if not success:
        exact_success, exact_strategy = await _attempt_exact_control_recovery(page, target, params.desired_value)
        if exact_success:
            success = True
            strategy = exact_strategy
            failure_error = None
            failure_reason = None
            current_value = await _read_control_value(page, target)
            has_error = await _field_has_validation_error(page, target.field_id)
            await publish_browser_session_trace(
                browser_session,
                "manual_recovery_attempt",
                {
                    "tool": "domhand_interact_control",
                    "field_id": target.field_id,
                    "field_key": get_stable_field_key(target),
                    "field_label": _preferred_field_label(target),
                    "field_type": target.field_type,
                    "desired_value": params.desired_value,
                    "strategy": exact_strategy,
                },
            )

    settled = False
    if success:
        settled = await _field_already_matches(page, target, params.desired_value)

    if success and settled:
        await _record_expected_value_if_settled(
            page=page,
            host=page_host,
            page_context_key=page_context_key,
            field=target,
            field_key=get_stable_field_key(target),
            expected_value=params.desired_value,
            source="derived_profile",
            log_context="domhand.interact_control",
        )
        update_blocker_attempt_state(
            browser_session,
            field_key=get_stable_field_key(target),
            field_id=target.field_id,
            strategy=strategy,
            desired_value=params.desired_value,
            observed_value=current_value,
            visible_error="",
            retry_capped=False,
            success=True,
            state_change="changed",
            recommended_next_action="call domhand_assess_state",
        )
        logger.info(
            "domhand.interact_control.result",
            extra={
                "field_label": _preferred_field_label(target),
                "desired_value": params.desired_value,
                "current_value": current_value,
                "field_type": target.field_type,
                "has_error": has_error,
                "section": target.section,
            },
        )
        await publish_browser_session_trace(
            browser_session,
            "tool_result",
            {
                "tool": "domhand_interact_control",
                "field_id": target.field_id,
                "field_key": get_stable_field_key(target),
                "field_label": _preferred_field_label(target),
                "field_type": target.field_type,
                "desired_value": params.desired_value,
                "observed_value": current_value,
                "visible_error": "",
                "strategy": strategy,
                "retry_capped": False,
                "state_change": "changed",
                "recommended_next_action": "call domhand_assess_state",
            },
        )
        return ActionResult(
            extracted_content=(
                f'Interacted with "{_preferred_field_label(target)}" and set it to "{current_value}". '
                "Immediately call domhand_assess_state for this blocker."
            ),
            include_extracted_content_only_once=False,
            metadata={
                "tool": "domhand_interact_control",
                "field_id": target.field_id,
                "field_key": get_stable_field_key(target),
                "strategy": strategy,
                "state_change": "changed",
                "retry_capped": False,
                "recommended_next_action": "call domhand_assess_state",
            },
        )
    if success and not settled:
        failure_error = "Control interaction did not settle to the requested value."
        failure_reason = failure_reason or "no_state_change"

    screenshot_path = await _capture_control_screenshot(browser_session, _preferred_field_label(target))
    logger.warning(
        "domhand.interact_control.failed",
        extra={
            "field_label": _preferred_field_label(target),
            "desired_value": params.desired_value,
            "current_value": current_value,
            "field_type": target.field_type,
            "has_error": has_error,
            "screenshot_path": screenshot_path,
        },
    )
    details = (
        f'{failure_error or "Failed to confirm the requested value."} '
        f'Failed to confirm "{params.desired_value}" for "{_preferred_field_label(target)}". '
        f'Current value: "{current_value}". Field type: {target.field_type}. '
        f"Validation error present: {has_error}."
    )
    if screenshot_path:
        details += f" Screenshot captured: {screenshot_path}."
    if failure_reason == DOMHAND_RETRY_CAPPED:
        details += " Do not repeat the same DomHand strategy on this field/value pair in this run."
    retry_capped = failure_reason == DOMHAND_RETRY_CAPPED
    update_blocker_attempt_state(
        browser_session,
        field_key=get_stable_field_key(target),
        field_id=target.field_id,
        strategy=strategy,
        desired_value=params.desired_value,
        observed_value=current_value,
        visible_error=str(has_error),
        retry_capped=retry_capped,
        success=False,
        state_change="no_state_change",
        recommended_next_action=(
            "change strategy for this blocker and reassess immediately"
            if retry_capped
            else "call domhand_assess_state after a different control-targeted recovery"
        ),
    )
    await publish_browser_session_trace(
        browser_session,
        "tool_result",
        {
            "tool": "domhand_interact_control",
            "field_id": target.field_id,
            "field_key": get_stable_field_key(target),
            "field_label": _preferred_field_label(target),
            "field_type": target.field_type,
            "desired_value": params.desired_value,
            "observed_value": current_value,
            "visible_error": str(has_error),
            "strategy": strategy,
            "retry_capped": retry_capped,
            "state_change": "no_state_change",
            "recommended_next_action": (
                "change strategy for this blocker and reassess immediately"
                if retry_capped
                else "call domhand_assess_state after a different control-targeted recovery"
            ),
        },
    )
    return ActionResult(
        error=details,
        metadata={
            "tool": "domhand_interact_control",
            "field_id": target.field_id,
            "field_key": get_stable_field_key(target),
            "strategy": strategy,
            "state_change": "no_state_change",
            "retry_capped": retry_capped,
            "recommended_next_action": (
                "change strategy for this blocker and reassess immediately"
                if retry_capped
                else "call domhand_assess_state after a different control-targeted recovery"
            ),
        },
    )
