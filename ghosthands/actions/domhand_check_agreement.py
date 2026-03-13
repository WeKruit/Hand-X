"""DomHand Check Agreement — force-check agreement/consent checkboxes.

This action exists specifically for auth pages (Create Account, Sign In) where
domhand_fill is intentionally skipped (to avoid using the wrong email).  The
agent can call this to reliably check "I agree" / privacy policy / terms
checkboxes that standard click actions fail on due to custom Workday widgets.

Strategy:
1. Inject __ff shadow-DOM helpers (since domhand_fill is skipped on auth pages)
2. Use __ff.queryAll() to find checkboxes ACROSS shadow DOM boundaries
3. For agreement-related ones, click via CDP Element.click() (trusted events)
4. Verify the state changed
5. Return which checkboxes were checked
"""

import asyncio
import json
import logging

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# JS to DISCOVER checkboxes and their labels using __ff for shadow DOM traversal.
# Falls back to document.querySelectorAll if __ff is not available.
_DISCOVER_CHECKBOXES_JS = r"""() => {
	var results = [];
	var keywords = [
		'i agree', 'i accept', 'i understand', 'i acknowledge',
		'i consent', 'i certify', 'privacy policy', 'terms of service',
		'terms and conditions', 'candidate consent', 'agree to',
		'terms of use', 'data privacy', 'acknowledge and agree',
		'consent to', 'privacy notice', 'create account'
	];

	function isAgreement(text) {
		var lower = (text || '').toLowerCase();
		for (var i = 0; i < keywords.length; i++) {
			if (lower.indexOf(keywords[i]) !== -1) return true;
		}
		return false;
	}

	// Use __ff.queryAll for shadow DOM traversal, fallback to document.querySelectorAll
	function queryAll(selector) {
		if (window.__ff && window.__ff.queryAll) {
			return window.__ff.queryAll(selector);
		}
		return Array.from(document.querySelectorAll(selector));
	}

	// Use __ff.getByDomId for cross-shadow-root ID lookup
	function getById(id) {
		if (window.__ff && window.__ff.getByDomId) {
			return window.__ff.getByDomId(id);
		}
		return document.getElementById(id);
	}

	// Use __ff.rootParent for cross-shadow-root parent traversal
	function getParent(el) {
		if (window.__ff && window.__ff.rootParent) {
			return window.__ff.rootParent(el);
		}
		return el.parentElement;
	}

	function getLabelText(el) {
		// 1. Ancestor label
		var label = el.closest('label');
		if (label) return label.textContent || '';

		// 2. aria-label
		var ariaLabel = el.getAttribute('aria-label');
		if (ariaLabel) return ariaLabel;

		// 3. aria-labelledby (cross shadow root)
		var labelledBy = el.getAttribute('aria-labelledby');
		if (labelledBy) {
			var ref = getById(labelledBy);
			if (ref) return ref.textContent || '';
		}

		// 4. label[for=id] (cross shadow root)
		if (el.id) {
			var forLabels = queryAll('label[for="' + el.id + '"]');
			if (forLabels.length > 0) return forLabels[0].textContent || '';
		}

		// 5. Walk up parents (cross shadow root) looking for text
		var node = getParent(el);
		for (var depth = 0; node && depth < 5; depth++) {
			var text = (node.textContent || '').trim();
			if (text.length > 5 && text.length < 500) return text;
			node = getParent(node);
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
		if (el.id) return '#' + CSS.escape(el.id);
		var automationId = el.getAttribute('data-automation-id');
		if (automationId) return '[data-automation-id="' + automationId + '"]';
		if (el.tagName === 'INPUT' && el.type === 'checkbox') {
			var name = el.getAttribute('name');
			if (name) return 'input[type="checkbox"][name="' + name + '"]';
			return 'input[type="checkbox"]';
		}
		if (el.getAttribute('role') === 'checkbox') return '[role="checkbox"]';
		return null;
	}

	// Build a selector for the CLICKABLE WRAPPER around a checkbox.
	// Many frameworks (Workday, etc.) hide the native input and overlay a
	// custom visual div.  Clicking the wrapper is what actually toggles state.
	function buildWrapperSelector(el) {
		// 1. Ancestor <label> is always a valid click target for checkboxes
		var label = el.closest('label');
		if (label) {
			if (label.id) return '#' + CSS.escape(label.id);
			var forAttr = label.getAttribute('for');
			if (forAttr) return 'label[for="' + forAttr + '"]';
		}
		// 2. Parent with data-automation-id (Workday pattern)
		var parent = el.parentElement;
		for (var depth = 0; parent && depth < 4; depth++) {
			var aid = parent.getAttribute('data-automation-id');
			if (aid) return '[data-automation-id="' + aid + '"]';
			// Parent with role=checkbox (the visual wrapper IS the checkbox)
			if (parent.getAttribute('role') === 'checkbox') {
				if (parent.id) return '#' + CSS.escape(parent.id);
				return null; // handled by the element selector itself
			}
			parent = parent.parentElement;
		}
		// 3. Immediate parent div (common wrapper pattern)
		if (el.parentElement && el.parentElement.tagName === 'DIV') {
			var pAid = el.parentElement.getAttribute('data-automation-id');
			if (pAid) return '[data-automation-id="' + pAid + '"]';
		}
		return null;
	}

	var selectors = [
		'input[type="checkbox"]',
		'[role="checkbox"]',
		'[data-automation-id*="checkbox"]',
		'[data-automation-id*="Checkbox"]',
		'[data-automation-id*="Check"]',
		'[data-automation-id*="agree"]',
		'[data-automation-id*="Agree"]',
		'[data-automation-id*="consent"]',
		'[data-automation-id*="Consent"]',
		'[data-automation-id*="acknowledge"]',
		'[data-automation-id*="terms"]',
		'[data-automation-id*="Terms"]'
	];
	var all = new Set();
	selectors.forEach(function(s) {
		try {
			queryAll(s).forEach(function(el) { all.add(el); });
		} catch(e) {}
	});

	all.forEach(function(el) {
		var labelText = getLabelText(el);
		var entry = {
			label: labelText.substring(0, 200).trim(),
			tag: el.tagName,
			role: el.getAttribute('role'),
			automationId: el.getAttribute('data-automation-id') || '',
			checked: getState(el),
			selector: buildSelector(el),
			wrapperSelector: buildWrapperSelector(el),
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

	// If still no agreement found, also check data-automation-id patterns
	// that are commonly agreement-related even without matching label text
	if (agreements.length === 0) {
		results.forEach(function(r) {
			var aid = (r.automationId || '').toLowerCase();
			if (aid.indexOf('agree') !== -1 || aid.indexOf('consent') !== -1 ||
				aid.indexOf('acknowledge') !== -1 || aid.indexOf('terms') !== -1 ||
				aid.indexOf('privacy') !== -1 || aid.indexOf('createaccount') !== -1) {
				r.isAgreement = true;
				r.fallback = 'automation_id_match';
			}
		});
	}

	return JSON.stringify(results);
}"""


