"""Smoke tests for ``tests/fixtures/domhand_dropdown_control_lab.html``.

Manual browsing shows a static fixture until you interact — the black log at the
bottom records clicks. For DomHand, run this file so Playwright serves the HTML
and runs extraction (no separate ``python -m http.server`` needed).

  uv run playwright install chromium   # once per machine
  uv run pytest tests/ci/test_domhand_lab_fixture.py -v

Chromium still speaks CDP under Playwright; DomHand does not give you “no CDP”.
These tests **do** lock in the painful part you care about: **shadow-piercing via
injected ``window.__ff``** (``queryAll`` / ``allRoots``) and **``extract_visible_form_fields``**
seeing controls inside the open shadow root. If someone “simplifies” extraction
to ``document.querySelector`` only, these tests fail immediately.

Section 10 (composite phone: country LOV + ``tel``) locks in **readback** for
Oracle-style country widgets where the visible label lives on a readonly
``role="textbox"`` inside ``role="combobox"``.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools
from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    _preferred_field_label,
    _read_field_value,
    extract_visible_form_fields,
)
from ghosthands.actions.domhand_interact_control import domhand_interact_control
from ghosthands.actions.views import DomHandInteractControlParams, FormField
from ghosthands.dom.fill_browser_scripts import (
    _CLICK_RADIO_OPTION_JS,
    _GET_GROUP_OPTION_TARGET_JS,
)
from ghosthands.dom.fill_executor import _fill_custom_dropdown_outcome, _fill_select_field_outcome
from ghosthands.dom.shadow_helpers import ensure_helpers

_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "domhand_dropdown_control_lab.html"


async def _launch_chromium(playwright):
    try:
        return await playwright.chromium.launch(headless=True)
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip(
                "Playwright browser missing; install with: uv run playwright install chromium",
            )
        raise


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
def lab_html() -> str:
    assert _FIXTURE.is_file(), f"missing fixture: {_FIXTURE}"
    return _FIXTURE.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_domhand_lab_domhand_extract_non_empty(httpserver, lab_html: str) -> None:
    httpserver.expect_request("/domhand_dropdown_control_lab.html").respond_with_data(
        lab_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/domhand_dropdown_control_lab.html")

    async with async_playwright() as p:
        browser = await _launch_chromium(p)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())
            fields = await extract_visible_form_fields(page)
            assert len(fields) >= 5, f"expected many fields, got {len(fields)}"
            names = {_preferred_field_label(f).lower() for f in fields}
            assert any("acme" in n or "previously worked" in n for n in names), names
            # Shadow fixture (section 9): must appear — proves extract walks shadow roots, not document-only.
            blob = " ".join(f"{_preferred_field_label(f)} {f.section} {f.name}".lower() for f in fields)
            assert "shadow" in blob, f"expected shadow DOM fields in extract, got labels blob={blob[:500]!r}"
            assert "country code" in blob, f"expected section 10 phone LOV in extract, blob={blob[:400]!r}"
            assert "main number" in blob or "phone" in blob, blob
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_domhand_lab_composite_phone_country_readback(httpserver, lab_html: str) -> None:
    """Section 10: Oracle-style country LOV stores label on readonly textbox, not combobox value."""
    httpserver.expect_request("/domhand_dropdown_control_lab.html").respond_with_data(
        lab_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/domhand_dropdown_control_lab.html")

    async with async_playwright() as p:
        browser = await _launch_chromium(p)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())
            fields = await extract_visible_form_fields(page)
            cc = next(
                (
                    f
                    for f in fields
                    if f.field_type == "select"
                    and "country" in _preferred_field_label(f).lower()
                    and "code" in _preferred_field_label(f).lower()
                ),
                None,
            )
            assert cc is not None, (
                f"expected a select-like Country code field; got {[_preferred_field_label(f) for f in fields]}"
            )
            read = await _read_field_value(page, cc.field_id)
            low = read.lower()
            assert "united states" in low or "+1" in low, f"readback={read!r}"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_domhand_lab_visa_dependency_requires_na_commit(
    httpserver,
    lab_html: str,
) -> None:
    """Selecting the sponsor pill must reveal and commit the dependent visa-type combobox."""
    httpserver.expect_request("/domhand_dropdown_control_lab.html").respond_with_data(
        lab_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/domhand_dropdown_control_lab.html")

    async with managed_browser_session() as browser_session:
        page = await browser_session.get_current_page()
        assert page is not None
        await page.goto(url)
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())

        before_fields = await extract_visible_form_fields(page)
        assert not any("visa type" in _preferred_field_label(field).lower() for field in before_fields), [
            _preferred_field_label(field) for field in before_fields
        ]

        sponsor_field = next(
            (
                field
                for field in before_fields
                if field.field_type == "button-group" and "visa sponsorship" in _preferred_field_label(field).lower()
            ),
            None,
        )
        assert sponsor_field is not None, [(_preferred_field_label(field), field.field_type) for field in before_fields]

        sponsor_result = await domhand_interact_control(
            DomHandInteractControlParams(
                field_label=_preferred_field_label(sponsor_field),
                desired_value="No",
                field_id=sponsor_field.field_id,
                field_type=sponsor_field.field_type,
            ),
            browser_session,
        )
        assert sponsor_result.error is None, sponsor_result

        revealed = False
        for _ in range(10):
            revealed = bool(
                await page.evaluate(
                    "() => !!document.getElementById('visa-type-block') && document.getElementById('visa-type-block').hidden === false"
                )
            )
            if revealed:
                break
            await asyncio.sleep(0.1)
        assert revealed is True
        await browser_session.get_browser_state_summary()

        after_fields = await extract_visible_form_fields(page)
        visa_field = next(
            (field for field in after_fields if "visa type" in _preferred_field_label(field).lower()),
            None,
        )
        assert visa_field is not None, [(_preferred_field_label(field), field.field_type) for field in after_fields]

        visa_result = await _fill_custom_dropdown_outcome(
            page,
            visa_field,
            "N/A",
            "[Visa type]",
            browser_session=browser_session,
        )
        assert visa_result.success is True, visa_result

        assert (
            await page.evaluate("() => document.getElementById('visa-type-input').getAttribute('data-committed-value')")
            == "N/A"
        )
        assert await page.evaluate("() => document.getElementById('visa-type-input').value") == "N/A"
        assert (
            await page.evaluate("() => document.getElementById('visa-type-input').getAttribute('aria-expanded')")
            == "false"
        )


@pytest.mark.asyncio
async def test_domhand_lab_shadow_piercing_ff_not_document(httpserver, lab_html: str) -> None:
    """``#sh-dept`` and shadow radios must be invisible to document scope but visible to ``__ff``."""
    httpserver.expect_request("/domhand_dropdown_control_lab.html").respond_with_data(
        lab_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/domhand_dropdown_control_lab.html")

    probe_js = """() => {
	const docSelect = document.querySelector("#sh-dept");
	const ff = window.__ff;
	if (!ff || !ff.queryAll) {
		return JSON.stringify({ ok: false, reason: "no __ff.queryAll" });
	}
	const ffSelect = ff.queryAll("#sh-dept");
	const ffRadios = ff.queryAll('input[type="radio"][name="sh_prior"]');
	return JSON.stringify({
		ok: true,
		documentMissesShadowSelect: docSelect === null,
		ffShadowSelectCount: ffSelect.length,
		ffShadowRadioCount: ffRadios.length,
	});
}"""

    async with async_playwright() as p:
        browser = await _launch_chromium(p)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())
            raw = await page.evaluate(probe_js)
            data = json.loads(raw) if isinstance(raw, str) else raw
            assert data.get("ok"), data
            assert data.get("documentMissesShadowSelect") is True, (
                "expected #sh-dept to live only under shadow root (document query must miss)"
            )
            assert int(data.get("ffShadowSelectCount") or 0) >= 1, data
            assert int(data.get("ffShadowRadioCount") or 0) >= 2, data
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_domhand_lab_async_combo_no_options_then_search(httpserver, lab_html: str) -> None:
    """Section 6: empty query shows 'No options'; typing reveals real options."""
    httpserver.expect_request("/domhand_dropdown_control_lab.html").respond_with_data(
        lab_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/domhand_dropdown_control_lab.html")

    async with async_playwright() as p:
        browser = await _launch_chromium(p)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            await page.locator("#async-skill-input").click()
            await page.wait_for_timeout(150)
            opt_texts = await page.locator("#async-skill-list [role='option']").all_inner_texts()
            assert opt_texts, "listbox should open on focus"
            assert any("no options" in t.lower() for t in opt_texts), opt_texts

            await page.fill("#async-skill-input", "py")
            await page.wait_for_timeout(150)
            opt_texts2 = await page.locator("#async-skill-list [role='option']").all_inner_texts()
            assert any("python" in t.lower() for t in opt_texts2), opt_texts2
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_extract_visible_form_fields_keeps_labeled_search_input_inside_dialog(httpserver) -> None:
    html = """
    <html>
      <body>
        <div class="form-group" data-automation-id="formField">
          <div class="field-shell">
            <div class="field-header">
              <div class="section-title">Skills</div>
              <div data-automation-id="fieldLabel">Type to Add Skills</div>
            </div>
            <div role="dialog" class="skill-dialog">
              <div class="inner-search-wrap">
                <input
                  id="skills-search"
                  type="search"
                  placeholder="Search skills"
                  autocomplete="off"
                />
              </div>
              <ul role="listbox">
                <li role="option">Python</li>
                <li role="option">React</li>
              </ul>
            </div>
          </div>
        </div>
        <div class="form-group" data-automation-id="formField">
          <div role="dialog" class="generic-search-dialog">
            <input
              id="generic-search"
              type="search"
              placeholder="Search"
              autocomplete="off"
            />
            <ul role="listbox">
              <li role="option">Ignore me</li>
            </ul>
          </div>
        </div>
      </body>
    </html>
    """
    httpserver.expect_request("/labeled-search-field.html").respond_with_data(
        html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/labeled-search-field.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())

        fields = await extract_visible_form_fields(page)
        labels = [(_preferred_field_label(field), field.field_type) for field in fields]

        assert ("Type to Add Skills", "search") in labels, labels
        assert all(label != "Search" for label, _ in labels), labels


@pytest.mark.asyncio
async def test_domhand_lab_domhand_fill_async_skill_uses_trusted_click(httpserver, lab_html: str) -> None:
    """Searchable combobox options must be committed via trusted click, not DOM-only text injection."""
    httpserver.expect_request("/domhand_dropdown_control_lab.html").respond_with_data(
        lab_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/domhand_dropdown_control_lab.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())
        fields = await extract_visible_form_fields(page)
        skill_field = next(
            (
                field
                for field in fields
                if field.field_type == "select" and "skill" in _preferred_field_label(field).lower()
            ),
            None,
        )
        assert skill_field is not None, [(_preferred_field_label(field), field.field_type) for field in fields]

        result = await _fill_select_field_outcome(
            page,
            skill_field,
            "Python",
            "[Skill]",
            browser_session=browser_session,
        )

        assert result.success is True, result
        assert await page.evaluate("() => document.getElementById('async-skill-input').value") == "Python"


@pytest.mark.asyncio
async def test_domhand_lab_workday_skills_prompt_search_contract(httpserver, lab_html: str) -> None:
    """Workday skills should show No Items until promptSearchButton is clicked."""
    httpserver.expect_request("/domhand_dropdown_control_lab.html").respond_with_data(
        lab_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/domhand_dropdown_control_lab.html")

    async with async_playwright() as p:
        browser = await _launch_chromium(p)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded")

            await page.locator("#wd-skills-input").click()
            await page.wait_for_timeout(150)
            initial = await page.locator("#wd-skills-list [role='option']").all_inner_texts()
            assert initial == ["No Items."], initial

            await page.locator("#wd-skills-prompt").click()
            await page.locator("#wd-skills-input").fill("Python")
            await page.wait_for_timeout(150)
            after_prompt = await page.locator("#wd-skills-list [role='option']").all_inner_texts()
            assert any("python" in text.lower() for text in after_prompt), after_prompt
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_domhand_lab_workday_skills_rejects_blob_query(httpserver, lab_html: str) -> None:
    """Workday skills should reject comma-joined multi-skill blobs with No Items."""
    httpserver.expect_request("/domhand_dropdown_control_lab.html").respond_with_data(
        lab_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/domhand_dropdown_control_lab.html")

    async with async_playwright() as p:
        browser = await _launch_chromium(p)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded")

            await page.locator("#wd-skills-prompt").click()
            await page.locator("#wd-skills-input").fill("Python,Java,React")
            await page.wait_for_timeout(150)
            after_blob = await page.locator("#wd-skills-list [role='option']").all_inner_texts()
            assert after_blob == ["No Items."], after_blob
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_domhand_lab_workday_skills_requires_same_skill_alias_query_for_alias_only_catalog(
    httpserver, lab_html: str
) -> None:
    """Alias-only catalog entries should require a deterministic same-skill fallback query."""
    httpserver.expect_request("/domhand_dropdown_control_lab.html").respond_with_data(
        lab_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/domhand_dropdown_control_lab.html")

    async with async_playwright() as p:
        browser = await _launch_chromium(p)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded")

            await page.locator("#wd-skills-prompt").click()
            await page.locator("#wd-skills-input").fill("React")
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(250)
            after_react = await page.locator("#wd-skills-list [role='option']").all_inner_texts()
            assert after_react == ["No Items."], after_react

            await page.locator("#wd-skills-input").fill("ReactJS")
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(250)
            after_alias = await page.locator("#wd-skills-list [role='option']").all_inner_texts()
            assert any("reactjs" in text.lower() for text in after_alias), after_alias
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_domhand_lab_workday_skills_fill_commits_real_tokens(httpserver, lab_html: str) -> None:
    """Skill fill must create chips/tokens, including same-skill alias commits."""
    httpserver.expect_request("/domhand_dropdown_control_lab.html").respond_with_data(
        lab_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/domhand_dropdown_control_lab.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())

        fields = await extract_visible_form_fields(page)
        skill_field = next(
            (
                field
                for field in fields
                if field.field_type == "select" and "type to add skills" in _preferred_field_label(field).lower()
            ),
            None,
        )
        assert skill_field is not None, [(_preferred_field_label(field), field.field_type) for field in fields]

        result = await _fill_select_field_outcome(
            page,
            skill_field,
            "Python, React, MySQLDB",
            "[Workday Skills]",
            browser_session=browser_session,
        )

        assert result.success is True, result
        assert await page.evaluate("() => document.getElementById('wd-skills-input').value") == ""
        assert (
            await page.evaluate("() => document.getElementById('wd-skills-selection-label').textContent.trim()")
            == "3 items selected"
        )
        chips_raw = await page.evaluate(
            "() => Array.from(document.querySelectorAll('#wd-skills-chip-row [data-automation-id=\"selectedItem\"]')).map((node) => node.textContent.replace(/\\s+/g, ' ').trim())"
        )
        chips = json.loads(chips_raw) if isinstance(chips_raw, str) else chips_raw
        assert any("Python" in chip for chip in chips), chips
        assert any("ReactJS" in chip for chip in chips), chips
        assert any("MySQL" in chip for chip in chips), chips


@pytest.mark.asyncio
async def test_domhand_lab_workday_language_proficiency_maps_semantic_value(httpserver, lab_html: str) -> None:
    """Comprehension/Overall should commit the live Workday option for semantic profile values."""
    httpserver.expect_request("/domhand_dropdown_control_lab.html").respond_with_data(
        lab_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/domhand_dropdown_control_lab.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())

        fields = await extract_visible_form_fields(page)
        labels = {field.field_id: _preferred_field_label(field) for field in fields}
        comprehension = next(
            (field for field in fields if labels[field.field_id].lower() == "comprehension"),
            None,
        )
        overall = next(
            (field for field in fields if labels[field.field_id].lower() == "overall"),
            None,
        )
        assert comprehension is not None, labels
        assert overall is not None, labels

        comprehension_result = await _fill_select_field_outcome(
            page,
            comprehension,
            "Native / bilingual",
            "[Languages Comprehension]",
            browser_session=browser_session,
        )
        overall_result = await _fill_select_field_outcome(
            page,
            overall,
            "Native / bilingual",
            "[Languages Overall]",
            browser_session=browser_session,
        )

        assert comprehension_result.success is True, comprehension_result
        assert overall_result.success is True, overall_result
        assert await page.evaluate("() => document.getElementById('wd-lang2-comprehension').value") == "5 - Fluent"
        assert await page.evaluate("() => document.getElementById('wd-lang2-overall').value") == "5 - Fluent"
        assert await page.evaluate("() => document.getElementById('wd-lang2-reading').value") == "5 - Fluent"
        assert await page.evaluate("() => document.getElementById('wd-lang2-speaking').value") == "5 - Fluent"
        assert await page.evaluate("() => document.getElementById('wd-lang2-writing').value") == "5 - Fluent"


@pytest.mark.asyncio
async def test_domhand_lab_domhand_fill_shadow_async_skill_uses_trusted_click(
    httpserver,
    lab_html: str,
) -> None:
    """Open-shadow searchable comboboxes should reuse the working multi-select/select path."""
    httpserver.expect_request("/domhand_dropdown_control_lab.html").respond_with_data(
        lab_html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/domhand_dropdown_control_lab.html")

    async with managed_browser_session() as browser_session:
        tools = Tools()
        await tools.navigate(url=url, new_tab=False, browser_session=browser_session)
        page = await browser_session.get_current_page()
        assert page is not None
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())
        fields = await extract_visible_form_fields(page)
        skill_field = next(
            (
                field
                for field in fields
                if field.field_type == "select" and "shadow skill" in _preferred_field_label(field).lower()
            ),
            None,
        )
        assert skill_field is not None, [(_preferred_field_label(field), field.field_type) for field in fields]

        result = await _fill_select_field_outcome(
            page,
            skill_field,
            "Python",
            "[Shadow Skill]",
            browser_session=browser_session,
        )

        assert result.success is True, result
        assert (
            await page.evaluate(
                "() => document.getElementById('shadow-lab-host').shadowRoot.getElementById('sh-skill-input').value"
            )
            == "Python"
        )


@pytest.mark.asyncio
async def test_domhand_fill_scopes_dropdown_option_click_to_own_field(httpserver) -> None:
    html = """
    <html>
      <body>
        <div class="form-group">
          <label for="state-a">State A</label>
          <input id="state-a" data-ff-id="ff-state-a" type="text" role="combobox" aria-controls="state-a-list" aria-expanded="true" aria-haspopup="listbox" />
          <ul id="state-a-list" role="listbox">
            <li role="option">Virginia</li>
            <li role="option">Nevada</li>
          </ul>
        </div>
        <div class="form-group">
          <label for="state-b">State B</label>
          <input id="state-b" data-ff-id="ff-state-b" type="text" role="combobox" aria-controls="state-b-list" aria-expanded="true" aria-haspopup="listbox" />
          <ul id="state-b-list" role="listbox">
            <li role="option">Virginia</li>
            <li role="option">California</li>
          </ul>
        </div>
        <script>
              function bind(inputId, listId) {
                const input = document.getElementById(inputId);
                const list = document.getElementById(listId);
                list.querySelectorAll('[role="option"]').forEach((option) => {
                  option.addEventListener('click', (event) => {
                    if (!event.isTrusted) return;
                    input.value = option.textContent.trim();
                    input.setAttribute('data-committed-value', input.value);
                    input.setAttribute('aria-expanded', 'false');
                    list.hidden = true;
                  });
                });
              }
          bind('state-a', 'state-a-list');
          bind('state-b', 'state-b-list');
        </script>
      </body>
    </html>
    """

    httpserver.expect_request("/scoped-dropdowns.html").respond_with_data(
        html,
        content_type="text/html; charset=utf-8",
    )
    url = httpserver.url_for("/scoped-dropdowns.html")

    async with managed_browser_session() as browser_session:
        page = await browser_session.get_current_page()
        assert page is not None
        await page.goto(url)
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())

        field = FormField(
            field_id="ff-state-b",
            name="State B",
            field_type="select",
            section="Address",
            required=True,
            is_native=False,
        )

        result = await _fill_custom_dropdown_outcome(
            page,
            field,
            "Virginia",
            "[State B]",
            browser_session=browser_session,
        )

        assert result.success is True, result
        assert await page.evaluate("() => document.getElementById('state-a').value") == ""
        assert await page.evaluate("() => document.getElementById('state-b').value") == "Virginia"


@pytest.mark.asyncio
async def test_group_option_scripts_use_label_for_native_checkbox_options() -> None:
    html = """
    <html>
      <body>
        <div class="checkbox-group">
          <div class="checkbox__input">
            <input id="sponsor-no" data-ff-id="ff-sponsor-no" type="checkbox" />
          </div>
          <label for="sponsor-no">No, I do not require sponsorship either now or in the future</label>
        </div>
      </body>
    </html>
    """

    async with async_playwright() as p:
        browser = await _launch_chromium(p)
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            await ensure_helpers(page)
            await page.evaluate(_build_inject_helpers_js())

            raw_click = await page.evaluate(
                f"([ffId, text]) => ({_CLICK_RADIO_OPTION_JS})(ffId, text)",
                [
                    "ff-sponsor-no",
                    "No, I do not require sponsorship either now or in the future",
                ],
            )
            click_data = json.loads(raw_click) if isinstance(raw_click, str) else raw_click
            assert click_data.get("clicked") is True, click_data
            assert await page.locator("#sponsor-no").is_checked() is True

            await page.evaluate("() => { document.querySelector('#sponsor-no').checked = false; }")
            raw_target = await page.evaluate(
                f"([ffId, text]) => ({_GET_GROUP_OPTION_TARGET_JS})(ffId, text)",
                [
                    "ff-sponsor-no",
                    "No, I do not require sponsorship either now or in the future",
                ],
            )
            target = json.loads(raw_target) if isinstance(raw_target, str) else raw_target
            assert target.get("found") is True, target

            await page.mouse.click(target["x"], target["y"])
            assert await page.locator("#sponsor-no").is_checked() is True
        finally:
            await browser.close()
