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
    _build_inject_helpers_js,
    _field_already_matches,
    _field_has_validation_error,
    _fill_single_field,
    _filter_fields_for_focus,
    _filter_fields_for_scope,
    _preferred_field_label,
    _read_field_value,
    _read_group_selection,
    extract_visible_form_fields,
)
from ghosthands.actions.views import DomHandInteractControlParams, FormField

logger = logging.getLogger(__name__)

_INTERACTIVE_FIELD_TYPES = {
    "select",
    "radio-group",
    "radio",
    "button-group",
    "checkbox-group",
    "checkbox",
    "toggle",
}


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


def _candidate_score(field: FormField, desired_value: str) -> tuple[int, int]:
    score = 0
    label = _preferred_field_label(field)
    if label:
        score += 3
    options = field.options or field.choices or []
    if desired_value and any(desired_value.strip().lower() in str(option).strip().lower() for option in options):
        score += 2
    if field.required:
        score += 1
    return score, len(options)


async def _read_control_value(page, field: FormField) -> str:
    if field.field_type in {"radio-group", "radio", "button-group"}:
        return await _read_group_selection(page, field.field_id)
    return await _read_field_value(page, field.field_id)


async def _capture_control_screenshot(browser_session: BrowserSession, field_label: str) -> str | None:
    try:
        slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in field_label).strip("-")[:48] or "control"
        path = Path(tempfile.gettempdir()) / f"domhand-control-{slug}-{uuid4().hex[:8]}.png"
        await browser_session.take_screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:
        return None


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

    extracted_interactive_fields = [
        field
        for field in fields
        if field.field_type in _INTERACTIVE_FIELD_TYPES
    ]
    interactive_fields = _filter_fields_for_scope(
        extracted_interactive_fields,
        target_section=params.target_section,
        heading_boundary=params.heading_boundary,
    )
    logger.info(
        "domhand.interact_control.candidates "
        f"requested_label={params.field_label!r} "
        f"desired_value={params.desired_value!r} "
        f"target_section={params.target_section or ''!r} "
        f"heading_boundary={params.heading_boundary or ''!r} "
        f"visible_field_count={len(fields)} "
        f"total_interactive_field_count={len(extracted_interactive_fields)} "
        f"scoped_interactive_field_count={len(interactive_fields)} "
        f"scoped_candidates={_summarize_controls(interactive_fields)} "
        f"unscoped_candidates={_summarize_controls(extracted_interactive_fields)}"
    )
    focused = _filter_fields_for_focus(interactive_fields, [params.field_label])

    if not focused:
        available_labels = sorted({_preferred_field_label(field) for field in interactive_fields if _preferred_field_label(field)})
        screenshot_path = await _capture_control_screenshot(browser_session, params.field_label)
        logger.warning(
            "domhand.interact_control.no_match "
            f"requested_label={params.field_label!r} "
            f"desired_value={params.desired_value!r} "
            f"target_section={params.target_section or ''!r} "
            f"heading_boundary={params.heading_boundary or ''!r} "
            f"visible_field_count={len(fields)} "
            f"total_interactive_field_count={len(extracted_interactive_fields)} "
            f"scoped_interactive_field_count={len(interactive_fields)} "
            f"available_labels={available_labels[:20]} "
            f"scoped_candidates={_summarize_controls(interactive_fields)} "
            f"unscoped_candidates={_summarize_controls(extracted_interactive_fields)} "
            f"screenshot_path={screenshot_path or ''!r}"
        )
        details = (
            f'No visible interactive control matched "{params.field_label}". '
            f"Available controls: {available_labels[:12]}"
        )
        if screenshot_path:
            details += f" Screenshot captured: {screenshot_path}."
        return ActionResult(
            error=details
        )

    target = sorted(
        focused,
        key=lambda field: _candidate_score(field, params.desired_value),
        reverse=True,
    )[0]

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

    if await _field_already_matches(page, target, params.desired_value):
        current_value = await _read_control_value(page, target)
        has_error = await _field_has_validation_error(page, target.field_id)
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
        return ActionResult(
            extracted_content=(
                f'Control "{_preferred_field_label(target)}" already matched "{params.desired_value}".'
            ),
            include_extracted_content_only_once=False,
        )

    success = await _fill_single_field(page, target, params.desired_value)
    current_value = await _read_control_value(page, target)
    has_error = await _field_has_validation_error(page, target.field_id)

    if success and current_value:
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
        return ActionResult(
            extracted_content=(
                f'Interacted with "{_preferred_field_label(target)}" and set it to "{current_value}". '
                "Immediately call domhand_assess_state for this blocker."
            ),
            include_extracted_content_only_once=False,
        )

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
        f'Failed to confirm "{params.desired_value}" for "{_preferred_field_label(target)}". '
        f'Current value: "{current_value}". Field type: {target.field_type}. '
        f"Validation error present: {has_error}."
    )
    if screenshot_path:
        details += f" Screenshot captured: {screenshot_path}."
    return ActionResult(error=details)