class DomHandCheckAgreementParams(BaseModel):
	"""Parameters for domhand_check_agreement action."""
	# No parameters needed — it finds and checks all agreement checkboxes automatically


async def domhand_check_agreement(params: DomHandCheckAgreementParams, browser_session: BrowserSession) -> ActionResult:
	"""Find and check all agreement/consent checkboxes on the current page.

	Injects __ff shadow-DOM helpers first (since domhand_fill is skipped on
	auth pages, __ff may not be present), then uses shadow-DOM-aware discovery
	to find checkboxes across all shadow roots.
	"""
	page = await browser_session.get_current_page()
	if not page:
		return ActionResult(error="No active page found")

	# Step 0: Inject __ff shadow-DOM helpers if not already present.
	# On auth pages domhand_fill is skipped, so __ff won't exist yet.
	try:
		has_ff = await page.evaluate("() => { return !!(window.__ff); }")
		if has_ff != "true" and has_ff is not True:
			from ghosthands.dom.shadow_helpers import _build_inject_helpers_js
			await page.evaluate(_build_inject_helpers_js())
			logger.info("domhand_check_agreement.injected_ff_helpers")
	except Exception as e:
		logger.debug(f"Failed to inject __ff helpers: {e}")
		# Continue anyway — discovery JS falls back to document.querySelectorAll

	# Step 1: Wait briefly for DOM to settle after prior actions (password input
	# triggers React re-renders which can temporarily detach elements).
	await asyncio.sleep(0.5)

	# Discover checkboxes. Retry once after delay if nothing found.
	checkboxes = []
	for attempt in range(2):
		try:
			result_json = await page.evaluate(_DISCOVER_CHECKBOXES_JS)
			checkboxes = json.loads(result_json)
		except Exception as e:
			logger.warning("domhand_check_agreement.discover_error", extra={"error": str(e), "attempt": attempt})
			if attempt == 0:
				await asyncio.sleep(1.0)
				continue
			return ActionResult(error=f"Failed to discover checkboxes: {e}")

		agreements = [c for c in checkboxes if c.get("isAgreement")]
		if agreements or attempt > 0:
			break
		# Nothing found — wait for DOM to finish rendering and retry
		logger.info("domhand_check_agreement.retry_after_delay", extra={"total_found": len(checkboxes)})
		await asyncio.sleep(1.0)

	logger.info("domhand_check_agreement.discovered", extra={
		"total": len(checkboxes),
		"agreements": sum(1 for c in checkboxes if c.get("isAgreement")),
		"details": checkboxes,
	})

	# Step 2: Click each agreement checkbox.
	#
	# KEY INSIGHT: Many frameworks (Workday, etc.) hide the native <input>
	# checkbox and overlay a custom visual div/label.  Clicking the hidden
	# input does nothing visible — you must click the WRAPPER element.
	# So we try wrapper first, verify, then fall back to the input itself.
	results = []
	for cb in checkboxes:
		if not cb.get("isAgreement"):
			continue

		label = cb.get("label", "?")[:80]
		selector = cb.get("selector")
		wrapper_selector = cb.get("wrapperSelector")
		automation_id = cb.get("automationId", "")
		was_checked = cb.get("checked")

		if was_checked is True:
			results.append({"label": label, "action": "already_checked"})
			continue

		confirmed = False

		# Helper: re-check whether the checkbox is now checked.
		# Uses automationId (most unique) then selector+label to disambiguate
		# when selectors are generic (e.g. 'input[type="checkbox"]').
		async def _is_now_checked() -> bool:
			try:
				v = await page.evaluate(_DISCOVER_CHECKBOXES_JS)
				for c in json.loads(v):
					if not c.get("isAgreement"):
						continue
					# Match by automationId (most specific)
					if automation_id and c.get("automationId") == automation_id:
						return c.get("checked") is True
					# Match by selector + label prefix (disambiguate generic selectors)
					if c.get("selector") == selector and c.get("label", "")[:40] == label[:40]:
						return c.get("checked") is True
			except Exception:
				pass
			return False

		# Strategy 1: JS click on the WRAPPER element (label/parent div)
		# via __ff.queryAll() — pierces shadow DOM where CDP cannot.
		# The visual wrapper is the real click target on Workday (hidden input).
		if wrapper_selector and not confirmed:
			try:
				from ghosthands.actions._highlight import highlight_element
				await highlight_element(page, wrapper_selector)
				clicked = await page.evaluate(r"""(sel) => {
					var q = (window.__ff && window.__ff.queryAll)
						? function(s) { return window.__ff.queryAll(s); }
						: function(s) { return Array.from(document.querySelectorAll(s)); };
					var els = q(sel);
					if (els.length > 0) {
						els[0].scrollIntoView({block: 'center', behavior: 'instant'});
						els[0].click();
						return true;
					}
					return false;
				}""", wrapper_selector)
				if clicked:
					await asyncio.sleep(0.5)
					if await _is_now_checked():
						confirmed = True
						results.append({"label": label, "action": "wrapper_click", "selector": wrapper_selector})
						logger.info("domhand_check_agreement.wrapper_click", extra={"label": label, "selector": wrapper_selector})
					else:
						logger.debug(f"Wrapper click didn't toggle state for '{label}'")
			except Exception as e:
				logger.debug(f"Wrapper click failed for '{label}': {e}")

		# Strategy 2: JS click on the checkbox element itself via __ff.queryAll()
		# Pierces shadow DOM unlike CDP get_elements_by_css_selector.
		if selector and not confirmed:
			try:
				from ghosthands.actions._highlight import highlight_element
				await highlight_element(page, selector)
				clicked = await page.evaluate(r"""(sel) => {
					var q = (window.__ff && window.__ff.queryAll)
						? function(s) { return window.__ff.queryAll(s); }
						: function(s) { return Array.from(document.querySelectorAll(s)); };
					var els = q(sel);
					if (els.length > 0) {
						els[0].scrollIntoView({block: 'center', behavior: 'instant'});
						els[0].focus();
						els[0].click();
						return true;
					}
					return false;
				}""", selector)
				if clicked:
					await asyncio.sleep(0.5)
					if await _is_now_checked():
						confirmed = True
						results.append({"label": label, "action": "js_element_click", "selector": selector})
						logger.info("domhand_check_agreement.js_element_click", extra={"label": label, "selector": selector})
					else:
						logger.debug(f"JS element click didn't toggle state for '{label}'")
			except Exception as e:
				logger.debug(f"JS element click failed for '{label}': {e}")

		# Strategy 3: JS click with React-compatible event sequence
		if not confirmed:
			try:
				click_js = r"""(targetSelector) => {
					function qAll(sel) {
						if (window.__ff && window.__ff.queryAll) return window.__ff.queryAll(sel);
						return Array.from(document.querySelectorAll(sel));
					}
					var el = null;
					if (targetSelector) {
						var hits = qAll(targetSelector);
						if (hits.length > 0) el = hits[0];
					}
					if (!el) {
						var cbs = qAll('input[type="checkbox"], [role="checkbox"]');
						for (var i = 0; i < cbs.length; i++) {
							var cb = cbs[i];
							if (cb.tagName === 'INPUT' && !cb.checked) { el = cb; break; }
							if (cb.getAttribute('aria-checked') !== 'true') { el = cb; break; }
						}
					}
					if (!el) return 'not_found';

					// Try clicking the label/wrapper first, then the element
					var label = el.closest('label');
					var clickTarget = label || el.parentElement || el;
					clickTarget.scrollIntoView({ block: 'center' });
					clickTarget.click();

					// Also try the element directly with full event sequence
					el.focus();
					if (el.tagName === 'INPUT' && el.type === 'checkbox') {
						el.click();
						el.dispatchEvent(new Event('change', { bubbles: true }));
						el.dispatchEvent(new Event('input', { bubbles: true }));
					} else {
						var mouseOpts = { bubbles: true, cancelable: true, view: window };
						el.dispatchEvent(new MouseEvent('mousedown', mouseOpts));
						el.dispatchEvent(new MouseEvent('mouseup', mouseOpts));
						el.dispatchEvent(new MouseEvent('click', mouseOpts));
						var current = el.getAttribute('aria-checked');
						if (current === 'false') {
							el.setAttribute('aria-checked', 'true');
						}
					}
					return 'clicked';
				}"""
				js_result = await page.evaluate(click_js, selector)
				if js_result != "not_found":
					await asyncio.sleep(0.5)
					if await _is_now_checked():
						confirmed = True
					results.append({"label": label, "action": "js_click", "confirmed": confirmed})
					logger.info("domhand_check_agreement.js_click", extra={"label": label, "confirmed": confirmed})
			except Exception as e:
				logger.debug(f"JS click failed for '{label}': {e}")

		# Strategy 4: Broad JS click on unchecked AGREEMENT checkboxes only.
		# Filters by label text to avoid checking unrelated boxes (e.g. "Remember me").
		if not confirmed:
			try:
				broad_js = r"""() => {
					var agreeKeywords = [
						'agree', 'accept', 'understand', 'acknowledge', 'consent',
						'privacy', 'terms', 'certify', 'create account'
					];
					function isAgreementEl(el) {
						var text = '';
						var label = el.closest('label');
						if (label) text = (label.textContent || '').toLowerCase();
						else {
							var p = el.parentElement;
							for (var d = 0; p && d < 5; d++) {
								text = (p.textContent || '').toLowerCase();
								if (text.length > 5 && text.length < 500) break;
								p = p.parentElement;
							}
						}
						var aid = (el.getAttribute('data-automation-id') || '').toLowerCase();
						for (var i = 0; i < agreeKeywords.length; i++) {
							if (text.indexOf(agreeKeywords[i]) !== -1) return true;
							if (aid.indexOf(agreeKeywords[i]) !== -1) return true;
						}
						return false;
					}
					function qAll(sel) {
						if (window.__ff && window.__ff.queryAll) return window.__ff.queryAll(sel);
						return Array.from(document.querySelectorAll(sel));
					}
					var count = 0;
					var cbs = qAll('input[type="checkbox"], [role="checkbox"]');
					cbs.forEach(function(el) {
						var isUnchecked = (el.tagName === 'INPUT' && !el.checked) ||
							(el.getAttribute('aria-checked') === 'false');
						if (isUnchecked && isAgreementEl(el)) {
							// Click wrapper first (label or parent), then element
							var wrapper = el.closest('label') || el.parentElement;
							if (wrapper && wrapper !== el) {
								wrapper.click();
							}
							el.focus();
							el.click();
							if (el.tagName === 'INPUT') {
								el.dispatchEvent(new Event('change', { bubbles: true }));
							}
							count++;
						}
					});
					return count;
				}"""
				count = await page.evaluate(broad_js)
				await asyncio.sleep(0.5)
				if await _is_now_checked():
					confirmed = True
				results.append({"label": label, "action": "js_broad_click", "count": count, "confirmed": confirmed})
				logger.info("domhand_check_agreement.js_broad_click", extra={"label": label, "count": count, "confirmed": confirmed})
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
	confirmed_count = sum(1 for r in results if r.get("action") in ("wrapper_click", "js_element_click")
						  or r.get("confirmed") is True)
	already_count = sum(1 for r in results if r.get("action") == "already_checked")
	unconfirmed_count = sum(1 for r in results
							if r.get("action") not in ("already_checked", "failed", "wrapper_click", "js_element_click")
							and r.get("confirmed") is not True)
	failed_count = sum(1 for r in results if r.get("action") == "failed")

	# Check if any agreement checkbox ended up still unchecked
	still_unchecked = [lbl for lbl, state in final_state.items() if state is not True]

	summary_parts = []
	if confirmed_count:
		summary_parts.append(f"{confirmed_count} checkbox(es) checked successfully")
	if already_count:
		summary_parts.append(f"{already_count} already checked")
	if unconfirmed_count:
		summary_parts.append(f"{unconfirmed_count} clicked but state unconfirmed")
	if failed_count:
		summary_parts.append(f"{failed_count} failed")
	if still_unchecked:
		summary_parts.append(f"WARNING: still unchecked after clicking: {still_unchecked}")
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
		detail_lines.append(f"  - \"{r.get('label', '?')}\" -> {r.get('action', 'unknown')}")
	if final_state:
		detail_lines.append(f"  Final state: {final_state}")
	if still_unchecked:
		detail_lines.append(
			"  The checkbox may still be unchecked. Try clicking it manually "
			"with the regular click action before clicking Create Account."
		)

	return ActionResult(
		extracted_content="\n".join(detail_lines),
		include_in_memory=True,
	)
