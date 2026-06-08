"""Email verification recovery primitives.

These modules intentionally expose deterministic, testable building blocks.
Full runtime integration happens in later phases.
"""

from ghosthands.email_verification.browser_helpers import fill_verification_code, open_magic_link_in_new_tab
from ghosthands.email_verification.gmail_inbox import (
    GmailInboxClient,
    build_gmail_search_query,
    gmail_email_to_mailbox_message,
    gmail_time_filter,
)
from ghosthands.email_verification.inbox import FakeInboxClient, InboxClient
from ghosthands.email_verification.models import (
    CodeEntryMode,
    CodeEntryResult,
    CodeEntryStatus,
    EmailVerificationAttemptResult,
    EmailVerificationAttemptStatus,
    EmailVerificationMode,
    EmailVerificationPageKind,
    EmailVerificationPageState,
    MagicLinkOpenResult,
    MagicLinkOpenStatus,
    MailboxEligibility,
    MailboxEligibilityStatus,
    MailboxMessage,
    MailboxVerificationQuery,
    VerificationArtifact,
    VerificationArtifactType,
    VerificationEmailCandidate,
)
from ghosthands.email_verification.page_state import (
    classify_email_verification_page_state,
    extract_email_verification_page_state,
    is_auto_resolvable_email_page,
)
from ghosthands.email_verification.selection import (
    build_attempt_result,
    evaluate_mailbox_eligibility,
    extract_artifacts_from_message,
    rank_verification_candidates,
    select_best_candidate,
)

__all__ = [
    "CodeEntryMode",
    "CodeEntryResult",
    "CodeEntryStatus",
    "EmailVerificationAttemptResult",
    "EmailVerificationAttemptStatus",
    "EmailVerificationMode",
    "EmailVerificationPageKind",
    "EmailVerificationPageState",
    "FakeInboxClient",
    "GmailInboxClient",
    "InboxClient",
    "MagicLinkOpenResult",
    "MagicLinkOpenStatus",
    "MailboxEligibility",
    "MailboxEligibilityStatus",
    "MailboxMessage",
    "MailboxVerificationQuery",
    "VerificationArtifact",
    "VerificationArtifactType",
    "VerificationEmailCandidate",
    "build_attempt_result",
    "build_gmail_search_query",
    "classify_email_verification_page_state",
    "evaluate_mailbox_eligibility",
    "extract_artifacts_from_message",
    "extract_email_verification_page_state",
    "fill_verification_code",
    "gmail_email_to_mailbox_message",
    "gmail_time_filter",
    "is_auto_resolvable_email_page",
    "open_magic_link_in_new_tab",
    "rank_verification_candidates",
    "select_best_candidate",
]
