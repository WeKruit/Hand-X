"""CI tests for repeater pre-fill detection on the toy Workday fixture.

Validates that _observe_existing_entries correctly detects pre-filled
entries using field observation + anchor matching, and that section
scoping isolates education from experience.

  uv run pytest tests/ci/test_toy_repeater_prefill.py -v
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools

from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    extract_visible_form_fields,
)
from ghosthands.actions.domhand_fill_repeaters import (
    _observe_existing_entries,
    ObservationResult,
)
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


_PREFILL_EXPERIENCE_JS = """(entries) => {
    prefillWorkExperience(entries);
    return true;
}"""

_PREFILL_EDUCATION_JS = """(entries) => {
    prefillEducation(entries);
    return true;
}"""


# ---------------------------------------------------------------------------
# 1. Empty page — no pre-fill → observation finds 0 anchors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_workday_no_prefill(httpserver, toy_html: str) -> None:
    """On an empty Workday page, observation finds 0 anchor values."""
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None

        # Navigate to My Experience section
        await page.evaluate("() => showStep(1)")
        await asyncio.sleep(0.3)

        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())

        profile_entries = [
            {"company": "Google", "job_title": "SWE"},
            {"company": "NASA", "job_title": "Intern"},
        ]

        obs = await _observe_existing_entries(page, "experience", profile_entries)
        assert isinstance(obs, ObservationResult)
        assert obs.existing_count == 0
        assert len(obs.page_anchor_values) == 0
        assert len(obs.unmatched_entries) == 2


# ---------------------------------------------------------------------------
# 2. Pre-filled experience — observation detects companies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefilled_experience_detected(httpserver, toy_html: str) -> None:
    """Pre-filled Company fields are detected as page anchors."""
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None

        await page.evaluate("() => showStep(1)")
        await asyncio.sleep(0.3)

        # Pre-fill 2 work experience entries
        await page.evaluate(_PREFILL_EXPERIENCE_JS, [
            {"company": "WeKruit", "jobTitle": "Software Engineer"},
            {"company": "NASA JPL", "jobTitle": "Intern"},
        ])
        await asyncio.sleep(0.3)

        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())

        # Profile has 4 entries, 2 match page
        profile_entries = [
            {"company": "WeKruit", "job_title": "Software Engineer"},
            {"company": "NASA JPL", "job_title": "Intern"},
            {"company": "Google", "job_title": "SWE"},
            {"company": "SpaceX", "job_title": "Engineer"},
        ]

        obs = await _observe_existing_entries(page, "experience", profile_entries)
        assert obs.existing_count >= 2, f"Expected >=2 anchors, got {obs.existing_count}"
        assert len(obs.page_anchor_values) >= 2
        # Exact match: "wekruit" and "nasa jpl" should be in page anchors
        assert "wekruit" in obs.page_anchor_values
        assert "nasa jpl" in obs.page_anchor_values
        # 2 matched, 2 unmatched
        assert len(obs.matched_profile_indices) >= 2
        assert len(obs.unmatched_entries) <= 2


# ---------------------------------------------------------------------------
# 3. Section scoping — education observation doesn't see experience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_section_scoping_isolates_education(httpserver, toy_html: str) -> None:
    """Education observation should NOT see experience Company fields."""
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None

        await page.evaluate("() => showStep(1)")
        await asyncio.sleep(0.3)

        # Pre-fill work experience but NOT education
        await page.evaluate(_PREFILL_EXPERIENCE_JS, [
            {"company": "WeKruit", "jobTitle": "Software Engineer"},
        ])
        await asyncio.sleep(0.3)

        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())

        # Education observation should find 0 anchors (no school values)
        edu_entries = [{"school": "MIT", "degree": "BS CS"}]
        obs = await _observe_existing_entries(page, "education", edu_entries)
        assert obs.existing_count == 0, (
            f"Education should not see experience fields, got anchors: {obs.page_anchor_values}"
        )
        assert len(obs.unmatched_entries) == 1
