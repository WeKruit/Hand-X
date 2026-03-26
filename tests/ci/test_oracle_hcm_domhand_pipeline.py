"""DomHand PIPELINE tests against Oracle HCM fixture.

Unlike test_toy_oracle_hcm_fixture.py (which tests fixture JS behavior),
these tests run DomHand's actual fill pipeline — extract_visible_form_fields,
_fill_select_field_outcome, _fill_text_field — against Oracle Cloud HCM patterns.

Expected: some will FAIL, exposing real DomHand bugs on Oracle.

  uv run pytest tests/ci/test_oracle_hcm_domhand_pipeline.py -v
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("playwright.async_api")
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools
from ghosthands.actions.domhand_assess_state import domhand_assess_state
from ghosthands.actions.domhand_fill import domhand_fill
from ghosthands.actions.views import DomHandAssessStateParams, DomHandFillParams
from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    _preferred_field_label,
    extract_visible_form_fields,
)
from ghosthands.dom.fill_executor import _fill_select_field_outcome
from ghosthands.dom.shadow_helpers import ensure_helpers

_FIXTURE = (
    Path(__file__).resolve().parent.parent.parent
    / "examples"
    / "toy-oracle-hcm"
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


def _find_free_port() -> int:
    """Reserve a free localhost TCP port for a real fixture server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


@asynccontextmanager
async def managed_local_fixture_server():
    """Serve the toy Oracle fixture through a real localhost HTTP server."""
    port = _find_free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "http.server",
        str(port),
        "--bind",
        "127.0.0.1",
        cwd=str(_FIXTURE.parent),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"localhost fixture server failed to start on port {port}")

        yield f"http://127.0.0.1:{port}/index.html"
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


def _find_field(fields, label_substr: str, field_type: str | None = None):
    """Find a field by label substring and optional type."""
    for f in fields:
        label = _preferred_field_label(f).lower()
        if label_substr.lower() in label:
            if field_type is None or f.field_type == field_type:
                return f
    return None


@pytest.fixture
def toy_html() -> str:
    assert _FIXTURE.is_file(), f"missing toy fixture: {_FIXTURE}"
    return _FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Field classification — are Oracle pill groups detected with options?
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pills_field_has_options(httpserver, toy_html: str) -> None:
    """cx-select-pills 'Prefix' should be extracted with discoverable options.

    EXPECTED TO FAIL: DomHand detects pills as button-group but opts=0.
    This means _fill_select_field_outcome cannot select a value.
    """
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())
        fields = await extract_visible_form_fields(page)

        prefix = _find_field(fields, "prefix")
        assert prefix is not None, f"Prefix field not found. Fields: {[_preferred_field_label(f) for f in fields]}"

        # The real test: does DomHand see the pill options (Dr, Miss, Mr, Mrs, Ms)?
        opts = prefix.options if hasattr(prefix, "options") and prefix.options else []
        assert len(opts) >= 5, (
            f"Prefix field type={prefix.field_type} has {len(opts)} options, "
            f"expected >=5 (Dr/Miss/Mr/Mrs/Ms). "
            f"DomHand cannot fill this field without knowing the options."
        )


# ---------------------------------------------------------------------------
# 2. DomHand fill pipeline — fill a cx-select combobox (Country)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domhand_fill_country_combobox(httpserver, toy_html: str) -> None:
    """DomHand should fill Country combobox by opening dropdown and selecting gridcell.

    Uses _fill_select_field_outcome — the actual code path for live Oracle forms.
    """
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())
        fields = await extract_visible_form_fields(page)

        country = _find_field(fields, "country", field_type="select")
        assert country is not None, f"Country select not found. Fields: {[(f.field_type, _preferred_field_label(f)) for f in fields]}"

        result = await _fill_select_field_outcome(
            page,
            country,
            "United States",
            "[Country]",
            browser_session=browser_session,
        )
        assert result.success, f"_fill_select_field_outcome failed: {result}"

        # Verify the value stuck
        val = await page.evaluate("() => document.getElementById('country-input')?.value || ''")
        assert "United States" in str(val), f"Country value not set, got: {val!r}"


