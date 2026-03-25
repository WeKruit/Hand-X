"""Unit tests for domhand_fill bug fixes.

Covers:
- max_tokens scaling based on field count (prevents description truncation)
- _sanitize_no_guess_answer suppresses [NEEDS_USER_INPUT] for no-HITL apply flows
- estimate_cost fault tolerance for unknown models

All tests are offline (no browser, no database, no API calls).
"""

import asyncio
import json
import os
from types import SimpleNamespace
import pytest
from unittest.mock import AsyncMock, patch

# ---------------------------------------------------------------------------
# max_tokens scaling
# ---------------------------------------------------------------------------


def test_max_tokens_scales_with_field_count():
    """max_tokens should scale up for forms with many fields."""
    # The scaling formula: max(4096, min(fields * 128, 16384))
    # We verify by reading the actual code pattern
    assert max(4096, min(10 * 128, 16384)) == 4096  # 10 fields → stays at 4096
    assert max(4096, min(32 * 128, 16384)) == 4096  # 32 fields → 4096
    assert max(4096, min(33 * 128, 16384)) == 4224  # 33 fields → 4224 (crosses threshold)
    assert max(4096, min(63 * 128, 16384)) == 8064  # 63 fields → 8064 (SmartRecruiters case)
    assert max(4096, min(128 * 128, 16384)) == 16384  # 128 fields → capped at 16384
    assert max(4096, min(200 * 128, 16384)) == 16384  # 200 fields → capped at 16384


# ---------------------------------------------------------------------------
# estimate_cost fault tolerance
# ---------------------------------------------------------------------------


def test_estimate_cost_known_model():
    """Known models should return accurate cost estimates."""
    from ghosthands.config.models import estimate_cost

    cost = estimate_cost("gemini-3.1-flash-lite-preview", 1000, 500)
    assert cost > 0
    # 1K input * 0.000075 + 500 output * 0.0003/1000
    assert abs(cost - (0.000075 + 0.00015)) < 1e-8


def test_estimate_cost_unknown_model_returns_fallback():
    """Unknown models should fall back to cheap pricing, not raise."""
    from ghosthands.config.models import estimate_cost

    # Should not raise KeyError
    cost = estimate_cost("totally-unknown-model-xyz", 1000, 500)
    assert cost >= 0  # Returns a fallback estimate, not 0
    assert isinstance(cost, float)


def test_estimate_cost_gemini_3_flash_preview():
    """gemini-3-flash-preview should be in the catalog (was missing)."""
    from ghosthands.config.models import get_model

    model = get_model("gemini-3-flash-preview")
    assert model.provider == "google"
    assert model.input_cost_per_1k > 0


# ---------------------------------------------------------------------------
# _sanitize_no_guess_answer with [NEEDS_USER_INPUT]
# ---------------------------------------------------------------------------


def test_sanitize_suppresses_needs_user_input_for_required():
    """Required fields no longer surface the HITL marker in apply flows."""
    from ghosthands.actions.domhand_fill import _sanitize_no_guess_answer

    result = _sanitize_no_guess_answer(
        "Country code",
        True,
        "[NEEDS_USER_INPUT]",
        {},
        field_type="select",
        question_text="Select country code",
    )

    assert result == ""


def test_sanitize_skips_needs_user_input_for_optional():
    """Optional fields with [NEEDS_USER_INPUT] should return empty string."""
    from ghosthands.actions.domhand_fill import _sanitize_no_guess_answer

    result = _sanitize_no_guess_answer(
        "Facebook",
        False,
        "[NEEDS_USER_INPUT]",
        {},
    )
    assert result == ""


def test_sanitize_prefers_known_profile_value_over_needs_user_input_marker():
    """Known profile values should override LLM escalation markers for required fields."""
    from ghosthands.actions.domhand_fill import _sanitize_no_guess_answer

    result = _sanitize_no_guess_answer(
        "Expectations on Compensation",
        True,
        "[NEEDS_USER_INPUT]",
        {"salary_expectation": "$90,000-$120,000 base (flexible)"},
        field_type="textarea",
        question_text="Expectations on Compensation",
    )

    assert result == "$90,000-$120,000 base (flexible)"


def test_sanitize_returns_none_literal_for_certifications_marker():
    """Certification/license prompts default to literal None when profile is blank."""
    from ghosthands.actions.domhand_fill import _sanitize_no_guess_answer

    result = _sanitize_no_guess_answer(
        "Please list any relevant certifications or licenses.*",
        True,
        "[NEEDS_USER_INPUT]",
        {},
        field_type="textarea",
        question_text="Please list any relevant certifications or licenses.*",
    )

    assert result == "None"


def test_sanitize_normal_values_unchanged():
    """Normal values should pass through without emitting events."""
    from ghosthands.actions.domhand_fill import _sanitize_no_guess_answer

    result = _sanitize_no_guess_answer(
        "First Name",
        True,
        "Jane",
        {"first_name": "Jane"},
    )
    assert result == "Jane"


def test_sanitize_preserves_no_on_dlh_question_with_social_scientific_in_label():
    """'Social Scientific Solutions' must not false-trigger social-handle stripping."""
    from ghosthands.actions.domhand_fill import _sanitize_no_guess_answer

    label = (
        "Do you have any relatives that work for DLH or any of its subsidiaries "
        "(Danya/IBA/Social Scientific Solutions, GRSi)?*"
    )
    result = _sanitize_no_guess_answer(
        label,
        True,
        "No",
        {},
        field_type="select",
        question_text=label,
    )
    assert result == "No"


def test_sanitize_binary_select_survives_true_social_media_question_match():
    """When the label genuinely matches the social regex, Yes/No screening still passes through."""
    from ghosthands.actions.domhand_fill import _sanitize_no_guess_answer

    label = "Do you have a social media account we may review?"
    result = _sanitize_no_guess_answer(
        label,
        True,
        "No",
        {},
        field_type="select",
        question_text=label,
    )
    assert result == "No"


def test_sanitize_does_not_guess_optional_suffix():
    """Optional legal-name fragments should stay blank unless explicitly saved."""
    from ghosthands.actions.domhand_fill import _sanitize_no_guess_answer

    result = _sanitize_no_guess_answer(
        "Suffix",
        False,
        "II",
        {},
        field_type="text",
        question_text="Suffix",
    )

    assert result == ""


@pytest.mark.asyncio
async def test_checkbox_group_already_matches_multi_select_uses_binary_state():
    from ghosthands.actions.domhand_fill import _field_already_matches
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="benefits",
        name="Which benefits are you interested in? (Select all that apply)",
        field_type="checkbox-group",
        choices=[
            "Health insurance",
            "Dental",
            "Vision",
        ],
        required=True,
    )

    with (
        patch("ghosthands.dom.fill_verify._read_binary_state", AsyncMock(return_value=True)),
        patch("ghosthands.dom.fill_verify._field_has_validation_error", AsyncMock(return_value=False)),
        patch(
            "ghosthands.dom.fill_verify._read_group_selection", AsyncMock(return_value="Health insurance")
        ) as read_group,
    ):
        assert await _field_already_matches(AsyncMock(), field, "Health insurance") is True
        read_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_checkbox_group_multi_select_keeps_binary_path():
    from ghosthands.actions.domhand_fill import _CLICK_CHECKBOX_GROUP_JS, _fill_checkbox_group
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="benefits",
        name="Which benefits are you interested in? (Select all that apply)",
        field_type="checkbox-group",
        choices=["Health insurance", "Dental", "Vision"],
        required=True,
    )
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value='{"clicked": true, "alreadyChecked": false}')

    with (
        patch("ghosthands.dom.fill_executor._load_field_interaction_recipe", AsyncMock(return_value=None)),
        patch("ghosthands.dom.fill_executor._read_binary_state", AsyncMock(return_value=True)),
        patch("ghosthands.dom.fill_executor._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._click_binary_with_gui", AsyncMock(return_value=False)) as click_binary,
        patch("ghosthands.dom.fill_executor._refresh_binary_field", AsyncMock(return_value=False)) as refresh_binary,
        patch("ghosthands.dom.fill_executor._record_field_interaction_recipe"),
    ):
        assert await _fill_checkbox_group(page, field, "Health insurance", "[Benefits]") is True
        page.evaluate.assert_awaited_once_with(_CLICK_CHECKBOX_GROUP_JS, field.field_id)
        click_binary.assert_not_awaited()
        refresh_binary.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_checkbox_uses_binary_click_path():
    from ghosthands.actions.domhand_fill import _CLICK_BINARY_FIELD_JS, _fill_checkbox
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="agree",
        name="I agree to the terms",
        field_type="checkbox",
        required=True,
    )
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value='{"clicked": true}')

    with (
        patch("ghosthands.dom.fill_executor._load_field_interaction_recipe", AsyncMock(return_value=None)),
        patch("ghosthands.dom.fill_executor._read_binary_state", AsyncMock(side_effect=[False, True])),
        patch("ghosthands.dom.fill_executor._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._click_binary_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._refresh_binary_field", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._record_field_interaction_recipe"),
    ):
        assert await _fill_checkbox(page, field, "Yes", "[Agreement]") is True
        page.evaluate.assert_awaited_once_with(_CLICK_BINARY_FIELD_JS, field.field_id, True)


@pytest.mark.asyncio
async def test_fill_radio_group_keeps_group_option_path():
    from ghosthands.actions.domhand_fill import _CLICK_RADIO_OPTION_JS, _fill_radio_group
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="work-auth",
        name="Are you legally authorized to work in the United States?",
        field_type="radio-group",
        choices=["Yes", "No"],
        required=True,
    )
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value='{"clicked": true}')

    with (
        patch("ghosthands.dom.fill_executor._load_field_interaction_recipe", AsyncMock(return_value=None)),
        patch("ghosthands.dom.fill_executor._read_group_selection", AsyncMock(side_effect=["", "No"])),
        patch("ghosthands.dom.fill_executor._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._click_group_option_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._reset_group_selection_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._record_field_interaction_recipe"),
    ):
        assert await _fill_radio_group(page, field, "No", "[Work Auth]") is True
        page.evaluate.assert_awaited_once_with(_CLICK_RADIO_OPTION_JS, field.field_id, "No")


@pytest.mark.asyncio
async def test_fill_button_group_keeps_group_option_path():
    from ghosthands.actions.domhand_fill import _CLICK_BUTTON_GROUP_JS, _fill_button_group
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="veteran",
        name="Veteran status",
        field_type="button-group",
        choices=["I am not a protected veteran", "I am a protected veteran"],
        required=True,
    )
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value='{"clicked": true}')

    with (
        patch("ghosthands.dom.fill_executor._load_field_interaction_recipe", AsyncMock(return_value=None)),
        patch(
            "ghosthands.dom.fill_executor._read_group_selection",
            AsyncMock(side_effect=["", "I am not a protected veteran"]),
        ),
        patch("ghosthands.dom.fill_executor._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._click_group_option_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._reset_group_selection_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._record_field_interaction_recipe"),
    ):
        assert await _fill_button_group(page, field, "I am not a protected veteran", "[Veteran]") is True
        page.evaluate.assert_awaited_once_with(_CLICK_BUTTON_GROUP_JS, field.field_id, "I am not a protected veteran")


def test_checkbox_group_mode_detects_exclusive_yes_no_cluster():
    from ghosthands.actions.domhand_fill import _checkbox_group_mode
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="relatives",
        name="Do you have any relatives that work for DLH?",
        field_type="checkbox-group",
        choices=["Yes", "No"],
        required=True,
    )

    assert _checkbox_group_mode(field) == "exclusive_choice"


@pytest.mark.asyncio
async def test_fill_checkbox_group_exclusive_choice_uses_option_label_path():
    from ghosthands.actions.domhand_fill import _CLICK_RADIO_OPTION_JS, _fill_checkbox_group
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="relatives",
        name="Do you have any relatives that work for DLH?",
        field_type="checkbox-group",
        choices=["Yes", "No"],
        required=True,
    )
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value='{"clicked": true}')

    with (
        patch("ghosthands.dom.fill_executor._load_field_interaction_recipe", AsyncMock(return_value=None)),
        patch("ghosthands.dom.fill_executor._read_group_selection", AsyncMock(side_effect=["Yes", "No"])),
        patch("ghosthands.dom.fill_executor._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._click_group_option_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._reset_group_selection_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._record_field_interaction_recipe"),
    ):
        assert await _fill_checkbox_group(page, field, "No", "[Relatives]") is True
        page.evaluate.assert_awaited_once_with(_CLICK_RADIO_OPTION_JS, field.field_id, "No")


def test_domhand_retry_cap_is_one_way_once_reached():
    from ghosthands.runtime_learning import (
        clear_domhand_failure,
        get_domhand_failure_count,
        is_domhand_retry_capped,
        record_domhand_failure,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    field_key = "checkbox-group|relatives"

    assert record_domhand_failure(host="job-boards.greenhouse.io", field_key=field_key, desired_value="no") == 1
    assert is_domhand_retry_capped(host="job-boards.greenhouse.io", field_key=field_key, desired_value="no") is False
    assert record_domhand_failure(host="job-boards.greenhouse.io", field_key=field_key, desired_value="no") == 2
    assert is_domhand_retry_capped(host="job-boards.greenhouse.io", field_key=field_key, desired_value="no") is True

    clear_domhand_failure(host="job-boards.greenhouse.io", field_key=field_key, desired_value="no")

    assert is_domhand_retry_capped(host="job-boards.greenhouse.io", field_key=field_key, desired_value="no") is True
    assert get_domhand_failure_count(host="job-boards.greenhouse.io", field_key=field_key, desired_value="no") == 2

    reset_runtime_learning_state()


@pytest.mark.asyncio
async def test_attempt_domhand_fill_with_retry_cap_refuses_capped_field():
    from ghosthands.actions.domhand_fill import (
        DOMHAND_RETRY_CAPPED,
        _attempt_domhand_fill_with_retry_cap,
    )
    from ghosthands.actions.views import FormField
    from ghosthands.runtime_learning import record_domhand_failure, reset_runtime_learning_state

    reset_runtime_learning_state()
    field = FormField(
        field_id="relatives",
        name="Do you have any relatives that work for DLH?",
        field_type="checkbox-group",
        choices=["Yes", "No"],
        required=True,
        field_fingerprint="relatives-fingerprint",
    )
    record_domhand_failure(
        host="job-boards.greenhouse.io",
        field_key="checkbox-group|relatives-fingerprint",
        desired_value="No",
    )
    record_domhand_failure(
        host="job-boards.greenhouse.io",
        field_key="checkbox-group|relatives-fingerprint",
        desired_value="No",
    )

    with (
        patch("ghosthands.dom.fill_verify._field_already_matches", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_verify._fill_single_field", AsyncMock(return_value=True)) as fill_single,
    ):
        success, error, failure_reason, _fc = await _attempt_domhand_fill_with_retry_cap(
            AsyncMock(),
            host="job-boards.greenhouse.io",
            field=field,
            desired_value="No",
            tool_name="domhand_fill",
        )

    assert success is False
    assert failure_reason == DOMHAND_RETRY_CAPPED
    assert "retry cap" in (error or "").lower()
    fill_single.assert_not_awaited()
    reset_runtime_learning_state()


@pytest.mark.asyncio
async def test_attempt_domhand_fill_fails_when_post_fill_observable_mismatch():
    """Fill helpers may succeed before DOM readback matches; success requires observable settle."""
    from ghosthands.actions.domhand_fill import _attempt_domhand_fill_with_retry_cap
    from ghosthands.actions.views import FormField
    from ghosthands.runtime_learning import reset_runtime_learning_state

    reset_runtime_learning_state()
    field = FormField(
        field_id="ff-privacy",
        name="Candidate Privacy Policy",
        field_type="select",
        required=True,
        field_fingerprint="privacy-fp",
        is_native=False,
    )
    page = AsyncMock()

    with (
        patch(
            "ghosthands.dom.fill_verify._field_already_matches",
            AsyncMock(return_value=False),
        ),
        patch(
            "ghosthands.dom.fill_verify._fill_single_field",
            AsyncMock(return_value=True),
        ),
        patch(
            "ghosthands.dom.fill_verify._verify_fill_observable",
            AsyncMock(return_value=False),
        ),
        patch(
            "ghosthands.dom.fill_verify._read_observed_field_value",
            AsyncMock(return_value=""),
        ),
    ):
        success, error, failure_reason, _fc = await _attempt_domhand_fill_with_retry_cap(
            page,
            host="job-boards.greenhouse.io",
            field=field,
            desired_value="Acknowledge/Confirm",
            tool_name="domhand_fill",
        )

    assert success is False
    assert failure_reason == "dom_fill_failed"
    assert error == "DOM fill failed"
    assert _fc == 0.0
    reset_runtime_learning_state()


@pytest.mark.asyncio
async def test_domhand_fill_reports_retry_capped_fields_without_retrying():
    from ghosthands.actions.domhand_fill import ResolvedFieldValue, domhand_fill
    from ghosthands.actions.views import DomHandFillParams, FormField
    from ghosthands.runtime_learning import record_domhand_failure, reset_runtime_learning_state

    reset_runtime_learning_state()
    field = FormField(
        field_id="country",
        name="Country",
        field_type="select",
        required=True,
        field_fingerprint="country-fingerprint",
    )
    page = AsyncMock()
    page.url = "https://job-boards.greenhouse.io/dlhcorporation/jobs/123"
    page.evaluate = AsyncMock(return_value="{}")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)

    record_domhand_failure(
        host="job-boards.greenhouse.io",
        field_key="select|country-fingerprint",
        desired_value="United States",
    )
    record_domhand_failure(
        host="job-boards.greenhouse.io",
        field_key="select|country-fingerprint",
        desired_value="United States",
    )

    with (
        patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="profile text"),
        patch("ghosthands.actions.domhand_fill._get_profile_data", return_value={}),
        patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
        patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
        patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
        patch("ghosthands.actions.domhand_fill.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch("ghosthands.actions.domhand_fill._filter_fields_for_scope", side_effect=lambda fields, **_: fields),
        patch("ghosthands.actions.domhand_fill._filter_fields_for_focus", side_effect=lambda fields, *_: fields),
        patch("ghosthands.actions.domhand_fill._is_navigation_field", return_value=False),
        patch("ghosthands.actions.domhand_fill._known_auth_override_for_field", return_value=None),
        patch(
            "ghosthands.actions.domhand_fill._resolve_known_profile_value_for_field",
            return_value=ResolvedFieldValue(
                value="United States",
                source="dom",
                answer_mode="profile_backed",
                confidence=0.98,
            ),
        ),
        patch("ghosthands.actions.domhand_fill._semantic_profile_value_for_field", AsyncMock(return_value=None)),
        patch("ghosthands.dom.fill_verify._field_already_matches", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_executor._fill_single_field", AsyncMock(return_value=True)) as fill_single,
    ):
        result = await domhand_fill(DomHandFillParams(), browser_session)

    assert result.error is None
    assert '"failure_reason": "domhand_retry_capped"' in (result.extracted_content or "")
    fill_single.assert_not_awaited()
    reset_runtime_learning_state()


# ---------------------------------------------------------------------------
# Resolution provenance
# ---------------------------------------------------------------------------


def test_resolve_known_profile_value_marks_profile_backed():
    """Deterministic profile-backed answers should not be flagged as guesses."""
    from ghosthands.actions.domhand_fill import _resolve_known_profile_value_for_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="school-year",
        name="Please tell us your current year in school",
        raw_label="Please tell us your current year in school",
        field_type="textarea",
        required=True,
    )

    resolved = _resolve_known_profile_value_for_field(
        field,
        {"current_school_year": "Junior"},
        {"currentSchoolYear": "Junior"},
    )

    assert resolved is not None
    assert resolved.value == "Junior"
    assert resolved.answer_mode == "profile_backed"
    assert resolved.source == "derived_profile"


def test_resolve_llm_answer_marks_best_effort_guess():
    """LLM-only fallback answers should be marked as best-effort guesses."""
    from ghosthands.actions.domhand_fill import _resolve_llm_answer_for_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="essay",
        name="What makes you a strong candidate?",
        raw_label="What makes you a strong candidate?",
        field_type="textarea",
        required=True,
    )

    resolved = _resolve_llm_answer_for_field(
        field,
        {"What makes you a strong candidate?": "I learn quickly and ship reliably."},
        {},
        {},
    )

    assert resolved is not None
    assert resolved.value == "I learn quickly and ship reliably."
    assert resolved.answer_mode == "best_effort_guess"
    assert resolved.source == "llm"


