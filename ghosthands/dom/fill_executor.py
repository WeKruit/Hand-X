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
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog

from browser_use.browser import BrowserSession
from ghosthands.actions.combobox_toggle import (
    CLICK_COMBOBOX_TOGGLE_BY_FFID_JS,
    CLICK_INPUT_BY_FFID_JS,
    combobox_toggle_clicked,
    trusted_open_combobox_by_ffid,
)
from ghosthands.actions.views import (
    FormField,
    generate_dropdown_search_terms,
    normalize_name,
    split_dropdown_value_hierarchy,
)
from ghosthands.dom.dropdown_fill import (
    POST_OPTION_CLICK_SETTLE_S,
    fill_interactive_dropdown,
)
from ghosthands.dom.dropdown_match import (
    SCAN_VISIBLE_OPTIONS_JS,
    synonym_groups_for_js,
)
from ghosthands.dom.dropdown_verify import selection_matches_desired
from ghosthands.dom.fill_browser_scripts import (
    _CLICK_BINARY_FIELD_JS,
    _CLICK_BUTTON_GROUP_JS,
    _CLICK_CHECKBOX_GROUP_JS,
    _CLICK_DROPDOWN_OPTION_JS,
    _CLICK_OTHER_TEXTLIKE_FIELD_JS,
    _CLICK_RADIO_OPTION_JS,
    _CLICK_SINGLE_RADIO_JS,
    _DISMISS_DROPDOWN_SOFT_JS,
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
    _SCROLL_FF_INTO_VIEW_JS,
    _SELECT_GROUPED_DATE_PICKER_VALUE_JS,
)
from ghosthands.dom.oracle_combobox_llm import (
    oracle_combobox_pick_option_llm,
    oracle_combobox_search_terms_llm,
    oracle_combobox_verify_commit_llm,
)
from ghosthands.runtime_learning import (
    detect_host_from_url,
    detect_platform_from_url,
    get_interaction_recipe,
    record_interaction_recipe,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class FieldFillOutcome:
    """Detailed outcome for a single-field fill attempt."""

    success: bool
    matched_label: str | None = None

    def __bool__(self) -> bool:
        return self.success


def _fill_outcome(success: bool, *, matched_label: str | None = None) -> FieldFillOutcome:
    return FieldFillOutcome(success=success, matched_label=matched_label)


def _verbose_dropdown_logs() -> bool:
    return (os.environ.get("GH_VERBOSE_DROPDOWN") or "").strip().lower() in ("1", "true", "yes", "on")


def _log_dropdown_diag(event: str, **kwargs: Any) -> None:
    if _verbose_dropdown_logs():
        logger.info(event, **kwargs)
    else:
        logger.debug(event, **kwargs)


# react-select / portal menus: listbox often mounts after the open click; scanning or
# clicking the option in the same tick closes the menu with nothing selected.
_DROPDOWN_MENU_OPEN_SETTLE_S = 0.55
_DROPDOWN_CDP_POLL_TICK_S = 0.22
_DROPDOWN_CDP_POLL_MAX_TICKS = 14
_WORKDAY_SKILL_RESULT_SETTLE_S = 3.0
_WORKDAY_SKILL_POST_ENTER_SETTLE_S = 3.0
_WORKDAY_REFERRAL_RESULT_SETTLE_S = 2.3
_WORKDAY_REFERRAL_POST_ENTER_SETTLE_S = 2.3

_TAG_SKILL_INPUT_JS = r"""(ffId) => {
    var old = document.querySelector('[data-dh-skill-input]');
    if (old) old.removeAttribute('data-dh-skill-input');
    var ff = window.__ff;
    var el = ff ? ff.byId(ffId) : null;
    if (!el) return false;
    var inputs = el.querySelectorAll('input[type="text"], input:not([type])');
    for (var i = 0; i < inputs.length; i++) {
        var inp = inputs[i];
        if (inp.offsetWidth > 0 && inp.offsetHeight > 0) {
            inp.setAttribute('data-dh-skill-input', 'true');
            return true;
        }
    }
    return false;
}"""


async def _clear_skill_input(page: Any, ff_id: str) -> None:
    """Clear a Workday skill search input by pressing Backspace repeatedly.

    Workday React inputs ignore JS value setters, Ctrl+A, and Playwright fill('').
    Brute-force Backspace is the only reliable method.
    """
    with contextlib.suppress(Exception):
        await page.evaluate(_FOCUS_FIELD_JS, ff_id)
    await asyncio.sleep(0.05)
    # Press End to ensure cursor is at the end, then Backspace 40 times
    with contextlib.suppress(Exception):
        await _press_key_compat(page, "End")
    for _ in range(40):
        with contextlib.suppress(Exception):
            await _press_key_compat(page, "Backspace")
    await asyncio.sleep(0.1)

_WORKDAY_SKILL_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    "react": ("ReactJS", "React.js"),
    "reactjs": ("React", "React.js"),
    "express": ("ExpressJS", "Express.js"),
    "expressjs": ("Express", "Express.js"),
    "nodejs": ("Node.js", "Node JS"),
    "mysql": ("MySQLDB", "My SQL"),
    "mysqldb": ("MySQL", "My SQL"),
    "tailwindcss": ("TailwindCSS",),
}


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


def _skill_signal_text(field: FormField) -> str:
    """Return the same skill-routing signal used by the stable main path.

    Skills/widgets should only route to ``_fill_multi_select`` when the field's
    primary label itself is skill-like. Broader placeholder/section heuristics
    caused unrelated searchable inputs and dropdowns to get mis-routed.
    """
    return str(field.name or "").strip()


async def _uses_workday_skill_multiselect(page: Any, field: FormField) -> bool:
    """Detect whether this field is the Workday prompt-search skill widget.

    Oracle searchable comboboxes can use labels like "Skill" or "Skill Name",
    but they are still single-select inputs. Only the real Workday skill widget
    should use the multi-select contract.
    """
    if field.field_type not in {"text", "search", "select"}:
        return False
    if not _is_skill_like(_skill_signal_text(field)):
        return False
    widget = await _read_workday_skill_widget(page, field.field_id)
    return bool(widget.get("is_workday_skill"))


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


def _compact_workday_skill_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _workday_skill_query_candidates(skill: str) -> list[str]:
    base = re.sub(r"\s+", " ", str(skill or "").strip())
    if not base:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        text = re.sub(r"\s+", " ", str(candidate or "").strip(" ,"))
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        candidates.append(text)

    add(base)
    compact = _compact_workday_skill_key(base)
    for alias in _WORKDAY_SKILL_ALIAS_MAP.get(compact, ()):
        add(alias)

    if compact.endswith("db"):
        add(re.sub(r"(?i)\s*db$", "", base).strip(" -_/"))
    if compact.endswith("database"):
        add(re.sub(r"(?i)\s*database$", "", base).strip(" -_/"))
    if compact.endswith("js") and compact not in {"javascript", "typescript", "nodejs"}:
        add(re.sub(r"(?i)\s*\.?js$", "", base).strip(" -_/"))
    if compact in {"react", "express"}:
        add(base + "JS")
        add(base + ".js")
    if compact == "nodejs":
        add("Node.js")
        add("Node JS")
    if compact == "tailwindcss":
        add("Tailwind CSS")

    return candidates


def _workday_skill_commit_matches(
    committed: dict[str, Any],
    desired_skill: str,
    *,
    matched_label: str | None = None,
) -> bool:
    if not committed.get("committed"):
        return False
    for token in committed.get("tokens", []):
        if _field_value_matches_expected(str(token).strip(), desired_skill, matched_label=matched_label):
            return True
    return False


def _find_exact_workday_skill_option(query: str, options: list[str]) -> str | None:
    needle = str(query or "").strip().casefold()
    if not needle:
        return None
    for option in options:
        text = str(option or "").strip()
        if text and text.casefold() == needle:
            return text
    return None


def _is_workday_url(url: str) -> bool:
    lowered = str(url or "").strip().lower()
    return any(
        token in lowered
        for token in (
            "myworkdayjobs.com",
            "myworkday.com",
            "myworkdaysite.com",
            "workday.com",
        )
    )


def _is_workday_prompt_search_field(field: FormField) -> bool:
    """Detect Workday prompt-search single-select widgets (School, Field of Study, etc.).

    These require Enter after typing to trigger the search — unlike regular dropdowns.
    """
    if field.field_type not in ("select",):
        return False
    label = normalize_name(_preferred_field_label(field))
    return any(
        needle in label
        for needle in (
            "school or university",
            "school",
            "university",
            "field of study",
            "college",
        )
    )


def _is_workday_referral_source_field(field: FormField) -> bool:
    label = normalize_name(_preferred_field_label(field))
    return any(
        needle in label
        for needle in (
            "how did you hear",
            "where did you hear",
            "learn about us",
            "referral source",
            "source of referral",
            "source of application",
            "application source",
            "hear about us",
        )
    )


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
    *,
    matched_label: str | None = None,
) -> str:
    """Wait briefly for a field's visible value to reflect the intended selection.

    ``matched_label`` is the option text we actually clicked (from fuzzy match); pass it so
    verification accepts UI text that differs from the profile string (e.g. USA vs United States).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_value = ""
    while True:
        current = await _read_field_value_for_field(page, field)
        if current:
            last_value = current
        if _field_value_matches_expected(current, expected, matched_label=matched_label):
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
    if field.field_type == "select" and (field.is_multi_select or await _uses_workday_skill_multiselect(page, field)):
        selection = await _read_multi_select_selection(page, field.field_id)
        tokens = [str(token).strip() for token in selection.get("tokens", []) if str(token).strip()]
        if tokens:
            return ", ".join(tokens)
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
            await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
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
            await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
            await asyncio.sleep(0.15)
        else:
            await locator.press("Tab")
            await asyncio.sleep(0.1)
    except Exception:
        try:
            await page.evaluate(_FOCUS_FIELD_JS, field.field_id)
            await asyncio.sleep(0.05)
            if needs_enter_commit:
                await _press_key_compat(page, "Enter")
                await asyncio.sleep(0.15)
            if needs_blur_revalidation:
                await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
                await asyncio.sleep(0.15)
            else:
                await _press_key_compat(page, "Tab")
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


async def _poll_group_selection(page: Any, field_id: str, expected: str, max_wait: float = 1.0) -> str:
    """Poll group selection for up to *max_wait* seconds after a click.

    Oracle Cloud HCM (JET/VDSA) can take 400-800ms to update aria-pressed
    after a trusted click.  A single-shot 250ms read misses this.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_wait
    interval = 0.2
    current = ""
    while True:
        try:
            current = await _read_group_selection(page, field_id)
        except Exception:
            current = ""
        if _field_value_matches_expected(current, expected):
            return current
        if loop.time() >= deadline:
            return current
        await asyncio.sleep(interval)


async def _click_group_option_with_gui(page: Any, field: FormField, value: str, tag: str) -> bool:
    """Use a real mouse click on the visible option when DOM clicks do not stick."""
    target = await _get_group_option_target(page, field.field_id, value)
    if not target.get("found"):
        return False
    try:
        mouse = page.mouse
        if hasattr(mouse, "__await__"):
            mouse = await mouse
        await mouse.move(int(target["x"]), int(target["y"]))
        await asyncio.sleep(0.05)
        await mouse.click(int(target["x"]), int(target["y"]))
        current = await _poll_group_selection(page, field.field_id, value, max_wait=1.0)
        if _field_value_matches_expected(current, value):
            logger.debug(f'gui-select {tag} -> "{target.get("text", value)}"')
            return True
    except Exception as exc:
        logger.debug(f"gui mouse click {tag} failed: {str(exc)[:60]}")
    option_ff_id = str(target.get("optionFfId") or "").strip()
    if option_ff_id:
        try:
            exact_elements = await page.get_elements_by_css_selector(f'[data-ff-id="{option_ff_id}"]')
            if exact_elements:
                await exact_elements[0].click()
                current = await _poll_group_selection(page, field.field_id, value, max_wait=0.8)
                if _field_value_matches_expected(current, value):
                    logger.debug(f'gui-select {tag} -> "{target.get("text", value)}"')
                    return True
        except Exception as exc:
            logger.debug(f"gui exact element click {tag} failed: {str(exc)[:60]}")
        try:
            locator = getattr(page, "locator", None)
            if callable(locator):
                await locator(f'[data-ff-id="{option_ff_id}"]').first.click(timeout=1200)
                current = await _poll_group_selection(page, field.field_id, value, max_wait=0.8)
                if _field_value_matches_expected(current, value):
                    logger.debug(f'gui-select {tag} -> "{target.get("text", value)}"')
                    return True
        except Exception as exc:
            logger.debug(f"gui locator click {tag} failed: {str(exc)[:60]}")
    try:
        current = await _read_group_selection(page, field.field_id)
        if _field_value_matches_expected(current, value):
            logger.debug(f'gui-select {tag} -> "{target.get("text", value)}"')
            return True
    except Exception:
        pass
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


async def _dispatch_platform_fill_outcome(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
    strategy: str,
    *,
    browser_session: BrowserSession | None = None,
) -> FieldFillOutcome | None:
    """Try a platform-specific fill strategy.

    Returns an outcome if the strategy handled the field,
    or ``None`` to fall through to default dispatch.
    """
    match strategy:
        case "combobox_toggle":
            return await _fill_custom_dropdown_outcome(page, field, value, tag, browser_session=browser_session)
        case "react_select":
            # Same path as non-native <select>: CDP open → discover options → click match,
            # then fill_interactive_dropdown with real keyboard typing. The old
            # _fill_searchable_dropdown path used JS fill on the input and skipped CDP-first,
            # which breaks Greenhouse react-select (typing fragments like "answer").
            return await _fill_custom_dropdown_outcome(page, field, value, tag, browser_session=browser_session)
        case "segmented_date":
            return _fill_outcome(await _fill_grouped_date_field(page, field, value, tag))
        case "searchable_dropdown":
            return await _fill_searchable_dropdown_outcome(page, field, value, tag)
        case "oracle_combobox":
            return await _fill_oracle_combobox_outcome(page, field, value, tag)
        case "playwright_fill":
            return _fill_outcome(await _fill_text_field(page, field, value, tag))
        case _:
            logger.warning(
                "domhand.unknown_platform_strategy",
                strategy=strategy,
                field_type=field.field_type,
            )
            return None


async def _dispatch_platform_fill(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
    strategy: str,
    *,
    browser_session: BrowserSession | None = None,
) -> bool | None:
    outcome = await _dispatch_platform_fill_outcome(
        page,
        field,
        value,
        tag,
        strategy,
        browser_session=browser_session,
    )
    return None if outcome is None else outcome.success


