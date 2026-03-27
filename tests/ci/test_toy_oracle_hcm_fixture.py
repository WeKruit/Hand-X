"""Smoke tests for ``examples/toy-oracle-hcm/index.html``.

Validates DomHand field extraction and interaction on Oracle Cloud HCM
patterns: cx-select-pills, cx-select combobox (with grid dropdown),
geo-hierarchy-form-element, composite phone, radio groups, file upload,
profile-item tiles, and multi-section pagination.

  uv run pytest tests/ci/test_toy_oracle_hcm_fixture.py -v
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools
from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    _preferred_field_label,
    extract_visible_form_fields,
)
from ghosthands.dom.shadow_helpers import ensure_helpers

_FIXTURE = (
    Path(__file__).resolve().parent.parent.parent
    / "examples"
    / "toy-oracle-hcm"
    / "index.html"
)
_PROFILE = Path(__file__).resolve().parent.parent.parent / "scripts" / "test_resume.json"


def _parse(result):
    """page.evaluate may return a dict or a JSON string — normalize."""
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
    """Reserve a free localhost TCP port for the fixture server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


@asynccontextmanager
async def managed_local_fixture_server():
    """Serve the toy Oracle fixture via a real localhost HTTP server."""
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


@pytest.fixture
def toy_html() -> str:
    assert _FIXTURE.is_file(), f"missing toy fixture: {_FIXTURE}"
    return _FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def sample_profile() -> dict:
    assert _PROFILE.is_file(), f"missing sample profile: {_PROFILE}"
    return json.loads(_PROFILE.read_text(encoding="utf-8"))


async def _eval(page, js: str):
    """Evaluate JS and parse result (handles string serialization)."""
    raw = await page.evaluate(js)
    return _parse(raw)


# ---------------------------------------------------------------------------
# 1. Field extraction — can DomHand see the Oracle HCM fields?
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oracle_hcm_field_extraction_page1(httpserver, toy_html: str) -> None:
    """DomHand should extract text, select, and pill fields from page 1."""
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

        labels = [_preferred_field_label(f).lower() for f in fields]
        types = {_preferred_field_label(f).lower(): f.field_type for f in fields}

        # Text fields
        assert any("first name" in l for l in labels), f"Missing first name in {labels}"
        assert any("last name" in l for l in labels), f"Missing last name in {labels}"

        # Email (readonly)
        assert any("email" in l for l in labels), f"Missing email in {labels}"

        # Phone number
        assert any("phone" in l for l in labels), f"Missing phone in {labels}"

        # Combobox fields (Country, Address Line 1, etc.)
        select_fields = [l for l, t in types.items() if t == "select"]
        assert len(select_fields) >= 3, f"Expected >=3 select fields, got {select_fields}"


# ---------------------------------------------------------------------------
# 2. cx-select-pills — clicking a pill sets aria-pressed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oracle_hcm_pills_toggle(httpserver, toy_html: str) -> None:
    """Clicking a cx-select-pill should toggle aria-pressed."""
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None

        # Click "Mr" pill
        result = await _eval(page, """() => {
            const pills = document.querySelectorAll('.cx-select-pills-container .cx-select-pill-section');
            const mr = Array.from(pills).find(p => p.textContent.trim() === 'Mr');
            if (!mr) return JSON.stringify({ found: false, count: pills.length });
            mr.click();
            return JSON.stringify({
                found: true,
                pressed: mr.getAttribute('aria-pressed'),
                text: mr.textContent.trim()
            });
        }""")
        assert result["found"], f"Mr pill not found: {result}"
        assert result["pressed"] == "true", f"Expected aria-pressed=true, got {result['pressed']}"

        # Click "Mrs" — Mr should become false
        result2 = await _eval(page, """() => {
            const pills = document.querySelectorAll('.cx-select-pills-container .cx-select-pill-section');
            const mrs = Array.from(pills).find(p => p.textContent.trim() === 'Mrs');
            const mr = Array.from(pills).find(p => p.textContent.trim() === 'Mr');
            if (!mrs) return JSON.stringify({ found: false });
            mrs.click();
            return JSON.stringify({
                found: true,
                mrs_pressed: mrs.getAttribute('aria-pressed'),
                mr_pressed: mr.getAttribute('aria-pressed')
            });
        }""")
        assert result2["mrs_pressed"] == "true"
        assert result2["mr_pressed"] == "false", "Previous pill should be deselected"


# ---------------------------------------------------------------------------
# 3. cx-select combobox — dropdown opens and options are selectable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oracle_hcm_combobox_country(httpserver, toy_html: str) -> None:
    """Country combobox should open dropdown and allow selection via gridcell."""
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None

        # Click toggle button to open country dropdown
        opened = await _eval(page, """() => {
            const toggle = document.querySelector('[data-toggle-for="country-dropdown"]');
            if (toggle) toggle.click();
            else { const input = document.getElementById('country-input'); if (input) input.click(); }
            const dropdown = document.getElementById('country-dropdown');
            return dropdown ? dropdown.getAttribute('data-open') : 'no-dropdown';
        }""")
        assert opened in ("true", True), f"Dropdown did not open: {opened}"

        # Select an option
        selected = await _eval(page, """() => {
            const cells = document.querySelectorAll('#country-dropdown [role="gridcell"]');
            const us = Array.from(cells).find(c => c.textContent.includes('United States'));
            if (!us) return JSON.stringify({ found: false, cells: cells.length });
            us.click();
            const input = document.getElementById('country-input');
            return JSON.stringify({
                found: true,
                value: input ? input.value : '',
                dropdown_open: document.getElementById('country-dropdown').getAttribute('data-open')
            });
        }""")
        assert selected["found"], f"US option not found, {selected.get('cells', 0)} cells"
        assert "United States" in selected["value"]
        assert selected["dropdown_open"] == "false", "Dropdown should close after selection"


# ---------------------------------------------------------------------------
# 4. Geo-hierarchy address — addr1 auto-suggest fills dependents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oracle_hcm_address_autosuggest_backfill(httpserver, toy_html: str) -> None:
    """Typing in Address Line 1 and selecting a suggestion should fill ZIP/City/State."""
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None

        # Type in address line 1 to trigger suggestions
        await page.evaluate("""() => {
            const input = document.getElementById('addr-line1');
            if (input) {
                input.focus();
                input.value = '123';
                input.dispatchEvent(new Event('input', { bubbles: true }));
            }
        }""")
        await asyncio.sleep(0.3)

        # Check suggestions appeared and click first one
        backfill = await _eval(page, """() => {
            const dropdown = document.getElementById('addr1-dropdown');
            if (!dropdown || dropdown.getAttribute('data-open') !== 'true')
                return JSON.stringify({ suggestions: false });
            const cells = dropdown.querySelectorAll('[role="gridcell"]');
            if (cells.length === 0) return JSON.stringify({ suggestions: false, cells: 0 });
            const row = cells[0].closest('[role="row"]');
            if (row) row.click(); else cells[0].click();
            return JSON.stringify({
                suggestions: true,
                addr1: document.getElementById('addr-line1').value,
                zip: (document.getElementById('zip-input') || {}).value || '',
                city: (document.getElementById('city-input') || {}).value || '',
                state: (document.getElementById('state-input') || {}).value || ''
            });
        }""")
        assert backfill["suggestions"], "Address suggestions did not appear"
        assert backfill["addr1"] != "", "Address Line 1 should have a value after selection"