# ---------------------------------------------------------------------------
# 3. DomHand fill pipeline — fill Address Line 1 + verify dependent backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domhand_fill_address_line1_with_backfill(httpserver, toy_html: str) -> None:
    """DomHand should fill Address Line 1, commit a gridcell, and backfill ZIP/City/State.

    This is the exact pattern that fails on live Goldman Sachs Oracle HCM.
    """
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())
        fields = await extract_visible_form_fields(page)

        addr = _find_field(fields, "address line 1", field_type="select")
        assert addr is not None, "Address Line 1 select not found"

        result = await _fill_select_field_outcome(
            page,
            addr,
            "123 Main St, New York, NY 10001",
            "[Address Line 1]",
            browser_session=browser_session,
        )
        assert result.success, f"Address Line 1 fill failed: {result}"

        # Check dependent fields got backfilled
        await asyncio.sleep(0.3)
        zip_val = await page.evaluate("() => (document.getElementById('zip-input') || {}).value || ''")
        city_val = await page.evaluate("() => (document.getElementById('city-input') || {}).value || ''")

        assert zip_val != "", f"ZIP should be backfilled after address commit, got empty"
        assert city_val != "", f"City should be backfilled after address commit, got empty"


# ---------------------------------------------------------------------------
# 4. DomHand fill pipeline — fill pills field (Prefix = "Mr")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domhand_fill_pills_prefix(httpserver, toy_html: str) -> None:
    """DomHand should fill cx-select-pills 'Prefix' by clicking the matching pill button.

    EXPECTED TO FAIL: DomHand's fill path for button-group may not know
    how to click the correct pill.
    """
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())
        fields = await extract_visible_form_fields(page)

        prefix = _find_field(fields, "prefix")
        assert prefix is not None, "Prefix field not found"

        # Try filling via _fill_select_field_outcome (DomHand's actual path)
        result = await _fill_select_field_outcome(
            page,
            prefix,
            "Mr",
            "[Prefix]",
            browser_session=browser_session,
        )
        assert result.success, f"Prefix pill fill failed: {result}"

        # Verify the Mr pill is now pressed
        pressed = await page.evaluate("""() => {
            const pills = document.querySelectorAll('.cx-select-pill-section');
            const mr = Array.from(pills).find(p => p.textContent.trim() === 'Mr');
            return mr ? mr.getAttribute('aria-pressed') : 'not-found';
        }""")
        assert pressed == "true", f"Mr pill should be aria-pressed=true, got {pressed}"


# ---------------------------------------------------------------------------
# 5. Page 2 fields — DomHand extraction after navigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domhand_extracts_page2_after_navigation(httpserver, toy_html: str) -> None:
    """After navigating to page 2, DomHand should extract Application Questions fields.

    Page 2 has ~10 cx-select-pills groups (experience, gender, work auth, etc.)
    These fields are only visible when page 2 is displayed.
    """
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None

        # Navigate to page 2
        await page.evaluate("""() => {
            document.querySelectorAll('.apply-flow-page').forEach(p => p.style.display = 'none');
            document.getElementById('page-2').style.display = 'block';
        }""")
        await asyncio.sleep(0.2)

        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())
        fields = await extract_visible_form_fields(page)

        labels = [_preferred_field_label(f).lower() for f in fields]

        # Should find experience, gender, work authorization questions
        assert any("experience" in l or "years" in l for l in labels), \
            f"Missing experience question on page 2. Fields: {labels}"
        assert any("gender" in l for l in labels), \
            f"Missing gender question on page 2. Fields: {labels}"

        # Count how many button-group / select fields found
        pill_fields = [f for f in fields if f.field_type == "button-group"]
        select_fields = [f for f in fields if f.field_type == "select"]
        total = len(pill_fields) + len(select_fields)
        assert total >= 3, (
            f"Expected >=3 pill/select fields on page 2, "
            f"got {len(pill_fields)} button-group + {len(select_fields)} select = {total}. "
            f"Fields: {[(f.field_type, _preferred_field_label(f)) for f in fields]}"
        )