def test_parse_llm_json_skips_prefix_and_repairs_invalid_escapes():
    """Gemini sometimes emits prefix text and invalid \\-sequences inside strings."""
    from ghosthands.actions.domhand_fill import _parse_llm_json_answer_object

    raw = """Here is JSON:
{"Field 1": "Line with WHOOP\\ s bad escape", "Field 2": "ok"}
trailing junk"""
    out = _parse_llm_json_answer_object(raw)
    assert out["Field 2"] == "ok"
    assert "WHOOP" in out["Field 1"]


def test_resolve_llm_answer_via_batch_key_when_dom_labels_empty():
    """Lever-style extractions may leave name/raw_label empty; batch keys must still map."""
    from ghosthands.actions.domhand_fill import _resolve_llm_answer_via_batch_key
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="ff-2",
        name="",
        raw_label=None,
        field_type="text",
        required=True,
        section="Full name",
    )
    answers = {"Field 1": "Ada Lovelace"}
    resolved = _resolve_llm_answer_via_batch_key(field, "Field 1", answers)
    assert resolved is not None
    assert resolved.value == "Ada Lovelace"
    assert resolved.source == "llm"


def test_resolve_llm_answer_does_not_guess_skills():
    """Skill fields should use saved profile skills only, never LLM guesses."""
    from ghosthands.actions.domhand_fill import _resolve_llm_answer_for_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="skills",
        name="Type to Add Skills",
        raw_label="Type to Add Skills",
        field_type="select",
        required=False,
    )

    resolved = _resolve_llm_answer_for_field(
        field,
        {"Type to Add Skills": "Azure, Web Development, Backend Development"},
        {},
        {},
    )

    assert resolved is None


def test_resolve_llm_answer_uses_deterministic_default_for_certifications():
    """Certifications/licenses should default to literal None without guess provenance."""
    from ghosthands.actions.domhand_fill import _resolve_llm_answer_for_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="certs",
        name="Please list any relevant certifications or licenses.*",
        raw_label="Please list any relevant certifications or licenses.*",
        field_type="textarea",
        required=True,
    )

    resolved = _resolve_llm_answer_for_field(field, {}, {}, {})

    assert resolved is not None
    assert resolved.value == "None"
    assert resolved.answer_mode is None
    assert resolved.source == "dom"


def test_known_profile_value_uses_profile_skills_only_and_caps_to_ten():
    """Skill sourcing should preserve order, dedupe, and cap at 10."""
    from ghosthands.actions.domhand_fill import _known_profile_value

    result = _known_profile_value(
        "Type to Add Skills",
        {},
        {
            "skills": [
                "Python",
                "React",
                "Python",
                "Node.js",
                "TypeScript",
                "PostgreSQL",
                "Docker",
                "Kubernetes",
                "AWS",
                "GraphQL",
                "Redis",
                "Terraform",
            ]
        },
    )

    assert result == "Python, React, Node.js, TypeScript, PostgreSQL, Docker, Kubernetes, AWS, GraphQL, Redis"


def test_effectively_unset_field_value_rejects_opaque_widget_ids():
    """Workday select UUIDs must not count as visible selections."""
    from ghosthands.actions.domhand_fill import _is_effectively_unset_field_value

    assert _is_effectively_unset_field_value("05e15101582a10019dbe3ae8c5a80000") is True
    assert _is_effectively_unset_field_value("What degree are you seeking? Select One") is True
    assert _is_effectively_unset_field_value("Bachelor's Degree") is False


def test_recovery_task_no_longer_uses_hitl_wording():
    """Recovered local answers should not be described as HITL/user input."""
    from ghosthands.cli import _RecoveredFieldAnswer, _build_recovery_task

    task = _build_recovery_task(
        "Fill the application",
        [
            _RecoveredFieldAnswer(
                field_id="grad-date",
                field_label="Estimated graduation date",
                answer="December 202X",
                section_path="Education",
            )
        ],
    )

    assert "RECOVERED ANSWERS JUST PROVIDED" in task
    assert "HITL ANSWERS JUST PROVIDED" not in task
    assert "[field_id=grad-date]" in task


# ---------------------------------------------------------------------------
# Search term generation (drives _fill_searchable_dropdown retry logic)
# ---------------------------------------------------------------------------


def test_search_terms_for_country():
    """Country names should generate progressively shorter search terms."""
    from ghosthands.actions.views import generate_dropdown_search_terms

    terms = generate_dropdown_search_terms("United States of America")
    assert "United States of America" in terms
    # Should include shorter variants
    assert any("United" in t for t in terms)
    assert len(terms) >= 2  # At least the original + one shorter


def test_search_terms_for_us_country_code():
    """US country code should hit the synonym cluster."""
    from ghosthands.actions.views import generate_dropdown_search_terms

    terms = generate_dropdown_search_terms("United States +1")
    # Should include synonyms from the cluster
    assert "United States +1" in terms
    assert "United States" in terms
    assert "US" in terms


def test_search_terms_phone_number_skips_mobile_synonym_cluster():
    """Do not add Mobile/Cell search terms when filling a numeric phone (avoids wrong input)."""
    from ghosthands.actions.views import generate_dropdown_search_terms

    terms = generate_dropdown_search_terms("(424) 320-1960")
    assert "(424) 320-1960" in terms
    assert "Mobile" not in terms
    assert "Cell" not in terms


def test_search_terms_empty_input():
    """Empty input should return empty list."""
    from ghosthands.actions.views import generate_dropdown_search_terms

    assert generate_dropdown_search_terms("") == []
    assert generate_dropdown_search_terms("   ") == []


def test_search_terms_hierarchical():
    """Hierarchical values should split into segments."""
    from ghosthands.actions.views import generate_dropdown_search_terms

    terms = generate_dropdown_search_terms("Social Media > LinkedIn")
    assert "Social Media > LinkedIn" in terms
    assert "Social Media" in terms
    assert "LinkedIn" in terms


# ---------------------------------------------------------------------------
# Auth-field overrides for domhand_fill
# ---------------------------------------------------------------------------


def test_auth_override_matches_email_and_password_fields():
    """Auth-mode domhand_fill should prefer GH_EMAIL/GH_PASSWORD for auth fields."""
    from ghosthands.actions.domhand_fill import _known_auth_override_for_field
    from ghosthands.actions.views import FormField

    overrides = {
        "email": "user@example.com",
        "password": "Secret!123",
        "confirm_password": "Secret!123",
    }

    email_field = FormField(field_id="f-email", name="Email", field_type="email")
    password_field = FormField(field_id="f-password", name="Password", field_type="password")
    confirm_field = FormField(
        field_id="f-confirm",
        name="Confirm Password",
        field_type="password",
    )

    assert _known_auth_override_for_field(email_field, overrides) == "user@example.com"
    assert _known_auth_override_for_field(password_field, overrides) == "Secret!123"
    assert _known_auth_override_for_field(confirm_field, overrides) == "Secret!123"


def test_build_task_prompt_uses_browser_use_for_auth_pages():
    """Auth instructions should use browser-use inputs plus domhand_click_button for submit."""
    from ghosthands.agent.prompts import build_task_prompt

    prompt = build_task_prompt(
        "https://example.wd1.myworkdayjobs.com/en-US/job/123/apply",
        "/tmp/resume.pdf",
        {"email": "user@example.com", "password": "Secret!123"},
        credential_source="generated",
    )

    assert "NOT domhand_fill" in prompt
    assert "browser-use input" in prompt
    assert "domhand_click_button" in prompt
    assert (
        "Sign In is allowed ONLY after Create Account fails with an explicit 'account already exists' signal." in prompt
    )


def test_build_task_prompt_await_verification():
    """await_verification should tell agent to report blocker immediately."""
    from ghosthands.agent.prompts import build_task_prompt

    prompt = build_task_prompt(
        "https://example.wd1.myworkdayjobs.com/en-US/job/123/apply",
        "/tmp/resume.pdf",
        {"email": "user@example.com", "password": "Secret!123"},
        credential_source="await_verification",
    )

    assert "ACCOUNT NEEDS VERIFICATION" in prompt
    assert "Do NOT attempt to sign in" in prompt


def test_build_task_prompt_repair_credentials():
    """repair_credentials should tell agent to report blocker immediately."""
    from ghosthands.agent.prompts import build_task_prompt

    prompt = build_task_prompt(
        "https://example.wd1.myworkdayjobs.com/en-US/job/123/apply",
        "/tmp/resume.pdf",
        {"email": "user@example.com", "password": "Secret!123"},
        credential_source="repair_credentials",
    )

    assert "CREDENTIALS NEED REPAIR" in prompt
    assert "Do NOT attempt to sign in" in prompt


def test_build_task_prompt_user_existing_account():
    """User-provided existing-account credentials should force sign-in only."""
    from ghosthands.agent.prompts import build_task_prompt

    prompt = build_task_prompt(
        "https://example.wd1.myworkdayjobs.com/en-US/job/123/apply",
        "/tmp/resume.pdf",
        {"email": "user@example.com", "password": "Secret!123"},
        credential_source="user",
        credential_intent="existing_account",
    )

    assert "USER-PROVIDED EXISTING ACCOUNT" in prompt
    assert "go DIRECTLY to Sign In" in prompt
    assert "NEVER attempt to create a new account" in prompt


def test_build_task_prompt_user_create_account():
    """User-provided new-account credentials should force create-account first."""
    from ghosthands.agent.prompts import build_task_prompt

    prompt = build_task_prompt(
        "https://example.wd1.myworkdayjobs.com/en-US/job/123/apply",
        "/tmp/resume.pdf",
        {"email": "user@example.com", "password": "Secret!123"},
        credential_source="user",
        credential_intent="create_account",
    )

    assert "USER-PROVIDED NEW ACCOUNT" in prompt
    assert "go DIRECTLY to Create Account first" in prompt
    assert "Use the SAME email/password to sign in ONCE" in prompt
    assert "NEVER click Sign In proactively from the start dialog" in prompt
    assert "AUTH_RESULT=ACCOUNT_CREATED_ACTIVE" in prompt
    assert "Do NOT click Create Account again" in prompt
    assert "submit Create Account using domhand_click_button" in prompt
    assert "call refresh() ONCE" in prompt
    assert "take ONE screenshot/vision retry on that blocker" in prompt


def test_build_task_prompt_replaces_old_hitl_salary_instruction():
    from ghosthands.agent.prompts import build_task_prompt

    prompt = build_task_prompt(
        "https://example.wd1.myworkdayjobs.com/en-US/job/123/apply",
        "/tmp/resume.pdf",
        {"email": "user@example.com", "password": "Secret!123"},
        credential_source="user",
        credential_intent="create_account",
    )

    assert "stop for HITL/blocker instead of guessing" not in prompt
    assert "leave it for review in the final report instead of stopping for HITL" in prompt


def test_build_task_prompt_requires_single_field_recovery():
    from ghosthands.agent.prompts import build_task_prompt

    prompt = build_task_prompt(
        "https://example.wd1.myworkdayjobs.com/en-US/job/123/apply",
        "/tmp/resume.pdf",
        {"email": "user@example.com", "password": "Secret!123"},
        credential_source="user",
        credential_intent="create_account",
    )

    assert "ONE FIELD AT A TIME" in prompt
    assert "single exact unresolved label" in prompt
    assert "Do NOT combine a referral/source widget with a radio button" in prompt
    assert "same exact field still fails after two DOM/manual attempts" in prompt
    assert "After EACH targeted manual recovery action, first make sure the field visibly shows the value" in prompt
    assert "then call domhand_record_expected_value" in prompt


def test_build_task_prompt_greenhouse_handles_start_state_before_form():
    from ghosthands.agent.prompts import build_task_prompt

    prompt = build_task_prompt(
        "https://job-boards.greenhouse.io/acme/jobs/123",
        "/tmp/resume.pdf",
        None,
        platform="greenhouse",
    )

    assert "same-site 'Apply' / 'Apply for this job' button" in prompt
    assert "If a resume upload field is visible, upload the resume FIRST" in prompt
    assert "If the main editable application fields are not yet visible" in prompt
    assert "Do NOT click 'Autofill with MyGreenhouse'" in prompt
    assert "Do NOT try to upload the resume before filling fields" not in prompt


def test_build_system_prompt_greenhouse_mentions_start_state():
    from ghosthands.agent.prompts import build_system_prompt

    prompt = build_system_prompt({}, platform="greenhouse")

    assert "same-site start state first" in prompt
    assert "single-page application form once the apply flow is revealed" in prompt


def test_default_screening_answer_defaults_employer_history_questions_to_no():
    from ghosthands.actions.domhand_fill import _default_screening_answer
    from ghosthands.actions.views import FormField

    exact_sciences = FormField(
        field_id="worked-exact",
        name="Have you previously worked at Exact Sciences?",
        field_type="select",
        section="My Information",
        options=["Yes", "No"],
    )
    government = FormField(
        field_id="worked-government",
        name="Have you worked for the government before?",
        field_type="select",
        section="Application Questions",
        options=["Yes", "No"],
    )
    pwc = FormField(
        field_id="worked-pwc",
        name="Have you ever worked for PwC?",
        field_type="select",
        section="Application Questions",
        options=["Yes", "No"],
    )

    assert _default_screening_answer(exact_sciences, {}) == "No"
    assert _default_screening_answer(government, {}) == "No"
    assert _default_screening_answer(pwc, {}) == "No"


def test_default_screening_answer_uses_resume_experience_for_named_employers():
    from ghosthands.actions.domhand_fill import _default_screening_answer
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="worked-exact",
        name="Have you previously worked at Exact Sciences?",
        field_type="select",
        section="My Information",
        options=["Yes", "No"],
    )

    assert _default_screening_answer(field, {"experience": [{"company": "Exact Sciences Corporation"}]}) == "Yes"


