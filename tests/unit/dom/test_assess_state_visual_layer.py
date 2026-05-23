"""Focused tests for assess-state visual verification integration."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from ghosthands.actions.views import DomHandAssessStateParams, FormField
from ghosthands.dom.page_visual_verifier import (
    VisualTrustTier,
    VisualVerificationBatchResult,
    VisualVerificationCandidate,
    VisualVerificationFieldOutcome,
    VisualVerificationMode,
)


def _page_scan_payload(heading: str = "Application Questions") -> dict:
    return {
        "button_texts": ["Save and Continue"],
        "body_text": "",
        "markers": [],
        "submit_visible": False,
        "submit_disabled": False,
        "advance_visible": True,
        "advance_disabled": False,
        "error_texts": [],
        "heading_texts": [heading],
    }


def _layout_payload(field_id: str) -> dict[str, dict[str, float | bool]]:
    return {field_id: {"in_view": True, "top": 0, "bottom": 24}}


@pytest.mark.asyncio
async def test_assess_state_visual_verifier_clears_required_missing_custom_select():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state

    field = FormField(
        field_id="country",
        name="Country / Territory",
        field_type="select",
        section="My Information",
        required=True,
        is_native=False,
    )
    candidate = VisualVerificationCandidate(
        field_id="country",
        field_key="country-key",
        field_label=field.name,
        field_type=field.field_type,
        required=True,
        section=field.section,
        widget_kind="custom_select",
        expected_value="United States of America",
        trust_tier=VisualTrustTier.TIER_A,
        verification_mode=VisualVerificationMode.EXACT_VISIBLE_VALUE,
    )
    visual_result = VisualVerificationBatchResult(
        attempted=True,
        page_context_key="ctx",
        model_name="gemini-3-flash-preview",
        candidate_count=1,
        calls=1,
        verified_count=1,
        input_tokens=1200,
        output_tokens=100,
        estimated_cost_usd=0.0009,
        results=[
            VisualVerificationFieldOutcome(
                field_id="country",
                field_key="country-key",
                field_label=field.name,
                field_type=field.field_type,
                expected_value="United States of America",
                observed_value="United States of America",
                required=True,
                trust_tier=VisualTrustTier.TIER_A,
                verification_mode=VisualVerificationMode.EXACT_VISIBLE_VALUE,
                matches_expected=True,
                confidence=0.98,
                status="verified",
            )
        ],
    )

    async def evaluate_side_effect(script, *args):
        if args == (["country"],):
            return _layout_payload("country")
        if args == ("country",):
            return json.dumps({"error_text": "", "widget_kind": "custom_select"})
        return _page_scan_payload("My Information")

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect)
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_assess_state.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch(
            "ghosthands.actions.domhand_assess_state._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch("ghosthands.actions.domhand_assess_state._get_page_context_key", AsyncMock(return_value="ctx")),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="")),
        patch("ghosthands.actions.domhand_assess_state.build_visual_candidates", return_value=[candidate]),
        patch(
            "ghosthands.actions.domhand_assess_state.verify_page_visual_candidates",
            AsyncMock(return_value=visual_result),
        ),
    ):
        result = await domhand_assess_state(DomHandAssessStateParams(target_section="My Information"), browser_session)

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is True
    assert payload["unresolved_required_fields"] == []
    assert payload["unverified_fields"] == []
    assert payload["visual_verification"]["verified_count"] == 1


@pytest.mark.asyncio
async def test_assess_state_visual_tier_a_mismatch_blocks_required_field():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state

    field = FormField(
        field_id="work-auth",
        name="Are you legally authorized to work in the United States?",
        field_type="button-group",
        section="Application Questions",
        required=True,
        choices=["Yes", "No"],
    )
    candidate = VisualVerificationCandidate(
        field_id="work-auth",
        field_key="work-auth-key",
        field_label=field.name,
        field_type=field.field_type,
        required=True,
        section=field.section,
        expected_value="Yes",
        trust_tier=VisualTrustTier.TIER_A,
        verification_mode=VisualVerificationMode.EXACT_VISIBLE_VALUE,
    )
    visual_result = VisualVerificationBatchResult(
        attempted=True,
        page_context_key="ctx",
        model_name="gemini-3-flash-preview",
        candidate_count=1,
        calls=1,
        mismatch_count=1,
        results=[
            VisualVerificationFieldOutcome(
                field_id="work-auth",
                field_key="work-auth-key",
                field_label=field.name,
                field_type=field.field_type,
                expected_value="Yes",
                observed_value="No",
                required=True,
                trust_tier=VisualTrustTier.TIER_A,
                verification_mode=VisualVerificationMode.EXACT_VISIBLE_VALUE,
                matches_expected=False,
                confidence=0.97,
                status="mismatch",
            )
        ],
    )

    async def evaluate_side_effect(script, *args):
        if args == (["work-auth"],):
            return _layout_payload("work-auth")
        if args == ("work-auth",):
            return json.dumps({"error_text": "", "widget_kind": ""})
        return _page_scan_payload()

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect)
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_assess_state.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch(
            "ghosthands.actions.domhand_assess_state._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch("ghosthands.actions.domhand_assess_state._get_page_context_key", AsyncMock(return_value="ctx")),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_group_selection", AsyncMock(return_value="Yes")),
        patch("ghosthands.actions.domhand_assess_state.build_visual_candidates", return_value=[candidate]),
        patch(
            "ghosthands.actions.domhand_assess_state.verify_page_visual_candidates",
            AsyncMock(return_value=visual_result),
        ),
    ):
        result = await domhand_assess_state(DomHandAssessStateParams(target_section=None), browser_session)

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is False
    assert len(payload["unresolved_required_fields"]) == 1
    assert payload["unresolved_required_fields"][0]["reason"] == "visual_mismatch"


@pytest.mark.asyncio
async def test_assess_state_visual_tier_b_mismatch_is_ignored_when_dom_has_no_issue():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state

    field = FormField(
        field_id="email",
        name="Email Address",
        field_type="email",
        section="My Information",
        required=True,
    )
    candidate = VisualVerificationCandidate(
        field_id="email",
        field_key="email-key",
        field_label=field.name,
        field_type=field.field_type,
        required=True,
        section=field.section,
        expected_value="spencer@example.com",
        trust_tier=VisualTrustTier.TIER_B,
        verification_mode=VisualVerificationMode.EXACT_VISIBLE_VALUE,
    )
    visual_result = VisualVerificationBatchResult(
        attempted=True,
        page_context_key="ctx",
        model_name="gemini-3-flash-preview",
        candidate_count=1,
        calls=1,
        mismatch_count=1,
        results=[
            VisualVerificationFieldOutcome(
                field_id="email",
                field_key="email-key",
                field_label=field.name,
                field_type=field.field_type,
                expected_value="spencer@example.com",
                observed_value="spencery@example.com",
                required=True,
                trust_tier=VisualTrustTier.TIER_B,
                verification_mode=VisualVerificationMode.EXACT_VISIBLE_VALUE,
                matches_expected=False,
                confidence=0.96,
                status="mismatch",
            )
        ],
    )

    async def evaluate_side_effect(script, *args):
        if args == (["email"],):
            return _layout_payload("email")
        if args == ("email",):
            return json.dumps({"error_text": "", "widget_kind": ""})
        return _page_scan_payload("My Information")

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect)
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_assess_state.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch(
            "ghosthands.actions.domhand_assess_state._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch("ghosthands.actions.domhand_assess_state._get_page_context_key", AsyncMock(return_value="ctx")),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch(
            "ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="spencer@example.com")
        ),
        patch("ghosthands.actions.domhand_assess_state.build_visual_candidates", return_value=[candidate]),
        patch(
            "ghosthands.actions.domhand_assess_state.verify_page_visual_candidates",
            AsyncMock(return_value=visual_result),
        ),
    ):
        result = await domhand_assess_state(DomHandAssessStateParams(target_section="My Information"), browser_session)

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is True
    assert payload["unresolved_required_fields"] == []
    assert payload["mismatched_fields"] == []
    assert payload["unverified_fields"] == []


@pytest.mark.asyncio
async def test_assess_state_visual_tier_b_unfilled_is_ignored_when_dom_has_no_issue():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state

    field = FormField(
        field_id="email",
        name="Email Address",
        field_type="email",
        section="My Information",
        required=True,
    )
    candidate = VisualVerificationCandidate(
        field_id="email",
        field_key="email-key",
        field_label=field.name,
        field_type=field.field_type,
        required=True,
        section=field.section,
        expected_value="spencer@example.com",
        trust_tier=VisualTrustTier.TIER_B,
        verification_mode=VisualVerificationMode.EXACT_VISIBLE_VALUE,
    )
    visual_result = VisualVerificationBatchResult(
        attempted=True,
        page_context_key="ctx",
        model_name="gemini-3-flash-preview",
        candidate_count=1,
        calls=1,
        unfilled_count=1,
        results=[
            VisualVerificationFieldOutcome(
                field_id="email",
                field_key="email-key",
                field_label=field.name,
                field_type=field.field_type,
                expected_value="spencer@example.com",
                observed_value="",
                required=True,
                trust_tier=VisualTrustTier.TIER_B,
                verification_mode=VisualVerificationMode.EXACT_VISIBLE_VALUE,
                matches_expected=False,
                confidence=0.96,
                status="unfilled",
            )
        ],
    )

    async def evaluate_side_effect(script, *args):
        if args == (["email"],):
            return _layout_payload("email")
        if args == ("email",):
            return json.dumps({"error_text": "", "widget_kind": ""})
        return _page_scan_payload("My Information")

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect)
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_assess_state.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch(
            "ghosthands.actions.domhand_assess_state._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch("ghosthands.actions.domhand_assess_state._get_page_context_key", AsyncMock(return_value="ctx")),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch(
            "ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="spencer@example.com")
        ),
        patch("ghosthands.actions.domhand_assess_state.build_visual_candidates", return_value=[candidate]),
        patch(
            "ghosthands.actions.domhand_assess_state.verify_page_visual_candidates",
            AsyncMock(return_value=visual_result),
        ),
    ):
        result = await domhand_assess_state(DomHandAssessStateParams(target_section="My Information"), browser_session)

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is True
    assert payload["unresolved_required_fields"] == []
    assert payload["unverified_fields"] == []
    assert payload["mismatched_fields"] == []


@pytest.mark.asyncio
async def test_assess_state_visual_verification_error_blocks_advancement():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state

    field = FormField(
        field_id="country",
        name="Country / Territory",
        field_type="select",
        section="My Information",
        required=True,
        is_native=False,
        current_value="United States of America",
    )
    candidate = VisualVerificationCandidate(
        field_id="country",
        field_key="country-key",
        field_label=field.name,
        field_type=field.field_type,
        required=True,
        section=field.section,
        expected_value="United States of America",
        trust_tier=VisualTrustTier.TIER_A,
        verification_mode=VisualVerificationMode.EXACT_VISIBLE_VALUE,
    )
    visual_result = VisualVerificationBatchResult(
        attempted=True,
        page_context_key="ctx",
        model_name="gemini-3-flash-preview",
        candidate_count=1,
        error="model unavailable",
    )

    async def evaluate_side_effect(script, *args):
        if args == (["country"],):
            return _layout_payload("country")
        if args == ("country",):
            return json.dumps({"error_text": "", "widget_kind": "custom_select"})
        return _page_scan_payload("My Information")

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect)
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_assess_state.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch(
            "ghosthands.actions.domhand_assess_state._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch("ghosthands.actions.domhand_assess_state._get_page_context_key", AsyncMock(return_value="ctx")),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch(
            "ghosthands.actions.domhand_assess_state._read_field_value",
            AsyncMock(return_value="United States of America"),
        ),
        patch("ghosthands.actions.domhand_assess_state.build_visual_candidates", return_value=[candidate]),
        patch(
            "ghosthands.actions.domhand_assess_state.verify_page_visual_candidates",
            AsyncMock(return_value=visual_result),
        ),
    ):
        result = await domhand_assess_state(DomHandAssessStateParams(target_section="My Information"), browser_session)

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is False
    assert payload["visual_verification"]["error"] == "model unavailable"


@pytest.mark.asyncio
async def test_assess_state_skips_shape_incompatible_visual_candidates():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state

    field = FormField(
        field_id="preferred-name-toggle",
        name="I have a preferred name",
        field_type="checkbox",
        section="My Information",
        required=True,
        current_value="checked",
    )
    candidate = VisualVerificationCandidate(
        field_id="preferred-name-toggle",
        field_key="preferred-name-toggle-key",
        field_label=field.name,
        field_type=field.field_type,
        required=True,
        section=field.section,
        expected_value="Ruiyang",
        trust_tier=VisualTrustTier.TIER_A,
        verification_mode=VisualVerificationMode.EXACT_VISIBLE_VALUE,
    )

    async def evaluate_side_effect(script, *args):
        if args == (["preferred-name-toggle"],):
            return _layout_payload("preferred-name-toggle")
        if args == ("preferred-name-toggle",):
            return json.dumps({"error_text": "", "widget_kind": ""})
        return _page_scan_payload("My Information")

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect)
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    verify_mock = AsyncMock()

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_assess_state.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch(
            "ghosthands.actions.domhand_assess_state._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch("ghosthands.actions.domhand_assess_state._get_page_context_key", AsyncMock(return_value="ctx")),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_binary_state", AsyncMock(return_value=True)),
        patch("ghosthands.actions.domhand_assess_state.build_visual_candidates", return_value=[candidate]),
        patch("ghosthands.actions.domhand_assess_state.verify_page_visual_candidates", verify_mock),
    ):
        result = await domhand_assess_state(DomHandAssessStateParams(target_section="My Information"), browser_session)

    payload = json.loads((result.metadata or {})["application_state_json"])
    verify_mock.assert_not_awaited()
    assert payload["advance_allowed"] is True
    assert payload["visual_verification"] is None


@pytest.mark.asyncio
async def test_assess_state_recomputes_when_pending_assessment_exists_on_same_page():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state

    field = FormField(
        field_id="first-name",
        name="First Name",
        field_type="text",
        section="My Information",
        required=True,
    )

    async def evaluate_side_effect(script, *args):
        if args == (["first-name"],):
            return _layout_payload("first-name")
        if args == ("first-name",):
            return json.dumps({"error_text": "", "widget_kind": ""})
        return _page_scan_payload("My Information")

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect)
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    browser_session._gh_last_application_state = {
        "page_context_key": "ctx",
        "page_url": "https://example.wd1.myworkdayjobs.com/job",
        "advance_allowed": True,
        "unresolved_required_count": 0,
        "optional_validation_count": 0,
        "visible_error_count": 0,
        "mismatched_count": 0,
        "opaque_count": 0,
        "unverified_count": 0,
    }
    browser_session._gh_pending_assessment = {
        "page_context_key": "ctx",
        "page_url": "https://example.wd1.myworkdayjobs.com/job",
        "source_action": "domhand_fill",
    }

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_assess_state.extract_visible_form_fields", AsyncMock(return_value=[field])
        ) as extract_mock,
        patch(
            "ghosthands.actions.domhand_assess_state._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch("ghosthands.actions.domhand_assess_state._get_page_context_key", AsyncMock(return_value="ctx")),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="Spencer")),
        patch("ghosthands.actions.domhand_assess_state.build_visual_candidates", return_value=[]),
    ):
        result = await domhand_assess_state(DomHandAssessStateParams(target_section="My Information"), browser_session)

    payload = json.loads((result.metadata or {})["application_state_json"])
    extract_mock.assert_awaited_once()
    assert payload["advance_allowed"] is True
    assert "unresolved_required_fields" in payload
    assert not hasattr(browser_session, "_gh_pending_assessment")
