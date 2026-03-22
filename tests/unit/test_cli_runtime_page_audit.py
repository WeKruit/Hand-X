from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from browser_use.agent.views import ActionResult

from ghosthands.cli import _apply_no_domhand_runtime_page_audit, _apply_runtime_page_audit, _build_runtime_page_audit_text


def _make_agent_with_results(*results: ActionResult, last_application_state: dict | None = None):
    browser_session = SimpleNamespace(_gh_last_application_state=last_application_state)
    history_entry = SimpleNamespace(result=list(results))
    history = SimpleNamespace(history=[history_entry])
    state = SimpleNamespace(last_result=list(results))
    return SimpleNamespace(browser_session=browser_session, history=history, state=state)


def test_build_runtime_page_audit_text_includes_blocker_context():
    text = _build_runtime_page_audit_text(
        {
            "current_section": "My Information",
            "advance_allowed": False,
            "unresolved_required_count": 2,
            "optional_validation_count": 0,
            "mismatched_count": 0,
            "opaque_count": 0,
            "unverified_count": 1,
            "blocking_field_labels": ["Home Street Address", "Military spouse"],
            "single_active_blocker": {"field_label": "Home Street Address"},
        }
    )

    assert text is not None
    assert "RUNTIME_PAGE_AUDIT:" in text
    assert "section=My Information" in text
    assert "unresolved_required=2" in text
    assert "Home Street Address" in text
    assert "Resolve this blocker next: Home Street Address." in text


async def test_apply_no_domhand_runtime_page_audit_appends_generic_page_audit_for_non_done_step():
    agent = _make_agent_with_results(
        ActionResult(extracted_content="Typed value into field"),
        last_application_state={
            "current_section": "My Information",
            "advance_allowed": False,
            "unresolved_required_count": 1,
            "optional_validation_count": 0,
            "mismatched_count": 0,
            "opaque_count": 0,
            "unverified_count": 0,
            "blocking_field_labels": ["Phone Device Type"],
            "single_active_blocker": {"field_label": "Phone Device Type"},
        },
    )

    with patch(
        "ghosthands.actions.domhand_assess_state.domhand_assess_state",
        AsyncMock(return_value=ActionResult(extracted_content="Application state refreshed")),
    ) as assess_mock:
        await _apply_no_domhand_runtime_page_audit(agent)

    assess_mock.assert_not_awaited()
    assert len(agent.history.history[-1].result) == 1


async def test_apply_no_domhand_runtime_page_audit_blocks_done_when_required_blockers_remain():
    agent = _make_agent_with_results(
        ActionResult(is_done=True, success=True, extracted_content="Reached final page"),
        last_application_state={
            "current_section": "Screening Questions",
            "page_context_key": "ctx-1",
            "page_url": "https://job-boards.greenhouse.io/example",
            "advance_allowed": False,
            "unresolved_required_count": 2,
            "blocking_field_keys": ["text|street", "select|spouse"],
            "blocking_field_labels": ["Home Street Address", "Are you a military spouse?"],
            "single_active_blocker": {
                "field_key": "text|street",
                "field_id": "ff-17",
                "field_label": "Home Street Address",
                "field_type": "text",
                "reason": "required_missing_value",
            },
        },
    )

    with patch(
        "ghosthands.actions.domhand_assess_state.domhand_assess_state",
        AsyncMock(return_value=ActionResult(extracted_content="Application state refreshed")),
    ) as assess_mock:
        await _apply_no_domhand_runtime_page_audit(agent)

    assess_mock.assert_not_awaited()
    assert len(agent.history.history[-1].result) == 1


async def test_apply_runtime_page_audit_auto_prefills_once_per_page_when_enabled():
    agent = _make_agent_with_results(
        ActionResult(extracted_content="Clicked Apply"),
        last_application_state={
            "page_context_key": "ctx-1",
            "page_url": "https://job-boards.greenhouse.io/example",
            "current_section": "My Information",
            "advance_allowed": False,
            "unresolved_required_count": 3,
            "optional_validation_count": 0,
            "mismatched_count": 0,
            "opaque_count": 0,
            "unverified_count": 0,
            "blocking_field_labels": ["Country*", "Phone", "Home Street Address"],
            "single_active_blocker": {"field_label": "Country*"},
        },
    )
    agent.browser_session._gh_domhand_execution_state = {}

    with (
        patch(
            "ghosthands.actions.domhand_assess_state.domhand_assess_state",
            AsyncMock(return_value=ActionResult(extracted_content="Application state refreshed")),
        ) as assess_mock,
        patch(
            "ghosthands.actions.domhand_fill.domhand_fill",
            AsyncMock(return_value=ActionResult(extracted_content="Bulk fill complete")),
        ) as fill_mock,
    ):
        await _apply_runtime_page_audit(agent, auto_domhand_prefill=True)

    fill_mock.assert_awaited_once()
    assert assess_mock.await_count == 2
    assert len(agent.history.history[-1].result) == 2
    appended = agent.history.history[-1].result[-1]
    assert appended.metadata == {"runtime_page_audit": True}
