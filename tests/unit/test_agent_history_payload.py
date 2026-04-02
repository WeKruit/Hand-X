"""Tests for ghosthands.output.agent_history_payload."""

from __future__ import annotations

from unittest.mock import MagicMock

from ghosthands.output.agent_history_payload import build_agent_history_payload


def test_none_history_returns_empty_shape() -> None:
    p = build_agent_history_payload(None)
    assert p["schemaVersion"] == 1
    assert p["history"] == []
    assert p["itemCount"] == 0
    assert p["usage"] is None


def test_magicmock_history_falls_back_safely() -> None:
    h = MagicMock()
    h.history = [MagicMock(), MagicMock()]
    h.model_dump.return_value = {"history": []}
    p = build_agent_history_payload(h, None)
    assert p["schemaVersion"] == 1
    assert isinstance(p["history"], list)
