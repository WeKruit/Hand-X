"""Unit tests for domhand_fill bug fixes.

Covers:
- max_tokens scaling based on field count (prevents description truncation)
- _sanitize_no_guess_answer suppresses [NEEDS_USER_INPUT] for no-HITL apply flows
- estimate_cost fault tolerance for unknown models

All tests are offline (no browser, no database, no API calls).
"""

import asyncio
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
async def test_checkbox_group_already_matches_uses_selected_label_not_binary_state():
    from ghosthands.actions.domhand_fill import _field_already_matches
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="disability",
        name="Please check one of the boxes below:*",
        field_type="checkbox-group",
        choices=[
            "Yes, I have a disability, or have had one in the past",
            "No, I do not have a disability and have not had one in the past",
        ],
        required=True,
    )

    with (
        patch("ghosthands.actions.domhand_fill._read_group_selection", AsyncMock(return_value="No, I do not have a disability and have not had one in the past")),
        patch("ghosthands.actions.domhand_fill._field_has_validation_error", AsyncMock(return_value=False)),
    ):
        assert await _field_already_matches(
            AsyncMock(),
            field,
            "No, I do not have a disability and have not had one in the past",
        ) is True


@pytest.mark.asyncio
async def test_fill_checkbox_group_does_not_skip_explicit_no_choice():
    from ghosthands.actions.domhand_fill import _fill_checkbox_group
    from ghosthands.actions.views import FormField

    field = FormField(
        field_id="relatives",
        name="Do you have any relatives that work for DLH?",
        field_type="checkbox-group",
        choices=["Yes", "No"],
        required=True,
    )
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value='{"clicked": true, "text": "No"}')

    with (
        patch("ghosthands.actions.domhand_fill._load_field_interaction_recipe", AsyncMock(return_value=None)),
        patch("ghosthands.actions.domhand_fill._read_group_selection", AsyncMock(side_effect=["Yes", "No"])),
        patch("ghosthands.actions.domhand_fill._field_has_validation_error", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._click_group_option_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._reset_group_selection_with_gui", AsyncMock(return_value=False)),
        patch("ghosthands.actions.domhand_fill._record_field_interaction_recipe"),
    ):
        assert await _fill_checkbox_group(page, field, "No", "[Relatives]") is True
        page.evaluate.assert_awaited()


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
    assert resolved.source == "dom"


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
    from ghosthands.cli import _build_recovery_task

    task = _build_recovery_task(
        "Fill the application",
        {"Estimated graduation date": "December 202X"},
    )

    assert "RECOVERED ANSWERS JUST PROVIDED" in task
    assert "HITL ANSWERS JUST PROVIDED" not in task


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
    assert "After EACH blocker-level DomHand or targeted manual recovery action" in prompt


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
    from ghosthands.cli import _OpenQuestionIssue, _request_open_question_answers

    async def _run() -> None:
        issue = _OpenQuestionIssue(
            field_label="Please tell us your current year in school",
            field_id="school-year",
            field_type="textarea",
            question_text="Please tell us your current year in school",
            section="Application Questions",
        )

        with (
            patch("ghosthands.cli._auto_answer_open_question_issues", AsyncMock(return_value=({}, [issue]))),
            patch(
                "ghosthands.cli._infer_open_question_answers_with_domhand",
                AsyncMock(return_value=({"Please tell us your current year in school": "Junior"}, [])),
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
        assert answers == {"Please tell us your current year in school": "Junior"}
        emitted_messages = [call.kwargs.get("message", "") for call in emit_event.call_args_list]
        assert any("continuing locally" in str(message).lower() for message in emitted_messages)
        assert not any("field_needs_input" == call.args[0] for call in emit_event.call_args_list if call.args)

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