async def _fill_single_field_outcome(
    page: Any,
    field: FormField,
    value: str,
    *,
    browser_session: BrowserSession | None = None,
) -> FieldFillOutcome:
    ff_id = field.field_id
    tag = f"[{field.name or field.field_type}]"

    try:
        exists_json = await page.evaluate(_ELEMENT_EXISTS_JS, ff_id, field.field_type)
        if not json.loads(exists_json):
            logger.debug(f"skip {tag} (not visible)")
            return _fill_outcome(False)
    except Exception:
        pass

    page_url = ""
    try:
        page_url = str(await page.evaluate("() => location.href") or "")
    except Exception:
        pass

    fill_strategy: str | None = None
    try:
        from ghosthands.platforms import get_fill_overrides

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
        # Workday skill widgets are technically select-like, but they must keep the
        # stable per-skill multi-select contract instead of the generic platform
        # combobox override.
        if field.field_type == "select" and await _uses_workday_skill_multiselect(page, field):
            fill_strategy = None

    if fill_strategy:
        result = await _dispatch_platform_fill_outcome(
            page,
            field,
            value,
            tag,
            fill_strategy,
            browser_session=browser_session,
        )
        if result is not None:
            return result

    # School-only: bypass generic Oracle combobox path and use LLM search+pick.
    if field.oracle_freeform_combobox_answer and _is_oracle_school_llm_field(field):
        outcome = await _fill_oracle_school_combobox_llm_outcome(page, field, value, tag)
        if outcome.success:
            return outcome
        # School LLM path failed — block fuzzy fallback (entity guard)
        logger.info(
            "domhand.oracle_school_llm_direct_failed",
            field_label=field.name,
            value=value[:60],
        )
        return outcome  # return failed outcome, no fallthrough

    oracle_first = await _try_oracle_searchable_combobox_first(
        page, field, value, tag, page_url=page_url
    )
    if oracle_first is not None:
        return oracle_first

    match field.field_type:
        case "text" | "email" | "tel" | "url" | "number" | "password" | "search":
            return _fill_outcome(await _fill_text_field(page, field, value, tag))
        case "date":
            return _fill_outcome(await _fill_date_field(page, field, value, tag))
        case "textarea":
            return _fill_outcome(await _fill_textarea_field(page, field, value, tag))
        case "select":
            return await _fill_select_field_outcome(page, field, value, tag, browser_session=browser_session)
        case "radio-group":
            return _fill_outcome(await _fill_radio_group(page, field, value, tag))
        case "radio":
            return _fill_outcome(await _fill_single_radio(page, field, value, tag))
        case "button-group":
            return _fill_outcome(await _fill_button_group(page, field, value, tag))
        case "checkbox-group":
            return _fill_outcome(await _fill_checkbox_group(page, field, value, tag))
        case "checkbox":
            return _fill_outcome(await _fill_checkbox(page, field, value, tag))
        case "toggle":
            return _fill_outcome(await _fill_toggle(page, field, value, tag))
        case _:
            return _fill_outcome(await _fill_text_field(page, field, value, tag))


async def _fill_single_field(
    page: Any,
    field: FormField,
    value: str,
    *,
    browser_session: BrowserSession | None = None,
) -> bool:
    return (await _fill_single_field_outcome(page, field, value, browser_session=browser_session)).success


async def _fill_text_field(page: Any, field: FormField, value: str, tag: str) -> bool:
    ff_id = field.field_id
    if await _uses_workday_skill_multiselect(page, field):
        values = [part.strip() for part in str(value or "").split(",") if part.strip()]
        if values:
            return await _fill_multi_select(page, field, values, tag)
    try:
        is_search_json = await page.evaluate(_IS_SEARCHABLE_DROPDOWN_JS, ff_id)
        is_searchable_dropdown = bool(json.loads(is_search_json))
        if is_searchable_dropdown:
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


async def _fill_searchable_dropdown_outcome(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
) -> FieldFillOutcome:
    ff_id = field.field_id
    if not value:
        logger.debug(f"skip {tag} (searchable dropdown, no answer)")
        return _fill_outcome(False)

    async def _open() -> None:
        await _try_open_combobox_menu(page, ff_id, tag=tag)

    async def _read() -> str:
        return await _read_field_value_for_field(page, field)

    async def _scan() -> list[str]:
        return await _scan_visible_dropdown_options(page, field_id=ff_id)

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
        await _clear_dropdown_search(page, ff_id)

    async def _settle() -> None:
        await _settle_dropdown_selection(page)

    async def _dismiss() -> None:
        try:
            await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
        except Exception:
            pass

    async def _click_option(text: str) -> dict[str, Any]:
        return await _click_dropdown_option(page, text, field_id=ff_id)

    result = await fill_interactive_dropdown(
        page,
        value,
        open_fn=_open,
        read_value_fn=_read,
        scan_options_fn=_scan,
        settle_fn=_settle,
        dismiss_fn=_dismiss,
        type_fn=_type,
        clear_fn=_clear,
        click_option_fn=_click_option,
        tag=f"search-select {tag}",
    )
    if result.success and await _field_has_validation_error(page, field.field_id):
        return _fill_outcome(False)
    return _fill_outcome(result.success, matched_label=result.matched_label)


async def _fill_searchable_dropdown(page: Any, field: FormField, value: str, tag: str) -> bool:
    return (await _fill_searchable_dropdown_outcome(page, field, value, tag)).success


# ---------------------------------------------------------------------------
# Oracle Cloud HCM combobox strategy
# ---------------------------------------------------------------------------
# Oracle's cx-select combobox requires real keyboard events — JS value injection
# is silently rejected by the framework. This strategy:
#   1. Focuses the input, clears it
#   2. Types with press_sequentially (real key events, 30ms delay)
#   3. Waits 1.0s for the suggestion dropdown to populate
#   4. Clicks the best matching option from the dropdown
#   5. Waits 1.2s for post-fill stability (Oracle can async-reject values)
#   6. Re-reads the committed value — if cleared, reports failure

_IS_ORACLE_SEARCHABLE_JS = r"""
(ffId) => {
    var el = window.__ff ? window.__ff.byId(ffId) : null;
    if (!el) return false;
    if (el.getAttribute('aria-autocomplete') === 'list' ||
        el.getAttribute('aria-autocomplete') === 'both') return true;
    if (el.getAttribute('role') === 'combobox') return true;
    if (el.getAttribute('aria-haspopup') === 'listbox' &&
        el.tagName === 'INPUT') return true;
    if (el.getAttribute('aria-haspopup') === 'grid' &&
        el.tagName === 'INPUT') return true;
    var container = el.closest('.cx-select-container');
    if (container) return true;
    return false;
}
"""


_ORACLE_COMBOBOX_CLICK_BEST_OPTION_JS = r"""
(ffId, desired) => {
    var el = window.__ff ? window.__ff.byId(ffId) : null;
    if (!el) return JSON.stringify({clicked: false, reason: 'element_not_found'});
    var controlsId = el.getAttribute('aria-controls');
    var dropdown = controlsId ? document.getElementById(controlsId) : null;
    if (!dropdown) {
        var container = el.closest('.cx-select-container') || el.closest('.input-field-container');
        if (container) dropdown = container.querySelector('.cx-select-dropdown, [role="grid"], [role="listbox"]');
    }
    if (!dropdown) {
        var allListboxes = document.querySelectorAll('[role="listbox"], [role="grid"]');
        for (var i = 0; i < allListboxes.length; i++) {
            var s = getComputedStyle(allListboxes[i]);
            if (s.display !== 'none' && s.visibility !== 'hidden') { dropdown = allListboxes[i]; break; }
        }
    }
    if (!dropdown) return JSON.stringify({clicked: false, reason: 'no_dropdown'});
    var rows = dropdown.querySelectorAll('[role="row"]:not([data-empty-row="true"]), [role="option"]');
    var visible = [];
    for (var j = 0; j < rows.length; j++) {
        var rs = getComputedStyle(rows[j]);
        if (rs.display !== 'none' && rs.visibility !== 'hidden') visible.push(rows[j]);
    }
    if (!visible.length) return JSON.stringify({clicked: false, reason: 'no_visible_options'});
    var dl = desired.toLowerCase().trim();
    var best = null; var matchedText = '';
    for (var k = 0; k < visible.length; k++) {
        var rv = (visible[k].getAttribute('data-value') || '').toLowerCase().trim();
        var rt = (visible[k].textContent || '').toLowerCase().trim();
        if (rv === dl || rt === dl) { best = visible[k]; matchedText = visible[k].getAttribute('data-value') || visible[k].textContent; break; }
    }
    if (!best) {
        for (var m = 0; m < visible.length; m++) {
            var combined = ((visible[m].getAttribute('data-value') || '') + ' ' + (visible[m].textContent || '')).toLowerCase();
            if (combined.indexOf(dl) !== -1) { best = visible[m]; matchedText = visible[m].getAttribute('data-value') || visible[m].textContent; break; }
        }
    }
    if (!best) return JSON.stringify({clicked: false, reason: 'no_match', visible_count: visible.length});
    best.click(); return JSON.stringify({clicked: true, text: (matchedText || '').substring(0, 120)});
    return JSON.stringify({clicked: false, reason: 'no_match'});
}
"""

_ORACLE_COMBOBOX_READ_VALUE_JS = r"""
(ffId) => {
    var el = window.__ff ? window.__ff.byId(ffId) : null;
    if (!el) return JSON.stringify({value: '', committed: ''});
    return JSON.stringify({value: el.value || '', committed: el.dataset ? (el.dataset.committedValue || '') : ''});
}
"""

_ORACLE_COMBOBOX_LIST_OPTIONS_JS = r"""
(ffId) => {
    var el = window.__ff ? window.__ff.byId(ffId) : null;
    if (!el) return JSON.stringify([]);
    var controlsId = el.getAttribute('aria-controls');
    var dropdown = controlsId ? document.getElementById(controlsId) : null;
    if (!dropdown) {
        var container = el.closest('.cx-select-container') || el.closest('.input-field-container');
        if (container) dropdown = container.querySelector('.cx-select-dropdown, [role="grid"], [role="listbox"]');
    }
    if (!dropdown) {
        var allListboxes = document.querySelectorAll('[role="listbox"], [role="grid"]');
        for (var i = 0; i < allListboxes.length; i++) {
            var s = getComputedStyle(allListboxes[i]);
            if (s.display !== 'none' && s.visibility !== 'hidden') { dropdown = allListboxes[i]; break; }
        }
    }
    if (!dropdown) return JSON.stringify([]);
    var rows = dropdown.querySelectorAll('[role="row"]:not([data-empty-row="true"]), [role="option"]');
    var visible = [];
    for (var j = 0; j < rows.length; j++) {
        var rs = getComputedStyle(rows[j]);
        if (rs.display !== 'none' && rs.visibility !== 'hidden') visible.push(rows[j]);
    }
    var out = [];
    var max = 30;
    for (var k = 0; k < visible.length && k < max; k++) {
        var row = visible[k];
        var t = (row.textContent || '').replace(/\s+/g, ' ').trim();
        var dv = (row.getAttribute('data-value') || '').trim();
        out.push({text: t.substring(0, 240), dataValue: dv.substring(0, 240)});
    }
    return JSON.stringify(out);
}
"""

_ORACLE_COMBOBOX_CLICK_INDEX_JS = r"""
(ffId, index) => {
    var el = window.__ff ? window.__ff.byId(ffId) : null;
    if (!el) return JSON.stringify({clicked: false, reason: 'element_not_found'});
    var controlsId = el.getAttribute('aria-controls');
    var dropdown = controlsId ? document.getElementById(controlsId) : null;
    if (!dropdown) {
        var container = el.closest('.cx-select-container') || el.closest('.input-field-container');
        if (container) dropdown = container.querySelector('.cx-select-dropdown, [role="grid"], [role="listbox"]');
    }
    if (!dropdown) {
        var allListboxes = document.querySelectorAll('[role="listbox"], [role="grid"]');
        for (var i = 0; i < allListboxes.length; i++) {
            var s = getComputedStyle(allListboxes[i]);
            if (s.display !== 'none' && s.visibility !== 'hidden') { dropdown = allListboxes[i]; break; }
        }
    }
    if (!dropdown) return JSON.stringify({clicked: false, reason: 'no_dropdown'});
    var rows = dropdown.querySelectorAll('[role="row"]:not([data-empty-row="true"]), [role="option"]');
    var visible = [];
    for (var j = 0; j < rows.length; j++) {
        var rs = getComputedStyle(rows[j]);
        if (rs.display !== 'none' && rs.visibility !== 'hidden') visible.push(rows[j]);
    }
    var idx = typeof index === 'number' ? index : parseInt(String(index), 10);
    if (isNaN(idx) || idx < 0 || idx >= visible.length) {
        return JSON.stringify({clicked: false, reason: 'bad_index', visible_count: visible.length});
    }
    var row = visible[idx];
    var matchedText = (row.getAttribute('data-value') || row.textContent || '').replace(/\s+/g, ' ').trim();
    // Oracle cx-select ignores simple el.click() — dispatch full mouse event sequence
    // on the deepest text-bearing child (Oracle binds handlers on inner spans/cells).
    var target = row.querySelector('[role="gridcell"] span, [role="gridcell"], span, a') || row;
    var rect = target.getBoundingClientRect();
    var cx = rect.left + rect.width / 2;
    var cy = rect.top + rect.height / 2;
    var opts = {bubbles: true, cancelable: true, view: window, clientX: cx, clientY: cy};
    target.dispatchEvent(new MouseEvent('mousedown', opts));
    target.dispatchEvent(new MouseEvent('mouseup', opts));
    target.dispatchEvent(new MouseEvent('click', opts));
    return JSON.stringify({clicked: true, text: matchedText.substring(0, 200)});
}
"""


def _is_oracle_school_llm_field(field: FormField) -> bool:
    """Oracle education combobox: use GPT type → scan → LLM index pick (not JS substring).

    Triage may set ``oracle_freeform_combobox_answer`` for both **school** and **field_of_study**
    on Oracle FA to skip option-list coercion. Only **school-like** labels enter this LLM picker;
    major / field-of-study / discipline labels stay on the generic Oracle combobox path
    (``_fill_oracle_combobox_outcome`` with keyboard + JS row match).
    """
    if not field.oracle_freeform_combobox_answer:
        return False
    label = normalize_name(_preferred_field_label(field))
    if any(
        tok in label
        for tok in (
            "major",
            "minor",
            "field of study",
            "discipline",
            "concentration",
            "area of study",
        )
    ):
        return False
    return any(tok in label for tok in ("school", "university", "college", "institution"))


def _is_oracle_entity_no_fuzzy_fallback_field(field: FormField) -> bool:
    """High-risk entity names where word-overlap fallback must NOT be used.

    School/college/university/institution and employer/company/organization labels
    share common tokens (e.g. "University") with many unrelated options.  If the
    Oracle combobox path fails, falling through to ``fill_interactive_dropdown`` →
    ``match_dropdown_option`` pass 5 (word overlap) would silently pick the wrong
    entity.  This guard does **not** require ``oracle_freeform_combobox_answer`` —
    triage can still miss the flag for employer fields or non-structured paths.
    """
    label = normalize_name(_preferred_field_label(field))
    # Exclude degree / major / discipline / visa — these have short, deterministic option lists.
    if any(
        tok in label
        for tok in (
            "major",
            "minor",
            "field of study",
            "discipline",
            "concentration",
            "area of study",
            "degree",
            "visa",
            "authorization",
            "sponsorship",
        )
    ):
        return False
    return any(
        tok in label
        for tok in (
            "school",
            "university",
            "college",
            "institution",
            "employer",
            "company",
            "organization",
            "latest employer",
        )
    )


def _oracle_combobox_options_raw_to_labels(raw: Any) -> list[str]:
    items = raw
    if isinstance(raw, str):
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(items, list):
        return []
    labels: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        t = str(item.get("text") or "").strip()
        dv = str(item.get("dataValue") or "").strip()
        label = t if t else dv
        if label:
            labels.append(label)
    return labels


async def _oracle_list_combobox_option_labels(page: Any, ff_id: str) -> list[str]:
    try:
        raw = await page.evaluate(_ORACLE_COMBOBOX_LIST_OPTIONS_JS, ff_id)
        return _oracle_combobox_options_raw_to_labels(raw)
    except Exception:
        return []


async def _poll_oracle_combobox_options(
    page: Any,
    ff_id: str,
    *,
    max_wait_s: float = 2.5,
    interval_s: float = 0.25,
) -> list[str]:
    """Poll for non-empty Oracle combobox option labels after typing.

    Shared by both the LLM school path and the generic Oracle combobox path.
    Returns the first non-empty label list, or ``[]`` on timeout.
    """
    elapsed = 0.0
    poll_count = 0
    await asyncio.sleep(0.5)  # initial settle for Oracle grid render
    elapsed += 0.5
    while elapsed < max_wait_s:
        poll_count += 1
        labels = await _oracle_list_combobox_option_labels(page, ff_id)
        if labels:
            logger.info(
                "domhand.oracle_poll_options_found",
                field_id=ff_id,
                poll_count=poll_count,
                elapsed_s=round(elapsed, 2),
                option_count=len(labels),
            )
            return labels
        await asyncio.sleep(interval_s)
        elapsed += interval_s
    logger.info(
        "domhand.oracle_poll_options_timeout",
        field_id=ff_id,
        poll_count=poll_count,
        elapsed_s=round(elapsed, 2),
    )
    return []


