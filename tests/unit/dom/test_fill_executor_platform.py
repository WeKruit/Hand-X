"""Tests for platform-aware fill routing in fill_executor."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ghosthands.actions.views import FormField
from ghosthands.dom.fill_executor import FieldFillOutcome, _dispatch_platform_fill


def _make_field(field_type: str = "select", name: str = "Country") -> FormField:
    return FormField(
        field_id="ff-1",
        field_type=field_type,
        name=name,
        options=[],
    )


@pytest.mark.asyncio
async def test_dispatch_combobox_toggle():
    fake_page = AsyncMock()
    field = _make_field("select")
    with patch(
        "ghosthands.dom.fill_executor._fill_custom_dropdown_outcome",
        new_callable=AsyncMock,
        return_value=FieldFillOutcome(success=True, matched_label="United States"),
    ) as mock_fill:
        result = await _dispatch_platform_fill(
            fake_page, field, "United States", "[Country]", "combobox_toggle",
        )
    assert result is True
    mock_fill.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_react_select():
    fake_page = AsyncMock()
    field = _make_field("select")
    with patch(
        "ghosthands.dom.fill_executor._fill_custom_dropdown_outcome",
        new_callable=AsyncMock,
        return_value=FieldFillOutcome(success=True, matched_label="Engineering"),
    ) as mock_fill:
        result = await _dispatch_platform_fill(
            fake_page,
            field,
            "Engineering",
            "[Department]",
            "react_select",
            browser_session=None,
        )
    assert result is True
    mock_fill.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_segmented_date():
    fake_page = AsyncMock()
    field = _make_field("date", name="Start Date")
    with patch(
        "ghosthands.dom.fill_executor._fill_grouped_date_field",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_fill:
        result = await _dispatch_platform_fill(
            fake_page, field, "2024-01-15", "[Start Date]", "segmented_date",
        )
    assert result is True
    mock_fill.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_unknown_strategy_returns_none():
    fake_page = AsyncMock()
    field = _make_field("select")
    result = await _dispatch_platform_fill(
        fake_page, field, "value", "[tag]", "nonexistent_strategy",
    )
    assert result is None


@pytest.mark.asyncio
async def test_fill_single_field_uses_platform_override():
    """When platform returns a fill override, _fill_single_field dispatches through it."""
    from ghosthands.dom.fill_executor import _fill_single_field

    fake_page = AsyncMock()
    fake_page.evaluate = AsyncMock(side_effect=[
        "true",
        "https://company.wd5.myworkdayjobs.com/en-US/jobs/apply",
    ])

    field = _make_field("select")
    with (
        patch(
            "ghosthands.platforms.get_fill_overrides",
            return_value={"select": "combobox_toggle"},
        ),
        patch(
            "ghosthands.dom.fill_executor._dispatch_platform_fill_outcome",
            new_callable=AsyncMock,
            return_value=FieldFillOutcome(success=True, matched_label="United States"),
        ) as mock_dispatch,
    ):
        result = await _fill_single_field(fake_page, field, "United States")
    assert result is True
    mock_dispatch.assert_awaited_once()
