from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from ghosthands.agent.hooks import _FINAL_SUBMIT_GUARD_JS, _READ_AND_CLEAR_FINAL_SUBMIT_BLOCK_JS
from ghosthands.dom.fill_browser_scripts import _CLICK_CHECKBOX_GROUP_JS


@pytest.mark.asyncio
async def test_review_guard_blocks_all_submit_mechanisms_but_allows_next_navigation() -> None:
    async with async_playwright() as playwright:
        launch_options: dict[str, object] = {"headless": True}
        if not Path(playwright.chromium.executable_path).exists():
            cache_roots = [Path.home() / "Library/Caches/ms-playwright", Path.home() / ".cache/ms-playwright"]
            cached = [path for root in cache_roots if root.exists() for path in root.rglob("chrome-headless-shell")]
            if cached:
                launch_options["executable_path"] = str(sorted(cached)[-1])
        browser = await playwright.chromium.launch(**launch_options)
        page = await browser.new_page()
        await page.set_content(
            """
            <form id="candidate" onsubmit="window.applicationSubmits += 1; return false">
              <input name="name" value="Ada">
              <button id="apply" type="submit">Apply</button>
            </form>
            <form id="navigation" onsubmit="window.navigationSubmits += 1; return false">
              <button id="next" type="submit">Next</button>
            </form>
            <script>window.applicationSubmits = 0; window.navigationSubmits = 0;</script>
            """
        )
        await page.evaluate(_FINAL_SUBMIT_GUARD_JS)

        await page.click("#apply")
        click_block = await page.evaluate(_READ_AND_CLEAR_FINAL_SUBMIT_BLOCK_JS)
        await page.evaluate("document.querySelector('#candidate').requestSubmit()")
        request_block = await page.evaluate(_READ_AND_CLEAR_FINAL_SUBMIT_BLOCK_JS)
        await page.evaluate("document.querySelector('#candidate').submit()")
        submit_block = await page.evaluate(_READ_AND_CLEAR_FINAL_SUBMIT_BLOCK_JS)
        await page.click("#next")
        next_block = await page.evaluate(_READ_AND_CLEAR_FINAL_SUBMIT_BLOCK_JS)

        assert click_block["blocked"] is True
        assert request_block["blocked"] is True
        assert submit_block["blocked"] is True
        assert await page.evaluate("window.applicationSubmits") == 0
        assert next_block is None
        assert await page.evaluate("window.navigationSubmits") == 1
        await browser.close()


@pytest.mark.asyncio
async def test_checkbox_group_script_selects_every_requested_answer() -> None:
    async with async_playwright() as playwright:
        launch_options: dict[str, object] = {"headless": True}
        if not Path(playwright.chromium.executable_path).exists():
            cache_roots = [Path.home() / "Library/Caches/ms-playwright", Path.home() / ".cache/ms-playwright"]
            cached = [path for root in cache_roots if root.exists() for path in root.rglob("chrome-headless-shell")]
            if cached:
                launch_options["executable_path"] = str(sorted(cached)[-1])
        browser = await playwright.chromium.launch(**launch_options)
        page = await browser.new_page()
        await page.set_content(
            """
            <fieldset id="skills" class="checkbox-group">
              <label><input type="checkbox" value="Python">Python</label>
              <label><input type="checkbox" value="Go">Go</label>
              <label><input type="checkbox" value="Rust">Rust</label>
            </fieldset>
            <script>
              window.__ff = {
                byId: (id) => document.getElementById(id),
                closestCrossRoot: (node, selector) => node.closest(selector),
                queryOne: (selector) => document.querySelector(selector),
              };
            </script>
            """
        )

        result = await page.evaluate(f"({_CLICK_CHECKBOX_GROUP_JS})('skills', 'Python, Go')")

        assert result == '{"clicked":true,"alreadyChecked":false,"matchedCount":2}'
        assert await page.locator('input[value="Python"]').is_checked()
        assert await page.locator('input[value="Go"]').is_checked()
        assert not await page.locator('input[value="Rust"]').is_checked()
        await browser.close()
