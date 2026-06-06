"""Deterministic artifact extraction and candidate ranking."""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

from ghosthands.email_verification.models import (
    EmailVerificationAttemptResult,
    EmailVerificationAttemptStatus,
    MailboxEligibility,
    MailboxEligibilityStatus,
    MailboxMessage,
    MailboxVerificationQuery,
    VerificationArtifact,
    VerificationArtifactType,
    VerificationEmailCandidate,
    normalize_email,
    normalize_hostname,
)

_CODE_PATTERN = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{4,10})(?![A-Z0-9])", re.IGNORECASE)
_HREF_PATTERN = re.compile(r"""href=["'](?P<url>https?://[^"']+)["']""", re.IGNORECASE)
_URL_PATTERN = re.compile(r"https?://[^\s<>'\"()]+", re.IGNORECASE)

_CODE_KEYWORDS = (
    "verification code",
    "security code",
    "one-time code",
    "one time code",
    "one-time passcode",
    "passcode",
    "otp",
    "code",
)
_LINK_KEYWORDS = (
    "verify",
    "verification",
    "confirm",
    "activate",
    "magic",
    "signin",
    "sign-in",
)
_REJECT_LINK_MARKERS = (
    "unsubscribe",
    "privacy",
    "terms",
    "preferences",
    "email-preferences",
    "support",
)
_REJECT_CODE_WORDS = {
    "code",
    "email",
    "verify",
    "account",
    "security",
    "workday",
    "greenhouse",
    "oracle",
    "confirm",
}


def evaluate_mailbox_eligibility(
    application_email: str | None,
    connected_email: str | None,
) -> MailboxEligibility:
    """Apply the v1 exact-match mailbox eligibility rule."""
    normalized_application = normalize_email(application_email)
    normalized_connected = normalize_email(connected_email)
    if not normalized_application:
        return MailboxEligibility(
            status=MailboxEligibilityStatus.MISSING_APPLICATION_EMAIL,
            application_email="",
            connected_email=normalized_connected,
            reason="No application email is available for inbox verification.",
        )
    if not normalized_connected:
        return MailboxEligibility(
            status=MailboxEligibilityStatus.MISSING_CONNECTED_EMAIL,
            application_email=normalized_application,
            connected_email="",
            reason="No connected Gmail address is available.",
        )
    if normalized_application != normalized_connected:
        return MailboxEligibility(
            status=MailboxEligibilityStatus.EMAIL_MISMATCH,
            application_email=normalized_application,
            connected_email=normalized_connected,
            reason="Application email must exactly match the connected Gmail address in v1.",
        )
    return MailboxEligibility(
        status=MailboxEligibilityStatus.ELIGIBLE,
        application_email=normalized_application,
        connected_email=normalized_connected,
        reason="Application email matches connected Gmail address.",
    )


def extract_artifacts_from_message(
    message: MailboxMessage,
    query: MailboxVerificationQuery,
) -> list[VerificationArtifact]:
    """Extract code/link artifacts from one normalized email message."""
    artifacts: list[VerificationArtifact] = []
    if VerificationArtifactType.CODE in query.artifact_type_set:
        artifacts.extend(_extract_code_artifacts(message, query))
    if VerificationArtifactType.MAGIC_LINK in query.artifact_type_set:
        artifacts.extend(_extract_magic_link_artifacts(message, query))
    return artifacts


def rank_verification_candidates(
    messages: list[MailboxMessage],
    query: MailboxVerificationQuery,
) -> list[VerificationEmailCandidate]:
    """Return ranked verification candidates, filtering stale/tried artifacts."""
    eligibility = evaluate_mailbox_eligibility(query.application_email, query.connected_email)
    if not eligibility.eligible:
        return []

    tried_message_ids = set(query.tried_message_ids)
    tried_artifact_values = {value.strip().lower() for value in query.tried_artifact_values}
    candidates: list[VerificationEmailCandidate] = []

    for message in messages:
        if message.message_id in tried_message_ids:
            continue
        if message.received_at < query.earliest_received_at:
            continue
        for artifact in extract_artifacts_from_message(message, query):
            if artifact.normalized_value in tried_artifact_values:
                continue
            candidate = VerificationEmailCandidate(
                message_id=message.message_id,
                received_at=message.received_at,
                sender=message.sender,
                subject=message.subject,
                recipients=message.recipients,
                artifact=artifact,
            )
            candidates.append(_score_candidate(candidate, message, query))

    return sorted(
        candidates,
        key=lambda item: (item.score, item.received_at, item.message_id),
        reverse=True,
    )[: query.max_results]


