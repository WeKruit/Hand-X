"""Unit tests for Oracle school combobox LLM path (type → scan → pick → verify)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ghosthands.actions.views import FormField
from ghosthands.dom import fill_executor as fe
from ghosthands.dom.oracle_combobox_llm import (
    _completion_text,
    _oracle_school_openai_base_url,
    oracle_combobox_pick_option_llm,
    oracle_combobox_search_terms_llm,
    oracle_combobox_verify_commit_llm,
)


def test_oracle_school_openai_base_url_with_proxy() -> None:
    # GH_LLM_PROXY_URL is set by buildManagedAnthropicRuntime() to:
    # "{brokerBase}/api/v1/local-workers/inference"
    # The function should append "/openai/v1" (not "/inference/openai/v1")
    # so that the final path matches the VALET route: .../inference/openai/*
    with patch("ghosthands.dom.oracle_combobox_llm.settings") as s:
        s.llm_proxy_url = "https://api.example.com/api/v1/local-workers/inference"
        assert _oracle_school_openai_base_url() == (
            "https://api.example.com/api/v1/local-workers/inference/openai/v1"
        )


def test_is_oracle_school_llm_field_true_for_school() -> None:
    f = FormField(
        field_id="ff1",
        name="School *",
        field_type="select",
        oracle_freeform_combobox_answer=True,
    )
    assert fe._is_school_combobox_field(f) is True


def test_is_oracle_school_llm_field_false_for_major_with_same_flag() -> None:
    f = FormField(
        field_id="ff1",
        name="Major *",
        field_type="select",
        oracle_freeform_combobox_answer=True,
    )
    assert fe._is_school_combobox_field(f) is False


def test_is_oracle_school_llm_field_false_for_field_of_study_with_same_flag() -> None:
    """Triage may set freeform for field_of_study; LLM school path still excludes it."""
    f = FormField(
        field_id="ff1",
        name="Field of Study *",
        field_type="select",
        oracle_freeform_combobox_answer=True,
    )
    assert fe._is_school_combobox_field(f) is False


def test_is_oracle_school_llm_field_true_for_institution() -> None:
    f = FormField(
        field_id="ff1",
        name="Institution *",
        field_type="select",
        oracle_freeform_combobox_answer=True,
    )
    assert fe._is_school_combobox_field(f) is True


def test_is_oracle_school_llm_field_false_random_label_with_flag() -> None:
    f = FormField(
        field_id="ff1",
        name="Employer *",
        field_type="select",
        oracle_freeform_combobox_answer=True,
    )
    assert fe._is_school_combobox_field(f) is False


def test_is_school_combobox_field_true_without_flag() -> None:
    """_is_school_combobox_field no longer depends on oracle_freeform_combobox_answer."""
    f = FormField(field_id="ff1", name="School *", field_type="select")
    assert fe._is_school_combobox_field(f) is True


def test_merge_unique_terms_order_and_dedupe() -> None:
    a = fe._merge_unique_terms(["B", "a"], ["a", "C"])
    assert a == ["B", "a", "C"]


def test_oracle_combobox_options_raw_to_labels() -> None:
    raw = json.dumps(
        [
            {"text": "  UCLA  ", "dataValue": ""},
            {"text": "", "dataValue": "USC"},
        ]
    )
    assert fe._oracle_combobox_options_raw_to_labels(raw) == ["UCLA", "USC"]


@pytest.mark.asyncio
async def test_search_terms_llm_empty_when_disabled() -> None:
    with patch("ghosthands.dom.oracle_combobox_llm._oracle_school_llm_disabled", return_value=True):
        terms = await oracle_combobox_search_terms_llm("Some University")
    assert terms == []


@pytest.mark.asyncio
async def test_completion_text_builds_user_message_with_content() -> None:
    fake_llm = MagicMock()
    fake_llm.ainvoke = AsyncMock(return_value=SimpleNamespace(completion="pong"))

    with patch("ghosthands.dom.oracle_combobox_llm._oracle_school_chat_openai", return_value=fake_llm):
        text = await _completion_text("hello world", max_tokens=32)

    assert text == "pong"
    sent_messages = fake_llm.ainvoke.await_args.args[0]
    assert len(sent_messages) == 1
    assert sent_messages[0].content == "hello world"


@pytest.mark.asyncio
async def test_search_terms_llm_parses_json() -> None:
    with (
        patch(
            "ghosthands.dom.oracle_combobox_llm._completion_text",
            new=AsyncMock(return_value='{"terms": ["University of California", "Los Angeles"]}'),
        ),
        patch("ghosthands.dom.oracle_combobox_llm._oracle_school_llm_disabled", return_value=False),
    ):
        terms = await oracle_combobox_search_terms_llm("University of California, Los Angeles")
    assert terms == ["University of California", "Los Angeles"]


@pytest.mark.asyncio
async def test_pick_option_llm_returns_index() -> None:
    opts = ["A", "B", "UCLA — Los Angeles", "D"]
    with (
        patch(
            "ghosthands.dom.oracle_combobox_llm._completion_text",
            new=AsyncMock(return_value='{"matched_index": 2}'),
        ),
        patch("ghosthands.dom.oracle_combobox_llm._oracle_school_llm_disabled", return_value=False),
    ):
        idx = await oracle_combobox_pick_option_llm(
            "University of California, Los Angeles",
            opts,
            "University of California",
        )
    assert idx == 2


@pytest.mark.asyncio
async def test_pick_option_llm_oob_index_returns_none() -> None:
    opts = ["A", "B"]
    with (
        patch(
            "ghosthands.dom.oracle_combobox_llm._completion_text",
            new=AsyncMock(return_value='{"matched_index": 99}'),
        ),
        patch("ghosthands.dom.oracle_combobox_llm._oracle_school_llm_disabled", return_value=False),
    ):
        idx = await oracle_combobox_pick_option_llm("UCLA", opts, "UCLA")
    assert idx is None


@pytest.mark.asyncio
async def test_pick_option_llm_none_when_no_match() -> None:
    with (
        patch(
            "ghosthands.dom.oracle_combobox_llm._completion_text",
            new=AsyncMock(return_value='{"matched_index": null}'),
        ),
        patch("ghosthands.dom.oracle_combobox_llm._oracle_school_llm_disabled", return_value=False),
    ):
        idx = await oracle_combobox_pick_option_llm("Stanford", ["9 Eylul University"], "Stanford")
    assert idx is None


@pytest.mark.asyncio
async def test_fill_oracle_school_combobox_llm_success_mocked_page() -> None:
    field = FormField(
        field_id="ff-school",
        name="School *",
        field_type="select",
        oracle_freeform_combobox_answer=True,
    )
    page = MagicMock()
    # dismiss, focus_clear, list_options, click_index, settle dismiss, read_value
    _eval_queue = [
        "{}",
        "{}",
        json.dumps(
            [
                {"text": "9 Eylul University", "dataValue": ""},
                {"text": "UCLA", "dataValue": ""},
            ]
        ),
        json.dumps({"clicked": True, "text": "UCLA"}),
        "{}",
        json.dumps({"value": "UCLA", "committed": "UCLA"}),
    ]
    qi = {"i": 0}

    async def eval_side_effect(_script: str, *_args: object) -> str:
        i = qi["i"]
        qi["i"] += 1
        return _eval_queue[i] if i < len(_eval_queue) else "{}"

    page.evaluate = AsyncMock(side_effect=eval_side_effect)

    async def no_validation(_page: object, _fid: str) -> bool:
        return False

    with (
        patch(
            "ghosthands.dom.fill_executor.oracle_combobox_search_terms_llm",
            new=AsyncMock(return_value=["University of California"]),
        ),
        patch(
            "ghosthands.dom.fill_executor.oracle_combobox_pick_option_llm",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "ghosthands.dom.fill_executor.oracle_combobox_verify_commit_llm",
            new=AsyncMock(return_value=True),
        ),
        patch("ghosthands.dom.fill_executor._type_text_compat", new=AsyncMock()),
        patch("ghosthands.dom.fill_executor._field_has_validation_error", new=no_validation),
    ):
        out = await fe._fill_oracle_school_combobox_llm_outcome(
            page, field, "University of California, Los Angeles", "[School]"
        )

    assert out.success is True
    assert out.matched_label


@pytest.mark.asyncio
async def test_fill_oracle_school_rejects_failed_verify() -> None:
    field = FormField(
        field_id="ff-school",
        name="School *",
        field_type="select",
        oracle_freeform_combobox_answer=True,
    )
    page = MagicMock()
    calls: list[str] = []

    async def eval_side_effect(script: str, *_args: object) -> str:
        s = str(script)
        if "out.push" in s and "max = 30" in s:
            calls.append("list")
            return json.dumps([{"text": "Wrong U", "dataValue": ""}])
        if "row.click()" in s or "bad_index" in s:
            calls.append("click")
            return json.dumps({"clicked": True, "text": "Wrong U"})
        if "el.dataset" in s and "committedValue" in s:
            return json.dumps({"value": "Wrong U", "committed": "Wrong U"})
        return "{}"

    page.evaluate = AsyncMock(side_effect=eval_side_effect)

    async def no_validation(_page: object, _fid: str) -> bool:
        return False

    with (
        patch(
            "ghosthands.dom.fill_executor.oracle_combobox_search_terms_llm",
            new=AsyncMock(return_value=["only_term"]),
        ),
        patch(
            "ghosthands.dom.fill_executor.oracle_combobox_pick_option_llm",
            new=AsyncMock(return_value=0),
        ),
        patch(
            "ghosthands.dom.fill_executor.oracle_combobox_verify_commit_llm",
            new=AsyncMock(return_value=False),
        ),
        patch("ghosthands.dom.fill_executor._type_text_compat", new=AsyncMock()),
        patch("ghosthands.dom.fill_executor._field_has_validation_error", new=no_validation),
        patch("ghosthands.dom.fill_executor._oracle_combobox_search_terms", return_value=[]),
    ):
        out = await fe._fill_oracle_school_combobox_llm_outcome(page, field, "UCLA", "[School]")

    assert out.success is False
    assert "click" in calls


@pytest.mark.asyncio
async def test_verify_commit_llm_true_when_committed_matches_picked() -> None:
    """Fast path: no LLM when committed text equals picked row label."""
    with patch(
        "ghosthands.dom.oracle_combobox_llm._completion_text",
        new=AsyncMock(side_effect=AssertionError("_completion_text must not be called")),
    ):
        ok = await oracle_combobox_verify_commit_llm("UCLA", "UCLA", "UCLA")
    assert ok is True


@pytest.mark.asyncio
async def test_verify_commit_llm_true() -> None:
    with (
        patch(
            "ghosthands.dom.oracle_combobox_llm._completion_text",
            new=AsyncMock(return_value='{"same_institution": true}'),
        ),
        patch("ghosthands.dom.oracle_combobox_llm._oracle_school_llm_disabled", return_value=False),
    ):
        ok = await oracle_combobox_verify_commit_llm("UCLA", "University of California Los Angeles", "UCLA")
    assert ok is True


@pytest.mark.asyncio
async def test_verify_commit_llm_false_from_model() -> None:
    # Empty picked skips the committed==picked short-circuit so the LLM path runs.
    with (
        patch(
            "ghosthands.dom.oracle_combobox_llm._completion_text",
            new=AsyncMock(return_value='{"same_institution": false}'),
        ),
        patch("ghosthands.dom.oracle_combobox_llm._oracle_school_llm_disabled", return_value=False),
    ):
        ok = await oracle_combobox_verify_commit_llm("UCLA", "Wrong U", "")
    assert ok is False
