"""Email verification recovery primitives.

Phase 1 intentionally exposes only deterministic, testable building blocks.
Runtime/browser integration happens in later phases.
"""

from ghosthands.email_verification.inbox import FakeInboxClient, InboxClient
from ghosthands.email_verification.models import (
    EmailVerificationAttemptResult,
    EmailVerificationAttemptStatus,
    EmailVerificationMode,
    EmailVerificationPageState,
    MailboxEligibility,
    MailboxEligibilityStatus,
    MailboxMessage,
    MailboxVerificationQuery,
    VerificationArtifact,
    VerificationArtifactType,
    VerificationEmailCandidate,
)
from ghosthands.email_verification.selection import (
    build_attempt_result,
    evaluate_mailbox_eligibility,
    extract_artifacts_from_message,
    rank_verification_candidates,
    select_best_candidate,
)

__all__ = [
    "EmailVerificationAttemptResult",
    "EmailVerificationAttemptStatus",
    "EmailVerificationMode",
    "EmailVerificationPageState",
    "FakeInboxClient",
    "InboxClient",
    "MailboxEligibility",
    "MailboxEligibilityStatus",
    "MailboxMessage",
    "MailboxVerificationQuery",
    "VerificationArtifact",
    "VerificationArtifactType",
    "VerificationEmailCandidate",
    "build_attempt_result",
    "evaluate_mailbox_eligibility",
    "extract_artifacts_from_message",
    "rank_verification_candidates",
    "select_best_candidate",
]
