"""Per-control-type form field fill strategies.

Extracted from ``ghosthands.actions.domhand_fill`` to keep each module focused on
a single concern.  This module owns the ``_fill_single_field`` dispatcher and every
control-specific fill function (text, textarea, select, dropdown, radio, checkbox,
toggle, date, grouped-date, button-group).

Dependencies that still live in ``domhand_fill`` or sibling ``dom.*`` modules are
accessed via late imports to avoid circular references.
"""

import asyncio
import contextlib
import json
import re
import time
from datetime import date
from typing import Any

import structlog

from browser_use.browser import BrowserSession
from ghosthands.actions.combobox_toggle import (
    CLICK_COMBOBOX_TOGGLE_BY_FFID_JS,
    CLICK_INPUT_BY_FFID_JS,
    combobox_toggle_clicked,
)
from ghosthands.actions.views import (
    FormField,
    generate_dropdown_search_terms,
    normalize_name,
    split_dropdown_value_hierarchy,
)
from ghosthands.dom.dropdown_fill import fill_interactive_dropdown
from ghosthands.dom.dropdown_match import (
    SCAN_VISIBLE_OPTIONS_JS,
    synonym_groups_for_js,
)
from ghosthands.dom.dropdown_verify import selection_matches_desired
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
    _READ_BINARY_STATE_JS,
    _READ_FIELD_VALUE_JS,
    _READ_GROUP_SELECTION_JS,
    _SELECT_GROUPED_DATE_PICKER_VALUE_JS,
)
from ghosthands.runtime_learning import (
    detect_host_from_url,
    detect_platform_from_url,
    get_interaction_recipe,
    record_interaction_recipe,
)

logger = structlog.get_logger(__name__)


# ── Platform helpers ────────────────────────────────────────────────────────

async def get_platform_selector(page: Any, role: str) -> str | None:
    """Look up a CSS selector for *role* from the current page's platform config.

    Returns ``None`` if no platform match or the role isn't mapped.
    """
    try:
        from ghosthands.platforms import get_automation_id_map
        url = await page.evaluate("() => location.href")
        return get_automation_id_map(url).get(role)
    except Exception:
        return None


# ── Late-import delegates for helpers still in domhand_fill / sibling modules ─

def _preferred_field_label(field: FormField) -> str:
    from ghosthands.actions.domhand_fill import _preferred_field_label as _impl
    return _impl(field)


def _is_effectively_unset_field_value(value: str | None) -> bool:
    from ghosthands.actions.domhand_fill import _is_effectively_unset_field_value as _impl
    return _impl(value)


def _is_explicit_false(val: str | None) -> bool:
    from ghosthands.actions.domhand_fill import _is_explicit_false as _impl
    return _impl(val)


def _is_skill_like(field_name: str) -> bool:
    from ghosthands.actions.domhand_fill import _is_skill_like as _impl
    return _impl(field_name)


def _field_widget_kind_for_debug(field: FormField) -> str:
    from ghosthands.actions.domhand_fill import _field_widget_kind_for_debug as _impl
    return _impl(field)


def _trace_profile_resolution(event: str, *, field_label: str, **extra: Any) -> None:
    from ghosthands.actions.domhand_fill import _trace_profile_resolution as _impl
    return _impl(event, field_label=field_label, **extra)


def _safe_page_url(page: Any) -> Any:
    from ghosthands.actions.domhand_fill import _safe_page_url as _impl
    return _impl(page)


def _get_profile_data() -> dict[str, Any]:
    from ghosthands.actions.domhand_fill import _get_profile_data as _impl
    return _impl()


def _compose_grouped_date_value(month: str | None, day: str | None, year: str | None) -> str:
    from ghosthands.actions.domhand_fill import _compose_grouped_date_value as _impl
    return _impl(month, day, year)


async def extract_visible_form_fields(page: Any) -> list[FormField]:
    from ghosthands.actions.domhand_fill import extract_visible_form_fields as _impl
    return await _impl(page)


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



def _parse_dropdown_click_result(raw_result: Any) -> dict[str, Any]:
    """Normalize dropdown click helper results into a dict."""
    if isinstance(raw_result, str):
        try:
            parsed = json.loads(raw_result)
        except json.JSONDecodeError:
            return {"clicked": False}
        return parsed if isinstance(parsed, dict) else {"clicked": False}
    return raw_result if isinstance(raw_result, dict) else {"clicked": False}