def select_best_candidate(
    messages: list[MailboxMessage],
    query: MailboxVerificationQuery,
) -> VerificationEmailCandidate | None:
    """Return the top-ranked candidate, if any."""
    candidates = rank_verification_candidates(messages, query)
    return candidates[0] if candidates else None


def build_attempt_result(
    messages: list[MailboxMessage],
    query: MailboxVerificationQuery,
) -> EmailVerificationAttemptResult:
    """Build a structured Phase 1 selection result."""
    eligibility = evaluate_mailbox_eligibility(query.application_email, query.connected_email)
    if not eligibility.eligible:
        status = (
            EmailVerificationAttemptStatus.EMAIL_MISMATCH
            if eligibility.status is MailboxEligibilityStatus.EMAIL_MISMATCH
            else EmailVerificationAttemptStatus.MAILBOX_NOT_CONNECTED
        )
        return EmailVerificationAttemptResult(
            status=status,
            eligibility=eligibility,
            message=eligibility.reason,
            retryable=False,
        )

    candidate = select_best_candidate(messages, query)
    if candidate:
        return EmailVerificationAttemptResult(
            status=EmailVerificationAttemptStatus.RESOLVED,
            candidate=candidate,
            eligibility=eligibility,
            message="Verification artifact selected.",
            retryable=False,
        )
    return EmailVerificationAttemptResult(
        status=EmailVerificationAttemptStatus.NO_RECENT_EMAIL,
        eligibility=eligibility,
        message="No recent verification email matched the current page.",
        retryable=True,
    )


def _extract_code_artifacts(
    message: MailboxMessage,
    query: MailboxVerificationQuery,
) -> list[VerificationArtifact]:
    haystack = _combined_text(message)
    found: dict[str, VerificationArtifact] = {}
    for match in _CODE_PATTERN.finditer(haystack):
        raw_code = match.group(1).strip()
        code = raw_code.upper()
        if not _looks_like_code(code, match, haystack, query):
            continue
        context = haystack[max(0, match.start() - 90) : match.end() + 90].lower()
        confidence = 0.55
        reasons = ["code-shaped token"]
        if any(keyword in context for keyword in _CODE_KEYWORDS):
            confidence += 0.2
            reasons.append("near code keyword")
        if query.expected_code_length:
            if len(code) == query.expected_code_length:
                confidence += 0.2
                reasons.append("length matches page hint")
            else:
                confidence -= 0.15
                reasons.append("length differs from page hint")
        if _verification_text(f"{message.subject} {message.sender}"):
            confidence += 0.05
            reasons.append("verification-like subject/sender")
        artifact = VerificationArtifact(
            artifact_type=VerificationArtifactType.CODE,
            value=code,
            confidence=_clamp(confidence),
            reason=", ".join(reasons),
        )
        previous = found.get(code)
        if previous is None or artifact.confidence > previous.confidence:
            found[code] = artifact
    return list(found.values())


def _extract_magic_link_artifacts(
    message: MailboxMessage,
    query: MailboxVerificationQuery,
) -> list[VerificationArtifact]:
    source = _combined_text(message)
    urls = _extract_urls(message)
    found: dict[str, VerificationArtifact] = {}
    for url in urls:
        normalized_url = _clean_url(url)
        if not _looks_like_magic_link(normalized_url, source):
            continue
        parsed = urlparse(normalized_url)
        url_text = f"{parsed.netloc} {parsed.path} {parsed.query}".lower()
        confidence = 0.55
        reasons = ["http link"]
        if any(keyword in url_text for keyword in _LINK_KEYWORDS):
            confidence += 0.25
            reasons.append("verification-like URL")
        if _verification_text(f"{message.subject} {source}"):
            confidence += 0.1
            reasons.append("verification-like email text")
        host = normalize_hostname(query.site_hostname)
        if host and host in normalize_hostname(parsed.netloc):
            confidence += 0.08
            reasons.append("host matches application")
        artifact = VerificationArtifact(
            artifact_type=VerificationArtifactType.MAGIC_LINK,
            value=normalized_url,
            confidence=_clamp(confidence),
            reason=", ".join(reasons),
            source_url=normalized_url,
        )
        previous = found.get(normalized_url)
        if previous is None or artifact.confidence > previous.confidence:
            found[normalized_url] = artifact
    return list(found.values())


