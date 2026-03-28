"""Smoke tests for ``examples/toy-workday/index.html``.

Locks in the strict Workday multiselect contract:
- ``selectinputicon``/``promptIcon`` must open the skill search panel
- typing alone is insufficient; ``Enter`` submits the query and reveals results
- blob queries like ``Python,Java,React`` return ``No Items.``
- each skill must commit as a token in ``selectedItemList``
- the input must clear after each commit instead of accumulating a blob
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("playwright.async_api")
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools

from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    _preferred_field_label,
    domhand_fill,
    extract_visible_form_fields,
)
from ghosthands.actions.views import DomHandFillParams
from ghosthands.dom.fill_executor import _fill_select_field_outcome
from ghosthands.dom.shadow_helpers import ensure_helpers

_FIXTURE = (
    Path(__file__).resolve().parent.parent.parent
    / "examples"
    / "toy-workday"
    / "index.html"
)


@asynccontextmanager
async def managed_browser_session():
    session = BrowserSession(
        browser_profile=BrowserProfile(
            headless=True,
            user_data_dir=None,
            keep_alive=True,
            enable_default_extensions=True,
        )
    )
    await session.start()
    try:
        yield session
    finally:
        await session.kill()
        await session.event_bus.stop(clear=True, timeout=5)


@pytest.fixture
def toy_html() -> str:
    assert _FIXTURE.is_file(), f"missing toy fixture: {_FIXTURE}"
    return _FIXTURE.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_toy_workday_skills_require_enter_before_live_results(httpserver, toy_html: str) -> None:
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await page.evaluate("() => showStep(1)")
        await page.evaluate(
            "() => document.querySelector('[data-automation-id=\"skillsSection\"]').scrollIntoView({block: \"center\"})"
        )
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())
        closed_popup_text = await page.evaluate(
            """() => document.querySelector('[data-multiselect="skills"] .wd-popup')?.textContent || ''"""
        )
        assert closed_popup_text == ""

        await page.evaluate(
            """() => {
                const button = document.querySelector('[data-multiselect="skills"] [data-uxi-widget-type="selectinputicon"]');
                if (!button) return;
                button.click();
            }"""
        )
        await page.evaluate(
            """() => {
                const input = document.querySelector('#skills--skills');
                if (!input) return;
                input.focus();
                input.value = 'Python';
                input.dispatchEvent(new Event('input', { bubbles: true }));
            }"""
        )

        before_enter_raw = await page.evaluate(
            """() => Array.from(
                document.querySelectorAll('[data-multiselect="skills"] .wd-popup [role="option"]')
            ).map((node) => node.textContent.trim())"""
        )
        before_enter = json.loads(before_enter_raw) if isinstance(before_enter_raw, str) else before_enter_raw
        assert before_enter == []

        await page.evaluate(
            """() => {
                const input = document.querySelector('#skills--skills');
                if (!input) return;
                input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
            }"""
        )

        results_raw = await page.evaluate(
            """() => Array.from(
                document.querySelectorAll('[data-multiselect="skills"] .wd-popup [role="option"]')
            ).map((node) => node.textContent.trim())"""
        )
        results = json.loads(results_raw) if isinstance(results_raw, str) else results_raw
        assert "Python" in results, results


@pytest.mark.asyncio
async def test_toy_workday_skills_reject_blob_query(httpserver, toy_html: str) -> None:
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await page.evaluate("() => showStep(1)")
        await page.evaluate(
            "() => document.querySelector('[data-automation-id=\"skillsSection\"]').scrollIntoView({block: \"center\"})"
        )
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())
        await page.evaluate(
            """() => {
                const button = document.querySelector('[data-multiselect="skills"] [data-uxi-widget-type="selectinputicon"]');
                const input = document.querySelector('#skills--skills');
                if (!button || !input) return;
                button.click();
                input.focus();
                input.value = 'Python,Java,React';
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
            }"""
        )

        results_raw = await page.evaluate(
            """() => Array.from(
                document.querySelectorAll('[data-multiselect="skills"] .wd-popup [role="option"]')
            ).map((node) => node.textContent.trim())"""
        )
        results = json.loads(results_raw) if isinstance(results_raw, str) else results_raw
        assert results == ["No Items."]


@pytest.mark.asyncio
async def test_toy_workday_skills_commit_selected_tokens(httpserver, toy_html: str) -> None:
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await page.evaluate("() => showStep(1)")
        await page.evaluate(
            "() => document.querySelector('[data-automation-id=\"skillsSection\"]').scrollIntoView({block: \"center\"})"
        )
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())

        fields = await extract_visible_form_fields(page)
        skill_field = next(
            (
                field
                for field in fields
                if field.field_type == "select"
                and "skills" in _preferred_field_label(field).lower()
            ),
            None,
        )
        assert skill_field is not None, [(_preferred_field_label(field), field.field_type) for field in fields]

        result = await _fill_select_field_outcome(
            page,
            skill_field,
            "Python, React",
            "[Workday Skills Toy]",
            browser_session=browser_session,
        )

        assert result.success is True, result
        assert await page.evaluate("() => document.getElementById('skills--skills').value") == ""
        selected_titles = await page.evaluate(
            """() => Array.from(
                document.querySelectorAll('[data-multiselect="skills"] [data-automation-id="selectedItem"]')
            ).map((node) => node.getAttribute('title') || node.textContent.trim())"""
        )
        assert "Python" in selected_titles, selected_titles
        assert "React" in selected_titles, selected_titles
        visible_results = await page.evaluate(
            """() => Array.from(
                document.querySelectorAll('[data-multiselect="skills"] .wd-popup [role="option"]')
            ).map((node) => node.textContent.trim()).filter(Boolean)"""
        )
        assert visible_results == [], visible_results


@pytest.mark.asyncio
async def test_toy_workday_skills_do_not_substitute_similar_option_text(httpserver, toy_html: str) -> None:
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await page.evaluate("() => showStep(1)")
        await page.evaluate(
            "() => document.querySelector('[data-automation-id=\"skillsSection\"]').scrollIntoView({block: \"center\"})"
        )
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())

        fields = await extract_visible_form_fields(page)
        skill_field = next(
            (
                field
                for field in fields
                if field.field_type == "select"
                and "skills" in _preferred_field_label(field).lower()
            ),
            None,
        )
        assert skill_field is not None, [(_preferred_field_label(field), field.field_type) for field in fields]

        result = await _fill_select_field_outcome(
            page,
            skill_field,
            "Supabase, React",
            "[Workday Skills Toy]",
            browser_session=browser_session,
        )

        assert result.success is True, result
        selected_titles = await page.evaluate(
            """() => Array.from(
                document.querySelectorAll('[data-multiselect="skills"] [data-automation-id="selectedItem"]')
            ).map((node) => node.getAttribute('title') || node.textContent.trim())"""
        )
        assert "React" in selected_titles, selected_titles
        assert "Sybase Unwired Platform (SUP)" not in selected_titles, selected_titles


@pytest.mark.asyncio
async def test_toy_workday_domhand_fill_commits_skills_widget(httpserver, toy_html: str) -> None:
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await page.evaluate("() => showStep(1)")
        await page.evaluate(
            "() => document.querySelector('[data-automation-id=\"skillsSection\"]').scrollIntoView({block: \"center\"})"
        )

        async def fake_generate(fields, *_args, **_kwargs):
            answers = {}
            for field in fields:
                label = _preferred_field_label(field)
                if "skills" in label.lower():
                    answers[label] = "Python, React"
            return answers, 10, 4, 0.001, "test-llm"

        with (
            patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="profile text"),
            patch("ghosthands.actions.domhand_fill._get_profile_data", return_value={"skills": ["Python", "React"]}),
            patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
            patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
            patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
            patch("ghosthands.actions.domhand_fill._generate_answers", AsyncMock(side_effect=fake_generate)),
            patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://intel.wd1.myworkdayjobs.com/job/123")),
            patch("ghosthands.actions.domhand_fill._get_page_context_key", AsyncMock(return_value="toy-workday-my-experience")),
            patch("ghosthands.actions.domhand_fill._stagehand_observe_cross_reference", AsyncMock(return_value=None)),
        ):
            result = await domhand_fill(
                DomHandFillParams(
                    target_section="My Experience",
                    focus_fields=["Type to Add Skills"],
                ),
                browser_session,
            )

        assert result.error is None, result
        selected_titles = await page.evaluate(
            """() => Array.from(
                document.querySelectorAll('[data-multiselect="skills"] [data-automation-id="selectedItem"]')
            ).map((node) => node.getAttribute('title') || node.textContent.trim())"""
        )
        assert "Python" in selected_titles, selected_titles
        assert "React" in selected_titles, selected_titles
        visible_results = await page.evaluate(
            """() => Array.from(
                document.querySelectorAll('[data-multiselect="skills"] .wd-popup [role="option"]')
            ).map((node) => node.textContent.trim()).filter(Boolean)"""
        )
        assert visible_results == [], visible_results


@pytest.mark.asyncio
async def test_toy_workday_domhand_fill_uses_profile_skills_without_llm(httpserver, toy_html: str) -> None:
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await page.evaluate("() => showStep(1)")
        await page.evaluate(
            "() => document.querySelector('[data-automation-id=\"skillsSection\"]').scrollIntoView({block: \"center\"})"
        )

        with (
            patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="profile text"),
            patch("ghosthands.actions.domhand_fill._get_profile_data", return_value={"skills": ["Python", "React"]}),
            patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
            patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
            patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
            patch("ghosthands.actions.domhand_fill._generate_answers", AsyncMock()) as generate_answers,
            patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value="https://intel.wd1.myworkdayjobs.com/job/123")),
            patch("ghosthands.actions.domhand_fill._get_page_context_key", AsyncMock(return_value="toy-workday-my-experience")),
            patch("ghosthands.actions.domhand_fill._stagehand_observe_cross_reference", AsyncMock(return_value=None)),
        ):
            result = await domhand_fill(
                DomHandFillParams(
                    target_section="My Experience",
                    focus_fields=["Type to Add Skills"],
                ),
                browser_session,
            )

        assert result.error is None, result
        generate_answers.assert_not_awaited()