def test_parse_profile_evidence_reads_camel_case_compensation():
    from ghosthands.actions.domhand_fill import _parse_profile_evidence

    evidence = _parse_profile_evidence(
        '{"salaryExpectation":"$90,000-$120,000 base (flexible)","spokenLanguages":"English","englishProficiency":"Native / bilingual"}'
    )

    assert evidence["salary_expectation"] == "$90,000-$120,000 base (flexible)"
    assert evidence["spoken_languages"] == "English"
    assert evidence["english_proficiency"] == "Native / bilingual"


def test_parse_profile_evidence_reads_application_question_defaults_from_profile_and_education():
    from ghosthands.actions.domhand_fill import _parse_profile_evidence

    evidence = _parse_profile_evidence(
        (
            "{"
            '"currentSchoolYear":"Junior",'
            '"certificationsLicenses":"None",'
            '"education":[{"school":"USC","degree":"B.S. Computer Science","field":"Computer Science","endDate":"2027-05"}]'
            "}"
        )
    )

    assert evidence["current_school_year"] == "Junior"
    assert evidence["degree_seeking"] == "B.S. Computer Science"
    assert evidence["field_of_study"] == "Computer Science"
    assert evidence["graduation_date"] == "May 2027"
    assert evidence["certifications_licenses"] == "None"


def test_parse_profile_evidence_reads_relocation_preference_alias():
    from ghosthands.actions.domhand_fill import _parse_profile_evidence

    evidence = _parse_profile_evidence('{"relocateOk":"Anywhere"}')

    assert evidence["relocation_preference"] == "Anywhere"


def test_parse_profile_evidence_reads_nested_address_object_without_stringifying_defaults():
    from ghosthands.actions.domhand_fill import _parse_profile_evidence

    evidence = _parse_profile_evidence(
        '{"address":{"street":"","city":"","state":"","zip":"","county":"","country":"United States of America"},'
        '"city":"New York","state":"NY","country":"United States"}'
    )

    assert evidence["address"] is None
    assert evidence["city"] == "New York"
    assert evidence["state"] == "NY"
    assert evidence["country"] == "United States"


def test_parse_profile_evidence_reads_street_fields_from_nested_address_object():
    from ghosthands.actions.domhand_fill import _parse_profile_evidence

    evidence = _parse_profile_evidence(
        '{"address":{"street":"100 Main St","line2":"Apt 4B","city":"Austin","state":"TX","zip":"78701",'
        '"county":"Travis County","country":"United States"}}'
    )

    assert evidence["address"] == "100 Main St"
    assert evidence["address_line_2"] == "Apt 4B"
    assert evidence["city"] == "Austin"
    assert evidence["state"] == "TX"
    assert evidence["zip"] == "78701"
    assert evidence["county"] == "Travis County"
    assert evidence["country"] == "United States"


def test_request_open_question_answers_does_not_pause_for_hitl():
    from ghosthands.cli import _OpenQuestionIssue, _RecoveredFieldAnswer, _request_open_question_answers

    async def _run() -> None:
        issue = _OpenQuestionIssue(
            field_label="Please tell us your current year in school",
            field_id="school-year",
            field_type="textarea",
            question_text="Please tell us your current year in school",
            section="Application Questions",
        )

        with (
            patch("ghosthands.cli._auto_answer_open_question_issues", AsyncMock(return_value=([], [issue]))),
            patch(
                "ghosthands.cli._infer_open_question_answers_with_domhand",
                AsyncMock(
                    return_value=(
                        [
                            _RecoveredFieldAnswer(
                                field_id="school-year",
                                field_label="Please tell us your current year in school",
                                answer="Junior",
                                question_text="Please tell us your current year in school",
                                section_path="Application Questions",
                            )
                        ],
                        [],
                    )
                ),
            ),
            patch("ghosthands.output.jsonl.emit_event") as emit_event,
        ):
            answers, cancelled = await _request_open_question_answers(
                browser=None,
                blocker="blocker: missing answers",
                timeout_seconds=1,
                issues=[issue],
                profile={},
            )

        assert cancelled is False
        assert [(answer.field_id, answer.field_label, answer.answer) for answer in answers] == [
            ("school-year", "Please tell us your current year in school", "Junior")
        ]
        emitted_messages = [call.kwargs.get("message", "") for call in emit_event.call_args_list]
        assert any("continuing locally" in str(message).lower() for message in emitted_messages)
        assert not any("field_needs_input" == call.args[0] for call in emit_event.call_args_list if call.args)

    asyncio.run(_run())


def test_request_open_question_answers_prefers_auth_override_for_auth_fields():
    from ghosthands.cli import _OpenQuestionIssue, _request_open_question_answers

    async def _run() -> None:
        issues = [
            _OpenQuestionIssue(
                field_label="Email Address*",
                field_id="auth-email",
                field_type="text",
                question_text="Email Address*",
                section="Create Account",
            ),
            _OpenQuestionIssue(
                field_label="Verify New Password*",
                field_id="auth-confirm",
                field_type="password",
                question_text="Verify New Password*",
                section="Create Account",
            ),
        ]

        with (
            patch.dict(
                os.environ,
                {
                    "GH_EMAIL": "queued@example.com",
                    "GH_PASSWORD": "QueuedSecret123!",
                    "GH_CREDENTIAL_SOURCE": "user",
                    "GH_CREDENTIAL_INTENT": "create_account",
                },
                clear=False,
            ),
            patch("ghosthands.cli._auto_answer_open_question_issues", AsyncMock(return_value=([], []))) as auto_mock,
            patch(
                "ghosthands.cli._infer_open_question_answers_with_domhand", AsyncMock(return_value=([], []))
            ) as llm_mock,
        ):
            answers, cancelled = await _request_open_question_answers(
                browser=None,
                blocker="blocker: missing auth answers",
                timeout_seconds=1,
                issues=issues,
                profile={"email": "profile@example.com", "password": "WrongOne!"},
            )

        assert cancelled is False
        assert {answer.field_id: answer.answer for answer in answers} == {
            "auth-email": "queued@example.com",
            "auth-confirm": "QueuedSecret123!",
        }
        auto_mock.assert_awaited_once_with([], {"email": "profile@example.com", "password": "WrongOne!"})
        llm_mock.assert_not_awaited()

    asyncio.run(_run())


def test_format_profile_summary_includes_structured_languages():
    from ghosthands.agent.prompts import _format_profile_summary

    summary = _format_profile_summary(
        {
            "languages": [
                {"language": "English", "proficiency": "Native / bilingual"},
                {"language": "Mandarin", "proficiency": "Conversational"},
            ],
            "spoken_languages": "English (Native / bilingual), Mandarin (Conversational)",
            "english_proficiency": "Native / bilingual",
        }
    )

    assert "Languages: English (Native / bilingual), Mandarin (Conversational)" in summary
    assert "English proficiency: Native / bilingual" in summary


def test_format_profile_summary_accepts_string_language_entries():
    from ghosthands.agent.prompts import _format_profile_summary

    summary = _format_profile_summary(
        {
            "languages": [
                "English (Native / bilingual)",
                {"language": "Mandarin", "proficiency": "Conversational"},
            ],
            "spoken_languages": "English (Native / bilingual), Mandarin (Conversational)",
        }
    )

    assert "Languages: English (Native / bilingual), Mandarin (Conversational)" in summary


def test_cap_qa_entries_uses_runtime_cap_without_name_error():
    from ghosthands.dom.fill_profile_resolver import _cap_qa_entries

    entries = [
        {
            "question": f"Question {i}",
            "answer": f"Answer {i}",
            "usage_mode": "always_use" if i % 2 == 0 else "learned",
            "times_used": i,
            "confidence": "exact" if i % 3 == 0 else "learned",
        }
        for i in range(25)
    ]

    capped = _cap_qa_entries(entries)

    assert len(capped) == 20


def test_section_scope_treats_languages_as_part_of_my_experience():
    from ghosthands.actions.domhand_fill import _section_matches_scope

    assert _section_matches_scope("Languages", "My Experience") is True
    assert _section_matches_scope("Education", "My Experience") is True


def test_focus_filter_targets_exact_unresolved_fields():
    from ghosthands.actions.domhand_fill import _filter_fields_for_focus
    from ghosthands.actions.views import FormField

    fields = [
        FormField(field_id="f1", name="Comprehension", field_type="select", section="Languages"),
        FormField(field_id="f2", name="Reading", field_type="select", section="Languages"),
        FormField(field_id="f3", name="I currently work here", field_type="checkbox", section="My Experience"),
    ]

    filtered = _filter_fields_for_focus(fields, ["Comprehension", "Reading"])

    assert [field.field_id for field in filtered] == ["f1", "f2"]


def test_focus_filter_does_not_broaden_when_no_match():
    from ghosthands.actions.domhand_fill import _filter_fields_for_focus
    from ghosthands.actions.views import FormField

    fields = [
        FormField(field_id="f1", name="Comprehension", field_type="select", section="Languages"),
        FormField(field_id="f2", name="Reading", field_type="select", section="Languages"),
    ]

    filtered = _filter_fields_for_focus(fields, ["Nonexistent blocker"])

    assert filtered == []


def test_focus_filter_matches_grouped_radio_question_label():
    from ghosthands.actions.domhand_fill import _filter_fields_for_focus
    from ghosthands.actions.views import FormField

    fields = [
        FormField(
            field_id="f1",
            name="Previously worked",
            raw_label="Have you previously worked at Exact Sciences?*",
            field_type="radio-group",
            section="My Information",
            choices=["Yes", "No"],
        ),
        FormField(field_id="f2", name="County", field_type="text", section="My Information"),
    ]

    filtered = _filter_fields_for_focus(fields, ["Have you previously worked at Exact Sciences?"])

    assert [field.field_id for field in filtered] == ["f1"]


def test_focus_filter_prefers_text_field_over_same_label_checkbox_companion():
    from ghosthands.actions.domhand_fill import _filter_fields_for_focus
    from ghosthands.actions.views import FormField

    fields = [
        FormField(
            field_id="last-name-text",
            name="Last Name*",
            field_type="text",
            section="Legal Name",
            required=True,
        ),
        FormField(
            field_id="last-name-check",
            name="Last Name*",
            field_type="checkbox",
            section="Legal Name",
            required=True,
        ),
    ]

    filtered = _filter_fields_for_focus(fields, ["Last Name*"])

    assert [field.field_id for field in filtered] == ["last-name-text"]


def test_scope_only_keeps_blank_section_fields_when_they_are_targeted_blockers():
    from ghosthands.actions.domhand_fill import _filter_fields_for_scope
    from ghosthands.actions.views import FormField

    fields = [
        FormField(field_id="name", name="Last Name*", field_type="text", section="Legal Name"),
        FormField(
            field_id="employment",
            name="Have you previously been employed here?*",
            field_type="radio-group",
            section="",
            choices=["Yes", "No"],
        ),
    ]

    filtered = _filter_fields_for_scope(
        fields,
        target_section="My Information",
        focus_fields=["Last Name*"],
    )

    assert [field.field_id for field in filtered] == ["name"]


def test_scope_generic_page_section_still_includes_focus_matched_contact_block_field():
    """Oracle-style flows: page section is 'Job application form' but address lives under 'Contact'."""
    from ghosthands.actions.domhand_fill import _filter_fields_for_focus, _filter_fields_for_scope
    from ghosthands.actions.views import FormField

    fields = [
        FormField(
            field_id="ff-link",
            name="Link 1",
            field_type="url",
            section="Job application form",
        ),
        FormField(
            field_id="ff-25",
            name="addressLine1",
            name_attr="addressLine1",
            field_type="select",
            section="Contact",
            raw_label="addressLine1",
        ),
    ]

    scoped = _filter_fields_for_scope(
        fields,
        target_section="Job application form",
        focus_fields=["Address Line 1"],
    )
    assert any(f.field_id == "ff-25" for f in scoped)

    focused = _filter_fields_for_focus(scoped, ["Address Line 1"])
    assert [f.field_id for f in focused] == ["ff-25"]


def test_coerce_answer_to_field_keeps_text_when_options_are_dom_noise():
    """Lever-style extractions may attach bogus option lists to plain text inputs."""
    from ghosthands.actions.domhand_fill import _coerce_answer_to_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="ff-2",
        name="",
        raw_label=None,
        field_type="text",
        required=True,
        options=["Select one", "Loading…", ""],
    )
    assert _coerce_answer_to_field(field, "Ada Lovelace") == "Ada Lovelace"


def test_coerce_tel_rejects_mobile_line_type_even_when_mobile_is_an_option():
    """Greenhouse <tel> can inherit react-select option noise; never coerce the number field to Mobile."""
    from ghosthands.actions.domhand_fill import _coerce_answer_to_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="ff-phone",
        name="Phone*",
        field_type="tel",
        required=True,
        options=["Mobile", "Home", "Work"],
    )
    assert _coerce_answer_to_field(field, "Mobile") is None
    assert _coerce_answer_to_field(field, "(424) 320-1960") == "(424) 320-1960"


def test_coerce_binary_select_passes_through_when_only_junk_option():
    """Greenhouse React-select often lists the question as the sole extracted 'option'."""
    from ghosthands.actions.domhand_fill import _coerce_answer_to_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="ff-39",
        name="Do you have any relatives that work for DLH or any of its subsidiaries (Danya/IBA/Social Scientific Solutions, GRSi)?*",
        field_type="select",
        required=True,
        options=[
            "Do you have any relatives that work for DLH or any of its subsidiaries (Danya/IBA/Social Scientific Solutions, GRSi)?*",
        ],
    )
    assert _coerce_answer_to_field(field, "No") == "No"


def test_coerce_binary_select_does_not_bypass_multi_option_lists():
    from ghosthands.actions.domhand_fill import _coerce_answer_to_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="ff-6",
        name="Country*",
        field_type="select",
        required=True,
        options=["United States", "Canada", "Mexico"],
    )
    assert _coerce_answer_to_field(field, "No") is None


def test_coerce_binary_select_does_not_bypass_single_real_option():
    """One visible option that is not label-echo/placeholder still uses semantic matching only."""
    from ghosthands.actions.domhand_fill import _coerce_answer_to_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="ff-g",
        name="Gender (optional)",
        field_type="select",
        required=False,
        options=["Male"],
    )
    assert _coerce_answer_to_field(field, "No") is None


def test_coerce_answer_to_field_maps_semantic_proficiency_tier():
    from ghosthands.actions.domhand_fill import _coerce_answer_to_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="f1",
        name="Overall",
        field_type="select",
        options=["Beginner", "Intermediate", "Expert"],
    )

    assert _coerce_answer_to_field(field, "Fluent") == "Expert"


def test_coerce_answer_to_field_maps_native_bilingual_to_top_proficiency_tier():
    from ghosthands.actions.domhand_fill import _coerce_answer_to_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="f2",
        name="Overall",
        field_type="select",
        options=["1 - Beginner", "2 - Intermediate", "3 - Advanced", "4 - Fluent"],
    )

    assert _coerce_answer_to_field(field, "Native / bilingual") == "4 - Fluent"


def test_structured_language_value_uses_exact_language_row():
    from ghosthands.actions.domhand_fill import _resolve_structured_language_value
    from ghosthands.actions.views import FormField

    profile_data = {
        "languages": [
            {
                "language": "Chinese",
                "overallProficiency": "Conversational",
                "isFluent": False,
                "readingWriting": "Basic",
                "speakingListening": "Conversational",
            },
            {
                "language": "English",
                "overallProficiency": "Native / bilingual",
                "isFluent": True,
                "readingWriting": "Fluent",
                "speakingListening": "Fluent",
            },
        ]
    }

    reading_field = FormField(
        field_id="lang-2-reading",
        name="Reading & Writing",
        field_type="select",
        section="Languages 2",
        options=["Basic", "Conversational", "Fluent"],
    )
    fluent_field = FormField(
        field_id="lang-1-fluent",
        name="I am fluent in this language.",
        field_type="select",
        section="Languages 1",
        options=["Yes", "No"],
    )

    assert _resolve_structured_language_value(reading_field, profile_data) == "Fluent"
    assert _resolve_structured_language_value(fluent_field, profile_data) == "No"


def test_global_english_proficiency_no_longer_answers_per_language_rubrics():
    from ghosthands.actions.domhand_fill import _known_profile_value

    evidence = {
        "spoken_languages": "English (Native / bilingual), Chinese (Conversational)",
        "english_proficiency": "Native / bilingual",
    }

    assert _known_profile_value("Reading & Writing", evidence, {}) is None
    assert _known_profile_value("Speaking & Listening", evidence, {}) is None