# ---------------------------------------------------------------------------
# 5. Multi-section pagination — Next/Back toggle visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oracle_hcm_pagination(httpserver, toy_html: str) -> None:
    """Clicking Next should show page 2, hide page 1."""
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None

        # Page 1 visible, page 2 hidden
        visibility = await _eval(page, """() => JSON.stringify({
            page1: document.getElementById('page-1')?.style.display || 'visible',
            page2: document.getElementById('page-2')?.style.display || 'visible'
        })""")
        assert visibility["page1"] != "none", "Page 1 should be visible initially"
        assert visibility["page2"] == "none", "Page 2 should be hidden initially"

        # Click Next
        await page.evaluate("""() => {
            const next = document.getElementById('btn-next');
            if (next) next.click();
        }""")
        await asyncio.sleep(0.2)

        visibility2 = await _eval(page, """() => JSON.stringify({
            page1: document.getElementById('page-1')?.style.display || 'visible',
            page2: document.getElementById('page-2')?.style.display || 'visible'
        })""")
        assert visibility2["page1"] == "none", f"Page 1 should be hidden after Next: {visibility2}"
        assert visibility2["page2"] != "none", f"Page 2 should be visible after Next: {visibility2}"


# ---------------------------------------------------------------------------
# 6. Profile item tiles — Add Education creates inline form
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oracle_hcm_profile_tile_add(httpserver, toy_html: str) -> None:
    """Clicking 'Add Experience' should create an inline form."""
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None

        # Show page 3 (Experience)
        await page.evaluate("""() => {
            document.querySelectorAll('.apply-flow-page').forEach(p => p.style.display = 'none');
            const p3 = document.getElementById('page-3');
            if (p3) p3.style.display = 'block';
        }""")

        # Count inputs before click
        before_count = await page.evaluate(
            "() => document.querySelectorAll('#page-3 input, #page-3 select').length"
        )

        # Click Add Experience button
        await page.evaluate("""() => {
            const btn = document.querySelector(
                '#page-3 .apply-flow-profile-item-tile__new-tile[data-profile-type="experience"]'
            );
            if (btn) btn.click();
        }""")
        await asyncio.sleep(0.3)

        after_count = await page.evaluate(
            "() => document.querySelectorAll('#page-3 input, #page-3 select').length"
        )
        assert after_count > before_count, \
            f"Expected new inputs after Add click: before={before_count}, after={after_count}"


