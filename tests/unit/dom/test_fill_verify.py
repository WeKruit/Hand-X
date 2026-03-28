"""Unit tests for observable fill verification plumbing."""

from unittest.mock import AsyncMock

import pytest

from ghosthands.actions.views import FormField
from ghosthands.dom import fill_verify
from ghosthands.dom.fill_executor import FieldFillOutcome


@pytest.mark.asyncio
async def test_field_already_matches_accepts_dropdown_matched_label(monkeypatch):
    field = FormField(field_id="ff-6", name="Country", field_type="select")

    monkeypatch.setattr(
        fill_verify,
        "_read_field_value_for_field",
        AsyncMock(return_value="+1"),
    )
    monkeypatch.setattr(
        fill_verify,
        "_field_has_validation_error",
        AsyncMock(return_value=False),
    )

    assert await fill_verify._field_already_matches(
        object(),
        field,
        "United States",
        matched_label="United States +1",
    )


@pytest.mark.asyncio
async def test_attempt_domhand_fill_with_retry_cap_returns_matched_label_as_settled_value(monkeypatch):
    field = FormField(field_id="ff-6", name="Country", field_type="select")
    page = object()

    verify_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(fill_verify, "_field_already_matches", AsyncMock(return_value=False))
    monkeypatch.setattr(fill_verify, "_is_domhand_retry_capped_for_field", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        fill_verify,
        "_fill_single_field_outcome",
        AsyncMock(return_value=FieldFillOutcome(success=True, matched_label="United States +1")),
    )
    monkeypatch.setattr(fill_verify, "_verify_fill_observable", verify_mock)
    monkeypatch.setattr(fill_verify, "_clear_domhand_failure_for_field", lambda *args, **kwargs: None)

    success, error, failure_reason, fill_confidence, settled_value = await fill_verify._attempt_domhand_fill_with_retry_cap(
        page,
        host="job-boards.greenhouse.io",
        field=field,
        desired_value="United States",
        tool_name="domhand_fill",
    )

    assert success is True
    assert error is None
    assert failure_reason is None
    assert fill_confidence == 1.0
    assert settled_value == "United States +1"
    verify_mock.assert_awaited_once_with(
        page,
        field,
        "United States",
        matched_label="United States +1",
    )
