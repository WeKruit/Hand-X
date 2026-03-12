"""DomHand Click Button — robust button clicking for React-heavy ATS sites.

browser-use's default click uses CDP Input.dispatchMouseEvent which works for most
elements but can fail on React form-submission buttons (Workday, etc.) that require
a specific event sequence or check form validation state.

This action:
1. Finds the button via JS (text match, aria-label, data-automation-id)
2. Clicks it using multiple strategies:
   - CSS selector + browser-use Element.click() (CDP coordinates)
   - JS full event sequence (focus → mousedown → mouseup → click)
   - Direct form.submit() as last resort
3. Verifies the page transitioned (URL change or DOM mutation)

Use this for auth buttons (Create Account, Sign In) or form submission
buttons that the regular click action doesn't trigger.
"""

import asyncio
import json
import logging

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# JS: Find a button by label and return info needed for clicking.
# Returns { found: true, selector: "...", index: N, text: "...", tag: "...", automationId: "..." }
# or { found: false, buttons: [...] } with all visible buttons for diagnostics.
_FIND_AND_CLICK_JS = r"""(label, shouldClick) => {
	var lower = (label || '').toLowerCase().trim();
	var results = { found: false, buttons: [] };

	// Shadow-DOM-aware query — uses __ff.queryAll when available
	function qAll(sel) {
		if (window.__ff && window.__ff.queryAll) return window.__ff.queryAll(sel);
		return Array.from(document.querySelectorAll(sel));
	}

	// Collect ALL visible buttons (across shadow roots)
	var allButtons = qAll(
		'button, [role="button"], input[type="submit"], [data-automation-id*="utton"]'
	);
	var candidates = [];

	for (var i = 0; i < allButtons.length; i++) {
		var btn = allButtons[i];
		var rect = btn.getBoundingClientRect();
		if (rect.width === 0 || rect.height === 0) continue;

		var text = (btn.textContent || btn.value || '').trim();
		var ariaLabel = btn.getAttribute('aria-label') || '';
		var automationId = btn.getAttribute('data-automation-id') || '';
		var displayText = text || ariaLabel;

		results.buttons.push({
			text: displayText.substring(0, 100),
			tag: btn.tagName,
			automationId: automationId,
			disabled: btn.disabled || btn.getAttribute('aria-disabled') === 'true'
		});

		// Check for match
		var textLower = displayText.toLowerCase();
		var aidLower = automationId.toLowerCase();
		var labelWords = lower.split(/\s+/);

		var isMatch = false;

		// Exact text match
		if (textLower === lower) isMatch = true;
		// Contains text match
		else if (textLower.indexOf(lower) !== -1) isMatch = true;
		// aria-label match
		else if (ariaLabel.toLowerCase() === lower) isMatch = true;
		// automation-id contains all words
		else if (labelWords.every(function(w) { return aidLower.indexOf(w) !== -1; })) isMatch = true;

		if (isMatch && !btn.disabled) {
			candidates.push({
				element: btn,
				text: displayText,
				automationId: automationId,
				exactMatch: textLower === lower
			});
		}
	}

	// Prefer exact matches
	candidates.sort(function(a, b) {
		if (a.exactMatch && !b.exactMatch) return -1;
		if (!a.exactMatch && b.exactMatch) return 1;
		return 0;
	});

	if (candidates.length === 0) {
		return JSON.stringify(results);
	}

	var target = candidates[0];
	results.found = true;
	results.text = target.text;
	results.automationId = target.automationId;

	// Build CSS selector for the element
	var el = target.element;
	if (el.id) {
		results.selector = '#' + CSS.escape(el.id);
	} else if (target.automationId) {
		results.selector = '[data-automation-id="' + target.automationId + '"]';
	} else if (el.getAttribute('aria-label')) {
		results.selector = '[aria-label="' + el.getAttribute('aria-label') + '"]';
	}

	if (shouldClick) {
		// Full event sequence that satisfies React's synthetic event system
		try {
			el.scrollIntoView({ block: 'center', behavior: 'instant' });

			// Focus the button first
			el.focus();

			// Dispatch full mouse event sequence
			var mouseOpts = { bubbles: true, cancelable: true, view: window };
			el.dispatchEvent(new MouseEvent('mouseenter', mouseOpts));
			el.dispatchEvent(new MouseEvent('mouseover', mouseOpts));
			el.dispatchEvent(new MouseEvent('mousedown', { ...mouseOpts, button: 0 }));
			el.dispatchEvent(new MouseEvent('mouseup', { ...mouseOpts, button: 0 }));
			el.dispatchEvent(new MouseEvent('click', { ...mouseOpts, button: 0 }));

			results.clicked = 'js_event_sequence';
		} catch(e) {
			results.clickError = e.message;
		}

		// Also try native .click() as backup
		try {
			el.click();
			if (!results.clicked) results.clicked = 'native_click';
		} catch(e2) {
			if (!results.clickError) results.clickError = e2.message;
		}

		// If this is inside a form, also try form.submit()
		try {
			var form = el.closest('form');
			if (form) {
				// Don't submit directly — just note it's available
				results.hasForm = true;
			}
		} catch(e) {}
	}

	return JSON.stringify(results);
}"""