# ---------------------------------------------------------------------------
# 6. Page 1 control-flow guard — do not refill after assess says advanceable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_personal_info_advanceable_page_blocks_same_page_refill_localhost() -> None:
    """Toy localhost should reproduce the same-page advanceable guard on page 1."""

    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None

            await page.evaluate(
                """() => {
                    document.getElementById('resume-filename').textContent = 'resume.pdf';
                    document.getElementById('file-drop-zone').classList.add('file-form-element--filled');
                    document.getElementById('file-drop-zone').setAttribute('data-state', 'FILLED');

                    const setValue = (id, value, committed = false) => {
                        const el = document.getElementById(id);
                        if (!el) return;
                        el.value = value;
                        if (committed) el.dataset.committedValue = value;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    };

                    setValue('legal-first-name', 'Spencer');
                    setValue('legal-last-name', 'Wang');
                    setValue('preferred-first-name', 'Spencer');
                    setValue('preferred-last-name', 'Wang');
                    setValue('phone-number', '(571) 778-8080');
                    setValue('country-input', 'United States', true);
                    setValue('addr-line1', '200 West Street', true);
                    setValue('zip-input', '10282', true);
                    setValue('city-input', 'New York', true);
                    setValue('state-input', 'New York', true);

                    document.querySelectorAll('[data-field="zipCode"], [data-field="city"], [data-field="state"]').forEach((row) => {
                        row.classList.remove('input-row--invalid');
                    });
                    document.querySelectorAll('#zip-input, #city-input, #state-input').forEach((el) => {
                        el.setAttribute('aria-invalid', 'false');
                    });

                    const mr = Array.from(document.querySelectorAll('.cx-select-pill-section')).find((node) => node.textContent.trim() === 'Mr');
                    if (mr) mr.click();
                }"""
            )

            assess = await domhand_assess_state(
                DomHandAssessStateParams(target_section="Personal Info"),
                browser_session,
            )
            state = json.loads((assess.metadata or {})["application_state_json"])
            assert state["advance_allowed"] is True, state

            with (
                patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="profile text"),
                patch("ghosthands.actions.domhand_fill._get_profile_data", return_value={}),
                patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
                patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
                patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
            ):
                refill = await domhand_fill(
                    DomHandFillParams(target_section="Personal Info"),
                    browser_session,
                )
            assert refill.error is not None
            assert "advance_allowed=yes" in refill.error
            assert (refill.metadata or {})["same_page_advance_guard"] is True

            await page.evaluate("""() => document.getElementById('btn-next')?.click()""")
            await asyncio.sleep(0.1)
            page_two_visible = await page.evaluate(
                """() => document.getElementById('page-2')?.style.display === 'block'"""
            )
            assert str(page_two_visible).lower() == "true"