def _score_candidate(
    candidate: VerificationEmailCandidate,
    message: MailboxMessage,
    query: MailboxVerificationQuery,
) -> VerificationEmailCandidate:
    score = candidate.artifact.confidence
    reasons = [candidate.artifact.reason]

    if message.received_at >= query.detected_at:
        score += 0.1
        reasons.append("received after wall detection")
    else:
        score -= 0.05
        reasons.append("received within allowed lookback")

    if _message_recipients_match(message, query):
        score += 0.12
        reasons.append("recipient matches application email")
    elif message.recipients:
        score -= 0.05
        reasons.append("recipient does not match application email")

    if _message_matches_site_hint(message, query):
        score += 0.08
        reasons.append("sender/subject/body matches site or company hint")

    if _verification_text(f"{message.subject} {message.sender}"):
        score += 0.05
        reasons.append("sender/subject has verification signal")

    return candidate.model_copy(
        update={
            "score": round(_clamp(score), 4),
            "reasons": tuple(reason for reason in reasons if reason),
        }
    )


def _combined_text(message: MailboxMessage) -> str:
    html_text = re.sub(r"<[^>]+>", " ", html.unescape(message.body_html or ""))
    return " ".join(
        part
        for part in (
            message.subject,
            message.sender,
            " ".join(message.recipients),
            message.body_text,
            html_text,
        )
        if part
    )


def _extract_urls(message: MailboxMessage) -> list[str]:
    source = f"{message.body_text}\n{message.body_html}"
    urls = [match.group("url") for match in _HREF_PATTERN.finditer(source)]
    urls.extend(match.group(0) for match in _URL_PATTERN.finditer(source))
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        cleaned = _clean_url(url)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            deduped.append(cleaned)
    return deduped


def _clean_url(url: str) -> str:
    return html.unescape(str(url or "").strip().rstrip(".,;:!?)\"]}'"))


def _looks_like_code(
    code: str,
    match: re.Match[str],
    haystack: str,
    query: MailboxVerificationQuery,
) -> bool:
    lower = code.lower()
    if lower in _REJECT_CODE_WORDS:
        return False
    if code.isalpha():
        return False
    if query.expected_code_length and len(code) != query.expected_code_length:
        context = haystack[max(0, match.start() - 90) : match.end() + 90].lower()
        if not any(keyword in context for keyword in _CODE_KEYWORDS):
            return False
    if code.isdigit() and len(code) == 4 and 1900 <= int(code) <= 2099:
        context = haystack[max(0, match.start() - 60) : match.end() + 60].lower()
        return any(keyword in context for keyword in _CODE_KEYWORDS)
    return True


def _looks_like_magic_link(url: str, source_text: str) -> bool:
    lower_url = url.lower()
    if not lower_url.startswith(("http://", "https://")):
        return False
    if any(marker in lower_url for marker in _REJECT_LINK_MARKERS):
        return False
    parsed = urlparse(url)
    if not parsed.netloc:
        return False
    url_signal = any(keyword in lower_url for keyword in _LINK_KEYWORDS)
    text_signal = _verification_text(source_text)
    return url_signal or text_signal


def _verification_text(text: str) -> bool:
    lower = str(text or "").lower()
    return any(keyword in lower for keyword in (*_CODE_KEYWORDS, *_LINK_KEYWORDS, "check your inbox"))


def _message_recipients_match(message: MailboxMessage, query: MailboxVerificationQuery) -> bool:
    if not message.recipients:
        return False
    target = normalize_email(query.application_email)
    return target in set(message.recipients)


def _message_matches_site_hint(message: MailboxMessage, query: MailboxVerificationQuery) -> bool:
    text = _combined_text(message).lower()
    host = normalize_hostname(query.site_hostname)
    host_parts = [part for part in host.split(".") if part and part not in {"com", "io", "net", "org"}]
    company = str(query.company_hint or "").strip().lower()
    if company and company in text:
        return True
    return any(part in text for part in host_parts[:2])


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
