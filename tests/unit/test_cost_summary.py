from __future__ import annotations

from types import SimpleNamespace

from ghosthands.cost_summary import build_cost_summary, mark_stagehand_usage, summarize_history_cost


def test_build_cost_summary_combines_browser_use_and_domhand() -> None:
    summary = build_cost_summary(
        browser_use_cost_usd=0.25,
        browser_use_prompt_tokens=100,
        browser_use_completion_tokens=40,
        domhand_cost_usd=0.10,
        domhand_prompt_tokens=20,
        domhand_completion_tokens=5,
        domhand_llm_calls=2,
        domhand_models=["google/gemini-3-flash-preview"],
    ).to_dict()

    assert summary["total_tracked_cost_usd"] == 0.35
    assert summary["total_tracked_prompt_tokens"] == 120
    assert summary["total_tracked_completion_tokens"] == 45
    assert summary["total_tracked_tokens"] == 165
    assert summary["domhand_llm_calls"] == 2
    assert summary["domhand_models"] == ["google/gemini-3-flash-preview"]
    assert summary["untracked_cost_possible"] is False
    assert summary["stagehand_calls"] == 0


def test_summarize_history_cost_marks_stagehand_as_untracked() -> None:
    history = SimpleNamespace(
        usage=SimpleNamespace(
            total_cost=0.25,
            total_prompt_tokens=100,
            total_completion_tokens=40,
        ),
        history=[
            SimpleNamespace(
                result=[
                    SimpleNamespace(
                        metadata={
                            "step_cost": 0.10,
                            "input_tokens": 20,
                            "output_tokens": 5,
                            "domhand_llm_calls": 2,
                            "model": "google/gemini-3-flash-preview",
                        }
                    )
                ]
            )
        ],
    )
    browser_session = SimpleNamespace()
    mark_stagehand_usage(browser_session, source="domhand_fill_escalation")
    mark_stagehand_usage(browser_session, source="domhand_fill_escalation")

    summary = summarize_history_cost(history, browser_session)

    assert summary["total_tracked_cost_usd"] == 0.35
    assert summary["total_tracked_tokens"] == 165
    assert summary["domhand_llm_calls"] == 2
    assert summary["stagehand_used"] is True
    assert summary["stagehand_calls"] == 2
    assert summary["untracked_cost_possible"] is True
    assert summary["untracked_reasons"] == ["stagehand_cost_not_tracked"]
    assert summary["stagehand_sources"] == ["domhand_fill_escalation"]


def test_stagehand_call_count_tracks_each_invocation() -> None:
    browser_session = SimpleNamespace()
    mark_stagehand_usage(browser_session, source="stagehand_fill_tool")
    mark_stagehand_usage(browser_session, source="stagehand_fill_tool")
    mark_stagehand_usage(browser_session, source="stagehand_observe_tool")

    summary = build_cost_summary(
        stagehand_used=True,
        stagehand_calls=3,
        stagehand_sources=["stagehand_fill_tool", "stagehand_observe_tool"],
    ).to_dict()

    assert summary["stagehand_calls"] == 3
    assert summary["stagehand_sources"] == ["stagehand_fill_tool", "stagehand_observe_tool"]
    assert summary["untracked_cost_possible"] is True


def test_output_contains_all_three_subsystem_fields() -> None:
    summary = build_cost_summary(
        browser_use_cost_usd=0.20,
        domhand_cost_usd=0.05,
        domhand_llm_calls=1,
        stagehand_used=True,
        stagehand_calls=2,
    ).to_dict()

    # Browser-use fields
    assert "browser_use_cost_usd" in summary
    assert "browser_use_prompt_tokens" in summary
    assert "browser_use_completion_tokens" in summary
    # DomHand fields
    assert "domhand_cost_usd" in summary
    assert "domhand_prompt_tokens" in summary
    assert "domhand_completion_tokens" in summary
    assert "domhand_llm_calls" in summary
    assert "domhand_models" in summary
    # Stagehand fields
    assert "stagehand_used" in summary
    assert "stagehand_calls" in summary
    assert "stagehand_sources" in summary
    # Aggregation
    assert summary["total_tracked_cost_usd"] == 0.25
    # No old "tool_*" keys
    assert "tool_cost_usd" not in summary
    assert "tool_models" not in summary
