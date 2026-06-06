"""Unit tests for Phase 1 email-verification inbox primitives."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ghosthands.email_verification import (
    EmailVerificationAttemptStatus,
    FakeInboxClient,
    MailboxEligibilityStatus,
    MailboxMessage,
    MailboxVerificationQuery,
    VerificationArtifactType,
    build_attempt_result,
    evaluate_mailbox_eligibility,
    extract_artifacts_from_message,
    rank_verification_candidates,
    select_best_candidate,
)

DETECTED_AT = datetime(2026, 6, 6, 18, 0, tzinfo=UTC)
APP_EMAIL = "candidate.qa@gmail.com"


def _query(**overrides) -> MailboxVerificationQuery:
    data = {
        "application_email": APP_EMAIL,
        "connected_email": APP_EMAIL,
        "detected_at": DETECTED_AT,
        "site_hostname": "jobs.example.com",
        "platform": "greenhouse",
        "company_hint": "Example",
        "expected_code_length": 6,
    }
    data.update(overrides)
    return MailboxVerificationQuery(**data)


def _message(**overrides) -> MailboxMessage:
    data = {
        "message_id": "msg-1",
        "received_at": DETECTED_AT + timedelta(seconds=20),
        "sender": "Example Careers <no-reply@jobs.example.com>",
        "subject": "Verify your email",
        "recipients": [APP_EMAIL],
        "body_text": "Your verification code is 482913. It expires in 10 minutes.",
    }
    data.update(overrides)
    return MailboxMessage(**data)


def test_exact_match_mailbox_eligibility_is_case_insensitive() -> None:
    result = evaluate_mailbox_eligibility("Candidate.QA@Gmail.com", "candidate.qa@gmail.com")

    assert result.status is MailboxEligibilityStatus.ELIGIBLE
    assert result.eligible is True


def test_exact_match_mailbox_eligibility_rejects_different_connected_email() -> None:
    result = evaluate_mailbox_eligibility("candidate.qa@gmail.com", "other.qa@gmail.com")

    assert result.status is MailboxEligibilityStatus.EMAIL_MISMATCH
    assert result.eligible is False
    assert "exactly match" in result.reason


def test_extracts_expected_length_verification_code() -> None:
    artifacts = extract_artifacts_from_message(_message(), _query())

    assert [(item.artifact_type, item.value) for item in artifacts] == [
        (VerificationArtifactType.CODE, "482913")
    ]
    assert artifacts[0].confidence >= 0.9


def test_extracts_alphanumeric_verification_code_near_keyword() -> None:
    message = _message(body_text="Your one-time code is AB12CD.")

    artifacts = extract_artifacts_from_message(message, _query())

    assert [(item.artifact_type, item.value) for item in artifacts] == [
        (VerificationArtifactType.CODE, "AB12CD")
    ]


def test_extracts_magic_link_from_html_href() -> None:
    message = _message(
        body_text="Click the button to confirm your email.",
        body_html='<a href="https://jobs.example.com/verify-email?token=abc123">Verify email</a>',
    )
    query = _query(artifact_types=[VerificationArtifactType.MAGIC_LINK])

    artifacts = extract_artifacts_from_message(message, query)

    assert len(artifacts) == 1
    assert artifacts[0].artifact_type is VerificationArtifactType.MAGIC_LINK
    assert artifacts[0].value == "https://jobs.example.com/verify-email?token=abc123"


def test_stale_email_before_allowed_lookback_is_rejected() -> None:
    stale = _message(received_at=DETECTED_AT - timedelta(minutes=10))

    candidates = rank_verification_candidates([stale], _query(lookback_seconds=60))

    assert candidates == []


def test_ranking_prefers_newer_matching_company_message() -> None:
    generic = _message(
        message_id="generic",
        received_at=DETECTED_AT + timedelta(seconds=15),
        sender="Alerts <no-reply@alerts.example.net>",
        subject="Security code",
        body_text="Your security code is 111111.",
    )
    stronger = _message(
        message_id="stronger",
        received_at=DETECTED_AT + timedelta(seconds=25),
        sender="Example Careers <no-reply@jobs.example.com>",
        subject="Example verification code",
        body_text="Use verification code 222222 for Example Careers.",
    )

    best = select_best_candidate([generic, stronger], _query())

    assert best is not None
    assert best.message_id == "stronger"
    assert best.artifact_value == "222222"


def test_tried_message_ids_and_artifact_values_are_excluded() -> None:
    first = _message(message_id="first", body_text="Your verification code is 111111.")
    second = _message(message_id="second", body_text="Your verification code is 222222.")

    candidates = rank_verification_candidates(
        [first, second],
        _query(tried_message_ids=["second"], tried_artifact_values=["111111"]),
    )

    assert candidates == []


def test_attempt_result_reports_email_mismatch_without_polling_candidates() -> None:
    result = build_attempt_result(
        [_message()],
        _query(connected_email="other.qa@gmail.com"),
    )

    assert result.status is EmailVerificationAttemptStatus.EMAIL_MISMATCH
    assert result.candidate is None
    assert result.eligibility is not None
    assert result.eligibility.status is MailboxEligibilityStatus.EMAIL_MISMATCH


@pytest.mark.asyncio
async def test_fake_inbox_client_returns_ranked_candidates() -> None:
    client = FakeInboxClient(
        [
            _message(
                message_id="first",
                received_at=DETECTED_AT + timedelta(seconds=30),
                body_text="Your verification code is 111111.",
            ),
            _message(
                message_id="second",
                received_at=DETECTED_AT + timedelta(seconds=20),
                body_text="Your verification code is 222222.",
            ),
        ]
    )

    candidates = await client.list_verification_candidates(_query())

    assert candidates[0].artifact_value == "111111"
    assert {candidate.artifact_value for candidate in candidates} == {"111111", "222222"}


@pytest.mark.asyncio
async def test_fake_inbox_client_loads_json_fixture(tmp_path) -> None:
    fixture_path = tmp_path / "fake_inbox.json"
    fixture_path.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "message_id": "fixture-message",
                        "received_at": (DETECTED_AT + timedelta(seconds=5)).isoformat(),
                        "sender": "Example Careers <no-reply@jobs.example.com>",
                        "subject": "Confirm your email",
                        "recipients": [APP_EMAIL],
                        "body_text": "Use code 135790 to confirm your email.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    client = FakeInboxClient.from_json_file(fixture_path)
    candidates = await client.list_verification_candidates(_query())

    assert len(candidates) == 1
    assert candidates[0].message_id == "fixture-message"
    assert candidates[0].artifact_value == "135790"