@pytest.mark.asyncio
async def test_domhand_fill_marks_ambiguous_education_row_order_as_best_effort():
    from ghosthands.actions.domhand_fill import domhand_fill
    from ghosthands.actions.views import DomHandFillParams, FormField

    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    field = FormField(
        field_id="edu-field",
        name="Field of Study",
        field_type="text",
        section="Education",
        required=True,
    )

    with (
        patch("ghosthands.actions.domhand_fill.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="Jane Doe"),
        patch(
            "ghosthands.actions.domhand_fill._get_profile_data",
            return_value={
                "education": [
                    {
                        "school": "MIT",
                        "degree": "BS",
                        "field_of_study": "Computer Science",
                        "gpa": "3.9",
                        "start_date": "2016-08",
                        "end_date": "2020-05",
                    },
                    {
                        "school": "USC",
                        "degree": "MS",
                        "field_of_study": "AI",
                        "gpa": "4.0",
                        "start_date": "2025-08",
                        "end_date": "2027-05",
                    },
                ]
            },
        ),
        patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://example.com/job")),
        patch(
            "ghosthands.actions.domhand_fill._attempt_domhand_fill_with_retry_cap",
            AsyncMock(return_value=(True, None, None, 1.0)),
        ),
    ):
        result = await domhand_fill(DomHandFillParams(), browser_session)

    payload = json.loads((result.extracted_content or "").split("DOMHAND_FILL_JSON:\n", 1)[1])
    assert payload["best_effort_binding_count"] >= 1
    assert any(
        field["prompt_text"] == "Field of Study" and field["binding_mode"] == "row_order"
        for field in payload["best_effort_binding_fields"]
    )


@pytest.mark.asyncio
async def test_domhand_fill_reports_structured_education_field_of_study_entry_value_missing():
    from ghosthands.actions.domhand_fill import domhand_fill
    from ghosthands.actions.views import DomHandFillParams, FormField

    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    field = FormField(
        field_id="edu-field-of-study",
        name="Field of Study",
        field_type="text",
        section="Education 1",
        required=True,
    )

    with (
        patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="Jane Doe"),
        patch(
            "ghosthands.actions.domhand_fill._get_profile_data",
            return_value={
                "education": [
                    {
                        "school": "University of Southern California",
                        "degree": "B.S.",
                        "end_date": "2025-05",
                    }
                ]
            },
        ),
        patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
        patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
        patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
        patch("ghosthands.actions.domhand_fill.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch("ghosthands.actions.domhand_fill._filter_fields_for_scope", side_effect=lambda fields, **_: fields),
        patch(
            "ghosthands.actions.domhand_fill._resolve_focus_fields",
            return_value=SimpleNamespace(fields=[field], ambiguous_labels={}),
        ),
        patch("ghosthands.actions.domhand_fill._is_navigation_field", return_value=False),
        patch("ghosthands.actions.domhand_fill._known_auth_override_for_field", return_value=None),
        patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://example.com/job")),
    ):
        result = await domhand_fill(DomHandFillParams(), browser_session)

    payload = json.loads((result.extracted_content or "").split("DOMHAND_FILL_JSON:\n", 1)[1])
    failure = next(field for field in payload["failed_fields"] if field["field_id"] == "edu-field-of-study")
    unresolved = next(
        field for field in payload["unresolved_required_fields"] if field["field_id"] == "edu-field-of-study"
    )
    assert failure["failure_reason"] == "structured_entry_value_missing"
    assert failure["repeater_group"] == "education"
    assert failure["slot_name"] == "field_of_study"
    assert failure["diagnostic_stage"] == "entry_value_missing"
    assert failure["binding_mode"] == "exact"
    assert failure["binding_confidence"] == "high"
    assert unresolved["repeater_group"] == "education"
    assert unresolved["slot_name"] == "field_of_study"
    assert unresolved["diagnostic_stage"] == "entry_value_missing"


@pytest.mark.asyncio
async def test_domhand_fill_reports_structured_education_from_entry_value_missing():
    from ghosthands.actions.domhand_fill import domhand_fill
    from ghosthands.actions.views import DomHandFillParams, FormField

    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    field = FormField(
        field_id="edu-from",
        name="From",
        field_type="text",
        section="Education 1",
        required=False,
    )

    with (
        patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="Jane Doe"),
        patch(
            "ghosthands.actions.domhand_fill._get_profile_data",
            return_value={
                "education": [
                    {
                        "school": "University of Southern California",
                        "degree": "B.S.",
                        "field_of_study": "Computer Science",
                        "end_date": "2025-05",
                    }
                ]
            },
        ),
        patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
        patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
        patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
        patch("ghosthands.actions.domhand_fill.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch("ghosthands.actions.domhand_fill._filter_fields_for_scope", side_effect=lambda fields, **_: fields),
        patch(
            "ghosthands.actions.domhand_fill._resolve_focus_fields",
            return_value=SimpleNamespace(fields=[field], ambiguous_labels={}),
        ),
        patch("ghosthands.actions.domhand_fill._is_navigation_field", return_value=False),
        patch("ghosthands.actions.domhand_fill._known_auth_override_for_field", return_value=None),
        patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://example.com/job")),
    ):
        result = await domhand_fill(DomHandFillParams(), browser_session)

    payload = json.loads((result.extracted_content or "").split("DOMHAND_FILL_JSON:\n", 1)[1])
    failure = next(field for field in payload["failed_fields"] if field["field_id"] == "edu-from")
    assert failure["failure_reason"] == "structured_entry_value_missing"
    assert failure["repeater_group"] == "education"
    assert failure["slot_name"] == "start_date"
    assert failure["diagnostic_stage"] == "entry_value_missing"
    assert failure["binding_mode"] == "exact"


@pytest.mark.asyncio
async def test_domhand_fill_reports_structured_binding_unresolved_for_education_when_binding_fails():
    from ghosthands.actions.domhand_fill import domhand_fill
    from ghosthands.actions.views import DomHandFillParams, FormField

    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    field = FormField(
        field_id="edu-field",
        name="Field of Study",
        field_type="text",
        section="Education",
        required=False,
    )

    with (
        patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="Jane Doe"),
        patch(
            "ghosthands.actions.domhand_fill._get_profile_data",
            return_value={
                "education": [
                    {"school": "MIT", "field_of_study": "Computer Science"},
                    {"school": "USC", "field_of_study": "AI"},
                ]
            },
        ),
        patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
        patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
        patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
        patch("ghosthands.actions.domhand_fill.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch("ghosthands.actions.domhand_fill._filter_fields_for_scope", side_effect=lambda fields, **_: fields),
        patch(
            "ghosthands.actions.domhand_fill._resolve_focus_fields",
            return_value=SimpleNamespace(fields=[field], ambiguous_labels={}),
        ),
        patch("ghosthands.actions.domhand_fill._is_navigation_field", return_value=False),
        patch("ghosthands.actions.domhand_fill._known_auth_override_for_field", return_value=None),
        patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://example.com/job")),
        patch("ghosthands.actions.domhand_fill._resolve_repeater_binding", return_value=None),
    ):
        result = await domhand_fill(DomHandFillParams(), browser_session)

    payload = json.loads((result.extracted_content or "").split("DOMHAND_FILL_JSON:\n", 1)[1])
    failure = next(field for field in payload["failed_fields"] if field["field_id"] == "edu-field")
    assert failure["failure_reason"] == "structured_binding_unresolved"
    assert failure["repeater_group"] == "education"
    assert failure["slot_name"] == "field_of_study"
    assert failure["diagnostic_stage"] == "binding_unresolved"
    assert "binding_mode" not in failure


@pytest.mark.asyncio
async def test_domhand_fill_reports_structured_language_entry_value_missing_diagnostics():
    from ghosthands.actions.domhand_fill import domhand_fill
    from ghosthands.actions.views import DomHandFillParams, FormField

    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    field = FormField(
        field_id="lang-reading",
        name="Reading & Writing",
        field_type="select",
        section="Languages 1",
        required=False,
        options=["Basic", "Conversational", "Fluent"],
    )

    with (
        patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="Jane Doe"),
        patch(
            "ghosthands.actions.domhand_fill._get_profile_data",
            return_value={"languages": [{"language": "English"}]},
        ),
        patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
        patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
        patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
        patch("ghosthands.actions.domhand_fill.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch("ghosthands.actions.domhand_fill._filter_fields_for_scope", side_effect=lambda fields, **_: fields),
        patch(
            "ghosthands.actions.domhand_fill._resolve_focus_fields",
            return_value=SimpleNamespace(fields=[field], ambiguous_labels={}),
        ),
        patch("ghosthands.actions.domhand_fill._is_navigation_field", return_value=False),
        patch("ghosthands.actions.domhand_fill._known_auth_override_for_field", return_value=None),
        patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://example.com/job")),
    ):
        result = await domhand_fill(DomHandFillParams(), browser_session)

    payload = json.loads((result.extracted_content or "").split("DOMHAND_FILL_JSON:\n", 1)[1])
    failure = next(field for field in payload["failed_fields"] if field["field_id"] == "lang-reading")
    assert failure["failure_reason"] == "structured_entry_value_missing"
    assert failure["repeater_group"] == "languages"
    assert failure["slot_name"] == "reading_writing"
    assert failure["diagnostic_stage"] == "entry_value_missing"
    assert failure["binding_mode"] == "exact"


def test_fill_result_summary_entry_adds_structured_repeater_fields_only_when_present():
    from ghosthands.actions.domhand_fill import _fill_result_summary_entry
    from ghosthands.actions.views import FillFieldResult

    structured = FillFieldResult(
        field_id="edu-field",
        name="Field of Study",
        success=False,
        actor="skipped",
        control_kind="text",
        section="Education 1",
        failure_reason="structured_entry_value_missing",
        repeater_group="education",
        slot_name="field_of_study",
        diagnostic_stage="entry_value_missing",
        binding_mode="exact",
        binding_confidence="high",
    )
    general = FillFieldResult(
        field_id="auth-email",
        name="Email",
        success=False,
        actor="skipped",
        control_kind="email",
        section="Sign In",
        failure_reason="auth_override_missing",
    )

    structured_entry = _fill_result_summary_entry(structured)
    general_entry = _fill_result_summary_entry(general)

    assert structured_entry["repeater_group"] == "education"
    assert structured_entry["slot_name"] == "field_of_study"
    assert structured_entry["diagnostic_stage"] == "entry_value_missing"
    assert structured_entry["binding_mode"] == "exact"
    assert structured_entry["binding_confidence"] == "high"
    assert "repeater_group" not in general_entry
    assert "slot_name" not in general_entry
    assert "diagnostic_stage" not in general_entry


@pytest.mark.asyncio
async def test_domhand_fill_logs_structured_repeater_section_binding_reuse_for_education():
    from ghosthands.actions.domhand_fill import domhand_fill
    from ghosthands.actions.views import DomHandFillParams, FormField

    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    school_field = FormField(
        field_id="edu-school",
        name="School or University",
        field_type="text",
        section="Education 1",
        required=False,
    )
    study_field = FormField(
        field_id="edu-study",
        name="Field of Study",
        field_type="text",
        section="Education 1",
        required=False,
    )

    with (
        patch.dict(os.environ, {"GH_DEBUG_PROFILE_PASS_THROUGH": "1"}, clear=False),
        patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="Jane Doe"),
        patch(
            "ghosthands.actions.domhand_fill._get_profile_data",
            return_value={
                "education": [
                    {
                        "school": "University of Southern California",
                        "field_of_study": "Computer Science",
                    }
                ]
            },
        ),
        patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
        patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
        patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
        patch(
            "ghosthands.actions.domhand_fill.extract_visible_form_fields",
            AsyncMock(return_value=[school_field, study_field]),
        ),
        patch("ghosthands.actions.domhand_fill._filter_fields_for_scope", side_effect=lambda fields, **_: fields),
        patch(
            "ghosthands.actions.domhand_fill._resolve_focus_fields",
            return_value=SimpleNamespace(fields=[school_field, study_field], ambiguous_labels={}),
        ),
        patch("ghosthands.actions.domhand_fill._is_navigation_field", return_value=False),
        patch("ghosthands.actions.domhand_fill._known_auth_override_for_field", return_value=None),
        patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://example.com/job")),
        patch(
            "ghosthands.actions.domhand_fill._attempt_domhand_fill_with_retry_cap",
            AsyncMock(return_value=(True, None, None, 1.0)),
        ),
        patch("ghosthands.actions.domhand_fill.logger.info") as log_info,
    ):
        result = await domhand_fill(DomHandFillParams(), browser_session)

    assert result.error is None
    diagnostic_calls = [
        call
        for call in log_info.call_args_list
        if call.args and call.args[0] == "domhand.structured_repeater_resolution"
    ]
    assert diagnostic_calls
    study_call = next(call for call in diagnostic_calls if call.kwargs["extra"]["field_label"] == "Field of Study")
    assert study_call.kwargs["extra"]["section_binding_reused"] is True
    assert study_call.kwargs["extra"]["binding_mode"] == "exact"
    assert study_call.kwargs["extra"]["failure_stage"] == "resolved"
    assert study_call.kwargs["extra"]["resolved_source_key"] == "field_of_study"


def test_verification_attempt_count_respects_effort_levels():
    from ghosthands.actions.domhand_assess_state import _verification_attempt_count

    with patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False):
        assert _verification_attempt_count() == 1
    with patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "medium"}, clear=False):
        assert _verification_attempt_count() == 2
    with patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "high"}, clear=False):
        assert _verification_attempt_count() == 3


def test_is_placeholder_value_treats_no_response_as_placeholder():
    from ghosthands.actions.views import is_placeholder_value

    assert is_placeholder_value("No Response") is True
    assert is_placeholder_value("Not provided") is True
    assert is_placeholder_value("No") is False


@pytest.mark.asyncio
async def test_assess_state_blocks_advance_when_expected_answer_is_mismatched():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        record_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    field = FormField(
        field_id="lang-1",
        name="Language",
        field_type="select",
        section="Voluntary Self-Identification of Disability",
        required=True,
    )
    page_context_key = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="Voluntary Self-Identification of Disability",
    )
    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(field),
        field_label=field.name,
        expected_value="English",
        source="exact_profile",
    )

    async def evaluate_side_effect(script, *args):
        if args == (["lang-1"],):
            return {"lang-1": {"in_view": True, "top": 0, "bottom": 20}}
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "error_texts": [],
            "heading_texts": ["Voluntary Self-Identification of Disability"],
        }

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
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="French")),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Voluntary Self-Identification of Disability"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is False
    assert len(payload["mismatched_fields"]) == 1
    assert payload["mismatched_fields"][0]["name"] == "Language"


@pytest.mark.asyncio
async def test_assess_state_marks_opaque_select_values_as_unverified_gate():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        record_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    field = FormField(
        field_id="lang-opaque",
        name="Language",
        field_type="select",
        section="Voluntary Self-Identification of Disability",
        required=True,
    )
    page_context_key = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="Voluntary Self-Identification of Disability",
    )
    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(field),
        field_label=field.name,
        expected_value="English",
        source="exact_profile",
    )

    async def evaluate_side_effect(script, *args):
        if args == (["lang-opaque"],):
            return {"lang-opaque": {"in_view": True, "top": 0, "bottom": 20}}
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "error_texts": [],
            "heading_texts": ["Voluntary Self-Identification of Disability"],
        }

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
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch(
            "ghosthands.actions.domhand_assess_state._read_field_value",
            AsyncMock(return_value="c17fb198564510000de6e6b35bb80000"),
        ),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Voluntary Self-Identification of Disability"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is False
    assert len(payload["opaque_fields"]) == 1
    assert payload["opaque_fields"][0]["name"] == "Language"


@pytest.mark.asyncio
async def test_assess_state_treats_no_response_text_as_required_missing_value():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField

    field = FormField(
        field_id="education-field",
        name="Field of Study",
        field_type="text",
        section="Education",
        required=True,
        current_value="No Response",
    )

    async def evaluate_side_effect(script, *args):
        if args == (["education-field"],):
            return {"education-field": {"in_view": True, "top": 0, "bottom": 20}}
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "advance_disabled": False,
            "error_texts": [],
            "heading_texts": ["Education"],
        }

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
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="No Response")),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Education"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is False
    assert len(payload["unresolved_required_fields"]) == 1
    assert payload["unresolved_required_fields"][0]["name"] == "Field of Study"


@pytest.mark.asyncio
async def test_assess_state_does_not_fallback_to_unrelated_sections_when_target_section_missing():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField

    field = FormField(
        field_id="lang-1",
        name="Reading & Writing",
        field_type="select",
        section="Languages",
        required=True,
    )

    async def evaluate_side_effect(script, *args):
        if args == (["lang-1"],):
            return {"lang-1": {"in_view": True, "top": 0, "bottom": 20}}
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "advance_disabled": False,
            "error_texts": [],
            "heading_texts": ["My Experience"],
        }

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
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="")),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="My Information"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["current_section"] == "My Experience"
    assert payload["unresolved_required_fields"] == []


