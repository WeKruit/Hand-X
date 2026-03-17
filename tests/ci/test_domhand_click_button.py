"""Regression tests for shadow-DOM-aware auth button selection."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from pytest_httpserver import HTTPServer

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.events import ClickElementEvent
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

ARIA_HIDDEN_OVERLAY_HTML = """
<!DOCTYPE html>
<html>
<head>
	<title>Aria Hidden Overlay</title>
	<style>
		body { font-family: sans-serif; padding: 24px; }
		.stack {
			position: relative;
			width: 320px;
			height: 40px;
		}
		.stack > * {
			position: absolute;
			inset: 0;
			width: 100%;
			height: 100%;
		}
		#hidden-submit {
			z-index: 1;
		}
		#overlay-submit {
			z-index: 2;
			display: flex;
			align-items: center;
			justify-content: center;
			background: rgba(0, 0, 0, 0.04);
			border: 1px solid #999;
			border-radius: 6px;
			cursor: pointer;
		}
	</style>
</head>
<body>
	<div class="stack">
		<button
			id="hidden-submit"
			type="submit"
			aria-hidden="true"
			tabindex="-2"
			data-automation-id="createAccountSubmitButton"
		>
			Create Account
		</button>
		<div
			id="overlay-submit"
			role="button"
			tabindex="0"
			aria-label="Create Account"
			data-automation-id="click_filter"
		></div>
	</div>
	<div id="status"></div>
	<script>
		window.__clicks = { hidden: 0, overlay: 0 };

		document.getElementById('hidden-submit').addEventListener('click', function(event) {
			window.__clicks.hidden += 1;
			event.preventDefault();
			event.stopPropagation();
			document.getElementById('status').textContent = 'hidden-only';
		});

		document.getElementById('overlay-submit').addEventListener('click', function() {
			window.__clicks.overlay += 1;
			document.getElementById('status').textContent = 'submitted';
			history.pushState({}, '', '/overlay/success');
		});
	</script>
</body>
</html>
"""

OCCLUDED_STANDARD_CLICK_HTML = """
<!DOCTYPE html>
<html>
<head>
	<title>Occluded Standard Click</title>
	<style>
		body { font-family: sans-serif; padding: 24px; }
		.stack {
			position: relative;
			width: 320px;
			height: 44px;
		}
		.stack > * {
			position: absolute;
			inset: 0;
			width: 100%;
			height: 100%;
		}
		#under-submit {
			z-index: 1;
		}
		#overlay-submit {
			z-index: 2;
			display: flex;
			align-items: center;
			justify-content: center;
			background: rgba(0, 0, 0, 0.04);
			border: 1px solid #999;
			border-radius: 6px;
			cursor: pointer;
			pointer-events: none;
		}
	</style>
</head>
<body>
	<div class="stack">
		<button id="under-submit" type="button">Continue</button>
		<div id="overlay-submit"></div>
	</div>
	<div id="status"></div>
	<script>
		window.__clicks = { under: 0, overlay: 0 };

		document.getElementById('under-submit').addEventListener('click', function(event) {
			window.__clicks.under += 1;
			event.preventDefault();
			event.stopPropagation();
			document.getElementById('status').textContent = 'under-only';
		});

		document.getElementById('overlay-submit').addEventListener('click', function() {
			window.__clicks.overlay += 1;
			document.getElementById('status').textContent = 'overlay';
			history.pushState({}, '', '/standard-click/success');
		});
	</script>
</body>
</html>
"""

GENERATED_AUTH_GUARD_HTML = """
<!DOCTYPE html>
<html>
<head>
	<title>Generated Auth Guard</title>
</head>
<body>
	<header>
		<button id="nav-sign-in" type="button">Sign In</button>
	</header>
	<main>
		<h1>Create Account</h1>
		<label>Email <input id="email" type="email" /></label>
		<label>Password <input id="password" type="password" /></label>
		<label>Confirm Password <input id="verify-password" type="password" /></label>
		<button id="create-account" type="button">Create Account</button>
	</main>
	<div id="status"></div>
	<script>
		window.__signInClicks = 0;
		document.getElementById('nav-sign-in').addEventListener('click', function() {
			window.__signInClicks += 1;
			document.getElementById('status').textContent = 'sign-in-clicked';
		});
	</script>
</body>
</html>
"""

NO_TRANSITION_AUTH_HTML = """
<!DOCTYPE html>
<html>
<head>
	<title>No Transition Auth</title>
