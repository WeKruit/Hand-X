"""Record an expected field value after raw manual recovery actions."""

import logging

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession

from ghosthands.step_trace import publish_browser_session_trace, update_blocker_attempt_state
from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    _filter_fields_for_scope,
    _field_has_validation_error,
    _field_value_matches_expected,
    _get_page_context_key,
    _preferred_field_label,
    _read_binary_state,
    _read_field_value_for_field,
    _read_group_selection,
    _resolve_focus_fields,
    _safe_page_url,
    _value_shape_is_compatible,
    extract_visible_form_fields,
)
from ghosthands.actions.views import DomHandRecordExpectedValueParams, FormField, get_stable_field_key
from ghosthands.runtime_learning import detect_host_from_url, record_expected_field_value

logger = logging.getLogger(__name__)


def _match_exact_field(
    fields: list[FormField],
    *,
    field_id: str | None,
    field_type: str | None,
) -> FormField | None:
    requested_id = str(field_id or "").strip()
    requested_type = str(field_type or "").strip().lower()
    if not requested_id:
        return None
    for field in fields:
        if field.field_id != requested_id:
            continue
        if requested_type and field.field_type.lower() != requested_type:
            continue
        return field
    return None


async def domhand_record_expected_value(
    params: DomHandRecordExpectedValueParams,
    browser_session: BrowserSession,
) -> ActionResult:
    """Record the intended value for one field after a raw manual recovery action."""
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
        return ActionResult(error=f"Failed to extract visible fields: {exc}")

    scoped_fields = _filter_fields_for_scope(
        fields,
        target_section=params.target_section,
        heading_boundary=params.heading_boundary,
        focus_fields=[params.field_label],
    )
    target = _match_exact_field(
        scoped_fields,
        field_id=params.field_id,
        field_type=params.field_type,
    )
    if params.field_id and target is None:
        stale_id_candidate = next(
            (field for field in scoped_fields if field.field_id == params.field_id),
            None,
        )
        if stale_id_candidate is not None:
            return ActionResult(
                error=(
                    f'No visible field matched field_id="{params.field_id}"'
                    + (f' and field_type="{params.field_type}"' if params.field_type else "")
                    + ". Refusing to fall back to label-only matching."
                ),
            )

        fallback_fields = scoped_fields
        if params.field_type:
            requested_type = str(params.field_type).strip().lower()
            fallback_fields = [
                field for field in scoped_fields if field.field_type.lower() == requested_type
            ]
        focused = _resolve_focus_fields(fallback_fields, [params.field_label])
        if focused.ambiguous_labels:
            details = ", ".join(
                f'{label}: {[f"{field.field_id} ({field.field_type})" for field in matches]}'
                for label, matches in focused.ambiguous_labels.items()
            )
            return ActionResult(
                error=(
                    "Provided field_id is stale and label fallback is ambiguous. "
                    f"Provide the live field_id before recording an expected value. {details}"
                ),
            )
        if len(focused.fields) != 1:
            return ActionResult(
                error=(
                    f'No visible field matched field_id="{params.field_id}"'
                    + (f' and field_type="{params.field_type}"' if params.field_type else "")
                    + ". Refusing to fall back to label-only matching."
                ),
            )
        target = focused.fields[0]
        logger.info(
            "domhand.record_expected_value.stale_field_id_fallback",
            extra={
                "requested_field_id": params.field_id,
                "resolved_field_id": target.field_id,
                "field_label": _preferred_field_label(target),
                "field_type": target.field_type,
            },
        )
    if target is None:
        focused = _resolve_focus_fields(scoped_fields, [params.field_label])
        if focused.ambiguous_labels:
            details = ", ".join(
                f'{label}: {[f"{field.field_id} ({field.field_type})" for field in matches]}'
                for label, matches in focused.ambiguous_labels.items()
            )
            return ActionResult(
                error=(
                    "Multiple visible fields matched the requested label. "
                    f"Provide the exact field_id and field_type before recording an expected value. {details}"
                ),
            )
        if focused.fields:
            target = focused.fields[0]

    if target is None:
        available = sorted({
            _preferred_field_label(field)
            for field in scoped_fields
            if _preferred_field_label(field)
        })
        return ActionResult(
            error=(
                f'No visible field matched "{params.field_label}". '
                f"Available fields: {available[:12]}"
            ),
        )

    if target.field_type in {"checkbox", "toggle"}:
        binary_state = await _read_binary_state(page, target.field_id)
        observed_value = "checked" if binary_state else ""
    elif target.field_type in {"radio-group", "button-group"}:
        observed_value = await _read_group_selection(page, target.field_id)
    else:
        observed_value = await _read_field_value_for_field(page, target)
    has_validation_error = await _field_has_validation_error(page, target.field_id)
    if not _field_value_matches_expected(observed_value, params.expected_value):
        return ActionResult(
            error=(
                f'Field "{_preferred_field_label(target)}" does not yet visibly match the intended value. '
                f'Observed "{observed_value or ""}", expected "{params.expected_value}". '
                "Re-commit the field and reassess before recording an expected value."
            ),
        )
    if has_validation_error:
        return ActionResult(
            error=(
                f'Field "{_preferred_field_label(target)}" still has an active validation error. '
                "Clear validation before recording an expected value."
            ),
        )
    if not _value_shape_is_compatible(target, params.expected_value):
        return ActionResult(
            error=(
                f'Field "{_preferred_field_label(target)}" cannot store "{params.expected_value}" as an expected value '
                "because it is incompatible with the field's current answer shape."
            ),
        )

    page_host = detect_host_from_url(await _safe_page_url(page))
    page_context_key = await _get_page_context_key(
        page,
        fields=scoped_fields,
        fallback_marker=params.target_section or params.heading_boundary,
    )
    field_key = get_stable_field_key(target)
    record_expected_field_value(
        host=page_host,
        page_context_key=page_context_key,
        field_key=field_key,
        field_label=_preferred_field_label(target),
        field_type=target.field_type,
        field_section=target.section or "",
        field_fingerprint=target.field_fingerprint or "",
        expected_value=params.expected_value,
        source="manual_recovery",
    )
    update_blocker_attempt_state(
        browser_session,
        field_key=field_key,
        field_id=target.field_id,
        strategy="domhand_record_expected_value",
        desired_value=params.expected_value,
        observed_value=observed_value,
        visible_error="",
        retry_capped=False,
        success=True,
        state_change="changed",
        recommended_next_action="continue_current_recovery",
    )
    await publish_browser_session_trace(
        browser_session,
        "manual_recovery_attempt",
        {
            "tool": "domhand_record_expected_value",
            "field_id": target.field_id,
            "field_key": field_key,
            "field_label": _preferred_field_label(target),
            "field_type": target.field_type,
            "expected_value": params.expected_value,
            "observed_value": observed_value,
            "state_change": "changed",
            "recommended_next_action": "continue_current_recovery",
        },
    )

    logger.info(
        "domhand.record_expected_value",
        extra={
            "field_id": target.field_id,
            "field_key": field_key,
            "page_context_key": page_context_key,
            "field_label": _preferred_field_label(target),
            "field_type": target.field_type,
            "field_section": target.section or "",
            "field_fingerprint": target.field_fingerprint or "",
            "expected_value": params.expected_value,
            "observed_value": observed_value,
            "validation_cleared": not has_validation_error,
            "target_section": params.target_section,
            "heading_boundary": params.heading_boundary,
        },
    )
    return ActionResult(
        extracted_content=(
            f'Recorded expected value "{params.expected_value}" for '
            f'"{_preferred_field_label(target)}" ({target.field_type}).'
        ),
        include_extracted_content_only_once=False,
        metadata={
            "tool": "domhand_record_expected_value",
            "field_id": target.field_id,
            "field_key": field_key,
            "strategy": "domhand_record_expected_value",
            "state_change": "changed",
            "retry_capped": False,
            "recommended_next_action": "continue_current_recovery",
        },
    )
