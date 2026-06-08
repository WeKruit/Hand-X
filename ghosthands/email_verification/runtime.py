"""Runtime orchestration for email-verification recovery during Hand-X runs."""

from __future__ import annotations

import asyncio
import logging
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ghosthands.email_verification.browser_helpers import fill_verification_code, open_magic_link_in_new_tab
from ghosthands.email_verification.gmail_inbox import GmailInboxClient
from ghosthands.email_verification.inbox import FakeInboxClient, InboxClient
from ghosthands.email_verification.models import (
    CodeEntryResult,
    EmailVerificationMode,
    EmailVerificationPageState,
    MagicLinkOpenResult,
    MailboxVerificationQuery,
    VerificationArtifactType,
    VerificationEmailCandidate,
    normalize_email,
)
from ghosthands.email_verification.page_state import (
    extract_email_verification_page_state,
    is_auto_resolvable_email_page,
)

logger = logging.getLogger(__name__)

_EMAIL_VERIFICATION_BLOCKER_RE = re.compile(
    r"\b(email verification required|account needs email verification|verify your account|"
    r"verify your email|confirm your email|check your inbox|verification email)\b",
    re.IGNORECASE,
)


class EmailVerificationRecoveryStatus(StrEnum):
    """Runtime recovery outcome for one blocker attempt."""

    DISABLED = "disabled"
    NOT_EMAIL_VERIFICATION_BLOCKER = "not_email_verification_blocker"
    MISSING_BROWSER_PAGE = "missing_browser_page"
    PAGE_NOT_AUTO_RESOLVABLE = "page_not_auto_resolvable"
    MISSING_APPLICATION_EMAIL = "missing_application_email"
    MISSING_INBOX_CLIENT = "missing_inbox_client"
    NO_CANDIDATES = "no_candidates"
    LOW_CONFIDENCE = "low_confidence"
    AMBIGUOUS_CANDIDATES = "ambiguous_candidates"
    RESOLVED_CODE = "resolved_code"
    RESOLVED_MAGIC_LINK = "resolved_magic_link"
    CODE_ENTRY_FAILED = "code_entry_failed"
    MAGIC_LINK_FAILED = "magic_link_failed"
    ERROR = "error"


class EmailVerificationRecoveryConfig(BaseModel):
    """Configuration for one runtime email-verification recovery attempt."""

    model_config = ConfigDict(frozen=True)

    mode: EmailVerificationMode = EmailVerificationMode.DISABLED
    application_email: str = ""
    connected_email: str = ""
    fake_inbox_path: str = ""
    gmail_credentials_file: str = ""
    gmail_token_file: str = ""
    gmail_config_dir: str = ""
    min_candidate_score: float = Field(default=0.75, ge=0.0, le=1.0)
    ambiguity_score_gap: float = Field(default=0.15, ge=0.0, le=1.0)
    poll_attempts: int = Field(default=3, ge=1, le=10)
    poll_interval_seconds: float = Field(default=2.0, ge=0.0, le=30.0)
    lookback_seconds: int = Field(default=300, ge=0, le=3600)
    max_results: int = Field(default=10, ge=1, le=50)

    @field_validator("application_email", "connected_email")
    @classmethod
    def _normalize_email_field(cls, value: str) -> str:
        return normalize_email(value) or ""

    @field_validator(
        "fake_inbox_path",
        "gmail_credentials_file",
        "gmail_token_file",
        "gmail_config_dir",
    )
    @classmethod
    def _strip_path(cls, value: str) -> str:
        return str(value or "").strip()


class EmailVerificationRecoveryResult(BaseModel):
    """Structured result returned to the CLI after one recovery attempt."""

    model_config = ConfigDict(frozen=True)

    status: EmailVerificationRecoveryStatus
    message: str = ""
    page_state: EmailVerificationPageState | None = None
    candidate: VerificationEmailCandidate | None = None
    candidates_seen: int = 0
    top_score: float | None = None
    competing_score: float | None = None
    code_entry_result: CodeEntryResult | None = None
    magic_link_result: MagicLinkOpenResult | None = None

    @property
    def resolved(self) -> bool:
        return self.status in {
            EmailVerificationRecoveryStatus.RESOLVED_CODE,
            EmailVerificationRecoveryStatus.RESOLVED_MAGIC_LINK,
        }

    @property
    def attempted(self) -> bool:
        return self.status not in {
            EmailVerificationRecoveryStatus.DISABLED,
            EmailVerificationRecoveryStatus.NOT_EMAIL_VERIFICATION_BLOCKER,
        }


class _SettingsLike(Protocol):
    email_verification_mode: str
    email_verification_connected_email: str
    email_verification_fake_inbox_path: str
    email_verification_gmail_credentials_file: str
    email_verification_gmail_token_file: str
    email_verification_gmail_config_dir: str
    email_verification_min_candidate_score: float
    email_verification_ambiguity_score_gap: float
    email_verification_poll_attempts: int
    email_verification_poll_interval_seconds: float
    email_verification_lookback_seconds: int