</head>
<body>
	<h1>Create Account</h1>
	<label>Email <input id="email" type="email" value="adamyang@usc.edu" /></label>
	<label>Password <input id="password" type="password" value="YourTestPass123!" /></label>
	<label>Confirm Password <input id="verify-password" type="password" value="YourTestPass123!" /></label>
	<label>
		<input id="agree" type="checkbox" checked />
		I Agree
	</label>
	<div id="status"></div>
	<button id="create-account" type="button">Create Account</button>
	<script>
		window.__createAccountClicks = 0;
		document.getElementById('create-account').addEventListener('click', function() {
			window.__createAccountClicks += 1;
		});
	</script>
</body>
</html>
"""

POST_CREATE_NATIVE_LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
	<title>Post Create Native Login</title>
</head>
<body>
	<h1>Sign In</h1>
	<label>Email <input id="email" type="email" /></label>
	<label>Password <input id="password" type="password" /></label>
	<button id="sign-in" type="button">Sign In</button>
	<a id="create-account-link" role="button" href="#">Create Account</a>
	<div id="status"></div>
	<script>
		window.__createAccountClicks = 0;
		document.getElementById('create-account-link').addEventListener('click', function(event) {
			event.preventDefault();
			window.__createAccountClicks += 1;
			document.getElementById('status').textContent = 'create-account-clicked';
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


async def test_domhand_click_button_skips_aria_hidden_submit_when_overlay_matches(
    httpserver: HTTPServer,
):
    """Prefer the visible overlay control over an aria-hidden submit duplicate."""
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/overlay").respond_with_data(ARIA_HIDDEN_OVERLAY_HTML, content_type="text/html")

        await tools.navigate(
            url=httpserver.url_for("/overlay"),
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
        assert "Page navigated" in result.extracted_content

        page = await browser_session.get_current_page()
        assert page is not None

        clicks = json.loads(await page.evaluate("() => JSON.stringify(window.__clicks)"))
        assert clicks == {"hidden": 0, "overlay": 1}

        current_url = await page.get_url()
        assert current_url.endswith("/overlay/success")


async def test_standard_click_reroutes_to_topmost_target_when_original_is_occluded(
    httpserver: HTTPServer,
):
    """Click handling should reroute to the topmost hit target when a stale node becomes occluded."""
    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/standard-click").respond_with_data(OCCLUDED_STANDARD_CLICK_HTML, content_type="text/html")

        await tools.navigate(
            url=httpserver.url_for("/standard-click"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.5)

        page = await browser_session.get_current_page()
        assert page is not None
        box = json.loads(
            await page.evaluate(
                """() => {
                    const rect = document.getElementById('under-submit').getBoundingClientRect();
                    return JSON.stringify({
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2,
                    });
                }"""
            )
        )
        original_node = await browser_session.get_dom_element_at_coordinates(int(box["x"]), int(box["y"]))
        assert original_node is not None, "Could not resolve original under-submit node"

        await page.evaluate(
            """() => {
                const overlay = document.getElementById('overlay-submit');
                overlay.setAttribute('role', 'button');
                overlay.setAttribute('tabindex', '0');
                overlay.setAttribute('aria-label', 'Continue');
                document.getElementById('overlay-submit').style.pointerEvents = 'auto';
            }"""
        )

        event = browser_session.event_bus.dispatch(ClickElementEvent(node=original_node))
        await event
        result = await event.event_result(raise_if_any=True, raise_if_none=False)
        assert not (isinstance(result, dict) and "validation_error" in result)

        clicks = json.loads(await page.evaluate("() => JSON.stringify(window.__clicks)"))
        assert clicks == {"under": 0, "overlay": 1}

        current_url = await page.get_url()
        assert current_url.endswith("/standard-click/success")


async def test_generated_credentials_guard_blocks_sign_in_click_on_create_account_page(
    httpserver: HTTPServer,
    monkeypatch,
):
    """Generated-credential runs should not click Sign In while Create Account is active."""
    async with managed_browser_session() as browser_session:
        tools = Tools()
        monkeypatch.setenv("GH_CREDENTIAL_SOURCE", "generated")
        httpserver.expect_request("/generated-auth-guard").respond_with_data(
            GENERATED_AUTH_GUARD_HTML, content_type="text/html"
        )

        await tools.navigate(
            url=httpserver.url_for("/generated-auth-guard"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.5)
        await browser_session.get_browser_state_summary()

        index = await browser_session.get_index_by_id("nav-sign-in")
        assert index is not None, "Could not find nav-sign-in in selector map"

        result = await tools.click(index=index, browser_session=browser_session)
        assert result.error is not None
        assert "NEW_CREDENTIALS run" in result.error

        page = await browser_session.get_current_page()
        assert page is not None
        sign_in_clicks = await page.evaluate("() => window.__signInClicks")
        assert int(sign_in_clicks) == 0


async def test_generated_credentials_guard_blocks_reentering_create_account_after_submit(
    httpserver: HTTPServer,
    monkeypatch,
):
    """Generated-credential runs should not return to Create Account from native login after submit."""
    async with managed_browser_session() as browser_session:
        tools = Tools()
        monkeypatch.setenv("GH_CREDENTIAL_SOURCE", "generated")
        setattr(browser_session, "_gh_generated_create_account_attempted", True)
        httpserver.expect_request("/post-create-native-login").respond_with_data(
            POST_CREATE_NATIVE_LOGIN_HTML, content_type="text/html"
        )

        await tools.navigate(
            url=httpserver.url_for("/post-create-native-login"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.5)
        await browser_session.get_browser_state_summary()

        index = await browser_session.get_index_by_id("create-account-link")
        assert index is not None, "Could not find create-account-link in selector map"

        result = await tools.click(index=index, browser_session=browser_session)
        assert result.error is not None
        assert "expected post-create step" in result.error.lower()

        page = await browser_session.get_current_page()
        assert page is not None
        create_account_clicks = await page.evaluate("() => window.__createAccountClicks")
        assert int(create_account_clicks) == 0


async def test_standard_click_no_transition_captures_visual_recheck_once(
    httpserver: HTTPServer,
    monkeypatch,
):
    """Critical button clicks should emit screenshot-backed diagnostics on no transition."""
    emitted_events: list[dict] = []

    def _record_no_transition(outcome, *, secondary_summary=None):
        emitted_events.append({
            "label": outcome.get("descriptor", {}).get("label"),
            "secondary_summary": secondary_summary,
            "screenshot_path": outcome.get("screenshot_path"),
        })

    monkeypatch.setattr(
        "browser_use.tools.service.emit_button_no_transition_event",
        _record_no_transition,
    )

    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/no-transition-auth").respond_with_data(
            NO_TRANSITION_AUTH_HTML, content_type="text/html"
        )

        await tools.navigate(
            url=httpserver.url_for("/no-transition-auth"),
            new_tab=False,
            browser_session=browser_session,
        )
        await asyncio.sleep(0.5)
        await browser_session.get_browser_state_summary()

        index = await browser_session.get_index_by_id("create-account")
        assert index is not None, "Could not find create-account in selector map"

        first = await tools.click(index=index, browser_session=browser_session)
        assert first.error is not None
        assert "No transition observed" in first.error
        assert isinstance(first.metadata, dict)
        attempt = first.metadata.get("button_attempt")
        assert isinstance(attempt, dict)
        assert attempt.get("no_transition") is True
        assert attempt.get("auth_state_before") == "still_create_account"
        assert attempt.get("auth_state_after") == "still_create_account"
        screenshot_path = attempt.get("screenshot_path")
        assert screenshot_path
        assert Path(screenshot_path).exists()
        assert attempt.get("visual_recheck", {}).get("performed") is True
        assert emitted_events and emitted_events[0]["screenshot_path"] == screenshot_path

        second = await tools.click(index=index, browser_session=browser_session)
        assert second.error is not None
        assert isinstance(second.metadata, dict)
        second_attempt = second.metadata.get("button_attempt")
        assert isinstance(second_attempt, dict)
        assert second_attempt.get("screenshot_path") == screenshot_path
        assert second_attempt.get("visual_recheck", {}).get("captured_new") is False
        assert len(emitted_events) == 2


async def test_domhand_click_button_no_transition_captures_visual_recheck(
    httpserver: HTTPServer,
    monkeypatch,
):
    """domhand_click_button should attach screenshot-backed diagnostics when a critical button stalls."""
    emitted_events: list[dict] = []

    def _record_no_transition(outcome, *, secondary_summary=None):
        emitted_events.append({
            "label": outcome.get("descriptor", {}).get("label"),
            "secondary_summary": secondary_summary,
            "screenshot_path": outcome.get("screenshot_path"),
        })

    monkeypatch.setattr(
        "ghosthands.actions.domhand_click_button.emit_button_no_transition_event",
        _record_no_transition,
    )

    async with managed_browser_session() as browser_session:
        tools = Tools()
        httpserver.expect_request("/domhand-no-transition-auth").respond_with_data(
            NO_TRANSITION_AUTH_HTML, content_type="text/html"
        )

        await tools.navigate(
            url=httpserver.url_for("/domhand-no-transition-auth"),
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
        assert "Visual re-check screenshot captured" in result.extracted_content
        assert isinstance(result.metadata, dict)
        attempt = result.metadata.get("button_attempt")
        assert isinstance(attempt, dict)
        assert attempt.get("no_transition") is True
        screenshot_path = attempt.get("screenshot_path")
        assert screenshot_path
        assert Path(screenshot_path).exists()
        assert emitted_events and emitted_events[0]["screenshot_path"] == screenshot_path
