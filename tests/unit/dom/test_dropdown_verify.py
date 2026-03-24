"""Unit tests for ghosthands.dom.dropdown_verify — no Playwright required."""

import pytest

from ghosthands.dom.dropdown_verify import selection_matches_desired


class TestDirectMatch:
    def test_exact_same(self):
        assert selection_matches_desired("Male", "Male") is True

    def test_case_insensitive(self):
        assert selection_matches_desired("male", "Male") is True

    def test_matched_label_exact(self):
        assert selection_matches_desired("Man", "Male", matched_label="Man") is True


class TestBinaryCollapse:
    def test_yes_checked(self):
        assert selection_matches_desired("checked", "Yes") is True

    def test_no_false(self):
        assert selection_matches_desired("false", "No") is True

    def test_agree_yes(self):
        assert selection_matches_desired("I agree", "Yes") is True


class TestDateMatch:
    def test_same_date_different_separator(self):
        assert selection_matches_desired("03-15-2025", "03/15/2025") is True

    def test_different_dates(self):
        assert selection_matches_desired("03/15/2025", "04/15/2025") is False


class TestSubstringContainment:
    def test_desired_in_current(self):
        assert selection_matches_desired("East Asian (Not Hispanic)", "East Asian") is True

    def test_current_in_desired(self):
        assert selection_matches_desired("Asian", "Asian (Not Hispanic or Latino)") is True

    def test_matched_label_containment(self):
        assert selection_matches_desired("Man (Male)", "Male", matched_label="Man") is True


class TestSynonymEquivalence:
    def test_man_matches_male(self):
        assert selection_matches_desired("Man", "Male") is True

    def test_woman_matches_female(self):
        assert selection_matches_desired("Woman", "Female") is True

    def test_decline_variants(self):
        assert selection_matches_desired(
            "I dont wish to answer", "I decline to self-identify"
        ) is True


class TestHierarchicalSegment:
    def test_last_segment_match(self):
        assert selection_matches_desired("Engineering", "STEM > Engineering") is True


class TestCountryPhoneCode:
    def test_us_plus_one(self):
        assert selection_matches_desired("United States +1", "United States") is True

    def test_uk_plus_44(self):
        assert selection_matches_desired("United Kingdom +44", "United Kingdom") is True

    def test_india_plus_91(self):
        assert selection_matches_desired("India +91", "India") is True

    def test_reverse_no_false_match(self):
        """Having a code suffix in desired but not in current should still work via containment."""
        assert selection_matches_desired("United States", "United States +1") is True

    def test_matched_label_with_code(self):
        assert selection_matches_desired(
            "United States +1", "US", matched_label="United States"
        ) is True

    def test_different_countries_no_match(self):
        assert selection_matches_desired("Canada +1", "United States") is False


class TestNoMatch:
    def test_unrelated(self):
        assert selection_matches_desired("Apple", "Orange") is False

    def test_empty_current(self):
        assert selection_matches_desired("", "Male") is False

    def test_empty_desired(self):
        assert selection_matches_desired("Male", "") is False

    def test_placeholder_current(self):
        assert selection_matches_desired("Select...", "Male") is False

    def test_select_prefix_placeholder(self):
        assert selection_matches_desired("Select one", "Male") is False
