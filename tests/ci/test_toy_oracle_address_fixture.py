"""Smoke tests for ``examples/toy-oracle-address/index.html``.

Locks in Oracle-style **address cluster** behavior:
  • Address Line 1 = combobox + ``role="gridcell"`` suggestions
  • ZIP / City / County depend on committing a suggestion (trusted click)

  uv run pytest tests/ci/test_toy_oracle_address_fixture.py -v
"""

from __future__ import annotations

import asyncio
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
from ghosthands.dom.fill_executor import _fill_select_field_outcome
from ghosthands.dom.shadow_helpers import ensure_helpers

_FIXTURE = Path(__file__).resolve().parent.parent.parent / "examples" / "toy-oracle-address" / "index.html"


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
async def test_toy_oracle_address_gridcell_commit_fills_dependents(httpserver, toy_html: str) -> None:
    """Manual flow: typing opens grid; clicking gridcell commits ZIP/City/County."""
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
        await page.evaluate(
            """() => {
            const e = document.getElementById('addr-line1');
            e.focus();
            e.click();
        }""",
        )
        await asyncio.sleep(0.2)
        await page.evaluate(
            """() => {
            const e = document.getElementById('addr-line1');
            e.value = '13600';
            e.dispatchEvent(new Event('input', { bubbles: true }));
        }""",
        )
        await asyncio.sleep(0.25)
        has_cell = await page.evaluate(
            """() => !!document.querySelector('#addr-suggest-panel [role="gridcell"]')""",
        )
        assert has_cell
        text = await page.evaluate(
            """() => {
            const c = document.querySelector('#addr-suggest-panel [role="gridcell"]');
            return c ? c.textContent.trim() : '';
        }""",
        )
        assert "BROCKMEYER" in text.upper()
        clicked = await page.evaluate(
            """() => {
            const cell = document.querySelector('#addr-suggest-panel [role="gridcell"]');
            if (!cell) return false;
            cell.closest('[role="row"]').click();
            return true;
        }""",
        )
        assert clicked
        assert await page.evaluate("() => document.getElementById('zip-field').value") == "20151"
        assert await page.evaluate("() => document.getElementById('city-field').value") == "CHANTILLY"
        assert await page.evaluate("() => document.getElementById('county-field').value") == "Fairfax"


@pytest.mark.asyncio
async def test_toy_oracle_address_domhand_fill_select_commits_address_line1(
    httpserver,
    toy_html: str,
) -> None:
    """DomHand should fill Address Line 1 combobox and commit a gridcell so dependents populate."""
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
        await ensure_helpers(page)
        await page.evaluate(_build_inject_helpers_js())
        fields = await extract_visible_form_fields(page)
        addr_field = next(
            (f for f in fields if f.field_type == "select" and "address line 1" in _preferred_field_label(f).lower()),
            None,
        )
        assert addr_field is not None, [(_preferred_field_label(f), f.field_type) for f in fields]

        result = await _fill_select_field_outcome(
            page,
            addr_field,
            "13600 Brockmeyer Ct",
            "[Address Line 1]",
            browser_session=browser_session,
        )
        assert result.success is True, result

        zip_val = await page.evaluate("() => document.getElementById('zip-field').value")
        city_val = await page.evaluate("() => document.getElementById('city-field').value")
        assert zip_val == "20151", f"expected ZIP backfill, got {zip_val!r}"
        assert city_val == "CHANTILLY", f"expected city backfill, got {city_val!r}"
