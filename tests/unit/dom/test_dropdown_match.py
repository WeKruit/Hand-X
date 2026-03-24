"""Unit tests for ghosthands.dom.dropdown_match — no Playwright required."""

import pytest

from ghosthands.dom.dropdown_match import (
    are_synonyms,
    match_dropdown_option,
    match_dropdown_option_dict,
    synonym_groups_for_js,
)


# ── match_dropdown_option: pass 1 (exact) ────────────────────────────

class TestExactMatch:
    def test_exact_case_insensitive(self):
        assert match_dropdown_option("Male", ["Male", "Female"]) == "Male"

    def test_exact_with_whitespace(self):
        assert match_dropdown_option("  Male  ", ["Male", "Female"]) == "Male"


# ── pass 2 (prefix) ──────────────────────────────────────────────────

class TestPrefixMatch:
    def test_target_prefix_of_option(self):
        assert match_dropdown_option("Asian", ["Asian (Not Hispanic)", "White"]) == "Asian (Not Hispanic)"

    def test_option_prefix_of_target(self):
        assert match_dropdown_option("Asian (Not Hispanic or Latino)", ["Asian", "White"]) == "Asian"


# ── pass 3 (contains, forward + reverse) ─────────────────────────────

class TestContainsMatch:
    def test_forward_contains(self):
        assert match_dropdown_option("Science", ["Computer Science", "Math"]) == "Computer Science"

    def test_reverse_contains(self):
        assert match_dropdown_option("Computer Science & Engineering", ["Computer Science", "Math"]) == "Computer Science"

    def test_short_option_skipped_if_too_short(self):
        # 2-char option "CS" should NOT reverse-match "Computer Science" (guard)
        assert match_dropdown_option("Computer Science", ["CS", "Math"]) is None


# ── pass 4 (synonyms) ────────────────────────────────────────────────

class TestSynonymMatch:
    def test_male_to_man(self):
        assert match_dropdown_option("Male", ["Man", "Woman", "Non-binary"]) == "Man"

    def test_female_to_woman(self):
        assert match_dropdown_option("Female", ["Man", "Woman", "Non-binary"]) == "Woman"

    def test_decline_variants(self):
        assert match_dropdown_option(
            "I decline to self-identify",
            ["Man", "Woman", "I dont wish to answer"],
        ) == "I dont wish to answer"

    def test_disability_no(self):
        assert match_dropdown_option(
            "No",
            ["I do not have a disability", "I have a disability"],
        ) == "I do not have a disability"

    def test_veteran_synonym(self):
        # "Non-veteran" is a synonym for "I am not a protected veteran" (pass 4),
        # but "Protected veteran" substring-matches first (pass 3 — "veteran" in both).
        # When the confounding option isn't present, synonym kicks in.
        assert match_dropdown_option(
            "I am not a protected veteran",
            ["Non-veteran", "Some other option"],
        ) == "Non-veteran"


# ── pass 5 (word overlap) ────────────────────────────────────────────

class TestWordOverlapMatch:
    def test_partial_overlap(self):
        assert match_dropdown_option(
            "Bachelor of Computer Science",
            ["Computer Science", "Mathematics"],
        ) == "Computer Science"


# ── pass 1.5 (phone code stripping) ──────────────────────────────────

class TestPhoneCodeStripping:
    def test_us_plus_1(self):
        assert match_dropdown_option("United States", ["United States +1", "Canada +1"]) == "United States +1"

    def test_uk_plus_44(self):
        assert match_dropdown_option("United Kingdom", ["United Kingdom +44", "France +33"]) == "United Kingdom +44"

    def test_option_without_code(self):
        assert match_dropdown_option("United States +1", ["United States", "Canada"]) == "United States"

    def test_no_false_match_different_country(self):
        # "India" should not match "Canada +1"
        result = match_dropdown_option("India", ["Canada +1", "Mexico +52"])
        assert result is None


# ── no match ──────────────────────────────────────────────────────────

class TestNoMatch:
    def test_completely_unrelated(self):
        assert match_dropdown_option("Zebra", ["Apple", "Banana"]) is None

    def test_empty_target(self):
        assert match_dropdown_option("", ["Apple"]) is None

    def test_empty_options(self):
        assert match_dropdown_option("Apple", []) is None


# ── match_dropdown_option_dict ────────────────────────────────────────

class TestDictMatch:
    def test_returns_dict(self):
        opts = [{"text": "Man", "value": "male"}, {"text": "Woman", "value": "female"}]
        result = match_dropdown_option_dict("Male", opts)
        assert result is not None
        assert result["text"] == "Man"

    def test_returns_none(self):
        opts = [{"text": "Apple", "value": "a"}]
        assert match_dropdown_option_dict("Zebra", opts) is None


# ── are_synonyms ─────────────────────────────────────────────────────

class TestAreSynonyms:
    def test_male_man(self):
        assert are_synonyms("male", "man") is True

    def test_female_woman(self):
        assert are_synonyms("female", "woman") is True

    def test_unrelated(self):
        assert are_synonyms("hello", "world") is False

    def test_cross_group(self):
        assert are_synonyms("male", "woman") is False


# ── synonym_groups_for_js ─────────────────────────────────────────────

class TestSynonymGroupsForJs:
    def test_returns_list_of_sorted_lists(self):
        groups = synonym_groups_for_js()
        assert isinstance(groups, list)
        for g in groups:
            assert isinstance(g, list)
            assert g == sorted(g)
