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

import json
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright

from ghosthands.actions.domhand_fill import (
    _build_inject_helpers_js,
    _preferred_field_label,
    _read_field_value,
    extract_visible_form_fields,
)
from ghosthands.dom.fill_browser_scripts import (
    _CLICK_RADIO_OPTION_JS,
    _GET_GROUP_OPTION_TARGET_JS,
)
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
            blob = " ".join(
                f"{_preferred_field_label(f)} {f.section} {f.name}".lower() for f in fields
            )
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
