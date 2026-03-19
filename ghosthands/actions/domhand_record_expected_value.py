"""Record an expected field value after raw manual recovery actions."""

import logging

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession

from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    _filter_fields_for_scope,
    _get_page_context_key,
    _preferred_field_label,
    _resolve_focus_fields,
    _safe_page_url,
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
    for field in fields:
        if requested_id and field.field_id != requested_id:
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
        return ActionResult(
            error=(
                f'No visible field matched field_id="{params.field_id}"'
                + (f' and field_type="{params.field_type}"' if params.field_type else "")
                + ". Refusing to fall back to label-only matching."
            ),
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
        expected_value=params.expected_value,
        source="manual_recovery",
    )

    logger.info(
        "domhand.record_expected_value",
        extra={
            "field_id": target.field_id,
            "field_key": field_key,
            "page_context_key": page_context_key,
            "field_label": _preferred_field_label(target),
            "expected_value": params.expected_value,
            "target_section": params.target_section,
            "heading_boundary": params.heading_boundary,
        },
    )
    return ActionResult(
        extracted_content=(
            f'Recorded expected value "{params.expected_value}" for '
            f'"{_preferred_field_label(target)}" ({target.field_type}). '
            "Immediately call domhand_assess_state before any unrelated action."
        ),
        include_extracted_content_only_once=False,
    )