def _merge_unique_terms(primary: list[str], secondary: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in (primary, secondary):
        for t in group:
            s = str(t).strip()
            if not s:
                continue
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
    return out


async def _fill_oracle_school_combobox_llm_outcome(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
) -> FieldFillOutcome:
    """Oracle school: keyboard type → list visible rows → GPT picks index → verify commit."""
    ff_id = field.field_id
    canonical = " ".join(str(value or "").split()).strip()
    if not canonical:
        return _fill_outcome(False)

    from ghosthands.dom.oracle_combobox_llm import _oracle_school_llm_disabled
    logger.info(
        "domhand.oracle_school_llm_enter",
        field_label=field.name,
        canonical=canonical[:80],
        llm_disabled=_oracle_school_llm_disabled(),
    )

    max_term_generations = 2
    # Each successful candidate search uses up to 2 LLM calls (pick + verify). Budget 12
    # caps total pick+verify invocations (~6 full try/verify cycles) across all search terms.
    max_option_llm_calls = 12
    option_llm_calls = 0
    all_terms_tried: list[str] = []

    det_terms = _oracle_combobox_search_terms(canonical) or [canonical]

    for gen in range(max_term_generations):
        if gen == 0:
            llm_terms = await oracle_combobox_search_terms_llm(canonical)
            search_terms = _merge_unique_terms(llm_terms, det_terms)
        else:
            llm_terms = await oracle_combobox_search_terms_llm(
                canonical, prior_terms_tried=all_terms_tried
            )
            search_terms = _merge_unique_terms(llm_terms, [])

        logger.info(
            "domhand.oracle_school_llm_terms",
            field_label=field.name,
            generation=gen + 1,
            llm_term_count=len(llm_terms),
            det_term_count=len(det_terms) if gen == 0 else 0,
            merged_count=len(search_terms),
            terms_preview=[t[:40] for t in search_terms[:5]],
        )
        if not search_terms:
            continue

        for term in search_terms:
            if term.lower() in {t.lower() for t in all_terms_tried}:
                continue
            all_terms_tried.append(term)

            if option_llm_calls >= max_option_llm_calls:
                break

            logger.info(
                "domhand.oracle_school_llm_typing",
                field_label=field.name,
                search_term=term[:60],
                generation=gen + 1,
                terms_tried=len(all_terms_tried),
                llm_calls_used=option_llm_calls,
            )
            with contextlib.suppress(Exception):
                await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
            await asyncio.sleep(0.12)
            await page.evaluate(_FOCUS_AND_CLEAR_JS, ff_id)
            await asyncio.sleep(0.1)
            await _press_key_compat(page, "Control+a")
            await _press_key_compat(page, "Backspace")
            await asyncio.sleep(0.1)
            await _type_text_compat(page, term, delay=30)

            option_labels = await _poll_oracle_combobox_options(page, ff_id)
            if option_labels:
                logger.info(
                    "domhand.oracle_school_llm_options_found",
                    field_label=field.name,
                    search_term=term[:60],
                    option_count=len(option_labels),
                    first_options=[o[:50] for o in option_labels[:5]],
                )
            if not option_labels:
                logger.info(
                    "domhand.oracle_school_llm_no_options",
                    field_label=field.name,
                    search_term=term[:60],
                    generation=gen + 1,
                )
                continue

            if option_llm_calls + 2 > max_option_llm_calls:
                break

            option_llm_calls += 1
            if option_llm_calls >= max_option_llm_calls:
                break

            idx = await oracle_combobox_pick_option_llm(canonical, option_labels, term)
            logger.info(
                "domhand.oracle_school_llm_pick_result",
                field_label=field.name,
                search_term=term[:60],
                picked_index=idx,
                picked_label=(option_labels[idx][:60] if idx is not None and 0 <= idx < len(option_labels) else None),
                option_count=len(option_labels),
            )
            if idx is None:
                continue

            click_raw = await page.evaluate(_ORACLE_COMBOBOX_CLICK_INDEX_JS, ff_id, idx)
            click_result = json.loads(click_raw) if isinstance(click_raw, str) else click_raw
            if not (isinstance(click_result, dict) and click_result.get("clicked")):
                reason = str((click_result or {}).get("reason", ""))[:40]
                logger.info(
                    "domhand.oracle_school_llm_click_failed",
                    field_label=field.name,
                    search_term=term[:60],
                    picked_index=idx,
                    reason=reason,
                )
                # Dropdown may have dismissed during LLM call — retype to reopen and retry click once
                if reason in ("no_dropdown", "element_not_found"):
                    await page.evaluate(_FOCUS_AND_CLEAR_JS, ff_id)
                    await asyncio.sleep(0.1)
                    await _press_key_compat(page, "Control+a")
                    await _press_key_compat(page, "Backspace")
                    await asyncio.sleep(0.1)
                    await _type_text_compat(page, term, delay=30)
                    retry_options = await _poll_oracle_combobox_options(page, ff_id)
                    if retry_options:
                        click_raw2 = await page.evaluate(_ORACLE_COMBOBOX_CLICK_INDEX_JS, ff_id, idx)
                        click_result2 = json.loads(click_raw2) if isinstance(click_raw2, str) else click_raw2
                        if isinstance(click_result2, dict) and click_result2.get("clicked"):
                            logger.info(
                                "domhand.oracle_school_llm_click_retry_ok",
                                field_label=field.name,
                                search_term=term[:60],
                                picked_index=idx,
                            )
                            # Fall through to value read + verify below
                        else:
                            continue
                    else:
                        continue
                else:
                    continue

            await asyncio.sleep(1.2)
            await _settle_dropdown_selection(page)

            val_raw = await page.evaluate(_ORACLE_COMBOBOX_READ_VALUE_JS, ff_id)
            val_result = json.loads(val_raw) if isinstance(val_raw, str) else val_raw
            effective = ""
            if isinstance(val_result, dict):
                effective = str(val_result.get("committed") or val_result.get("value") or "").strip()

            picked_label = option_labels[idx] if 0 <= idx < len(option_labels) else str(
                click_result.get("text") or ""
            ).strip()

            if not effective:
                logger.info(
                    "domhand.oracle_school_llm_empty_after_click",
                    field_label=field.name,
                    search_term=term[:60],
                    val_raw=str(val_raw)[:120] if val_raw else "None",
                )
                continue

            option_llm_calls += 1
            if option_llm_calls >= max_option_llm_calls:
                break

            verified = await oracle_combobox_verify_commit_llm(canonical, effective, picked_label)
            logger.info(
                "domhand.oracle_school_llm_verify",
                field_label=field.name,
                verified=verified,
                canonical=canonical[:60],
                committed=effective[:60],
                picked=picked_label[:60],
            )
            if not verified:
                with contextlib.suppress(Exception):
                    await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
                await asyncio.sleep(0.1)
                # Clear residual text to prevent Oracle auto-commit on blur
                with contextlib.suppress(Exception):
                    await page.evaluate(_FOCUS_AND_CLEAR_JS, ff_id)
                continue

            if await _field_has_validation_error(page, ff_id):
                return _fill_outcome(False)

            logger.info(
                "domhand.oracle_school_llm_ok",
                field_label=field.name,
                committed=effective[:80],
                search_term=term[:60],
                generation=gen + 1,
            )
            return _fill_outcome(True, matched_label=picked_label or effective)

        if option_llm_calls >= max_option_llm_calls:
            break

    # Clear the combobox input to prevent Oracle auto-committing the first
    # visible filtered option when focus leaves (the "A&M University" bug).
    with contextlib.suppress(Exception):
        await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
    await asyncio.sleep(0.1)
    with contextlib.suppress(Exception):
        await page.evaluate(_FOCUS_AND_CLEAR_JS, ff_id)
    with contextlib.suppress(Exception):
        await page.evaluate(
            r"""(ffId) => {
            var el = window.__ff ? window.__ff.byId(ffId) : null;
            if (!el) return;
            el.value = '';
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.blur();
        }""",
            ff_id,
        )
    logger.warning(
        "domhand.oracle_school_llm_exhausted",
        field_label=field.name,
        canonical=canonical[:80],
        terms_tried=len(all_terms_tried),
    )
    return _fill_outcome(False)


def _oracle_combobox_search_terms(value: str) -> list[str]:
    """Generate progressively shorter search terms for Oracle combobox retry.

    When the full value doesn't match Oracle's naming format (e.g. comma vs
    dash, abbreviation), shorter sub-phrases are more likely to filter the
    dropdown to a small set containing the target.
    """
    raw = re.sub(r"\s+", " ", (value or "").strip())
    if not raw:
        return []
    terms: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        t = t.strip()
        if not t or t.lower() in seen:
            return
        seen.add(t.lower())
        terms.append(t)

    # 1. Full value (stripped of parenthetical suffix)
    stripped = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
    add(stripped)
    # 2. Parenthetical abbreviation (e.g. "UCLA" from "(UCLA)")
    paren = re.search(r"\(([^)]{2,})\)\s*$", raw)
    if paren:
        add(paren.group(1))
    # 3. Comma segments: "University of California, Los Angeles" → "Los Angeles"
    parts = [p.strip() for p in stripped.split(",") if p.strip()]
    if len(parts) >= 2:
        # Last segment (e.g. "Los Angeles")
        add(parts[-1])
        # Last two segments combined
        add(", ".join(parts[-2:]))
    # 4. First significant word (skip stop words + school-generic words)
    _stop = {
        "of", "and", "in", "the", "a", "an", "for", "to", "at", "by",
        "university", "college", "institute", "school", "academy",
        "polytechnic", "new", "state",
    }
    words = [w for w in stripped.split() if w.lower() not in _stop and len(w) > 3]
    if words:
        add(words[0])
    return terms


async def _fill_oracle_combobox_outcome(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
) -> FieldFillOutcome:
    """Fill an Oracle cx-select combobox using real keyboard events.

    Oracle's React/Fusion framework ignores values set via JS — it requires
    actual keystrokes to trigger the autocomplete and commit the selection.

    When the first type attempt doesn't find a matching option (naming format
    differs between profile and Oracle's database), retries with progressively
    shorter search terms derived from the value.
    """
    ff_id = field.field_id
    if not value:
        logger.debug(f"skip {tag} (oracle combobox, no answer)")
        return _fill_outcome(False)

    try:
        exists_raw = await page.evaluate(_ELEMENT_EXISTS_JS, ff_id, field.field_type)
        exists = json.loads(exists_raw) if isinstance(exists_raw, str) else exists_raw
        if not (isinstance(exists, dict) and exists.get("exists")):
            logger.warning(
                "domhand.oracle_combobox_element_not_found",
                field_label=field.name,
                field_id=ff_id,
                field_type=field.field_type,
                oracle_freeform_flag=field.oracle_freeform_combobox_answer,
                exists_raw=str(exists_raw)[:120] if exists_raw else "None",
            )
            return _fill_outcome(False)

        is_school_llm = _is_oracle_school_llm_field(field)
        logger.info(
            "domhand.oracle_combobox_path_decision",
            field_label=field.name,
            field_id=ff_id,
            is_school_llm=is_school_llm,
            oracle_freeform_flag=field.oracle_freeform_combobox_answer,
            value=value[:80],
        )
        if is_school_llm:
            from ghosthands.dom.oracle_combobox_llm import _oracle_school_llm_disabled
            if _oracle_school_llm_disabled():
                logger.warning(
                    "domhand.oracle_school_llm_disabled",
                    field_label=field.name,
                    reason="no OpenAI API key or VALET proxy grant configured",
                )
            return await _fill_oracle_school_combobox_llm_outcome(page, field, value, tag)

        # Build search terms: full value first, then shorter alternatives.
        search_terms = _oracle_combobox_search_terms(value)
        if not search_terms:
            search_terms = [value]

        matched_label: str | None = None
        effective = ""

        for attempt_idx, term in enumerate(search_terms):
            await page.evaluate(_FOCUS_AND_CLEAR_JS, ff_id)
            await asyncio.sleep(0.15)

            await _type_text_compat(page, term, delay=30)

            # Poll for visible options instead of a fixed sleep — Oracle's grid
            # can take >1s to render and a single read misses late arrivals.
            polled_options = await _poll_oracle_combobox_options(page, ff_id)
            if not polled_options:
                logger.debug(
                    "domhand.oracle_combobox_no_options_after_poll",
                    field_label=field.name,
                    search_term=term[:60],
                    attempt=attempt_idx + 1,
                )

            # Try to click the best matching option — match against the
            # ORIGINAL desired value, not the search term.
            click_raw = await page.evaluate(
                _ORACLE_COMBOBOX_CLICK_BEST_OPTION_JS, ff_id, value,
            )
            click_result = json.loads(click_raw) if isinstance(click_raw, str) else click_raw
            if isinstance(click_result, dict) and click_result.get("clicked"):
                matched_label = click_result.get("text")
                await asyncio.sleep(0.3)

            await asyncio.sleep(1.2)

            val_raw = await page.evaluate(_ORACLE_COMBOBOX_READ_VALUE_JS, ff_id)
            val_result = json.loads(val_raw) if isinstance(val_raw, str) else val_raw
            current_val = (val_result.get("value") or "") if isinstance(val_result, dict) else ""
            committed = (val_result.get("committed") or "") if isinstance(val_result, dict) else ""
            effective = committed or current_val

            if effective.strip():
                logger.debug(
                    "domhand.oracle_combobox_ok",
                    field_label=field.name,
                    value=effective[:60],
                    matched_label=(matched_label or "")[:60],
                    search_term=term[:60],
                    attempt=attempt_idx + 1,
                )
                if await _field_has_validation_error(page, ff_id):
                    return _fill_outcome(False)
                return _fill_outcome(True, matched_label=matched_label)

            # No match with this term — log and try the next one.
            logger.debug(
                "domhand.oracle_combobox_retry",
                field_label=field.name,
                search_term=term[:60],
                attempt=attempt_idx + 1,
                remaining=len(search_terms) - attempt_idx - 1,
            )

        # All search terms exhausted — employer fallback to "Other".
        _is_employer = any(
            token in normalize_name(field.name or "")
            for token in ("employer", "company", "organization")
        )
        if _is_employer and value.lower() != "other":
            logger.info(
                "domhand.oracle_combobox_employer_fallback",
                field_label=field.name,
                attempted=value[:60],
                fallback="Other",
            )
            await page.evaluate(_FOCUS_AND_CLEAR_JS, ff_id)
            await asyncio.sleep(0.15)
            await _type_text_compat(page, "Other", delay=30)
            await asyncio.sleep(1.0)
            click_raw = await page.evaluate(
                _ORACLE_COMBOBOX_CLICK_BEST_OPTION_JS, ff_id, "Other",
            )
            click_result = json.loads(click_raw) if isinstance(click_raw, str) else click_raw
            if isinstance(click_result, dict) and click_result.get("clicked"):
                matched_label = click_result.get("text")
                await asyncio.sleep(1.2)
                val_raw = await page.evaluate(_ORACLE_COMBOBOX_READ_VALUE_JS, ff_id)
                val_result = json.loads(val_raw) if isinstance(val_raw, str) else val_raw
                effective = (val_result.get("committed") or val_result.get("value") or "") if isinstance(val_result, dict) else ""
                if effective.strip():
                    return _fill_outcome(True, matched_label=matched_label)

        # Clear the combobox input to prevent Oracle auto-committing the first
        # visible filtered option when focus leaves (the "A&M University" bug).
        with contextlib.suppress(Exception):
            await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
        await asyncio.sleep(0.1)
        with contextlib.suppress(Exception):
            await page.evaluate(_FOCUS_AND_CLEAR_JS, ff_id)
        with contextlib.suppress(Exception):
            await page.evaluate(
                r"""(ffId) => {
                var el = window.__ff ? window.__ff.byId(ffId) : null;
                if (!el) return;
                el.value = '';
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.blur();
            }""",
                ff_id,
            )
        logger.warning(
            "domhand.oracle_combobox_rejected",
            field_label=field.name,
            attempted=value[:60],
            search_terms_tried=[t[:40] for t in search_terms],
        )
        return _fill_outcome(False)

    except Exception as exc:
        logger.warning(
            "domhand.oracle_combobox_error",
            field_label=field.name,
            error=str(exc),
        )
        return _fill_outcome(False)


_ORACLE_COMBOBOX_TRY_FIRST_TYPES = frozenset(
    {"text", "email", "tel", "url", "number", "password", "search", "select"}
)


async def _try_oracle_searchable_combobox_first(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
    *,
    page_url: str,
) -> FieldFillOutcome | None:
    """Oracle only: if the DOM node matches searchable-combobox signals, try ``oracle_combobox``.

    Returns a successful ``FieldFillOutcome`` only when the keyboard + pick path commits.
    Otherwise returns ``None`` so the caller continues with the normal pipeline
    (``_fill_select_field_outcome`` / ``_fill_text_field`` / searchable dropdown, etc.).
    This avoids blanket ``fill_overrides`` that force every ``select`` down one path.
    """
    if field.field_type not in _ORACLE_COMBOBOX_TRY_FIRST_TYPES:
        return None
    try:
        from ghosthands.platforms import detect_platform

        if detect_platform(page_url) != "oracle":
            return None
    except Exception:
        return None
    try:
        searchable = await page.evaluate(_IS_ORACLE_SEARCHABLE_JS, field.field_id)
        if not searchable:
            return None
    except Exception:
        return None
    outcome = await _fill_oracle_combobox_outcome(page, field, value, tag)
    if outcome.success:
        return outcome
    # Entity-name fields (school, employer): do NOT fall through to generic
    # fill_interactive_dropdown — word-overlap matching silently picks wrong entities
    # (e.g. "University" token matching "9 Eylul University" for UCLA).
    if _is_oracle_entity_no_fuzzy_fallback_field(field):
        logger.info(
            "domhand.oracle_entity_no_fuzzy_fallback",
            field_label=field.name,
            field_type=field.field_type,
            value=value[:60],
        )
        return outcome  # return the failed outcome — caller sees success=False, no fallthrough
    logger.debug(
        "domhand.oracle_combobox_fallthrough",
        field_label=field.name,
        field_type=field.field_type,
    )
    return None


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
) -> FieldFillOutcome:
    """Greenhouse / react-select: same CDP discovery + open/poll as domhand_select, then page-level option click.

    Skips ``GetDropdownOptionsEvent`` so the default action watchdog does not log errors for
    ``input.select__input`` comboboxes during the initial domhand_fill pass.
    """
    from browser_use.browser.events import ClickElementEvent
    from ghosthands.actions.domhand_select import (
        _DISCOVER_OPTIONS_ON_NODE_JS,
        _SELECT_NATIVE_ON_NODE_JS,
        _call_function_on_node,
        _fuzzy_match_option,
        _meaningful_dropdown_options,
        _needs_dropdown_open_trigger,
        _options_for_fuzzy_match,
        _try_click_combobox_toggle,
    )
    from ghosthands.dom.shadow_helpers import ensure_helpers

    with contextlib.suppress(Exception):
        await ensure_helpers(page)

    with contextlib.suppress(Exception):
        await page.evaluate(_SCROLL_FF_INTO_VIEW_JS, field.field_id)
        await asyncio.sleep(0.35)

    node = await _find_dom_tree_node_by_ff_id(browser_session, field.field_id)
    if node is None:
        _log_dropdown_diag(
            "domhand.fill.dropdown_cdp_no_node",
            field_id=field.field_id,
            field_label=_preferred_field_label(field),
            tag=tag,
        )
        return _fill_outcome(False)

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
        return _fill_outcome(False)

    if not isinstance(discovery, dict):
        return _fill_outcome(False)

    dropdown_type = str(discovery.get("type") or "unknown")
    options: list[dict[str, Any]] = list(discovery.get("options") or [])

    if _needs_dropdown_open_trigger(is_native_select, dropdown_type, options):
        for _click_attempt in range(3):
            try:
                toggled = False
                if not is_native_select:
                    trusted = await trusted_open_combobox_by_ffid(page, field.field_id)
                    toggled = bool(trusted.get("clicked") or trusted.get("already_open"))
                    if not toggled:
                        toggled = await _try_click_combobox_toggle(browser_session, node)
                if not toggled:
                    event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
                    await event
                    await event.event_result(raise_if_any=True, raise_if_none=False)
                await asyncio.sleep(_DROPDOWN_MENU_OPEN_SETTLE_S)
                for _tick in range(_DROPDOWN_CDP_POLL_MAX_TICKS):
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
                    await asyncio.sleep(_DROPDOWN_CDP_POLL_TICK_S)
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
        _log_dropdown_diag(
            "domhand.fill.dropdown_cdp_no_fuzzy_match",
            field_id=field.field_id,
            field_label=_preferred_field_label(field),
            tag=tag,
            desired_preview=str(value)[:80],
            option_sample=[str(o.get("text") or "")[:40] for o in match_options[:5]],
        )
        return _fill_outcome(False)

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
            clicked = await _click_dropdown_option(page, matched_text, field_id=field.field_id)
            result = {
                "success": bool(clicked.get("clicked")),
                "clicked": clicked.get("text", matched_text),
            }
    except Exception as exc:
        logger.debug(
            "domhand.fill.dropdown_cdp_click_fail",
            field_id=field.field_id,
            tag=tag,
            error=str(exc)[:120],
        )
        return _fill_outcome(False)

    if not (isinstance(result, dict) and result.get("success")):
        return _fill_outcome(False)

    # Let react-select commit before reading single-value (avoids false mismatch vs verify).
    await asyncio.sleep(POST_OPTION_CLICK_SETTLE_S)

    # Pass matched_text so verify accepts committed UI that differs from profile (synonyms / longer labels).
    current = await _wait_for_field_value(page, field, value, timeout=2.85, matched_label=matched_text)
    if not _field_value_matches_expected(current, value, matched_label=matched_text):
        return _fill_outcome(False)

    await _settle_dropdown_selection(page)
    if await _field_has_validation_error(page, field.field_id):
        return _fill_outcome(False)
    _log_dropdown_diag(
        "domhand.fill.dropdown_cdp_first_ok",
        field_id=field.field_id,
        tag=tag,
        matched_text=matched_text[:80],
    )
    return _fill_outcome(True, matched_label=matched_text)