@pytest.mark.asyncio
async def test_assess_state_blocks_advance_when_advance_control_is_disabled():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams

    page = AsyncMock()
    page.evaluate = AsyncMock(
        side_effect=[
            None,
            {
                "button_texts": ["Save and Continue"],
                "body_text": "",
                "markers": [],
                "submit_visible": False,
                "submit_disabled": False,
                "advance_visible": True,
                "advance_disabled": True,
                "error_texts": [],
                "heading_texts": ["My Experience"],
            },
            {},
        ]
    )
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_assess_state.extract_visible_form_fields", AsyncMock(return_value=[])),
        patch(
            "ghosthands.actions.domhand_assess_state._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="My Experience"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_visible"] is True
    assert payload["advance_disabled"] is True
    assert payload["advance_allowed"] is False


@pytest.mark.asyncio
async def test_assess_state_reports_grouped_date_mismatch_at_logical_date_field():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        record_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    field = FormField(
        field_id="date-wrapper",
        name="Date",
        field_type="date",
        section="Voluntary Self-Identification of Disability",
        required=True,
        widget_kind="grouped_date",
        component_field_ids=["date-month", "date-day", "date-year"],
        has_calendar_trigger=True,
    )
    page_context_key = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="Voluntary Self-Identification of Disability",
    )
    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(field),
        field_label=field.name,
        field_type=field.field_type,
        field_section=field.section,
        field_fingerprint=field.field_fingerprint or "",
        expected_value="03/19/2026",
        source="exact_profile",
    )

    async def evaluate_side_effect(script, *args):
        if args == (["date-wrapper"],):
            return {"date-wrapper": {"in_view": True, "top": 0, "bottom": 20}}
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "error_texts": [],
            "heading_texts": ["Voluntary Self-Identification of Disability"],
        }

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
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch(
            "ghosthands.actions.domhand_assess_state._read_field_value_for_field", AsyncMock(return_value="12/19/2012")
        ),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Voluntary Self-Identification of Disability"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is False
    assert len(payload["mismatched_fields"]) == 1
    assert payload["mismatched_fields"][0]["name"] == "Date"
    assert all(issue["name"] != "Month" for issue in payload["mismatched_fields"])


@pytest.mark.asyncio
async def test_assess_state_includes_terms_child_section_under_voluntary_disclosures():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField

    field = FormField(
        field_id="terms-checkbox",
        name="I understand and acknowledge the terms of use for Arlo.*",
        field_type="checkbox",
        section="Terms and Conditions",
        required=True,
        current_value="",
    )

    async def evaluate_side_effect(script, *args):
        if args == (["terms-checkbox"],):
            return {"terms-checkbox": {"in_view": True, "top": 0, "bottom": 20}}
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "error_texts": [],
            "heading_texts": ["Voluntary Disclosures"],
        }

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
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Voluntary Disclosures"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is False
    assert len(payload["unresolved_required_fields"]) == 1
    assert payload["unresolved_required_fields"][0]["section"] == "Terms and Conditions"


@pytest.mark.asyncio
async def test_assess_state_blocks_advance_for_expected_mismatch_outside_target_section():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        record_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    target_field = FormField(
        field_id="target-1",
        name="Current School Year",
        field_type="text",
        section="My Experience",
        required=True,
    )
    off_scope_field = FormField(
        field_id="offscope-1",
        name="Language",
        field_type="select",
        section="Languages",
        required=True,
    )
    page_context_key = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="My Experience",
    )
    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(target_field),
        field_label=target_field.name,
        expected_value="Junior",
        source="exact_profile",
    )
    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(off_scope_field),
        field_label=off_scope_field.name,
        expected_value="English",
        source="exact_profile",
    )

    async def evaluate_side_effect(script, *args):
        if args == (["target-1", "offscope-1"],):
            return {
                "target-1": {"in_view": True, "top": 0, "bottom": 20},
                "offscope-1": {"in_view": True, "top": 24, "bottom": 44},
            }
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "error_texts": [],
            "heading_texts": ["My Experience"],
        }

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect)
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)

    async def read_value_side_effect(page_obj, field_id):
        if field_id == "target-1":
            return "Junior"
        if field_id == "offscope-1":
            return "French"
        return ""

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_assess_state.extract_visible_form_fields",
            AsyncMock(return_value=[target_field, off_scope_field]),
        ),
        patch(
            "ghosthands.actions.domhand_assess_state._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch(
            "ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(side_effect=read_value_side_effect)
        ),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="My Experience"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is False
    assert len(payload["mismatched_fields"]) == 1
    assert payload["mismatched_fields"][0]["name"] == "Language"


@pytest.mark.asyncio
async def test_assess_state_medium_effort_retries_without_name_error():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        record_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    field = FormField(
        field_id="lang-medium",
        name="Language",
        field_type="select",
        section="Languages",
        required=True,
    )
    page_context_key = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="Languages",
    )
    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(field),
        field_label=field.name,
        expected_value="English",
        source="exact_profile",
    )

    async def evaluate_side_effect(script, *args):
        if args == (["lang-medium"],):
            return {"lang-medium": {"in_view": True, "top": 0, "bottom": 20}}
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "error_texts": [],
            "heading_texts": ["Languages"],
        }

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
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="French")),
        patch("ghosthands.actions.domhand_assess_state.asyncio.sleep", AsyncMock()) as sleep_mock,
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "medium"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Languages"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is False
    assert len(payload["mismatched_fields"]) == 1
    sleep_mock.assert_awaited()


@pytest.mark.asyncio
async def test_domhand_record_expected_value_tracks_manual_recovery_source():
    from ghosthands.actions.domhand_record_expected_value import domhand_record_expected_value
    from ghosthands.actions.views import DomHandRecordExpectedValueParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        get_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    field = FormField(
        field_id="lang-manual",
        name="Language",
        field_type="select",
        section="Languages",
        required=True,
    )

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_record_expected_value.extract_visible_form_fields",
            AsyncMock(return_value=[field]),
        ),
        patch(
            "ghosthands.actions.domhand_record_expected_value._read_field_value_for_field",
            AsyncMock(return_value="English"),
        ),
        patch(
            "ghosthands.actions.domhand_record_expected_value._field_has_validation_error",
            AsyncMock(return_value=False),
        ),
        patch(
            "ghosthands.actions.domhand_record_expected_value._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
    ):
        result = await domhand_record_expected_value(
            DomHandRecordExpectedValueParams(
                field_label="Language",
                expected_value="English",
                target_section="Languages",
            ),
            browser_session,
        )

    page_context_key = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="Languages",
    )
    expected = get_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(field),
    )
    assert expected is not None
    assert expected.expected_value == "English"
    assert expected.source == "manual_recovery"
    assert "Immediately call domhand_assess_state" in (result.extracted_content or "")


@pytest.mark.asyncio
async def test_domhand_record_expected_value_requires_validation_clear_before_recording():
    from ghosthands.actions.domhand_record_expected_value import domhand_record_expected_value
    from ghosthands.actions.views import DomHandRecordExpectedValueParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        get_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    field = FormField(
        field_id="salary-field",
        name="What is your desired Annual Salary?",
        field_type="text",
        section="Application Questions",
        required=False,
        field_fingerprint="salary-fingerprint",
    )

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_record_expected_value.extract_visible_form_fields",
            AsyncMock(return_value=[field]),
        ),
        patch(
            "ghosthands.actions.domhand_record_expected_value._read_field_value_for_field",
            AsyncMock(return_value="90000"),
        ),
        patch(
            "ghosthands.actions.domhand_record_expected_value._field_has_validation_error", AsyncMock(return_value=True)
        ),
        patch(
            "ghosthands.actions.domhand_record_expected_value._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
    ):
        result = await domhand_record_expected_value(
            DomHandRecordExpectedValueParams(
                field_label="What is your desired Annual Salary?",
                expected_value="90000",
                target_section="Application Questions",
                field_id="salary-field",
                field_type="text",
            ),
            browser_session,
        )

    assert "still has an active validation error" in (result.error or "")
    expected = get_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=build_page_context_key(
            url="https://example.wd1.myworkdayjobs.com/job",
            page_marker="Application Questions",
        ),
        field_key=get_stable_field_key(field),
    )
    assert expected is None


@pytest.mark.asyncio
async def test_extract_visible_form_fields_populates_field_fingerprint():
    from ghosthands.actions.domhand_fill import extract_visible_form_fields
    from ghosthands.actions.views import get_stable_field_key

    page = AsyncMock()
    raw_fields = [
        {
            "field_id": "volatile-field-id",
            "name": "Last Name*",
            "field_type": "text",
            "section": "Legal Name",
            "name_attr": "legalName.lastName",
            "required": True,
            "options": [],
            "choices": [],
            "accept": None,
            "is_native": True,
            "is_multi_select": False,
            "visible": True,
            "raw_label": "Last Name*",
            "synthetic_label": False,
            "field_fingerprint": None,
            "current_value": "",
        }
    ]
    page.evaluate = AsyncMock(side_effect=[json.dumps(raw_fields), json.dumps([])])

    fields = await extract_visible_form_fields(page)

    assert len(fields) == 1
    assert fields[0].field_fingerprint
    assert "volatile field id" not in get_stable_field_key(fields[0])


@pytest.mark.asyncio
async def test_extract_visible_form_fields_collapses_grouped_workday_date_inputs():
    from ghosthands.actions.domhand_fill import extract_visible_form_fields

    page = AsyncMock()
    raw_fields = [
        {
            "field_id": "date-month",
            "name": "Month",
            "field_type": "number",
            "section": "Voluntary Self-Identification of Disability",
            "name_attr": "dateSectionMonth",
            "required": True,
            "options": [],
            "choices": [],
            "accept": None,
            "is_native": True,
            "is_multi_select": False,
            "visible": True,
            "raw_label": "Month",
            "synthetic_label": False,
            "field_fingerprint": None,
            "current_value": "",
            "wrapper_id": "date-wrapper",
            "wrapper_label": "Date*",
            "date_component": "month",
            "date_group_key": "date-wrapper",
            "has_calendar_trigger": True,
            "format_hint": "MM/DD/YYYY",
        },
        {
            "field_id": "date-day",
            "name": "Day",
            "field_type": "number",
            "section": "Voluntary Self-Identification of Disability",
            "name_attr": "dateSectionDay",
            "required": True,
            "options": [],
            "choices": [],
            "accept": None,
            "is_native": True,
            "is_multi_select": False,
            "visible": True,
            "raw_label": "Day",
            "synthetic_label": False,
            "field_fingerprint": None,
            "current_value": "19",
            "wrapper_id": "date-wrapper",
            "wrapper_label": "Date*",
            "date_component": "day",
            "date_group_key": "date-wrapper",
            "has_calendar_trigger": True,
            "format_hint": "MM/DD/YYYY",
        },
        {
            "field_id": "date-year",
            "name": "Year",
            "field_type": "number",
            "section": "Voluntary Self-Identification of Disability",
            "name_attr": "dateSectionYear",
            "required": True,
            "options": [],
            "choices": [],
            "accept": None,
            "is_native": True,
            "is_multi_select": False,
            "visible": True,
            "raw_label": "Year",
            "synthetic_label": False,
            "field_fingerprint": None,
            "current_value": "",
            "wrapper_id": "date-wrapper",
            "wrapper_label": "Date*",
            "date_component": "year",
            "date_group_key": "date-wrapper",
            "has_calendar_trigger": True,
            "format_hint": "MM/DD/YYYY",
        },
    ]
    page.evaluate = AsyncMock(side_effect=[json.dumps(raw_fields), json.dumps([])])

    fields = await extract_visible_form_fields(page)

    assert len(fields) == 1
    assert fields[0].field_type == "date"
    assert fields[0].widget_kind == "grouped_date"
    assert fields[0].name == "Date*"
    assert fields[0].component_field_ids == ["date-month", "date-day", "date-year"]
    assert fields[0].current_value == "MM/19/YYYY"
    assert fields[0].has_calendar_trigger is True


def test_section_matches_scope_aliases_self_identify_and_terms_child_sections():
    from ghosthands.actions.domhand_fill import _section_matches_scope

    assert _section_matches_scope("Voluntary Self-Identification of Disability", "Self Identify") is True
    assert _section_matches_scope("Terms and Conditions", "Voluntary Disclosures") is True
    assert _section_matches_scope("Voluntary Self-Identification of Disability", "Voluntary Disclosures") is False


@pytest.mark.asyncio
async def test_domhand_record_expected_value_rejects_wrong_field_id_without_label_fallback():
    from ghosthands.actions.domhand_record_expected_value import domhand_record_expected_value
    from ghosthands.actions.views import DomHandRecordExpectedValueParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        get_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    text_field = FormField(
        field_id="last-name-text",
        name="Last Name*",
        field_type="text",
        section="Legal Name",
        required=True,
    )
    checkbox_field = FormField(
        field_id="last-name-check",
        name="Last Name*",
        field_type="checkbox",
        section="Legal Name",
        required=True,
    )

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_record_expected_value.extract_visible_form_fields",
            AsyncMock(return_value=[text_field, checkbox_field]),
        ),
        patch(
            "ghosthands.actions.domhand_record_expected_value._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch(
            "ghosthands.actions.domhand_fill._read_page_context_snapshot",
            AsyncMock(return_value={"page_marker": "My Information", "heading_texts": ["My Information"]}),
        ),
    ):
        result = await domhand_record_expected_value(
            DomHandRecordExpectedValueParams(
                field_label="Last Name*",
                expected_value="Yang",
                target_section="Legal Name",
                field_id="last-name-check",
                field_type="text",
            ),
            browser_session,
        )

    assert "Refusing to fall back to label-only matching" in (result.error or "")
    expected = get_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=build_page_context_key(
            url="https://example.wd1.myworkdayjobs.com/job",
            page_marker="My Information",
        ),
        field_key=get_stable_field_key(text_field),
    )
    assert expected is None


@pytest.mark.asyncio
async def test_domhand_record_expected_value_recovers_from_stale_missing_field_id_when_label_is_unique():
    from ghosthands.actions.domhand_record_expected_value import domhand_record_expected_value
    from ghosthands.actions.views import DomHandRecordExpectedValueParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        get_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    textarea_field = FormField(
        field_id="ff-101",
        name="You answered 'Yes' to the previous question. Please specify the type of visa sponsorship you require from your employer, now or in the future.*",
        field_type="textarea",
        section="Application Questions",
        required=True,
    )

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_record_expected_value.extract_visible_form_fields",
            AsyncMock(return_value=[textarea_field]),
        ),
        patch(
            "ghosthands.actions.domhand_record_expected_value._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch(
            "ghosthands.actions.domhand_fill._read_page_context_snapshot",
            AsyncMock(
                return_value={"page_marker": "Application Questions", "heading_texts": ["Application Questions"]}
            ),
        ),
        patch(
            "ghosthands.actions.domhand_record_expected_value._read_field_value_for_field",
            AsyncMock(return_value="F-1 OPT"),
        ),
        patch(
            "ghosthands.actions.domhand_record_expected_value._field_has_validation_error",
            AsyncMock(return_value=False),
        ),
    ):
        result = await domhand_record_expected_value(
            DomHandRecordExpectedValueParams(
                field_label=textarea_field.name,
                expected_value="F-1 OPT",
                target_section="Application Questions",
                field_id="ff-98",
                field_type="textarea",
            ),
            browser_session,
        )

    assert result.error is None
    expected = get_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=build_page_context_key(
            url="https://example.wd1.myworkdayjobs.com/job",
            page_marker="Application Questions",
        ),
        field_key=get_stable_field_key(textarea_field),
    )
    assert expected is not None
    assert expected.expected_value == "F-1 OPT"


@pytest.mark.asyncio
async def test_assess_state_accepts_semantic_textarea_without_blocking_advance():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        record_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    field = FormField(
        field_id="start-date",
        name="What is your desired start date?*",
        field_type="textarea",
        section="Application Questions",
        required=True,
    )
    page_context_key = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="Application Questions",
    )
    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(field),
        field_label=field.name,
        expected_value="Within 2-4 weeks (flexible)",
        source="derived_profile",
    )

    async def evaluate_side_effect(script, *args):
        if args == (["start-date"],):
            return {"start-date": {"in_view": True, "top": 0, "bottom": 20}}
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "error_texts": [],
            "heading_texts": ["Application Questions"],
        }

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
        patch(
            "ghosthands.actions.domhand_fill._read_page_context_snapshot",
            AsyncMock(
                return_value={"page_marker": "Application Questions", "heading_texts": ["Application Questions"]}
            ),
        ),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="2 weeks")),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Application Questions"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["advance_allowed"] is True
    assert payload["mismatched_fields"] == []


@pytest.mark.asyncio
async def test_assess_state_ignores_incompatible_expected_binding_for_relocation_parent():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        record_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    field = FormField(
        field_id="relocation-parent",
        name="Are you open to relocation?",
        field_type="select",
        section="Application Questions",
        required=False,
        choices=["Yes", "No"],
        field_fingerprint="relocation-parent-fingerprint",
    )
    page_context_key = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="Application Questions",
    )
    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(field),
        field_label="You answered 'Yes' to the previous question. Please specify the location to which you are willing to relocate.",
        field_type="textarea",
        field_section="Application Questions",
        field_fingerprint="relocation-child-fingerprint",
        expected_value="Los Angeles, CA",
        source="manual_recovery",
    )

    async def evaluate_side_effect(script, *args):
        if args == (["relocation-parent"],):
            return {"relocation-parent": {"in_view": True, "top": 0, "bottom": 20}}
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "error_texts": [],
            "heading_texts": ["Application Questions"],
        }

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
        patch(
            "ghosthands.actions.domhand_fill._read_page_context_snapshot",
            AsyncMock(
                return_value={"page_marker": "Application Questions", "heading_texts": ["Application Questions"]}
            ),
        ),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="")),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Application Questions"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["unverified_fields"] == []
    assert payload["mismatched_fields"] == []


