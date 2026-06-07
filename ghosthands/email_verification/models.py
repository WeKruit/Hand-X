"""Pydantic models for deterministic email-verification recovery."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_EMAIL_IN_TEXT_RE = re.compile(r"(?P<email>[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", re.IGNORECASE)


def normalize_email(value: str | None) -> str:
    """Normalize an email-like value without requiring pydantic[email]."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    match = _EMAIL_IN_TEXT_RE.search(text)
    return match.group("email").lower() if match else text


def normalize_hostname(value: str | None) -> str:
    """Normalize host hints for deterministic scoring."""
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/", 1)[0]
    return text.removeprefix("www.")


def ensure_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class EmailVerificationMode(StrEnum):
    """Configured mailbox access mode for a Hand-X run."""

    DISABLED = "disabled"
    FAKE_INBOX = "fake_inbox"
    LOCAL_GMAIL_OAUTH = "local_gmail_oauth"
    VALET_BROKERED = "valet_brokered"


class VerificationArtifactType(StrEnum):
    """Verification artifact types Hand-X can resolve."""

    CODE = "code"
    MAGIC_LINK = "magic_link"


class EmailVerificationPageKind(StrEnum):
    """Current-page verification state inferred from visible browser facts."""

    EMAIL_CODE = "email_code"
    EMAIL_MAGIC_LINK = "email_magic_link"
    SMS_CODE = "sms_code"
    AUTHENTICATOR_CODE = "authenticator_code"
    CAPTCHA = "captcha"
    AMBIGUOUS = "ambiguous"
    NOT_VERIFICATION = "not_verification"


class CodeEntryMode(StrEnum):
    """How a verification code was entered into the page."""

    SINGLE_INPUT = "single_input"
    SEGMENTED_INPUTS = "segmented_inputs"
    NONE = "none"


class CodeEntryStatus(StrEnum):
    """Structured result status for deterministic code entry."""

    ENTERED = "entered"
    MISSING_CODE = "missing_code"
    NO_INPUT = "no_code_input"
    FAILED = "failed"


class MagicLinkOpenStatus(StrEnum):
    """Structured result status for deterministic magic-link opening."""

    OPENED = "opened"
    MISSING_LINK = "missing_link"
    MISSING_BROWSER_SESSION = "missing_browser_session"
    FAILED = "failed"


class MailboxEligibilityStatus(StrEnum):
    """V1 mailbox eligibility states."""

    ELIGIBLE = "eligible"
    MISSING_CONNECTED_EMAIL = "missing_connected_email"
    MISSING_APPLICATION_EMAIL = "missing_application_email"
    EMAIL_MISMATCH = "gmail_email_mismatch"


class EmailVerificationAttemptStatus(StrEnum):
    """Structured result status for the non-browser Phase 1 resolver core."""

    RESOLVED = "resolved"
    NO_RECENT_EMAIL = "no_recent_verification_email"
    EMAIL_MISMATCH = "gmail_email_mismatch"
    MAILBOX_NOT_CONNECTED = "gmail_not_connected"
    AMBIGUOUS = "ambiguous_verification_email"


class MailboxEligibility(BaseModel):
    """Whether a connected mailbox may resolve the current application email."""

    model_config = ConfigDict(frozen=True)

    status: MailboxEligibilityStatus
    application_email: str = ""
    connected_email: str = ""
    reason: str = ""

    @property
    def eligible(self) -> bool:
        return self.status is MailboxEligibilityStatus.ELIGIBLE


class EmailVerificationPageState(BaseModel):
    """Structured page facts gathered when a verification wall is detected."""

    model_config = ConfigDict(frozen=True)

    current_url: str = ""
    site_hostname: str = ""
    platform: str = "generic"
    company_hint: str | None = None
    application_email: str = ""
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expected_code_length: int | None = Field(default=None, ge=3, le=12)
    supports_code_entry: bool = False
    supports_magic_link: bool = False
    page_kind: EmailVerificationPageKind = EmailVerificationPageKind.NOT_VERIFICATION
    visible_text: str = ""
    heading_text: str = ""
    code_input_count: int = Field(default=0, ge=0)
    code_input_selectors: tuple[str, ...] = ()
    action_button_labels: tuple[str, ...] = ()
    detected_email_addresses: tuple[str, ...] = ()
    resend_available: bool = False
    verify_button_available: bool = False
    continue_button_available: bool = False
    email_signals: bool = False
    magic_link_signals: bool = False
    sms_signals: bool = False
    authenticator_signals: bool = False
    captcha_signals: bool = False

    @field_validator("application_email")
    @classmethod
    def _normalize_application_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("site_hostname")
    @classmethod
    def _normalize_site_hostname(cls, value: str) -> str:
        return normalize_hostname(value)

    @field_validator("detected_at")
    @classmethod
    def _normalize_detected_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class CodeEntryResult(BaseModel):
    """Structured browser result after attempting to enter a verification code."""

    model_config = ConfigDict(frozen=True)

    status: CodeEntryStatus
    mode: CodeEntryMode = CodeEntryMode.NONE
    filled_input_count: int = Field(default=0, ge=0)
    clicked_action: bool = False
    clicked_action_label: str = ""
    page_url: str = ""
    reason: str = ""

    @property
    def success(self) -> bool:
        return self.status is CodeEntryStatus.ENTERED


