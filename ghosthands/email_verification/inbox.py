"""Inbox client protocol and fake inbox adapter for Phase 1 tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from ghosthands.email_verification.models import MailboxMessage, MailboxVerificationQuery, VerificationEmailCandidate
from ghosthands.email_verification.selection import rank_verification_candidates


@runtime_checkable
class InboxClient(Protocol):
    """Narrow mailbox capability consumed by Hand-X verification recovery."""

    async def list_verification_candidates(
        self,
        query: MailboxVerificationQuery,
    ) -> list[VerificationEmailCandidate]:
        """Return ranked code/link candidates for the current verification wall."""
        ...


class FakeInboxClient:
    """Deterministic in-memory inbox for unit and local harness tests."""

    def __init__(self, messages: list[MailboxMessage]) -> None:
        self._messages = tuple(messages)

    @classmethod
    def from_json_file(cls, path: str | Path) -> FakeInboxClient:
        """Load fake messages from a JSON array or {"messages": [...]} file."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = raw.get("messages", raw) if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            raise ValueError("Fake inbox fixture must be a JSON list or object with a messages list")
        return cls([MailboxMessage.model_validate(row) for row in rows])

    async def list_verification_candidates(
        self,
        query: MailboxVerificationQuery,
    ) -> list[VerificationEmailCandidate]:
        return rank_verification_candidates(list(self._messages), query)
