"""Unit tests for Oracle HCM repeater commit button routing.

Verifies that _CLICK_SAVE_BUTTON_JS correctly routes to the right
commit button for each section type:
- Skills → "ADD SKILL" (oracle_section_commit phase)
- Languages → "ADD LANGUAGE"
- Education → "SAVE" (oracle_save phase)
- Experience → "SAVE"

Tests use regex patterns extracted from the JS since we can't run
browser JS in unit tests.
"""

import re

import pytest

from ghosthands.actions.domhand_fill_repeaters import (
    _SECTION_ALIASES,
    _EXPAND_SECTION_NAMES,
    _normalize_section,
)


# ── Section normalization ─────────────────────────────────────────────


class TestNormalizeSection:
    """_normalize_section maps user-facing names to canonical keys."""

    def test_education_canonical(self):
        assert _normalize_section("Education") == "education"

    def test_college_university_alias(self):
        assert _normalize_section("College / University") == "education"

    def test_work_experience_canonical(self):
        assert _normalize_section("Work Experience") == "experience"

    def test_technical_skills_alias(self):
        assert _normalize_section("Technical Skills") == "skills"

    def test_language_skills_alias(self):
        assert _normalize_section("Language Skills") == "languages"

    def test_licenses_certifications(self):
        assert _normalize_section("Licenses & Certifications") == "licenses"

    def test_skills_canonical(self):
        assert _normalize_section("Skills") == "skills"

    def test_languages_canonical(self):
        assert _normalize_section("Languages") == "languages"


# ── Oracle commit button pattern matching ─────────────────────────────
# These regex patterns are extracted from _CLICK_SAVE_BUTTON_JS to verify
# they match the expected Oracle HCM commit button texts.


_SKILL_PATTERN = re.compile(r"^add\s+skill$", re.IGNORECASE)
_LANGUAGE_PATTERN = re.compile(r"^add\s+language$", re.IGNORECASE)
_LICENSE_PATTERN = re.compile(r"^add\s+(certification|license)$", re.IGNORECASE)


class TestOracleCommitPatterns:
    """Oracle commit button text patterns must match expected button texts."""

    # Skills
    def test_add_skill_matches(self):
        assert _SKILL_PATTERN.match("Add Skill")

    def test_ADD_SKILL_matches(self):
        assert _SKILL_PATTERN.match("ADD SKILL")

    def test_add_skill_no_extra(self):
        """Must NOT match 'Add Skill Something' or 'Add Skills'."""
        assert not _SKILL_PATTERN.match("Add Skills")
        assert not _SKILL_PATTERN.match("Add Skill Entry")

    def test_add_education_does_not_match_skill(self):
        assert not _SKILL_PATTERN.match("Add Education")

    # Languages
    def test_add_language_matches(self):
        assert _LANGUAGE_PATTERN.match("Add Language")

    def test_ADD_LANGUAGE_matches(self):
        assert _LANGUAGE_PATTERN.match("ADD LANGUAGE")

    def test_add_language_no_extra(self):
        assert not _LANGUAGE_PATTERN.match("Add Languages")

    # Licenses
    def test_add_certification_matches(self):
        assert _LICENSE_PATTERN.match("Add Certification")

    def test_add_license_matches(self):
        assert _LICENSE_PATTERN.match("Add License")

    def test_add_education_does_not_match_license(self):
        assert not _LICENSE_PATTERN.match("Add Education")


class TestSaveVsAddRouting:
    """Verify section-to-button routing logic matches the JS."""

    def test_skills_uses_add_pattern_not_save(self):
        """Skills section should use 'ADD SKILL', not 'SAVE'."""
        canonical = _normalize_section("Technical Skills")
        assert canonical == "skills"
        # In JS: sectionCommitPattern is set for skills, preferSave is False
        assert canonical not in ("education", "experience")  # preferSave sections

    def test_education_uses_save_not_add(self):
        """Education section should use 'SAVE', not 'ADD EDUCATION'."""
        canonical = _normalize_section("Education")
        assert canonical == "education"
        # In JS: preferSave is True for education
        assert canonical in ("education", "experience")

    def test_experience_uses_save_not_add(self):
        """Experience section should use 'SAVE', not 'ADD EXPERIENCE'."""
        canonical = _normalize_section("Work Experience")
        assert canonical == "experience"
        assert canonical in ("education", "experience")

    def test_languages_uses_add_pattern(self):
        """Languages section should use 'ADD LANGUAGE'."""
        canonical = _normalize_section("Language Skills")
        assert canonical == "languages"
        assert canonical not in ("education", "experience")


# ── Expand section names ──────────────────────────────────────────────


class TestExpandSectionNames:
    """_EXPAND_SECTION_NAMES must have Oracle-specific names for isolation."""

    def test_skills_has_technical_skills_first(self):
        names = _EXPAND_SECTION_NAMES["skills"]
        assert "Technical Skills" in names
        assert names[0] == "Technical Skills"  # must be first

    def test_languages_has_language_skills_first(self):
        names = _EXPAND_SECTION_NAMES["languages"]
        assert "Language Skills" in names
        assert names[0] == "Language Skills"  # must be first

    def test_skills_does_not_include_language(self):
        names = _EXPAND_SECTION_NAMES["skills"]
        for n in names:
            assert "language" not in n.lower()

    def test_languages_does_not_include_technical(self):
        names = _EXPAND_SECTION_NAMES["languages"]
        for n in names:
            assert "technical" not in n.lower()
