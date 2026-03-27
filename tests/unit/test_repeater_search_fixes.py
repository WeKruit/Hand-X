"""Unit tests for Oracle HCM repeater scenario fixes.

Covers:
- GPA dropdown nearest-match (match_dropdown_option pass 6)
- Phone code stripping skips pure numeric values (_strip_phone_code_norm)
- School name stop words in generate_dropdown_search_terms
- Numeric search term truncation in generate_dropdown_search_terms
- Select coercion rejects long sentence-like values (_coerce_answer_to_field)
- Education location slot names (_education_slot_name)
- JSON control char repair (_repair_invalid_json_string_escapes)
- Field label sanitization (_disambiguated_field_names)
- Employer combobox fallback helpers

All tests are offline (no browser, no database, no API calls).
"""

import json

import pytest

from ghosthands.actions.views import FormField, generate_dropdown_search_terms, normalize_name
from ghosthands.dom.dropdown_match import _strip_phone_code_norm, match_dropdown_option


# ── Helpers ───────────────────────────────────────────────────────────

GPA_OPTIONS = [
    "Below 2.0",
    "2.0", "2.1", "2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "2.8", "2.9",
    "3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "3.8", "3.9",
    "4.0", "4.1", "4.2", "4.3",
]


def _make_field(
    *,
    name: str = "Test Field",
    field_type: str = "text",
    options: list[str] | None = None,
    raw_label: str | None = None,
    section: str = "",
    field_id: str = "ff-1",
    name_attr: str = "",
) -> FormField:
    return FormField(
        field_id=field_id,
        name=name,
        field_type=field_type,
        options=options or [],
        raw_label=raw_label,
        section=section,
        name_attr=name_attr,
    )


# ═══════════════════════════════════════════════════════════════════════
# 1. GPA dropdown nearest-match (match_dropdown_option)
# ═══════════════════════════════════════════════════════════════════════


class TestGPADropdownNearestMatch:
    """Pass 6 of match_dropdown_option: closest numeric match for GPA values."""

    def test_floor_391_to_39(self):
        assert match_dropdown_option("3.91", GPA_OPTIONS) == "3.9"

    def test_floor_381_to_38(self):
        assert match_dropdown_option("3.81", GPA_OPTIONS) == "3.8"

    def test_exact_38(self):
        assert match_dropdown_option("3.8", GPA_OPTIONS) == "3.8"

    def test_floor_275_to_27(self):
        assert match_dropdown_option("2.75", GPA_OPTIONS) == "2.7"

    def test_exact_40(self):
        assert match_dropdown_option("4.0", GPA_OPTIONS) == "4.0"

    def test_below_min_picks_closest_above(self):
        """1.5 is below all numeric options; should pick the closest above (2.0)."""
        result = match_dropdown_option("1.5", GPA_OPTIONS)
        assert result == "2.0"


# ═══════════════════════════════════════════════════════════════════════
# 2. Phone code stripping skips pure numeric (_strip_phone_code_norm)
# ═══════════════════════════════════════════════════════════════════════


class TestStripPhoneCodeNorm:
    """_strip_phone_code_norm must not alter pure numeric strings like GPA values."""

    def test_pure_numeric_391_unchanged(self):
        assert _strip_phone_code_norm("3.91") == "3.91"

    def test_pure_numeric_30_unchanged(self):
        assert _strip_phone_code_norm("3.0") == "3.0"

    def test_country_with_code_stripped(self):
        assert _strip_phone_code_norm("united states 1") == "united states"

    def test_canada_with_code_stripped(self):
        assert _strip_phone_code_norm("canada 1") == "canada"

    def test_country_without_code_unchanged(self):
        assert _strip_phone_code_norm("france") == "france"


# ═══════════════════════════════════════════════════════════════════════
# 3. School name stop words (generate_dropdown_search_terms)
# ═══════════════════════════════════════════════════════════════════════