async def _fill_select_field_outcome(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
    *,
    browser_session: BrowserSession | None = None,
) -> FieldFillOutcome:
    if not value:
        logger.debug(f"skip {tag} (no value)")
        return _fill_outcome(False)
    if field.is_native:
        try:
            result_json = await page.evaluate(_FILL_FIELD_JS, field.field_id, value, "select")
            result = json.loads(result_json) if isinstance(result_json, str) else result_json
            if isinstance(result, dict) and result.get("success"):
                logger.debug(f'select {tag} -> "{value}"')
                return _fill_outcome(True)
        except Exception:
            pass
        logger.debug(f"skip {tag} (native select failed)")
        return _fill_outcome(False)

    page_url = ""
    with contextlib.suppress(Exception):
        page_url = str(await _safe_page_url(page) or "")
    if _is_workday_url(page_url) and _is_workday_referral_source_field(field):
        return await _fill_workday_referral_source_select(page, field, value, tag)

    if _is_workday_url(page_url) and _is_workday_prompt_search_field(field):
        return await _fill_workday_prompt_search(page, field, value, tag)

    if field.is_multi_select or await _uses_workday_skill_multiselect(page, field):
        values = [v.strip() for v in value.split(",") if v.strip()]
        return _fill_outcome(await _fill_multi_select(page, field, values, tag))
    return await _fill_custom_dropdown_outcome(
        page,
        field,
        value,
        tag,
        browser_session=browser_session,
    )


async def _fill_select_field(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
    *,
    browser_session: BrowserSession | None = None,
) -> bool:
    return (await _fill_select_field_outcome(page, field, value, tag, browser_session=browser_session)).success


async def _type_text_compat(page: Any, text: str, *, delay: int = 0) -> None:
    """Type text on either a Playwright page or browser_use actor page."""
    keyboard = getattr(page, "keyboard", None)
    if keyboard is not None and hasattr(keyboard, "type"):
        await keyboard.type(text, delay=delay)
        return
    client = getattr(page, "_client", None)
    session_id = None
    if client is not None and getattr(type(page), "session_id", None) is not None:
        with contextlib.suppress(Exception):
            session_id = await page.session_id
    if client is not None and session_id:
        if delay and delay > 0:
            for char in text:
                await client.send.Input.insertText(params={"text": char}, session_id=session_id)
                await asyncio.sleep(delay / 1000.0)
        else:
            await client.send.Input.insertText(params={"text": text}, session_id=session_id)
        return
    raise AttributeError("page does not support text typing")


async def _press_key_compat(page: Any, key: str) -> None:
    """Press a key on either a Playwright page or browser_use actor page."""
    keyboard = getattr(page, "keyboard", None)
    if keyboard is not None and hasattr(keyboard, "press"):
        await keyboard.press(key)
        return
    if hasattr(page, "press"):
        await page.press(key)
        return
    raise AttributeError(f"page does not support key press: {key}")


async def _fill_multi_select(page: Any, field: FormField, values: list[str], tag: str) -> bool:
    ff_id = field.field_id
    try:
        if await _uses_workday_skill_multiselect(page, field):
            workday_result = await _fill_workday_skill_multiselect(page, field, values, tag)
            if workday_result is not None:
                return workday_result
        picked_count = 0
        for val in values:
            before_selection = await _read_multi_select_selection(page, ff_id)
            await _try_open_combobox_menu(page, ff_id, tag=tag)
            await asyncio.sleep(0.2)
            await _clear_dropdown_search(page, ff_id)
            with contextlib.suppress(Exception):
                await page.evaluate(_FOCUS_FIELD_JS, ff_id)
            await asyncio.sleep(0.08)
            await _type_text_compat(page, val, delay=55)
            await asyncio.sleep(0.45)
            clicked = await _poll_click_dropdown_option(page, val, field_id=ff_id, max_wait_s=2.85)
            if clicked.get("clicked"):
                await _settle_dropdown_selection(page)
                committed = await _wait_for_multi_select_commit(
                    page, field, val,
                    previous_selection=before_selection,
                    matched_label=clicked.get("text"),
                )
                if committed.get("committed"):
                    picked_count += 1
                    continue
            with contextlib.suppress(Exception):
                await _press_key_compat(page, "ArrowDown")
            await asyncio.sleep(0.12)
            with contextlib.suppress(Exception):
                await _press_key_compat(page, "Enter")
            await asyncio.sleep(0.3)
            await _settle_dropdown_selection(page)
            committed = await _wait_for_multi_select_commit(
                page, field, val,
                previous_selection=before_selection,
            )
            if committed.get("committed"):
                picked_count += 1
                continue
            logger.debug(f'multi-select {tag} option "{val}" not committed')

        try:
            await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
        except Exception:
            pass
        if picked_count > 0:
            logger.debug(f"multi-select {tag} -> {picked_count}/{len(values)} options")
            return True
    except Exception as e:
        logger.debug(f"multi-select {tag} failed: {str(e)[:60]}")
    return False


async def _fill_workday_skill_multiselect(
    page: Any,
    field: FormField,
    values: list[str],
    tag: str,
) -> bool | None:
    """Workday skill multi-select — matches main's _fill_multi_select flow.

    For each skill: open → clear → type → poll click → Enter fallback → count picked.
    Keeps normalization/dedup and 15-skill cap from current branch.
    """
    ff_id = field.field_id
    widget = await _read_workday_skill_widget(page, ff_id)
    if not widget.get("is_workday_skill"):
        return None

    normalized_values: list[str] = []
    seen_values: set[str] = set()
    for raw_value in values:
        skill = str(raw_value or "").strip()
        if not skill:
            continue
        key = skill.casefold()
        if key in seen_values:
            continue
        seen_values.add(key)
        normalized_values.append(skill)
        if len(normalized_values) >= 15:
            break
    if not normalized_values:
        return False

    picked_count = 0
    try:
        for val in normalized_values:
            before_selection = await _read_multi_select_selection(page, ff_id)
            # Open/focus (like main's click-to-open but via combobox helper)
            await _try_open_combobox_menu(page, ff_id, tag=tag)
            await asyncio.sleep(0.3)

            # Clear stale query — JS React setter + keyboard fallback
            with contextlib.suppress(Exception):
                await _clear_skill_input(page, ff_id)
            await _clear_dropdown_search(page, ff_id)
            with contextlib.suppress(Exception):
                await page.evaluate(_FOCUS_FIELD_JS, ff_id)
            await asyncio.sleep(0.15)

            # Type the skill (keyboard is more reliable for Workday React search)
            await _type_text_compat(page, val, delay=55)

            # Workday skill: press Enter after typing — many Workday skill widgets
            # are freeform and commit on Enter without showing a dropdown.
            await asyncio.sleep(1.0)
            with contextlib.suppress(Exception):
                await _press_key_compat(page, "Enter")
            await asyncio.sleep(_WORKDAY_SKILL_POST_ENTER_SETTLE_S)

            # Check if Enter committed a chip
            committed = await _wait_for_multi_select_commit(
                page, field, val,
                previous_selection=before_selection,
            )
            if committed.get("committed"):
                picked_count += 1
                # Clear input after commit — Workday keeps stale text
                with contextlib.suppress(Exception):
                    await _clear_skill_input(page, ff_id)
                await _clear_dropdown_search(page, ff_id)
                continue

            # Enter didn't commit — scan visible options and use LLM to pick
            from ghosthands.dom.oracle_combobox_llm import dropdown_pick_option_llm

            try:
                raw_options = await page.evaluate(SCAN_VISIBLE_OPTIONS_JS)
                visible_opts = json.loads(raw_options) if isinstance(raw_options, str) else raw_options
                opt_labels = [str(o.get("text", "")).strip() for o in (visible_opts or []) if o.get("text", "").strip()]
            except Exception:
                opt_labels = []

            if opt_labels:
                llm_idx = await dropdown_pick_option_llm(val, opt_labels, context="skill")
                if llm_idx is not None:
                    # Click the LLM-picked option by index
                    clicked = await _click_dropdown_option_by_index(page, llm_idx)
                    if clicked:
                        await _settle_dropdown_selection(page)
                        committed = await _wait_for_multi_select_commit(
                            page, field, val,
                            previous_selection=before_selection,
                            matched_label=opt_labels[llm_idx] if llm_idx < len(opt_labels) else None,
                        )
                        if committed.get("committed"):
                            picked_count += 1
                            with contextlib.suppress(Exception):
                                await _clear_skill_input(page, ff_id)
                            await _clear_dropdown_search(page, ff_id)
                            continue

            logger.debug(f'workday skill multi-select {tag} option "{val}" not committed')

        # Dismiss (like main)
        try:
            await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
        except Exception:
            pass

        if picked_count > 0:
            logger.debug(f"workday skill multi-select {tag} -> {picked_count}/{len(normalized_values)} skills")
            return True
    except Exception as exc:
        logger.debug(f"workday skill multi-select {tag} failed: {str(exc)[:60]}")
    return False


