"""Unit tests for repeater pre-fill observation building blocks.

Validates anchor label matching, profile anchor key extraction, and the
ObservationResult data contract from domhand_fill_repeaters.

  uv run pytest tests/unit/test_observation_anchors.py -v
"""

from __future__ import annotations

import pytest

from ghosthands.actions.domhand_fill_repeaters import (
    _ANCHOR_LABELS,
    _PROFILE_ANCHOR_KEYS,
    ObservationResult,
    _extract_profile_anchor_value,
    _is_anchor_field,
)


# ---------------------------------------------------------------------------
# 1. _ANCHOR_LABELS structure
# ---------------------------------------------------------------------------


class TestAnchorLabelsStructure:
    """Validate _ANCHOR_LABELS dict has expected sections and values."""

    def test_all_sections_present(self):
        expected = {"experience", "education", "languages", "skills", "licenses"}
        assert set(_ANCHOR_LABELS.keys()) == expected

    def test_experience_labels(self):
        labels = _ANCHOR_LABELS["experience"]
        assert "company" in labels
        assert "employer" in labels

    def test_education_labels(self):
        labels = _ANCHOR_LABELS["education"]
        assert "school" in labels
        assert "university" in labels

    def test_languages_labels(self):
        assert "language" in _ANCHOR_LABELS["languages"]

    def test_skills_labels(self):
        assert "skill" in _ANCHOR_LABELS["skills"]

    def test_licenses_labels(self):
        labels = _ANCHOR_LABELS["licenses"]
        assert "certification" in labels
        assert "license" in labels


# ---------------------------------------------------------------------------
# 2. _PROFILE_ANCHOR_KEYS structure
# ---------------------------------------------------------------------------


class TestProfileAnchorKeysStructure:
    """Validate _PROFILE_ANCHOR_KEYS dict has expected sections and keys."""

    def test_all_sections_present(self):
        expected = {"experience", "education", "languages", "skills", "licenses"}
        assert set(_PROFILE_ANCHOR_KEYS.keys()) == expected

    def test_experience_keys(self):
        keys = _PROFILE_ANCHOR_KEYS["experience"]
        assert "company" in keys

    def test_education_keys(self):
        keys = _PROFILE_ANCHOR_KEYS["education"]
        assert "school" in keys

    def test_skills_keys(self):
        keys = _PROFILE_ANCHOR_KEYS["skills"]
        assert "skill_name" in keys


# ---------------------------------------------------------------------------
# 3. _is_anchor_field
# ---------------------------------------------------------------------------


class TestIsAnchorField:
    """Validate anchor field label matching."""

    def test_school_is_education_anchor(self):
        assert _is_anchor_field("School", "education") is True

    def test_school_name_is_education_anchor(self):
        assert _is_anchor_field("School Name", "education") is True

    def test_institution_is_education_anchor(self):
        assert _is_anchor_field("Institution", "education") is True

    def test_university_is_education_anchor(self):
        assert _is_anchor_field("University", "education") is True

    def test_company_is_experience_anchor(self):
        assert _is_anchor_field("Company", "experience") is True

    def test_employer_name_is_experience_anchor(self):
        assert _is_anchor_field("Employer Name", "experience") is True

    def test_language_is_languages_anchor(self):
        assert _is_anchor_field("Language", "languages") is True

    def test_skill_is_skills_anchor(self):
        assert _is_anchor_field("Skill", "skills") is True

    def test_certification_is_licenses_anchor(self):
        assert _is_anchor_field("Certification Name", "licenses") is True

    # Negative cases
    def test_email_not_education_anchor(self):
        assert _is_anchor_field("Email", "education") is False

    def test_start_date_not_experience_anchor(self):
        assert _is_anchor_field("Start Date", "experience") is False

    def test_gpa_not_education_anchor(self):
        assert _is_anchor_field("GPA", "education") is False

    def test_description_not_experience_anchor(self):
        assert _is_anchor_field("Role Description", "experience") is False

    def test_empty_name_returns_false(self):
        assert _is_anchor_field("", "education") is False

    def test_unknown_section_returns_false(self):
        assert _is_anchor_field("Company", "unknown_section") is False

    # Cross-contamination
    def test_school_not_experience_anchor(self):
        assert _is_anchor_field("School", "experience") is False

    def test_company_not_education_anchor(self):
        assert _is_anchor_field("Company", "education") is False


