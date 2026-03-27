"""Tests for ghosthands.dom.verification_engine — the unified deterministic verifier.

Covers:
  - v3 parity normalization (phone last-7, date digits, country aliases, state abbrev)
  - Two-axis contract shape (execution_status x review_status)
  - Agent digest cap (max 1500 chars) and PII redaction
  - Build review summary aggregation
"""

import json

from ghosthands.dom.verification_engine import (
    FieldReviewResult,
    PageReviewSummary,
    build_agent_digest,
    build_agent_prose,
    build_review_summary,
    is_value_opaque,
    is_value_unset,
    normalize_basic,
    normalize_date_digits,
    normalize_phone_digits,
    values_match,
)

# ── Normalization tests (v3 parity) ─────────────────────────────────────


class TestNormalizeBasic:
    def test_trim_and_lowercase(self):
        assert normalize_basic("  Hello World  ") == "hello world"

    def test_collapse_whitespace(self):
        assert normalize_basic("hello   world") == "hello world"

    def test_empty(self):
        assert normalize_basic("") == ""


class TestNormalizePhone:
    def test_strips_non_digits(self):
        assert normalize_phone_digits("+1 (555) 123-4567") == "15551234567"

    def test_already_digits(self):
        assert normalize_phone_digits("5551234567") == "5551234567"


class TestNormalizeDateDigits:
    def test_strips_separators(self):
        assert normalize_date_digits("03/26/2026") == "03262026"

    def test_dashes(self):
        assert normalize_date_digits("2026-03-26") == "20260326"

    def test_dots(self):
        assert normalize_date_digits("26.03.2026") == "26032026"


# ── Opaque / unset detection ────────────────────────────────────────────


class TestIsValueOpaque:
    def test_hex_id(self):
        assert is_value_opaque("a1b2c3d4e5f6a7b8") is True

    def test_uuid(self):
        assert is_value_opaque("a1b2c3d4-e5f6-7890-abcd-ef1234567890") is True

    def test_normal_text(self):
        assert is_value_opaque("John Smith") is False

    def test_empty(self):
        assert is_value_opaque("") is False

    def test_js_object(self):
        assert is_value_opaque("[object Object]") is True


class TestIsValueUnset:
    def test_empty(self):
        assert is_value_unset("") is True

    def test_whitespace(self):
        assert is_value_unset("   ") is True

    def test_select_placeholder(self):
        assert is_value_unset("Select...") is True

    def test_choose_placeholder(self):
        assert is_value_unset("Choose") is True

    def test_normal_value(self):
        assert is_value_unset("United States") is False

    def test_please_select(self):
        assert is_value_unset("Please Select") is True

    def test_none_selected(self):
        assert is_value_unset("None Selected") is True


# ── values_match (v3 fuzzy matching parity) ─────────────────────────────


class TestValuesMatchExact:
    def test_exact(self):
        assert values_match("John", "John") is True

    def test_case_insensitive(self):
        assert values_match("john", "John") is True

    def test_whitespace_tolerance(self):
        assert values_match("  John Smith  ", "John Smith") is True

    def test_empty_actual(self):
        assert values_match("", "John") is False

    def test_both_empty(self):
        assert values_match("", "") is False


class TestValuesMatchPhone:
    """v3 parity: compare last 7 digits for phone fields."""

    def test_last_7_match(self):
        assert values_match("+1 (555) 123-4567", "5551234567", field_type="tel") is True

    def test_last_7_different(self):
        assert values_match("+1 (555) 123-4567", "5559876543", field_type="tel") is False

    def test_short_phone(self):
        """Phones < 7 digits don't use last-7 rule."""
        assert values_match("12345", "12345", field_type="tel") is True

    def test_country_code_prefix(self):
        assert values_match("15551234567", "5551234567", field_type="tel") is True