class TestSchoolNameStopWords:
    """Stop words like 'University', 'College', 'Technology' must not appear as standalone search terms."""

    def test_nyu_no_university_alone(self):
        terms = generate_dropdown_search_terms("New York University")
        norm_terms = [normalize_name(t) for t in terms]
        assert "university" not in norm_terms
        assert "New York University" in terms

    def test_ucla_produces_single_term(self):
        terms = generate_dropdown_search_terms("UCLA")
        assert terms == ["UCLA"]

    def test_mit_no_technology_or_institute_alone(self):
        terms = generate_dropdown_search_terms("Massachusetts Institute of Technology")
        norm_terms = [normalize_name(t) for t in terms]
        assert "technology" not in norm_terms
        assert "institute" not in norm_terms
        assert "Massachusetts Institute of Technology" in terms

    def test_georgia_tech_includes_full_name(self):
        terms = generate_dropdown_search_terms("Georgia Tech")
        assert "Georgia Tech" in terms


# ═══════════════════════════════════════════════════════════════════════
# 4. Numeric search term truncation (generate_dropdown_search_terms)
# ═══════════════════════════════════════════════════════════════════════


class TestNumericSearchTermTruncation:
    """Numeric values with extra decimal digits get a truncated variant."""

    def test_391_produces_truncated(self):
        terms = generate_dropdown_search_terms("3.91")
        assert "3.91" in terms
        assert "3.9" in terms

    def test_38_no_truncation(self):
        terms = generate_dropdown_search_terms("3.8")
        assert terms == ["3.8"]

    def test_40_no_truncation(self):
        terms = generate_dropdown_search_terms("4.0")
        assert terms == ["4.0"]

    def test_275_produces_truncated(self):
        terms = generate_dropdown_search_terms("2.75")
        assert "2.75" in terms
        assert "2.7" in terms


# ═══════════════════════════════════════════════════════════════════════
# 5. Select coercion rejects long values (_coerce_answer_to_field)
# ═══════════════════════════════════════════════════════════════════════


class TestSelectCoercionRejectsLongValues:
    """Select fields without options should reject sentence-like values."""

    def test_short_value_accepted(self):
        from ghosthands.dom.fill_label_match import _coerce_answer_to_field

        field = _make_field(name="Visa Status", field_type="select", options=[])
        result = _coerce_answer_to_field(field, "N/A")
        assert result == "N/A"

    def test_long_sentence_rejected(self):
        from ghosthands.dom.fill_label_match import _coerce_answer_to_field

        field = _make_field(name="Work Authorization", field_type="select", options=[])
        result = _coerce_answer_to_field(
            field, "Likely yes, based on US location (Chantilly, VA)"
        )
        assert result is None

    def test_many_spaces_rejected(self):
        from ghosthands.dom.fill_label_match import _coerce_answer_to_field

        field = _make_field(name="Country", field_type="select", options=[])
        result = _coerce_answer_to_field(
            field, "some very long sentence with many many many words"
        )
        assert result is None

    def test_select_with_matching_option_accepted(self):
        from ghosthands.dom.fill_label_match import _coerce_answer_to_field

        field = _make_field(
            name="Country",
            field_type="select",
            options=["United States", "Canada", "Mexico"],
        )
        result = _coerce_answer_to_field(field, "United States")
        assert result == "United States"

    def test_text_field_long_value_not_rejected(self):
        """Non-select fields should not have the length gate applied."""
        from ghosthands.dom.fill_label_match import _coerce_answer_to_field

        field = _make_field(name="Description", field_type="text", options=[])
        long_text = "This is a long descriptive answer with many many many words in it"
        result = _coerce_answer_to_field(field, long_text)
        assert result == long_text


# ═══════════════════════════════════════════════════════════════════════
# 6. Education location slot names (_education_slot_name)
# ═══════════════════════════════════════════════════════════════════════


