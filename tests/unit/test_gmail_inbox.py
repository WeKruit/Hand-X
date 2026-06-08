"""Unit tests for the local/dev Gmail inbox adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ghosthands.email_verification import (
    GmailInboxClient,
    MailboxVerificationQuery,
    build_gmail_search_query,
    gmail_email_to_mailbox_message,
    gmail_time_filter,
)

DETECTED_AT = datetime(2026, 6, 8, 8, 0, tzinfo=UTC)
APP_EMAIL = "candidate.qa@gmail.com"


class FakeGmailService:
    def __init__(
        self,
        emails: list[dict[str, Any]],
        *,
        authenticated: bool = False,
        auth_result: bool = True,
    ) -> None:
        self.emails = emails
        self.authenticated = authenticated
        self.auth_result = auth_result
        self.auth_calls = 0
        self.fetch_calls: list[dict[str, Any]] = []

    def is_authenticated(self) -> bool:
        return self.authenticated

    async def authenticate(self) -> bool:
        self.auth_calls += 1
        self.authenticated = self.auth_result
        return self.auth_result

    async def get_recent_emails(
        self,
        max_results: int = 10,
        query: str = "",
        time_filter: str = "1h",
    ) -> list[dict[str, Any]]:
        self.fetch_calls.append(
            {
                "max_results": max_results,
                "query": query,
                "time_filter": time_filter,
            }
        )
        return self.emails


def _query(**overrides) -> MailboxVerificationQuery:
    data = {
        "application_email": APP_EMAIL,
        "connected_email": APP_EMAIL,
        "detected_at": DETECTED_AT,
        "site_hostname": "jobs.example.com",
        "platform": "greenhouse",
        "company_hint": "Example",
        "expected_code_length": 6,
        "lookback_seconds": 300,
    }
    data.update(overrides)
    return MailboxVerificationQuery(**data)


def _gmail_email(**overrides) -> dict[str, Any]:
    data = {
        "id": "gmail-message-1",
        "thread_id": "thread-1",
        "subject": "Example Careers verification code",
        "from": "Example Careers <no-reply@jobs.example.com>",
        "to": f"Candidate QA <{APP_EMAIL}>",
        "date": "Mon, 08 Jun 2026 08:00:10 +0000",
        "timestamp": int(DETECTED_AT.timestamp() * 1000) + 10_000,
        "body": "Use verification code 482913 to continue your Example Careers application.",
    }
    data.update(overrides)
    return data


def test_gmail_time_filter_uses_smallest_clear_unit() -> None:
    assert gmail_time_filter(0) == "1m"
    assert gmail_time_filter(300) == "5m"
    assert gmail_time_filter(3600) == "1h"
    assert gmail_time_filter(90_000) == "2d"


def test_build_gmail_search_query_includes_recipient_and_verification_terms() -> None:
    gmail_query = build_gmail_search_query(_query())

    assert f"to:{APP_EMAIL}" in gmail_query
    assert "verification" in gmail_query
    assert "security code" in gmail_query


def test_gmail_email_to_mailbox_message_normalizes_headers_and_timestamp() -> None:
    message = gmail_email_to_mailbox_message(_gmail_email())

    assert message.message_id == "gmail-message-1"
    assert message.received_at == datetime.fromtimestamp((int(DETECTED_AT.timestamp() * 1000) + 10_000) / 1000, tz=UTC)
    assert message.sender == "Example Careers <no-reply@jobs.example.com>"
    assert message.recipients == (APP_EMAIL,)
    assert "482913" in message.body_text


@pytest.mark.asyncio
async def test_gmail_inbox_client_authenticates_fetches_and_ranks_candidates() -> None:
    service = FakeGmailService([_gmail_email()])
    client = GmailInboxClient(gmail_service=service)

    candidates = await client.list_verification_candidates(_query())

    assert service.auth_calls == 1
    assert len(service.fetch_calls) == 1
    assert service.fetch_calls[0]["time_filter"] == "5m"
    assert f"to:{APP_EMAIL}" in service.fetch_calls[0]["query"]
    assert len(candidates) == 1
    assert candidates[0].message_id == "gmail-message-1"
    assert candidates[0].artifact_value == "482913"


@pytest.mark.asyncio
async def test_gmail_inbox_client_can_supply_connected_email() -> None:
    service = FakeGmailService([_gmail_email()], authenticated=True)
    client = GmailInboxClient(gmail_service=service, connected_email=APP_EMAIL)

    candidates = await client.list_verification_candidates(_query(connected_email=None))

    assert service.auth_calls == 0
    assert len(candidates) == 1
    assert candidates[0].artifact_value == "482913"


@pytest.mark.asyncio
async def test_gmail_inbox_client_returns_no_candidates_when_auth_fails() -> None:
    service = FakeGmailService([_gmail_email()], auth_result=False)
    client = GmailInboxClient(gmail_service=service)

    candidates = await client.list_verification_candidates(_query())

    assert candidates == []
    assert service.auth_calls == 1
    assert service.fetch_calls == []
