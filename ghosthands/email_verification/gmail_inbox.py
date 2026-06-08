"""Local/dev Gmail InboxClient adapter for email-verification recovery."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Protocol, runtime_checkable

from ghosthands.email_verification.models import MailboxMessage, MailboxVerificationQuery, VerificationEmailCandidate
from ghosthands.email_verification.selection import rank_verification_candidates

_VERIFICATION_TERMS = (
    '"verification code"',
    '"security code"',
    '"one-time code"',
    '"one time code"',
    "verification",
    "verify",
    "confirm",
    "code",
)
_HTML_MARKER_RE = re.compile(r"<(?:html|body|a|div|p|span|br)\b", re.IGNORECASE)


@runtime_checkable
class GmailServiceLike(Protocol):
    """Subset of GmailService consumed by the local/dev inbox adapter."""

    def is_authenticated(self) -> bool:
        """Return whether the Gmail service is ready for API reads."""
        ...

    async def authenticate(self) -> bool:
        """Authenticate or refresh credentials."""
        ...

    async def get_recent_emails(
        self,
        max_results: int = 10,
        query: str = "",
        time_filter: str = "1h",
    ) -> list[dict[str, Any]]:
        """Return raw Gmail email dictionaries."""
        ...


class GmailInboxClient:
    """InboxClient implementation backed by the existing Gmail API service."""

    def __init__(
        self,
        *,
        gmail_service: GmailServiceLike | None = None,
        connected_email: str | None = None,
        credentials_file: str | None = None,
        token_file: str | None = None,
        config_dir: str | None = None,
        access_token: str | None = None,
    ) -> None:
        self._gmail_service = gmail_service or _build_default_gmail_service(
            credentials_file=credentials_file,
            token_file=token_file,
            config_dir=config_dir,
            access_token=access_token,
        )
        self._connected_email = connected_email

    async def authenticate(self) -> bool:
        """Authenticate the underlying Gmail service."""

        if self._gmail_service.is_authenticated():
            return True
        return await self._gmail_service.authenticate()

    async def list_verification_candidates(
        self,
        query: MailboxVerificationQuery,
    ) -> list[VerificationEmailCandidate]:
        """Return ranked candidates from recent Gmail messages."""

        if not await self.authenticate():
            return []

        effective_query = query
        if not effective_query.connected_email and self._connected_email:
            effective_query = effective_query.model_copy(update={"connected_email": self._connected_email})

        raw_emails = await self._gmail_service.get_recent_emails(
            max_results=effective_query.max_results,
            query=build_gmail_search_query(effective_query),
            time_filter=gmail_time_filter(effective_query.lookback_seconds),
        )
        messages = [gmail_email_to_mailbox_message(row) for row in raw_emails]
        return rank_verification_candidates(messages, effective_query)


def _build_default_gmail_service(
    *,
    credentials_file: str | None,
    token_file: str | None,
    config_dir: str | None,
    access_token: str | None,
) -> GmailServiceLike:
    from browser_use.integrations.gmail.service import GmailService

    return GmailService(
        credentials_file=credentials_file,
        token_file=token_file,
        config_dir=config_dir,
        access_token=access_token,
    )


def build_gmail_search_query(query: MailboxVerificationQuery) -> str:
    """Build a broad Gmail search query while leaving final ranking deterministic."""

    parts: list[str] = []
    if query.application_email:
        parts.append(f"to:{query.application_email}")
    parts.append("(" + " OR ".join(_VERIFICATION_TERMS) + ")")
    return " ".join(parts)


def gmail_time_filter(lookback_seconds: int) -> str:
    """Convert the resolver lookback window into Gmail's newer_than filter."""

    seconds = max(0, int(lookback_seconds))
    minutes = max(1, math.ceil(seconds / 60))
    if minutes < 60:
        return f"{minutes}m"
    hours = math.ceil(minutes / 60)
    if hours < 24:
        return f"{hours}h"
    return f"{math.ceil(hours / 24)}d"


def gmail_email_to_mailbox_message(row: Mapping[str, Any]) -> MailboxMessage:
    """Normalize one GmailService email dictionary into a MailboxMessage."""

    body = str(row.get("body") or row.get("body_text") or "")
    body_html = body if _HTML_MARKER_RE.search(body) else str(row.get("body_html") or "")
    message_id = str(row.get("id") or row.get("message_id") or row.get("thread_id") or "").strip()
    if not message_id:
        message_id = f"gmail:{row.get('timestamp') or row.get('date') or 'unknown'}"

    return MailboxMessage(
        message_id=message_id,
        received_at=_parse_received_at(row),
        sender=str(row.get("from") or row.get("sender") or ""),
        subject=str(row.get("subject") or ""),
        recipients=tuple(_parse_email_addresses(str(row.get("to") or row.get("recipients") or ""))),
        body_text=body,
        body_html=body_html,
    )


def _parse_received_at(row: Mapping[str, Any]) -> datetime:
    raw_timestamp = row.get("timestamp") or row.get("internalDate")
    if isinstance(raw_timestamp, int | float):
        seconds = float(raw_timestamp) / 1000 if raw_timestamp > 10_000_000_000 else float(raw_timestamp)
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(raw_timestamp, str) and raw_timestamp.strip().isdigit():
        value = int(raw_timestamp.strip())
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=UTC)

    raw_date = str(row.get("date") or "").strip()
    if raw_date:
        parsed = parsedate_to_datetime(raw_date)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    return datetime.now(UTC)


def _parse_email_addresses(header_value: str) -> list[str]:
    return [email.lower() for _name, email in getaddresses([header_value]) if email]
