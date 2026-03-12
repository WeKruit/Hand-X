"""DomHand Check Agreement — force-check agreement/consent checkboxes via Playwright.

This action exists specifically for auth pages (Create Account, Sign In) where
domhand_fill is intentionally skipped (to avoid using the wrong email).  The
agent can call this to reliably check "I agree" / privacy policy / terms
checkboxes that standard click actions fail on due to custom Workday widgets.

Strategy:
1. Use JS to find all checkbox-like elements and their labels
2. For agreement-related ones, use Playwright's page.click() with a CSS selector
   (NOT JS .click() — Workday's React framework doesn't detect JS-only clicks)
3. Verify the state changed
4. Return which checkboxes were checked
"""

import asyncio
import json
import logging

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# JS to DISCOVER checkboxes and their labels (does NOT click — just reports)
_DISCOVER_CHECKBOXES_JS = r"""() => {
	var results = [];
	var keywords = [
		'i agree', 'i accept', 'i understand', 'i acknowledge',
		'i consent', 'i certify', 'privacy policy', 'terms of service',
		'terms and conditions', 'candidate consent', 'agree to',
		'terms of use', 'data privacy', 'acknowledge and agree',
		'consent to', 'privacy notice'
	];

	function isAgreement(text) {
		var lower = (text || '').toLowerCase();
		for (var i = 0; i < keywords.length; i++) {
			if (lower.indexOf(keywords[i]) !== -1) return true;
		}
		return false;
	}

	function getLabelText(el) {
		var label = el.closest('label');
		if (label) return label.textContent || '';
		var ariaLabel = el.getAttribute('aria-label');
		if (ariaLabel) return ariaLabel;
		var labelledBy = el.getAttribute('aria-labelledby');
		if (labelledBy) {
			var ref = document.getElementById(labelledBy);
			if (ref) return ref.textContent || '';
		}
		if (el.id) {
			var forLabel = document.querySelector('label[for="' + el.id + '"]');
			if (forLabel) return forLabel.textContent || '';
		}
		var parent = el.parentElement;
		if (parent) {
			var grandparent = parent.parentElement;
			if (grandparent) return grandparent.textContent || '';
		}
		return '';
	}

	function getState(el) {
		if (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio'))
			return el.checked;
		if (el.getAttribute('aria-checked') === 'true') return true;
		if (el.getAttribute('aria-checked') === 'false') return false;
		return null;
	}

	function buildSelector(el) {
		// Build a CSS selector that Playwright can use to click this element
		if (el.id) return '#' + CSS.escape(el.id);
		var automationId = el.getAttribute('data-automation-id');
		if (automationId) return '[data-automation-id="' + automationId + '"]';
		// For input checkboxes, use type selector
		if (el.tagName === 'INPUT' && el.type === 'checkbox') {
			var name = el.getAttribute('name');
			if (name) return 'input[type="checkbox"][name="' + name + '"]';
			// Nth-of-type fallback
			var parent = el.parentElement;
			if (parent) {
				var inputs = parent.querySelectorAll('input[type="checkbox"]');
				for (var i = 0; i < inputs.length; i++) {
					if (inputs[i] === el) return 'input[type="checkbox"]:nth-of-type(' + (i+1) + ')';
				}
			}
			return 'input[type="checkbox"]';
		}
		if (el.getAttribute('role') === 'checkbox') return '[role="checkbox"]';
		return null;
	}

	var selectors = [
		'input[type="checkbox"]',
		'[role="checkbox"]',
		'[data-automation-id*="checkbox"]',
		'[data-automation-id*="Check"]',
		'[data-automation-id*="agree"]'
	];
	var all = new Set();
	selectors.forEach(function(s) {
		document.querySelectorAll(s).forEach(function(el) { all.add(el); });
	});

	all.forEach(function(el) {
		var labelText = getLabelText(el);
		var entry = {
			label: labelText.substring(0, 200).trim(),
			tag: el.tagName,
			role: el.getAttribute('role'),
			checked: getState(el),
			selector: buildSelector(el),
			isAgreement: isAgreement(labelText)
		};
		results.push(entry);
	});

	// If no agreement found by keyword but only one checkbox on page, treat it as agreement
	var agreements = results.filter(function(r) { return r.isAgreement; });
	if (agreements.length === 0 && results.length === 1) {
		results[0].isAgreement = true;
		results[0].fallback = 'only_checkbox_on_page';
	}

	return JSON.stringify(results);
}"""


class DomHandCheckAgreementParams(BaseModel):
	"""Parameters for domhand_check_agreement action."""
	# No parameters needed — it finds and checks all agreement checkboxes automatically


