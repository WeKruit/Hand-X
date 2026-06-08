"""Unit tests for Phase 5 email-verification runtime orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from ghosthands.email_verification import (
    EmailVerificationMode,
    EmailVerificationRecoveryConfig,
    EmailVerificationRecoveryStatus,
    MailboxMessage,
    MailboxVerificationQuery,
    VerificationArtifact,
    VerificationArtifactType,
    VerificationEmailCandidate,
    looks_like_email_verification_blocker,
    rank_verification_candidates,
    recover_email_verification_if_possible,
    recovery_config_from_settings,
    select_acceptable_candidate,
)

APP_EMAIL = "candidate.qa@gmail.com"
DETECTED_AT = datetime(2026, 6, 8, 8, 0, tzinfo=UTC)


@dataclass
class SettingsStub:
    email_verification_mode: str = "disabled"
    email_verification_connected_email: str = ""
    email_verification_fake_inbox_path: str = ""
    email_verification_gmail_credentials_file: str = ""
    email_verification_gmail_token_file: str = ""
    email_verification_gmail_config_dir: str = ""
    email_verification_min_candidate_score: float = 0.75
    email_verification_ambiguity_score_gap: float = 0.15
    email_verification_poll_attempts: int = 3
    email_verification_poll_interval_seconds: float = 2.0
    email_verification_lookback_seconds: int = 300


class DynamicInboxClient:
    """Inbox client that returns messages relative to the runtime query time."""

    def __init__(self, messages: list[MailboxMessage] | None = None) -> None:
        self._messages = messages
        self.queries: list[MailboxVerificationQuery] = []

    async def list_verification_candidates(
        self,
        query: MailboxVerificationQuery,
    ) -> list[VerificationEmailCandidate]:
        self.queries.append(query)
        messages = self._messages
        if messages is None:
            messages = [
                MailboxMessage(
                    message_id="runtime-message",
                    received_at=query.detected_at + timedelta(seconds=1),
                    sender="Acme Careers <no-reply@jobs.example.com>",
                    subject="Acme Careers verification code",
                    recipients=(APP_EMAIL,),
                    body_text="Use verification code 482913 to continue your Acme Careers application.",
                )
            ]
        return rank_verification_candidates(messages, query)


class FakeVerificationPage:
    def __init__(self, *, application_email: str = APP_EMAIL, kind: str = "email_code") -> None:
        self.application_email = application_email
        self.kind = kind
        self.filled_payload: dict[str, Any] | None = None

    async def evaluate(self, script: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if payload is not None:
            self.filled_payload = payload
            return {
                "status": "entered",
                "mode": "single_input",
                "filled_input_count": 1,
                "clicked_action": True,
                "clicked_action_label": "Verify",
                "page_url": "https://jobs.example.com/verify",
                "reason": "Verification code entered and a safe action button was clicked.",
            }
        if self.kind == "sms_code":
            return {
                "current_url": "https://jobs.example.com/verify",
                "site_hostname": "jobs.example.com",
                "application_email": "",
                "visible_text": "Enter the code we sent by text message to your phone.",
                "heading_text": "Phone verification",
                "code_input_count": 1,
                "code_input_selectors": ["#code"],
                "supports_code_entry": True,
                "email_signals": False,
                "sms_signals": True,
            }
        return {
            "current_url": "https://jobs.example.com/verify",
            "site_hostname": "jobs.example.com",
            "application_email": self.application_email,
            "visible_text": f"We sent a verification code to {self.application_email}. Enter the code to continue.",
            "heading_text": "Verify your email",
            "code_input_count": 1,
            "code_input_selectors": ["#code"],
            "action_button_labels": ["Verify"],
            "supports_code_entry": True,
            "email_signals": True,
        }


class FakeBrowserSession:
    def __init__(self, page: FakeVerificationPage | None) -> None:
        self.page = page

    async def get_current_page(self) -> FakeVerificationPage | None:
        return self.page


def _config(**overrides: Any) -> EmailVerificationRecoveryConfig:
    data = {
        "mode": EmailVerificationMode.FAKE_INBOX,
        "application_email": APP_EMAIL,
        "connected_email": APP_EMAIL,
        "min_candidate_score": 0.75,
        "ambiguity_score_gap": 0.15,
        "poll_attempts": 1,
        "poll_interval_seconds": 0,
    }
    data.update(overrides)
    return EmailVerificationRecoveryConfig(**data)


def _candidate(value: str, score: float) -> VerificationEmailCandidate:
    return VerificationEmailCandidate(
        message_id=f"msg-{value}",
        received_at=DETECTED_AT,
        sender="Acme Careers <no-reply@jobs.example.com>",
        subject="Acme verification code",
        recipients=(APP_EMAIL,),
        artifact=VerificationArtifact(
            artifact_type=VerificationArtifactType.CODE,
            value=value,
            confidence=score,
            reason="test candidate",
        ),
        score=score,
        reasons=("test candidate",),
    )


def test_blocker_detection_requires_blocker_text() -> None:
    assert looks_like_email_verification_blocker(
        "blocker: email verification required -- user must verify email"
    )
    assert not looks_like_email_verification_blocker("email verification required but agent is still working")
    assert not looks_like_email_verification_blocker("blocker: CAPTCHA detected")


def test_recovery_config_from_settings_normalizes_mode_and_email() -> None:
    settings = SettingsStub(
        email_verification_mode=" LOCAL_GMAIL_OAUTH ",
        email_verification_min_candidate_score=0.8,
        email_verification_ambiguity_score_gap=0.2,
        email_verification_poll_attempts=2,
        email_verification_poll_interval_seconds=0.5,
        email_verification_lookback_seconds=120,
    )

    config = recovery_config_from_settings(settings, application_email="Candidate.QA@Gmail.com")

    assert config.mode is EmailVerificationMode.LOCAL_GMAIL_OAUTH
    assert config.application_email == APP_EMAIL
    assert config.connected_email == APP_EMAIL
    assert config.min_candidate_score == 0.8


def test_cli_recovery_summary_does_not_include_code_value() -> None:
    from ghosthands.cli import _email_verification_recovery_summary

    result = SimpleNamespace(
        status=EmailVerificationRecoveryStatus.RESOLVED_CODE,
        resolved=True,
        attempted=True,
        message="Verification code entered successfully.",
        page_state=SimpleNamespace(page_kind="email_code"),
        candidate=_candidate("482913", 1.0),
        candidates_seen=1,
        top_score=1.0,
        competing_score=None,
        code_entry_result=SimpleNamespace(status="entered"),
        magic_link_result=None,
    )

    summary = _email_verification_recovery_summary(result)

    assert summary["status"] == "resolved_code"
    assert summary["artifactType"] == "code"
    assert "482913" not in str(summary)


def test_candidate_gate_rejects_low_confidence() -> None:
    candidate, rejection = select_acceptable_candidate([_candidate("482913", 0.7)], _config())

    assert candidate is None
    assert rejection is not None
    assert rejection.status is EmailVerificationRecoveryStatus.LOW_CONFIDENCE


def test_candidate_gate_rejects_ambiguous_competing_codes() -> None:
    candidate, rejection = select_acceptable_candidate(
        [_candidate("482913", 0.91), _candidate("111111", 0.82)],
        _config(ambiguity_score_gap=0.15),
    )

    assert candidate is None
    assert rejection is not None
    assert rejection.status is EmailVerificationRecoveryStatus.AMBIGUOUS_CANDIDATES
    assert rejection.competing_score == 0.82


@pytest.mark.asyncio
async def test_runtime_recovery_enters_high_confidence_code() -> None:
    page = FakeVerificationPage()
    result = await recover_email_verification_if_possible(
        FakeBrowserSession(page),
        blocker_text="blocker: email verification required -- user must verify email then retry",
        config=_config(),
        inbox_client=DynamicInboxClient(),
        platform="greenhouse",
        company_hint="Acme",
    )

    assert result.status is EmailVerificationRecoveryStatus.RESOLVED_CODE
    assert result.resolved is True
    assert result.candidate is not None
    assert result.candidate.artifact_value == "482913"
    assert page.filled_payload is not None
    assert page.filled_payload["code"] == "482913"


@pytest.mark.asyncio
async def test_runtime_recovery_refuses_sms_code_page() -> None:
    result = await recover_email_verification_if_possible(
        FakeBrowserSession(FakeVerificationPage(kind="sms_code")),
        blocker_text="blocker: email verification required -- user must verify email then retry",
        config=_config(),
        inbox_client=DynamicInboxClient(),
    )

    assert result.status is EmailVerificationRecoveryStatus.PAGE_NOT_AUTO_RESOLVABLE
    assert result.resolved is False
    assert result.page_state is not None
    assert result.page_state.page_kind.value == "sms_code"


@pytest.mark.asyncio
async def test_runtime_recovery_preserves_disabled_mode() -> None:
    result = await recover_email_verification_if_possible(
        FakeBrowserSession(FakeVerificationPage()),
        blocker_text="blocker: email verification required -- user must verify email then retry",
        config=_config(mode=EmailVerificationMode.DISABLED),
        inbox_client=DynamicInboxClient(),
    )

    assert result.status is EmailVerificationRecoveryStatus.DISABLED
    assert result.resolved is False
