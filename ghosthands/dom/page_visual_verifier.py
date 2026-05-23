"""Page-batched visual verification helpers for DomHand.

This module is intentionally standalone so the visual layer can be tested in
isolation before it is wired into ``domhand_assess_state``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from inspect import isawaitable
import json
import logging
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from browser_use.agent.prompts import AgentMessagePrompt
from browser_use.browser import BrowserSession
from browser_use.browser.views import BrowserStateSummary, PageInfo, TabInfo
from browser_use.dom.views import SerializedDOMState
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm.messages import SystemMessage
from ghosthands.actions.views import FormField, get_stable_field_key
from ghosthands.config.models import estimate_cost
from ghosthands.config.settings import settings
from ghosthands.llm.client import get_chat_model
from ghosthands.runtime_learning import get_expected_field_value

logger = logging.getLogger(__name__)

MAX_VISUAL_FIELDS_PER_BATCH = 6
_VISUAL_PROMPT_FS_DIR = Path(tempfile.gettempdir()) / "gh_visual_verifier"
_VISUAL_SEGMENT_OVERLAP_RATIO = 0.2
_MIN_VISUAL_SEGMENT_OVERLAP_PX = 96
_VISUAL_SCROLL_SETTLE_SECONDS = 0.2

VisualFieldStatus = Literal["verified", "mismatch", "unfilled", "uncertain"]


@dataclass(frozen=True)
class _VisualViewportSegment:
    """One viewport-sized slice of the page that should be verified together."""

    index: int
    scroll_y: int
    start_y: int
    end_y: int
    candidates: list[VisualVerificationCandidate]


class VisualTrustTier(StrEnum):
    """Typed trust tier for how strongly Gemini outcomes should be enforced."""

    TIER_A = "tier_a"
    TIER_B = "tier_b"
    TIER_C = "tier_c"


class VisualVerificationMode(StrEnum):
    """How the verifier should reason about a target field."""

    EXACT_VISIBLE_VALUE = "exact_visible_value"
    FILLEDNESS_ONLY = "filledness_only"


class VisualVerificationCandidate(BaseModel):
    """One visible field that should be included in a page-batched visual call."""

    model_config = ConfigDict(extra="ignore")

    field_id: str
    field_key: str
    field_label: str
    field_type: str
    required: bool = False
    section: str = ""
    widget_kind: str | None = None
    expected_value: str
    trust_tier: VisualTrustTier
    verification_mode: VisualVerificationMode


class VisualVerificationFieldResponse(BaseModel):
    """Structured model response for one requested field."""

    model_config = ConfigDict(extra="ignore")

    field_key: str
    field_label: str = ""
    observed_value: str = ""
    matches_expected: bool | None = None
    confidence: float | None = None


class VisualVerificationPayload(BaseModel):
    """Structured Gemini response for one page-batched call."""

    model_config = ConfigDict(extra="ignore")

    page_context_key: str
    fields: list[VisualVerificationFieldResponse] = Field(default_factory=list)


class VisualVerificationFieldOutcome(BaseModel):
    """Normalized field outcome used by assess-state integration."""

    model_config = ConfigDict(extra="ignore")

    field_id: str
    field_key: str
    field_label: str
    field_type: str
    expected_value: str
    observed_value: str = ""
    required: bool = False
    trust_tier: VisualTrustTier
    verification_mode: VisualVerificationMode
    matches_expected: bool | None = None
    confidence: float | None = None
    status: VisualFieldStatus = "uncertain"


class VisualVerificationBatchResult(BaseModel):
    """Aggregate result for one assess-state visual verification pass."""

    model_config = ConfigDict(extra="ignore")

    attempted: bool = False
    cache_hit: bool = False
    page_context_key: str = ""
    model_name: str = ""
    candidate_count: int = 0
    segment_count: int = 0
    calls: int = 0
    verified_count: int = 0
    mismatch_count: int = 0
    unfilled_count: int = 0
    uncertain_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    prompt_image_tokens: int = 0
    estimated_cost_usd: float = 0.0
    screenshot_dimensions: str | None = None
    error: str | None = None
    results: list[VisualVerificationFieldOutcome] = Field(default_factory=list)


def classify_visual_trust_tier(field: FormField) -> VisualTrustTier:
    """Map field types into the first-pass visual trust tiers."""

    widget_kind = str(field.widget_kind or "").strip().lower()
    field_type = str(field.field_type or "").strip().lower()

    if widget_kind and widget_kind not in {"text_input", "textarea", "search_input"}:
        return VisualTrustTier.TIER_A
    if field_type in {
        "select",
        "checkbox",
        "checkbox-group",
        "radio",
        "radio-group",
        "toggle",
        "button-group",
        "date",
    }:
        return VisualTrustTier.TIER_A
    if field_type == "textarea":
        return VisualTrustTier.TIER_C
    return VisualTrustTier.TIER_B


def verification_mode_for_tier(tier: VisualTrustTier) -> VisualVerificationMode:
    """Translate trust tiers into the verification mode sent to Gemini."""

    if tier is VisualTrustTier.TIER_C:
        return VisualVerificationMode.FILLEDNESS_ONLY
    return VisualVerificationMode.EXACT_VISIBLE_VALUE


def build_visual_candidates(
    fields: list[FormField],
    *,
    page_host: str,
    page_context_key: str,
) -> list[VisualVerificationCandidate]:
    """Build visual-verification candidates from the visible field set."""

    candidates: list[VisualVerificationCandidate] = []
    for field in fields:
        if not field.visible or field.field_type == "file":
            continue
        expected = get_expected_field_value(
            host=page_host,
            page_context_key=page_context_key,
            field_key=get_stable_field_key(field),
        )
        if expected is None:
            continue
        expected_value = str(getattr(expected, "expected_value", "") or "").strip()
        if not expected_value:
            continue
        trust_tier = classify_visual_trust_tier(field)
        candidates.append(
            VisualVerificationCandidate(
                field_id=field.field_id,
                field_key=get_stable_field_key(field),
                field_label=field.name,
                field_type=field.field_type,
                required=field.required,
                section=field.section or "",
                widget_kind=field.widget_kind,
                expected_value=expected_value,
                trust_tier=trust_tier,
                verification_mode=verification_mode_for_tier(trust_tier),
            )
        )
    return candidates


def _chunked(items: list[VisualVerificationCandidate], size: int) -> list[list[VisualVerificationCandidate]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _get_visual_model() -> Any:
    return get_chat_model(settings.domhand_visual_model)


def _build_system_message() -> SystemMessage:
    return SystemMessage(
        content=(
            "You are a careful visual verifier for job application forms. "
            "Verify only the explicitly listed visible fields. "
            "Return JSON with `page_context_key` and a `fields` array. "
            "Each field entry must include `field_key`, `field_label`, `observed_value`, "
            "`matches_expected`, and `confidence`. "
            "For `verification_mode = filledness_only`, treat the field as matching when it is visibly non-empty."
        )
    )


def _build_task(page_context_key: str, candidates: list[VisualVerificationCandidate]) -> str:
    field_lines = []
    for candidate in candidates:
        expected = candidate.expected_value if candidate.expected_value else "[empty string]"
        field_lines.append(
            f'- field_key="{candidate.field_key}"'
            f' | label="{candidate.field_label}"'
            f' | type="{candidate.field_type}"'
            f' | verification_mode="{candidate.verification_mode.value}"'
            f' | expected_visible_value="{expected}"'
        )
    joined = "\n".join(field_lines)
    return (
        "This is a visual verification request for a job application page.\n"
        f'Page context key: "{page_context_key}"\n'
        "Verify only the following visible fields and compare each one against the expected visible value.\n"
        f"{joined}\n"
        "Return structured output only."
    )


def _minimal_browser_state(state: BrowserStateSummary) -> BrowserStateSummary:
    tabs = state.tabs or [TabInfo(url=state.url, title=state.title, target_id="domhand-visual-tab")]
    return BrowserStateSummary(
        dom_state=SerializedDOMState(_root=None, selector_map={}),
        url=state.url,
        title=state.title,
        tabs=tabs,
        screenshot=None,
    )


def _screenshot_dimensions(screenshot_b64: str) -> str:
    from io import BytesIO

    from PIL import Image

    image_bytes = base64.b64decode(screenshot_b64)
    with Image.open(BytesIO(image_bytes)) as image:
        return f"{image.size[0]}x{image.size[1]}"


def _get_visual_file_system(browser_session: BrowserSession) -> FileSystem:
    fs = getattr(browser_session, "_gh_visual_prompt_file_system", None)
    if isinstance(fs, FileSystem):
        return fs
    _VISUAL_PROMPT_FS_DIR.mkdir(parents=True, exist_ok=True)
    fs = FileSystem(base_dir=_VISUAL_PROMPT_FS_DIR)
    object.__setattr__(browser_session, "_gh_visual_prompt_file_system", fs)
    return fs


def _page_scroll_y(page_info: PageInfo | None) -> int:
    return int(getattr(page_info, "scroll_y", 0) or 0)


def _page_viewport_height(page_info: PageInfo | None) -> int:
    return int(getattr(page_info, "viewport_height", 0) or 0)


def _page_height(page_info: PageInfo | None) -> int:
    return int(getattr(page_info, "page_height", 0) or 0)


def _absolute_layout_bounds(layout_entry: dict[str, Any] | None, *, fallback_scroll_y: int) -> tuple[float, float] | None:
    if not isinstance(layout_entry, dict):
        return None

    raw_top = layout_entry.get("abs_top")
    raw_bottom = layout_entry.get("abs_bottom")
    if raw_top is None or raw_bottom is None:
        raw_top = layout_entry.get("top")
        raw_bottom = layout_entry.get("bottom")
        if raw_top is None or raw_bottom is None:
            return None
        raw_top = float(raw_top) + float(fallback_scroll_y)
        raw_bottom = float(raw_bottom) + float(fallback_scroll_y)

    top = float(raw_top)
    bottom = float(raw_bottom)
    if bottom < top:
        top, bottom = bottom, top
    return top, bottom


def _sort_candidates_for_segment(
    candidates: list[VisualVerificationCandidate],
    position_by_field_id: dict[str, tuple[float, float]],
) -> list[VisualVerificationCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            position_by_field_id.get(candidate.field_id, (0.0, 0.0))[0],
            position_by_field_id.get(candidate.field_id, (0.0, 0.0))[1],
            candidate.field_label.lower(),
            candidate.field_id,
        ),
    )


def _fallback_single_segment(
    candidates: list[VisualVerificationCandidate],
    *,
    page_info: PageInfo | None,
    layout: dict[str, Any] | None,
) -> list[_VisualViewportSegment]:
    if not candidates:
        return []
    layout = layout or {}
    in_view_candidates = [
        candidate for candidate in candidates if bool((layout.get(candidate.field_id) or {}).get("in_view"))
    ]
    chosen = in_view_candidates or candidates
    scroll_y = _page_scroll_y(page_info)
    viewport_height = _page_viewport_height(page_info)
    return [
        _VisualViewportSegment(
            index=0,
            scroll_y=scroll_y,
            start_y=scroll_y,
            end_y=scroll_y + max(viewport_height, 1),
            candidates=chosen,
        )
    ]


def _build_viewport_segments(
    candidates: list[VisualVerificationCandidate],
    *,
    layout: dict[str, Any] | None,
    page_info: PageInfo | None,
    overlap_ratio: float = _VISUAL_SEGMENT_OVERLAP_RATIO,
) -> list[_VisualViewportSegment]:
    if not candidates:
        return []

    layout = layout or {}
    viewport_height = _page_viewport_height(page_info)
    page_height = _page_height(page_info)
    fallback_scroll_y = _page_scroll_y(page_info)
    if viewport_height <= 0:
        return _fallback_single_segment(candidates, page_info=page_info, layout=layout)

    position_by_field_id: dict[str, tuple[float, float]] = {}
    for candidate in candidates:
        bounds = _absolute_layout_bounds(layout.get(candidate.field_id), fallback_scroll_y=fallback_scroll_y)
        if bounds is not None:
            position_by_field_id[candidate.field_id] = bounds

    positioned_candidates = [candidate for candidate in candidates if candidate.field_id in position_by_field_id]
    if not positioned_candidates:
        return _fallback_single_segment(candidates, page_info=page_info, layout=layout)

    overlap_px = min(
        max(int(viewport_height * overlap_ratio), _MIN_VISUAL_SEGMENT_OVERLAP_PX),
        max(viewport_height - 1, 1),
    )
    step = max(1, viewport_height - overlap_px)
    max_scroll = max(page_height - viewport_height, 0)

    min_abs_top = min(position_by_field_id[candidate.field_id][0] for candidate in positioned_candidates)
    max_abs_bottom = max(position_by_field_id[candidate.field_id][1] for candidate in positioned_candidates)
    coverage_start = max(0, int(min_abs_top) - overlap_px)
    coverage_end = min(max(page_height, viewport_height), int(max_abs_bottom) + overlap_px)
    start_y = min(coverage_start, max_scroll)

    segments: list[_VisualViewportSegment] = []
    seen_signatures: set[tuple[int, tuple[str, ...]]] = set()

    while True:
        end_y = start_y + viewport_height
        segment_candidates = [
            candidate
            for candidate in positioned_candidates
            if (
                position_by_field_id[candidate.field_id][1] >= start_y - overlap_px
                and position_by_field_id[candidate.field_id][0] <= end_y + overlap_px
            )
        ]
        if segment_candidates:
            ordered = _sort_candidates_for_segment(segment_candidates, position_by_field_id)
            signature = (start_y, tuple(candidate.field_id for candidate in ordered))
            if signature not in seen_signatures:
                seen_signatures.add(signature)
                segments.append(
                    _VisualViewportSegment(
                        index=len(segments),
                        scroll_y=start_y,
                        start_y=start_y,
                        end_y=end_y,
                        candidates=ordered,
                    )
                )

        if end_y >= coverage_end or start_y >= max_scroll:
            break

        next_start = min(start_y + step, max_scroll)
        if next_start <= start_y:
            break
        start_y = next_start

    if not segments:
        return _fallback_single_segment(candidates, page_info=page_info, layout=layout)
    return segments


def _outcome_priority(outcome: VisualVerificationFieldOutcome) -> tuple[int, float, int]:
    status_priority = {
        "verified": 4,
        "mismatch": 3,
        "unfilled": 3,
        "uncertain": 2,
    }.get(outcome.status, 1)
    confidence = float(outcome.confidence or 0.0)
    has_observed_value = 1 if outcome.observed_value else 0
    return status_priority, confidence, has_observed_value


def _merge_segment_outcomes(
    merged: dict[str, tuple[VisualVerificationFieldOutcome, int]],
    outcomes: list[VisualVerificationFieldOutcome],
    *,
    segment_index: int,
) -> None:
    for outcome in outcomes:
        current = merged.get(outcome.field_id)
        if current is None:
            merged[outcome.field_id] = (outcome, segment_index)
            continue

        current_outcome, current_segment_index = current
        if _outcome_priority(outcome) > _outcome_priority(current_outcome):
            merged[outcome.field_id] = (outcome, segment_index)
            continue
        if _outcome_priority(outcome) == _outcome_priority(current_outcome) and segment_index >= current_segment_index:
            merged[outcome.field_id] = (outcome, segment_index)


def _merged_outcomes_in_candidate_order(
    candidates: list[VisualVerificationCandidate],
    merged: dict[str, tuple[VisualVerificationFieldOutcome, int]],
) -> list[VisualVerificationFieldOutcome]:
    outcomes_by_field_id = {field_id: outcome for field_id, (outcome, _segment_index) in merged.items()}
    ordered: list[VisualVerificationFieldOutcome] = []
    for candidate in candidates:
        outcome = outcomes_by_field_id.get(candidate.field_id)
        if outcome is not None:
            ordered.append(outcome)
    return ordered


async def _scroll_page_to(page: Any, scroll_y: int) -> int:
    scroll_js = """(targetY) => {
        const nextY = Math.max(0, Number(targetY || 0));
        window.scrollTo(0, nextY);
        return JSON.stringify({
            scrollY: window.pageYOffset || document.documentElement.scrollTop || 0
        });
    }"""
    payload = await page.evaluate(scroll_js, int(scroll_y))
    data = json.loads(payload) if isinstance(payload, str) else payload or {}
    await asyncio.sleep(_VISUAL_SCROLL_SETTLE_SECONDS)
    return int(data.get("scrollY", scroll_y) or scroll_y)


async def _capture_viewport_screenshot_b64(browser_session: BrowserSession) -> str:
    screenshot_bytes = await browser_session.take_screenshot(full_page=False)
    return base64.b64encode(screenshot_bytes).decode("utf-8")


async def _invoke_visual_batch(
    *,
    llm: Any,
    file_system: FileSystem,
    messages_prefix: list[SystemMessage],
    browser_state: BrowserStateSummary,
    page_context_key: str,
    batch: list[VisualVerificationCandidate],
    screenshot_b64: str,
) -> tuple[list[VisualVerificationFieldOutcome], int, int, int]:
    prompt = AgentMessagePrompt(
        browser_state_summary=browser_state,
        file_system=file_system,
        task=_build_task(page_context_key, batch),
        screenshots=[screenshot_b64],
        vision_detail_level="auto",
        llm_screenshot_size=None,
    )
    user_message = prompt.get_user_message(use_vision=True)
    response = await llm.ainvoke(
        [*messages_prefix, user_message],
        output_format=VisualVerificationPayload,
    )
    prompt_tokens = 0
    completion_tokens = 0
    prompt_image_tokens = 0
    if response.usage:
        prompt_tokens = int(response.usage.prompt_tokens)
        completion_tokens = int(response.usage.completion_tokens)
        prompt_image_tokens = int(response.usage.prompt_image_tokens or 0)

    parsed_by_key = {item.field_key: item for item in response.completion.fields}
    outcomes = [_outcome_from_response(candidate, parsed_by_key.get(candidate.field_key)) for candidate in batch]
    return outcomes, prompt_tokens, completion_tokens, prompt_image_tokens


def _build_cache_key(
    *,
    page_context_key: str,
    page_url: str,
    screenshot_b64: str,
    candidates: list[VisualVerificationCandidate],
    segments: list[_VisualViewportSegment] | None = None,
) -> str:
    payload = {
        "page_context_key": page_context_key,
        "page_url": page_url,
        "screenshot_sha256": hashlib.sha256(screenshot_b64.encode("ascii")).hexdigest(),
        "candidates": [
            {
                "field_key": candidate.field_key,
                "expected_value": candidate.expected_value,
                "verification_mode": candidate.verification_mode.value,
                "trust_tier": candidate.trust_tier.value,
            }
            for candidate in candidates
        ],
        "segments": [
            {
                "scroll_y": segment.scroll_y,
                "field_keys": [candidate.field_key for candidate in segment.candidates],
            }
            for segment in (segments or [])
        ],
    }
    return json.dumps(payload, sort_keys=True)


def _outcome_from_response(
    candidate: VisualVerificationCandidate,
    response: VisualVerificationFieldResponse | None,
) -> VisualVerificationFieldOutcome:
    observed_value = str(getattr(response, "observed_value", "") or "").strip()
    matches_expected = getattr(response, "matches_expected", None)
    confidence = getattr(response, "confidence", None)

    if matches_expected is True:
        status: VisualFieldStatus = "verified"
    elif matches_expected is False and not observed_value:
        status = "unfilled"
    elif matches_expected is False:
        status = "mismatch"
    else:
        status = "uncertain"

    return VisualVerificationFieldOutcome(
        field_id=candidate.field_id,
        field_key=candidate.field_key,
        field_label=candidate.field_label,
        field_type=candidate.field_type,
        expected_value=candidate.expected_value,
        observed_value=observed_value,
        required=candidate.required,
        trust_tier=candidate.trust_tier,
        verification_mode=candidate.verification_mode,
        matches_expected=matches_expected,
        confidence=confidence,
        status=status,
    )


async def verify_page_visual_candidates(
    browser_session: BrowserSession,
    *,
    page_context_key: str,
    candidates: list[VisualVerificationCandidate],
    layout: dict[str, Any] | None = None,
    max_fields_per_call: int = MAX_VISUAL_FIELDS_PER_BATCH,
) -> VisualVerificationBatchResult:
    """Run page-batched Gemini verification for the current page."""

    if not candidates:
        return VisualVerificationBatchResult(page_context_key=page_context_key)

    try:
        raw_state = await browser_session.get_browser_state_summary(include_screenshot=True)
    except Exception as exc:
        logger.info(
            "domhand.visual_verifier.browser_state_unavailable",
            extra={"error": str(exc), "page_context_key": page_context_key},
        )
        return VisualVerificationBatchResult(
            page_context_key=page_context_key,
            candidate_count=len(candidates),
            model_name=settings.domhand_visual_model,
            error="browser_state_unavailable",
        )

    if not isinstance(raw_state, BrowserStateSummary):
        logger.debug(
            "domhand.visual_verifier.skipped_invalid_browser_state",
            extra={"page_context_key": page_context_key, "state_type": type(raw_state).__name__},
        )
        return VisualVerificationBatchResult(
            page_context_key=page_context_key,
            candidate_count=len(candidates),
            model_name=settings.domhand_visual_model,
        )

    state = raw_state
    if not state.screenshot:
        return VisualVerificationBatchResult(
            page_context_key=page_context_key,
            candidate_count=len(candidates),
            model_name=settings.domhand_visual_model,
            error="browser_state_missing_screenshot",
        )

    segments = _build_viewport_segments(
        candidates,
        layout=layout,
        page_info=state.page_info,
    )
    cache_key = _build_cache_key(
        page_context_key=page_context_key,
        page_url=state.url,
        screenshot_b64=state.screenshot,
        candidates=candidates,
        segments=segments,
    )
    cache_store = getattr(browser_session, "_gh_visual_verification_cache", None)
    if isinstance(cache_store, dict) and cache_key in cache_store:
        cached = VisualVerificationBatchResult.model_validate(cache_store[cache_key])
        cached.cache_hit = True
        return cached

    llm = _get_visual_model()
    file_system = _get_visual_file_system(browser_session)
    browser_state = _minimal_browser_state(state)
    messages_prefix = [_build_system_message()]

    merged_outcomes: dict[str, tuple[VisualVerificationFieldOutcome, int]] = {}
    input_tokens = 0
    output_tokens = 0
    prompt_image_tokens = 0
    calls = 0
    current_scroll_y = _page_scroll_y(state.page_info)
    original_scroll_y = current_scroll_y
    current_screenshot_b64 = state.screenshot
    page = None
    if len(segments) > 1 or any(segment.scroll_y != current_scroll_y for segment in segments):
        get_current_page = getattr(browser_session, "get_current_page", None)
        if callable(get_current_page):
            maybe_page = get_current_page()
            page = await maybe_page if isawaitable(maybe_page) else maybe_page

    try:
        for segment in segments:
            if page is not None and segment.scroll_y != current_scroll_y:
                current_scroll_y = await _scroll_page_to(page, segment.scroll_y)
                current_screenshot_b64 = await _capture_viewport_screenshot_b64(browser_session)
            elif segment.index > 0:
                current_screenshot_b64 = await _capture_viewport_screenshot_b64(browser_session)

            logger.info(
                "domhand.visual_verifier.segment",
                extra={
                    "page_context_key": page_context_key,
                    "segment_index": segment.index,
                    "scroll_y": segment.scroll_y,
                    "candidate_count": len(segment.candidates),
                },
            )

            for batch in _chunked(segment.candidates, max_fields_per_call):
                outcomes, prompt_tokens, completion_tokens, image_tokens = await _invoke_visual_batch(
                    llm=llm,
                    file_system=file_system,
                    messages_prefix=messages_prefix,
                    browser_state=browser_state,
                    page_context_key=page_context_key,
                    batch=batch,
                    screenshot_b64=current_screenshot_b64,
                )
                calls += 1
                input_tokens += prompt_tokens
                output_tokens += completion_tokens
                prompt_image_tokens += image_tokens
                _merge_segment_outcomes(merged_outcomes, outcomes, segment_index=segment.index)
    except Exception as exc:
        logger.warning(
            "domhand.visual_verifier.failed", extra={"error": str(exc), "page_context_key": page_context_key}
        )
        return VisualVerificationBatchResult(
            attempted=True,
            page_context_key=page_context_key,
            candidate_count=len(candidates),
            segment_count=len(segments),
            model_name=getattr(llm, "model", settings.domhand_visual_model),
            calls=calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            prompt_image_tokens=prompt_image_tokens,
            screenshot_dimensions=_screenshot_dimensions(state.screenshot),
            error=str(exc),
            results=_merged_outcomes_in_candidate_order(candidates, merged_outcomes),
        )
    finally:
        if page is not None and current_scroll_y != original_scroll_y:
            with suppress(Exception):
                await _scroll_page_to(page, original_scroll_y)

    all_outcomes = _merged_outcomes_in_candidate_order(candidates, merged_outcomes)
    result = VisualVerificationBatchResult(
        attempted=True,
        page_context_key=page_context_key,
        model_name=getattr(llm, "model", settings.domhand_visual_model),
        candidate_count=len(candidates),
        segment_count=len(segments),
        calls=calls,
        verified_count=sum(1 for outcome in all_outcomes if outcome.status == "verified"),
        mismatch_count=sum(1 for outcome in all_outcomes if outcome.status == "mismatch"),
        unfilled_count=sum(1 for outcome in all_outcomes if outcome.status == "unfilled"),
        uncertain_count=sum(1 for outcome in all_outcomes if outcome.status == "uncertain"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        prompt_image_tokens=prompt_image_tokens,
        estimated_cost_usd=estimate_cost(
            getattr(llm, "model", settings.domhand_visual_model), input_tokens, output_tokens
        ),
        screenshot_dimensions=_screenshot_dimensions(state.screenshot),
        results=all_outcomes,
    )

    cache_store = cache_store if isinstance(cache_store, dict) else {}
    cache_store[cache_key] = result.model_dump(mode="json")
    object.__setattr__(browser_session, "_gh_visual_verification_cache", cache_store)
    return result
