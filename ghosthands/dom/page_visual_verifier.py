"""Page-batched visual verification helpers for DomHand.

This module is intentionally standalone so the visual layer can be tested in
isolation before it is wired into ``domhand_assess_state``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from browser_use.agent.prompts import AgentMessagePrompt
from browser_use.browser import BrowserSession
from browser_use.browser.views import BrowserStateSummary, TabInfo
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

VisualFieldStatus = Literal["verified", "mismatch", "unfilled", "uncertain"]


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


def _build_cache_key(
    *,
    page_context_key: str,
    page_url: str,
    screenshot_b64: str,
    candidates: list[VisualVerificationCandidate],
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
    max_fields_per_call: int = MAX_VISUAL_FIELDS_PER_BATCH,
) -> VisualVerificationBatchResult:
    """Run page-batched Gemini verification for the visible candidate set."""

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

    cache_key = _build_cache_key(
        page_context_key=page_context_key,
        page_url=state.url,
        screenshot_b64=state.screenshot,
        candidates=candidates,
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

    all_outcomes: list[VisualVerificationFieldOutcome] = []
    input_tokens = 0
    output_tokens = 0
    prompt_image_tokens = 0
    calls = 0

    try:
        for batch in _chunked(candidates, max_fields_per_call):
            prompt = AgentMessagePrompt(
                browser_state_summary=browser_state,
                file_system=file_system,
                task=_build_task(page_context_key, batch),
                screenshots=[state.screenshot],
                vision_detail_level="auto",
                llm_screenshot_size=None,
            )
            user_message = prompt.get_user_message(use_vision=True)
            response = await llm.ainvoke(
                [*messages_prefix, user_message],
                output_format=VisualVerificationPayload,
            )
            calls += 1
            if response.usage:
                input_tokens += int(response.usage.prompt_tokens)
                output_tokens += int(response.usage.completion_tokens)
                prompt_image_tokens += int(response.usage.prompt_image_tokens or 0)

            parsed_by_key = {item.field_key: item for item in response.completion.fields}
            all_outcomes.extend(
                _outcome_from_response(candidate, parsed_by_key.get(candidate.field_key)) for candidate in batch
            )
    except Exception as exc:
        logger.warning(
            "domhand.visual_verifier.failed", extra={"error": str(exc), "page_context_key": page_context_key}
        )
        return VisualVerificationBatchResult(
            attempted=True,
            page_context_key=page_context_key,
            candidate_count=len(candidates),
            model_name=getattr(llm, "model", settings.domhand_visual_model),
            calls=calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            prompt_image_tokens=prompt_image_tokens,
            screenshot_dimensions=_screenshot_dimensions(state.screenshot),
            error=str(exc),
            results=all_outcomes,
        )

    result = VisualVerificationBatchResult(
        attempted=True,
        page_context_key=page_context_key,
        model_name=getattr(llm, "model", settings.domhand_visual_model),
        candidate_count=len(candidates),
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
