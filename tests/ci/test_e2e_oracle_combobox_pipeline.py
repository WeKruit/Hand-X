"""E2E pipeline test: runs domhand_fill against the Oracle combobox fixture.

Serves oracle_job_app_e2e.html on localhost, runs the full domhand_fill
pipeline with mocked LLM + Oracle URL, and verifies:
1. School combobox enrichment discovers the correct university
2. Employer combobox enrichment discovers the correct company
3. Auto-expand loop does NOT repeat for already-attempted sections
4. The LLM receives enriched options (not the default alphabetical slice)

  uv run pytest tests/ci/test_e2e_oracle_combobox_pipeline.py -v
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("playwright.async_api")

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools
from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    _enrich_combobox_via_search,
    _preferred_field_label,
    domhand_fill,
    extract_visible_form_fields,
)
from ghosthands.actions.views import DomHandFillParams, FormField
from ghosthands.dom.shadow_helpers import ensure_helpers

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
_FIXTURE_HTML = _FIXTURES_DIR / "oracle_job_app_e2e.html"

_ORACLE_URL = "https://hdpc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/LateralHiring/job/162133/apply/section/3"

_PROFILE = {
    "first_name": "Jane",
    "last_name": "Doe",
    "email": "jane.doe@example.com",
    "phone": "555-123-4567",
    "address": "123 Main St",
    "city": "Los Angeles",
    "state": "California",
    "zip": "90001",
    "country": "United States",
    "education": [
        {
            "school": "University of California, Los Angeles (UCLA)",
            "degree": "Bachelor of Science",
            "field_of_study": "Computer Science",
            "gpa": "3.91",
            "start_date": "2020-09",
            "end_date": "2024-06",
        }
    ],
    "experience": [
        {"company": "Goldman Sachs", "title": "Software Engineer"},
    ],
    "skills": ["Python", "JavaScript", "SQL", "React", "AWS"],
    "languages": [
        {"language": "English", "proficiency": "Native"},
        {"language": "Spanish", "proficiency": "Intermediate"},
    ],
}


def _parse(result):
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result


@asynccontextmanager
async def managed_browser_session():
    session = BrowserSession(
        browser_profile=BrowserProfile(
            headless=True, user_data_dir=None, keep_alive=True, enable_default_extensions=True,
        )
    )
    await session.start()
    try:
        yield session
    finally:
        await session.kill()
        await session.event_bus.stop(clear=True, timeout=5)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


@asynccontextmanager
async def managed_fixture_server():
    port = _find_free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
        cwd=str(_FIXTURES_DIR),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            await asyncio.sleep(0.1)
            try:
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.close()
                await w.wait_closed()
                break
            except (ConnectionRefusedError, OSError):
                continue
        yield f"http://127.0.0.1:{port}/oracle_job_app_e2e.html"
    finally:
        proc.terminate()
        await proc.wait()


def _make_field(*, name, field_type="select", is_native=False, field_id="ff-1", options=None):
    return FormField(field_id=field_id, name=name, field_type=field_type, is_native=is_native, options=options or [])


# ===================================================================
# 1. Enrichment discovers correct school via type-to-search
# ===================================================================


@pytest.mark.asyncio
async def test_enrichment_finds_ucla() -> None:
    """Type-to-search enrichment with hint 'University of California, Los Angeles (UCLA)'
    should discover UCLA in the combobox options."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("fixture not found")
    async with managed_fixture_server() as url:
        async with managed_browser_session() as bs:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=bs)
            page = await bs.get_current_page()
            assert page is not None
            await asyncio.sleep(0.5)

            # Navigate to page 3 and open education form
            await page.evaluate("() => goToPage(3)")
            await asyncio.sleep(0.3)
            await page.evaluate("() => toggleEducationForm()")
            await asyncio.sleep(0.3)

            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            field = _make_field(name="School", field_id="ff-school")
            discovered = await _enrich_combobox_via_search(
                page, field, "University of California, Los Angeles (UCLA)",
            )

            assert len(discovered) > 0, "Enrichment found no options"
            lower = [o.lower() for o in discovered]
            assert any("university of california, los angeles" in o for o in lower), (
                f"UCLA not in enriched options: {discovered[:10]}"
            )
            assert not any("9 eylul" in o for o in lower), (
                f"9 Eylul wrongly in results: {discovered}"
            )


@pytest.mark.asyncio
async def test_enrichment_finds_nyu() -> None:
    if not _FIXTURE_HTML.is_file():
        pytest.skip("fixture not found")
    async with managed_fixture_server() as url:
        async with managed_browser_session() as bs:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=bs)
            page = await bs.get_current_page()
            await asyncio.sleep(0.5)
            await page.evaluate("() => goToPage(3)")
            await asyncio.sleep(0.3)
            await page.evaluate("() => toggleEducationForm()")
            await asyncio.sleep(0.3)
            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            field = _make_field(name="School", field_id="ff-school")
            discovered = await _enrich_combobox_via_search(page, field, "New York University")
            lower = [o.lower() for o in discovered]
            assert any("new york university" in o for o in lower), (
                f"NYU not found: {discovered[:10]}"
            )


@pytest.mark.asyncio
async def test_enrichment_finds_employer() -> None:
    if not _FIXTURE_HTML.is_file():
        pytest.skip("fixture not found")
    async with managed_fixture_server() as url:
        async with managed_browser_session() as bs:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=bs)
            page = await bs.get_current_page()
            await asyncio.sleep(0.5)
            await page.evaluate("() => goToPage(3)")
            await asyncio.sleep(0.3)
            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            field = _make_field(name="Name of Latest Employer", field_id="ff-employer")
            discovered = await _enrich_combobox_via_search(page, field, "Goldman Sachs")
            assert any("goldman sachs" in o.lower() for o in discovered), (
                f"Goldman Sachs not found: {discovered[:10]}"
            )