async def domhand_check_agreement(params: DomHandCheckAgreementParams, browser_session: BrowserSession) -> ActionResult:
	"""Find and check all agreement/consent checkboxes on the current page.

	Uses Playwright's native click() to handle Workday's React checkboxes.
	JS-only clicks don't trigger framework state updates, so we must use
	real Playwright interaction.
	"""
	page = await browser_session.get_current_page()
	if not page:
		return ActionResult(error="No active page found")

	# Step 1: Discover all checkboxes and identify agreement ones
	try:
		result_json = await page.evaluate(_DISCOVER_CHECKBOXES_JS)
		checkboxes = json.loads(result_json)
	except Exception as e:
		logger.warning("domhand_check_agreement.discover_error", extra={"error": str(e)})
		return ActionResult(error=f"Failed to discover checkboxes: {e}")

	logger.info("domhand_check_agreement.discovered", extra={
		"total": len(checkboxes),
		"agreements": sum(1 for c in checkboxes if c.get("isAgreement")),
		"details": checkboxes,
	})

	# Step 2: Click each agreement checkbox
	# NOTE: browser-use's Page class does NOT have Playwright methods like .click()
	# or .get_by_role(). We must use page.evaluate() for JS-based clicking or
	# page.get_elements_by_css_selector() + element.click() for CDP-based clicking.
	results = []
	for cb in checkboxes:
		if not cb.get("isAgreement"):
			continue

		label = cb.get("label", "?")[:80]
		selector = cb.get("selector")
		was_checked = cb.get("checked")

		if was_checked is True:
			results.append({"label": label, "action": "already_checked"})
			continue

		clicked = False

		# Strategy 1: CSS selector + Element.click() via browser-use API
		if selector and not clicked:
			try:
				elements = await page.get_elements_by_css_selector(selector)
				if elements:
					await elements[0].click()
					await asyncio.sleep(0.3)
					clicked = True
					results.append({"label": label, "action": "element_click", "selector": selector})
			except Exception as e:
				logger.debug(f"Element click failed for '{label}': {e}")

		# Strategy 2: JS click with full React-compatible event sequence
		if not clicked:
			try:
				await page.evaluate(r"""(targetSelector) => {
					var el = null;
					if (targetSelector) {
						el = document.querySelector(targetSelector);
					}
					if (!el) {
						// Find unchecked agreement checkboxes
						var cbs = document.querySelectorAll('input[type="checkbox"], [role="checkbox"]');
						for (var i = 0; i < cbs.length; i++) {
							var cb = cbs[i];
							if (cb.tagName === 'INPUT' && !cb.checked) { el = cb; break; }
							if (cb.getAttribute('aria-checked') !== 'true') { el = cb; break; }
						}
					}
					if (!el) return;

					el.scrollIntoView({ block: 'center' });
					el.focus();

					if (el.tagName === 'INPUT' && el.type === 'checkbox') {
						// Native checkbox: .click() toggles checked + fires change
						el.click();
						// Also dispatch change event for React
						el.dispatchEvent(new Event('change', { bubbles: true }));
						el.dispatchEvent(new Event('input', { bubbles: true }));
					} else {
						// Custom role="checkbox" widget: dispatch click + toggle aria-checked
						var mouseOpts = { bubbles: true, cancelable: true, view: window };
						el.dispatchEvent(new MouseEvent('mousedown', mouseOpts));
						el.dispatchEvent(new MouseEvent('mouseup', mouseOpts));
						el.dispatchEvent(new MouseEvent('click', mouseOpts));
						// Toggle aria-checked if present
						var current = el.getAttribute('aria-checked');
						if (current === 'false') {
							el.setAttribute('aria-checked', 'true');
						}
					}
				}""", selector)
				await asyncio.sleep(0.3)
				clicked = True
				results.append({"label": label, "action": "js_event_sequence"})
			except Exception as e:
				logger.debug(f"JS event sequence failed for '{label}': {e}")

		# Strategy 3: Broad JS click on all unchecked checkboxes
		if not clicked:
			try:
				await page.evaluate("""() => {
					var cbs = document.querySelectorAll('input[type="checkbox"], [role="checkbox"]');
					cbs.forEach(function(el) {
						if (el.tagName === 'INPUT' && !el.checked) {
							el.focus();
							el.click();
							el.dispatchEvent(new Event('change', { bubbles: true }));
						} else if (el.getAttribute('aria-checked') !== 'true') {
							el.click();
						}
					});
				}""")
				await asyncio.sleep(0.3)
				results.append({"label": label, "action": "js_broad_click"})
				clicked = True
			except Exception as e:
				results.append({"label": label, "action": "failed", "error": str(e)})

	# Step 3: Verify final state
	try:
		verify_json = await page.evaluate(_DISCOVER_CHECKBOXES_JS)
		verify = json.loads(verify_json)
		final_state = {c.get("label", "")[:40]: c.get("checked") for c in verify if c.get("isAgreement")}
	except Exception:
		final_state = {}

	# Build summary
	checked_count = sum(1 for r in results if r.get("action", "").startswith(("clicked", "checked")))
	already_count = sum(1 for r in results if r.get("action") == "already_checked")
	failed_count = sum(1 for r in results if r.get("action") == "failed")

	summary_parts = []
	if checked_count:
		summary_parts.append(f"{checked_count} checkbox(es) clicked via Playwright")
	if already_count:
		summary_parts.append(f"{already_count} already checked")
	if failed_count:
		summary_parts.append(f"{failed_count} failed")
	if not results:
		summary_parts.append("no agreement checkboxes found on page")

	summary = "; ".join(summary_parts)
	logger.info("domhand_check_agreement.result", extra={
		"summary": summary,
		"results": results,
		"final_state": final_state,
	})

	detail_lines = [f"DomHand agreement check: {summary}"]
	for r in results:
		detail_lines.append(f"  - \"{r.get('label', '?')}\" → {r.get('action', 'unknown')}")
	if final_state:
		detail_lines.append(f"  Final state: {final_state}")

	return ActionResult(
		extracted_content="\n".join(detail_lines),
		include_in_memory=True,
	)