# JS: Submit the form containing a specific button
_SUBMIT_FORM_JS = r"""(label) => {
	var lower = (label || '').toLowerCase().trim();
	function qAll(sel) {
		if (window.__ff && window.__ff.queryAll) return window.__ff.queryAll(sel);
		return Array.from(document.querySelectorAll(sel));
	}
	var buttons = qAll('button, [role="button"], input[type="submit"]');
	for (var i = 0; i < buttons.length; i++) {
		var text = (buttons[i].textContent || buttons[i].value || '').trim().toLowerCase();
		if (text === lower || text.indexOf(lower) !== -1) {
			var form = buttons[i].closest('form');
			if (form) {
				form.submit();
				return JSON.stringify({ submitted: true });
			}
			// Try clicking as HTMLElement
			buttons[i].click();
			return JSON.stringify({ clicked: true, noForm: true });
		}
	}
	return JSON.stringify({ submitted: false, error: 'button not found' });
}"""

# JS: Check current page state for diagnostics
_PAGE_STATE_JS = r"""() => {
	function qAll(sel) {
		if (window.__ff && window.__ff.queryAll) return window.__ff.queryAll(sel);
		return Array.from(document.querySelectorAll(sel));
	}
	var state = {
		url: window.location.href,
		title: document.title,
		forms: [],
		hiddenErrors: []
	};

	// Check all forms for validity (across shadow roots)
	qAll('form').forEach(function(form, i) {
		var formInfo = { index: i, valid: form.checkValidity(), fields: [] };
		form.querySelectorAll('input, select, textarea').forEach(function(field) {
			if (!field.checkValidity()) {
				formInfo.fields.push({
					name: field.name || field.id || field.type,
					valid: false,
					message: field.validationMessage
				});
			}
		});
		state.forms.push(formInfo);
	});

	// Look for hidden error messages
	var errorPatterns = [
		'[class*="error"]', '[class*="Error"]',
		'[class*="invalid"]', '[class*="Invalid"]',
		'[role="alert"]', '[aria-invalid="true"]'
	];
	errorPatterns.forEach(function(sel) {
		qAll(sel).forEach(function(el) {
			var text = (el.textContent || '').trim();
			if (text && text.length < 200) {
				state.hiddenErrors.push({ selector: sel, text: text });
			}
		});
	});

	// Check checkboxes state (across shadow roots)
	var checkboxes = [];
	qAll('input[type="checkbox"], [role="checkbox"]').forEach(function(cb) {
		var label = '';
		var parent = cb.closest('label');
		if (parent) label = parent.textContent || '';
		else if (cb.getAttribute('aria-label')) label = cb.getAttribute('aria-label');
		checkboxes.push({
			checked: cb.checked || cb.getAttribute('aria-checked') === 'true',
			label: label.trim().substring(0, 100)
		});
	});
	state.checkboxes = checkboxes;

	return JSON.stringify(state);
}"""


class DomHandClickButtonParams(BaseModel):
	"""Parameters for domhand_click_button action."""

	button_label: str = Field(
		description=(
			'The visible text label of the button to click '
			'(e.g., "Create Account", "Sign In", "Next", "Continue").'
		)
	)


