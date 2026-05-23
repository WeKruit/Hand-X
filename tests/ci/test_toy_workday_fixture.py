"""Smoke tests for ``examples/toy-workday/index.html``.

Locks in the strict Workday multiselect contract:
- ``selectinputicon``/``promptIcon`` must open the skill search panel
- typing alone is insufficient; ``Enter`` submits the query and reveals results
- blob queries like ``Python,Java,React`` return ``No Items.``
- each skill must commit as a token in ``selectedItemList``
- the input must clear after each commit instead of accumulating a blob
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("playwright.async_api")
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.profile import ViewportSize
from browser_use.tools.service import Tools
from ghosthands.actions.domhand_assess_state import domhand_assess_state
from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    _preferred_field_label,
    domhand_fill,
    extract_visible_form_fields,
)
from ghosthands.actions.views import DomHandAssessStateParams, DomHandFillParams, get_stable_field_key
from ghosthands.dom.fill_executor import _fill_select_field_outcome
from ghosthands.dom.page_visual_verifier import (
    VisualVerificationFieldOutcome,
    VisualVerificationMode,
)
from ghosthands.dom.shadow_helpers import ensure_helpers
from ghosthands.runtime_learning import record_expected_field_value, reset_runtime_learning_state

_FIXTURE = Path(__file__).resolve().parent.parent.parent / "examples" / "toy-workday" / "index.html"


@asynccontextmanager
async def managed_browser_session(*, viewport: ViewportSize | None = None):
    session = BrowserSession(
        browser_profile=BrowserProfile(
            headless=True,
            user_data_dir=None,
            keep_alive=True,
            enable_default_extensions=True,
            viewport=viewport,
            window_size=viewport,
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
            '() => document.querySelector(\'[data-automation-id="skillsSection"]\').scrollIntoView({block: "center"})'
        )
        await ensure_helpers(cast(Any, page))
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
            '() => document.querySelector(\'[data-automation-id="skillsSection"]\').scrollIntoView({block: "center"})'
        )
        await ensure_helpers(cast(Any, page))
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
            '() => document.querySelector(\'[data-automation-id="skillsSection"]\').scrollIntoView({block: "center"})'
        )
        await ensure_helpers(cast(Any, page))
        await page.evaluate(_build_inject_helpers_js())

        fields = await extract_visible_form_fields(page)
        skill_field = next(
            (
                field
                for field in fields
                if field.field_type == "select" and "skills" in _preferred_field_label(field).lower()
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
        visible_results_raw = await page.evaluate(
            """() => Array.from(
                document.querySelectorAll('[data-multiselect="skills"] .wd-popup [role="option"]')
            ).map((node) => node.textContent.trim()).filter(Boolean)"""
        )
        visible_results = (
            json.loads(visible_results_raw) if isinstance(visible_results_raw, str) else visible_results_raw
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
            '() => document.querySelector(\'[data-automation-id="skillsSection"]\').scrollIntoView({block: "center"})'
        )
        await ensure_helpers(cast(Any, page))
        await page.evaluate(_build_inject_helpers_js())

        fields = await extract_visible_form_fields(page)
        skill_field = next(
            (
                field
                for field in fields
                if field.field_type == "select" and "skills" in _preferred_field_label(field).lower()
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
            '() => document.querySelector(\'[data-automation-id="skillsSection"]\').scrollIntoView({block: "center"})'
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
            patch(
                "ghosthands.actions.domhand_fill._safe_page_url",
                AsyncMock(return_value="https://intel.wd1.myworkdayjobs.com/job/123"),
            ),
            patch(
                "ghosthands.actions.domhand_fill._get_page_context_key",
                AsyncMock(return_value="toy-workday-my-experience"),
            ),
            patch("ghosthands.actions.domhand_fill._stagehand_observe_cross_reference", AsyncMock(return_value=None)),
        ):
            result = await domhand_fill(
                DomHandFillParams(
                    target_section="My Experience",
                    heading_boundary=None,
                    focus_fields=["Type to Add Skills"],
                    entry_data=None,
                    use_auth_credentials=False,
                    strict_scope=False,
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
        visible_results_raw = await page.evaluate(
            """() => Array.from(
                document.querySelectorAll('[data-multiselect="skills"] .wd-popup [role="option"]')
            ).map((node) => node.textContent.trim()).filter(Boolean)"""
        )
        visible_results = (
            json.loads(visible_results_raw) if isinstance(visible_results_raw, str) else visible_results_raw
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
            '() => document.querySelector(\'[data-automation-id="skillsSection"]\').scrollIntoView({block: "center"})'
        )

        with (
            patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="profile text"),
            patch("ghosthands.actions.domhand_fill._get_profile_data", return_value={"skills": ["Python", "React"]}),
            patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
            patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
            patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
            patch("ghosthands.actions.domhand_fill._generate_answers", AsyncMock()) as generate_answers,
            patch(
                "ghosthands.actions.domhand_fill._safe_page_url",
                AsyncMock(return_value="https://intel.wd1.myworkdayjobs.com/job/123"),
            ),
            patch(
                "ghosthands.actions.domhand_fill._get_page_context_key",
                AsyncMock(return_value="toy-workday-my-experience"),
            ),
            patch("ghosthands.actions.domhand_fill._stagehand_observe_cross_reference", AsyncMock(return_value=None)),
        ):
            result = await domhand_fill(
                DomHandFillParams(
                    target_section="My Experience",
                    heading_boundary=None,
                    focus_fields=["Type to Add Skills"],
                    entry_data=None,
                    use_auth_credentials=False,
                    strict_scope=False,
                ),
                browser_session,
            )

        assert result.error is None, result
        generate_answers.assert_not_awaited()


@pytest.mark.asyncio
async def test_toy_workday_assess_state_scroll_batches_visual_verification(
    httpserver, toy_html: str, monkeypatch
) -> None:
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/index.html")
    host = "company.myworkdayjobs.com.lvh.me"
    page_context_key = "toy-workday-my-information-scroll-batch"
    short_viewport = ViewportSize(width=1280, height=420)
    visual_batches: list[list[str]] = []

    async def fake_invoke_visual_batch(
        *,
        llm,
        file_system,
        messages_prefix,
        browser_state,
        page_context_key,
        batch,
        screenshot_b64,
    ):
        del llm, file_system, messages_prefix, browser_state, page_context_key, screenshot_b64
        visual_batches.append([candidate.field_label for candidate in batch])
        outcomes = [
            VisualVerificationFieldOutcome(
                field_id=candidate.field_id,
                field_key=candidate.field_key,
                field_label=candidate.field_label,
                field_type=candidate.field_type,
                expected_value=candidate.expected_value,
                observed_value=(
                    "Filled"
                    if candidate.verification_mode is VisualVerificationMode.FILLEDNESS_ONLY
                    else candidate.expected_value
                ),
                required=candidate.required,
                trust_tier=candidate.trust_tier,
                verification_mode=candidate.verification_mode,
                matches_expected=True,
                confidence=0.99,
                status="verified",
            )
            for candidate in batch
        ]
        return outcomes, 1200, 180, 400

    reset_runtime_learning_state()
    try:
        async with managed_browser_session(viewport=short_viewport) as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None
            await page.evaluate(
                """() => {
                    showStep(0);
                    window.scrollTo(0, 0);

                    const selectDropdown = (key, value) => {
                      const button = document.querySelector(`.wd-dropdown[data-dropdown="${key}"] button`);
                      const popup = document.querySelector(`.wd-popup[data-for="${key}"]`);
                      if (!button || !popup) throw new Error(`Missing dropdown ${key}`);
                      commitDropdown(button, popup, value);
                    };

                    const selectPrompt = (inputId, value) => {
                      const input = document.getElementById(inputId);
                      if (!input) throw new Error(`Missing prompt input ${inputId}`);
                      const widget = input.closest('.wd-prompt');
                      if (!widget) throw new Error(`Missing prompt widget for ${inputId}`);
                      commitPromptSelection(widget, value);
                    };

                    const previousWorkerNo = document.querySelector('input[name="candidateIsPreviousWorker"][value="No"]');
                    if (!previousWorkerNo) throw new Error("Missing previous worker radio");
                    previousWorkerNo.checked = true;
                    previousWorkerNo.dispatchEvent(new Event("change", { bubbles: true }));

                    selectPrompt("source--source", "Company Website");
                    selectDropdown("country", "United States of America");
                    selectDropdown("state", "Virginia");
                    selectDropdown("phone-device", "Mobile");
                    selectPrompt("phone--countryPhoneCode", "United States of America (+1)");

                    document.getElementById("name--legalName--firstName").value = "Spencer";
                    document.getElementById("name--legalName--middleName").value = "Yi Chen";
                    document.getElementById("name--legalName--lastName").value = "Wang";
                    document.getElementById("address--addressLine1").value = "123 Main St";
                    document.getElementById("address--city").value = "Chantilly";
                    document.getElementById("address--postalCode").value = "20151";
                    document.getElementById("contactInformation--email").value = "spencer@example.com";
                    document.getElementById("phone--phoneNumber").value = "5717788080";
                    document.getElementById("phone--phoneExtension").value = "123";
                }"""
            )

            await ensure_helpers(cast(Any, page))
            await page.evaluate(_build_inject_helpers_js())
            fields = await extract_visible_form_fields(page)
            recorded_expected_field_count = 0
            for field in fields:
                current_value = str(field.current_value or "").strip()
                if not current_value or current_value == "\u00d7 \u2630":
                    continue
                recorded_expected_field_count += 1
                record_expected_field_value(
                    host=host,
                    page_context_key=page_context_key,
                    field_key=get_stable_field_key(field),
                    field_label=field.name,
                    field_type=field.field_type,
                    field_section=field.section or "",
                    field_fingerprint=field.field_fingerprint or "",
                    expected_value=current_value,
                    source="manual_recovery",
                )

            monkeypatch.setattr(
                "ghosthands.dom.page_visual_verifier.get_chat_model",
                lambda model: SimpleNamespace(model=model),
            )
            monkeypatch.setattr("ghosthands.dom.page_visual_verifier._invoke_visual_batch", fake_invoke_visual_batch)
            monkeypatch.setattr("ghosthands.dom.page_visual_verifier._VISUAL_SCROLL_SETTLE_SECONDS", 0.0)

            with (
                patch(
                    "ghosthands.actions.domhand_assess_state._safe_page_url",
                    AsyncMock(return_value=f"http://{host}/index.html"),
                ),
                patch(
                    "ghosthands.actions.domhand_assess_state._get_page_context_key",
                    AsyncMock(return_value=page_context_key),
                ),
            ):
                result = await domhand_assess_state(
                    DomHandAssessStateParams(target_section=None),
                    browser_session,
                )

            payload = json.loads((result.metadata or {})["application_state_json"])
            visual = payload["visual_verification"]
            assert visual["attempted"] is True
            assert visual["segment_count"] >= 2
            assert visual["calls"] >= visual["segment_count"]
            assert visual["candidate_count"] == recorded_expected_field_count
            assert len(visual_batches) == visual["calls"]
            assert payload["advance_allowed"] is False
            assert payload["unresolved_required_fields"] == [
                {
                    "field_id": "ff-20",
                    "name": "First Name* Middle Name Last Name*",
                    "field_type": "checkbox",
                    "section": "",
                    "section_path": "",
                    "required": True,
                    "reason": "required_missing_value",
                    "relative_position": "below",
                    "takeover_suggestion": "browser_use_takeover",
                    "question_text": "First Name* Middle Name Last Name*",
                    "current_value": "",
                    "visible_error": None,
                    "widget_kind": "checkbox",
                    "options": [],
                }
            ]
            assert any(any("First Name" in label for label in batch) for batch in visual_batches)
            assert any(any("Phone Number" in label for label in batch) for batch in visual_batches)
            final_scroll_y = await page.evaluate("() => window.scrollY")
            assert int(final_scroll_y) == 0
    finally:
        reset_runtime_learning_state()