def looks_like_email_verification_blocker(text: str | None) -> bool:
    """Return whether final agent text describes an email-verification blocker."""

    normalized = str(text or "")
    if "blocker:" not in normalized.lower():
        return False
    return bool(_EMAIL_VERIFICATION_BLOCKER_RE.search(normalized))


def recovery_config_from_settings(
    settings: _SettingsLike,
    *,
    application_email: str = "",
) -> EmailVerificationRecoveryConfig:
    """Build runtime recovery config from Hand-X settings and resolved auth email."""

    connected_email = str(getattr(settings, "email_verification_connected_email", "") or "").strip()
    app_email = normalize_email(application_email) or ""
    mode_text = str(getattr(settings, "email_verification_mode", "disabled") or "disabled").strip().lower()
    return EmailVerificationRecoveryConfig(
        mode=EmailVerificationMode(mode_text),
        application_email=app_email,
        connected_email=connected_email or app_email,
        fake_inbox_path=str(getattr(settings, "email_verification_fake_inbox_path", "") or ""),
        gmail_credentials_file=str(getattr(settings, "email_verification_gmail_credentials_file", "") or ""),
        gmail_token_file=str(getattr(settings, "email_verification_gmail_token_file", "") or ""),
        gmail_config_dir=str(getattr(settings, "email_verification_gmail_config_dir", "") or ""),
        min_candidate_score=float(getattr(settings, "email_verification_min_candidate_score", 0.75)),
        ambiguity_score_gap=float(getattr(settings, "email_verification_ambiguity_score_gap", 0.15)),
        poll_attempts=int(getattr(settings, "email_verification_poll_attempts", 3)),
        poll_interval_seconds=float(getattr(settings, "email_verification_poll_interval_seconds", 2.0)),
        lookback_seconds=int(getattr(settings, "email_verification_lookback_seconds", 300)),
    )


def build_inbox_client(config: EmailVerificationRecoveryConfig) -> InboxClient | None:
    """Build the configured inbox client for local/fake recovery modes."""

    if config.mode is EmailVerificationMode.DISABLED:
        return None
    if config.mode is EmailVerificationMode.FAKE_INBOX:
        if not config.fake_inbox_path:
            return None
        return FakeInboxClient.from_json_file(Path(config.fake_inbox_path).expanduser())
    if config.mode is EmailVerificationMode.LOCAL_GMAIL_OAUTH:
        return GmailInboxClient(
            connected_email=config.connected_email or None,
            credentials_file=config.gmail_credentials_file or None,
            token_file=config.gmail_token_file or None,
            config_dir=config.gmail_config_dir or None,
        )
    return None


def select_acceptable_candidate(
    candidates: list[VerificationEmailCandidate],
    config: EmailVerificationRecoveryConfig,
) -> tuple[VerificationEmailCandidate | None, EmailVerificationRecoveryResult | None]:
    """Apply conservative confidence/ambiguity gates before browser entry."""

    if not candidates:
        return None, EmailVerificationRecoveryResult(
            status=EmailVerificationRecoveryStatus.NO_CANDIDATES,
            message="No recent verification email matched the current page.",
        )

    top = candidates[0]
    competing = next(
        (
            candidate
            for candidate in candidates[1:]
            if candidate.artifact.normalized_value != top.artifact.normalized_value
        ),
        None,
    )
    if top.score < config.min_candidate_score:
        return None, EmailVerificationRecoveryResult(
            status=EmailVerificationRecoveryStatus.LOW_CONFIDENCE,
            message="Top verification email candidate was below the configured confidence threshold.",
            candidate=top,
            candidates_seen=len(candidates),
            top_score=top.score,
            competing_score=competing.score if competing else None,
        )

    if competing and (top.score - competing.score) < config.ambiguity_score_gap:
        return None, EmailVerificationRecoveryResult(
            status=EmailVerificationRecoveryStatus.AMBIGUOUS_CANDIDATES,
            message="Multiple competing verification email candidates were too close to choose safely.",
            candidate=top,
            candidates_seen=len(candidates),
            top_score=top.score,
            competing_score=competing.score,
        )

    return top, None


