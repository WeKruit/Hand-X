"""Runtime follow-up tests for blocker recovery orchestration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from browser_use.agent.views import ActionResult
from ghosthands.agent.handx_agent import HandXAgent


def _make_agent_with_browser_state(current_url: str) -> HandXAgent:
	agent = object.__new__(HandXAgent)
	agent.task_id = "task-1234"
	agent.state = SimpleNamespace(n_steps=7)
	agent.browser_session = SimpleNamespace(
		id="browser-1234",
		agent_focus_target_id="focus-1",
		get_current_page_url=AsyncMock(return_value=current_url),
		event_bus=SimpleNamespace(dispatch=lambda *_args, **_kwargs: None),
	)
	return agent


async def test_manual_click_followup_records_expected_value_then_reassesses():
	agent = _make_agent_with_browser_state("https://jobs.lever.co/acme/123/apply")
	pre_action_state = {
		"page_url": "https://jobs.lever.co/acme/123/apply",
		"single_active_blocker": {
			"field_key": "radio-group|ff-28",
			"field_id": "ff-28",
			"field_label": "Are you legally authorized to work in the United States?*",
			"field_type": "radio-group",
		},
		"recovery_target": {
			"field_id": "ff-28",
			"field_key": "radio-group|ff-28",
			"field_type": "radio-group",
			"field_label": "Are you legally authorized to work in the United States?*",
			"question_text": "Are you legally authorized to work in the United States?*",
			"section": "Application Questions",
			"desired_value": "No",
			"allowed_action_family": "binary",
		},
	}

	with (
		patch(
			"ghosthands.actions.domhand_record_expected_value.domhand_record_expected_value",
			AsyncMock(return_value=ActionResult(extracted_content="Recorded expected value")),
		) as record_mock,
		patch(
			"ghosthands.actions.domhand_assess_state.domhand_assess_state",
			AsyncMock(return_value=ActionResult(extracted_content="Application state: review")),
		) as assess_mock,
	):
		results = await agent._maybe_run_runtime_followups(
			action_name="click",
			result=ActionResult(extracted_content="Clicked input type=radio"),
			pre_action_url="https://jobs.lever.co/acme/123/apply",
			pre_action_state=pre_action_state,
		)

	assert len(results) == 2
	record_mock.assert_awaited_once()
	assess_mock.assert_awaited_once()
	record_params = record_mock.await_args.args[0]
	assert record_params.field_id == "ff-28"
	assert record_params.field_type == "radio-group"
	assert record_params.expected_value == "No"
	assert record_params.target_section == "Application Questions"
	assert results[-1].extracted_content == "Application state: review"


async def test_manual_click_followup_still_reassesses_when_recording_is_not_settled():
	agent = _make_agent_with_browser_state("https://jobs.lever.co/acme/123/apply")
	pre_action_state = {
		"page_url": "https://jobs.lever.co/acme/123/apply",
		"single_active_blocker": {
			"field_key": "radio-group|ff-31",
			"field_id": "ff-31",
			"field_label": "Will you require sponsorship?*",
			"field_type": "radio-group",
		},
		"recovery_target": {
			"field_id": "ff-31",
			"field_key": "radio-group|ff-31",
			"field_type": "radio-group",
			"field_label": "Will you require sponsorship?*",
			"question_text": "Will you require sponsorship?*",
			"desired_value": "Yes",
			"allowed_action_family": "binary",
		},
	}

	with (
		patch(
			"ghosthands.actions.domhand_record_expected_value.domhand_record_expected_value",
			AsyncMock(return_value=ActionResult(error="Observed value does not yet match expected value")),
		) as record_mock,
		patch(
			"ghosthands.actions.domhand_assess_state.domhand_assess_state",
			AsyncMock(return_value=ActionResult(extracted_content="Application state: advanceable")),
		) as assess_mock,
	):
		results = await agent._maybe_run_runtime_followups(
			action_name="click",
			result=ActionResult(extracted_content="Clicked input type=radio"),
			pre_action_url="https://jobs.lever.co/acme/123/apply",
			pre_action_state=pre_action_state,
		)

	assert len(results) == 1
	record_mock.assert_awaited_once()
	assess_mock.assert_awaited_once()
	assert results[0].extracted_content == "Application state: advanceable"


async def test_successful_tool_result_that_requests_assessment_is_auto_reassessed():
	agent = _make_agent_with_browser_state("https://example.wd1.myworkdayjobs.com/job")

	with patch(
		"ghosthands.actions.domhand_assess_state.domhand_assess_state",
		AsyncMock(return_value=ActionResult(extracted_content="Application state: advanceable")),
	) as assess_mock:
		results = await agent._maybe_run_runtime_followups(
			action_name="domhand_interact_control",
			result=ActionResult(
				extracted_content="Interacted with blocker control.",
				metadata={"recommended_next_action": "call domhand_assess_state"},
			),
			pre_action_url="https://example.wd1.myworkdayjobs.com/job",
			pre_action_state=None,
		)

	assert len(results) == 1
	assess_mock.assert_awaited_once()
	assert results[0].extracted_content == "Application state: advanceable"


async def test_primary_blocker_guard_rejects_later_generic_field_target():
	agent = object.__new__(HandXAgent)
	agent.browser_session = SimpleNamespace(
		get_dom_element_by_index=AsyncMock(
			return_value=SimpleNamespace(
				attributes={"aria-label": "Are you legally authorized to work in the United States?"},
				ax_node=SimpleNamespace(name="Are you legally authorized to work in the United States?"),
				get_meaningful_text_for_llm=lambda: "Are you legally authorized to work in the United States?",
				get_all_children_text=lambda: "Are you legally authorized to work in the United States?",
			)
		)
	)

	last_state = {
		"blocking_field_keys": ["text|home_street_address", "combobox|work_authorization"],
		"primary_active_blocker": {
			"field_key": "text|home_street_address",
			"field_id": "question_home_address",
			"field_label": "Please provide Home Street Address*",
			"question_text": "Please provide Home Street Address*",
			"field_type": "text",
		},
	}
	actions_data = [{"action": "click", "params": {"index": 21}}]

	decision = await agent._guard_primary_blocker_target(actions_data, last_state)

	assert decision is not None
	assert decision["reason"] == "manual_non_primary_blocker_target"
	assert "Home Street Address" in decision["message"]


async def test_manual_runtime_followups_are_disabled_when_domhand_is_disabled():
	agent = _make_agent_with_browser_state("https://job-boards.greenhouse.io/acme/123")
	pre_action_state = {
		"page_url": "https://job-boards.greenhouse.io/acme/123",
		"single_active_blocker": {
			"field_key": "text|phone",
			"field_id": "ff-9",
			"field_label": "Phone*",
			"field_type": "tel",
		},
		"recovery_target": {
			"field_id": "ff-9",
			"field_key": "text|phone",
			"field_type": "tel",
			"field_label": "Phone*",
			"question_text": "Phone*",
			"desired_value": "6466789391",
			"allowed_action_family": "text",
		},
	}

	with (
		patch.object(__import__("ghosthands.agent.handx_agent", fromlist=["settings"]).settings, "enable_domhand", False),
		patch(
			"ghosthands.actions.domhand_assess_state.domhand_assess_state",
			AsyncMock(return_value=ActionResult(extracted_content="Application state: advanceable")),
		) as assess_mock,
	):
		results = await agent._maybe_run_runtime_followups(
			action_name="input",
			result=ActionResult(extracted_content="Typed phone number"),
			pre_action_url="https://job-boards.greenhouse.io/acme/123",
			pre_action_state=pre_action_state,
		)

	assert results == []
	assess_mock.assert_not_awaited()


async def test_pending_manual_recovery_guard_blocks_drifting_to_later_field():
	agent = object.__new__(HandXAgent)
	agent.state = SimpleNamespace(
		last_model_output=SimpleNamespace(
			current_state=SimpleNamespace(
				evaluation_previous_goal=(
					"The action to click 'NY' was performed, but the browser state does not yet "
					"reflect the updated value in the input field. Verdict: Uncertain."
				)
			)
		)
	)
	agent.browser_session = SimpleNamespace(
		_gh_pending_manual_recovery={
			"field_index": 16,
			"field_label": "Please provide Home State*",
			"expected_value": "NY",
			"phase": "awaiting_settlement",
		},
		get_dom_element_by_index=AsyncMock(
			side_effect=[
				SimpleNamespace(
					attributes={"role": "combobox", "aria-label": "Please provide Home State*"},
					ax_node=SimpleNamespace(name="Please provide Home State*"),
					get_meaningful_text_for_llm=lambda: "Please provide Home State* Select...",
					get_all_children_text=lambda *args, **kwargs: "Please provide Home State* Select...",
				),
				SimpleNamespace(
					attributes={"aria-label": "Are you currently working under a non-compete agreement?*"},
					ax_node=SimpleNamespace(name="Are you currently working under a non-compete agreement?*"),
					get_meaningful_text_for_llm=lambda: "Are you currently working under a non-compete agreement?*",
					get_all_children_text=lambda *args, **kwargs: "Are you currently working under a non-compete agreement?*",
				),
			]
		),
	)

	with patch.object(__import__("ghosthands.agent.handx_agent", fromlist=["settings"]).settings, "enable_domhand", False):
		decision = await agent._guard_pending_manual_recovery([{"action": "click", "params": {"index": 19}}])

	assert decision is not None
	assert decision["reason"] == "pending_manual_recovery"
	assert "Home State" in decision["message"]


async def test_pending_manual_recovery_guard_allows_clicking_matching_option():
	agent = object.__new__(HandXAgent)
	agent.state = SimpleNamespace(
		last_model_output=SimpleNamespace(
			current_state=SimpleNamespace(evaluation_previous_goal="Typed 'NY' into the Home State combobox.")
		)
	)
	agent.browser_session = SimpleNamespace(
		_gh_pending_manual_recovery={
			"field_index": 16,
			"field_label": "Please provide Home State*",
			"expected_value": "NY",
			"phase": "awaiting_selection",
		},
		get_dom_element_by_index=AsyncMock(
			side_effect=[
				SimpleNamespace(
					attributes={"role": "option", "aria-label": "NY"},
					ax_node=SimpleNamespace(name="NY"),
					get_meaningful_text_for_llm=lambda: "NY",
					get_all_children_text=lambda *args, **kwargs: "NY",
				)
			]
		),
	)

	with patch.object(__import__("ghosthands.agent.handx_agent", fromlist=["settings"]).settings, "enable_domhand", False):
		decision = await agent._guard_pending_manual_recovery([{"action": "click", "params": {"index": 2971}}])

	assert decision is None