def _field_value_matches_expected(current: str, expected: str, matched_label: str | None = None) -> bool:
    """Return True when the visible field value reflects the intended selection.

    Delegates to ``ghosthands.dom.dropdown_verify.selection_matches_desired``
    which is the single source of truth for this check.  The *matched_label*
    parameter is threaded through so callers that clicked a fuzzy-matched
    option can pass the actual label they clicked.
    """
    if _is_effectively_unset_field_value((current or "").strip()):
        return False
    return selection_matches_desired(current, expected, matched_label=matched_label)


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


async def _dispatch_platform_fill(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
    strategy: str,
    *,
    browser_session: BrowserSession | None = None,
) -> bool | None:
    """Try a platform-specific fill strategy.

    Returns ``True``/``False`` if the strategy handled the field,
    or ``None`` to fall through to default dispatch.
    """
    match strategy:
        case "combobox_toggle":
            return await _fill_custom_dropdown(page, field, value, tag, browser_session=browser_session)
        case "react_select":
            # Same path as non-native <select>: CDP open → discover options → click match,
            # then fill_interactive_dropdown with real keyboard typing. The old
            # _fill_searchable_dropdown path used JS fill on the input and skipped CDP-first,
            # which breaks Greenhouse react-select (typing fragments like "answer").
            return await _fill_custom_dropdown(
                page, field, value, tag, browser_session=browser_session
            )
        case "segmented_date":
            return await _fill_grouped_date_field(page, field, value, tag)
        case "searchable_dropdown":
            return await _fill_searchable_dropdown(page, field, value, tag)
        case "playwright_fill":
            return await _fill_text_field(page, field, value, tag)
        case _:
            logger.warning(
                "domhand.unknown_platform_strategy",
                strategy=strategy,
                field_type=field.field_type,
            )
            return None


async def _fill_single_field(
    page: Any,
    field: FormField,
    value: str,
    *,
    browser_session: BrowserSession | None = None,
) -> bool:
    ff_id = field.field_id
    tag = f"[{field.name or field.field_type}]"

    try:
        exists_json = await page.evaluate(_ELEMENT_EXISTS_JS, ff_id, field.field_type)
        if not json.loads(exists_json):
            logger.debug(f"skip {tag} (not visible)")
            return False
    except Exception:
        pass

    fill_strategy: str | None = None
    try:
        from ghosthands.platforms import get_fill_overrides
        page_url = await page.evaluate("() => location.href")
        overrides = get_fill_overrides(page_url)
        fill_strategy = overrides.get(field.field_type)
        if fill_strategy:
            logger.debug(
                "domhand.platform_fill_override",
                field_type=field.field_type,
                strategy=fill_strategy,
                field_label=field.name,
            )
    except Exception:
        pass

    if fill_strategy:
        result = await _dispatch_platform_fill(
            page, field, value, tag, fill_strategy, browser_session=browser_session,
        )
        if result is not None:
            return result

    match field.field_type:
        case "text" | "email" | "tel" | "url" | "number" | "password" | "search":
            return await _fill_text_field(page, field, value, tag)
        case "date":
            return await _fill_date_field(page, field, value, tag)
        case "textarea":
            return await _fill_textarea_field(page, field, value, tag)
        case "select":
            return await _fill_select_field(page, field, value, tag, browser_session=browser_session)
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

    async def _open() -> None:
        await _try_open_combobox_menu(page, ff_id, tag=tag)

    async def _read() -> str:
        return await _read_field_value_for_field(page, field)

    async def _type(text: str) -> None:
        await page.evaluate(_FOCUS_AND_CLEAR_JS, ff_id)
        await asyncio.sleep(0.1)
        await page.evaluate(_FILL_FIELD_JS, ff_id, text, "text")
        await page.evaluate(
            r"""(ffId) => {
            var el = window.__ff ? window.__ff.byId(ffId) : null;
            if (el) { el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('keyup', {bubbles: true})); }
            return 'ok';
        }""",
            ff_id,
        )

    async def _clear() -> None:
        await _clear_dropdown_search(page)

    async def _settle() -> None:
        await _settle_dropdown_selection(page)

    async def _dismiss() -> None:
        try:
            await page.evaluate(_DISMISS_DROPDOWN_JS)
        except Exception:
            pass

    result = await fill_interactive_dropdown(
        page,
        value,
        open_fn=_open,
        read_value_fn=_read,
        settle_fn=_settle,
        dismiss_fn=_dismiss,
        type_fn=_type,
        clear_fn=_clear,
        tag=f"search-select {tag}",
    )
    return result.success


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

    # Workday (and similar) often wrap compensation in a controlled textarea: DOM .value can
    # look set while validation still sees empty unless we run the same commit path as text
    # fields (blur / dismiss-overlay) and/or real Playwright typing.
    salary_like = _is_salary_like_field(field)
    if salary_like:
        try:
            if await _fill_text_like_with_keyboard(page, field, value, tag):
                logger.debug(f'fill {tag} = "{value[:80]}..." (keyboard-first compensation textarea)')
                return True
        except Exception:
            pass

    try:
        result_json = await page.evaluate(_FILL_FIELD_JS, field.field_id, value, "textarea")
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
        if (
            isinstance(result, dict)
            and result.get("success")
            and await _confirm_text_like_value(page, field, value, tag)
        ):
            logger.debug(f'fill {tag} = "{value[:80]}{"..." if len(value) > 80 else ""}"')
            return True
    except Exception:
        pass
    try:
        result_json = await page.evaluate(_FILL_CONTENTEDITABLE_JS, field.field_id, value)
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
        if (
            isinstance(result, dict)
            and result.get("success")
            and await _confirm_text_like_value(page, field, value, tag)
        ):
            logger.debug(f'fill {tag} = "{value[:80]}..." (contenteditable)')
            return True
    except Exception:
        pass

    if not salary_like:
        try:
            if await _fill_text_like_with_keyboard(page, field, value, tag):
                logger.debug(f'fill {tag} = "{value[:80]}..." (keyboard fallback textarea)')
                return True
        except Exception:
            pass

    logger.debug(f"skip {tag} (textarea not fillable)")
    return False