@pytest.mark.asyncio
async def test_oracle_hcm_profile_tile_harsh_flow_localhost() -> None:
    """Serve the full toy site on localhost and save all five repeater entries on page 3."""
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None

            await page.evaluate("""() => {
                document.querySelectorAll('.apply-flow-page').forEach(p => p.style.display = 'none');
                const p3 = document.getElementById('page-3');
                if (p3) p3.style.display = 'block';
            }""")

            await page.evaluate("""() => {
                ['experience', 'education', 'skill', 'language', 'license'].forEach((type) => {
                    const btn = document.querySelector(
                        `.apply-flow-profile-item-tile__new-tile[data-profile-type="${type}"]`
                    );
                    if (btn) btn.click();
                });
            }""")
            await asyncio.sleep(0.4)

            await page.evaluate("""() => {
                const setValue = (id, value) => {
                    const input = document.getElementById(id);
                    if (!input) return false;
                    input.focus();
                    input.value = value;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                };

                return JSON.stringify({
                    jobTitle: setValue('job-title-1', 'Software Engineering Intern'),
                    company: setValue('company-1', 'WeKruit'),
                    location: setValue('location-1', 'Los Angeles'),
                    fieldOfStudy: setValue('field-of-study-1', 'Computer Science'),
                    licenseName: setValue('license-name-1', 'Series 7'),
                    issuingOrg: setValue('issuing-org-1', 'FINRA')
                });
            }""")

            filled = await _eval(page, """() => {
                const choose = (inputId, value) => {
                    const input = document.getElementById(inputId);
                    if (!input) return false;
                    const toggle = input.parentElement && input.parentElement.querySelector('.cx-select-toggle');
                    if (toggle) toggle.click();
                    const dropdownId = input.getAttribute('aria-controls');
                    const dropdown = dropdownId ? document.getElementById(dropdownId) : null;
                    if (!dropdown) return false;
                    const row = Array.from(dropdown.querySelectorAll('[role="row"]'))
                        .find(r => (r.getAttribute('data-value') || '').trim() === value);
                    if (!row) return false;
                    row.click();
                    return input.value === value;
                };

                return JSON.stringify({
                    employmentType: choose('employment-type-1', 'Internship'),
                    degree: choose('degree-1', "Bachelor's"),
                    school: choose('school-1', 'University of California, Los Angeles'),
                    educationStartMonth: choose('start-date-month-1', 'September'),
                    educationStartYear: choose('start-date-year-1', '2024'),
                    educationEndMonth: choose('end-date-month-1', 'June'),
                    educationEndYear: choose('end-date-year-1', '2029'),
                    skillType: choose('skill-type-1', 'Technical'),
                    skillName: choose('skill-name-1', 'Python'),
                    skillProficiency: choose('proficiency-1', 'Advanced'),
                    language: choose('language-name-1', 'English'),
                    languageProficiency: choose('lang-proficiency-1', 'Native'),
                    inlineForms: document.querySelectorAll('#page-3 .profile-inline-form').length,
                    employmentTypeValue: (document.getElementById('employment-type-1') || {}).value || '',
                    degreeValue: (document.getElementById('degree-1') || {}).value || '',
                    schoolValue: (document.getElementById('school-1') || {}).value || '',
                    educationStartMonthValue: (document.getElementById('start-date-month-1') || {}).value || '',
                    educationStartYearValue: (document.getElementById('start-date-year-1') || {}).value || '',
                    educationEndMonthValue: (document.getElementById('end-date-month-1') || {}).value || '',
                    educationEndYearValue: (document.getElementById('end-date-year-1') || {}).value || '',
                    skillTypeValue: (document.getElementById('skill-type-1') || {}).value || '',
                    skillNameValue: (document.getElementById('skill-name-1') || {}).value || '',
                    skillProficiencyValue: (document.getElementById('proficiency-1') || {}).value || '',
                    languageValue: (document.getElementById('language-name-1') || {}).value || '',
                    languageProficiencyValue: (document.getElementById('lang-proficiency-1') || {}).value || ''
                });
            }""")

            assert filled["inlineForms"] == 5, f"Expected 5 inline forms, got {filled}"
            assert filled["employmentType"] is True, f"Employment type did not commit: {filled}"
            assert filled["degree"] is True, f"Degree combobox did not commit: {filled}"
            assert filled["school"] is True, f"School combobox did not commit: {filled}"
            assert filled["educationStartMonth"] is True, f"Education start month did not commit: {filled}"
            assert filled["educationStartYear"] is True, f"Education start year did not commit: {filled}"
            assert filled["educationEndMonth"] is True, f"Education end month did not commit: {filled}"
            assert filled["educationEndYear"] is True, f"Education end year did not commit: {filled}"
            assert filled["skillType"] is True, f"Skill type did not commit: {filled}"
            assert filled["skillName"] is True, f"Skill combobox did not commit: {filled}"
            assert filled["skillProficiency"] is True, f"Skill proficiency did not commit: {filled}"
            assert filled["language"] is True, f"Language combobox did not commit: {filled}"
            assert filled["languageProficiency"] is True, f"Language proficiency did not commit: {filled}"
            assert filled["employmentTypeValue"] == "Internship"
            assert filled["degreeValue"] == "Bachelor's"
            assert filled["schoolValue"] == "University of California, Los Angeles"
            assert filled["educationStartMonthValue"] == "September"
            assert filled["educationStartYearValue"] == "2024"
            assert filled["educationEndMonthValue"] == "June"
            assert filled["educationEndYearValue"] == "2029"
            assert filled["skillTypeValue"] == "Technical"
            assert filled["skillNameValue"] == "Python"
            assert filled["skillProficiencyValue"] == "Advanced"
            assert filled["languageValue"] == "English"
            assert filled["languageProficiencyValue"] == "Native"

            await page.evaluate("""() => {
                ['experience', 'education', 'skill', 'language', 'license'].forEach((type) => {
                    const saveBtn = document.querySelector(
                        `.profile-inline-form[data-profile-type="${type}"] .profile-inline-form__save`
                    );
                    if (saveBtn) saveBtn.click();
                });
            }""")
            await asyncio.sleep(0.4)

            saved = await _eval(page, """() => {
                const getContainerSummary = (containerId) => {
                    const container = document.getElementById(containerId);
                    const tile = container && container.querySelector('.apply-flow-profile-item-tile--saved');
                    return {
                        addDisabled: !!(container && container.querySelector('.apply-flow-profile-item-tile__new-tile')?.disabled),
                        title: tile && tile.querySelector('.apply-flow-profile-item-tile__summary-title')
                            ? tile.querySelector('.apply-flow-profile-item-tile__summary-title').textContent.trim()
                            : '',
                        subtitle: tile && tile.querySelector('.apply-flow-profile-item-tile__summary-subtitle')
                            ? tile.querySelector('.apply-flow-profile-item-tile__summary-subtitle').textContent.trim()
                            : ''
                    };
                };

                return JSON.stringify({
                    inlineForms: document.querySelectorAll('#page-3 .profile-inline-form').length,
                    savedTiles: document.querySelectorAll('#page-3 .apply-flow-profile-item-tile--saved').length,
                    experience: getContainerSummary('experience-container'),
                    education: getContainerSummary('education-container'),
                    skill: getContainerSummary('skills-container'),
                    language: getContainerSummary('languages-container'),
                    license: getContainerSummary('licenses-container')
                });
            }""")

            assert saved["inlineForms"] == 0, f"Expected all inline forms to close after save: {saved}"
            assert saved["savedTiles"] == 5, f"Expected 5 committed tiles after save: {saved}"
            assert saved["experience"]["addDisabled"] is False
            assert saved["education"]["addDisabled"] is False
            assert saved["skill"]["addDisabled"] is False
            assert saved["language"]["addDisabled"] is False
            assert saved["license"]["addDisabled"] is False
            assert saved["experience"]["title"] == "Software Engineering Intern"
            assert saved["experience"]["subtitle"] == "WeKruit — Los Angeles"
            assert saved["education"]["title"] == "University of California, Los Angeles"
            assert saved["education"]["subtitle"] == "Bachelor's — Computer Science"
            assert saved["skill"]["title"] == "Python"
            assert saved["skill"]["subtitle"] == "Advanced"
            assert saved["language"]["title"] == "English"
            assert saved["language"]["subtitle"] == "Native"
            assert saved["license"]["title"] == "Series 7"
            assert saved["license"]["subtitle"] == "FINRA"