_TAG_WORKDAY_CHIP_DELETE_JS = r"""(ffId) => {
    var old = document.querySelector('[data-dh-chip-delete]');
    if (old) old.removeAttribute('data-dh-chip-delete');

    var ff = window.__ff;
    var el = ff ? ff.byId(ffId) : null;
    if (!el) return JSON.stringify({found: false, reason: 'no_element'});

    // Find pill/chip elements
    var pills = el.querySelectorAll('[role="option"], [data-automation-id*="pill"], [id*="pill"]');
    for (var p of pills) {
        if (p.offsetWidth <= 0 || p.offsetHeight <= 0) continue;
        // Look for the × delete button inside the chip
        var del = p.querySelector('button[aria-label*="elete"], button[aria-label*="emove"], [data-automation-id="delete"]');
        if (!del) {
            // Try any small button/span inside the pill
            var candidates = p.querySelectorAll('button, span[role="button"], [role="button"]');
            for (var c of candidates) {
                var t = (c.textContent || '').trim();
                if (t === '×' || t === 'x' || t === '✕' || c.offsetWidth < 30) {
                    del = c;
                    break;
                }
            }
        }
        if (del) {
            del.setAttribute('data-dh-chip-delete', 'true');
            del.scrollIntoView({block: 'center', behavior: 'instant'});
            return JSON.stringify({found: true, chip_text: (p.textContent||'').trim().slice(0,80)});
        }
        // No × found — tag the pill itself
        p.setAttribute('data-dh-chip-delete', 'true');
        p.scrollIntoView({block: 'center', behavior: 'instant'});
        return JSON.stringify({found: true, chip_text: (p.textContent||'').trim().slice(0,80), tagged_pill: true});
    }
    return JSON.stringify({found: false, reason: 'no_chips'});
}"""


async def _fill_workday_prompt_search(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
) -> FieldFillOutcome:
    """Fill a Workday prompt-search single-select widget (School, Field of Study).

    Workday prompt-search widgets require Enter after typing to trigger search.
    Flow: remove wrong chip (if any) → clear → type → Enter → wait → click result.
    """
    ff_id = field.field_id
    desired = str(value or "").strip()
    if not desired:
        return _fill_outcome(False)

    logger.info(
        "domhand.workday_prompt_search.enter",
        field_label=field.name,
        field_id=ff_id,
        value=desired[:60],
    )

    try:
        # Step 1: Remove wrong autofill chip(s) via Playwright click on × button
        # JS tags the × button, Playwright clicks it (trusted event for React)
        for _chip_attempt in range(3):  # max 3 chips to remove
            try:
                tag_raw = await page.evaluate(_TAG_WORKDAY_CHIP_DELETE_JS, ff_id)
                tag_result = json.loads(tag_raw) if isinstance(tag_raw, str) else tag_raw
            except Exception:
                break

            if not tag_result.get("found"):
                break

            logger.info(
                "domhand.workday_prompt_search.removing_chip",
                field_label=field.name,
                chip_text=tag_result.get("chip_text", ""),
            )

            try:
                btn = page.locator('[data-dh-chip-delete="true"]')
                await btn.click(timeout=3000)
                await asyncio.sleep(0.5)
            except Exception:
                # Fallback: keyboard Backspace
                await _clear_skill_input(page, ff_id)
                with contextlib.suppress(Exception):
                    await _press_key_compat(page, "Backspace")
                await asyncio.sleep(0.3)
            finally:
                with contextlib.suppress(Exception):
                    await page.evaluate(
                        'var e=document.querySelector("[data-dh-chip-delete]");'
                        'if(e)e.removeAttribute("data-dh-chip-delete")'
                    )

        # Step 2: Ensure input is focused and clear
        await _try_open_combobox_menu(page, ff_id, tag=tag)
        await asyncio.sleep(0.3)
        await _clear_skill_input(page, ff_id)
        with contextlib.suppress(Exception):
            await page.evaluate(_FOCUS_FIELD_JS, ff_id)
        await asyncio.sleep(0.15)

        # Step 4: Type search term + Enter to trigger search
        # Use partial fragments for better matching
        search_terms = _workday_school_search_terms(desired)

        for term in search_terms:
            # Clear before each attempt
            await _clear_skill_input(page, ff_id)
            with contextlib.suppress(Exception):
                await page.evaluate(_FOCUS_FIELD_JS, ff_id)
            await asyncio.sleep(0.1)

            await _type_text_compat(page, term, delay=55)
            await asyncio.sleep(0.5)

            # Press Enter to trigger Workday search
            with contextlib.suppress(Exception):
                await _press_key_compat(page, "Enter")
            await asyncio.sleep(3.0)

            # Step 5: Poll for matching result
            clicked = await _poll_click_dropdown_option(
                page, desired, field_id=ff_id, max_wait_s=2.0,
            )
            if clicked.get("clicked"):
                await asyncio.sleep(0.5)
                logger.info(
                    "domhand.workday_prompt_search.committed",
                    field_label=field.name,
                    search_term=term,
                    clicked_text=clicked.get("text", ""),
                )
                return _fill_outcome(True, clicked.get("text") or desired)

            logger.debug(
                f"workday prompt-search {tag}: no match for term '{term}'"
            )

        logger.warning(
            "domhand.workday_prompt_search.no_match",
            field_label=field.name,
            value=desired[:60],
            terms_tried=len(search_terms),
        )
        return _fill_outcome(False)

    except Exception as exc:
        logger.warning(
            "domhand.workday_prompt_search.failed",
            field_label=field.name,
            error=str(exc)[:120],
        )
        return _fill_outcome(False)


def _workday_school_search_terms(full_name: str) -> list[str]:
    """Generate search fragments for Workday school/university lookup."""
    full = re.sub(r"\s+", " ", str(full_name or "").strip())
    if not full:
        return []

    terms: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        key = t.strip().lower()
        if key and key not in seen:
            seen.add(key)
            terms.append(t.strip())

    # Full name first
    add(full)

    # Without parenthetical (UCLA) suffix
    no_paren = re.sub(r"\s*\([^)]*\)\s*$", "", full).strip()
    if no_paren != full:
        add(no_paren)

    # Abbreviation in parentheses
    paren_match = re.search(r"\(([^)]+)\)", full)
    if paren_match:
        add(paren_match.group(1))

    # Last significant word (city/campus name)
    words = [w for w in full.split() if len(w) > 2 and w.lower() not in
             ("of", "the", "and", "at", "in", "for", "university", "college", "institute")]
    if words:
        add(words[-1])

    return terms[:4]


async def _fill_workday_referral_source_select(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
) -> FieldFillOutcome:
    """Workday referral/source handler — matches main's _fill_custom_dropdown flow.

    Open → direct click → hierarchy → type-search+click → ArrowDown+Enter.
    """
    ff_id = field.field_id
    desired = str(value or "").strip()
    if not desired:
        logger.debug(f"skip {tag} (workday referral/source, no value)")
        return _fill_outcome(False)

    try:
        # Phase 1: Open and try direct click (main's first pass)
        await _try_open_combobox_menu(page, ff_id, tag=tag)
        await asyncio.sleep(0.6)

        clicked = await _click_dropdown_option(page, desired, field_id=ff_id)
        if clicked.get("clicked"):
            current = await _wait_for_field_value(page, field, desired, matched_label=clicked.get("text"))
            if _field_value_matches_expected(current, desired, matched_label=clicked.get("text")):
                logger.debug(f'workday referral/source {tag} -> "{clicked.get("text", desired)}"')
                await _settle_dropdown_selection(page)
                return _fill_outcome(True, matched_label=clicked.get("text"))

        # Phase 2: Hierarchy segments (main's second pass)
        segments = split_dropdown_value_hierarchy(desired)
        if len(segments) > 1:
            all_clicked = True
            for idx, segment in enumerate(segments):
                seg_clicked = await _click_dropdown_option(page, segment, field_id=ff_id)
                if not seg_clicked.get("clicked"):
                    all_clicked = False
                    break
                await asyncio.sleep(0.8 if idx < len(segments) - 1 else 0.45)
            if all_clicked:
                current = await _wait_for_field_value(page, field, desired, timeout=2.8)
                if _field_value_matches_expected(current, desired):
                    logger.debug(f'workday referral/source {tag} -> "{desired}" (hierarchy)')
                    await _settle_dropdown_selection(page, delay=0.6)
                    return _fill_outcome(True)

        # Phase 3: Type search terms and click (main's type-and-click + searchable fallback)
        search_terms = generate_dropdown_search_terms(desired)
        if not search_terms:
            search_terms = [desired]

        for term_idx, term in enumerate(search_terms):
            query = str(term or "").strip()
            if not query:
                continue
            try:
                if term_idx > 0:
                    await _clear_dropdown_search(page, ff_id)
                with contextlib.suppress(Exception):
                    await page.evaluate(_FOCUS_FIELD_JS, ff_id)
                    await asyncio.sleep(0.08)
                await _type_text_compat(page, query, delay=45)

                poll_budget = 2.85 if term_idx == 0 else 2.2
                clicked = await _poll_click_dropdown_option(
                    page, desired, query, field_id=ff_id, max_wait_s=poll_budget,
                )
                if clicked.get("clicked"):
                    logger.debug(f'workday referral/source {tag} -> "{clicked.get("text", desired)}" (typed "{query}")')
                    await _settle_dropdown_selection(page)
                    return _fill_outcome(True, matched_label=clicked.get("text"))

                if term_idx == len(search_terms) - 1:
                    # Last resort: ArrowDown + Enter (main's fallback)
                    with contextlib.suppress(Exception):
                        await _press_key_compat(page, "ArrowDown")
                    await asyncio.sleep(0.2)
                    with contextlib.suppress(Exception):
                        await _press_key_compat(page, "Enter")
                    await asyncio.sleep(0.35)
                    logger.debug(f'workday referral/source {tag} -> first result (keyboard, term: "{query}")')
                    await _settle_dropdown_selection(page)
                    return _fill_outcome(True)
            except Exception:
                continue

    except Exception as exc:
        logger.debug(f"workday referral/source {tag} failed: {str(exc)[:60]}")

    return _fill_outcome(False)


async def _set_text_field_value(page: Any, field_id: str, value: str) -> None:
    try:
        await page.evaluate(_FILL_FIELD_JS, field_id, value, "text")
    except Exception:
        pass


async def _read_workday_skill_widget(page: Any, field_id: str) -> dict[str, Any]:
    try:
        raw = await page.evaluate(_READ_WORKDAY_SKILL_WIDGET_JS, field_id)
    except Exception:
        return {"is_workday_skill": False}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {"is_workday_skill": False}
    return parsed if isinstance(parsed, dict) else {"is_workday_skill": False}


async def _read_workday_skill_input_state(page: Any, field_id: str) -> dict[str, Any]:
    try:
        raw = await page.evaluate(_READ_WORKDAY_SKILL_INPUT_STATE_JS, field_id)
    except Exception:
        return {"found_input": False, "input_value": "", "has_clear_button": False}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {"found_input": False, "input_value": "", "has_clear_button": False}
    return parsed if isinstance(parsed, dict) else {"found_input": False, "input_value": "", "has_clear_button": False}


async def _focus_workday_skill_input(page: Any, field_id: str) -> bool:
    try:
        raw = await page.evaluate(_FOCUS_WORKDAY_SKILL_INPUT_JS, field_id)
    except Exception:
        return False
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return False
    return bool(isinstance(parsed, dict) and parsed.get("focused"))


async def _set_workday_skill_input_value(page: Any, field_id: str, value: str) -> bool:
    try:
        raw = await page.evaluate(_SET_WORKDAY_SKILL_INPUT_VALUE_JS, [field_id, value])
    except Exception:
        return False
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return False
    return bool(isinstance(parsed, dict) and parsed.get("success"))


async def _click_workday_skill_clear_button(page: Any, *, field_id: str) -> bool:
    try:
        raw_target = await page.evaluate(_GET_WORKDAY_SKILL_CLEAR_TARGET_JS, field_id)
        target = json.loads(raw_target) if isinstance(raw_target, str) else raw_target
    except Exception:
        return False
    if not isinstance(target, dict) or not target.get("found"):
        return False
    try:
        mouse = await page.mouse
        await mouse.move(float(target["x"]), float(target["y"]))
        await asyncio.sleep(0.05)
        await mouse.click(float(target["x"]), float(target["y"]))
        await asyncio.sleep(0.15)
        return True
    except Exception:
        pass
    try:
        marked = await page.get_elements_by_css_selector('[data-domhand-workday-skill-clear-target="true"]')
    except Exception:
        marked = []
    if marked:
        try:
            await marked[0].click()
            await asyncio.sleep(0.15)
            return True
        except Exception:
            pass
    return False


async def _ensure_workday_skill_query_empty(page: Any, field_id: str, *, tag: str) -> bool:
    state = await _read_workday_skill_input_state(page, field_id)
    current_value = str(state.get("input_value") or "").strip()
    if not current_value:
        return True
    if bool(state.get("has_clear_button")):
        with contextlib.suppress(Exception):
            if await _click_workday_skill_clear_button(page, field_id=field_id):
                await asyncio.sleep(0.1)
                state = await _read_workday_skill_input_state(page, field_id)
                if not str(state.get("input_value") or "").strip():
                    return True
    with contextlib.suppress(Exception):
        await _focus_workday_skill_input(page, field_id)
        await asyncio.sleep(0.05)
    for shortcut in ("Meta+A", "Control+A"):
        with contextlib.suppress(Exception):
            await _press_key_compat(page, shortcut)
            await asyncio.sleep(0.03)
    for key in ("Backspace", "Delete"):
        with contextlib.suppress(Exception):
            await _press_key_compat(page, key)
            await asyncio.sleep(0.06)
    state = await _read_workday_skill_input_state(page, field_id)
    if not str(state.get("input_value") or "").strip():
        return True
    if await _set_workday_skill_input_value(page, field_id, ""):
        await asyncio.sleep(0.12)
        state = await _read_workday_skill_input_state(page, field_id)
        if not str(state.get("input_value") or "").strip():
            return True
    logger.debug(
        "domhand.workday_skill_clear_failed",
        field_id=field_id,
        tag=tag,
        current_value=str(state.get("input_value") or ""),
    )
    return False


async def _set_workday_skill_query(page: Any, field_id: str, value: str, *, tag: str) -> bool:
    desired = str(value or "").strip()
    if not desired:
        return True
    with contextlib.suppress(Exception):
        await _focus_workday_skill_input(page, field_id)
        await asyncio.sleep(0.05)
    await _type_text_compat(page, desired, delay=55)
    await asyncio.sleep(0.2)
    state = await _read_workday_skill_input_state(page, field_id)
    actual = str(state.get("input_value") or "").strip()
    if actual == desired:
        return True
    if not await _ensure_workday_skill_query_empty(page, field_id, tag=tag):
        return False
    if await _set_workday_skill_input_value(page, field_id, desired):
        await asyncio.sleep(0.15)
        state = await _read_workday_skill_input_state(page, field_id)
        actual = str(state.get("input_value") or "").strip()
        if actual == desired:
            return True
    logger.debug(
        "domhand.workday_skill_query_mismatch",
        field_id=field_id,
        tag=tag,
        desired_value=desired,
        actual_value=actual,
    )
    return False


async def _open_workday_skill_prompt(page: Any, field_id: str, *, tag: str) -> None:
    with contextlib.suppress(Exception):
        if await _focus_workday_skill_input(page, field_id):
            await asyncio.sleep(0.05)
    with contextlib.suppress(Exception):
        prompt = await _get_workday_skill_prompt_target(page, field_id)
        if prompt.get("clicked"):
            logger.debug(
                "domhand.workday_skill_prompt_open",
                field_id=field_id,
                tag=tag,
                via=f"prompt_{prompt.get('via') or 'click'}",
            )
            await asyncio.sleep(0.08)
            return
    logger.debug("domhand.workday_skill_prompt_open", field_id=field_id, tag=tag, via="focus_then_combobox")
    await _try_open_combobox_menu(page, field_id, tag=tag)


