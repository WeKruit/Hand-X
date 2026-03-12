"""Regression tests for shadow-DOM-aware auth button selection."""

import asyncio
from contextlib import asynccontextmanager
import json

from pytest_httpserver import HTTPServer

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.tools.service import Tools
from ghosthands.actions.domhand_click_button import (
    DomHandClickButtonParams,
    domhand_click_button,
)

AUTH_MODAL_HTML = """
<!DOCTYPE html>
<html>
<head>
	<title>Shadow Auth Modal</title>
	<style>
		body { font-family: sans-serif; margin: 0; padding: 0; }
		header {
			display: flex;
			justify-content: flex-end;
			padding: 12px 16px;
			border-bottom: 1px solid #ddd;
		}
		#result {
			padding: 12px 16px;
			color: #0a5;
			font-weight: 600;
		}
	</style>
</head>
<body>
	<header>
		<button id="nav-sign-in" type="button">Sign In</button>
	</header>
	<div id="auth-host"></div>
	<div id="result"></div>
	<script>
		window.__clicks = { nav: 0, tab: 0, submit: 0 };

		document.getElementById('nav-sign-in').addEventListener('click', function() {
			window.__clicks.nav += 1;
			document.getElementById('result').textContent = 'clicked-nav';
		});

		const host = document.getElementById('auth-host');
		const root = host.attachShadow({ mode: 'open' });
		root.innerHTML = `
			<style>
				.wrapper {
					max-width: 420px;
					margin: 48px auto;
					border: 1px solid #ddd;
					border-radius: 12px;
					padding: 24px;
					box-shadow: 0 12px 40px rgba(0, 0, 0, 0.12);
					background: white;
				}
				.tab-row {
					display: flex;
					justify-content: space-between;
					align-items: center;
					margin-bottom: 16px;
				}
				form {
					display: flex;
					flex-direction: column;
					gap: 12px;
				}
				label {
					display: flex;
					flex-direction: column;
					gap: 6px;
				}
				.actions {
					display: flex;
					justify-content: flex-end;
					margin-top: 12px;
				}
				#error {
					color: #b00020;
					min-height: 1.2em;
				}
			</style>
			<div class="wrapper" role="dialog" aria-modal="true" aria-label="Sign In dialog">
				<div class="tab-row" role="tablist" aria-label="Auth tabs">
					<button id="tab-sign-in" type="button" role="tab" aria-selected="true">Sign In</button>
					<span id="panel-state">tab-ready</span>
				</div>
				<form id="auth-form">
					<label>
						<span>Email</span>
						<input id="email" type="email" name="email" autocomplete="username" />
					</label>
					<label>
						<span>Password</span>
						<input id="password" type="password" name="password" autocomplete="current-password" />
					</label>
					<div id="error"></div>
					<div class="actions">
						<button id="submit-sign-in" type="submit" data-automation-id="signInSubmitButton">Sign In</button>
					</div>
				</form>
			</div>
		`;

		root.getElementById('tab-sign-in').addEventListener('click', function() {
			window.__clicks.tab += 1;
			root.getElementById('panel-state').textContent = 'clicked-tab';
		});

		root.getElementById('auth-form').addEventListener('submit', function(event) {
			event.preventDefault();
			window.__clicks.submit += 1;

			const email = root.getElementById('email').value.trim();
			const password = root.getElementById('password').value.trim();

			if (!email || !password) {
				root.getElementById('error').textContent = 'missing credentials';
				return;
			}

			root.getElementById('error').textContent = '';
			document.getElementById('result').textContent = 'submitted';
			history.pushState({}, '', '/shadow-auth/signed-in');
		});
	</script>
</body>
</html>
"""

AGREEMENT_RETRY_HTML = """
<!DOCTYPE html>
<html>
<head>
	<title>Agreement Retry</title>
</head>
<body>
	<form id="signup-form">
		<label for="email">Email</label>
		<input id="email" type="email" value="happy@ucla.edu" />
		<label for="password">Password</label>
		<input id="password" type="password" value="YourTestPass123!" />
		<label>
			<input id="agree" type="checkbox" />
			I agree to the Terms of Service
		</label>
		<label>
			<input id="marketing" type="checkbox" />
			Send me product updates
		</label>
		<div id="error"></div>
		<div id="status"></div>
		<button id="create-account" type="submit">Create Account</button>
	</form>
	<script>
		window.__submitCount = 0;

		document.getElementById('signup-form').addEventListener('submit', function(event) {
			event.preventDefault();
			window.__submitCount += 1;

			const agree = document.getElementById('agree').checked;
			const marketing = document.getElementById('marketing').checked;
			window.__checkboxState = { agree, marketing };

			if (!agree) {
				document.getElementById('error').textContent = 'terms required';
				return;
			}

			document.getElementById('error').textContent = '';
			document.getElementById('status').textContent = 'submitted';
			history.pushState({}, '', '/agreements/success');
		});
	</script>
</body>
</html>
"""


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


async def test_domhand_click_button_prefers_shadow_dialog_submit(
    httpserver: HTTPServer,
):
    """Select the real submit button when duplicate auth buttons are present."""
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/shadow-auth").respond_with_data(AUTH_MODAL_HTML, content_type="text/html")

        await tools.navigate(
            url=httpserver.url_for("/shadow-auth"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.5)

        page = await browser_session.get_current_page()
        assert page is not None

        await page.evaluate(
            """() => {
			const root = document.getElementById('auth-host').shadowRoot;
			root.getElementById('email').value = 'happy@ucla.edu';
			root.getElementById('password').value = 'YourTestPass123!';
		}"""
        )

        result = await domhand_click_button(
            DomHandClickButtonParams(button_label="Sign In"),
            browser_session,
        )

        assert result.error is None
        assert result.extracted_content is not None
        assert "Page navigated" in result.extracted_content

        clicks = json.loads(await page.evaluate("() => JSON.stringify(window.__clicks)"))
        assert clicks == {"nav": 0, "tab": 0, "submit": 1}

        current_url = await page.get_url()
        assert current_url.endswith("/shadow-auth/signed-in")

        result_text = await page.evaluate("() => document.getElementById('result').textContent")
        assert result_text == "submitted"


async def test_domhand_click_button_only_auto_checks_agreement_boxes(
    httpserver: HTTPServer,
):
    """Retry should only toggle agreement checkboxes, not unrelated preferences."""
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/agreements").respond_with_data(AGREEMENT_RETRY_HTML, content_type="text/html")

        await tools.navigate(
            url=httpserver.url_for("/agreements"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.5)

        result = await domhand_click_button(
            DomHandClickButtonParams(button_label="Create Account"),
            browser_session,
        )

        assert result.error is None
        assert result.extracted_content is not None
        assert "auto-checked agreement checkbox" in result.extracted_content

        page = await browser_session.get_current_page()
        assert page is not None

        state = json.loads(await page.evaluate("() => JSON.stringify(window.__checkboxState || {})"))
        assert state == {"agree": True, "marketing": False}

        submit_count = await page.evaluate("() => window.__submitCount")
        assert int(submit_count) >= 2

        current_url = await page.get_url()
        assert current_url.endswith("/agreements/success")
