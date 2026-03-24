"""Tests for the LLM escalation layer in fill_llm_escalation.py."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ghosthands.actions.views import FormField
from ghosthands.dom.fill_llm_escalation import (
    llm_execute_fill_suggestion,
    llm_suggest_fill_action,
    llm_verify_field_value,
)


def _make_field(field_type: str = "select", name: str = "Country") -> FormField:
    return FormField(
        field_id="ff-1",
        field_type=field_type,
        name=name,
        options=["United States", "Canada", "Mexico"],
    )


def _mock_anthropic_response(text: str) -> MagicMock:
    resp = MagicMock()
    block = MagicMock()
    block.text = text
    resp.content = [block]
    return resp


# ── llm_verify_field_value ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_returns_true_on_match():
    page = AsyncMock()
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock(return_value=b"\x89PNG_fake")

    field = _make_field()
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response('{"matches": true}')
    )
    with patch(
        "ghosthands.dom.fill_llm_escalation._get_anthropic_client",
        return_value=mock_client,
    ):
        result = await llm_verify_field_value(page, field, "United States")
    assert result is True


@pytest.mark.asyncio
async def test_verify_returns_false_on_mismatch():
    page = AsyncMock()
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock(return_value=b"\x89PNG_fake")

    field = _make_field()
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response('{"matches": false}')
    )
    with patch(
        "ghosthands.dom.fill_llm_escalation._get_anthropic_client",
        return_value=mock_client,
    ):
        result = await llm_verify_field_value(page, field, "Canada")
    assert result is False


@pytest.mark.asyncio
async def test_verify_returns_none_on_screenshot_failure():
    page = AsyncMock()
    page.query_selector = AsyncMock(side_effect=Exception("no DOM"))
    page.screenshot = AsyncMock(side_effect=Exception("no page"))

    field = _make_field()
    result = await llm_verify_field_value(page, field, "United States")
    assert result is None


@pytest.mark.asyncio
async def test_verify_returns_none_on_api_error():
    page = AsyncMock()
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock(return_value=b"\x89PNG_fake")

    field = _make_field()
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API down"))
    with patch(
        "ghosthands.dom.fill_llm_escalation._get_anthropic_client",
        return_value=mock_client,
    ):
        result = await llm_verify_field_value(page, field, "United States")
    assert result is None


# ── llm_suggest_fill_action ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_suggest_returns_strategy_dict():
    page = AsyncMock()
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock(return_value=b"\x89PNG_fake")

    field = _make_field()
    suggestion = {
        "strategy": "click_then_type",
        "selector": "#country-input",
        "steps": ["click selector", "type value"],
    }
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response(json.dumps(suggestion))
    )
    with patch(
        "ghosthands.dom.fill_llm_escalation._get_anthropic_client",
        return_value=mock_client,
    ):
        result = await llm_suggest_fill_action(page, field, "United States")
    assert result == suggestion


@pytest.mark.asyncio
async def test_suggest_returns_none_on_failure():
    page = AsyncMock()
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock(side_effect=Exception("fail"))

    field = _make_field()
    result = await llm_suggest_fill_action(page, field, "United States")
    assert result is None


# ── llm_execute_fill_suggestion ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_click_then_type():
    page = AsyncMock()
    el = AsyncMock()
    page.query_selector = AsyncMock(return_value=el)
    page.keyboard = AsyncMock()

    field = _make_field("text", "Name")
    result = await llm_execute_fill_suggestion(
        page, field, "John Doe",
        {"strategy": "click_then_type", "selector": "#name"},
    )
    assert result is True
    el.click.assert_awaited_once()
    page.keyboard.type.assert_awaited_once_with("John Doe", delay=30)


@pytest.mark.asyncio
async def test_execute_clear_and_retype():
    page = AsyncMock()
    el = AsyncMock()
    page.query_selector = AsyncMock(return_value=el)
    page.keyboard = AsyncMock()

    field = _make_field("text", "Email")
    result = await llm_execute_fill_suggestion(
        page, field, "test@example.com",
        {"strategy": "clear_and_retype", "selector": "#email"},
    )
    assert result is True
    el.click.assert_awaited_once_with(click_count=3)


@pytest.mark.asyncio
async def test_execute_unknown_strategy():
    page = AsyncMock()
    field = _make_field()
    result = await llm_execute_fill_suggestion(
        page, field, "value",
        {"strategy": "nonexistent"},
    )
    assert result is False


@pytest.mark.asyncio
async def test_execute_use_keyboard():
    page = AsyncMock()
    page.keyboard = AsyncMock()
    page.query_selector = AsyncMock(return_value=AsyncMock())

    field = _make_field("text", "Name")
    result = await llm_execute_fill_suggestion(
        page, field, "test",
        {
            "strategy": "use_keyboard",
            "steps": ["press:Tab", "type:hello", "click:#submit"],
        },
    )
    assert result is True
    page.keyboard.press.assert_awaited_once_with("Tab")
    page.keyboard.type.assert_awaited_once_with("hello", delay=30)


# ── Integration with fill_verify ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_pipeline_llm_override():
    """When DOM verify fails but LLM confirms, the fill should succeed."""
    import ghosthands.actions.domhand_fill  # noqa: F401 — force import before patching
    from ghosthands.dom.fill_verify import _attempt_domhand_fill_with_retry_cap

    page = AsyncMock()
    field = _make_field("text", "Name")

    with (
        patch("ghosthands.dom.fill_verify._field_already_matches", new_callable=AsyncMock, return_value=False),
        patch("ghosthands.dom.fill_verify._is_domhand_retry_capped_for_field", return_value=False),
        patch("ghosthands.dom.fill_verify._fill_single_field", new_callable=AsyncMock, return_value=True),
        patch("ghosthands.dom.fill_verify._verify_fill_observable", new_callable=AsyncMock, return_value=False),
        patch("ghosthands.dom.fill_verify._read_observed_field_value", new_callable=AsyncMock, return_value="wrong"),
        patch("ghosthands.dom.fill_verify._llm_verify_if_available", new_callable=AsyncMock, return_value=True),
        patch("ghosthands.dom.fill_verify._clear_domhand_failure_for_field"),
    ):
        success, msg, code, fc = await _attempt_domhand_fill_with_retry_cap(
            page, host="example.com", field=field,
            desired_value="John", tool_name="test",
        )
    assert success is True
    assert msg is None
    assert fc == 0.8


@pytest.mark.asyncio
async def test_verify_pipeline_llm_escalation_rescues():
    """When DOM fill fails, LLM escalation can rescue."""
    import ghosthands.actions.domhand_fill  # noqa: F401 — force import before patching
    from ghosthands.dom.fill_verify import _attempt_domhand_fill_with_retry_cap

    page = AsyncMock()
    field = _make_field("select", "Country")

    with (
        patch("ghosthands.dom.fill_verify._field_already_matches", new_callable=AsyncMock, return_value=False),
        patch("ghosthands.dom.fill_verify._is_domhand_retry_capped_for_field", return_value=False),
        patch("ghosthands.dom.fill_verify._fill_single_field", new_callable=AsyncMock, return_value=False),
        patch("ghosthands.dom.fill_verify._llm_verify_if_available", new_callable=AsyncMock, return_value=None),
        patch("ghosthands.dom.fill_verify._llm_escalate_fill", new_callable=AsyncMock, return_value=True),
        patch("ghosthands.dom.fill_verify._clear_domhand_failure_for_field"),
    ):
        success, msg, code, fc = await _attempt_domhand_fill_with_retry_cap(
            page, host="example.com", field=field,
            desired_value="United States", tool_name="test",
        )
    assert success is True
    assert fc == 0.8