# ---------------------------------------------------------------------------
# 4. _extract_profile_anchor_value
# ---------------------------------------------------------------------------


class TestExtractProfileAnchorValue:
    """Validate profile entry anchor value extraction."""

    def test_experience_company(self):
        assert _extract_profile_anchor_value("experience", {"company": "Google"}) == "google"

    def test_experience_employer_fallback(self):
        assert _extract_profile_anchor_value("experience", {"employer": "NASA"}) == "nasa"

    def test_experience_first_key_wins(self):
        """company is before employer in _PROFILE_ANCHOR_KEYS, so it wins."""
        result = _extract_profile_anchor_value("experience", {"company": "Google", "employer": "NASA"})
        assert result == "google"

    def test_education_school(self):
        assert _extract_profile_anchor_value("education", {"school": "MIT"}) == "mit"

    def test_skills_skill_name(self):
        assert _extract_profile_anchor_value("skills", {"skill_name": "Python"}) == "python"

    def test_languages_language(self):
        assert _extract_profile_anchor_value("languages", {"language": "Spanish"}) == "spanish"

    def test_licenses_certification_name(self):
        assert _extract_profile_anchor_value("licenses", {"certification_name": "AWS"}) == "aws"

    def test_empty_entry_returns_empty(self):
        assert _extract_profile_anchor_value("experience", {}) == ""

    def test_none_value_skipped(self):
        assert _extract_profile_anchor_value("experience", {"company": None}) == ""

    def test_whitespace_only_skipped(self):
        assert _extract_profile_anchor_value("experience", {"company": "  "}) == ""

    def test_unknown_section_returns_empty(self):
        assert _extract_profile_anchor_value("unknown", {"company": "Google"}) == ""

    def test_non_string_value_skipped(self):
        assert _extract_profile_anchor_value("experience", {"company": 123}) == ""

    def test_normalize_strips_special_chars(self):
        result = _extract_profile_anchor_value("education", {"school": "St. John's University"})
        assert "st. johns university" == result


# ---------------------------------------------------------------------------
# 5. ObservationResult dataclass
# ---------------------------------------------------------------------------


class TestObservationResult:
    """Validate ObservationResult dataclass contract."""

    def test_construct_with_all_fields(self):
        r = ObservationResult(
            existing_count=3,
            matched_profile_indices=[0, 2],
            unmatched_entries=[{"company": "SpaceX"}],
            page_anchor_values=["google", "nasa", "apple"],
        )
        assert r.existing_count == 3
        assert r.matched_profile_indices == [0, 2]
        assert r.unmatched_entries == [{"company": "SpaceX"}]
        assert r.page_anchor_values == ["google", "nasa", "apple"]

    def test_empty_result(self):
        r = ObservationResult(
            existing_count=0,
            matched_profile_indices=[],
            unmatched_entries=[],
            page_anchor_values=[],
        )
        assert r.existing_count == 0
        assert len(r.matched_profile_indices) == 0
        assert len(r.unmatched_entries) == 0

    def test_all_matched(self):
        entries = [{"company": "A"}, {"company": "B"}]
        r = ObservationResult(
            existing_count=2,
            matched_profile_indices=[0, 1],
            unmatched_entries=[],
            page_anchor_values=["a", "b"],
        )
        assert len(r.matched_profile_indices) == 2
        assert len(r.unmatched_entries) == 0

    def test_none_matched(self):
        entries = [{"company": "A"}, {"company": "B"}]
        r = ObservationResult(
            existing_count=0,
            matched_profile_indices=[],
            unmatched_entries=entries,
            page_anchor_values=[],
        )
        assert len(r.matched_profile_indices) == 0
        assert len(r.unmatched_entries) == 2
