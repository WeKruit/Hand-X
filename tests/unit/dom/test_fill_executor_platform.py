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


def test_workday_platform_does_not_override_select_fill_strategy():
    from ghosthands.platforms import get_fill_overrides

    overrides = get_fill_overrides("https://company.wd5.myworkdayjobs.com/en-US/jobs/apply")

    assert overrides.get("date") == "segmented_date"
    assert "select" not in overrides


@pytest.mark.asyncio
async def test_fill_select_field_outcome_routes_workday_referral_source_to_dedicated_handler():
    from ghosthands.dom.fill_executor import _fill_select_field_outcome

    fake_page = AsyncMock()
    field = _make_field("select", name="How Did You Hear About Us?")

    with (
        patch(
            "ghosthands.dom.fill_executor._safe_page_url",
            new=AsyncMock(return_value="https://company.wd5.myworkdayjobs.com/en-US/jobs/apply"),
        ),
        patch(
            "ghosthands.dom.fill_executor._fill_workday_referral_source_select",
            new_callable=AsyncMock,
            return_value=FieldFillOutcome(success=True, matched_label="Other"),
        ) as mock_referral,
        patch(
            "ghosthands.dom.fill_executor._fill_custom_dropdown_outcome",
            new_callable=AsyncMock,
        ) as mock_generic,
    ):
        result = await _fill_select_field_outcome(fake_page, field, "Other", "[How Did You Hear About Us?]")

    assert result.success is True
    mock_referral.assert_awaited_once()
    mock_generic.assert_not_awaited()


@pytest.mark.asyncio
async def test_fill_select_field_outcome_keeps_non_referral_workday_selects_on_generic_path():
    from ghosthands.dom.fill_executor import _fill_select_field_outcome

    fake_page = AsyncMock()
    field = _make_field("select", name="Country")

    with (
        patch(
            "ghosthands.dom.fill_executor._safe_page_url",
            new=AsyncMock(return_value="https://company.wd5.myworkdayjobs.com/en-US/jobs/apply"),
        ),
        patch(
            "ghosthands.dom.fill_executor._fill_workday_referral_source_select",
            new_callable=AsyncMock,
        ) as mock_referral,
        patch(
            "ghosthands.dom.fill_executor._fill_custom_dropdown_outcome",
            new_callable=AsyncMock,
            return_value=FieldFillOutcome(success=True, matched_label="United States"),
        ) as mock_generic,
    ):
        result = await _fill_select_field_outcome(fake_page, field, "United States", "[Country]")

    assert result.success is True
    mock_referral.assert_not_awaited()
    mock_generic.assert_awaited_once()


@pytest.mark.asyncio
async def test_workday_referral_source_uses_arrow_enter_last_resort():
    """Main's _fill_custom_dropdown uses ArrowDown+Enter as last resort when no
    click match is found. Referral source is intentionally generous."""
    from ghosthands.dom.fill_executor import _fill_workday_referral_source_select

    fake_page = AsyncMock()
    field = _make_field("select", name="How Did You Hear About Us?")

    with (
        patch("ghosthands.dom.fill_executor._try_open_combobox_menu", new=AsyncMock()),
        patch("ghosthands.dom.fill_executor._clear_dropdown_search", new=AsyncMock()),
        patch("ghosthands.dom.fill_executor._type_text_compat", new=AsyncMock()),
        patch("ghosthands.dom.fill_executor._click_dropdown_option", new=AsyncMock(return_value={"clicked": False})),
        patch("ghosthands.dom.fill_executor._poll_click_dropdown_option", new=AsyncMock(return_value={"clicked": False})),
        patch("ghosthands.dom.fill_executor._settle_dropdown_selection", new=AsyncMock()),
        patch("ghosthands.dom.fill_executor._press_key_compat", new=AsyncMock()) as mock_press,
    ):
        result = await _fill_workday_referral_source_select(
            fake_page,
            field,
            "Other",
            "[How Did You Hear About Us?]",
        )

    # Main-like: ArrowDown+Enter last resort → optimistic success
    assert result.success is True
    assert mock_press.await_count >= 2  # ArrowDown + Enter