@pytest.mark.asyncio
async def test_oracle_hcm_localhost_profile_driven_supported_content(
    sample_profile: dict,
) -> None:
    """Fill the localhost fixture from sample profile data and assert supported content sticks."""
    profile_json = json.dumps(sample_profile)

    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None

            summary = await _eval(page, f"""() => {{
                const profile = {profile_json};

                const setText = (id, value) => {{
                    const input = document.getElementById(id);
                    if (!input) return false;
                    input.focus();
                    input.value = value ?? '';
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                    return true;
                }};

                const chooseOption = (inputId, value) => {{
                    const input = document.getElementById(inputId);
                    if (!input) return false;
                    const toggle = input.parentElement && input.parentElement.querySelector('.cx-select-toggle');
                    if (toggle) toggle.click();
                    const dropdownId = input.getAttribute('aria-controls');
                    const dropdown = dropdownId ? document.getElementById(dropdownId) : null;
                    if (!dropdown) return false;
                    const row = Array.from(dropdown.querySelectorAll('[role="row"]'))
                        .find(r => (r.getAttribute('data-value') || '').trim() === value);
                    if (!row) return false;
                    row.click();
                    return input.value === value;
                }};

                const clickPill = (fieldName, value) => {{
                    const row = document.querySelector(`.input-row[data-field="${{fieldName}}"]`);
                    if (!row) return false;
                    const pill = Array.from(row.querySelectorAll('.cx-select-pill-section'))
                        .find(p => (p.getAttribute('data-value') || '').trim() === value);
                    if (!pill) return false;
                    pill.click();
                    return pill.getAttribute('aria-pressed') === 'true';
                }};

                const showPage = (n) => {{
                    document.querySelectorAll('.apply-flow-page').forEach(p => p.style.display = 'none');
                    const page = document.getElementById(`page-${{n}}`);
                    if (page) page.style.display = 'block';
                }};

                const getSavedTitles = (containerId) => {{
                    const container = document.getElementById(containerId);
                    return container
                        ? Array.from(container.querySelectorAll('.apply-flow-profile-item-tile__summary-title'))
                            .map(el => el.textContent.trim())
                        : [];
                }};

                const getSavedSubtitles = (containerId) => {{
                    const container = document.getElementById(containerId);
                    return container
                        ? Array.from(container.querySelectorAll('.apply-flow-profile-item-tile__summary-subtitle'))
                            .map(el => el.textContent.trim())
                        : [];
                }};

                const stateFullName = {{
                    NY: 'New York',
                    CA: 'California',
                    NJ: 'New Jersey',
                    IL: 'Illinois',
                    TX: 'Texas',
                }};

                // Page 1
                showPage(1);
                document.querySelector('.edit-btn')?.click();
                setText('legal-first-name', profile.first_name || '');
                setText('legal-last-name', profile.last_name || '');
                setText('preferred-first-name', profile.preferred_name || '');
                setText('preferred-last-name', profile.last_name || '');
                setText('email-field', profile.email || '');
                setText('phone-number', profile.phone || '');
                chooseOption('country-input', profile.address?.country || profile.country || 'United States');
                setText('zip-input', profile.address?.zip || profile.postal_code || '');
                chooseOption('city-input', profile.address?.city || profile.city || '');
                chooseOption('state-input', stateFullName[profile.address?.state || profile.state] || profile.address?.state || profile.state || '');
                document.querySelector('.edit-btn')?.click();

                const page1 = {{
                    legalFirstName: document.getElementById('legal-first-name')?.value || '',
                    legalLastName: document.getElementById('legal-last-name')?.value || '',
                    preferredFirstName: document.getElementById('preferred-first-name')?.value || '',
                    preferredLastName: document.getElementById('preferred-last-name')?.value || '',
                    email: document.getElementById('email-field')?.value || '',
                    phone: document.getElementById('phone-number')?.value || '',
                    country: document.getElementById('country-input')?.value || '',
                    zip: document.getElementById('zip-input')?.value || '',
                    city: document.getElementById('city-input')?.value || '',
                    state: document.getElementById('state-input')?.value || '',
                }};

                // Page 2
                showPage(2);
                clickPill('yearsExperience', '1-2 years');
                clickPill('workAuthorization', profile.work_authorization === 'Yes' ? 'Yes' : 'No');
                setText('employer-input', profile.current_company || '');
                const page2 = {{
                    years: Array.from(document.querySelectorAll('.input-row[data-field="yearsExperience"] .cx-select-pill-section'))
                        .find(p => p.getAttribute('aria-pressed') === 'true')?.getAttribute('data-value') || '',
                    workAuthorization: Array.from(document.querySelectorAll('.input-row[data-field="workAuthorization"] .cx-select-pill-section'))
                        .find(p => p.getAttribute('aria-pressed') === 'true')?.getAttribute('data-value') || '',
                    employer: document.getElementById('employer-input')?.value || '',
                }};

                // Page 3
                showPage(3);
                (profile.experience || []).forEach((entry, i) => {{
                    document.querySelector('.apply-flow-profile-item-tile__new-tile[data-profile-type="experience"]')?.click();
                    const idx = i + 1;
                    setText(`job-title-${{idx}}`, entry.title || '');
                    setText(`company-${{idx}}`, entry.company || '');
                    setText(`location-${{idx}}`, entry.location || '');
                    document.querySelector(`.profile-inline-form[data-profile-type="experience"][data-profile-index="${{idx}}"] .profile-inline-form__save`)?.click();
                }});

                (profile.education || []).forEach((entry, i) => {{
                    document.querySelector('.apply-flow-profile-item-tile__new-tile[data-profile-type="education"]')?.click();
                    const idx = i + 1;
                    const [startYear, startMonth] = String(entry.start_date || '').split('-');
                    const [endYear, endMonth] = String(entry.end_date || '').split('-');
                    const monthName = (monthNumber) => {{
                        const monthIndex = Number(monthNumber || '0');
                        return ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'][monthIndex - 1] || '';
                    }};
                    chooseOption(`degree-${{idx}}`, entry.degree || '');
                    chooseOption(`school-${{idx}}`, entry.school || '');
                    setText(`field-of-study-${{idx}}`, entry.field_of_study || '');
                    chooseOption(`start-date-month-${{idx}}`, monthName(startMonth));
                    chooseOption(`start-date-year-${{idx}}`, startYear || '');
                    if (endMonth) chooseOption(`end-date-month-${{idx}}`, monthName(endMonth));
                    if (endYear) chooseOption(`end-date-year-${{idx}}`, endYear || '');
                    document.querySelector(`.profile-inline-form[data-profile-type="education"][data-profile-index="${{idx}}"] .profile-inline-form__save`)?.click();
                }});

                (profile.skills || []).forEach((skill, i) => {{
                    document.querySelector('.apply-flow-profile-item-tile__new-tile[data-profile-type="skill"]')?.click();
                    const idx = i + 1;
                    const inferSkillType = (value) => {{
                        if (['MATLAB', 'SuperCollider'].includes(value)) return 'Research';
                        if (['English', 'Spanish', 'Mandarin', 'French', 'German', 'Japanese'].includes(value)) return 'Language';
                        return 'Technical';
                    }};
                    chooseOption(`skill-type-${{idx}}`, inferSkillType(skill || ''));
                    chooseOption(`skill-name-${{idx}}`, skill || '');
                    document.querySelector(`.profile-inline-form[data-profile-type="skill"][data-profile-index="${{idx}}"] .profile-inline-form__save`)?.click();
                }});

                const page3 = {{
                    inlineFormsRemaining: document.querySelectorAll('#page-3 .profile-inline-form').length,
                    experienceTitles: getSavedTitles('experience-container'),
                    experienceSubtitles: getSavedSubtitles('experience-container'),
                    educationTitles: getSavedTitles('education-container'),
                    educationSubtitles: getSavedSubtitles('education-container'),
                    skillTitles: getSavedTitles('skills-container'),
                    languageTitles: getSavedTitles('languages-container'),
                    licenseTitles: getSavedTitles('licenses-container'),
                }};

                // Page 4
                showPage(4);
                chooseOption('veteran-input', "I don't wish to answer");
                document.querySelector('.apply-flow-input-radio-control[value="prefer-not"]')?.click();
                setText('e-signature-input', profile.full_name || `${{profile.first_name || ''}} ${{profile.last_name || ''}}`.trim());
                const page4 = {{
                    veteran: document.getElementById('veteran-input')?.value || '',
                    disabilityChecked: !!document.querySelector('.apply-flow-input-radio-control[value="prefer-not"]:checked'),
                    esignature: document.getElementById('e-signature-input')?.value || '',
                }};

                return JSON.stringify({{ page1, page2, page3, page4 }});
            }}""")

            assert summary["page1"]["legalFirstName"] == sample_profile["first_name"]
            assert summary["page1"]["legalLastName"] == sample_profile["last_name"]
            assert summary["page1"]["preferredFirstName"] == sample_profile["preferred_name"]
            assert summary["page1"]["preferredLastName"] == sample_profile["last_name"]
            assert summary["page1"]["email"] == sample_profile["email"]
            assert sample_profile["phone"].replace("-", "") in summary["page1"]["phone"].replace("-", "")
            assert summary["page1"]["country"] == "United States"
            assert summary["page1"]["zip"] == sample_profile["address"]["zip"]
            assert summary["page1"]["city"] == sample_profile["address"]["city"]
            assert summary["page1"]["state"] == "New York"

            assert summary["page2"]["years"] == "1-2 years"
            assert summary["page2"]["workAuthorization"] == "Yes"
            assert summary["page2"]["employer"] == sample_profile["current_company"]

            assert summary["page3"]["inlineFormsRemaining"] == 0
            assert summary["page3"]["experienceTitles"] == [
                entry["title"] for entry in sample_profile["experience"]
            ]
            assert summary["page3"]["experienceSubtitles"] == [
                entry["company"] + (f" — {entry['location']}" if entry.get("location") else "")
                for entry in sample_profile["experience"]
            ]
            assert summary["page3"]["educationTitles"] == [
                entry["school"] for entry in sample_profile["education"]
            ]
            assert summary["page3"]["educationSubtitles"] == [
                entry["degree"] + (f" — {entry['field_of_study']}" if entry.get("field_of_study") else "")
                for entry in sample_profile["education"]
            ]
            assert summary["page3"]["skillTitles"] == sample_profile["skills"]
            assert summary["page3"]["languageTitles"] == []
            assert summary["page3"]["licenseTitles"] == []

            assert summary["page4"]["veteran"] == "I don't wish to answer"
            assert summary["page4"]["disabilityChecked"] is True
            assert summary["page4"]["esignature"] == sample_profile["full_name"]