class TestEducationSlotName:
    """_education_slot_name maps field labels to structured education slots."""

    def test_country_maps_to_school_country(self):
        from ghosthands.dom.fill_resolution import _education_slot_name

        field = _make_field(name="Country", section="Education")
        assert _education_slot_name(field) == "school_country"

    def test_state_maps_to_school_state(self):
        from ghosthands.dom.fill_resolution import _education_slot_name

        field = _make_field(name="State", section="Education")
        assert _education_slot_name(field) == "school_state"

    def test_city_maps_to_school_city(self):
        from ghosthands.dom.fill_resolution import _education_slot_name

        field = _make_field(name="City", section="Education")
        assert _education_slot_name(field) == "school_city"

    def test_school_label(self):
        from ghosthands.dom.fill_resolution import _education_slot_name

        field = _make_field(name="School", section="Education")
        assert _education_slot_name(field) == "school"

    def test_degree_label(self):
        from ghosthands.dom.fill_resolution import _education_slot_name

        field = _make_field(name="Degree", section="Education")
        assert _education_slot_name(field) == "degree"

    def test_gpa_label(self):
        from ghosthands.dom.fill_resolution import _education_slot_name

        field = _make_field(name="GPA", section="Education")
        assert _education_slot_name(field) == "gpa"

    def test_university_label_maps_to_school(self):
        from ghosthands.dom.fill_resolution import _education_slot_name

        field = _make_field(name="University", section="Education")
        assert _education_slot_name(field) == "school"

    def test_field_of_study_label(self):
        from ghosthands.dom.fill_resolution import _education_slot_name

        field = _make_field(name="Field of Study", section="Education")
        assert _education_slot_name(field) == "field_of_study"


# ═══════════════════════════════════════════════════════════════════════
# 7. JSON control char repair (_repair_invalid_json_string_escapes)
# ═══════════════════════════════════════════════════════════════════════


class TestRepairInvalidJsonStringEscapes:
    """Literal control characters inside JSON strings must be escaped."""

    def test_literal_newline_escaped(self):
        from ghosthands.dom.fill_llm_answers import _repair_invalid_json_string_escapes

        # Construct JSON with a literal newline inside a string value
        bad_json = '{"key": "line1\nline2"}'
        repaired = _repair_invalid_json_string_escapes(bad_json)
        parsed = json.loads(repaired)
        assert parsed["key"] == "line1\nline2"

    def test_literal_tab_escaped(self):
        from ghosthands.dom.fill_llm_answers import _repair_invalid_json_string_escapes

        bad_json = '{"key": "col1\tcol2"}'
        repaired = _repair_invalid_json_string_escapes(bad_json)
        parsed = json.loads(repaired)
        assert parsed["key"] == "col1\tcol2"

    def test_normal_json_unchanged(self):
        from ghosthands.dom.fill_llm_answers import _repair_invalid_json_string_escapes

        good_json = '{"key": "normal value", "num": 42}'
        repaired = _repair_invalid_json_string_escapes(good_json)
        assert json.loads(repaired) == {"key": "normal value", "num": 42}

    def test_already_escaped_newline_preserved(self):
        from ghosthands.dom.fill_llm_answers import _repair_invalid_json_string_escapes

        good_json = '{"key": "line1\\nline2"}'
        repaired = _repair_invalid_json_string_escapes(good_json)
        parsed = json.loads(repaired)
        assert parsed["key"] == "line1\nline2"

    def test_literal_carriage_return_escaped(self):
        from ghosthands.dom.fill_llm_answers import _repair_invalid_json_string_escapes

        bad_json = '{"key": "line1\rline2"}'
        repaired = _repair_invalid_json_string_escapes(bad_json)
        parsed = json.loads(repaired)
        assert parsed["key"] == "line1\rline2"


# ═══════════════════════════════════════════════════════════════════════
# 8. Field label sanitization (_disambiguated_field_names)
# ═══════════════════════════════════════════════════════════════════════


