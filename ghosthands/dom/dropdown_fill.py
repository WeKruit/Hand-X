"""Unified dropdown fill orchestrator — open → scan → match → click → verify → fallback.

This module replaces the three separate strategies that existed before
(``_fill_searchable_dropdown``, ``_fill_custom_dropdown``, CDP-first) with a
single reusable flow.  The caller supplies open/click/type callbacks so the
orchestrator is decoupled from Playwright specifics while still working with
any dropdown widget.

Lifecycle::

    1. open_fn()          — open the dropdown menu (click trigger / chevron)
    2. scan options        — ``SCAN_VISIBLE_OPTIONS_JS`` reads visible labels
    3. match               — ``match_dropdown_option`` picks the best label
    4. click matched label — via JS ``CLICK_DROPDOWN_OPTION_ENHANCED_JS``
    5. verify              — ``selection_matches_desired`` checks UI committed
    6. fallback            — type search terms, re-scan, re-match, re-click
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import structlog

from ghosthands.actions.views import generate_dropdown_search_terms, split_dropdown_value_hierarchy
from ghosthands.dom.dropdown_match import (
    CLICK_DROPDOWN_OPTION_ENHANCED_JS,
    SCAN_VISIBLE_OPTIONS_JS,
    match_dropdown_option,
    synonym_groups_for_js,
)
from ghosthands.dom.dropdown_verify import selection_matches_desired

logger = structlog.get_logger(__name__)

# Greenhouse / react-select: the listbox often paints 400–900ms after open; selection commits
# asynchronously. Too-short waits + early Escape (settle) looks like "nothing popped" or instant
# click-away in headful runs.
_DROPDOWN_OPEN_SETTLE_S = 1.8
# After clicking a menu option: give react-select time to commit before dismiss/verify.
POST_OPTION_CLICK_SETTLE_S = 2.0
_POST_OPTION_CLICK_BREATHE_S = POST_OPTION_CLICK_SETTLE_S


@dataclass
class DropdownFillResult:
    """Outcome of a ``fill_interactive_dropdown`` attempt."""

    success: bool = False
    matched_label: str | None = None
    committed_value: str | None = None
    pass_name: str | None = None  # e.g. "scan_click", "type_click", "arrow_enter"


# ── Internal helpers ──────────────────────────────────────────────────

async def _scan_options(page: Any) -> list[str]:
    try:
        raw = await page.evaluate(SCAN_VISIBLE_OPTIONS_JS)
        if isinstance(raw, str):
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


async def _click_option_js(page: Any, text: str) -> dict[str, Any]:
    try:
        raw = await page.evaluate(CLICK_DROPDOWN_OPTION_ENHANCED_JS, text, synonym_groups_for_js())
    except Exception:
        return {"clicked": False}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"clicked": False}
    return raw if isinstance(raw, dict) else {"clicked": False}


async def _read_value(page: Any, read_value_fn: Callable[..., Coroutine[Any, Any, str]]) -> str:
    try:
        return await read_value_fn()
    except Exception:
        return ""


# ── Core orchestrator ─────────────────────────────────────────────────

async def fill_interactive_dropdown(
    page: Any,
    desired_value: str,
    *,
    open_fn: Callable[..., Coroutine[Any, Any, None]],
    read_value_fn: Callable[..., Coroutine[Any, Any, str]],
    settle_fn: Callable[..., Coroutine[Any, Any, None]] | None = None,
    dismiss_fn: Callable[..., Coroutine[Any, Any, None]] | None = None,
    type_fn: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    clear_fn: Callable[..., Coroutine[Any, Any, None]] | None = None,
    tag: str = "",
) -> DropdownFillResult:
    """Unified dropdown fill — open → scan → match → click → verify → fallback type.

    Parameters
    ----------
    page:
        The Playwright page object.
    desired_value:
        The answer to commit into the dropdown.
    open_fn:
        Async callable that opens the dropdown menu.
    read_value_fn:
        Async callable returning the current committed field value text.
    settle_fn:
        Optional async callable to dismiss the dropdown and let the UI commit.
    dismiss_fn:
        Optional async callable to dismiss the dropdown without committing.
    type_fn:
        Optional async callable ``type_fn(text)`` that types into the dropdown
        filter input.  When ``None``, the type-to-filter fallback is skipped.
    clear_fn:
        Optional async callable to clear the dropdown search input before typing.
    tag:
        Logging context identifier.
    """
    if not desired_value:
        return DropdownFillResult()

    async def _settle() -> None:
        if settle_fn:
            await settle_fn()
        else:
            await asyncio.sleep(0.45)

    async def _dismiss() -> None:
        if dismiss_fn:
            await dismiss_fn()
        else:
            await asyncio.sleep(0.2)

    # ── Phase 1: open → scan → match → click ────────────────────────
    try:
        await open_fn()
        await asyncio.sleep(_DROPDOWN_OPEN_SETTLE_S)
    except Exception:
        return DropdownFillResult()

    options = await _scan_options(page)
    if not options:
        # Async option lists (Greenhouse) may render after open. Wait/rescan before
        # calling open_fn() again — a second toggle click closes react-select (feels
        # like a stray click / "click away").
        for _ in range(5):
            await asyncio.sleep(0.5)
            options = await _scan_options(page)
            if options:
                break
        if not options:
            try:
                await open_fn()
                await asyncio.sleep(0.9)
                options = await _scan_options(page)
            except Exception:
                pass
    if options:
        matched = match_dropdown_option(desired_value, options)
        if matched:
            clicked = await _click_option_js(page, matched)
            if clicked.get("clicked"):
                await asyncio.sleep(_POST_OPTION_CLICK_BREATHE_S)
                await _settle()
                current = await _read_value(page, read_value_fn)
                if selection_matches_desired(current, desired_value, matched_label=matched):
                    logger.debug("dropdown_fill.scan_click_ok", tag=tag, matched=matched[:60])
                    return DropdownFillResult(
                        success=True,
                        matched_label=matched,
                        committed_value=current,
                        pass_name="scan_click",
                    )

    # ── Phase 2: hierarchical segments ───────────────────────────────
    segments = split_dropdown_value_hierarchy(desired_value)
    if len(segments) > 1:
        all_clicked = True
        for idx, segment in enumerate(segments):
            seg_options = await _scan_options(page)
            seg_match = match_dropdown_option(segment, seg_options) if seg_options else None
            if seg_match:
                clicked = await _click_option_js(page, seg_match)
                if not clicked.get("clicked"):
                    all_clicked = False
                    break
            else:
                all_clicked = False
                break
            await asyncio.sleep(0.8 if idx < len(segments) - 1 else 0.4)

        if all_clicked:
            await asyncio.sleep(_POST_OPTION_CLICK_BREATHE_S)
            await _settle()
            current = await _read_value(page, read_value_fn)
            if selection_matches_desired(current, desired_value):
                logger.debug("dropdown_fill.hierarchy_ok", tag=tag, segments=segments)
                return DropdownFillResult(
                    success=True,
                    matched_label=segments[-1],
                    committed_value=current,
                    pass_name="hierarchy_click",
                )

    # ── Phase 3: type-to-filter fallback ─────────────────────────────
    if type_fn:
        search_terms = generate_dropdown_search_terms(desired_value)
        if not search_terms:
            search_terms = [desired_value]

        for term_idx, term in enumerate(search_terms):
            try:
                if clear_fn and term_idx > 0:
                    await clear_fn()
                await type_fn(term)
                await asyncio.sleep(0.52)

                # Scan after typing and try fuzzy match
                typed_options = await _scan_options(page)
                if typed_options:
                    typed_match = match_dropdown_option(desired_value, typed_options)
                    if typed_match:
                        clicked = await _click_option_js(page, typed_match)
                        if clicked.get("clicked"):
                            await asyncio.sleep(_POST_OPTION_CLICK_BREATHE_S)
                            await _settle()
                            current = await _read_value(page, read_value_fn)
                            if selection_matches_desired(current, desired_value, matched_label=typed_match):
                                logger.debug(
                                    "dropdown_fill.type_scan_click_ok",
                                    tag=tag,
                                    term=term[:40],
                                    matched=typed_match[:60],
                                )
                                return DropdownFillResult(
                                    success=True,
                                    matched_label=typed_match,
                                    committed_value=current,
                                    pass_name="type_scan_click",
                                )

                # Direct JS click without pre-scan (poll retries)
                deadline = asyncio.get_event_loop().time() + 2.5
                while asyncio.get_event_loop().time() < deadline:
                    clicked = await _click_option_js(page, desired_value)
                    if clicked.get("clicked"):
                        await asyncio.sleep(_POST_OPTION_CLICK_BREATHE_S)
                        await _settle()
                        current = await _read_value(page, read_value_fn)
                        if selection_matches_desired(current, desired_value, matched_label=clicked.get("text")):
                            logger.debug("dropdown_fill.type_poll_click_ok", tag=tag, term=term[:40])
                            return DropdownFillResult(
                                success=True,
                                matched_label=clicked.get("text"),
                                committed_value=current,
                                pass_name="type_poll_click",
                            )
                        break
                    await asyncio.sleep(0.18)

            except Exception:
                continue

    # ── Phase 4: ArrowDown + Enter last resort ───────────────────────
    try:
        await page.keyboard.press("ArrowDown")
        await asyncio.sleep(0.35)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.55)
        await _settle()
        current = await _read_value(page, read_value_fn)
        if selection_matches_desired(current, desired_value):
            logger.debug("dropdown_fill.arrow_enter_ok", tag=tag)
            return DropdownFillResult(
                success=True,
                matched_label=None,
                committed_value=current,
                pass_name="arrow_enter",
            )
    except Exception:
        pass

    await _dismiss()
    return DropdownFillResult(success=False, pass_name="failed")