# ---------------------------------------------------------------------------
# 8. Repeat add — each repeater can add multiple committed entries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oracle_hcm_repeat_addition_across_repeaters_localhost() -> None:
    """Each page-3 repeater should support repeated Add -> Save cycles on localhost."""
    seed = {
        "experience": [
            {"job-title": "Experience Alpha", "company": "Acme", "location": "New York"},
            {"job-title": "Experience Beta", "company": "Bravo", "location": "Austin"},
        ],
        "education": [
            {
                "degree": "Bachelor of Science",
                "school": "School One",
                "field-of-study": "Mathematics",
                "start-date-month": "September",
                "start-date-year": "2021",
                "end-date-month": "May",
                "end-date-year": "2025",
            },
            {
                "degree": "Master of Science",
                "school": "School Two",
                "field-of-study": "Computer Science",
                "start-date-month": "September",
                "start-date-year": "2025",
                "end-date-month": "May",
                "end-date-year": "2027",
            },
        ],
        "skill": [
            {"skill-type": "Technical", "skill-name": "Python", "proficiency": "Advanced"},
            {"skill-type": "Technical", "skill-name": "Rust", "proficiency": "Intermediate"},
        ],
        "language": [
            {"language-name": "English", "lang-proficiency": "Native"},
            {"language-name": "Spanish", "lang-proficiency": "Professional"},
        ],
        "license": [
            {"license-name": "Series 7", "issuing-org": "FINRA"},
            {"license-name": "AWS SAA", "issuing-org": "Amazon"},
        ],
    }
    seed_json = json.dumps(seed)

    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None

            summary = await _eval(page, f"""() => {{
                const seed = {seed_json};

                const showPage = (n) => {{
                    document.querySelectorAll('.apply-flow-page').forEach(p => p.style.display = 'none');
                    const page = document.getElementById(`page-${{n}}`);
                    if (page) page.style.display = 'block';
                }};

                const setText = (id, value) => {{
                    const input = document.getElementById(id);
                    if (!input) return false;
                    input.focus();
                    input.value = value ?? '';
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return true;
                }};

                const chooseOption = (inputId, value) => {{
                    const input = document.getElementById(inputId);
                    if (!input) return false;
                    const toggle = input.parentElement && input.parentElement.querySelector('.cx-select-toggle');
                    if (toggle) toggle.click();
                    const dropdownId = input.getAttribute('aria-controls');
                    const dropdown = dropdownId ? document.getElementById(dropdownId) : null;
                    if (!dropdown) return false;
                    const row = Array.from(dropdown.querySelectorAll('[role="row"]'))
                        .find(r => (r.getAttribute('data-value') || '').trim() === value);
                    if (!row) return false;
                    row.click();
                    return input.value === value;
                }};

                const addButton = (type) =>
                    document.querySelector(`.apply-flow-profile-item-tile__new-tile[data-profile-type="${{type}}"]`);

                const getTitles = (containerId) => {{
                    const container = document.getElementById(containerId);
                    return container
                        ? Array.from(container.querySelectorAll('.apply-flow-profile-item-tile__summary-title'))
                            .map(el => el.textContent.trim())
                        : [];
                }};

                const getSubtitles = (containerId) => {{
                    const container = document.getElementById(containerId);
                    return container
                        ? Array.from(container.querySelectorAll('.apply-flow-profile-item-tile__summary-subtitle'))
                            .map(el => el.textContent.trim())
                        : [];
                }};

                showPage(3);

                const status = {{}};
                Object.entries(seed).forEach(([type, entries]) => {{
                    status[type] = [];
                    entries.forEach((entry, i) => {{
                        const idx = i + 1;
                        const btnBefore = addButton(type);
                        const enabledBefore = !!(btnBefore && !btnBefore.disabled);
                        if (btnBefore) btnBefore.click();

                        Object.entries(entry).forEach(([fieldId, value]) => {{
                            const inputId = `${{fieldId}}-${{idx}}`;
                            if (
                                fieldId === 'degree' ||
                                fieldId === 'school' ||
                                fieldId === 'start-date-month' ||
                                fieldId === 'start-date-year' ||
                                fieldId === 'end-date-month' ||
                                fieldId === 'end-date-year' ||
                                fieldId === 'skill-type' ||
                                fieldId === 'skill-name' ||
                                fieldId === 'proficiency' ||
                                fieldId === 'language-name' ||
                                fieldId === 'lang-proficiency'
                            ) {{
                                if (!chooseOption(inputId, value)) setText(inputId, value);
                            }} else {{
                                setText(inputId, value);
                            }}
                        }});

                        document.querySelector(
                            `.profile-inline-form[data-profile-type="${{type}}"][data-profile-index="${{idx}}"] .profile-inline-form__save`
                        )?.click();

                        status[type].push({{
                            enabledBefore,
                            addDisabledAfterSave: !!(addButton(type) && addButton(type).disabled),
                            tileCountAfterSave: document.querySelectorAll(
                                `#${{type === 'skill' ? 'skills' : type === 'language' ? 'languages' : type === 'license' ? 'licenses' : type}}-container .apply-flow-profile-item-tile--saved`
                            ).length,
                        }});
                    }});
                }});

                return JSON.stringify({{
                    inlineFormsRemaining: document.querySelectorAll('#page-3 .profile-inline-form').length,
                    status,
                    experienceTitles: getTitles('experience-container'),
                    experienceSubtitles: getSubtitles('experience-container'),
                    educationTitles: getTitles('education-container'),
                    educationSubtitles: getSubtitles('education-container'),
                    skillTitles: getTitles('skills-container'),
                    skillSubtitles: getSubtitles('skills-container'),
                    languageTitles: getTitles('languages-container'),
                    languageSubtitles: getSubtitles('languages-container'),
                    licenseTitles: getTitles('licenses-container'),
                    licenseSubtitles: getSubtitles('licenses-container'),
                }});
            }}""")

            assert summary["inlineFormsRemaining"] == 0

            for repeater_type in ("experience", "education", "skill", "language", "license"):
                assert [item["enabledBefore"] for item in summary["status"][repeater_type]] == [True, True]
                assert [item["addDisabledAfterSave"] for item in summary["status"][repeater_type]] == [False, False]
                assert [item["tileCountAfterSave"] for item in summary["status"][repeater_type]] == [1, 2]

            assert summary["experienceTitles"] == ["Experience Alpha", "Experience Beta"]
            assert summary["experienceSubtitles"] == ["Acme — New York", "Bravo — Austin"]
            assert summary["educationTitles"] == ["School One", "School Two"]
            assert summary["educationSubtitles"] == [
                "Bachelor of Science — Mathematics",
                "Master of Science — Computer Science",
            ]
            assert summary["skillTitles"] == ["Python", "Rust"]
            assert summary["skillSubtitles"] == ["Advanced", "Intermediate"]
            assert summary["languageTitles"] == ["English", "Spanish"]
            assert summary["languageSubtitles"] == ["Native", "Professional"]
            assert summary["licenseTitles"] == ["Series 7", "AWS SAA"]
            assert summary["licenseSubtitles"] == ["FINRA", "Amazon"]