async def _find_dom_tree_node_by_ff_id(browser_session: BrowserSession, ff_id: str) -> Any:
    """Resolve ``EnhancedDOMTreeNode`` for ``data-ff-id`` from the agent selector map (CDP-backed)."""
    if not str(ff_id).strip():
        return None
    try:
        selector_map = await browser_session.get_selector_map()
    except Exception:
        return None
    matches: list[Any] = []
    for node in selector_map.values():
        attrs = getattr(node, "attributes", None) or {}
        if attrs.get("data-ff-id") != ff_id:
            continue
        matches.append(node)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    def _score(n: Any) -> int:
        a = getattr(n, "attributes", None) or {}
        tag = (getattr(n, "tag_name", None) or "").lower()
        if tag == "select":
            return 4
        if a.get("role") == "combobox":
            return 3
        if tag == "input":
            return 2
        if a.get("aria-hidden") == "true":
            return -2
        return 0

    matches.sort(key=_score, reverse=True)
    return matches[0]


async def _fill_custom_dropdown_cdp_first(
    browser_session: BrowserSession,
    page: Any,
    field: FormField,
    value: str,
    tag: str,
) -> bool:
    """Greenhouse / react-select: same CDP discovery + open/poll as domhand_select, then page-level option click.

    Skips ``GetDropdownOptionsEvent`` so the default action watchdog does not log errors for
    ``input.select__input`` comboboxes during the initial domhand_fill pass.
    """
    from browser_use.browser.events import ClickElementEvent
    from ghosthands.actions.domhand_select import (
        _DISCOVER_OPTIONS_ON_NODE_JS,
        _SELECT_NATIVE_ON_NODE_JS,
        _call_function_on_node,
        _click_option_via_page_js,
        _fuzzy_match_option,
        _meaningful_dropdown_options,
        _needs_dropdown_open_trigger,
        _options_for_fuzzy_match,
        _try_click_combobox_toggle,
    )
    from ghosthands.dom.shadow_helpers import ensure_helpers

    with contextlib.suppress(Exception):
        await ensure_helpers(page)

    node = await _find_dom_tree_node_by_ff_id(browser_session, field.field_id)
    if node is None:
        logger.debug("domhand.fill.dropdown_cdp_no_node", field_id=field.field_id, tag=tag)
        return False

    is_native_select = getattr(node, "tag_name", None) == "select"

    try:
        discovery: Any = await _call_function_on_node(browser_session, node, _DISCOVER_OPTIONS_ON_NODE_JS)
    except Exception as exc:
        logger.debug(
            "domhand.fill.dropdown_cdp_discover_fail",
            field_id=field.field_id,
            tag=tag,
            error=str(exc)[:120],
        )
        return False

    if not isinstance(discovery, dict):
        return False

    dropdown_type = str(discovery.get("type") or "unknown")
    options: list[dict[str, Any]] = list(discovery.get("options") or [])

    if _needs_dropdown_open_trigger(is_native_select, dropdown_type, options):
        for _click_attempt in range(3):
            try:
                toggled = False
                if not is_native_select:
                    toggled = await _try_click_combobox_toggle(browser_session, node)
                if not toggled:
                    event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
                    await event
                    await event.event_result(raise_if_any=True, raise_if_none=False)
                for _tick in range(10):
                    await asyncio.sleep(0.15)
                    try:
                        discovery = await _call_function_on_node(
                            browser_session,
                            node,
                            _DISCOVER_OPTIONS_ON_NODE_JS,
                        )
                        if isinstance(discovery, dict):
                            dropdown_type = str(discovery.get("type") or "unknown")
                            options = list(discovery.get("options") or [])
                    except Exception:
                        pass
                    if is_native_select and options:
                        break
                    if _meaningful_dropdown_options(options):
                        break
                if is_native_select and options:
                    break
                if _meaningful_dropdown_options(options):
                    break
            except Exception:
                break

    match_options = _options_for_fuzzy_match(is_native_select, options)
    matched = _fuzzy_match_option(value, match_options)
    if not matched and len(split_dropdown_value_hierarchy(value)) > 1:
        for segment in split_dropdown_value_hierarchy(value):
            matched = _fuzzy_match_option(segment, match_options)
            if matched:
                break

    if not matched:
        logger.debug(
            "domhand.fill.dropdown_cdp_no_fuzzy_match",
            field_id=field.field_id,
            tag=tag,
            option_sample=[str(o.get("text") or "")[:40] for o in match_options[:5]],
        )
        return False

    matched_text = str(matched.get("text") or value).strip() or value

    try:
        if dropdown_type == "native_select" or is_native_select:
            raw = await _call_function_on_node(
                browser_session,
                node,
                _SELECT_NATIVE_ON_NODE_JS,
                arguments=[{"value": matched_text}],
            )
            result = raw if isinstance(raw, dict) else {"success": False}
        else:
            result = await _click_option_via_page_js(page, matched_text, dropdown_type)
    except Exception as exc:
        logger.debug(
            "domhand.fill.dropdown_cdp_click_fail",
            field_id=field.field_id,
            tag=tag,
            error=str(exc)[:120],
        )
        return False

    if not (isinstance(result, dict) and result.get("success")):
        return False

    current = await _wait_for_field_value(page, field, value, timeout=2.85)
    if not _field_value_matches_expected(current, value):
        return False

    await _settle_dropdown_selection(page)
    logger.info(
        "domhand.fill.dropdown_cdp_first_ok",
        field_id=field.field_id,
        tag=tag,
        matched_text=matched_text[:80],
    )
    return True


