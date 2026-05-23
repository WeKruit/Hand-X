"""Unit tests for the page-batched visual verification helper."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from browser_use.browser.views import BrowserStateSummary, TabInfo
from browser_use.dom.views import SerializedDOMState
from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage
from ghosthands.actions.views import FormField, get_stable_field_key
from ghosthands.dom.page_visual_verifier import (
    VisualTrustTier,
    VisualVerificationCandidate,
    VisualVerificationFieldResponse,
    VisualVerificationMode,
    VisualVerificationPayload,
    build_visual_candidates,
    classify_visual_trust_tier,
    verification_mode_for_tier,
    verify_page_visual_candidates,
)
from ghosthands.runtime_learning import (
    build_page_context_key,
    record_expected_field_value,
    reset_runtime_learning_state,
)

_ONE_BY_ONE_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9s5n0TsAAAAASUVORK5CYII="


class _FakeLLM:
    model = "gemini-3-flash-preview"

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages, output_format=None):
        self.calls += 1
        assert output_format is VisualVerificationPayload
        completion = output_format(
            page_context_key="ctx",
            fields=[
                VisualVerificationFieldResponse(
                    field_key="country-key",
                    field_label="Country",
                    observed_value="United States",
                    matches_expected=True,
                    confidence=0.99,
                ),
                VisualVerificationFieldResponse(
                    field_key="email-key",
                    field_label="Email",
                    observed_value="wrong@example.com",
                    matches_expected=False,
                    confidence=0.82,
                ),
            ],
        )
        usage = ChatInvokeUsage(
            prompt_tokens=1000,
            prompt_cached_tokens=None,
            prompt_cache_creation_tokens=None,
            prompt_image_tokens=300,
            completion_tokens=200,
            total_tokens=1200,
        )
        return ChatInvokeCompletion(completion=completion, usage=usage, stop_reason="end_turn")


def _browser_state() -> BrowserStateSummary:
    return BrowserStateSummary(
        dom_state=SerializedDOMState(_root=None, selector_map={}),
        url="https://job-boards.greenhouse.io/acme/jobs/123",
        title="Greenhouse Application",
        tabs=[
            TabInfo(
                url="https://job-boards.greenhouse.io/acme/jobs/123",
                title="Greenhouse Application",
                target_id="tab-1",
            )
        ],
        screenshot=_ONE_BY_ONE_PNG_B64,
    )


def test_classify_visual_trust_tier_and_mode():
    select_field = FormField(field_id="country", name="Country", field_type="select", is_native=False)
    text_field = FormField(field_id="email", name="Email", field_type="email", is_native=True)
    textarea_field = FormField(field_id="essay", name="Why us?", field_type="textarea", is_native=True)

    assert classify_visual_trust_tier(select_field) is VisualTrustTier.TIER_A
    assert classify_visual_trust_tier(text_field) is VisualTrustTier.TIER_B
    assert classify_visual_trust_tier(textarea_field) is VisualTrustTier.TIER_C
    assert verification_mode_for_tier(VisualTrustTier.TIER_A) is VisualVerificationMode.EXACT_VISIBLE_VALUE
    assert verification_mode_for_tier(VisualTrustTier.TIER_C) is VisualVerificationMode.FILLEDNESS_ONLY


def test_build_visual_candidates_uses_runtime_expected_values():
    reset_runtime_learning_state()
    host = "job-boards.greenhouse.io"
    page_context_key = build_page_context_key(
        url="https://job-boards.greenhouse.io/acme/jobs/123",
        page_marker="Personal Information",
    )
    country = FormField(field_id="country", name="Country", field_type="select", is_native=False, visible=True)
    upload = FormField(field_id="resume", name="Resume", field_type="file", visible=True)

    record_expected_field_value(
        host=host,
        page_context_key=page_context_key,
        field_key=get_stable_field_key(country),
        field_label=country.name,
        expected_value="United States",
        source="exact_profile",
    )

    candidates = build_visual_candidates([country, upload], page_host=host, page_context_key=page_context_key)

    assert len(candidates) == 1
    assert candidates[0].field_id == "country"
    assert candidates[0].trust_tier is VisualTrustTier.TIER_A
    assert candidates[0].expected_value == "United States"


@pytest.mark.asyncio
async def test_verify_page_visual_candidates_uses_cache_and_tracks_cost(monkeypatch):
    fake_llm = _FakeLLM()
    browser_session = SimpleNamespace()
    browser_session.get_browser_state_summary = AsyncMock(return_value=_browser_state())

    monkeypatch.setattr("ghosthands.dom.page_visual_verifier.get_chat_model", lambda model: fake_llm)

    candidates = [
        VisualVerificationCandidate(
            field_id="country",
            field_key="country-key",
            field_label="Country",
            field_type="select",
            expected_value="United States",
            trust_tier=VisualTrustTier.TIER_A,
            verification_mode=VisualVerificationMode.EXACT_VISIBLE_VALUE,
        ),
        VisualVerificationCandidate(
            field_id="email",
            field_key="email-key",
            field_label="Email",
            field_type="email",
            expected_value="right@example.com",
            trust_tier=VisualTrustTier.TIER_B,
            verification_mode=VisualVerificationMode.EXACT_VISIBLE_VALUE,
        ),
    ]

    first = await verify_page_visual_candidates(
        cast(Any, browser_session),
        page_context_key="ctx",
        candidates=candidates,
    )
    second = await verify_page_visual_candidates(
        cast(Any, browser_session),
        page_context_key="ctx",
        candidates=candidates,
    )

    assert first.attempted is True
    assert first.cache_hit is False
    assert first.candidate_count == 2
    assert first.calls == 1
    assert first.verified_count == 1
    assert first.mismatch_count == 1
    assert first.uncertain_count == 0
    assert first.estimated_cost_usd == pytest.approx(0.0011)
    assert second.cache_hit is True
    assert fake_llm.calls == 1
