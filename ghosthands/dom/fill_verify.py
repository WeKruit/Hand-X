"""Observable verification, retry caps, and failure tracking for form fills.

Dependencies that still live in ``domhand_fill`` or sibling ``dom.*`` modules are
accessed via late imports to avoid circular references.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import structlog

from browser_use.browser import BrowserSession
from ghosthands.actions.views import FormField, get_stable_field_key
from ghosthands.cost_summary import mark_stagehand_usage
from ghosthands.dom.fill_browser_scripts import _COLLAPSE_COMBOBOX_FOR_FF_JS
from ghosthands.runtime_learning import (
    DOMHAND_RETRY_CAP,
    clear_domhand_failure,
    get_domhand_failure_count,
    is_domhand_retry_capped,
    record_domhand_failure,
    record_expected_field_value,
)

logger = structlog.get_logger(__name__)

DOMHAND_RETRY_CAPPED = "domhand_retry_capped"

# Fill executor reported success but DOM readback never matched the profile string within the
# observable poll window (common on Oracle cx-select / address LOVs). We still count the fill as
# successful so dom_failure_count does not drive the agent; browser-use vision should confirm UI.
FILL_CONFIDENCE_FILLED_READBACK_UNVERIFIED = 0.55


def is_fill_readback_unverified_confidence(fill_confidence: float | None) -> bool:
    """True when ``_attempt_domhand_fill_with_retry_cap`` trusted executor success without readback match."""
    return abs(float(fill_confidence or 0.0) - FILL_CONFIDENCE_FILLED_READBACK_UNVERIFIED) < 1e-6


# ── Late-import delegates ────────────────────────────────────────────────


def _preferred_field_label(field: FormField) -> str:
    from ghosthands.dom.fill_label_match import _preferred_field_label as _impl

    return _impl(field)


def _fill_single_field(page: Any, field: FormField, value: str, *, browser_session: BrowserSession | None = None):
    from ghosthands.dom.fill_executor import _fill_single_field as _impl

    return _impl(page, field, value, browser_session=browser_session)


def _fill_single_field_outcome(
    page: Any,
    field: FormField,
    value: str,
    *,
    browser_session: BrowserSession | None = None,
):
    from ghosthands.dom.fill_executor import _fill_single_field_outcome as _impl

    return _impl(page, field, value, browser_session=browser_session)


def _checkbox_group_is_exclusive_choice(field: FormField) -> bool:
    from ghosthands.dom.fill_executor import _checkbox_group_is_exclusive_choice as _impl

    return _impl(field)


def _read_binary_state(page: Any, field_id: str):
    from ghosthands.dom.fill_executor import _read_binary_state as _impl

    return _impl(page, field_id)


def _read_group_selection(page: Any, field_id: str):
    from ghosthands.dom.fill_executor import _read_group_selection as _impl

    return _impl(page, field_id)


def _read_multi_select_selection(page: Any, field_id: str):
    from ghosthands.dom.fill_executor import _read_multi_select_selection as _impl

    return _impl(page, field_id)


def _field_has_validation_error(page: Any, field_id: str):
    from ghosthands.dom.fill_executor import _field_has_validation_error as _impl

    return _impl(page, field_id)


def _read_field_value_for_field(page: Any, field: FormField):
    from ghosthands.actions.domhand_fill import _read_field_value_for_field as _impl

    return _impl(page, field)


def _field_value_matches_expected(
    current: str,
    expected: str,
    matched_label: str | None = None,
) -> bool:
    from ghosthands.actions.domhand_fill import _field_value_matches_expected as _impl

    return _impl(current, expected, matched_label=matched_label)


def _is_explicit_false(val: str | None) -> bool:
    from ghosthands.dom.fill_llm_answers import _is_explicit_false as _impl

    return _impl(val)


async def _uses_multi_select_observation(page: Any, field: FormField) -> bool:
    """Mirror executor routing when deciding how to observe a settled field.

    Oracle searchable comboboxes can be labeled like a skill field, but they are
    still single-select inputs. Observable verification must only use token-based
    multi-select reads for actual multi-select widgets.
    """
    if field.field_type != "select":
        return False
    if field.is_multi_select:
        return True
    try:
        from ghosthands.dom.fill_executor import _uses_workday_skill_multiselect

        return await _uses_workday_skill_multiselect(page, field)
    except Exception:
        return False


async def _is_workday_prompt_search_widget(page: Any, field: FormField) -> bool:
    if field.field_type != "select":
        return False
    if await _uses_multi_select_observation(page, field):
        return True
    signal = " ".join(
        part
        for part in (
            getattr(field, "name", "") or "",
            getattr(field, "raw_label", "") or "",
            getattr(field, "section", "") or "",
            getattr(field, "placeholder", "") or "",
        )
        if part
    ).lower()
    prompt_search_markers = (
        "school or university",
        "field of study",
        "latest employer",
        "employer",
        "language",
        "how did you hear",
        "referral",
        "source",
    )
    return any(marker in signal for marker in prompt_search_markers)


# ── Retry identity helpers ───────────────────────────────────────────────


def _domhand_retry_field_identity(field: FormField) -> str:
    return get_stable_field_key(field)


def _domhand_retry_message(field: FormField) -> str:
    return (
        f'"{_preferred_field_label(field)}" hit the DomHand retry cap after '
        f"{DOMHAND_RETRY_CAP} failed attempts. Use browser-use or one screenshot/vision fallback."
    )


# ── Skill widget verification helpers ───────────────────────────────────


def _normalize_skill_list(raw_value: str | None) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for part in str(raw_value or "").split(","):
        item = part.strip()
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(item)
        if len(values) >= 15:
            break
    return values


async def _skill_select_matches_observed_tokens(page: Any, field: FormField, desired_value: str) -> bool:
    selection = await _read_multi_select_selection(page, field.field_id)
    observed_tokens = [
        str(token or "").strip() for token in (selection.get("tokens") or []) if str(token or "").strip()
    ]
    if not observed_tokens:
        return False
    desired_tokens = _normalize_skill_list(desired_value)
    if not desired_tokens:
        return False
    for observed in observed_tokens:
        if not any(_field_value_matches_expected(observed, desired) for desired in desired_tokens):
            return False
    return True


# ── LLM escalation helpers ───────────────────────────────────────────────


async def _llm_verify_if_available(page: Any, field: FormField, desired_value: str) -> bool | None:
    """Try LLM-based screenshot verification.  Returns True/False/None."""
    try:
        from ghosthands.dom.fill_llm_escalation import llm_verify_field_value

        return await llm_verify_field_value(page, field, desired_value)
    except Exception as exc:
        logger.debug("llm_escalation.verify_unavailable", error=str(exc))
        return None


async def _stagehand_escalate_fill(
    page: Any,
    field: FormField,
    desired_value: str,
    browser_session: BrowserSession | None = None,
) -> bool:
    """Delegate to the optional fill-escalation callback on the session.

    Stagehand start is gated in layer.py — if no desktop proxy or Browserbase
    key, ``ensure_stagehand_for_session`` returns immediately (no SEA spawn).
    """
    try:
        from ghosthands.stagehand.compat import ensure_stagehand_for_session

        if browser_session is None:
            return False

        mark_stagehand_usage(browser_session, source="domhand_fill_escalation")
        layer = await ensure_stagehand_for_session(browser_session)
        if not layer.is_available:
            return False

        label = field.name or field.raw_label or field.field_type
        if field.field_type in ("select", "radio-group", "radio", "button-group"):
            instruction = f"Select '{desired_value}' in the '{label}' dropdown or field"
        else:
            instruction = f"Fill the '{label}' field with '{desired_value}'"

        result = await layer.act(instruction)
        if not result.success:
            logger.debug(
                "stagehand.escalation.act_failed",
                field_label=label,
                message=result.message[:120],
            )
            return False

        await asyncio.sleep(0.95)
        with contextlib.suppress(Exception):
            await page.evaluate(_COLLAPSE_COMBOBOX_FOR_FF_JS, field.field_id)
        await asyncio.sleep(0.35)
        verified = await _verify_fill_observable(
            page,
            field,
            desired_value,
            timeout_s=6.0 if field.field_type == "select" else None,
            poll_interval_s=0.22 if field.field_type == "select" else None,
        )
        logger.info(
            "stagehand.escalation.result",
            field_label=label,
            desired_preview=str(desired_value)[:80],
            verified=verified,
        )
        return verified

    except Exception as exc:
        logger.debug("stagehand.escalation.unavailable", error=str(exc))
        return False


async def _llm_escalate_fill(page: Any, field: FormField, desired_value: str) -> bool:
    """Try LLM-guided fill when DOM-first methods failed.  Returns True if rescued."""
    try:
        from ghosthands.dom.fill_llm_escalation import (
            llm_execute_fill_suggestion,
            llm_suggest_fill_action,
        )

        suggestion = await llm_suggest_fill_action(page, field, desired_value)
        if not suggestion:
            return False
        executed = await llm_execute_fill_suggestion(page, field, desired_value, suggestion)
        if not executed:
            return False
        return await _verify_fill_observable(page, field, desired_value)
    except Exception as exc:
        logger.debug("llm_escalation.fill_unavailable", error=str(exc))
        return False


# ── Extracted functions ──────────────────────────────────────────────────


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


async def _verify_fill_observable(
    page: Any,
    field: FormField,
    desired_value: str,
    *,
    matched_label: str | None = None,
    timeout_s: float | None = None,
    poll_interval_s: float | None = None,
) -> bool:
    """Poll until the filled value is readable in the DOM with no validation error.

    Per-field fill helpers can return True when the last action succeeded, but react-select
    and similar widgets may commit asynchronously. This gate aligns ``success`` with what
    ``domhand_assess_state`` and ``_record_expected_value_if_settled`` consider settled.
    """
    is_select = field.field_type == "select"
    to = timeout_s if timeout_s is not None else (5.5 if is_select else 2.5)
    poll = poll_interval_s if poll_interval_s is not None else (0.22 if is_select else 0.12)
    deadline = time.monotonic() + to
    while time.monotonic() < deadline:
        if is_select:
            with contextlib.suppress(Exception):
                await page.evaluate(_COLLAPSE_COMBOBOX_FOR_FF_JS, field.field_id)
            await asyncio.sleep(0.14)
        if await _field_already_matches(page, field, desired_value, matched_label=matched_label):
            return True
        await asyncio.sleep(poll)
    return False


async def _attempt_domhand_fill_with_retry_cap(
    page: Any,
    *,
    host: str,
    field: FormField,
    desired_value: str,
    tool_name: str,
    browser_session: BrowserSession | None = None,
) -> tuple[bool, str | None, str | None, float, str]:
    """Attempt a DomHand field fill while enforcing the generic per-field retry cap.

    Returns (success, error_msg, failure_reason, fill_confidence, settled_value) where
    fill_confidence is 1.0 for DOM-verified, 0.8 for LLM-verified, 0.0 for failed.
    """
    if await _field_already_matches(page, field, desired_value):
        _clear_domhand_failure_for_field(host, field, desired_value)
        return True, None, None, 1.0, desired_value

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
        return False, _domhand_retry_message(field), DOMHAND_RETRY_CAPPED, 0.0, desired_value

    fill_confidence = 0.0
    skip_freeform_escalation = await _is_workday_prompt_search_widget(page, field)

    fill_result = await _fill_single_field_outcome(
        page,
        field,
        desired_value,
        browser_session=browser_session,
    )
    success = fill_result.success
    settled_value = fill_result.matched_label or desired_value
    if success and not await _verify_fill_observable(
        page,
        field,
        desired_value,
        matched_label=fill_result.matched_label,
    ):
        observed = await _read_observed_field_value(page, field)

        # Tier 1: Try deterministic verification_engine match first (v3 parity).
        # This covers country aliases, phone formatting, state abbreviations, etc.
        # that the simple observable poll missed.
        from ghosthands.dom.verification_engine import values_match as _ve_match

        if observed and _ve_match(
            observed,
            desired_value,
            field_type=field.field_type,
            matched_label=fill_result.matched_label,
        ):
            fill_confidence = 1.0
            logger.info(
                "domhand.fill.verification_engine_match",
                tool=tool_name,
                field_label=_preferred_field_label(field),
                desired_preview=str(desired_value)[:120],
                observed_preview=str(observed)[:120],
            )
        else:
            # Tier 5 (last resort): LLM screenshot verify — only if deterministic failed
            llm_confirmed = await _llm_verify_if_available(page, field, desired_value)
            if llm_confirmed is True:
                fill_confidence = 0.8
                logger.info(
                    "domhand.fill.llm_verify_override",
                    tool=tool_name,
                    field_label=_preferred_field_label(field),
                    desired_preview=str(desired_value)[:120],
                    observed_preview=str(observed)[:120],
                )
            else:
                # Readback unverified — executor reported success but readback never matched.
                # The verification_engine in domhand_fill will map this to review_status=unreadable.
                fill_confidence = FILL_CONFIDENCE_FILLED_READBACK_UNVERIFIED
                logger.info(
                    "domhand.fill.observable_verify_unconfirmed",
                    tool=tool_name,
                    field_id=field.field_id,
                    field_label=_preferred_field_label(field),
                    field_type=field.field_type,
                    desired_preview=str(desired_value)[:120],
                    observed_preview=str(observed)[:120],
                    llm_verify_result=llm_confirmed,
                )
    elif success:
        fill_confidence = 1.0

    if not success and not skip_freeform_escalation:
        stagehand_rescued = await _stagehand_escalate_fill(
            page,
            field,
            desired_value,
            browser_session=browser_session,
        )
        if stagehand_rescued:
            fill_confidence = 0.6
            logger.info(
                "domhand.fill.stagehand_escalation_success",
                tool=tool_name,
                field_label=_preferred_field_label(field),
                desired_preview=str(desired_value)[:120],
            )
            success = True
    elif not success and skip_freeform_escalation:
        logger.info(
            "domhand.fill.skip_freeform_prompt_search_escalation",
            tool=tool_name,
            field_label=_preferred_field_label(field),
            desired_preview=str(desired_value)[:120],
        )

    if not success and not skip_freeform_escalation:
        llm_rescued = await _llm_escalate_fill(page, field, desired_value)
        if llm_rescued:
            fill_confidence = 0.8
            logger.info(
                "domhand.fill.llm_escalation_success",
                tool=tool_name,
                field_label=_preferred_field_label(field),
                desired_preview=str(desired_value)[:120],
            )
            success = True

    if success:
        _clear_domhand_failure_for_field(host, field, desired_value)
        return True, None, None, fill_confidence, settled_value

    _, capped = _record_domhand_failure_for_field(host, field, desired_value, tool_name)
    if capped:
        return False, _domhand_retry_message(field), DOMHAND_RETRY_CAPPED, 0.0, desired_value
    return False, "DOM fill failed", "dom_fill_failed", 0.0, desired_value


async def _field_already_matches(
    page: Any,
    field: FormField,
    value: str | None,
    matched_label: str | None = None,
) -> bool:
    """Live DOM check to avoid re-filling a field that already settled correctly."""
    if not value:
        return False
    if field.field_type == "checkbox-group":
        if _checkbox_group_is_exclusive_choice(field):
            current = await _read_group_selection(page, field.field_id)
            return _field_value_matches_expected(
                current, value, matched_label=matched_label
            ) and not await _field_has_validation_error(page, field.field_id)
        desired_checked = not _is_explicit_false(value)
        state = await _read_binary_state(page, field.field_id)
        return state is desired_checked and not await _field_has_validation_error(page, field.field_id)
    if field.field_type in {"checkbox", "toggle"}:
        desired_checked = not _is_explicit_false(value)
        state = await _read_binary_state(page, field.field_id)
        return state is desired_checked and not await _field_has_validation_error(page, field.field_id)
    if field.field_type in {"radio-group", "radio", "button-group"}:
        current = await _read_group_selection(page, field.field_id)
        return _field_value_matches_expected(
            current, value, matched_label=matched_label
        ) and not await _field_has_validation_error(page, field.field_id)
    if await _uses_multi_select_observation(page, field):
        return await _skill_select_matches_observed_tokens(
            page, field, value
        ) and not await _field_has_validation_error(page, field.field_id)
    current = await _read_field_value_for_field(page, field)
    return _field_value_matches_expected(
        current,
        value,
        matched_label=matched_label,
    ) and not await _field_has_validation_error(page, field.field_id)


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
    if await _uses_multi_select_observation(page, field):
        selection = await _read_multi_select_selection(page, field.field_id)
        tokens = [str(token or "").strip() for token in (selection.get("tokens") or []) if str(token or "").strip()]
        return ", ".join(tokens)
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
        logger.debug(
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
        logger.debug(
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
    logger.debug(
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