async def _fill_select_field(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
    *,
    browser_session: BrowserSession | None = None,
) -> bool:
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
    return await _fill_custom_dropdown(
        page,
        field,
        value,
        tag,
        browser_session=browser_session,
    )


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
            try:
                await page.evaluate(
                    r"""(ffId) => {
					var el = window.__ff ? window.__ff.byId(ffId) : null;
					if (el) {
						el.dispatchEvent(new Event('input', {bubbles: true}));
						el.dispatchEvent(new Event('keyup', {bubbles: true}));
					}
					return 'ok';
				}""",
                    ff_id,
                )
            except Exception:
                pass
            clicked = await _poll_click_dropdown_option(page, val, max_wait_s=2.4)
            if clicked.get("clicked"):
                picked_count += 1
                await asyncio.sleep(0.2)
                continue
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
    """Click a visible dropdown option by text using the enhanced 5-pass matcher."""
    try:
        raw_result = await page.evaluate(_CLICK_DROPDOWN_OPTION_JS, text, synonym_groups_for_js())
    except Exception:
        return {"clicked": False}
    return _parse_dropdown_click_result(raw_result)


async def _poll_click_dropdown_option(
    page: Any,
    *match_texts: str,
    max_wait_s: float = 2.75,
    interval_s: float = 0.12,
) -> dict[str, Any]:
    """Poll for visible ``role=option`` rows after typing — async lists (e.g. Greenhouse country).

    Append-only: does not change matching rules in ``_CLICK_DROPDOWN_OPTION_JS``; only retries
    until options appear or ``max_wait_s`` elapses.
    """
    texts = [m for m in match_texts if m and str(m).strip()]
    if not texts:
        return {"clicked": False}
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        for mt in texts:
            result = await _click_dropdown_option(page, mt)
            if result.get("clicked"):
                return result
        await asyncio.sleep(interval_s)
    return {"clicked": False}