@pytest.mark.asyncio
async def test_oracle_hcm_skill_type_dependency_and_no_results_localhost() -> None:
    """Skill entry should require a committed skill type and show no-results for invalid skill queries."""
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None

            summary = await _eval(page, """() => {
                document.querySelectorAll('.apply-flow-page').forEach(p => p.style.display = 'none');
                document.getElementById('page-3').style.display = 'block';
                document.querySelector('.apply-flow-profile-item-tile__new-tile[data-profile-type="skill"]')?.click();

                const chooseOption = (inputId, value) => {
                    const input = document.getElementById(inputId);
                    if (!input) return false;
                    const toggle = input.parentElement && input.parentElement.querySelector('.cx-select-toggle');
                    if (toggle) toggle.click();
                    const dropdownId = input.getAttribute('aria-controls');
                    const dropdown = dropdownId ? document.getElementById(dropdownId) : null;
                    if (!dropdown) return false;
                    const row = Array.from(dropdown.querySelectorAll('[role="row"]'))
                        .find(r => (r.getAttribute('data-value') || '').trim() === value);
                    if (!row) return false;
                    row.click();
                    return input.value === value;
                };

                const typeCommitted = chooseOption('skill-type-1', 'Technical');
                const skillInput = document.getElementById('skill-name-1');
                skillInput.focus();
                skillInput.value = 'Java/Java Servlets/JSTailwind CSS';
                skillInput.dispatchEvent(new Event('input', { bubbles: true }));
                const skillDropdown = document.getElementById('skill-name-1-dropdown');
                const emptyRow = skillDropdown.querySelector('.cx-select-dropdown__empty');

                return JSON.stringify({
                    typeCommitted,
                    emptyVisible: !!emptyRow && emptyRow.style.display !== 'none',
                    emptyText: emptyRow ? emptyRow.textContent.trim() : '',
                    visibleOptions: Array.from(skillDropdown.querySelectorAll('[role="row"]'))
                        .filter(r => r.style.display !== 'none' && r.getAttribute('data-empty-row') !== 'true')
                        .map(r => (r.getAttribute('data-value') || '').trim()),
                });
            }""")

            assert summary["typeCommitted"] is True
            assert summary["emptyVisible"] is True
            assert summary["emptyText"] == "No results were found."
            assert summary["visibleOptions"] == []


