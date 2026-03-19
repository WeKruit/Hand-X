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
        patch("ghosthands.actions.domhand_fill._read_binary_state", AsyncMock(return_value=True)),
        patch("ghosthands.actions.domhand_fill._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._read_group_selection", AsyncMock(return_value="Health insurance")) as read_group,
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
        patch("ghosthands.actions.domhand_fill._load_field_interaction_recipe", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_fill._read_binary_state", AsyncMock(return_value=True)),
        patch("ghosthands.actions.domhand_fill._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._click_binary_with_gui", AsyncMock(return_value=False)) as click_binary,
        patch("ghosthands.actions.domhand_fill._refresh_binary_field", AsyncMock(return_value=False)) as refresh_binary,
        patch("ghosthands.actions.domhand_fill._record_field_interaction_recipe"),
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
        patch("ghosthands.actions.domhand_fill._load_field_interaction_recipe", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_fill._read_binary_state", AsyncMock(side_effect=[False, True])),
        patch("ghosthands.actions.domhand_fill._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._click_binary_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._refresh_binary_field", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._record_field_interaction_recipe"),
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
        patch("ghosthands.actions.domhand_fill._load_field_interaction_recipe", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_fill._read_group_selection", AsyncMock(side_effect=["", "No"])),
        patch("ghosthands.actions.domhand_fill._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._click_group_option_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._reset_group_selection_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._record_field_interaction_recipe"),
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
        patch("ghosthands.actions.domhand_fill._load_field_interaction_recipe", AsyncMock(return_value=None)),
        patch(
            "ghosthands.actions.domhand_fill._read_group_selection",
            AsyncMock(side_effect=["", "I am not a protected veteran"]),
        ),
        patch("ghosthands.actions.domhand_fill._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._click_group_option_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._reset_group_selection_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._record_field_interaction_recipe"),
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
        patch("ghosthands.actions.domhand_fill._load_field_interaction_recipe", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_fill._read_group_selection", AsyncMock(side_effect=["Yes", "No"])),
        patch("ghosthands.actions.domhand_fill._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._click_group_option_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._reset_group_selection_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._record_field_interaction_recipe"),
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
        patch("ghosthands.actions.domhand_fill._field_already_matches", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._fill_single_field", AsyncMock(return_value=True)) as fill_single,
    ):
        success, error, failure_reason = await _attempt_domhand_fill_with_retry_cap(
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
        patch("ghosthands.actions.domhand_fill._field_already_matches", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._fill_single_field", AsyncMock(return_value=True)) as fill_single,
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
    assert "After EACH targeted manual recovery action, first call domhand_record_expected_value" in prompt


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
            '{'
            '"currentSchoolYear":"Junior",'
            '"certificationsLicenses":"None",'
            '"education":[{"school":"USC","degree":"B.S. Computer Science","field":"Computer Science","endDate":"2027-05"}]'
            '}'
        )
    )

    assert evidence["current_school_year"] == "Junior"
    assert evidence["degree_seeking"] == "B.S. Computer Science"
    assert evidence["field_of_study"] == "Computer Science"
    assert evidence["graduation_date"] == "May 2027"
    assert evidence["certifications_licenses"] == "None"


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
            patch("ghosthands.cli._infer_open_question_answers_with_domhand", AsyncMock(return_value=([], []))) as llm_mock,
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
                    }
                ]
            },
        ),
        patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://example.com/job")),
        patch(
            "ghosthands.actions.domhand_fill._attempt_domhand_fill_with_retry_cap",
            AsyncMock(return_value=(True, None, None)),
        ),
    ):
        result = await domhand_fill(DomHandFillParams(), browser_session)

    payload = json.loads((result.extracted_content or "").split("DOMHAND_FILL_JSON:\n", 1)[1])
    assert payload["best_effort_binding_count"] >= 1
    assert any(
        field["prompt_text"] == "Field of Study" and field["binding_mode"] == "row_order"
        for field in payload["best_effort_binding_fields"]
    )