async def _try_open_combobox_menu(page: Any, ff_id: str, *, tag: str) -> None:
    """Open react-select / combobox: prefer chevron / Toggle flyout; fall back to input click."""
    try:
        raw = await page.evaluate(CLICK_COMBOBOX_TOGGLE_BY_FFID_JS, ff_id)
        if combobox_toggle_clicked(raw):
            logger.debug("domhand.combobox_open", field_id=ff_id, tag=tag, via="toggle")
            return
    except Exception:
        pass
    try:
        await page.evaluate(CLICK_INPUT_BY_FFID_JS, ff_id)
        logger.debug("domhand.combobox_open", field_id=ff_id, tag=tag, via="input_click")
    except Exception:
        pass


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
            clicked = await _poll_click_dropdown_option(page, value, term, max_wait_s=2.85)
            if clicked.get("clicked"):
                logger.debug(f'select {tag} -> "{clicked.get("text", value)}" (typed search)')
                return clicked
            await page.keyboard.press("Enter")
            await asyncio.sleep(0.35)
            clicked = await _poll_click_dropdown_option(page, value, term, max_wait_s=1.2)
            if clicked.get("clicked"):
                logger.debug(f'select {tag} -> "{clicked.get("text", value)}" (typed search, enter)')
                return clicked
        except Exception as e:
            logger.debug(f'dropdown search {tag} term "{term}" failed: {str(e)[:60]}')
    return {"clicked": False}


async def _scan_visible_dropdown_options(page: Any) -> list[str]:
    """Read all visible dropdown option labels from the open menu."""
    try:
        raw = await page.evaluate(SCAN_VISIBLE_OPTIONS_JS)
        if isinstance(raw, str):
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


async def _fill_custom_dropdown(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
    *,
    browser_session: BrowserSession | None = None,
) -> bool:
    if browser_session is not None:
        try:
            if await _fill_custom_dropdown_cdp_first(browser_session, page, field, value, tag):
                return True
        except Exception as exc:
            logger.debug(
                "domhand.fill.dropdown_cdp_first_exception",
                field_id=field.field_id,
                tag=tag,
                error=str(exc)[:120],
            )
    ff_id = field.field_id
    pre_value = await _read_field_value_for_field(page, field)
    visible_before = await _visible_field_id_snapshot(page)

    async def _open() -> None:
        await _try_open_combobox_menu(page, ff_id, tag=tag)

    async def _read() -> str:
        return await _read_field_value_for_field(page, field)

    async def _type(text: str) -> None:
        await page.keyboard.type(text, delay=45)

    async def _clear() -> None:
        await _clear_dropdown_search(page)

    async def _settle() -> None:
        await _settle_dropdown_selection(page)

    async def _dismiss() -> None:
        try:
            await page.evaluate(_DISMISS_DROPDOWN_JS)
        except Exception:
            pass

    result = await fill_interactive_dropdown(
        page,
        value,
        open_fn=_open,
        read_value_fn=_read,
        settle_fn=_settle,
        dismiss_fn=_dismiss,
        type_fn=_type,
        clear_fn=_clear,
        tag=f"custom-select {tag}",
    )

    post_value = result.committed_value or await _read_field_value_for_field(page, field)
    visible_after = await _visible_field_id_snapshot(page)
    follow_up_appeared = bool(visible_after - visible_before - {field.field_id})
    logger.debug(
        "domhand.custom_widget_attempt",
        extra={
            "field_id": field.field_id,
            "field_label": _preferred_field_label(field),
            "desired_value": value,
            "pre_value": pre_value,
            "post_value": post_value,
            "open_succeeded": True,
            "selection_stuck": result.success,
            "follow_up_appeared": follow_up_appeared,
            "widget_kind": _field_widget_kind_for_debug(field),
            "pass_name": result.pass_name,
        },
    )
    return result.success


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