async def recover_email_verification_if_possible(
    browser_session: Any,
    *,
    blocker_text: str | None,
    config: EmailVerificationRecoveryConfig,
    inbox_client: InboxClient | None = None,
    platform: str = "generic",
    company_hint: str | None = None,
) -> EmailVerificationRecoveryResult:
    """Resolve an email-verification wall after the agent reports a blocker."""

    if config.mode is EmailVerificationMode.DISABLED:
        return EmailVerificationRecoveryResult(
            status=EmailVerificationRecoveryStatus.DISABLED,
            message="Email verification recovery is disabled.",
        )
    if not looks_like_email_verification_blocker(blocker_text):
        return EmailVerificationRecoveryResult(
            status=EmailVerificationRecoveryStatus.NOT_EMAIL_VERIFICATION_BLOCKER,
            message="Final result was not an email-verification blocker.",
        )

    try:
        page_getter = getattr(browser_session, "get_current_page", None)
        page = await page_getter() if page_getter else None
        if page is None:
            return EmailVerificationRecoveryResult(
                status=EmailVerificationRecoveryStatus.MISSING_BROWSER_PAGE,
                message="No current browser page was available for verification recovery.",
            )

        state = await extract_email_verification_page_state(page, platform=platform, company_hint=company_hint)
        if config.application_email and not state.application_email:
            state = state.model_copy(update={"application_email": config.application_email})
        if not is_auto_resolvable_email_page(state):
            return EmailVerificationRecoveryResult(
                status=EmailVerificationRecoveryStatus.PAGE_NOT_AUTO_RESOLVABLE,
                message=f"Current page is {state.page_kind}, not an auto-resolvable email verification page.",
                page_state=state,
            )
        if not state.application_email:
            return EmailVerificationRecoveryResult(
                status=EmailVerificationRecoveryStatus.MISSING_APPLICATION_EMAIL,
                message="No application email was available for exact-match inbox recovery.",
                page_state=state,
            )

        client = inbox_client or build_inbox_client(config)
        if client is None:
            return EmailVerificationRecoveryResult(
                status=EmailVerificationRecoveryStatus.MISSING_INBOX_CLIENT,
                message=f"No inbox client is configured for mode {config.mode}.",
                page_state=state,
            )

        query = MailboxVerificationQuery(
            application_email=state.application_email,
            connected_email=config.connected_email or state.application_email,
            detected_at=state.detected_at,
            site_hostname=state.site_hostname,
            platform=state.platform,
            company_hint=state.company_hint,
            expected_code_length=state.expected_code_length,
            max_results=config.max_results,
            lookback_seconds=config.lookback_seconds,
        )

        candidates: list[VerificationEmailCandidate] = []
        for attempt_index in range(config.poll_attempts):
            candidates = await client.list_verification_candidates(query)
            if candidates:
                break
            if attempt_index < config.poll_attempts - 1 and config.poll_interval_seconds > 0:
                await asyncio.sleep(config.poll_interval_seconds)

        candidate, rejection = select_acceptable_candidate(candidates, config)
        if rejection is not None:
            return rejection.model_copy(update={"page_state": state})
        assert candidate is not None

        if candidate.artifact_type is VerificationArtifactType.CODE:
            entry = await fill_verification_code(page, candidate.artifact_value, state=state)
            if not entry.success:
                return EmailVerificationRecoveryResult(
                    status=EmailVerificationRecoveryStatus.CODE_ENTRY_FAILED,
                    message=entry.reason or "Verification code entry failed.",
                    page_state=state,
                    candidate=candidate,
                    candidates_seen=len(candidates),
                    top_score=candidate.score,
                    code_entry_result=entry,
                )
            return EmailVerificationRecoveryResult(
                status=EmailVerificationRecoveryStatus.RESOLVED_CODE,
                message="Verification code entered successfully.",
                page_state=state,
                candidate=candidate,
                candidates_seen=len(candidates),
                top_score=candidate.score,
                code_entry_result=entry,
            )

        if candidate.artifact_type is VerificationArtifactType.MAGIC_LINK:
            link_result = await open_magic_link_in_new_tab(browser_session, candidate.artifact_value)
            if not link_result.success:
                return EmailVerificationRecoveryResult(
                    status=EmailVerificationRecoveryStatus.MAGIC_LINK_FAILED,
                    message=link_result.reason or "Verification magic link failed.",
                    page_state=state,
                    candidate=candidate,
                    candidates_seen=len(candidates),
                    top_score=candidate.score,
                    magic_link_result=link_result,
                )
            return EmailVerificationRecoveryResult(
                status=EmailVerificationRecoveryStatus.RESOLVED_MAGIC_LINK,
                message="Verification magic link opened successfully.",
                page_state=state,
                candidate=candidate,
                candidates_seen=len(candidates),
                top_score=candidate.score,
                magic_link_result=link_result,
            )

        return EmailVerificationRecoveryResult(
            status=EmailVerificationRecoveryStatus.ERROR,
            message=f"Unsupported verification artifact type: {candidate.artifact_type}.",
            page_state=state,
            candidate=candidate,
            candidates_seen=len(candidates),
            top_score=candidate.score,
        )
    except Exception as exc:
        logger.exception("email_verification.runtime_recovery_failed")
        return EmailVerificationRecoveryResult(
            status=EmailVerificationRecoveryStatus.ERROR,
            message=f"Email verification recovery failed: {type(exc).__name__}: {exc}",
        )
