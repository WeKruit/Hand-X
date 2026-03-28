from __future__ import annotations

from unittest.mock import MagicMock


def _make_settings(
    *,
    email: str,
    password: str,
    credential_source: str,
    credential_intent: str,
):
    settings = MagicMock()
    settings.email = email
    settings.password = password
    settings.credential_source = credential_source
    settings.credential_intent = credential_intent
    return settings


def test_local_existing_account_override_stays_separate_from_applicant_profile_email():
    from ghosthands.cli import _resolve_sensitive_data

    applicant_profile = {"email": "applicant@example.com"}
    settings = _make_settings(
        email="existing-account@example.com",
        password="ExistingSecret123!",
        credential_source="user",
        credential_intent="existing_account",
    )

    sensitive = _resolve_sensitive_data(settings, embedded_credentials=None, platform="workday")

    assert applicant_profile["email"] == "applicant@example.com"
    assert sensitive == {
        "email": "existing-account@example.com",
        "password": "ExistingSecret123!",
    }


def test_local_create_account_override_stays_separate_from_applicant_profile_email():
    from ghosthands.cli import _resolve_sensitive_data

    applicant_profile = {"email": "applicant@example.com"}
    settings = _make_settings(
        email="new-account@example.com",
        password="CreateSecret123!",
        credential_source="user",
        credential_intent="create_account",
    )

    sensitive = _resolve_sensitive_data(settings, embedded_credentials=None, platform="workday")

    assert applicant_profile["email"] == "applicant@example.com"
    assert sensitive == {
        "email": "new-account@example.com",
        "password": "CreateSecret123!",
    }