# ---------------------------------------------------------------------------
# 7. Page 3 repeaters — real DomHand fill pipeline on localhost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domhand_repeat_addition_pipeline_localhost() -> None:
    """DomHand should fill two repeated entries for each Oracle page-3 repeater."""

    seed = {
        "experience": [
            {
                "job_title": "Experience Alpha",
                "company": "Acme",
                "location": "New York",
                "employment_type": "Internship",
            },
            {
                "job_title": "Experience Beta",
                "company": "Bravo",
                "location": "Austin",
                "employment_type": "Contract",
            },
        ],
        "education": [
            {
                "degree": "Bachelor of Science",
                "school": "School One",
                "field_of_study": "Mathematics",
                "start_date_month": "September",
                "start_date_year": "2021",
                "end_date_month": "May",
                "end_date_year": "2025",
            },
            {
                "degree": "Master of Science",
                "school": "School Two",
                "field_of_study": "Computer Science",
                "start_date_month": "September",
                "start_date_year": "2025",
                "end_date_month": "May",
                "end_date_year": "2027",
            },
        ],
        "skill": [
            {"skill_type": "Technical", "skill_name": "Python", "proficiency": "Advanced"},
            {"skill_type": "Technical", "skill_name": "Rust", "proficiency": "Intermediate"},
        ],
        "language": [
            {"language_name": "English", "lang_proficiency": "Native"},
            {"language_name": "Spanish", "lang_proficiency": "Professional"},
        ],
        "license": [
            {"license_name": "Series 7", "issuing_org": "FINRA"},
            {"license_name": "AWS SAA", "issuing_org": "Amazon"},
        ],
    }
    current_entry: dict[str, str] = {}

    def normalize_label(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.lower().strip())
        return re.sub(r"_+", "_", normalized).strip("_")

    async def fake_generate_answers(fields, profile_text, profile_data=None):
        aliases = {
            "job_title": {"title"},
            "field_of_study": {"major", "major_minor", "field_study"},
            "skill_name": {"skill"},
            "language_name": {"language"},
            "lang_proficiency": {"proficiency_level", "language_proficiency"},
            "license_name": {"license_certification_name"},
            "issuing_org": {"issuing_organization"},
        }
        answers: dict[str, str] = {}
        for field in fields:
            original_label = getattr(field, "name", "") or ""
            normalized_label = normalize_label(original_label)
            value = current_entry.get(normalized_label)
            if value is None:
                for canonical_key, alias_set in aliases.items():
                    if normalized_label == canonical_key and canonical_key in current_entry:
                        value = current_entry[canonical_key]
                        break
                    if normalized_label in alias_set and canonical_key in current_entry:
                        value = current_entry[canonical_key]
                        break
            if value is not None:
                answers[original_label] = value
        return answers, 0, 0, 0.0, "stub"

    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None

            await page.evaluate(
                """() => {
                    document.querySelectorAll('.apply-flow-page').forEach(p => p.style.display = 'none');
                    document.getElementById('page-3').style.display = 'block';
                }"""
            )

            with (
                patch("ghosthands.actions.domhand_fill._get_profile_text", return_value="profile text"),
                patch("ghosthands.actions.domhand_fill._get_profile_data", return_value={}),
                patch("ghosthands.actions.domhand_fill._get_auth_override_data", return_value={}),
                patch("ghosthands.actions.domhand_fill._infer_entry_data_from_scope", return_value=None),
                patch("ghosthands.actions.domhand_fill._parse_profile_evidence", return_value={}),
                patch(
                    "ghosthands.actions.domhand_fill._safe_page_url",
                    AsyncMock(return_value="http://127.0.0.1:8767/index.html#/apply/section/3"),
                ),
                patch("ghosthands.actions.domhand_fill._stagehand_observe_cross_reference", AsyncMock(return_value=None)),
                patch("ghosthands.actions.domhand_fill._generate_answers", side_effect=fake_generate_answers),
            ):
                for profile_type, entries in seed.items():
                    for idx, entry in enumerate(entries, start=1):
                        current_entry.clear()
                        current_entry.update(entry)

                        opened = await page.evaluate(
                            """(ptype) => {
                                const btn = document.querySelector(`.apply-flow-profile-item-tile__new-tile[data-profile-type="${ptype}"]`);
                                if (!btn || btn.disabled) return false;
                                btn.click();
                                return true;
                            }""",
                            profile_type,
                        )
                        assert opened, f"Could not open Add form for {profile_type} #{idx}"

                        result = await domhand_fill(
                            DomHandFillParams(entry_data=entry),
                            browser_session,
                        )
                        assert result.error is None, f"{profile_type} #{idx} fill failed: {result.error}"

                        payload = (result.metadata or {}).get("domhand_fill_json")
                        if payload is not None:
                            assert payload["dom_failure_count"] == 0, (
                                f"{profile_type} #{idx} had DOM fill failures: {payload}"
                            )
                            assert not payload["unresolved_required_fields"], (
                                f"{profile_type} #{idx} left required fields unresolved: {payload}"
                            )

                        saved = await page.evaluate(
                            """({ptype, idx}) => {
                                const btn = document.querySelector(
                                    `.profile-inline-form[data-profile-type="${ptype}"][data-profile-index="${idx}"] .profile-inline-form__save`
                                );
                                if (!btn) return false;
                                btn.click();
                                return true;
                            }""",
                            {"ptype": profile_type, "idx": idx},
                        )
                        assert saved, f"Could not save {profile_type} #{idx}"
                        await asyncio.sleep(0.1)

            summary = json.loads(
                await page.evaluate(
                    """() => JSON.stringify({
                        inlineFormsRemaining: document.querySelectorAll('#page-3 .profile-inline-form').length,
                        experienceTitles: Array.from(document.querySelectorAll('#experience-container .apply-flow-profile-item-tile__summary-title')).map(el => el.textContent.trim()),
                        educationTitles: Array.from(document.querySelectorAll('#education-container .apply-flow-profile-item-tile__summary-title')).map(el => el.textContent.trim()),
                        skillTitles: Array.from(document.querySelectorAll('#skills-container .apply-flow-profile-item-tile__summary-title')).map(el => el.textContent.trim()),
                        languageTitles: Array.from(document.querySelectorAll('#languages-container .apply-flow-profile-item-tile__summary-title')).map(el => el.textContent.trim()),
                        licenseTitles: Array.from(document.querySelectorAll('#licenses-container .apply-flow-profile-item-tile__summary-title')).map(el => el.textContent.trim())
                    })"""
                )
            )

            assert summary["inlineFormsRemaining"] == 0
            assert summary["experienceTitles"] == ["Experience Alpha", "Experience Beta"]
            assert summary["educationTitles"] == ["School One", "School Two"]
            assert summary["skillTitles"] == ["Python", "Rust"]
            assert summary["languageTitles"] == ["English", "Spanish"]
            assert summary["licenseTitles"] == ["Series 7", "AWS SAA"]