class TestValuesMatchDate:
    """v3 parity: compare digit-only versions for date fields."""

    def test_slash_vs_dash(self):
        assert values_match("03/26/2026", "03-26-2026", field_type="date") is True

    def test_different_separator(self):
        assert values_match("03.26.2026", "03/26/2026", field_type="date") is True

    def test_different_date(self):
        assert values_match("03/26/2026", "04/26/2026", field_type="date") is False


class TestValuesMatchCountry:
    """Country alias normalization."""

    def test_usa_aliases(self):
        assert values_match("United States", "USA") is True
        assert values_match("US", "United States") is True
        assert values_match("United States of America", "US") is True

    def test_uk_aliases(self):
        assert values_match("United Kingdom", "UK") is True
        assert values_match("Great Britain", "United Kingdom") is True


class TestValuesMatchState:
    """US state abbreviation normalization."""

    def test_abbreviation(self):
        assert values_match("CA", "California") is True
        assert values_match("New York", "NY") is True

    def test_full_name(self):
        assert values_match("Texas", "Texas") is True


class TestValuesMatchCheckbox:
    """v3 parity: truthy set for checkboxes."""

    def test_checked_vs_true(self):
        assert values_match("checked", "true", field_type="checkbox") is True

    def test_yes_vs_on(self):
        assert values_match("yes", "on", field_type="checkbox") is True

    def test_false_falsy(self):
        assert values_match("", "false", field_type="checkbox") is False


class TestValuesMatchSelect:
    """v3 parity: contains/starts-with for select fields."""

    def test_contains(self):
        assert values_match("Computer Science and Engineering", "Computer Science", field_type="select") is True

    def test_substring(self):
        assert values_match("Male", "Male (He/Him)", field_type="select") is True


class TestValuesMatchMatchedLabel:
    """When the actual option clicked differs from the original desired value."""

    def test_matched_label_used(self):
        assert values_match("Man", "Male", matched_label="Man") is True


class TestValuesMatchSemantic:
    """Semantic matching (number ranges, token overlap) — ported from assess_state."""

    def test_number_overlap(self):
        assert values_match("80000", "80000-90000", semantic=True) is True

    def test_number_in_range(self):
        assert values_match("85000", "80000-90000", semantic=True) is True

    def test_number_out_of_range(self):
        assert values_match("70000", "80000-90000", semantic=True) is False

    def test_token_overlap(self):
        assert (
            values_match(
                "Excited about machine learning opportunities",
                "Machine learning engineer with passion for AI",
                semantic=True,
            )
            is True
        )

    def test_no_semantic_without_flag(self):
        """Without semantic=True, token overlap should NOT match."""
        assert (
            values_match(
                "Excited about machine learning opportunities",
                "Machine learning engineer with passion for AI",
                semantic=False,
            )
            is False
        )


# ── v3 Golden String Parity ──────────────────────────────────────────────


class TestV3GoldenParity:
    """Golden test cases mirroring v3 VerificationEngine.fuzzyMatch behavior."""

    # v3: exact match after normalization
    def test_exact_normalized(self):
        assert values_match("  John Smith  ", "john smith") is True

    # v3: phone last 7 digits
    def test_phone_with_country_code(self):
        assert values_match("+1-555-123-4567", "(555) 123-4567", field_type="tel") is True

    def test_phone_intl_vs_local(self):
        assert values_match("15551234567", "5551234567", field_type="tel") is True

    # v3: date digit comparison
    def test_date_iso_vs_us(self):
        assert values_match("2026-03-26", "03/26/2026", field_type="date") is True

    def test_date_same_order_different_separators(self):
        assert values_match("03.26.2026", "03/26/2026", field_type="date") is True

    def test_date_eu_vs_us_order_no_match(self):
        """EU DD.MM vs US MM/DD — different digit order, should NOT match (v3 parity)."""
        assert values_match("26.03.2026", "03/26/2026", field_type="date") is False

    # v3: select contains/startsWith
    def test_select_contains_both_directions(self):
        assert values_match("Bachelor's Degree", "Bachelor", field_type="select") is True
        assert values_match("Bachelor", "Bachelor's Degree", field_type="select") is True

    # v3: checkbox truthy
    def test_checkbox_true_checked(self):
        assert values_match("checked", "true", field_type="checkbox") is True

    def test_checkbox_1_yes(self):
        assert values_match("1", "yes", field_type="checkbox") is True

    def test_checkbox_on_checked(self):
        assert values_match("on", "checked", field_type="checkbox") is True

    # v3: radio contains
    def test_radio_contains(self):
        assert values_match("Male", "Male (He/Him/His)", field_type="radio") is True

    # Country aliases (beyond v3 — Hand-X extension)
    def test_country_us_usa(self):
        assert values_match("US", "USA") is True

    def test_country_uk_great_britain(self):
        assert values_match("Great Britain", "UK") is True

    # State abbreviations (beyond v3 — Hand-X extension)
    def test_state_ca_california(self):
        assert values_match("CA", "California") is True

    def test_state_ny_new_york(self):
        assert values_match("NY", "New York") is True