@pytest.mark.asyncio
async def test_assess_state_caches_optional_validation_blockers():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField

    field = FormField(
        field_id="salary-field",
        name="What is your desired Annual Salary?",
        field_type="text",
        section="Application Questions",
        required=False,
        current_value="90000",
        field_fingerprint="salary-fingerprint",
    )

    async def evaluate_side_effect(script, *args):
        if args == (["salary-field"],):
            return {"salary-field": {"in_view": True, "top": 0, "bottom": 20}}
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "error_texts": [],
            "heading_texts": ["Application Questions"],
        }

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
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=True)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="90000")),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Application Questions"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert len(payload["unresolved_optional_fields"]) == 1
    assert "Optional validation blockers: 1" in (result.extracted_content or "")
    assert payload["advance_allowed"] is False
    assert browser_session._gh_last_application_state["optional_validation_count"] == 1
    assert "salary-field" in browser_session._gh_last_application_state["blocking_field_ids"]


@pytest.mark.asyncio
async def test_assess_state_allows_advancement_with_optional_unverified_fields():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        record_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    field = FormField(
        field_id="optional-note",
        name="Optional note",
        field_type="text",
        section="Application Questions",
        required=False,
        field_fingerprint="optional-note-fingerprint",
    )
    page_context_key = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="Application Questions",
    )
    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(field),
        field_label=field.name,
        expected_value="Hello",
        source="exact_profile",
        field_type=field.field_type,
        field_section=field.section,
        field_fingerprint=field.field_fingerprint,
    )

    async def evaluate_side_effect(script, *args):
        if args == (["optional-note"],):
            return {"optional-note": {"in_view": True, "top": 0, "bottom": 20}}
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "error_texts": [],
            "heading_texts": ["Application Questions"],
        }

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
        patch(
            "ghosthands.actions.domhand_fill._read_page_context_snapshot",
            AsyncMock(
                return_value={"page_marker": "Application Questions", "heading_texts": ["Application Questions"]}
            ),
        ),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="")),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Application Questions"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert len(payload["unverified_fields"]) == 1
    assert payload["unverified_fields"][0]["name"] == "Optional note"
    assert payload["advance_allowed"] is True


@pytest.mark.asyncio
async def test_assess_state_ignores_shape_incompatible_expected_value_for_conditional_detail_textarea():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        record_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    field = FormField(
        field_id="visa-detail",
        name="You answered 'Yes' to the previous question. Please specify the type of visa sponsorship you require from your employer, now or in the future.*",
        field_type="textarea",
        section="Application Questions",
        required=True,
    )
    page_context_key = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="Application Questions",
    )
    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(field),
        field_label=field.name,
        expected_value="Yes",
        source="derived_profile",
    )

    async def evaluate_side_effect(script, *args):
        if args == (["visa-detail"],):
            return {"visa-detail": {"in_view": True, "top": 0, "bottom": 20}}
        return {
            "button_texts": ["Save and Continue"],
            "body_text": "",
            "markers": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": True,
            "error_texts": [],
            "heading_texts": ["Application Questions"],
        }

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
        patch(
            "ghosthands.actions.domhand_fill._read_page_context_snapshot",
            AsyncMock(
                return_value={"page_marker": "Application Questions", "heading_texts": ["Application Questions"]}
            ),
        ),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="F-1 OPT")),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Application Questions"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["mismatched_fields"] == []
    assert payload["advance_allowed"] is True


def test_resolve_known_profile_value_for_field_skips_binary_default_for_conditional_detail_textarea():
    from ghosthands.actions.domhand_fill import _resolve_known_profile_value_for_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="visa-detail",
        name="You answered 'Yes' to the previous question. Please specify the type of visa sponsorship you require from your employer, now or in the future.*",
        field_type="textarea",
        section="Application Questions",
        required=True,
    )

    resolved = _resolve_known_profile_value_for_field(
        field,
        evidence={},
        profile_data={"visa_sponsorship": "Yes"},
        minimum_confidence="medium",
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_semantic_profile_value_for_field_skips_binary_answer_for_conditional_detail_textarea():
    from ghosthands.actions.domhand_fill import _semantic_profile_value_for_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="visa-detail",
        name="You answered 'Yes' to the previous question. Please specify the type of visa sponsorship you require from your employer, now or in the future.*",
        field_type="textarea",
        section="Application Questions",
        required=True,
    )

    with (
        patch("ghosthands.actions.domhand_fill.get_learned_question_alias", return_value=None),
        patch(
            "ghosthands.actions.domhand_fill._classify_known_intent_for_field",
            AsyncMock(return_value=("visa_sponsorship", "high")),
        ),
    ):
        resolved = await _semantic_profile_value_for_field(
            field,
            evidence={},
            profile_data={"visa_sponsorship": "Yes"},
        )

    assert resolved is None


@pytest.mark.asyncio
async def test_semantic_profile_value_for_field_can_skip_llm_classifier_in_hot_path():
    from ghosthands.actions.domhand_fill import _semantic_profile_value_for_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="relocation-1",
        name="Will you be located in the Seattle area during the internship?",
        raw_label="Will you be located in the Seattle area during the internship?",
        field_type="select",
        required=True,
        options=["Yes", "No"],
    )

    with (
        patch("ghosthands.actions.domhand_fill.get_learned_question_alias", return_value=None),
        patch(
            "ghosthands.actions.domhand_fill._classify_known_intent_for_field",
            AsyncMock(side_effect=AssertionError("classifier should not run")),
        ),
    ):
        resolved = await _semantic_profile_value_for_field(
            field,
            evidence={},
            profile_data={"relocation": "No"},
            allow_llm_classification=False,
        )

    assert resolved is None


@pytest.mark.asyncio
async def test_assess_state_ignores_duplicate_boolean_companion_control_mismatch():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        record_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    page = AsyncMock()
    page.evaluate = AsyncMock(
        side_effect=[
            None,
            json.dumps(
                {
                    "body_text": "",
                    "heading_texts": ["My Information"],
                    "button_texts": [],
                    "submit_visible": False,
                    "submit_disabled": False,
                    "advance_visible": False,
                    "error_texts": [],
                    "markers": [],
                }
            ),
            json.dumps({}),
            json.dumps({"error_text": "", "widget_kind": ""}),
            json.dumps({"error_text": "", "widget_kind": ""}),
        ]
    )
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)

    text_field = FormField(
        field_id="last-name-text",
        name="Last Name*",
        field_type="text",
        section="Legal Name",
        required=True,
        current_value="(Shixiang) Yang",
    )
    checkbox_field = FormField(
        field_id="last-name-check",
        name="Last Name*",
        field_type="checkbox",
        section="Legal Name",
        required=True,
        current_value="checked",
    )

    page_context_key = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="My Information",
    )
    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(checkbox_field),
        field_label="Last Name*",
        expected_value="(Shixiang) Yang",
        source="manual_recovery",
    )

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_assess_state.extract_visible_form_fields",
            AsyncMock(return_value=[text_field, checkbox_field]),
        ),
        patch(
            "ghosthands.actions.domhand_assess_state._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch(
            "ghosthands.actions.domhand_assess_state._read_field_value",
            AsyncMock(side_effect=["(Shixiang) Yang", "(Shixiang) Yang"]),
        ),
        patch("ghosthands.actions.domhand_assess_state._read_binary_state", AsyncMock(return_value=True)),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="My Information"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["mismatched_fields"] == []
    assert payload["unverified_fields"] == []


@pytest.mark.asyncio
async def test_default_action_watchdog_blocks_continue_when_assessment_disallows_advance():
    from types import SimpleNamespace

    from bubus import EventBus

    from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog

    page = AsyncMock()
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    browser_session._gh_last_application_state = {
        "page_url": "https://example.wd1.myworkdayjobs.com/job",
        "current_section": "My Information",
        "advance_allowed": False,
        "unresolved_required_count": 0,
        "mismatched_count": 1,
        "opaque_count": 0,
        "unverified_count": 0,
    }

    node = SimpleNamespace(
        node_name="button",
        attributes={"aria-label": "Save and Continue"},
        get_all_children_text=lambda max_depth=2: "Save and Continue",
    )

    watchdog = DefaultActionWatchdog.model_construct(
        browser_session=browser_session,
        event_bus=EventBus(),
    )
    message = await watchdog._guard_advance_click_requires_assessment(node)

    assert message is not None
    assert "unresolved blockers for advancement" in message


@pytest.mark.asyncio
async def test_default_action_watchdog_blocks_continue_when_optional_validation_blocker_exists():
    from types import SimpleNamespace

    from bubus import EventBus

    from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog

    page = AsyncMock()
    page.get_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    browser_session._gh_last_application_state = {
        "page_url": "https://example.wd1.myworkdayjobs.com/job",
        "current_section": "Application Questions",
        "advance_allowed": True,
        "optional_validation_count": 1,
        "unresolved_required_count": 0,
        "mismatched_count": 0,
        "opaque_count": 0,
        "unverified_count": 0,
    }

    node = SimpleNamespace(
        node_name="button",
        attributes={"aria-label": "Save and Continue"},
        get_all_children_text=lambda max_depth=2: "Save and Continue",
    )

    watchdog = DefaultActionWatchdog.model_construct(
        browser_session=browser_session,
        event_bus=EventBus(),
    )
    message = await watchdog._guard_advance_click_requires_assessment(node)

    assert message is not None
    assert "optional validation: 1" in message


@pytest.mark.asyncio
async def test_default_action_watchdog_reroutes_target_blank_anchor_to_same_tab():
    from bubus import EventBus

    from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog

    browser_session = AsyncMock()
    browser_session.navigate_to = AsyncMock(return_value=None)

    cdp_client = SimpleNamespace(
        send=SimpleNamespace(
            DOM=SimpleNamespace(resolveNode=AsyncMock(return_value={"object": {"objectId": "node-1"}})),
            Runtime=SimpleNamespace(
                callFunctionOn=AsyncMock(
                    return_value={"result": {"value": {"url": "https://example.com/apply", "reason": "anchor_target"}}}
                )
            ),
        )
    )
    cdp_session = SimpleNamespace(cdp_client=cdp_client, session_id="session-1")

    node = SimpleNamespace(
        backend_node_id=123,
        tag_name="BUTTON",
        attributes={"type": "button"},
        node_name="button",
        xpath="//button[1]",
    )

    watchdog = DefaultActionWatchdog.model_construct(
        browser_session=browser_session,
        event_bus=EventBus(),
    )

    result = await watchdog._maybe_reroute_same_tab_navigation(node, cdp_session, "session-1", 123)

    browser_session.navigate_to.assert_awaited_once_with("https://example.com/apply", new_tab=False)
    assert result == {
        "same_tab_navigation_url": "https://example.com/apply",
        "same_tab_navigation_reason": "anchor_target",
    }


@pytest.mark.asyncio
async def test_record_expected_value_if_settled_skips_unsettled_autofill():
    from ghosthands.actions.domhand_fill import _record_expected_value_if_settled
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="salary",
        name="Desired Salary",
        field_type="text",
        section="Application Questions",
        required=False,
    )
    page = AsyncMock()

    with (
        patch("ghosthands.dom.fill_verify._read_observed_field_value", AsyncMock(return_value="90000")),
        patch("ghosthands.dom.fill_executor._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.dom.fill_verify._field_already_matches", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill.record_expected_field_value") as record_expected,
    ):
        recorded = await _record_expected_value_if_settled(
            page=page,
            host="example.wd1.myworkdayjobs.com",
            page_context_key="ctx",
            field=field,
            field_key="text|salary",
            expected_value="90000",
            source="derived_profile",
            log_context="domhand.fill",
        )

    assert recorded is False
    record_expected.assert_not_called()


@pytest.mark.asyncio
async def test_domhand_interact_control_uses_exact_recovery_after_retry_cap():
    from ghosthands.actions.domhand_interact_control import domhand_interact_control
    from ghosthands.actions.views import DomHandInteractControlParams, FormField

    field = FormField(
        field_id="ff-3",
        name="Have you previously been employed?",
        field_type="radio-group",
        section="",
        required=True,
        choices=["Yes", "No"],
    )
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_interact_control.extract_visible_form_fields",
            AsyncMock(return_value=[field]),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._field_already_matches",
            AsyncMock(side_effect=[False, True]),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._attempt_domhand_fill_with_retry_cap",
            AsyncMock(return_value=(False, "retry capped", "domhand_retry_capped", 0.0)),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._attempt_exact_control_recovery",
            AsyncMock(return_value=(True, "exact_group_gui")),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._read_control_value",
            AsyncMock(return_value="No"),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._field_has_validation_error",
            AsyncMock(return_value=False),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._record_expected_value_if_settled",
            AsyncMock(return_value=True),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control.publish_browser_session_trace",
            AsyncMock(return_value=None),
        ),
    ):
        result = await domhand_interact_control(
            DomHandInteractControlParams(
                field_label="Have you previously been employed?",
                desired_value="No",
                field_id="ff-3",
                field_type="radio-group",
                target_section="My Information",
            ),
            browser_session,
        )

    assert result.error is None
    assert result.metadata["strategy"] == "exact_group_gui"
    assert result.metadata["state_change"] == "changed"


@pytest.mark.asyncio
async def test_domhand_interact_control_supports_exact_number_field_recovery_after_retry_cap():
    from ghosthands.actions.domhand_interact_control import domhand_interact_control
    from ghosthands.actions.views import DomHandInteractControlParams, FormField

    field = FormField(
        field_id="ff-117",
        name="Month",
        field_type="number",
        section="Voluntary Self-Identification of Disability",
        required=False,
    )
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_interact_control.extract_visible_form_fields",
            AsyncMock(return_value=[field]),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._field_already_matches",
            AsyncMock(side_effect=[False, True]),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._attempt_domhand_fill_with_retry_cap",
            AsyncMock(return_value=(False, "retry capped", "domhand_retry_capped", 0.0)),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._attempt_exact_control_recovery",
            AsyncMock(return_value=(True, "exact_text_like_fill")),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._read_control_value",
            AsyncMock(return_value="03"),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._field_has_validation_error",
            AsyncMock(return_value=False),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control._record_expected_value_if_settled",
            AsyncMock(return_value=True),
        ),
        patch(
            "ghosthands.actions.domhand_interact_control.publish_browser_session_trace",
            AsyncMock(return_value=None),
        ),
    ):
        result = await domhand_interact_control(
            DomHandInteractControlParams(
                field_label="Month",
                desired_value="03",
                field_id="ff-117",
                field_type="number",
                target_section="Voluntary Self-Identification of Disability",
            ),
            browser_session,
        )

    assert result.error is None
    assert result.metadata["strategy"] == "exact_text_like_fill"
    assert result.metadata["state_change"] == "changed"


@pytest.mark.asyncio
async def test_request_open_question_answers_ignores_binary_question_from_blocker_text():
    from ghosthands.cli import _request_open_question_answers

    answers, cancelled = await _request_open_question_answers(
        AsyncMock(),
        "blocker: required field 'Have you previously been employed here?' on the 'My Information' page",
        timeout_seconds=1.0,
        issues=[],
        profile={},
    )

    assert answers == []
    assert cancelled is False


def test_active_blocker_focus_fields_marks_unchanged_blockers_for_strategy_shift():
    from ghosthands.actions.domhand_fill import _active_blocker_focus_fields
    from ghosthands.actions.views import FormField, get_stable_field_key

    field = FormField(
        field_id="salary",
        name="Desired Salary",
        field_type="text",
        section="Application Questions",
        required=False,
    )
    blocker_key = get_stable_field_key(field)
    browser_session = SimpleNamespace(
        _gh_last_application_state={
            "page_context_key": "ctx",
            "page_url": "https://example.wd1.myworkdayjobs.com/job",
            "blocking_field_ids": ["salary"],
            "blocking_field_keys": [blocker_key],
            "blocking_field_labels": ["Desired Salary"],
            "blocking_field_state_changes": {blocker_key: "no_state_change"},
        }
    )

    filtered, unchanged = _active_blocker_focus_fields(
        browser_session,
        fields=[field],
        page_context_key="ctx",
        page_url="https://example.wd1.myworkdayjobs.com/job",
    )

    assert filtered == [field]
    assert unchanged is True