async def domhand_click_button(params: DomHandClickButtonParams, browser_session: BrowserSession) -> ActionResult:
	"""Click a button using multiple strategies: JS event dispatch, CDP Element.click(), form.submit().

	Designed for React-based sites (Workday, etc.) where browser-use's standard
	click via CDP mouse events may not trigger form submission handlers.
	"""
	page = await browser_session.get_current_page()
	if not page:
		return ActionResult(error="No active page found")

	# Inject __ff shadow-DOM helpers if not present (auth pages skip domhand_fill)
	try:
		has_ff = await page.evaluate("() => { return !!(window.__ff); }")
		if has_ff != "true" and has_ff is not True:
			from ghosthands.dom.shadow_helpers import _build_inject_helpers_js
			await page.evaluate(_build_inject_helpers_js())
			logger.debug("domhand_click_button: injected __ff helpers")
	except Exception as e:
		logger.debug(f"Failed to inject __ff helpers: {e}")

	label = params.button_label.strip()
	logger.info("domhand_click_button.start", extra={"label": label})

	# Capture URL before click to detect navigation
	try:
		url_before = await page.get_url()
	except Exception:
		url_before = ""

	# ── Strategy 1: JS find + full event sequence ──────────────────
	# Uses dispatchEvent with mousedown/mouseup/click sequence
	try:
		result_json = await page.evaluate(_FIND_AND_CLICK_JS, label, True)
		result = json.loads(result_json)

		# Visual highlight — show the user which button was clicked
		if result.get("found") and result.get("selector"):
			from ghosthands.actions._highlight import highlight_element
			await highlight_element(page, result["selector"])

		if result.get("found") and result.get("clicked"):
			await asyncio.sleep(1.0)  # Wait for potential navigation

			# Check if page changed
			try:
				url_after = await page.get_url()
			except Exception:
				url_after = url_before

			method = result.get("clicked", "unknown")
			matched_text = result.get("text", "")
			logger.info("domhand_click_button.js_click", extra={
				"label": label,
				"method": method,
				"matched_text": matched_text,
				"matched_automation_id": result.get("automationId", ""),
				"url_changed": url_before != url_after,
			})

			# If URL changed, we're done
			if url_before != url_after:
				return ActionResult(
					extracted_content=f"DomHand click: clicked '{label}' via JS event sequence. Page navigated to {url_after}.",
					include_in_memory=True,
				)

			# URL didn't change — button was clicked but page didn't navigate.
			# This could mean: form validation failed, or the click didn't register properly.
			# Try CDP click next.

	except Exception as e:
		logger.debug(f"JS find+click failed for '{label}': {e}")
		result = {"found": False}

	# ── Strategy 2: CSS selector + Element.click() (CDP mouse events) ──
	if result.get("found") and result.get("selector"):
		selector = result["selector"]
		try:
			elements = await page.get_elements_by_css_selector(selector)
			if elements:
				await elements[0].click()
				await asyncio.sleep(1.0)

				try:
					url_after = await page.get_url()
				except Exception:
					url_after = url_before

				logger.info("domhand_click_button.cdp_click", extra={
					"label": label,
					"selector": selector,
					"url_changed": url_before != url_after,
				})

				if url_before != url_after:
					return ActionResult(
						extracted_content=f"DomHand click: clicked '{label}' via CDP Element.click(). Page navigated to {url_after}.",
						include_in_memory=True,
					)
		except Exception as e:
			logger.debug(f"CDP Element click failed for '{label}': {e}")

	# ── Strategy 3: form.submit() as last resort ───────────────────
	if result.get("found"):
		try:
			submit_json = await page.evaluate(_SUBMIT_FORM_JS, label)
			submit_result = json.loads(submit_json)

			if submit_result.get("submitted") or submit_result.get("clicked"):
				await asyncio.sleep(1.5)

				try:
					url_after = await page.get_url()
				except Exception:
					url_after = url_before

				logger.info("domhand_click_button.form_submit", extra={
					"label": label,
					"url_changed": url_before != url_after,
				})

				if url_before != url_after:
					return ActionResult(
						extracted_content=f"DomHand click: submitted form for '{label}'. Page navigated to {url_after}.",
						include_in_memory=True,
					)
		except Exception as e:
			logger.debug(f"Form submit failed for '{label}': {e}")

	# ── Diagnostics: check page state to understand why click didn't work ──
	diagnostics = {}
	try:
		state_json = await page.evaluate(_PAGE_STATE_JS)
		diagnostics = json.loads(state_json)
	except Exception:
		pass

	if result.get("found"):
		# Button found and clicked, but page didn't transition
		hidden_errors = diagnostics.get("hiddenErrors", [])
		checkboxes = diagnostics.get("checkboxes", [])
		forms = diagnostics.get("forms", [])

		unchecked_boxes = [cb for cb in checkboxes if not cb.get("checked")]

		# Filter to agreement-related checkboxes only — don't auto-check
		# unrelated boxes like "Remember me" or notification preferences.
		_agree_kw = ["agree", "accept", "consent", "terms", "privacy", "acknowledge", "i understand", "certify"]
		agreement_unchecked = [
			cb for cb in unchecked_boxes
			if any(kw in (cb.get("label") or "").lower() for kw in _agree_kw)
		]

		# ── Auto-fix: if unchecked AGREEMENT checkboxes are likely blocking
		#    submission, check them automatically and retry the button click once.
		if agreement_unchecked:
			logger.info("domhand_click_button.auto_checking_boxes", extra={
				"label": label,
				"unchecked": [cb.get("label", "?")[:60] for cb in unchecked_boxes],
			})
			try:
				from ghosthands.actions.domhand_check_agreement import (
					DomHandCheckAgreementParams,
					domhand_check_agreement,
				)
				await domhand_check_agreement(DomHandCheckAgreementParams(), browser_session)
				await asyncio.sleep(0.5)

				# Retry the button click after checking boxes
				retry_json = await page.evaluate(_FIND_AND_CLICK_JS, label, True)
				retry_result = json.loads(retry_json)
				if retry_result.get("found") and retry_result.get("clicked"):
					await asyncio.sleep(1.5)
					try:
						url_after = await page.get_url()
					except Exception:
						url_after = url_before

					if url_before != url_after:
						logger.info("domhand_click_button.retry_after_checkbox_success", extra={
							"label": label,
						})
						return ActionResult(
							extracted_content=(
								f"DomHand click: auto-checked agreement checkbox, then clicked "
								f"'{label}'. Page navigated to {url_after}."
							),
							include_in_memory=True,
						)
			except Exception as e:
				logger.debug(f"Auto-check-and-retry failed: {e}")

		detail_parts = [
			f"DomHand click: found and clicked '{label}' but page did not navigate.",
		]

		if hidden_errors:
			error_texts = [e.get("text", "")[:80] for e in hidden_errors[:5]]
			detail_parts.append(f"Hidden errors found: {error_texts}")

		if unchecked_boxes:
			labels = [cb.get("label", "?")[:60] for cb in unchecked_boxes]
			detail_parts.append(
				f"UNCHECKED CHECKBOXES DETECTED: {labels}. "
				"An unchecked agreement checkbox is the most likely cause. "
				"Use domhand_check_agreement or click the checkbox manually, "
				"then try domhand_click_button again."
			)

		invalid_forms = [f for f in forms if not f.get("valid")]
		if invalid_forms:
			for f in invalid_forms:
				invalid_fields = [fld.get("name", "?") + ": " + fld.get("message", "?")
								for fld in f.get("fields", [])]
				detail_parts.append(f"Form validation errors: {invalid_fields}")

		if not unchecked_boxes and not hidden_errors and not invalid_forms:
			detail_parts.append(
				"The button was clicked but the page did not transition. "
				"Possible causes: the site blocks automated clicks, or a "
				"required field is missing. Check for error messages."
			)

		logger.warning("domhand_click_button.no_transition", extra={
			"label": label,
			"hidden_errors": hidden_errors[:3],
			"checkboxes": checkboxes,
			"forms": forms,
		})

		return ActionResult(
			extracted_content="\n".join(detail_parts),
			include_in_memory=True,
		)

	# Button not found at all
	visible_buttons = [b.get("text", "?")[:60] for b in result.get("buttons", [])
					   if not b.get("disabled")]

	logger.warning("domhand_click_button.not_found", extra={
		"label": label,
		"visible_buttons": visible_buttons,
	})

	return ActionResult(
		error=f"Could not find button '{label}' on page. "
			f"Visible buttons: {visible_buttons[:10]}. "
			f"Try using the exact text shown on the button.",
	)