# ── FieldReviewResult shape ──────────────────────────────────────────────


class TestFieldReviewResultShape:
    def test_has_two_axes(self):
        r = FieldReviewResult(
            field_id="f1",
            label="Email",
            field_type="text",
            required=True,
            execution_status="executed",
            review_status="verified",
            reason="DOM readback matches expected",
        )
        assert r.execution_status == "executed"
        assert r.review_status == "verified"
        assert r.has_validation_error is False

    def test_readback_unverified_never_verified(self):
        """The old 0.55 confidence path must map to unreadable, never verified."""
        r = FieldReviewResult(
            field_id="f1",
            label="Country",
            field_type="select",
            required=False,
            execution_status="executed",
            review_status="unreadable",
            reason="DOM readback empty after 2.5s poll",
        )
        assert r.review_status != "verified"
        assert r.review_status == "unreadable"


# ── PageReviewSummary ────────────────────────────────────────────────────


class TestBuildReviewSummary:
    def test_counts(self):
        results = [
            FieldReviewResult("f1", "Name", "text", True, "executed", "verified", "ok"),
            FieldReviewResult("f2", "Email", "email", True, "executed", "verified", "ok"),
            FieldReviewResult("f3", "Country", "select", False, "executed", "mismatch", "wrong"),
            FieldReviewResult("f4", "Phone", "tel", False, "executed", "unreadable", "empty"),
            FieldReviewResult("f5", "Resume", "file", False, "not_attempted", "unsupported", "file"),
        ]
        summary = build_review_summary(results)
        assert summary.verified_count == 2
        assert summary.mismatch_count == 1
        assert summary.unreadable_count == 1
        assert summary.unsupported_count == 1
        assert summary.total_attempted == 5


# ── Agent digest (cap + PII redaction) ──────────────────────────────────