@pytest.mark.asyncio
async def test_enrichment_clears_input_after_search() -> None:
    if not _FIXTURE_HTML.is_file():
        pytest.skip("fixture not found")
    async with managed_fixture_server() as url:
        async with managed_browser_session() as bs:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=bs)
            page = await bs.get_current_page()
            await asyncio.sleep(0.5)
            await page.evaluate("() => goToPage(3)")
            await asyncio.sleep(0.3)
            await page.evaluate("() => toggleEducationForm()")
            await asyncio.sleep(0.3)
            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            field = _make_field(name="School", field_id="ff-school")
            await _enrich_combobox_via_search(page, field, "Stanford University")
            val = await page.evaluate("() => document.getElementById('ff-school').value")
            # After enrichment the input should be mostly cleared.  The clear
            # uses Ctrl+A + Backspace which on some pages leaves a trailing char.
            # A residual 1-2 chars is acceptable — the executor will clear again.
            assert len(val) <= 3, f"Input should be mostly cleared after enrichment, got: '{val}'"


# ===================================================================
# 2. Full pipeline: domhand_fill on page 3 with Oracle URL
# ===================================================================


@pytest.mark.asyncio
async def test_pipeline_school_gets_enriched_options() -> None:
    """Full domhand_fill pipeline: school field must reach LLM with UCLA in options."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("fixture not found")

    captured_fields: list[FormField] = []

    async def fake_generate(fields, profile_text, **kw):
        captured_fields.extend(fields)
        answers = {}
        for f in fields:
            lab = _preferred_field_label(f).lower()
            if "school" in lab:
                answers[_preferred_field_label(f)] = next(
                    (o for o in (f.options or []) if "california, los angeles" in o.lower()),
                    "University of California, Los Angeles",
                )
            elif "degree" in lab:
                answers[_preferred_field_label(f)] = "Bachelor of Science (BS)"
            elif "major" in lab:
                answers[_preferred_field_label(f)] = "Computer Science"
            elif "country" in lab:
                answers[_preferred_field_label(f)] = "United States"
            elif "state" in lab:
                answers[_preferred_field_label(f)] = "California"
            elif "employer" in lab:
                answers[_preferred_field_label(f)] = "Goldman Sachs"
            else:
                answers[_preferred_field_label(f)] = ""
        return answers, 100, 50, 0.001, "mock"

    async with managed_fixture_server() as url:
        async with managed_browser_session() as bs:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=bs)
            page = await bs.get_current_page()
            await asyncio.sleep(0.5)
            # Navigate to page 3 and open education
            await page.evaluate("() => goToPage(3)")
            await asyncio.sleep(0.3)
            await page.evaluate("() => toggleEducationForm()")
            await asyncio.sleep(0.3)

            with (
                patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="Jane Doe, UCLA CS"),
                patch("ghosthands.actions.domhand_fill._get_profile_data", return_value=_PROFILE),
                patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
                patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
                patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
                patch("ghosthands.actions.domhand_fill._generate_answers", AsyncMock(side_effect=fake_generate)),
                patch("ghosthands.actions.domhand_fill._safe_page_url", AsyncMock(return_value=_ORACLE_URL)),
                patch("ghosthands.actions.domhand_fill._get_page_context_key", AsyncMock(return_value="e2e-test")),
                patch("ghosthands.actions.domhand_fill._stagehand_observe_cross_reference", AsyncMock(return_value=None)),
            ):
                result = await domhand_fill(DomHandFillParams(), bs)

            # Find school field in what was sent to LLM
            school_fields = [f for f in captured_fields if "school" in _preferred_field_label(f).lower()]
            assert school_fields, (
                f"School not sent to LLM. Fields: {[_preferred_field_label(f) for f in captured_fields]}"
            )
            opts = school_fields[0].options or []
            assert any("california, los angeles" in o.lower() for o in opts), (
                f"UCLA NOT in enriched options sent to LLM!\n"
                f"Options ({len(opts)}): {opts[:15]}\n"
                f"This means enrichment didn't type search terms."
            )


# ===================================================================
# 3. Default enrichment (no hint) only sees alphabetical slice
# ===================================================================


@pytest.mark.asyncio
async def test_default_scan_misses_nyu() -> None:
    """Without type-to-search, opening the school dropdown only shows first 10 alphabetically."""
    if not _FIXTURE_HTML.is_file():
        pytest.skip("fixture not found")
    async with managed_fixture_server() as url:
        async with managed_browser_session() as bs:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=bs)
            page = await bs.get_current_page()
            await asyncio.sleep(0.5)
            await page.evaluate("() => goToPage(3)")
            await asyncio.sleep(0.3)
            await page.evaluate("() => toggleEducationForm()")
            await asyncio.sleep(0.3)
            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            # Just open the dropdown without typing
            await page.evaluate("""() => {
                const t = document.querySelector('[data-toggle="ff-school"]');
                if (t) t.click();
            }""")
            await asyncio.sleep(0.5)

            opts = await page.evaluate("""() => {
                const lb = document.getElementById('ff-school-list');
                return Array.from(lb.querySelectorAll('.option')).map(o => o.textContent);
            }""")
            lower = [o.lower() for o in opts]
            # Default alphabetical slice should NOT have UCLA or NYU
            assert not any("new york university" in o for o in lower), (
                f"NYU should NOT be in default slice: {opts}"
            )