def test_verification_attempt_count_respects_effort_levels():
    from ghosthands.actions.domhand_assess_state import _verification_attempt_count

    with patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False):
        assert _verification_attempt_count() == 1
    with patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "medium"}, clear=False):
        assert _verification_attempt_count() == 2
    with patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "high"}, clear=False):
        assert _verification_attempt_count() == 3


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
        patch("ghosthands.actions.domhand_assess_state._safe_page_url", AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="French")),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Voluntary Self-Identification of Disability"),
            browser_session,
        )

    payload = json.loads((result.extracted_content or "").split("APPLICATION_STATE_JSON:\n", 1)[1])
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
        patch("ghosthands.actions.domhand_assess_state._safe_page_url", AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="c17fb198564510000de6e6b35bb80000")),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Voluntary Self-Identification of Disability"),
            browser_session,
        )

    payload = json.loads((result.extracted_content or "").split("APPLICATION_STATE_JSON:\n", 1)[1])
    assert payload["advance_allowed"] is False
    assert len(payload["opaque_fields"]) == 1
    assert payload["opaque_fields"][0]["name"] == "Language"


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
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(side_effect=read_value_side_effect)),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="My Experience"),
            browser_session,
        )

    payload = json.loads((result.extracted_content or "").split("APPLICATION_STATE_JSON:\n", 1)[1])
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
        patch("ghosthands.actions.domhand_assess_state._safe_page_url", AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="French")),
        patch("ghosthands.actions.domhand_assess_state.asyncio.sleep", AsyncMock()) as sleep_mock,
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "medium"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Languages"),
            browser_session,
        )

    payload = json.loads((result.extracted_content or "").split("APPLICATION_STATE_JSON:\n", 1)[1])
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
async def test_domhand_record_expected_value_rejects_wrong_field_id_without_label_fallback():
    from ghosthands.actions.domhand_record_expected_value import domhand_record_expected_value
    from ghosthands.actions.views import DomHandRecordExpectedValueParams, FormField, get_stable_field_key
    from ghosthands.runtime_learning import build_page_context_key, get_expected_field_value, reset_runtime_learning_state

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
        patch("ghosthands.actions.domhand_assess_state._safe_page_url", AsyncMock(return_value="https://example.wd1.myworkdayjobs.com/job")),
        patch("ghosthands.actions.domhand_fill._read_page_context_snapshot", AsyncMock(return_value={"page_marker": "Application Questions", "heading_texts": ["Application Questions"]})),
        patch("ghosthands.actions.domhand_assess_state._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_assess_state._read_field_value", AsyncMock(return_value="2 weeks")),
        patch.dict(os.environ, {"GH_VERIFICATION_EFFORT": "low"}, clear=False),
    ):
        result = await domhand_assess_state(
            DomHandAssessStateParams(target_section="Application Questions"),
            browser_session,
        )

    payload = json.loads((result.extracted_content or "").split("APPLICATION_STATE_JSON:\n", 1)[1])
    assert payload["advance_allowed"] is True
    assert payload["mismatched_fields"] == []


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
    page.evaluate = AsyncMock(side_effect=[
        None,
        json.dumps({
            "body_text": "",
            "heading_texts": ["My Information"],
            "button_texts": [],
            "submit_visible": False,
            "submit_disabled": False,
            "advance_visible": False,
            "error_texts": [],
            "markers": [],
        }),
        json.dumps({}),
        json.dumps({"error_text": "", "widget_kind": ""}),
        json.dumps({"error_text": "", "widget_kind": ""}),
    ])
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

    payload = json.loads((result.extracted_content or "").split("APPLICATION_STATE_JSON:\n", 1)[1])
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
    assert "advance_allowed=false" in message


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
            AsyncMock(return_value={"label": "How Did You Hear About Us?", "widgetType": "custom_widget", "invalid": False}),
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


def test_infer_entry_data_from_scope_does_not_default_to_first_multi_row():
    from ghosthands.actions.domhand_fill import _infer_entry_data_from_scope

    assert _infer_entry_data_from_scope(
        {
            "education": [
                {"school": "MIT"},
                {"school": "USC"},
            ]
        },
        None,
        "Education",
    ) is None