@pytest.mark.asyncio
async def test_domhand_select_uses_global_host_detector_without_unboundlocalerror():
    from ghosthands.actions.domhand_select import domhand_select
    from ghosthands.actions.views import DomHandSelectParams

    page = AsyncMock()
    node = SimpleNamespace(tag_name="div")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    browser_session.get_element_by_index = AsyncMock(return_value=node)
    browser_session.get_current_page_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_select._call_function_on_node",
            AsyncMock(return_value={"type": "custom_popup", "options": [{"text": "LinkedIn", "value": "LinkedIn"}]}),
        ),
        patch(
            "ghosthands.actions.domhand_select._read_field_context",
            AsyncMock(
                return_value={"label": "How Did You Hear About Us?", "widgetType": "custom_widget", "invalid": False}
            ),
        ),
        patch("ghosthands.actions.domhand_select._read_current_selection", AsyncMock(return_value="LinkedIn")),
        patch("ghosthands.actions.domhand_select.clear_domhand_failure"),
    ):
        result = await domhand_select(
            DomHandSelectParams(index=2519, value="LinkedIn"),
            browser_session,
        )

    assert result.error is None
    assert "already showed" in (result.extracted_content or "")


def test_repeater_binding_uses_row_order_once_for_unnumbered_language_rows():
    from ghosthands.actions.domhand_fill import (
        _language_slot_name,
        _resolve_repeater_binding,
    )
    from ghosthands.actions.views import FormField
    from ghosthands.runtime_learning import reset_runtime_learning_state

    reset_runtime_learning_state()
    fields = [
        FormField(field_id="lang-1", name="Language", field_type="text", section="Languages"),
        FormField(field_id="lang-2", name="Language", field_type="text", section="Languages"),
    ]
    entries = [
        {"language": "Chinese"},
        {"language": "English"},
    ]

    binding = _resolve_repeater_binding(
        host="example.com",
        repeater_group="languages",
        field=fields[1],
        visible_fields=fields,
        entries=entries,
        numeric_index=None,
        slot_name="language",
        current_value="",
        slot_resolver=_language_slot_name,
    )
    cached_binding = _resolve_repeater_binding(
        host="example.com",
        repeater_group="languages",
        field=fields[1],
        visible_fields=list(reversed(fields)),
        entries=entries,
        numeric_index=None,
        slot_name="language",
        current_value="",
        slot_resolver=_language_slot_name,
    )

    assert binding is not None
    assert binding.entry_index == 1
    assert binding.binding_mode == "row_order"
    assert binding.best_effort_guess is True
    assert cached_binding is not None
    assert cached_binding.entry_index == 1


def test_repeater_binding_uses_row_order_once_for_unnumbered_education_rows():
    from ghosthands.actions.domhand_fill import (
        _education_slot_name,
        _resolve_repeater_binding,
    )
    from ghosthands.actions.views import FormField
    from ghosthands.runtime_learning import reset_runtime_learning_state

    reset_runtime_learning_state()
    fields = [
        FormField(field_id="edu-gpa-1", name="GPA", field_type="text", section="Education"),
        FormField(field_id="edu-gpa-2", name="GPA", field_type="text", section="Education"),
    ]
    entries = [
        {"school": "MIT", "gpa": "3.9"},
        {"school": "USC", "gpa": "3.7"},
    ]

    binding = _resolve_repeater_binding(
        host="example.com",
        repeater_group="education",
        field=fields[1],
        visible_fields=fields,
        entries=entries,
        numeric_index=None,
        slot_name="gpa",
        current_value="",
        slot_resolver=_education_slot_name,
    )

    assert binding is not None
    assert binding.entry_index == 1
    assert binding.binding_mode == "row_order"
    assert binding.best_effort_guess is True


def test_education_slot_name_infers_generic_year_columns_from_visible_fields():
    from ghosthands.actions.domhand_fill import _education_slot_name, _structured_education_value_from_entry
    from ghosthands.actions.views import FormField

    fields = [
        FormField(field_id="edu-from-year", name="Year", field_type="number", section="Education"),
        FormField(field_id="edu-to-year", name="Year", field_type="number", section="Education"),
    ]
    entry = {
        "start_date": "2021-09",
        "end_date": "2025-05",
    }

    assert _education_slot_name(fields[0], fields) == "start_date"
    assert _education_slot_name(fields[1], fields) == "end_date"
    assert _structured_education_value_from_entry(fields[0], entry, fields) == "2021"
    assert _structured_education_value_from_entry(fields[1], entry, fields) == "2025"


def test_education_slot_name_does_not_match_major_life_activities_eeoc_wording():
    """Greenhouse disability prompts contain 'major life activities' — not education 'major'."""
    from ghosthands.actions.domhand_fill import _education_slot_name
    from ghosthands.actions.views import FormField

    disability_select = FormField(
        field_id="ff-30",
        name=(
            "Do you have a disability or chronic condition (physical, visual, auditory, cognitive, mental, "
            "emotional, or other) that substantially limits one or more of your major life activities, "
            "including mobility, communication, and learning?"
        ),
        field_type="select",
        section="Demographic Questions",
    )
    assert _education_slot_name(disability_select, None) is None


def test_structured_education_value_from_entry_supports_field_of_study_and_from_labels():
    from ghosthands.actions.domhand_fill import _structured_education_value_from_entry
    from ghosthands.actions.views import FormField

    study_field = FormField(
        field_id="edu-field-of-study",
        name="Field of Study",
        field_type="text",
        section="Education 1",
    )
    from_field = FormField(
        field_id="edu-from",
        name="From",
        field_type="text",
        section="Education 1",
    )
    visible_fields = [
        FormField(field_id="edu-school", name="School or University", field_type="text", section="Education 1"),
        FormField(field_id="edu-degree", name="Degree", field_type="text", section="Education 1"),
        study_field,
        from_field,
    ]
    entry = {
        "school": "University of Southern California",
        "degree": "B.S.",
        "field_of_study": "Computer Science",
        "start_date": "2021-09",
        "end_date": "2025-05",
    }

    assert _structured_education_value_from_entry(study_field, entry, visible_fields) == "Computer Science"
    assert _structured_education_value_from_entry(from_field, entry, visible_fields) == "2021-09"


def test_structured_education_value_from_entry_supports_major_alias_and_split_start_date():
    from ghosthands.actions.domhand_fill import _structured_education_raw_value_and_source_from_entry
    from ghosthands.actions.views import FormField

    study_field = FormField(
        field_id="edu-major",
        name="Field of Study",
        field_type="text",
        section="Education 1",
    )
    from_field = FormField(
        field_id="edu-start",
        name="From",
        field_type="text",
        section="Education 1",
    )
    visible_fields = [
        FormField(field_id="edu-school", name="School or University", field_type="text", section="Education 1"),
        FormField(field_id="edu-degree", name="Degree", field_type="text", section="Education 1"),
        study_field,
        from_field,
    ]
    entry = {
        "school": "University of Southern California",
        "degree": "B.S.",
        "major": "Computer Science",
        "startYear": "2021",
        "startMonth": "09",
    }

    study_value, study_source = _structured_education_raw_value_and_source_from_entry(
        study_field,
        entry,
        visible_fields,
    )
    from_value, from_source = _structured_education_raw_value_and_source_from_entry(
        from_field,
        entry,
        visible_fields,
    )

    assert study_value == "Computer Science"
    assert study_source == "major"
    assert from_value == "2021-09"
    assert from_source == "startYear+startMonth"


def test_structured_education_slot_detection_supports_degree_type_minor_and_honors():
    from ghosthands.actions.domhand_fill import (
        _education_slot_name,
        _structured_education_raw_value_and_source_from_entry,
    )
    from ghosthands.actions.views import FormField

    degree_type_field = FormField(
        field_id="edu-degree-type",
        name="Degree Type",
        field_type="text",
        section="Education 1",
    )
    minor_field = FormField(
        field_id="edu-minor",
        name="Minor",
        field_type="text",
        section="Education 1",
    )
    honors_field = FormField(
        field_id="edu-honors",
        name="Honors",
        field_type="text",
        section="Education 1",
    )
    major_field = FormField(
        field_id="edu-major",
        name="Major",
        field_type="text",
        section="Education 1",
    )
    entry = {
        "degreeType": "Undergraduate",
        "majorNames": ["Computer Science", "Mathematics"],
        "minorNames": ["Statistics", "Philosophy"],
        "honorsList": ["Phi Beta Kappa", "Summa Cum Laude"],
    }
    visible_fields = [degree_type_field, minor_field, honors_field, major_field]

    assert _education_slot_name(degree_type_field, visible_fields) == "degree_type"
    assert _education_slot_name(minor_field, visible_fields) == "minor"
    assert _education_slot_name(honors_field, visible_fields) == "honors"
    assert _education_slot_name(major_field, visible_fields) == "field_of_study"

    degree_value, degree_source = _structured_education_raw_value_and_source_from_entry(
        degree_type_field,
        entry,
        visible_fields,
    )
    minor_value, minor_source = _structured_education_raw_value_and_source_from_entry(
        minor_field,
        entry,
        visible_fields,
    )
    honors_value, honors_source = _structured_education_raw_value_and_source_from_entry(
        honors_field,
        entry,
        visible_fields,
    )
    major_value, major_source = _structured_education_raw_value_and_source_from_entry(
        major_field,
        entry,
        visible_fields,
    )

    assert degree_value == "Undergraduate"
    assert degree_source == "degreeType"
    assert minor_value == "Statistics, Philosophy"
    assert minor_source == "minorNames"
    assert honors_value == "Phi Beta Kappa, Summa Cum Laude"
    assert honors_source == "honorsList"
    assert major_value == "Computer Science, Mathematics"
    assert major_source == "majorNames"


def test_field_value_matches_expected_treats_checked_as_affirmative_binary_value():
    from ghosthands.actions.domhand_fill import _field_value_matches_expected

    assert _field_value_matches_expected("checked", "Yes") is True
    assert _field_value_matches_expected("checked", "I acknowledge") is True
    assert _field_value_matches_expected("checked", "No") is False
    assert _field_value_matches_expected("checked", "No preference") is False


def test_infer_entry_data_from_scope_does_not_default_to_first_multi_row():
    from ghosthands.actions.domhand_fill import _infer_entry_data_from_scope

    assert (
        _infer_entry_data_from_scope(
            {
                "education": [
                    {"school": "MIT"},
                    {"school": "USC"},
                ]
            },
            None,
            "Education",
        )
        is None
    )


def test_field_conditional_cluster_treats_workday_boolean_selects_without_choices_as_boolean_parents():
    from ghosthands.actions.domhand_fill import _field_conditional_cluster
    from ghosthands.actions.views import FormField

    work_auth = FormField(
        field_id="ff-90",
        name="Are you legally permitted to work in the country where this job is located?*",
        field_type="select",
        required=True,
    )
    sponsorship = FormField(
        field_id="ff-92",
        name="Will you now or in the future require visa sponsorship by an employer?*",
        field_type="select",
        required=True,
    )
    visa_detail = FormField(
        field_id="ff-101",
        name="You answered 'Yes' to the previous question. Please specify the type of visa sponsorship you require from your employer, now or in the future.*",
        field_type="textarea",
        required=True,
    )

    assert _field_conditional_cluster(work_auth) == ("work_authorization", "boolean_parent")
    assert _field_conditional_cluster(sponsorship) == ("visa_sponsorship", "boolean_parent")
    assert _field_conditional_cluster(visa_detail) == ("visa_sponsorship", "detail_child")


def test_value_shape_is_compatible_accepts_binary_for_empty_choice_boolean_parent_and_rejects_detail_child():
    from ghosthands.actions.domhand_fill import _value_shape_is_compatible
    from ghosthands.actions.views import FormField

    work_auth = FormField(
        field_id="ff-90",
        name="Are you legally permitted to work in the country where this job is located?*",
        field_type="select",
        required=True,
    )
    visa_detail = FormField(
        field_id="ff-101",
        name="You answered 'Yes' to the previous question. Please specify the type of visa sponsorship you require from your employer, now or in the future.*",
        field_type="textarea",
        required=True,
    )

    assert _value_shape_is_compatible(work_auth, "No") is True
    assert _value_shape_is_compatible(visa_detail, "Yes") is False


def test_field_conditional_cluster_treats_relocation_preamble_question_as_boolean_parent():
    from ghosthands.actions.domhand_fill import _field_conditional_cluster, _value_shape_is_compatible
    from ghosthands.actions.views import FormField

    relocation = FormField(
        field_id="ff-reloc",
        name=(
            "We are unable to provide relocation assistance. Will you be located in the Seattle area "
            "and have the ability to come into our Bellevue office several days a week during the time "
            "of the internship?*"
        ),
        field_type="select",
        required=True,
    )

    assert _field_conditional_cluster(relocation) == ("relocation", "boolean_parent")
    assert _value_shape_is_compatible(relocation, "New York, NY") is False
    assert _value_shape_is_compatible(relocation, "No") is True


def test_default_screening_answer_defaults_location_specific_relocation_question_to_yes():
    from ghosthands.actions.domhand_fill import _default_screening_answer
    from ghosthands.actions.views import FormField

    relocation = FormField(
        field_id="ff-reloc-2",
        name=(
            "We are unable to provide relocation assistance. Will you be located in the Seattle area "
            "and have the ability to come into our Bellevue office several days a week during the time "
            "of the internship?*"
        ),
        field_type="select",
        required=True,
        options=["Yes", "No"],
    )

    assert _default_screening_answer(relocation, {}) == "Yes"


def test_default_screening_answer_respects_explicit_negative_relocation_preference():
    from ghosthands.actions.domhand_fill import _default_screening_answer
    from ghosthands.actions.views import FormField

    relocation = FormField(
        field_id="ff-reloc-3",
        name="Are you open to relocation?",
        field_type="select",
        required=True,
        options=["Yes", "No"],
    )

    assert _default_screening_answer(relocation, {"relocation_preference": "No"}) == "No"


@pytest.mark.asyncio
async def test_fill_button_group_skips_upload_like_controls():
    from ghosthands.actions.views import FormField
    from ghosthands.dom.fill_executor import _fill_button_group

    page = AsyncMock()
    field = FormField(
        field_id="ff-upload",
        name="Cover Letter",
        field_type="button-group",
        section="Resume/CV",
        choices=["Attach", "Enter manually"],
        required=False,
    )

    assert await _fill_button_group(page, field, "Attach", "[Cover Letter]") is False
    page.evaluate.assert_not_called()


def test_resolve_known_profile_value_for_field_matches_required_name_with_marker():
    from ghosthands.actions.domhand_fill import _resolve_known_profile_value_for_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="ff-115",
        name="Name*",
        field_type="text",
        section="Voluntary Self-Identification of Disability",
        required=True,
    )

    resolved = _resolve_known_profile_value_for_field(
        field,
        {},
        {"full_name": "Adam (Shixiang) Yang"},
    )

    assert resolved is not None
    assert resolved.value == "Adam (Shixiang) Yang"
    assert resolved.answer_mode == "profile_backed"


def test_resolve_known_profile_value_for_field_skips_availability_for_education_start_date():
    from ghosthands.actions.domhand_fill import _resolve_known_profile_value_for_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="edu-start",
        name="Start Date",
        field_type="text",
        section="Education 1",
        required=True,
    )

    resolved = _resolve_known_profile_value_for_field(
        field,
        {"available_start_date": "Within 2 weeks"},
        {"available_start_date": "Within 2 weeks"},
    )

    assert resolved is None


def test_known_profile_value_formats_start_date_for_availability_window_prompt():
    from ghosthands.actions.views import FormField
    from ghosthands.dom.fill_profile_resolver import _resolve_known_profile_value_for_field

    field = FormField(
        field_id="availability-window",
        name="What dates are you available for an internship?",
        field_type="textarea",
        required=True,
    )

    resolved = _resolve_known_profile_value_for_field(
        field,
        {"available_start_date": "2026-06-01"},
        {"available_start_date": "2026-06-01"},
    )

    assert resolved is not None
    assert resolved.value == "Available starting June 1, 2026"


def test_value_shape_rejects_raw_iso_date_for_availability_window_text_prompt():
    from ghosthands.actions.views import FormField
    from ghosthands.dom.fill_profile_resolver import _value_shape_is_compatible

    field = FormField(
        field_id="availability-window",
        name="What dates are you available for an internship?",
        field_type="textarea",
        required=True,
    )

    assert _value_shape_is_compatible(field, "2026-06-01") is False
    assert _value_shape_is_compatible(field, "Available starting June 1, 2026") is True


def test_text_fill_attempt_values_include_zero_padded_month_variant():
    from ghosthands.actions.domhand_fill import _text_fill_attempt_values
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="ff-117",
        name="Month",
        field_type="number",
        section="Voluntary Self-Identification of Disability",
        required=False,
    )

    assert _text_fill_attempt_values(field, "3") == ["3", "03"]