_DISMISS_WORKDAY_SKILL_PROMPT_JS = r"""(ffId) => {
    const ff = window.__ff || null;
    const byFfId = (id) => (ff && ff.byId) ? ff.byId(id) : document.querySelector('[data-ff-id="' + id + '"]');
    const scopedQuery = (root, sel) => {
        if (!root || !root.querySelector) return null;
        try { return root.querySelector(sel); } catch (e) { return null; }
    };
    const isVisible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const el = byFfId(ffId);
    if (!el) return JSON.stringify({ dismissed: false, reason: 'missing_field' });
    const inputContainer = (ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="multiselectInputContainer"]')
        : (el.closest ? el.closest('[data-automation-id="multiselectInputContainer"]') : null);
    const wrapper = ((ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]')
        : (el.closest ? el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]') : null))
        || inputContainer
        || (el.parentElement || null);
    const effectiveContainer = inputContainer || wrapper;
    const input = scopedQuery(effectiveContainer, '[data-uxi-widget-type="selectinput"], input[role="combobox"], input[type="search"], input[type="text"]');
    const button = scopedQuery(effectiveContainer, '[data-automation-id="promptSearchButton"], [data-uxi-widget-type="selectinputicon"]')
        || scopedQuery(wrapper, '[data-automation-id="promptSearchButton"], [data-uxi-widget-type="selectinputicon"]');
    const popup = scopedQuery(wrapper, '[data-automation-id="responsiveMonikerPrompt"], [data-automation-id*="responsiveMonikerPrompt"], [data-uxi-widget-type="prompt"]')
        || document.querySelector('[data-automation-id="responsiveMonikerPrompt"], [data-automation-id*="responsiveMonikerPrompt"], [data-uxi-widget-type="prompt"]');
    let action = 'noop';
    if (isVisible(popup) && button) {
        try {
            button.click();
            action = 'button';
        } catch (e) {}
    }
    if (input && input.blur) {
        try { input.blur(); } catch (e) {}
        if (action === 'noop') action = 'blur';
    }
    if (document.activeElement && document.activeElement.blur) {
        try { document.activeElement.blur(); } catch (e) {}
    }
    if (isVisible(popup)) {
        try {
            document.body.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            if (action === 'noop') action = 'outside_click';
        } catch (e) {}
    }
    if (popup) {
        try { popup.classList.remove('open'); } catch (e) {}
        try { popup.innerHTML = ''; } catch (e) {}
        if (action === 'noop') action = 'popup_clear';
    }
    if (input && input.setAttribute) {
        try { input.setAttribute('aria-expanded', 'false'); } catch (e) {}
    }
    return JSON.stringify({ dismissed: action !== 'noop', action });
}"""


async def _dismiss_workday_skill_prompt(page: Any, field_id: str, *, tag: str) -> None:
    with contextlib.suppress(Exception):
        await _press_key_compat(page, "Escape")
        await asyncio.sleep(0.08)
    with contextlib.suppress(Exception):
        raw = await page.evaluate(_DISMISS_WORKDAY_SKILL_PROMPT_JS, field_id)
        result = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(result, dict) and result.get("dismissed"):
            logger.debug(
                "domhand.workday_skill_prompt_dismissed",
                field_id=field_id,
                tag=tag,
                action=result.get("action") or "unknown",
            )
        await asyncio.sleep(0.08)


async def _get_workday_skill_prompt_target(page: Any, field_id: str) -> dict[str, Any]:
    try:
        raw = await page.evaluate(_GET_WORKDAY_SKILL_PROMPT_TARGET_JS, field_id)
        target = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"clicked": False}
    if not isinstance(target, dict) or not target.get("found"):
        return {"clicked": False}
    try:
        mouse = await page.mouse
        await mouse.move(float(target["x"]), float(target["y"]))
        await asyncio.sleep(0.05)
        await mouse.click(float(target["x"]), float(target["y"]))
        await asyncio.sleep(0.15)
        return {"clicked": True, "via": "mouse"}
    except Exception:
        pass
    try:
        marked = await page.get_elements_by_css_selector('[data-domhand-workday-skill-prompt-target="true"]')
    except Exception:
        marked = []
    if marked:
        try:
            await marked[0].click()
            await asyncio.sleep(0.15)
            return {"clicked": True, "via": "actor_marked"}
        except Exception:
            pass
    return {"clicked": False}


async def _click_workday_skill_option(page: Any, text: str, *, field_id: str) -> dict[str, Any]:
    try:
        raw_target = await page.evaluate(
            _GET_WORKDAY_SKILL_OPTION_TARGET_JS,
            [str(field_id), text, synonym_groups_for_js()],
        )
        target = json.loads(raw_target) if isinstance(raw_target, str) else raw_target
    except Exception:
        return {"clicked": False}
    if not isinstance(target, dict) or not target.get("found"):
        return {"clicked": False}
    try:
        mouse = await page.mouse
        await mouse.move(float(target["x"]), float(target["y"]))
        await asyncio.sleep(0.05)
        await mouse.click(float(target["x"]), float(target["y"]))
        await asyncio.sleep(0.15)
        return {"clicked": True, "text": target.get("text", text), "via": "mouse_scoped"}
    except Exception:
        pass
    try:
        marked = await page.get_elements_by_css_selector('[data-domhand-workday-skill-option-target="true"]')
    except Exception:
        marked = []
    if marked:
        try:
            await marked[0].click()
            await asyncio.sleep(0.15)
            return {"clicked": True, "text": target.get("text", text), "via": "actor_marked"}
        except Exception:
            pass
    try:
        raw_clicked = await page.evaluate(
            """() => {
                const node = document.querySelector('[data-domhand-workday-skill-option-target="true"]');
                if (!node) return false;
                try {
                    if (node.scrollIntoView) node.scrollIntoView({ block: 'nearest', inline: 'nearest' });
                    node.click();
                    return true;
                } catch (e) {
                    return false;
                }
            }"""
        )
        if raw_clicked:
            await asyncio.sleep(0.15)
            return {"clicked": True, "text": target.get("text", text), "via": "dom_click"}
    except Exception:
        pass
    return {"clicked": False}


async def _poll_click_workday_skill_option(
    page: Any,
    *match_texts: str,
    field_id: str,
    max_wait_s: float = 2.85,
    interval_s: float = 0.18,
) -> dict[str, Any]:
    texts = [m for m in match_texts if m and str(m).strip()]
    if not texts:
        return {"clicked": False}
    await asyncio.sleep(0.3)
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        for mt in texts:
            result = await _click_workday_skill_option(page, mt, field_id=field_id)
            if result.get("clicked"):
                return result
        await asyncio.sleep(interval_s)
    return {"clicked": False}


_CLICK_OPTION_BY_INDEX_JS = r"""(targetIndex) => {
    var selectors = '[role="option"], [role="menuitem"], [role="treeitem"], [role="listitem"], [data-automation-id*="promptOption"], [data-automation-id*="selectOption"]';
    var els = document.querySelectorAll(selectors);
    var visibleIdx = 0;
    for (var i = 0; i < els.length; i++) {
        var rect = els[i].getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        var t = (els[i].textContent || '').trim();
        if (!t) continue;
        if (visibleIdx === targetIndex) {
            els[i].click();
            return JSON.stringify({clicked: true, text: t, index: targetIndex});
        }
        visibleIdx++;
    }
    return JSON.stringify({clicked: false, reason: 'index_out_of_range'});
}"""


async def _click_dropdown_option_by_index(page: Any, index: int) -> bool:
    """Click a visible dropdown option by its 0-based index among visible options."""
    try:
        raw = await page.evaluate(_CLICK_OPTION_BY_INDEX_JS, index)
        result = json.loads(raw) if isinstance(raw, str) else raw
        return bool(result.get("clicked"))
    except Exception:
        return False


async def _click_dropdown_option(page: Any, text: str, *, field_id: str | None = None) -> dict[str, Any]:
    """Click a visible dropdown option by text, scoped to the current field when possible."""
    if str(field_id or "").strip():
        try:
            raw_target = await page.evaluate(
                _GET_SCOPED_DROPDOWN_OPTION_TARGET_JS,
                [str(field_id), text, synonym_groups_for_js()],
            )
            target = json.loads(raw_target) if isinstance(raw_target, str) else raw_target
        except Exception:
            target = {"found": False}
        if isinstance(target, dict) and target.get("found"):
            try:
                mouse = await page.mouse
                await mouse.move(float(target["x"]), float(target["y"]))
                await asyncio.sleep(0.05)
                await mouse.click(float(target["x"]), float(target["y"]))
                await asyncio.sleep(0.15)
                return {
                    "clicked": True,
                    "text": target.get("text", text),
                    "source": target.get("source", "scoped"),
                    "pass": target.get("pass"),
                    "via": "mouse_scoped",
                }
            except Exception:
                pass
            try:
                marked = await page.get_elements_by_css_selector('[data-domhand-option-target="true"]')
            except Exception:
                marked = []
            if marked:
                try:
                    await marked[0].click()
                    await asyncio.sleep(0.15)
                    return {
                        "clicked": True,
                        "text": target.get("text", text),
                        "source": target.get("source", "scoped"),
                        "pass": target.get("pass"),
                        "via": "actor_marked",
                    }
                except Exception:
                    pass
    try:
        raw_result = await page.evaluate(_CLICK_DROPDOWN_OPTION_JS, text, synonym_groups_for_js())
    except Exception:
        return {"clicked": False}
    return _parse_dropdown_click_result(raw_result)


async def _poll_click_dropdown_option(
    page: Any,
    *match_texts: str,
    field_id: str | None = None,
    max_wait_s: float = 2.85,
    interval_s: float = 0.18,
) -> dict[str, Any]:
    """Poll for visible ``role=option`` rows after typing — async lists (e.g. Greenhouse country).

    Append-only: does not change matching rules in ``_CLICK_DROPDOWN_OPTION_JS``; only retries
    until options appear or ``max_wait_s`` elapses.
    """
    texts = [m for m in match_texts if m and str(m).strip()]
    if not texts:
        return {"clicked": False}
    await asyncio.sleep(0.3)
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        for mt in texts:
            result = await _click_dropdown_option(page, mt, field_id=field_id)
            if result.get("clicked"):
                return result
        await asyncio.sleep(interval_s)
    return {"clicked": False}


async def _try_open_combobox_menu(page: Any, ff_id: str, *, tag: str) -> None:
    """Open react-select / combobox: prefer chevron / Toggle flyout; fall back to input click.

    Idempotent: if the menu is already open (``aria-expanded=true`` on the combobox),
    do **not** click the toggle again — a second click closes the menu (reads as
    "opened then immediately clicked away").
    """
    try:
        trusted = await trusted_open_combobox_by_ffid(page, ff_id)
        if trusted.get("already_open"):
            visible_options = await _scan_visible_dropdown_options(page, field_id=ff_id)
            if visible_options:
                logger.debug("domhand.combobox_open", field_id=ff_id, tag=tag, via="already_open")
                return
        if trusted.get("clicked"):
            logger.debug(
                "domhand.combobox_open",
                field_id=ff_id,
                tag=tag,
                via=f"trusted_{trusted.get('via') or 'click'}",
            )
            return
    except Exception:
        pass
    try:
        raw = await page.evaluate(
            r"""(ffId) => {
			var ff = window.__ff;
			var el = ff ? ff.byId(ffId) : null;
			if (!el) return JSON.stringify({open: false});
			var combo = el.closest('[role="combobox"]');
			if (!combo && el.getAttribute && el.getAttribute('role') === 'combobox') combo = el;
			if (!combo) {
				var inner = el.querySelector && el.querySelector('[role="combobox"]');
				if (inner) combo = inner;
			}
			if (!combo) return JSON.stringify({open: false});
			return JSON.stringify({open: combo.getAttribute('aria-expanded') === 'true'});
		}""",
            ff_id,
        )
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict) and parsed.get("open"):
            logger.debug("domhand.combobox_open", field_id=ff_id, tag=tag, via="already_expanded")
            return
    except Exception:
        pass
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


async def _clear_dropdown_search(page: Any, ff_id: str | None = None) -> None:
    """Clear the current searchable dropdown query if one is focused.

    When ``ff_id`` is set, focus that combobox first so shortcuts apply to the react-select input.
    """
    if ff_id:
        try:
            await page.evaluate(_FOCUS_FIELD_JS, ff_id)
            await asyncio.sleep(0.08)
        except Exception:
            pass
    for shortcut in ("Meta+A", "Control+A"):
        try:
            await _press_key_compat(page, shortcut)
        except Exception:
            pass
    try:
        await _press_key_compat(page, "Backspace")
    except Exception:
        pass
    await asyncio.sleep(0.15)


async def _settle_dropdown_selection(page: Any, delay: float = 0.55) -> None:
    """Let react-select finish committing, then close without a synthetic body click (race-prone)."""
    await asyncio.sleep(0.28)
    with contextlib.suppress(Exception):
        await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
    await asyncio.sleep(delay)


_READ_MULTI_SELECT_SELECTION_JS = r"""(ffId) => {
    const ff = window.__ff || null;
    const byId = (id) => {
        if (!id) return null;
        if (ff && ff.byId) return ff.byId(id);
        return document.querySelector('[data-ff-id="' + id + '"]') || document.getElementById(id);
    };
    const el = byId(ffId);
    const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const visible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const visibleText = (node) => {
        if (!node || !visible(node)) return '';
        const clone = node.cloneNode(true);
        clone.querySelectorAll('script,style,[aria-hidden="true"],[hidden],input,textarea,select').forEach((child) => child.remove());
        return clean(clone.textContent || '');
    };
    const isUnsetLike = (text) => {
        const value = clean(text).toLowerCase();
        return !value
            || value === 'search'
            || value === 'no items.'
            || value === 'no items'
            || value === '0 items selected'
            || value === 'select one';
    };
    if (!el) return JSON.stringify({tokens: [], count: 0, summary: ''});
    const wrapper =
        (ff && ff.closestCrossRoot && (
            ff.closestCrossRoot(el, '[data-automation-id="formField"]') ||
            ff.closestCrossRoot(el, '[data-automation-id*="formField"]') ||
            ff.closestCrossRoot(el, '[data-automation-id="multiselectInputContainer"]') ||
            ff.closestCrossRoot(el, '.input-field-container') ||
            ff.closestCrossRoot(el, '.cx-select-container') ||
            ff.closestCrossRoot(el, '.input-row__control-container') ||
            ff.closestCrossRoot(el, '.input-row')
        )) ||
        el.parentElement ||
        el;
    const selectors = [
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
    const tokens = [];
    const seen = new Set();
    const pushToken = (raw) => {
        const text = clean(raw);
        if (isUnsetLike(text)) return;
        const key = text.toLowerCase();
        if (seen.has(key)) return;
        seen.add(key);
        tokens.push(text);
    };
    for (const selector of selectors) {
        for (const node of wrapper.querySelectorAll(selector)) {
            pushToken(visibleText(node));
        }
    }
    const summary = visibleText(wrapper);
    const countMatch = summary.match(/\b(\d+)\s+items?\s+selected\b/i);
    const count = countMatch ? parseInt(countMatch[1], 10) : tokens.length;
    return JSON.stringify({tokens, count: Number.isFinite(count) ? count : tokens.length, summary});
}"""


_READ_WORKDAY_SKILL_WIDGET_JS = r"""(ffId) => {
    const ff = window.__ff || null;
    const byFfId = (id) => (ff && ff.byId) ? ff.byId(id) : document.querySelector('[data-ff-id="' + id + '"]');
    const scopedQuery = (root, sel) => {
        if (!root || !root.querySelector) return null;
        try { return root.querySelector(sel); } catch (e) { return null; }
    };
    const el = byFfId(ffId);
    if (!el) return JSON.stringify({ is_workday_skill: false, reason: 'missing_field' });
    const inputContainer = (ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="multiselectInputContainer"]')
        : (el.closest ? el.closest('[data-automation-id="multiselectInputContainer"]') : null);
    const wrapper = ((ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]')
        : (el.closest ? el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]') : null))
        || inputContainer
        || (el.parentElement || null);
    if (!wrapper) return JSON.stringify({ is_workday_skill: false, reason: 'missing_wrapper' });
    const effectiveContainer = inputContainer || wrapper;
    const promptButton = scopedQuery(effectiveContainer, '[data-automation-id="promptSearchButton"], [data-uxi-widget-type="selectinputicon"]')
        || scopedQuery(wrapper, '[data-automation-id="promptSearchButton"], [data-uxi-widget-type="selectinputicon"]');
    const promptPopup = scopedQuery(wrapper, '[data-automation-id="responsiveMonikerPrompt"], [data-automation-id*="responsiveMonikerPrompt"], [data-uxi-widget-type="prompt"]')
        || document.querySelector('[data-automation-id="responsiveMonikerPrompt"], [data-automation-id*="responsiveMonikerPrompt"], [data-uxi-widget-type="prompt"]');
    const selectedList = scopedQuery(wrapper, '[data-automation-id="selectedItemList"], [data-automation-id="selectedItems"]')
        || scopedQuery(effectiveContainer, '[data-automation-id="selectedItemList"], [data-automation-id="selectedItems"]');
    const isSelectInput =
        (el.getAttribute && el.getAttribute('data-uxi-widget-type') === 'selectinput') ||
        el.type === 'search' ||
        el.getAttribute('role') === 'combobox';
    return JSON.stringify({
        is_workday_skill: Boolean(wrapper && promptButton && isSelectInput),
        has_prompt_button: Boolean(promptButton),
        has_prompt_popup: Boolean(promptPopup),
        has_selected_list: Boolean(selectedList),
    });
}"""