class TestDisambiguatedFieldNames:
    """Control characters in field labels must be sanitized for JSON key safety."""

    def test_newlines_replaced_with_spaces(self):
        from ghosthands.dom.fill_llm_answers import _disambiguated_field_names

        field = _make_field(name="Question\n\nWith Newlines", raw_label="Question\n\nWith Newlines")
        names = _disambiguated_field_names([field])
        assert len(names) == 1
        # No literal newlines in the output
        assert "\n" not in names[0]
        # Content is preserved (collapsed to spaces)
        assert "Question" in names[0]
        assert "With Newlines" in names[0]

    def test_normal_label_unchanged(self):
        from ghosthands.dom.fill_llm_answers import _disambiguated_field_names

        field = _make_field(name="First Name", raw_label="First Name")
        names = _disambiguated_field_names([field])
        assert names == ["First Name"]

    def test_tab_characters_sanitized(self):
        from ghosthands.dom.fill_llm_answers import _disambiguated_field_names

        field = _make_field(name="Label\tWith\tTabs", raw_label="Label\tWith\tTabs")
        names = _disambiguated_field_names([field])
        assert "\t" not in names[0]

    def test_duplicate_labels_get_suffix(self):
        from ghosthands.dom.fill_llm_answers import _disambiguated_field_names

        fields = [
            _make_field(name="Phone Number", field_id="ff-1"),
            _make_field(name="Phone Number", field_id="ff-2"),
        ]
        names = _disambiguated_field_names(fields)
        assert names[0] == "Phone Number"
        assert names[1] == "Phone Number #2"


# ═══════════════════════════════════════════════════════════════════════
# 9. Employer combobox fallback
# ═══════════════════════════════════════════════════════════════════════


class TestEmployerComboboxFallback:
    """Helpers for latest-employer Oracle combobox detection and value extraction."""

    def test_is_latest_employer_search_field_true(self):
        from ghosthands.actions.domhand_fill import _is_latest_employer_search_field

        field = _make_field(
            name="Name of Latest Employer",
            field_type="select",
        )
        assert _is_latest_employer_search_field(field) is True

    def test_is_latest_employer_search_field_text_type_false(self):
        from ghosthands.actions.domhand_fill import _is_latest_employer_search_field

        field = _make_field(
            name="Name of Latest Employer",
            field_type="text",
        )
        assert _is_latest_employer_search_field(field) is False

    def test_is_latest_employer_search_field_unrelated_label(self):
        from ghosthands.actions.domhand_fill import _is_latest_employer_search_field

        field = _make_field(name="First Name", field_type="select")
        assert _is_latest_employer_search_field(field) is False

    def test_current_employer_variant(self):
        from ghosthands.actions.domhand_fill import _is_latest_employer_search_field

        field = _make_field(name="Current Employer", field_type="select")
        assert _is_latest_employer_search_field(field) is True

    def test_most_recent_employer_variant(self):
        from ghosthands.actions.domhand_fill import _is_latest_employer_search_field

        field = _make_field(name="Most Recent Employer", field_type="select")
        assert _is_latest_employer_search_field(field) is True

    def test_current_or_latest_employer_name_from_top_level(self):
        from ghosthands.dom.fill_profile_resolver import _current_or_latest_employer_name

        profile = {"current_company": "Acme Corp"}
        assert _current_or_latest_employer_name(profile) == "Acme Corp"

    def test_current_or_latest_employer_name_from_experience(self):
        from ghosthands.dom.fill_profile_resolver import _current_or_latest_employer_name

        profile = {
            "experience": [
                {"company": "Old Corp", "currently_work_here": False},
                {"company": "Current Corp", "currently_work_here": True},
            ]
        }
        assert _current_or_latest_employer_name(profile) == "Current Corp"

    def test_current_or_latest_employer_name_fallback_to_first(self):
        from ghosthands.dom.fill_profile_resolver import _current_or_latest_employer_name

        profile = {
            "experience": [
                {"company": "First Corp"},
                {"company": "Second Corp"},
            ]
        }
        assert _current_or_latest_employer_name(profile) == "First Corp"

    def test_current_or_latest_employer_name_none_profile(self):
        from ghosthands.dom.fill_profile_resolver import _current_or_latest_employer_name

        assert _current_or_latest_employer_name(None) is None

    def test_current_or_latest_employer_name_empty_experience(self):
        from ghosthands.dom.fill_profile_resolver import _current_or_latest_employer_name

        profile = {"experience": []}
        assert _current_or_latest_employer_name(profile) is None