class MagicLinkOpenResult(BaseModel):
    """Structured browser result after opening a verification magic link."""

    model_config = ConfigDict(frozen=True)

    status: MagicLinkOpenStatus
    original_target_id: str = ""
    magic_link_target_id: str = ""
    original_url: str = ""
    magic_link_url: str = ""
    returned_to_original: bool = False
    reason: str = ""

    @property
    def success(self) -> bool:
        return self.status is MagicLinkOpenStatus.OPENED


class MailboxVerificationQuery(BaseModel):
    """Query sent to an inbox adapter for verification candidates."""

    model_config = ConfigDict(frozen=True)

    application_email: str
    detected_at: datetime
    connected_email: str | None = None
    site_hostname: str = ""
    platform: str = "generic"
    company_hint: str | None = None
    expected_code_length: int | None = Field(default=None, ge=3, le=12)
    artifact_types: tuple[VerificationArtifactType, ...] = (
        VerificationArtifactType.CODE,
        VerificationArtifactType.MAGIC_LINK,
    )
    max_results: int = Field(default=10, ge=1, le=50)
    lookback_seconds: int = Field(
        default=300,
        ge=0,
        le=3600,
        description="Allowed clock skew/lookback before the wall was detected.",
    )
    tried_message_ids: tuple[str, ...] = ()
    tried_artifact_values: tuple[str, ...] = ()

    @field_validator("application_email", "connected_email", mode="before")
    @classmethod
    def _normalize_optional_email(cls, value: Any) -> str | None:
        normalized = normalize_email(value)
        return normalized or None

    @field_validator("site_hostname")
    @classmethod
    def _normalize_query_hostname(cls, value: str) -> str:
        return normalize_hostname(value)

    @field_validator("detected_at")
    @classmethod
    def _normalize_query_detected_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("artifact_types", mode="before")
    @classmethod
    def _normalize_artifact_types(cls, value: Any) -> tuple[VerificationArtifactType, ...]:
        if value in (None, ""):
            return (VerificationArtifactType.CODE, VerificationArtifactType.MAGIC_LINK)
        if isinstance(value, str):
            value = [value]
        normalized = tuple(VerificationArtifactType(item) for item in value)
        return normalized or (VerificationArtifactType.CODE, VerificationArtifactType.MAGIC_LINK)

    @field_validator("tried_message_ids", "tried_artifact_values", mode="before")
    @classmethod
    def _normalize_tuple(cls, value: Any) -> tuple[str, ...]:
        if value in (None, ""):
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(str(item) for item in value if str(item).strip())

    @property
    def earliest_received_at(self) -> datetime:
        return self.detected_at - timedelta(seconds=self.lookback_seconds)

    @property
    def artifact_type_set(self) -> set[VerificationArtifactType]:
        return set(self.artifact_types)


class MailboxMessage(BaseModel):
    """Normalized inbox message used by fake/local/VALET adapters."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    message_id: str = Field(min_length=1)
    received_at: datetime
    sender: str = ""
    subject: str = ""
    recipients: tuple[str, ...] = ()
    body_text: str = ""
    body_html: str = ""

    @field_validator("received_at")
    @classmethod
    def _normalize_received_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("sender")
    @classmethod
    def _normalize_sender(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("recipients", mode="before")
    @classmethod
    def _normalize_recipients(cls, value: Any) -> tuple[str, ...]:
        if value in (None, ""):
            return ()
        raw_parts = re.split(r"[,;]", value) if isinstance(value, str) else [str(item) for item in value]
        return tuple(email for email in (normalize_email(part) for part in raw_parts) if email)


class VerificationArtifact(BaseModel):
    """A code or magic link extracted from one email."""

    model_config = ConfigDict(frozen=True)

    artifact_type: VerificationArtifactType
    value: str = Field(min_length=1)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    source_url: str | None = None

    @field_validator("value")
    @classmethod
    def _normalize_artifact_value(cls, value: str) -> str:
        return str(value or "").strip()

    @property
    def normalized_value(self) -> str:
        return self.value.strip().lower()


class VerificationEmailCandidate(BaseModel):
    """Ranked verification artifact with source-message metadata."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    received_at: datetime
    sender: str = ""
    subject: str = ""
    recipients: tuple[str, ...] = ()
    artifact: VerificationArtifact
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    reasons: tuple[str, ...] = ()

    @field_validator("received_at")
    @classmethod
    def _normalize_candidate_received_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @property
    def artifact_type(self) -> VerificationArtifactType:
        return self.artifact.artifact_type

    @property
    def artifact_value(self) -> str:
        return self.artifact.value


class EmailVerificationAttemptResult(BaseModel):
    """Structured Phase 1 selection result."""

    model_config = ConfigDict(frozen=True)

    status: EmailVerificationAttemptStatus
    candidate: VerificationEmailCandidate | None = None
    eligibility: MailboxEligibility | None = None
    message: str = ""
    retryable: bool = False