_READ_WORKDAY_SKILL_INPUT_STATE_JS = r"""(ffId) => {
    const ff = window.__ff || null;
    const byFfId = (id) => (ff && ff.byId) ? ff.byId(id) : document.querySelector('[data-ff-id="' + id + '"]');
    const scopedQuery = (root, sel) => {
        if (!root || !root.querySelector) return null;
        try { return root.querySelector(sel); } catch (e) { return null; }
    };
    const visible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const el = byFfId(ffId);
    if (!el) return JSON.stringify({ found_input: false, input_value: '', has_clear_button: false });
    const wrapper = (ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]')
        : (el.closest ? el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]') : null);
    if (!wrapper) return JSON.stringify({ found_input: false, input_value: '', has_clear_button: false });
    const inputContainer = scopedQuery(wrapper, '[data-automation-id="multiselectInputContainer"]') || wrapper;
    const input = scopedQuery(inputContainer, '[data-uxi-widget-type="selectinput"], input[role="combobox"], input[type="search"], input[type="text"]');
    if (!input) return JSON.stringify({ found_input: false, input_value: '', has_clear_button: false });
    const clearButton = scopedQuery(
        inputContainer,
        '[data-automation-id="clearSearchButton"], [data-automation-id*="clearSearch"], [data-automation-id*="ClearSearch"], button[aria-label*="clear" i], [role="button"][aria-label*="clear" i], [title*="clear" i], [data-icon*="clear" i]'
    );
    return JSON.stringify({
        found_input: true,
        input_value: String(input.value || ''),
        has_clear_button: Boolean(clearButton && visible(clearButton)),
    });
}"""


_FOCUS_WORKDAY_SKILL_INPUT_JS = r"""(ffId) => {
    const ff = window.__ff || null;
    const byFfId = (id) => (ff && ff.byId) ? ff.byId(id) : document.querySelector('[data-ff-id="' + id + '"]');
    const scopedQuery = (root, sel) => {
        if (!root || !root.querySelector) return null;
        try { return root.querySelector(sel); } catch (e) { return null; }
    };
    const el = byFfId(ffId);
    if (!el) return JSON.stringify({ focused: false, reason: 'missing_field' });
    const wrapper = (ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]')
        : (el.closest ? el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]') : null);
    const inputContainer = scopedQuery(wrapper || el, '[data-automation-id="multiselectInputContainer"]') || wrapper || el;
    const input = scopedQuery(inputContainer, '[data-uxi-widget-type="selectinput"], input[role="combobox"], input[type="search"], input[type="text"]');
    if (!input) return JSON.stringify({ focused: false, reason: 'missing_input' });
    try {
        if (input.scrollIntoView) input.scrollIntoView({ block: 'center', inline: 'center' });
        if (input.focus) input.focus();
        if (typeof input.setSelectionRange === 'function') {
            const current = String(input.value || '');
            input.setSelectionRange(current.length, current.length);
        }
    } catch (e) {}
    return JSON.stringify({ focused: document.activeElement === input || input.matches(':focus') });
}"""


_SET_WORKDAY_SKILL_INPUT_VALUE_JS = r"""([ffId, value]) => {
    const ff = window.__ff || null;
    const byFfId = (id) => (ff && ff.byId) ? ff.byId(id) : document.querySelector('[data-ff-id="' + id + '"]');
    const scopedQuery = (root, sel) => {
        if (!root || !root.querySelector) return null;
        try { return root.querySelector(sel); } catch (e) { return null; }
    };
    const el = byFfId(ffId);
    if (!el) return JSON.stringify({ success: false, reason: 'missing_field' });
    const wrapper = (ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]')
        : (el.closest ? el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]') : null);
    const inputContainer = scopedQuery(wrapper || el, '[data-automation-id="multiselectInputContainer"]') || wrapper || el;
    const input = scopedQuery(inputContainer, '[data-uxi-widget-type="selectinput"], input[role="combobox"], input[type="search"], input[type="text"]');
    if (!input) return JSON.stringify({ success: false, reason: 'missing_input' });
    try {
        const setter =
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value') ||
            Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
        if (setter && setter.set) setter.set.call(input, String(value || ''));
        else input.value = String(value || '');
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
        input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Backspace', code: 'Backspace' }));
        return JSON.stringify({ success: true, value: String(input.value || '') });
    } catch (e) {
        return JSON.stringify({ success: false, reason: e && e.message ? e.message : 'unknown' });
    }
}"""


_GET_WORKDAY_SKILL_PROMPT_TARGET_JS = r"""(ffId) => {
    const ff = window.__ff || null;
    const byFfId = (id) => (ff && ff.byId) ? ff.byId(id) : document.querySelector('[data-ff-id="' + id + '"]');
    const globalQueryAll = (sel) => (ff && ff.queryAll) ? ff.queryAll(sel) : Array.from(document.querySelectorAll(sel));
    const scopedQuery = (root, sel) => {
        if (!root || !root.querySelector) return null;
        try { return root.querySelector(sel); } catch (e) { return null; }
    };
    const visible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const clearMarks = () => {
        for (const node of globalQueryAll('[data-domhand-workday-skill-prompt-target="true"]')) {
            node.removeAttribute('data-domhand-workday-skill-prompt-target');
        }
    };
    const el = byFfId(ffId);
    if (!el) return JSON.stringify({ found: false, reason: 'missing_field' });
    const wrapper = (ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]')
        : (el.closest ? el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]') : null);
    const button = scopedQuery(wrapper, '[data-automation-id="promptSearchButton"], [data-uxi-widget-type="selectinputicon"]');
    if (!visible(button)) return JSON.stringify({ found: false, reason: 'missing_prompt_button' });
    clearMarks();
    button.setAttribute('data-domhand-workday-skill-prompt-target', 'true');
    const rect = button.getBoundingClientRect();
    return JSON.stringify({
        found: true,
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
    });
}"""


_GET_WORKDAY_SKILL_CLEAR_TARGET_JS = r"""(ffId) => {
    const ff = window.__ff || null;
    const byFfId = (id) => (ff && ff.byId) ? ff.byId(id) : document.querySelector('[data-ff-id="' + id + '"]');
    const globalQueryAll = (sel) => (ff && ff.queryAll) ? ff.queryAll(sel) : Array.from(document.querySelectorAll(sel));
    const scopedQuery = (root, sel) => {
        if (!root || !root.querySelector) return null;
        try { return root.querySelector(sel); } catch (e) { return null; }
    };
    const visible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const clearMarks = () => {
        for (const node of globalQueryAll('[data-domhand-workday-skill-clear-target="true"]')) {
            node.removeAttribute('data-domhand-workday-skill-clear-target');
        }
    };
    const el = byFfId(ffId);
    if (!el) return JSON.stringify({ found: false, reason: 'missing_field' });
    const wrapper = (ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]')
        : (el.closest ? el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]') : null);
    if (!wrapper) return JSON.stringify({ found: false, reason: 'missing_wrapper' });
    const inputContainer = scopedQuery(wrapper, '[data-automation-id="multiselectInputContainer"]') || wrapper;
    const button = scopedQuery(
        inputContainer,
        '[data-automation-id="clearSearchButton"], [data-automation-id*="clearSearch"], [data-automation-id*="ClearSearch"], button[aria-label*="clear" i], [role="button"][aria-label*="clear" i], [title*="clear" i], [data-icon*="clear" i]'
    );
    if (!visible(button)) return JSON.stringify({ found: false, reason: 'missing_clear_button' });
    clearMarks();
    button.setAttribute('data-domhand-workday-skill-clear-target', 'true');
    const rect = button.getBoundingClientRect();
    return JSON.stringify({
        found: true,
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
    });
}"""


_GET_WORKDAY_SKILL_OPTION_TARGET_JS = r"""([ffId, targetText, synonymGroups]) => {
    const ff = window.__ff || null;
    const byFfId = (id) => (ff && ff.byId) ? ff.byId(id) : document.querySelector('[data-ff-id="' + id + '"]');
    const globalQueryAll = (sel) => (ff && ff.queryAll) ? ff.queryAll(sel) : Array.from(document.querySelectorAll(sel));
    const scopedQueryAll = (root, sel) => {
        if (!root || !root.querySelectorAll) return [];
        try { return Array.from(root.querySelectorAll(sel)); } catch (e) { return []; }
    };
    const visible = (node) => {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const normalize = (text) => (text || '').replace(/\s+/g, ' ').trim();
    const stopWords = { the: 1, a: 1, an: 1, of: 1, for: 1, in: 1, to: 1, and: 1, or: 1 };
    const lowerTarget = normalize(targetText).toLowerCase();
    const clearMarks = () => {
        for (const node of globalQueryAll('[data-domhand-workday-skill-option-target="true"]')) {
            node.removeAttribute('data-domhand-workday-skill-option-target');
        }
    };
    const el = byFfId(ffId);
    if (!el || !lowerTarget) return JSON.stringify({ found: false, reason: 'missing_field_or_target' });
    const inputContainer = (ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="multiselectInputContainer"]')
        : (el.closest ? el.closest('[data-automation-id="multiselectInputContainer"]') : null);
    const wrapper = ((ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]')
        : (el.closest ? el.closest('[data-automation-id="multiSelectContainer"], [data-automation-id*="multiSelectContainer"], [data-uxi-widget-type="multiselect"]') : null))
        || inputContainer
        || (el.parentElement || null);
    if (!wrapper) return JSON.stringify({ found: false, reason: 'missing_wrapper' });
    const popupCandidates = [];
    const pushPopup = (node) => {
        if (!node || !visible(node)) return;
        if (popupCandidates.includes(node)) return;
        popupCandidates.push(node);
    };
    pushPopup(wrapper.querySelector('[data-automation-id="responsiveMonikerPrompt"], [data-automation-id*="responsiveMonikerPrompt"], [data-uxi-widget-type="prompt"]'));
    if (inputContainer) {
        pushPopup(inputContainer.querySelector('[data-automation-id="responsiveMonikerPrompt"], [data-automation-id*="responsiveMonikerPrompt"], [data-uxi-widget-type="prompt"]'));
    }
    for (const popup of globalQueryAll('[data-automation-id="responsiveMonikerPrompt"], [data-automation-id*="responsiveMonikerPrompt"], [data-uxi-widget-type="prompt"]')) {
        pushPopup(popup);
    }
    if (!popupCandidates.length) return JSON.stringify({ found: false, reason: 'no_prompt_popup' });
    const options = [];
    for (const popup of popupCandidates) {
        const optionNodes = scopedQueryAll(
            popup,
            '[role="option"], [data-automation-id*="promptOption"], [data-automation-id*="menuItem"]'
        );
        for (const node of optionNodes) {
            if (!visible(node)) continue;
            if (node.closest && node.closest('[data-automation-id="selectedItemList"], [data-automation-id="selectedItems"]')) continue;
            const text = normalize(node.textContent || node.getAttribute('aria-label') || node.getAttribute('title') || '');
            if (!text || text.toLowerCase() === 'no items.' || text.toLowerCase() === 'no items') continue;
            const rect = node.getBoundingClientRect();
            options.push({
                node,
                text,
                lower: text.toLowerCase(),
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
            });
        }
    }
    if (!options.length) return JSON.stringify({ found: false, reason: 'no_options' });
    const finalizeTarget = (opt, pass) => {
        try {
            if (opt.node && opt.node.scrollIntoView) {
                opt.node.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            }
        } catch (e) {}
        clearMarks();
        opt.node.setAttribute('data-domhand-workday-skill-option-target', 'true');
        const rect = opt.node.getBoundingClientRect();
        return JSON.stringify({
            found: true,
            text: opt.text,
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2,
            pass,
        });
    };
    const canonicalSkill = (value) => normalize(String(value || '').replace(/\s*\([^)]*\)\s*$/, ''));
    const canonicalTarget = canonicalSkill(lowerTarget);
    const exact = options.find((opt) => opt.lower === lowerTarget);
    if (exact) return finalizeTarget(exact, 1);
    if (canonicalTarget) {
        const canonicalExact = options.find((opt) => canonicalSkill(opt.text) === canonicalTarget);
        if (canonicalExact) return finalizeTarget(canonicalExact, 2);
    }
    return JSON.stringify({
        found: false,
        reason: 'no_match',
        available: options.slice(0, 20).map((opt) => opt.text),
    });
}"""


async def _read_multi_select_selection(page: Any, field_id: str) -> dict[str, Any]:
    try:
        raw = await page.evaluate(_READ_MULTI_SELECT_SELECTION_JS, field_id)
    except Exception:
        return {"tokens": [], "count": 0, "summary": ""}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {"tokens": [], "count": 0, "summary": ""}
    if not isinstance(parsed, dict):
        return {"tokens": [], "count": 0, "summary": ""}
    tokens = [str(token).strip() for token in parsed.get("tokens", []) if str(token).strip()]
    count = parsed.get("count", len(tokens))
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = len(tokens)
    return {
        "tokens": tokens,
        "count": max(count, len(tokens)),
        "summary": str(parsed.get("summary") or "").strip(),
    }


async def _wait_for_multi_select_commit(
    page: Any,
    field: FormField,
    expected: str,
    *,
    previous_selection: dict[str, Any],
    matched_label: str | None = None,
    timeout: float = 1.8,
    poll_interval: float = 0.18,
) -> dict[str, Any]:
    previous_tokens = {
        str(token).strip().lower() for token in previous_selection.get("tokens", []) if str(token).strip()
    }
    previous_count = int(previous_selection.get("count", 0) or 0)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_selection: dict[str, Any] = {
        "tokens": list(previous_selection.get("tokens", [])),
        "count": previous_count,
        "summary": str(previous_selection.get("summary") or "").strip(),
    }
    while True:
        current = await _read_multi_select_selection(page, field.field_id)
        last_selection = current
        tokens = [str(token).strip() for token in current.get("tokens", []) if str(token).strip()]
        for token in tokens:
            if _field_value_matches_expected(token, expected, matched_label=matched_label):
                return {**current, "committed": True, "via": "token_match"}
        if int(current.get("count", 0) or 0) > previous_count:
            return {**current, "committed": True, "via": "count_increase"}
        if any(token.lower() not in previous_tokens for token in tokens):
            return {**current, "committed": True, "via": "new_token"}
        if loop.time() >= deadline:
            return {**last_selection, "committed": False}
        await asyncio.sleep(poll_interval)


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
            await _type_text_compat(page, term, delay=55)
            await asyncio.sleep(0.4)
            clicked = await _poll_click_dropdown_option(page, value, term, max_wait_s=2.85)
            if clicked.get("clicked"):
                logger.debug(f'select {tag} -> "{clicked.get("text", value)}" (typed search)')
                return clicked
            await _press_key_compat(page, "Enter")
            await asyncio.sleep(0.45)
            clicked = await _poll_click_dropdown_option(page, value, term, max_wait_s=1.2)
            if clicked.get("clicked"):
                logger.debug(f'select {tag} -> "{clicked.get("text", value)}" (typed search, enter)')
                return clicked
        except Exception as e:
            logger.debug(f'dropdown search {tag} term "{term}" failed: {str(e)[:60]}')
    return {"clicked": False}