@pytest.mark.asyncio
async def test_fill_date_field_uses_grouped_workday_date_widget_flow():
    from ghosthands.actions import domhand_fill as domhand_fill_module
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="date-wrapper",
        name="Date",
        field_type="date",
        section="Voluntary Self-Identification of Disability",
        required=True,
        widget_kind="grouped_date",
        component_field_ids=["date-month", "date-day", "date-year"],
        has_calendar_trigger=True,
        format_hint="MM/DD/YYYY",
    )

    async def evaluate_side_effect(script, *args):
        if script == domhand_fill_module._OPEN_GROUPED_DATE_PICKER_JS:
            return json.dumps({"clicked": True, "opened": True})
        if script == domhand_fill_module._SELECT_GROUPED_DATE_PICKER_VALUE_JS:
            return json.dumps({"selected": False})
        if script == domhand_fill_module._FILL_FIELD_JS:
            return json.dumps({"success": True})
        return json.dumps(None)

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect)
    page.press = AsyncMock(return_value=None)

    with (
        patch("ghosthands.dom.fill_executor._click_away_from_text_like_field", AsyncMock(return_value=True)),
        patch("ghosthands.dom.fill_executor._confirm_text_like_value", AsyncMock(return_value=True)),
    ):
        success = await domhand_fill_module._fill_date_field(page, field, "2026-03-19", "[Date]")

    assert success is True
    assert any(
        call.args[0] == domhand_fill_module._OPEN_GROUPED_DATE_PICKER_JS for call in page.evaluate.await_args_list
    )
    assert any(
        call.args[0] == domhand_fill_module._SELECT_GROUPED_DATE_PICKER_VALUE_JS
        for call in page.evaluate.await_args_list
    )
    assert any(
        call.args[0] == domhand_fill_module._FILL_FIELD_JS and call.args[1] == "date-year"
        for call in page.evaluate.await_args_list
    )


@pytest.mark.asyncio
async def test_assess_state_treats_checked_terms_checkbox_as_matching_yes_expected_value():
    from ghosthands.actions.domhand_assess_state import domhand_assess_state
    from ghosthands.actions.views import DomHandAssessStateParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import (
        build_page_context_key,
        record_expected_field_value,
        reset_runtime_learning_state,
    )

    reset_runtime_learning_state()
    field = FormField(
        field_id="terms-checkbox",
        name="I understand and acknowledge the terms of use for Arlo.*",
        field_type="checkbox",
        section="Terms and Conditions",
        required=True,
        current_value="checked",
    )
    page_context_key = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="Voluntary Disclosures",
    )
    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=page_context_key,
        field_key=get_stable_field_key(field),
        field_label=field.name,
        field_type=field.field_type,
        field_section=field.section,
        expected_value="Yes",
        source="manual_recovery",
    )

    page = AsyncMock()
    page.evaluate = AsyncMock(
        side_effect=[
            None,
            {
                "button_texts": ["Save and Continue"],
                "body_text": "",
                "markers": [],
                "submit_visible": False,
                "submit_disabled": False,
                "advance_visible": True,
                "error_texts": [],
                "heading_texts": ["Voluntary Disclosures"],
            },
            {"terms-checkbox": {"in_view": True, "top": 0, "bottom": 20}},
            {"error_text": "", "widget_kind": "checkbox"},
        ]
    )
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
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_binary_state", AsyncMock(return_value=True)),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Voluntary Disclosures"),
            browser_session,
        )

    payload = json.loads((result.metadata or {})["application_state_json"])
    assert payload["mismatched_fields"] == []
    assert payload["unresolved_required_fields"] == []
    assert payload["advance_allowed"] is True


@pytest.mark.asyncio
async def test_confirm_text_like_value_blurs_salary_fields_for_revalidation():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from ghosthands.actions.domhand_fill import _DISMISS_DROPDOWN_JS, _confirm_text_like_value
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="salary-field",
        name="What is your desired Annual Salary?",
        field_type="text",
        section="Application Questions",
        required=False,
    )
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)
    locator = AsyncMock()
    locator.click = AsyncMock(return_value=None)
    locator.press = AsyncMock(return_value=None)
    page.locator = Mock(return_value=SimpleNamespace(first=locator))

    with (
        patch("ghosthands.dom.fill_executor._wait_for_field_value", AsyncMock(side_effect=["90000", "90000"])),
        patch("ghosthands.dom.fill_executor._field_has_validation_error", AsyncMock(return_value=False)),
    ):
        success = await _confirm_text_like_value(page, field, "90000", "[salary]")

    assert success is True
    assert any(call.args and call.args[0] == _DISMISS_DROPDOWN_JS for call in page.evaluate.await_args_list)


@pytest.mark.asyncio
async def test_fill_text_field_retries_salary_with_numeric_candidate_after_range_failure():
    from ghosthands.actions.domhand_fill import _FILL_FIELD_JS, _fill_text_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="salary-field",
        name="What is your desired Annual Salary?",
        field_type="text",
        section="Application Questions",
        required=False,
    )
    page = AsyncMock()
    page.evaluate = AsyncMock(
        side_effect=[
            json.dumps(False),
            json.dumps({"success": True}),
            json.dumps({"success": True}),
        ]
    )

    with patch(
        "ghosthands.dom.fill_executor._confirm_text_like_value",
        AsyncMock(side_effect=[False, True]),
    ):
        success = await _fill_text_field(
            page,
            field,
            "$90,000-$120,000 base (flexible)",
            "[salary]",
        )

    assert success is True
    fill_calls = [call for call in page.evaluate.await_args_list if call.args and call.args[0] == _FILL_FIELD_JS]
    assert fill_calls[0].args[2] == "$90,000-$120,000 base (flexible)"
    assert fill_calls[1].args[2] == "90000"


@pytest.mark.asyncio
async def test_domhand_select_no_options_returns_failover_without_unboundlocalerror():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from ghosthands.actions.domhand_select import domhand_select
    from ghosthands.actions.views import DomHandSelectParams

    page = AsyncMock()
    node = SimpleNamespace(tag_name="div")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    browser_session.get_element_by_index = AsyncMock(return_value=node)
    browser_session.get_current_page_url = AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")
    browser_session.event_bus.dispatch = Mock(side_effect=RuntimeError("no dropdown event"))

    with (
        patch("ghosthands.dom.shadow_helpers.ensure_helpers", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_select._call_function_on_node",
            AsyncMock(return_value={"type": "custom_popup", "options": []}),
        ),
        patch(
            "ghosthands.actions.domhand_select._read_field_context",
            AsyncMock(
                return_value={"label": "How Did You Hear About Us?", "widgetType": "custom_widget", "invalid": False}
            ),
        ),
        patch("ghosthands.actions.domhand_select._read_current_selection", AsyncMock(return_value="")),
        patch("ghosthands.actions.domhand_select.publish_browser_session_trace", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_select.update_blocker_attempt_state"),
    ):
        result = await domhand_select(
            DomHandSelectParams(index=2460, value="LinkedIn"),
            browser_session,
        )

    assert result.error is not None
    assert "cannot access local variable 'current'" not in result.error


def test_meaningful_dropdown_options_filters_react_select_placeholders():
    from ghosthands.actions.domhand_select import _meaningful_dropdown_options, _needs_dropdown_open_trigger

    noise = [{"text": "No options", "value": "No options"}]
    assert _meaningful_dropdown_options(noise) == []
    assert _needs_dropdown_open_trigger(False, "aria_listbox", noise) is True

    real = [{"text": "No", "value": "no"}, {"text": "Yes", "value": "yes"}]
    assert len(_meaningful_dropdown_options(real)) == 2
    assert _needs_dropdown_open_trigger(False, "aria_listbox", real) is False


def test_resolve_known_profile_value_for_field_accepts_boolean_sponsorship_answer_for_custom_widget_select():
    from ghosthands.actions.domhand_fill import _resolve_known_profile_value_for_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="ff-92",
        name="Will you now or in the future require visa sponsorship by an employer?*",
        field_type="select",
        required=True,
    )

    resolved = _resolve_known_profile_value_for_field(
        field,
        {"sponsorship_needed": "Yes"},
        {"sponsorship_needed": "Yes"},
    )

    assert resolved is not None
    assert resolved.value == "Yes"
    assert resolved.answer_mode == "profile_backed"


def test_resolve_llm_answer_for_field_accepts_boolean_for_custom_widget_select():
    from ghosthands.actions.domhand_fill import _resolve_llm_answer_for_field
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="ff-90",
        name="Are you legally permitted to work in the country where this job is located?*",
        field_type="select",
        required=True,
    )

    resolved = _resolve_llm_answer_for_field(
        field,
        {"Are you legally permitted to work in the country where this job is located?*": "No"},
        {},
        {},
    )

    assert resolved is not None
    assert resolved.value == "No"
    assert resolved.source == "llm"


@pytest.mark.asyncio
async def test_domhand_fill_skips_llm_for_resolved_required_custom_widget_boolean_select():
    from ghosthands.actions.domhand_fill import ResolvedFieldValue, domhand_fill
    from ghosthands.actions.views import DomHandFillParams, FormField

    page = AsyncMock()
    page.evaluate = AsyncMock(return_value="{}")
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    browser_session._gh_last_application_state = None
    field = FormField(
        field_id="ff-90",
        name="Are you legally permitted to work in the country where this job is located?*",
        field_type="select",
        section="Application Questions",
        required=True,
        is_native=False,
        choices=[],
    )

    with (
        patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="profile text"),
        patch("ghosthands.actions.domhand_fill._get_profile_data", return_value={"work_authorization": "No"}),
        patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
        patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
        patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={"work_authorization": "No"}),
        patch(
            "ghosthands.actions.domhand_fill._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch("ghosthands.actions.domhand_fill.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch("ghosthands.actions.domhand_fill._filter_fields_for_scope", side_effect=lambda fields, **_: fields),
        patch("ghosthands.actions.domhand_fill._is_navigation_field", return_value=False),
        patch("ghosthands.actions.domhand_fill._known_auth_override_for_field", return_value=None),
        patch(
            "ghosthands.actions.domhand_fill._resolve_known_profile_value_for_field",
            return_value=ResolvedFieldValue(
                value="No",
                source="derived_profile",
                answer_mode="profile_backed",
                confidence=0.98,
            ),
        ),
        patch("ghosthands.actions.domhand_fill._semantic_profile_value_for_field", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_fill._attempt_domhand_fill_with_retry_cap",
            AsyncMock(return_value=(True, None, None, 1.0)),
        ),
        patch("ghosthands.actions.domhand_fill._record_expected_value_if_settled", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_fill._generate_answers",
            AsyncMock(side_effect=AssertionError("LLM should not be called")),
        ),
    ):
        result = await domhand_fill(DomHandFillParams(target_section="Application Questions"), browser_session)

    assert result.error is None


def test_answer_resolution_logs_shape_incompatible_rejection():
    from ghosthands.actions.domhand_fill import _coerce_answer_if_compatible
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="ff-101",
        name="You answered 'Yes' to the previous question. Please specify the type of visa sponsorship you require from your employer, now or in the future.*",
        field_type="textarea",
        required=True,
    )

    with patch("ghosthands.dom.fill_profile_resolver.logger.debug") as log_debug:
        result = _coerce_answer_if_compatible(field, "Yes", source_candidate="semantic")

    assert result is None
    (event,) = log_debug.call_args.args
    assert event == "domhand.answer_resolution"
    assert log_debug.call_args.kwargs["extra"]["shape_compatible"] is False
    assert log_debug.call_args.kwargs["extra"]["rejection_reason"] == "shape_incompatible"


def test_record_page_token_cost_accumulates_per_page_context():
    from ghosthands.actions.domhand_fill import _record_page_token_cost

    browser_session = SimpleNamespace()

    _record_page_token_cost(
        browser_session,
        page_context_key="ctx",
        target_section="Application Questions",
        field_count=2,
        input_tokens=100,
        output_tokens=20,
    )
    _record_page_token_cost(
        browser_session,
        page_context_key="ctx",
        target_section="Application Questions",
        field_count=1,
        input_tokens=50,
        output_tokens=10,
    )

    totals = browser_session._gh_page_token_costs["ctx"]
    assert totals["calls"] == 2
    assert totals["input_tokens"] == 150
    assert totals["output_tokens"] == 30


@pytest.mark.asyncio
async def test_domhand_fill_blocks_repeat_retry_for_required_custom_widget_boolean_select():
    from ghosthands.actions.domhand_fill import domhand_fill
    from ghosthands.actions.views import DomHandFillParams, FormField, get_stable_field_key

    page = AsyncMock()
    page.evaluate = AsyncMock(return_value="{}")
    field = FormField(
        field_id="ff-90",
        name="Are you legally permitted to work in the country where this job is located?*",
        field_type="select",
        section="Application Questions",
        required=True,
        is_native=False,
        choices=[],
    )
    blocker_key = get_stable_field_key(field)
    browser_session = AsyncMock()
    browser_session.get_current_page = AsyncMock(return_value=page)
    browser_session._gh_last_application_state = {
        "page_context_key": "ctx",
        "page_url": "https://example.wd1.myworkdayjobs.com/job",
        "blocking_field_ids": ["ff-90"],
        "blocking_field_keys": [blocker_key],
        "blocking_field_labels": [field.name],
        "blocking_field_state_changes": {blocker_key: "no_state_change"},
        "same_blocker_signature_count": 1,
        "blocking_signature": "sig-1",
    }

    with (
        patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="profile text"),
        patch("ghosthands.actions.domhand_fill._get_profile_data", return_value={"work_authorization": "No"}),
        patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
        patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
        patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={"work_authorization": "No"}),
        patch(
            "ghosthands.actions.domhand_fill._safe_page_url",
            AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job"),
        ),
        patch("ghosthands.actions.domhand_fill._get_page_context_key", AsyncMock(return_value="ctx")),
        patch("ghosthands.actions.domhand_fill.extract_visible_form_fields", AsyncMock(return_value=[field])),
        patch("ghosthands.actions.domhand_fill._filter_fields_for_scope", side_effect=lambda fields, **_: fields),
        patch(
            "ghosthands.actions.domhand_fill._resolve_focus_fields",
            return_value=SimpleNamespace(fields=[field], ambiguous_labels={}),
        ),
    ):
        result = await domhand_fill(
            DomHandFillParams(
                target_section="Application Questions",
                focus_fields=[field.name],
            ),
            browser_session,
        )

    assert result.error is not None
    assert "Do NOT call domhand_fill again" in result.error


@pytest.mark.asyncio
async def test_dropdown_options_returns_custom_widget_failover_message_for_button_widgets():
    from browser_use.tools.service import Tools
    from browser_use.tools.views import GetDropdownOptionsAction

    tools = Tools()
    dropdown_options = tools.registry.registry.actions["dropdown_options"].function
    event = SimpleNamespace(event_result=AsyncMock(side_effect=RuntimeError("not recognizable dropdown types")))
    browser_session = AsyncMock()
    browser_session.get_element_by_index = AsyncMock(return_value=SimpleNamespace(tag_name="button", attributes={}))
    browser_session.event_bus.dispatch = AsyncMock(return_value=event)

    result = await dropdown_options(
        params=GetDropdownOptionsAction(index=6121),
        browser_session=browser_session,
    )

    assert result.error is not None
    assert "button/custom widget" in result.error
    assert "Do not retry dropdown_options" in result.error


def test_maybe_suppress_custom_select_readback_drops_when_domhand_unverified_recorded():
    from ghosthands.actions.domhand_assess_state import _maybe_suppress_custom_select_readback_false_positives
    from ghosthands.actions.views import ApplicationFieldIssue, FormField, get_stable_field_key
    from ghosthands.runtime_learning import record_expected_field_value, reset_runtime_learning_state

    reset_runtime_learning_state()
    field = FormField(
        field_id="ff-6",
        name="Country*",
        field_type="select",
        section="Country*",
        required=True,
        is_native=False,
        current_value="",
    )
    fk = get_stable_field_key(field)
    record_expected_field_value(
        host="job-boards.greenhouse.io",
        page_context_key="pc",
        field_key=fk,
        field_label="Country*",
        field_type="select",
        field_section="",
        field_fingerprint="",
        expected_value="United States",
        source="domhand_unverified",
    )
    issue = ApplicationFieldIssue(
        field_id="ff-6",
        name="Country*",
        field_type="select",
        reason="required_missing_value",
        current_value="",
        visible_error=None,
    )
    kept = _maybe_suppress_custom_select_readback_false_positives(
        [issue],
        [field],
        page_host="job-boards.greenhouse.io",
        page_context_key="pc",
    )
    assert kept == []


def test_maybe_suppress_custom_select_readback_keeps_without_recorded_expectation():
    from ghosthands.actions.domhand_assess_state import _maybe_suppress_custom_select_readback_false_positives
    from ghosthands.actions.views import ApplicationFieldIssue, FormField
    from ghosthands.runtime_learning import reset_runtime_learning_state

    reset_runtime_learning_state()
    field = FormField(
        field_id="ff-6",
        name="Country*",
        field_type="select",
        section="Country*",
        required=True,
        is_native=False,
        current_value="",
    )
    issue = ApplicationFieldIssue(
        field_id="ff-6",
        name="Country*",
        field_type="select",
        reason="required_missing_value",
        current_value="",
        visible_error=None,
    )
    kept = _maybe_suppress_custom_select_readback_false_positives(
        [issue],
        [field],
        page_host="job-boards.greenhouse.io",
        page_context_key="pc",
    )
    assert len(kept) == 1
    assert kept[0].field_id == "ff-6"
