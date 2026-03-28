"""Tests for conditional field re-discovery in domhand_fill."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ghosthands.actions.domhand_fill import _rescan_for_conditional_fields
from ghosthands.actions.views import FormField


def _make_field(fid: str, name: str, field_type: str = "text") -> FormField:
    return FormField(field_id=fid, name=name, field_type=field_type, options=[])


@pytest.mark.asyncio
async def test_rescan_returns_only_new_fields():
    existing = _make_field("f1", "First Name")
    revealed = _make_field("f2", "Please explain")

    page = AsyncMock()
    with patch(
        "ghosthands.actions.domhand_fill.extract_visible_form_fields",
        new_callable=AsyncMock,
        return_value=[existing, revealed],
    ):
        new_fields = await _rescan_for_conditional_fields(
            page,
            known_field_ids={"f1"},
        )
    assert len(new_fields) == 1
    assert new_fields[0].field_id == "f2"


@pytest.mark.asyncio
async def test_rescan_returns_empty_when_no_new_fields():
    existing = _make_field("f1", "First Name")

    page = AsyncMock()
    with patch(
        "ghosthands.actions.domhand_fill.extract_visible_form_fields",
        new_callable=AsyncMock,
        return_value=[existing],
    ):
        new_fields = await _rescan_for_conditional_fields(
            page,
            known_field_ids={"f1"},
        )
    assert new_fields == []


@pytest.mark.asyncio
async def test_rescan_handles_extraction_failure():
    page = AsyncMock()
    with patch(
        "ghosthands.actions.domhand_fill.extract_visible_form_fields",
        new_callable=AsyncMock,
        side_effect=Exception("DOM error"),
    ):
        new_fields = await _rescan_for_conditional_fields(
            page,
            known_field_ids=set(),
        )
    assert new_fields == []


@pytest.mark.asyncio
async def test_rescan_respects_section_filter():
    """Conditional re-scan should still apply target_section filtering."""
    f1 = _make_field("f1", "First Name")
    f1.section = "Personal"
    f2 = _make_field("f2", "Company")
    f2.section = "Work"

    page = AsyncMock()
    with (
        patch(
            "ghosthands.actions.domhand_fill.extract_visible_form_fields",
            new_callable=AsyncMock,
            return_value=[f1, f2],
        ),
        patch(
            "ghosthands.actions.domhand_fill._filter_fields_for_scope",
            side_effect=lambda fields, **kw: [f for f in fields if f.section == "Personal"],
        ),
    ):
        new_fields = await _rescan_for_conditional_fields(
            page,
            known_field_ids=set(),
            target_section="Personal",
        )
    assert len(new_fields) == 1
    assert new_fields[0].name == "First Name"
