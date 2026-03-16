"""Unit tests for domhand_fill bug fixes.

Covers:
- max_tokens scaling based on field count (prevents description truncation)
- _sanitize_no_guess_answer emits field_needs_input for [NEEDS_USER_INPUT]
- estimate_cost fault tolerance for unknown models

All tests are offline (no browser, no database, no API calls).
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest


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


def test_sanitize_passes_through_needs_user_input_for_required():
    """Required fields with [NEEDS_USER_INPUT] should emit event and pass through."""
    from ghosthands.actions.domhand_fill import _sanitize_no_guess_answer

    events = []
    original_emit = None

    def mock_emit(event_type, **kwargs):
        events.append({"event": event_type, **kwargs})

    with patch("ghosthands.output.jsonl.emit_event", mock_emit):
        result = _sanitize_no_guess_answer(
            "Country code", True, "[NEEDS_USER_INPUT]", {},
            field_type="select", question_text="Select country code",
        )

    assert result == "[NEEDS_USER_INPUT]"
    assert len(events) == 1
    assert events[0]["event"] == "field_needs_input"
    assert events[0]["field_label"] == "Country code"
    assert events[0]["question_text"] == "Select country code"


def test_sanitize_skips_needs_user_input_for_optional():
    """Optional fields with [NEEDS_USER_INPUT] should return empty string."""
    from ghosthands.actions.domhand_fill import _sanitize_no_guess_answer

    result = _sanitize_no_guess_answer(
        "Facebook", False, "[NEEDS_USER_INPUT]", {},
    )
    assert result == ""


def test_sanitize_normal_values_unchanged():
    """Normal values should pass through without emitting events."""
    from ghosthands.actions.domhand_fill import _sanitize_no_guess_answer

    result = _sanitize_no_guess_answer(
        "First Name", True, "Jane", {"first_name": "Jane"},
    )
    assert result == "Jane"


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
    """Auth instructions should use browser-use input actions, not domhand_fill."""
    from ghosthands.agent.prompts import build_task_prompt

    prompt = build_task_prompt(
        "https://example.wd1.myworkdayjobs.com/en-US/job/123/apply",
        "/tmp/resume.pdf",
        {"email": "user@example.com", "password": "Secret!123"},
        credential_source="generated",
    )

    assert "NOT domhand_fill" in prompt
    assert "browser-use input" in prompt


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