@pytest.mark.asyncio
async def test_oracle_hcm_hover_edit_and_remove_tile_localhost() -> None:
    """Saved repeater tiles should reveal actions on hover and support edit/remove flows."""
    async with managed_local_fixture_server() as url:
        async with managed_browser_session() as browser_session:
            tools = Tools()
            await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
            page = await browser_session.get_current_page()
            assert page is not None

            await page.evaluate("""() => {
                document.querySelectorAll('.apply-flow-page').forEach(p => p.style.display = 'none');
                document.getElementById('page-3').style.display = 'block';
                document.querySelector('.apply-flow-profile-item-tile__new-tile[data-profile-type="education"]')?.click();

                const chooseOption = (inputId, value) => {
                    const input = document.getElementById(inputId);
                    if (!input) return false;
                    const toggle = input.parentElement && input.parentElement.querySelector('.cx-select-toggle');
                    if (toggle) toggle.click();
                    const dropdownId = input.getAttribute('aria-controls');
                    const dropdown = dropdownId ? document.getElementById(dropdownId) : null;
                    if (!dropdown) return false;
                    const row = Array.from(dropdown.querySelectorAll('[role="row"]'))
                        .find(r => (r.getAttribute('data-value') || '').trim() === value);
                    if (!row) return false;
                    row.click();
                    return input.value === value;
                };

                const setText = (id, value) => {
                    const input = document.getElementById(id);
                    if (!input) return false;
                    input.focus();
                    input.value = value;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                };

                chooseOption('degree-1', 'Bachelor of Science');
                chooseOption('school-1', 'Adelphi University');
                setText('field-of-study-1', 'Mathematics');
                chooseOption('start-date-month-1', 'September');
                chooseOption('start-date-year-1', '2024');
                chooseOption('end-date-month-1', 'May');
                chooseOption('end-date-year-1', '2027');
                document.querySelector('.profile-inline-form[data-profile-type="education"][data-profile-index="1"] .profile-inline-form__save')?.click();
            }""")
            await asyncio.sleep(0.2)

            initial = await _eval(page, """() => {
                const tile = document.querySelector('#education-container .apply-flow-profile-item-tile--saved');
                const actions = tile ? tile.querySelector('.apply-flow-profile-item-tile__actions') : null;
                return JSON.stringify({
                    tileExists: !!tile,
                    title: tile?.querySelector('.apply-flow-profile-item-tile__summary-title')?.textContent.trim() || '',
                });
            }""")
            assert initial["tileExists"] is True
            assert initial["title"] == "Adelphi University"

            await page.evaluate("""() => {
                const tile = document.querySelector('#education-container .apply-flow-profile-item-tile--saved');
                const actions = tile ? tile.querySelector('.apply-flow-profile-item-tile__actions') : null;
                if (tile) tile.classList.add('apply-flow-profile-item-tile--show-actions');
                if (actions) {
                    actions.style.opacity = '1';
                    actions.style.pointerEvents = 'auto';
                }
            }""")
            await page.evaluate("""() => {
                const tile = document.querySelector('#education-container .apply-flow-profile-item-tile--saved');
                window.__toyOracleFixture?.openProfileTileEditor?.(tile);
            }""")
            edited = await _eval(page, """() => {
                const form = document.querySelector('.profile-inline-form[data-profile-type="education"][data-profile-index="1"]');
                return JSON.stringify({
                    formOpen: !!form,
                    school: document.getElementById('school-1')?.value || '',
                    fieldOfStudy: document.getElementById('field-of-study-1')?.value || '',
                    startMonth: document.getElementById('start-date-month-1')?.value || '',
                    startYear: document.getElementById('start-date-year-1')?.value || '',
                });
            }""")
            assert edited["formOpen"] is True
            assert edited["school"] == "Adelphi University"
            assert edited["fieldOfStudy"] == "Mathematics"
            assert edited["startMonth"] == "September"
            assert edited["startYear"] == "2024"

            await page.evaluate("""() => {
                const input = document.getElementById('field-of-study-1');
                input.value = 'Applied Mathematics';
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                document.querySelector('.profile-inline-form[data-profile-type="education"][data-profile-index="1"] .profile-inline-form__save')?.click();
            }""")
            await asyncio.sleep(0.2)

            updated = await _eval(page, """() => {
                const tile = document.querySelector('#education-container .apply-flow-profile-item-tile--saved');
                return JSON.stringify({
                    title: tile?.querySelector('.apply-flow-profile-item-tile__summary-title')?.textContent.trim() || '',
                    subtitle: tile?.querySelector('.apply-flow-profile-item-tile__summary-subtitle')?.textContent.trim() || '',
                    formOpen: !!document.querySelector('.profile-inline-form[data-profile-type="education"]'),
                });
            }""")
            assert updated["formOpen"] is False
            assert updated["title"] == "Adelphi University"
            assert updated["subtitle"] == "Bachelor of Science — Applied Mathematics"

            await page.evaluate("""() => {
                const tile = document.querySelector('#education-container .apply-flow-profile-item-tile--saved');
                const actions = tile ? tile.querySelector('.apply-flow-profile-item-tile__actions') : null;
                if (tile) tile.classList.add('apply-flow-profile-item-tile--show-actions');
                if (actions) {
                    actions.style.opacity = '1';
                    actions.style.pointerEvents = 'auto';
                }
            }""")
            await page.evaluate("""() => {
                const tile = document.querySelector('#education-container .apply-flow-profile-item-tile--saved');
                window.__toyOracleFixture?.removeProfileTile?.(tile);
            }""")
            removed = await _eval(page, """() => JSON.stringify({
                remainingTiles: document.querySelectorAll('#education-container .apply-flow-profile-item-tile--saved').length,
                addDisabled: !!document.querySelector('#education-container .apply-flow-profile-item-tile__new-tile')?.disabled
            })""")
            assert removed["remainingTiles"] == 0
            assert removed["addDisabled"] is False


# ---------------------------------------------------------------------------
# 9. Radio group — Disability radio buttons
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oracle_hcm_radio_disability(httpserver, toy_html: str) -> None:
    """Disability radio buttons should work with Oracle's custom styling."""
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None

        # Show page 4 (More About You)
        await page.evaluate("""() => {
            document.querySelectorAll('.apply-flow-page').forEach(p => p.style.display = 'none');
            const p4 = document.getElementById('page-4');
            if (p4) p4.style.display = 'block';
        }""")

        # Find and click a radio button
        result = await _eval(page, """() => {
            const radios = document.querySelectorAll('.apply-flow-input-radio-control');
            if (radios.length === 0) return JSON.stringify({ found: false });
            radios[0].click();
            return JSON.stringify({
                found: true,
                checked: radios[0].checked,
                total: radios.length
            });
        }""")
        assert result["found"], "No radio buttons found on page 4"
        assert result["checked"], "Radio should be checked after click"
        assert result["total"] >= 3, f"Expected >= 3 radio options, got {result['total']}"