_SCOPED_DROPDOWN_OPTIONS_JS = r"""(ffId) => {
    const ff = window.__ff || null;
    const byFfId = (id) => (ff && ff.byId) ? ff.byId(id) : document.querySelector('[data-ff-id="' + id + '"]');
    const getByDomId = (id) => {
        if (!id) return null;
        if (ff && ff.getByDomId) return ff.getByDomId(id);
        return document.getElementById(id);
    };
    const globalQueryAll = (sel) => (ff && ff.queryAll) ? ff.queryAll(sel) : Array.from(document.querySelectorAll(sel));
    const scopedQueryAll = (root, sel) => {
        if (!root || !root.querySelectorAll) return [];
        try { return Array.from(root.querySelectorAll(sel)); } catch (e) { return []; }
    };
    const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const normalize = (text) => (text || '').replace(/\s+/g, ' ').trim();
    const collectIds = (node, attr) => {
        if (!node || !node.getAttribute) return [];
        return String(node.getAttribute(attr) || '').split(/\s+/).map((part) => part.trim()).filter(Boolean);
    };
    const el = byFfId(ffId);
    if (!el) return JSON.stringify([]);
    const optionSelectors = '[role="option"], [role="gridcell"], [role="menuitem"], [role="listitem"], li, [class*="option"], [data-value]';
    const popupSelectors = '[role="listbox"], [role="menu"], [role="grid"], [data-automation-id="activeListContainer"], [class*="dropdown-menu"], [class*="options-list"], [class*="listbox"], [class*="menu"]';
    const fieldRect = el.getBoundingClientRect();
    const fieldCenterX = fieldRect.left + fieldRect.width / 2;
    const fieldCenterY = fieldRect.top + fieldRect.height / 2;
    const scorePopup = (popup, boost) => {
        const rect = popup.getBoundingClientRect();
        let score = boost || 0;
        const centerX = rect.left + rect.width / 2;
        const centerY = rect.top + rect.height / 2;
        score -= Math.abs(centerX - fieldCenterX) * 0.04;
        score -= Math.abs(centerY - fieldCenterY) * 0.02;
        if (rect.top >= fieldRect.top - 12) score += 25;
        if (rect.left <= fieldRect.right + 12 && rect.right >= fieldRect.left - 12) score += 15;
        if (popup.contains(el)) score += 5;
        return score;
    };
    const optionData = (popup) => {
        const out = [];
        const isSelectedToken = (node) => {
            if (!node || !node.closest) return false;
            return Boolean(
                node.closest(
                    '[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"], [data-automation-id="selectedItems"], [data-automation-id*="selectedItem"], [class*="selectedItem"], [class*="SelectedItem"], [class*="token"], [class*="Token"], [class*="chip"], [class*="Chip"], [class*="pill"], [class*="Pill"]'
                )
            );
        };
        for (const opt of scopedQueryAll(popup, optionSelectors)) {
            if (!visible(opt)) continue;
            if (isSelectedToken(opt)) continue;
            const text = normalize(opt.textContent || opt.getAttribute('aria-label') || '');
            if (!text) continue;
            out.push({ text, lower: text.toLowerCase() });
        }
        return out;
    };
    const popupCandidates = [];
    const seenPopups = new Set();
    const addPopup = (popup, boost) => {
        if (!popup || !visible(popup) || seenPopups.has(popup)) return;
        const opts = optionData(popup);
        if (!opts.length) return;
        seenPopups.add(popup);
        popupCandidates.push({ score: scorePopup(popup, boost), opts });
    };
    const combo = (el.closest && el.closest('[role="combobox"]')) || (el.getAttribute && el.getAttribute('role') === 'combobox' ? el : null);
    const wrapper = (ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="formField"], [data-automation-id*="formField"], fieldset, .form-group, .field, [role="group"]')
        : (el.closest ? el.closest('[data-automation-id="formField"], [data-automation-id*="formField"], fieldset, .form-group, .field, [role="group"]') : null);
    const root = el.getRootNode ? el.getRootNode() : document;
    const controlledIds = [
        ...collectIds(el, 'aria-controls'),
        ...collectIds(el, 'aria-owns'),
        ...collectIds(combo, 'aria-controls'),
        ...collectIds(combo, 'aria-owns'),
    ];
    for (const id of controlledIds) addPopup(getByDomId(id), 1000);
    if (wrapper) {
        for (const popup of scopedQueryAll(wrapper, popupSelectors)) addPopup(popup, 250);
    }
    if (root && root !== document) {
        for (const popup of scopedQueryAll(root, popupSelectors)) addPopup(popup, 320);
    }
    for (const popup of globalQueryAll(popupSelectors)) addPopup(popup, 0);
    popupCandidates.sort((a, b) => b.score - a.score);
    if (!popupCandidates.length) return JSON.stringify([]);
    const seenText = new Set();
    const out = [];
    for (const opt of popupCandidates[0].opts) {
        if (seenText.has(opt.lower)) continue;
        seenText.add(opt.lower);
        out.push(opt.text);
    }
    return JSON.stringify(out);
}"""


_GET_SCOPED_DROPDOWN_OPTION_TARGET_JS = r"""([ffId, targetText, synonymGroups]) => {
    const ff = window.__ff || null;
    const byFfId = (id) => (ff && ff.byId) ? ff.byId(id) : document.querySelector('[data-ff-id="' + id + '"]');
    const getByDomId = (id) => {
        if (!id) return null;
        if (ff && ff.getByDomId) return ff.getByDomId(id);
        return document.getElementById(id);
    };
    const globalQueryAll = (sel) => (ff && ff.queryAll) ? ff.queryAll(sel) : Array.from(document.querySelectorAll(sel));
    const scopedQueryAll = (root, sel) => {
        if (!root || !root.querySelectorAll) return [];
        try { return Array.from(root.querySelectorAll(sel)); } catch (e) { return []; }
    };
    const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const normalize = (text) => (text || '').replace(/\s+/g, ' ').trim();
    const collectIds = (node, attr) => {
        if (!node || !node.getAttribute) return [];
        return String(node.getAttribute(attr) || '').split(/\s+/).map((part) => part.trim()).filter(Boolean);
    };
    const stopWords = { the: 1, a: 1, an: 1, of: 1, for: 1, in: 1, to: 1, and: 1, or: 1 };
    const lowerTarget = normalize(targetText).toLowerCase();
    const el = byFfId(ffId);
    if (!el || !lowerTarget) return JSON.stringify({ found: false, reason: 'missing_field_or_target' });
    const optionSelectors = '[role="option"], [role="gridcell"], [role="menuitem"], [role="listitem"], li, [class*="option"], [data-value]';
    const popupSelectors = '[role="listbox"], [role="menu"], [role="grid"], [data-automation-id="activeListContainer"], [class*="dropdown-menu"], [class*="options-list"], [class*="listbox"], [class*="menu"]';
    const fieldRect = el.getBoundingClientRect();
    const fieldCenterX = fieldRect.left + fieldRect.width / 2;
    const fieldCenterY = fieldRect.top + fieldRect.height / 2;
    const scorePopup = (popup, boost) => {
        const rect = popup.getBoundingClientRect();
        let score = boost || 0;
        const centerX = rect.left + rect.width / 2;
        const centerY = rect.top + rect.height / 2;
        score -= Math.abs(centerX - fieldCenterX) * 0.04;
        score -= Math.abs(centerY - fieldCenterY) * 0.02;
        if (rect.top >= fieldRect.top - 12) score += 25;
        if (rect.left <= fieldRect.right + 12 && rect.right >= fieldRect.left - 12) score += 15;
        if (popup.contains(el)) score += 5;
        return score;
    };
    const optionData = (popup) => {
        const out = [];
        const isSelectedToken = (node) => {
            if (!node || !node.closest) return false;
            return Boolean(
                node.closest(
                    '[data-automation-id="selectedItem"], [data-automation-id="selectedItemList"], [data-automation-id="selectedItems"], [data-automation-id*="selectedItem"], [class*="selectedItem"], [class*="SelectedItem"], [class*="token"], [class*="Token"], [class*="chip"], [class*="Chip"], [class*="pill"], [class*="Pill"]'
                )
            );
        };
        for (const opt of scopedQueryAll(popup, optionSelectors)) {
            if (!visible(opt)) continue;
            if (isSelectedToken(opt)) continue;
            const text = normalize(opt.textContent || opt.getAttribute('aria-label') || '');
            if (!text) continue;
            const rect = opt.getBoundingClientRect();
            out.push({
                node: opt,
                text,
                lower: text.toLowerCase(),
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
            });
        }
        return out;
    };
    const markTarget = (opt) => {
        for (const candidate of globalQueryAll('[data-domhand-option-target="true"]')) {
            candidate.removeAttribute('data-domhand-option-target');
        }
        if (opt && opt.node && opt.node.setAttribute) {
            opt.node.setAttribute('data-domhand-option-target', 'true');
        }
    };
    const popupCandidates = [];
    const seenPopups = new Set();
    const addPopup = (popup, boost, source) => {
        if (!popup || !visible(popup) || seenPopups.has(popup)) return;
        const opts = optionData(popup);
        if (!opts.length) return;
        seenPopups.add(popup);
        popupCandidates.push({ score: scorePopup(popup, boost), opts, source });
    };
    const combo = (el.closest && el.closest('[role="combobox"]')) || (el.getAttribute && el.getAttribute('role') === 'combobox' ? el : null);
    const wrapper = (ff && ff.closestCrossRoot)
        ? ff.closestCrossRoot(el, '[data-automation-id="formField"], [data-automation-id*="formField"], fieldset, .form-group, .field, [role="group"]')
        : (el.closest ? el.closest('[data-automation-id="formField"], [data-automation-id*="formField"], fieldset, .form-group, .field, [role="group"]') : null);
    const root = el.getRootNode ? el.getRootNode() : document;
    const controlledIds = [
        ...collectIds(el, 'aria-controls'),
        ...collectIds(el, 'aria-owns'),
        ...collectIds(combo, 'aria-controls'),
        ...collectIds(combo, 'aria-owns'),
    ];
    for (const id of controlledIds) addPopup(getByDomId(id), 1000, 'controlled:' + id);
    if (wrapper) {
        for (const popup of scopedQueryAll(wrapper, popupSelectors)) addPopup(popup, 250, 'wrapper');
    }
    if (root && root !== document) {
        for (const popup of scopedQueryAll(root, popupSelectors)) addPopup(popup, 320, 'same_root');
    }
    for (const popup of globalQueryAll(popupSelectors)) addPopup(popup, 0, 'global');
    popupCandidates.sort((a, b) => b.score - a.score);
    if (!popupCandidates.length) return JSON.stringify({ found: false, reason: 'no_popup' });
    const opts = popupCandidates[0].opts;
    const finalizeTarget = (opt, pass) => {
        try {
            if (opt.node && opt.node.scrollIntoView) {
                opt.node.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            }
        } catch (e) {}
        const rect = opt.node && opt.node.getBoundingClientRect ? opt.node.getBoundingClientRect() : null;
        const x = rect ? rect.left + rect.width / 2 : opt.x;
        const y = rect ? rect.top + rect.height / 2 : opt.y;
        markTarget(opt);
        return JSON.stringify({
            found: true,
            text: opt.text,
            x,
            y,
            source: popupCandidates[0].source,
            pass,
        });
    };
    const exact = opts.find((opt) => opt.lower === lowerTarget);
    if (exact) {
        return finalizeTarget(exact, 1);
    }
    const prefix = opts.find((opt) => opt.lower.startsWith(lowerTarget) || lowerTarget.startsWith(opt.lower));
    if (prefix) {
        return finalizeTarget(prefix, 2);
    }
    const partial = opts.find((opt) => lowerTarget.includes(opt.lower) || opt.lower.includes(lowerTarget));
    if (partial) {
        return finalizeTarget(partial, 3);
    }
    if (Array.isArray(synonymGroups) && synonymGroups.length > 0) {
        let targetGroup = null;
        for (const group of synonymGroups) {
            if (Array.isArray(group) && group.includes(lowerTarget)) {
                targetGroup = group;
                break;
            }
        }
        if (targetGroup) {
            const synonymMatch = opts.find((opt) => targetGroup.includes(opt.lower));
            if (synonymMatch) {
                return finalizeTarget(synonymMatch, 4);
            }
        }
    }
    const targetWords = lowerTarget.split(/\s+/).filter((word) => word.length > 1 && !stopWords[word]);
    if (targetWords.length > 0) {
        let best = null;
        let bestScore = 0;
        for (const opt of opts) {
            const words = opt.lower.split(/\s+/).filter((word) => word.length > 1 && !stopWords[word]);
            let score = 0;
            for (const word of targetWords) {
                if (words.includes(word)) score += 1;
            }
            if (score > bestScore) {
                bestScore = score;
                best = opt;
            }
        }
        if (best && bestScore >= 1) {
            return finalizeTarget(best, 5);
        }
    }
    return JSON.stringify({ found: false, reason: 'no_match', source: popupCandidates[0].source, available: opts.slice(0, 20).map((opt) => opt.text) });
}"""


async def _scan_visible_dropdown_options(page: Any, *, field_id: str | None = None) -> list[str]:
    """Read visible dropdown option labels from the field's active popup when possible."""
    if str(field_id or "").strip():
        try:
            raw = await page.evaluate(_SCOPED_DROPDOWN_OPTIONS_JS, str(field_id))
            if isinstance(raw, str):
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, list) else []
            return raw if isinstance(raw, list) else []
        except Exception:
            pass
    try:
        raw = await page.evaluate(SCAN_VISIBLE_OPTIONS_JS)
        if isinstance(raw, str):
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


async def _fill_custom_dropdown_outcome(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
    *,
    browser_session: BrowserSession | None = None,
) -> FieldFillOutcome:
    if browser_session is not None:
        try:
            cdp_result = await _fill_custom_dropdown_cdp_first(browser_session, page, field, value, tag)
            if cdp_result:
                return cdp_result
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

    async def _scan() -> list[str]:
        return await _scan_visible_dropdown_options(page, field_id=ff_id)

    async def _type(text: str) -> None:
        try:
            await page.evaluate(_FOCUS_FIELD_JS, ff_id)
            await asyncio.sleep(0.18)
        except Exception:
            pass
        await _type_text_compat(page, text, delay=55)

    async def _clear() -> None:
        await _clear_dropdown_search(page, ff_id)

    async def _settle() -> None:
        await _settle_dropdown_selection(page)

    async def _dismiss() -> None:
        try:
            await page.evaluate(_DISMISS_DROPDOWN_SOFT_JS)
        except Exception:
            pass

    async def _click_option(text: str) -> dict[str, Any]:
        return await _click_dropdown_option(page, text, field_id=ff_id)

    result = await fill_interactive_dropdown(
        page,
        value,
        open_fn=_open,
        read_value_fn=_read,
        scan_options_fn=_scan,
        settle_fn=_settle,
        dismiss_fn=_dismiss,
        type_fn=_type,
        clear_fn=_clear,
        click_option_fn=_click_option,
        tag=f"custom-select {tag}",
    )
    if result.success and await _field_has_validation_error(page, field.field_id):
        result = FieldFillOutcome(success=False, matched_label=result.matched_label)

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
    return _fill_outcome(result.success, matched_label=result.matched_label)


async def _fill_custom_dropdown(
    page: Any,
    field: FormField,
    value: str,
    tag: str,
    *,
    browser_session: BrowserSession | None = None,
) -> bool:
    return (
        await _fill_custom_dropdown_outcome(
            page,
            field,
            value,
            tag,
            browser_session=browser_session,
        )
    ).success


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
            current = await _poll_group_selection(page, field.field_id, choice, max_wait=0.8)
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
            current = await _poll_group_selection(page, field.field_id, choice, max_wait=0.8)
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
    from ghosthands.actions.domhand_fill import _is_upload_like_field

    if _is_upload_like_field(field):
        logger.debug(f"skip {tag} (upload-like button-group; requires domhand_upload)")
        return False
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
            current = await _poll_group_selection(page, field.field_id, choice, max_wait=0.8)
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
            current = await _poll_group_selection(page, field.field_id, choice, max_wait=0.8)
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