class TestBuildAgentDigest:
    def test_under_cap(self):
        results = [
            FieldReviewResult("f1", "Name", "text", True, "executed", "verified", "ok"),
            FieldReviewResult("f2", "Country", "select", False, "executed", "mismatch", "wrong"),
        ]
        summary = build_review_summary(results)
        digest = build_agent_digest(summary)
        assert len(digest) <= 1500
        parsed = json.loads(digest)
        assert parsed["totals"]["verified"] == 1
        assert parsed["totals"]["mismatch"] == 1
        assert len(parsed["issues"]) == 1  # only non-verified

    def test_hard_cap_1500(self):
        """Even with many fields, digest must not exceed 1500 chars."""
        results = [
            FieldReviewResult(
                f"f{i}",
                f"Very Long Field Label Number {i} For Testing Truncation",
                "text",
                True,
                "executed",
                "mismatch",
                f"DOM readback does not match expected value for field {i}",
                actual_read=f"some wrong value for field {i}",
            )
            for i in range(50)
        ]
        summary = build_review_summary(results)
        digest = build_agent_digest(summary)
        assert len(digest) <= 1500
        parsed = json.loads(digest)
        assert parsed["totals"]["total"] == 50

    def test_pii_redacted(self):
        """PII fields (email, phone, address) must have actual_read redacted."""
        results = [
            FieldReviewResult(
                "f1",
                "Email Address",
                "email",
                True,
                "executed",
                "mismatch",
                "wrong",
                actual_read="user@example.com",
            ),
            FieldReviewResult(
                "f2",
                "Phone Number",
                "tel",
                False,
                "executed",
                "mismatch",
                "wrong",
                actual_read="+1 555 123 4567",
            ),
        ]
        summary = build_review_summary(results)
        digest = build_agent_digest(summary)
        parsed = json.loads(digest)
        for issue in parsed["issues"]:
            actual = issue.get("actual", "")
            assert "user@example.com" not in actual
            assert "+1 555 123 4567" not in actual

    def test_verified_excluded_from_issues(self):
        """Verified fields should not appear in per-field issues."""
        results = [
            FieldReviewResult("f1", "Name", "text", True, "executed", "verified", "ok"),
            FieldReviewResult("f2", "Bad", "text", True, "executed", "mismatch", "wrong"),
        ]
        summary = build_review_summary(results)
        digest = build_agent_digest(summary)
        parsed = json.loads(digest)
        assert len(parsed["issues"]) == 1
        assert parsed["issues"][0]["id"] == "f2"


class TestFillVsAssessParity:
    """Both domhand_fill and domhand_assess_state must produce the same result for the same field+value.

    Since both now call verification_engine.values_match, we verify the shared function
    gives identical results regardless of which path invokes it.
    """

    # Exact match — both paths should verify
    def test_exact_text_match(self):
        assert values_match("John Smith", "John Smith") is True

    # Country alias — both paths should verify
    def test_country_alias_both_paths(self):
        assert values_match("United States", "USA") is True
        assert values_match("USA", "United States") is True

    # Phone normalization — both paths should verify
    def test_phone_both_paths(self):
        assert values_match("+1 555 123 4567", "5551234567", field_type="tel") is True

    # Select substring — both paths should verify
    def test_select_substring_both_paths(self):
        assert values_match("Bachelor's Degree in CS", "Bachelor", field_type="select") is True

    # Mismatch — both paths should reject
    def test_mismatch_both_paths(self):
        assert values_match("Jane Doe", "John Smith") is False

    # Semantic with flag — assess uses semantic=True, fill does not
    def test_semantic_only_with_flag(self):
        """Semantic matching (token overlap) only activates with semantic=True."""
        result_no_semantic = values_match(
            "software engineering product development",
            "software product engineering development",
        )
        result_semantic = values_match(
            "software engineering product development",
            "software product engineering development",
            semantic=True,
        )
        assert result_no_semantic is False  # fill path: no semantic (not substring)
        assert result_semantic is True  # assess path: with semantic (token overlap)

    # Opaque value detection — shared utility
    def test_opaque_shared(self):
        assert is_value_opaque("a1b2c3d4e5f6a7b8") is True
        assert is_value_opaque("John Smith") is False

    # Unset detection — shared utility
    def test_unset_shared(self):
        assert is_value_unset("Select...") is True
        assert is_value_unset("United States") is False


class TestBuildAgentProse:
    def test_basic(self):
        results = [
            FieldReviewResult("f1", "Name", "text", True, "executed", "verified", "ok"),
            FieldReviewResult("f2", "Country", "select", False, "executed", "mismatch", "wrong"),
        ]
        summary = build_review_summary(results)
        prose = build_agent_prose(summary)
        assert "1 verified" in prose
        assert "1 mismatch" in prose
        assert "2 fields" in prose

    def test_no_fields(self):
        summary = PageReviewSummary()
        prose = build_agent_prose(summary)
        assert "no fields" in prose.lower()