# ---------------------------------------------------------------------------
# 10. DomHand executor + platform smoke (Oracle toy fixture)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oracle_combobox_fills_searchable_selects_via_keyboard(httpserver, toy_html: str) -> None:
    """`_fill_oracle_combobox_outcome` should commit a cx-select Degree value on the toy fixture."""
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    from typing import Any

    from playwright.async_api import ElementHandle, async_playwright
    from playwright.async_api import Error as PlaywrightError

    from ghosthands.actions.views import FormField as ActionFormField
    from ghosthands.dom.fill_executor import _fill_oracle_combobox_outcome

    async def _element_handle_press_sequentially(
        self: ElementHandle,
        text: str,
        delay: int = 0,
        **_kw: Any,
    ) -> None:
        await self.type(text, delay=float(delay) if delay else None)

    # Playwright `ElementHandle` exposes `type()`; DomHand calls `press_sequentially` (Locator API).
    _prev_ps = getattr(ElementHandle, "press_sequentially", None)
    ElementHandle.press_sequentially = _element_handle_press_sequentially  # type: ignore[method-assign]

    # `_fill_oracle_combobox_outcome` uses Playwright element APIs (`query_selector`, `press_sequentially`).
    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except PlaywrightError as exc:
                pytest.skip(f"Playwright Chromium not installed or not runnable: {exc}")

            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")

                await page.evaluate("""() => {
                    document.querySelectorAll('.apply-flow-page').forEach(p => { p.style.display = 'none'; });
                    const p3 = document.getElementById('page-3');
                    if (p3) p3.style.display = 'block';
                }""")
                await page.evaluate("""() => {
                    const btn = document.querySelector(
                        '#education-container .apply-flow-profile-item-tile__new-tile[data-profile-type="education"]'
                    );
                    if (btn) btn.click();
                }""")
                for _ in range(30):
                    visible = await page.evaluate(
                        """() => !!document.querySelector(
                            '.profile-inline-form[data-profile-type="education"] input#degree-1'
                        )"""
                    )
                    if visible:
                        break
                    await asyncio.sleep(0.1)
                assert await page.evaluate(
                    """() => !!document.querySelector(
                        '.profile-inline-form[data-profile-type="education"]'
                    )"""
                ), "Education inline form did not open"

                field = ActionFormField(
                    field_id="degree-1",
                    name="Degree",
                    field_type="select",
                    section="",
                    required=True,
                    options=[
                        "Bachelor's",
                        "Bachelor of Science",
                        "Master's",
                        "Master of Science",
                        "PhD",
                        "Associate's",
                        "High School Diploma",
                    ],
                    current_value="",
                    is_native=False,
                    is_multi_select=False,
                    has_calendar_trigger=False,
                )
                outcome = await _fill_oracle_combobox_outcome(
                    page, field, "Bachelor of Science", "toy-oracle-degree"
                )
                assert outcome.success, f"Expected combobox fill success, got {outcome}"

                await asyncio.sleep(2.0)
                persisted = await _eval(
                    page,
                    """() => {
                        const el = document.querySelector('#degree-1');
                        if (!el) return JSON.stringify({ found: false });
                        const v = (el.value || '').trim();
                        const c = (el.dataset.committedValue || '').trim();
                        return JSON.stringify({ found: true, value: v, committed: c });
                    }""",
                )
                assert persisted["found"]
                effective = (persisted.get("committed") or persisted.get("value") or "").strip()
                assert "bachelor" in effective.lower() and "science" in effective.lower(), persisted
            finally:
                await browser.close()
    finally:
        if _prev_ps is None:
            delattr(ElementHandle, "press_sequentially")
        else:
            ElementHandle.press_sequentially = _prev_ps  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_scoped_fill_dedup_blocks_repeat_calls(httpserver, toy_html: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Second domhand_fill with the same heading_boundary should hit the scoped dedup guard."""
    monkeypatch.setenv("GH_USER_PROFILE_PATH", str(_PROFILE))
    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    from unittest.mock import AsyncMock, patch

    from ghosthands.actions.domhand_fill import domhand_fill
    from ghosthands.actions.views import DomHandFillParams

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)

        browser_session._gh_completed_scoped_fills = {("test_ctx", "education"): {"filled_count": 2}}  # type: ignore[attr-defined]
        browser_session._gh_completed_scoped_page = "test_ctx"  # type: ignore[attr-defined]

        with patch(
            "ghosthands.actions.domhand_fill._get_page_context_key",
            new_callable=AsyncMock,
            return_value="test_ctx",
        ):
            result = await domhand_fill(
                DomHandFillParams(heading_boundary="education"),
                browser_session,
            )

    blob = (result.extracted_content or "") + (result.error or "")
    assert "already" in blob.lower() or "COMPLETE" in blob


@pytest.mark.asyncio
async def test_already_filled_returns_complete_message(
    httpserver,
    toy_html: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When visible fields already match the profile, summary should report section COMPLETE."""
    prof = json.loads(_PROFILE.read_text(encoding="utf-8"))
    prof["address"] = {
        "street": "1 New York Plaza",
        "city": "New York",
        "state": "NY",
        "zip": "10004",
        "country": "United States",
    }
    profile_path = tmp_path / "aligned_profile.json"
    profile_path.write_text(json.dumps(prof), encoding="utf-8")
    monkeypatch.setenv("GH_USER_PROFILE_PATH", str(profile_path))

    httpserver.expect_request("/index.html").respond_with_data(
        toy_html, content_type="text/html; charset=utf-8"
    )
    url = httpserver.url_for("/index.html")

    from unittest.mock import AsyncMock, patch

    from ghosthands.actions.domhand_fill import domhand_fill
    from ghosthands.actions.views import DomHandFillParams

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None

        await page.evaluate("""() => {
            document.querySelectorAll('.apply-flow-page').forEach(p => { p.style.display = 'none'; });
            const p1 = document.getElementById('page-1');
            if (p1) p1.style.display = 'block';
        }""")

        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())

        await page.evaluate("""() => {
            function setCx(el, text) {
                if (!el) return;
                el.value = text;
                el.dataset.committedValue = text;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.setAttribute('aria-invalid', 'false');
            }
            const mr = Array.from(
                document.querySelectorAll('.cx-select-pills-container .cx-select-pill-section')
            ).find(p => p.textContent.trim() === 'Mr');
            if (mr) mr.click();

            const setText = (id, v) => {
                const el = document.getElementById(id);
                if (!el) return;
                el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            };
            setText('legal-first-name', 'Ruiyang');
            setText('legal-middle-name', '');
            setText('legal-last-name', 'Chen');
            setText('preferred-first-name', 'Ringo');
            setText('preferred-last-name', 'Chen');
            setText('email-field', 'rc5663@nyu.edu');
            setText('phone-number', '6466789391');

            setCx(document.getElementById('country-input'), 'United States');
            setCx(document.getElementById('addr-line1'), '1 New York Plaza');
            setCx(document.getElementById('zip-input'), '10004');
            setCx(document.getElementById('city-input'), 'New York');
            setCx(document.getElementById('state-input'), 'New York');

            document.querySelectorAll('.input-row--invalid').forEach(row => {
                row.classList.remove('input-row--invalid');
            });
            document.querySelectorAll('.input-row__validation').forEach(v => {
                v.style.display = 'none';
            });
        }""")

        with patch("ghosthands.actions.domhand_fill._generate_answers", new_callable=AsyncMock) as gen_mock:
            gen_mock.return_value = ({}, 0, 0, 0.0, None)
            result = await domhand_fill(DomHandFillParams(), browser_session)

    meta = result.metadata or {}
    log_summary = str(meta.get("domhand_fill_log_summary") or "")
    concise = (result.extracted_content or "") + (result.error or "")
    combined = concise + "\n" + log_summary
    assert "COMPLETE" in combined or "already have correct values" in combined.lower(), combined[:2000]


def test_oracle_platform_detected_from_fixture_url() -> None:
    """URL-based platform detection for Oracle HCM and Workday."""
    from ghosthands.platforms import detect_platform

    assert (
        detect_platform(
            "https://fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/12345"
        )
        == "oracle"
    )
    assert detect_platform("https://wd5.myworkday.com/wday/authgwy/company/login.htmld") == "workday"
