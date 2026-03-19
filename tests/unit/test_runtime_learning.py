import json

import pytest

from ghosthands.actions.views import FormField
from ghosthands.runtime_learning import (
    build_page_context_key,
    export_runtime_learning_payload,
    get_expected_field_value,
    get_interaction_recipe,
    get_learned_question_alias,
    record_expected_field_value,
    record_interaction_recipe,
    reset_runtime_learning_state,
    stage_learned_question_alias,
    confirm_learned_question_alias,
)


def setup_function() -> None:
    reset_runtime_learning_state()


def test_runtime_learning_loads_learned_aliases_from_profile_payload():
    profile = {
        "learnedQuestionAliases": [
            {
                "normalizedLabel": "are you legally eligible to work here",
                "intent": "work_authorization",
                "source": "semantic_fallback",
                "confidence": "high",
            }
        ]
    }

    alias = get_learned_question_alias("Are you legally eligible to work here?", profile)

    assert alias is not None
    assert alias.intent == "work_authorization"
    assert alias.normalized_label == "are you legally eligible to work here"


def test_runtime_learning_stages_and_confirms_new_aliases():
    stage_learned_question_alias(
        "Would you require immigration sponsorship now or later?",
        "visa_sponsorship",
    )
    confirm_learned_question_alias("Would you require immigration sponsorship now or later?")

    payload = export_runtime_learning_payload()

    assert payload["learned_question_aliases"] == [
        {
            "normalized_label": "would you require immigration sponsorship now or later",
            "intent": "visa_sponsorship",
            "source": "semantic_fallback",
            "confidence": "high",
        }
    ]


def test_runtime_learning_loads_and_records_interaction_recipes():
    profile = {
        "learnedInteractionRecipes": [
            {
                "platform": "workday",
                "host": "example.wd1.myworkdayjobs.com",
                "normalizedLabel": "how did you hear about us",
                "widgetSignature": "custom_popup",
                "preferredActionChain": ["typed_search"],
                "source": "visual_fallback",
            }
        ]
    }

    loaded = get_interaction_recipe(
        platform="workday",
        host="example.wd1.myworkdayjobs.com",
        label="How did you hear about us?",
        widget_signature="custom_popup",
        profile_data=profile,
    )

    assert loaded is not None
    assert loaded.preferred_action_chain == ["typed_search"]

    record_interaction_recipe(
        platform="workday",
        host="example.wd1.myworkdayjobs.com",
        label="How did you hear about us?",
        widget_signature="custom_popup",
        preferred_action_chain=["typed_search"],
    )

    payload = export_runtime_learning_payload()
    assert payload["learned_interaction_recipes"] == []


@pytest.mark.asyncio
async def test_semantic_profile_value_uses_learned_alias_without_llm():
    from ghosthands.actions.domhand_fill import _parse_profile_evidence, _semantic_profile_value_for_field

    profile = {
        "work_authorization": "Yes",
        "learnedQuestionAliases": [
            {
                "normalizedLabel": "do you have legal authorization to work here",
                "intent": "work_authorization",
                "source": "semantic_fallback",
                "confidence": "high",
            }
        ],
    }
    field = FormField(
        field_id="field-1",
        name="Do you have legal authorization to work here?",
        raw_label="Do you have legal authorization to work here?",
        field_type="select",
        required=True,
        options=["Yes", "No"],
    )

    answer = await _semantic_profile_value_for_field(
        field,
        _parse_profile_evidence(json.dumps(profile)),
        profile,
    )

    assert answer == "Yes"


def test_expected_field_values_are_isolated_by_page_context():
    field = FormField(
        field_id="field-1",
        name="Language",
        field_type="select",
        required=True,
    )
    my_info_context = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="My Information",
    )
    questions_context = build_page_context_key(
        url="https://example.wd1.myworkdayjobs.com/job",
        page_marker="Application Questions",
    )

    record_expected_field_value(
        host="example.wd1.myworkdayjobs.com",
        page_context_key=my_info_context,
        field_key="select|field-1",
        field_label=field.name,
        expected_value="English",
        source="exact_profile",
    )

    assert (
        get_expected_field_value(
            host="example.wd1.myworkdayjobs.com",
            page_context_key=questions_context,
            field_key="select|field-1",
        )
        is None
    )
