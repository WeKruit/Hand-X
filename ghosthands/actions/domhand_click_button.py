"""DomHand Click Button — robust button clicking for React-heavy ATS sites.

browser-use's default click uses CDP Input.dispatchMouseEvent which works for most
elements but can fail on React form-submission buttons (Workday, etc.) that require
a specific event sequence or check form validation state.

This action:
1. Finds the button via JS (text match, aria-label, data-automation-id)
2. Clicks it using multiple strategies:
   - JS full event sequence (focus → mousedown → mouseup → click)
   - CSS selector + browser-use Element.click() (CDP coordinates)
   - Direct form.submit()
   - Keyboard Enter on focused button (for React forms)
3. Verifies the page transitioned (URL change or DOM mutation)

Use this for auth buttons (Create Account, Sign In) or form submission
buttons that the regular click action doesn't trigger.
"""

import asyncio
import json
import logging

from pydantic import BaseModel, Field

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from ghosthands.actions._highlight import highlight_element

logger = logging.getLogger(__name__)

# JS: Find the best-matching button by label, preferring the active dialog/form.
# Uses __ff helpers when available so buttons inside open shadow roots are visible.
_FIND_AND_CLICK_JS = r"""(label, shouldClick) => {
	var ff = window.__ff || null;
	var lower = (label || '').toLowerCase().trim();
	var labelWords = lower.split(/\s+/).filter(Boolean);
	var results = { found: false, buttons: [], topCandidates: [] };

	function queryAll(selector) {
		if (ff && ff.queryAll) return ff.queryAll(selector);
		return Array.from(document.querySelectorAll(selector));
	}

	function rootParent(node) {
		if (ff && ff.rootParent) return ff.rootParent(node);
		if (!node) return null;
		if (node.parentElement) return node.parentElement;
		var root = node.getRootNode ? node.getRootNode() : null;
		if (root && root.host) return root.host;
		return null;
	}

	function closestCrossRoot(node, selector) {
		if (ff && ff.closestCrossRoot) return ff.closestCrossRoot(node, selector);
		return node && node.closest ? node.closest(selector) : null;
	}

	function deepActiveElement(root) {
		var active = (root && root.activeElement) || document.activeElement;
		while (active && active.shadowRoot && active.shadowRoot.activeElement) {
			active = active.shadowRoot.activeElement;
		}
		return active;
	}

	function isVisible(el) {
		if (!el) return false;
		if (ff && ff.isVisible && !ff.isVisible(el)) return false;
		var rect = el.getBoundingClientRect();
		if (rect.width === 0 || rect.height === 0) return false;
		var style = window.getComputedStyle(el);
		if (style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none') return false;
		return true;
	}

	function normalize(text) {
		return (text || '').replace(/\s+/g, ' ').trim();
	}

	function getButtonText(el) {
		var candidates = [];
		try {
			if (ff && ff.getAccessibleName) {
				candidates.push(normalize(ff.getAccessibleName(el)));
			}
		} catch (e) {}
		candidates.push(normalize(el.textContent || ''));
		candidates.push(normalize(el.value || ''));
		candidates.push(normalize(el.getAttribute('aria-label') || ''));
		candidates.push(normalize(el.getAttribute('title') || ''));
		for (var i = 0; i < candidates.length; i++) {
			if (candidates[i]) return candidates[i];
		}
		return '';
	}

	function findForm(el) {
		var node = el;
		for (var depth = 0; node && depth < 12; depth++) {
			if (node.tagName === 'FORM') return node;
			node = rootParent(node);
		}
		return null;
	}

	function getContainerStats(container, activeEl) {
		var stats = {
			visibleInputs: 0,
			filledInputs: 0,
			passwordInputs: 0,
			emailInputs: 0,
			hasActive: false,
		};
		if (!container || !container.querySelectorAll) return stats;
		var inputs = container.querySelectorAll('input, textarea, select');
		for (var i = 0; i < inputs.length; i++) {
			var input = inputs[i];
			if (!isVisible(input)) continue;
			stats.visibleInputs += 1;
			var type = (input.getAttribute('type') || input.type || input.tagName || '').toLowerCase();
			var inputName = (input.name || input.id || '').toLowerCase();
			var value = '';
			try {
				value = normalize(input.value || input.textContent || '');
			} catch (e) {}
			if (type === 'password') stats.passwordInputs += 1;
			if (type === 'email' || inputName.indexOf('email') !== -1) stats.emailInputs += 1;
			if (value) stats.filledInputs += 1;
			if (activeEl && (input === activeEl || (input.contains && input.contains(activeEl)))) {
				stats.hasActive = true;
			}
		}
		return stats;
	}

	function buildSelector(el, automationId) {
		if (!el) return null;
		var root = el.getRootNode ? el.getRootNode() : document;
		if (root !== document) return null;
		if (el.id) return '#' + CSS.escape(el.id);
		if (automationId) return '[data-automation-id="' + automationId + '"]';
		var ariaLabel = el.getAttribute('aria-label');
		if (ariaLabel) return '[aria-label="' + ariaLabel.replace(/"/g, '\\"') + '"]';
		return null;
	}

	var activeEl = deepActiveElement(document);
	var allButtons = queryAll(
		'button, [role="button"], input[type="submit"], input[type="button"], [data-automation-id*="utton"]'
	);
	var candidates = [];

	for (var i = 0; i < allButtons.length; i++) {
		var btn = allButtons[i];
		if (!isVisible(btn)) continue;

		var rect = btn.getBoundingClientRect();
		var text = getButtonText(btn);
		var ariaLabel = normalize(btn.getAttribute('aria-label') || '');
		var automationId = btn.getAttribute('data-automation-id') || '';
		var displayText = text || ariaLabel;
		var role = btn.getAttribute('role') || '';
		var type = btn.getAttribute('type') || '';
		var disabled = !!btn.disabled || btn.getAttribute('aria-disabled') === 'true';
		var textLower = displayText.toLowerCase();
		var aidLower = automationId.toLowerCase();
		var ariaLower = ariaLabel.toLowerCase();
		var root = btn.getRootNode ? btn.getRootNode() : document;
		var inShadowRoot = root !== document;
		var form = findForm(btn);
		var dialog = closestCrossRoot(btn, '[role="dialog"], [aria-modal="true"], dialog, [data-automation-id*="modal"], [data-automation-id*="Modal"]');
		var inHeader = !!closestCrossRoot(btn, 'header, nav, [role="banner"], [role="navigation"]');
		var inTablist = role === 'tab' || !!closestCrossRoot(btn, '[role="tablist"]');
		var ariaSelected = btn.getAttribute('aria-selected');
		var formStats = getContainerStats(form, activeEl);
		var dialogStats = getContainerStats(dialog, activeEl);

		results.buttons.push({
			text: displayText.substring(0, 100),
			tag: btn.tagName,
			role: role,
			type: type,
			automationId: automationId,
			disabled: disabled,
			inShadowRoot: inShadowRoot,
			inDialog: !!dialog,
			inForm: !!form,
		});

		if (disabled) continue;

		var exactMatch = textLower === lower || ariaLower === lower;
		var containsMatch = (textLower && textLower.indexOf(lower) !== -1) || (ariaLower && ariaLower.indexOf(lower) !== -1);
		var wordMatch = labelWords.length > 0 && labelWords.every(function(word) {
			return textLower.indexOf(word) !== -1 || ariaLower.indexOf(word) !== -1;
		});
		var automationMatch = labelWords.length > 0 && labelWords.every(function(word) {
			return aidLower.indexOf(word) !== -1;
		});

		if (!exactMatch && !containsMatch && !wordMatch && !automationMatch) continue;

		var matchKind = 'automation_id';
		var score = 0;
		if (exactMatch) {
			matchKind = 'exact';
			score += 120;
		} else if (containsMatch) {
			matchKind = 'contains';
			score += 90;
		} else if (wordMatch) {
			matchKind = 'word_match';
			score += 75;
		} else {
			score += 55;
		}

		if (dialog) score += 35;
		if (form) score += 25;
		if (formStats.passwordInputs > 0) score += 45;
		if (formStats.emailInputs > 0) score += 20;
		if (dialogStats.passwordInputs > 0 && !form) score += 20;
		if (formStats.filledInputs > 0) score += Math.min(45, formStats.filledInputs * 15);
		if (formStats.hasActive) score += 35;
		if ((type || '').toLowerCase() === 'submit') score += 45;
		if (aidLower.indexOf('submit') !== -1 || aidLower.indexOf('continue') !== -1 || aidLower.indexOf('next') !== -1) score += 20;
		if (rect.top > window.innerHeight * 0.35) score += 10;
		if (inHeader) score -= 35;
		if (inTablist) score -= 120;
		if (ariaSelected === 'true') score -= 60;

		var ffId = null;
		try {
			ffId = ff && ff.tag ? ff.tag(btn) : null;
		} catch (e) {}

		candidates.push({
			element: btn,
			ffId: ffId,
			text: displayText,
			tag: btn.tagName,
			role: role,
			type: type,
			automationId: automationId,
			matchKind: matchKind,
			exactMatch: exactMatch,
			score: score,
			inDialog: !!dialog,
			inForm: !!form,
			inHeader: inHeader,
			inTablist: inTablist,
			inShadowRoot: inShadowRoot,
			formStats: formStats,
			selector: buildSelector(btn, automationId),
			scope: (dialog ? 'dialog' : 'page') + (form ? '>form' : ''),
		});
	}

	candidates.sort(function(a, b) {
		if (b.score !== a.score) return b.score - a.score;
		if (a.inShadowRoot !== b.inShadowRoot) return a.inShadowRoot ? -1 : 1;
		return (b.formStats.filledInputs || 0) - (a.formStats.filledInputs || 0);
	});

	results.topCandidates = candidates.slice(0, 6).map(function(candidate) {
		return {
			text: candidate.text.substring(0, 100),
			tag: candidate.tag,
			role: candidate.role,
			type: candidate.type,
			automationId: candidate.automationId,
			matchKind: candidate.matchKind,
			score: candidate.score,
			scope: candidate.scope,
			inShadowRoot: candidate.inShadowRoot,
			filledInputs: candidate.formStats.filledInputs,
			hasPasswordField: candidate.formStats.passwordInputs > 0,
			hasActiveInput: candidate.formStats.hasActive,
			selector: candidate.selector,
			ffId: candidate.ffId,
		};
	});

	if (candidates.length === 0) {
		return JSON.stringify(results);
	}

	var target = candidates[0];
	results.found = true;
	results.ffId = target.ffId;
	results.text = target.text;
	results.automationId = target.automationId;
	results.tag = target.tag;
	results.role = target.role;
	results.type = target.type;
	results.matchKind = target.matchKind;
	results.score = target.score;
	results.scope = target.scope;
	results.inShadowRoot = target.inShadowRoot;
	results.selector = target.selector;

	var el = target.element;
	if (shouldClick) {
		try {
			var targetRect = el.getBoundingClientRect();
			el.scrollIntoView({ block: 'center', behavior: 'instant' });
			el.focus();
			var mouseOpts = { bubbles: true, cancelable: true, view: window };
			var pointerOpts = { bubbles: true, cancelable: true, composed: true, pointerType: 'mouse', isPrimary: true };
			el.dispatchEvent(new PointerEvent('pointerdown', { button: 0, buttons: 1, clientX: targetRect.left + 4, clientY: targetRect.top + 4, ...pointerOpts }));
			el.dispatchEvent(new MouseEvent('mouseenter', mouseOpts));
			el.dispatchEvent(new MouseEvent('mouseover', mouseOpts));
			el.dispatchEvent(new MouseEvent('mousedown', { ...mouseOpts, button: 0 }));
			el.dispatchEvent(new PointerEvent('pointerup', { button: 0, buttons: 0, clientX: targetRect.left + 4, clientY: targetRect.top + 4, ...pointerOpts }));
			el.dispatchEvent(new MouseEvent('mouseup', { ...mouseOpts, button: 0 }));
			el.dispatchEvent(new MouseEvent('click', { ...mouseOpts, button: 0 }));
			results.clicked = 'js_event_sequence';
		} catch(e) {
			results.clickError = e.message;
		}

        if (!results.clicked) {
            try {
                el.click();
                results.clicked = 'native_click';
            } catch(e2) {
                if (!results.clickError) results.clickError = e2.message;
            }
        }
		results.hasForm = !!findForm(el);
	}

	return JSON.stringify(results);
}"""

# JS: Submit the form containing the chosen button.
_SUBMIT_FORM_JS = r"""(ffId) => {
	var ff = window.__ff || {};

	function byId(id) {
		if (!id) return null;
		if (ff.byId) return ff.byId(id);
		return document.querySelector('[data-ff-id="' + id + '"]');
	}

	function rootParent(node) {
		if (ff.rootParent) return ff.rootParent(node);
		if (!node) return null;
		if (node.parentElement) return node.parentElement;
		var root = node.getRootNode ? node.getRootNode() : null;
		if (root && root.host) return root.host;
		return null;
	}

	function findForm(node) {
		var current = node;
		for (var depth = 0; current && depth < 12; depth++) {
			if (current.tagName === 'FORM') return current;
			current = rootParent(current);
		}
		return null;
	}

	var button = byId(ffId);
	if (!button) {
		return JSON.stringify({ submitted: false, error: 'button not found' });
	}

	var form = findForm(button);
	if (!form) {
		try {
			button.click();
			return JSON.stringify({ clicked: true, method: 'button.click', noForm: true });
		} catch (e) {
			return JSON.stringify({ submitted: false, error: e.message || 'no form found' });
		}
	}

	try {
		if (typeof form.requestSubmit === 'function') {
			form.requestSubmit(button);
			return JSON.stringify({ submitted: true, method: 'requestSubmit' });
		}
	} catch (e1) {}

	try {
		var submitEvent = new Event('submit', { bubbles: true, cancelable: true });
		var dispatched = form.dispatchEvent(submitEvent);
		if (dispatched && typeof form.submit === 'function') {
			form.submit();
			return JSON.stringify({ submitted: true, method: 'dispatchEvent+submit' });
		}
		return JSON.stringify({ submitted: dispatched, method: 'dispatchEvent', prevented: !dispatched });
	} catch (e2) {}

	try {
		form.submit();
		return JSON.stringify({ submitted: true, method: 'submit' });
	} catch (e3) {
		return JSON.stringify({ submitted: false, error: e3.message || 'submit failed' });
	}
}"""

# JS: Focus the chosen button before pressing Enter.
_FOCUS_BUTTON_JS = r"""(ffId) => {
	var ff = window.__ff || {};
	var el = ff.byId ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return JSON.stringify({ focused: false });
	el.scrollIntoView({ block: 'center', behavior: 'instant' });
	el.focus();
	return JSON.stringify({
		focused: document.activeElement === el || (el.shadowRoot && el.shadowRoot.activeElement != null),
		tag: el.tagName,
	});
}"""

# JS: Page fingerprint for logging whether the DOM meaningfully changed.
_PAGE_FINGERPRINT_JS = r"""() => {
	var ff = window.__ff || null;

	function queryAll(selector) {
		if (ff && ff.queryAll) return ff.queryAll(selector);
		return Array.from(document.querySelectorAll(selector));
	}

	function normalize(text) {
		return (text || '').replace(/\s+/g, ' ').trim();
	}

	function texts(selector, limit) {
		return queryAll(selector)
			.map(function(el) { return normalize(el.textContent || el.value || el.getAttribute('aria-label') || ''); })
			.filter(Boolean)
			.slice(0, limit);
	}

	return JSON.stringify({
		url: window.location.href,
		title: document.title,
		headings: texts('h1, h2, h3, [role="heading"], [data-automation-id*="pageHeader"], [data-automation-id*="stepTitle"]', 6),
		buttons: texts('button, [role="button"], input[type="submit"]', 8),
		passwordInputs: queryAll('input[type="password"]').length,
		formCount: queryAll('form').length,
		errorCount: queryAll('[role="alert"], [aria-invalid="true"], [class*="error"], [class*="invalid"]').length,
	});
}"""

# JS: Check current page state for diagnostics across shadow roots.
_PAGE_STATE_JS = r"""() => {
	var ff = window.__ff || null;
	var state = {
		url: window.location.href,
		title: document.title,
		forms: [],
		hiddenErrors: []
	};

	function queryAll(selector) {
		if (ff && ff.queryAll) return ff.queryAll(selector);
		return Array.from(document.querySelectorAll(selector));
	}

	function rootParent(node) {
		if (ff && ff.rootParent) return ff.rootParent(node);
		if (!node) return null;
		if (node.parentElement) return node.parentElement;
		var root = node.getRootNode ? node.getRootNode() : null;
		if (root && root.host) return root.host;
		return null;
	}

	function getLabelText(el) {
		var node = el;
		for (var depth = 0; node && depth < 5; depth++) {
			if (node.getAttribute && node.getAttribute('aria-label')) return node.getAttribute('aria-label');
			if (node.tagName === 'LABEL') return (node.textContent || '').trim();
			node = rootParent(node);
		}
		return '';
	}

	queryAll('form').forEach(function(form, i) {
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

	var errorPatterns = [
		'[class*="error"]', '[class*="Error"]',
		'[class*="invalid"]', '[class*="Invalid"]',
		'[role="alert"]', '[aria-invalid="true"]'
	];
	errorPatterns.forEach(function(sel) {
		queryAll(sel).forEach(function(el) {
			var text = (el.textContent || '').trim();
			if (text && text.length < 200) {
				state.hiddenErrors.push({ selector: sel, text: text });
			}
		});
	});

	var checkboxes = [];
	queryAll('input[type="checkbox"], [role="checkbox"]').forEach(function(cb) {
		var label = getLabelText(cb);
		checkboxes.push({
			checked: cb.checked || cb.getAttribute('aria-checked') === 'true',
			label: label.trim().substring(0, 100)
		});
	});
	state.checkboxes = checkboxes;
	state.authInputs = {
		passwords: queryAll('input[type="password"]').length,
		emails: queryAll('input[type="email"]').length,
	};

	return JSON.stringify(state);
}"""


class DomHandClickButtonParams(BaseModel):
    """Parameters for domhand_click_button action."""

    button_label: str = Field(
        description=(
            'The visible text label of the button to click (e.g., "Create Account", "Sign In", "Next", "Continue").'
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

    label = params.button_label.strip()
    logger.info("domhand_click_button.start", extra={"label": label})

    try:
        from ghosthands.dom.shadow_helpers import ensure_helpers

        await ensure_helpers(page)
    except Exception as e:
        logger.debug(f"Could not inject __ff helpers: {e}")

    # Capture URL before click to detect navigation
    try:
        url_before = await page.get_url()
    except Exception:
        url_before = ""

    fingerprint_before = {}
    try:
        fingerprint_before = json.loads(await page.evaluate(_PAGE_FINGERPRINT_JS))
    except Exception:
        fingerprint_before = {}

    def _candidate_log_payload(result: dict) -> dict:
        return {
            "label": label,
            "chosen_text": result.get("text", ""),
            "match_kind": result.get("matchKind"),
            "score": result.get("score"),
            "scope": result.get("scope"),
            "in_shadow_root": result.get("inShadowRoot"),
            "selector": result.get("selector"),
            "top_candidates": result.get("topCandidates", [])[:5],
        }

    async def _capture_transition_state() -> tuple[str, dict, bool]:
        try:
            url_after = await page.get_url()
        except Exception:
            url_after = url_before

        fingerprint_after = {}
        try:
            fingerprint_after = json.loads(await page.evaluate(_PAGE_FINGERPRINT_JS))
        except Exception:
            fingerprint_after = {}

        state_changed = bool(fingerprint_before and fingerprint_after and fingerprint_before != fingerprint_after)
        return url_after, fingerprint_after, state_changed

    async def _wait_for_transition(timeout_seconds: float = 4.5, poll_interval: float = 0.45) -> tuple[str, dict, bool]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        url_after = url_before
        fingerprint_after: dict = {}
        state_changed = False
        while True:
            await asyncio.sleep(poll_interval)
            url_after, fingerprint_after, state_changed = await _capture_transition_state()
            if url_before != url_after or state_changed:
                return url_after, fingerprint_after, state_changed
            if loop.time() >= deadline:
                return url_after, fingerprint_after, state_changed

    # ── Strategy 1: preview, highlight, then JS event sequence ────────────
    try:
        preview_json = await page.evaluate(_FIND_AND_CLICK_JS, label, False)
        preview_result = json.loads(preview_json)

        if preview_result.get("topCandidates"):
            logger.info("domhand_click_button.candidates", extra=_candidate_log_payload(preview_result))

        if preview_result.get("found"):
            highlight_selector = preview_result.get("selector")
            if not highlight_selector and preview_result.get("ffId"):
                highlight_selector = f'[data-ff-id="{preview_result["ffId"]}"]'
            if highlight_selector:
                await highlight_element(page, highlight_selector)

        result_json = await page.evaluate(_FIND_AND_CLICK_JS, label, True)
        result = json.loads(result_json)
        if not result.get("topCandidates") and preview_result.get("topCandidates"):
            result["topCandidates"] = preview_result["topCandidates"]
        if not result.get("selector") and preview_result.get("selector"):
            result["selector"] = preview_result["selector"]
        if not result.get("ffId") and preview_result.get("ffId"):
            result["ffId"] = preview_result["ffId"]
        if result.get("inShadowRoot") is None and preview_result.get("inShadowRoot") is not None:
            result["inShadowRoot"] = preview_result["inShadowRoot"]

        if result.get("found") and result.get("clicked"):
            url_after, fingerprint_after, state_changed = await _wait_for_transition()

            method = result.get("clicked", "unknown")
            logger.info(
                "domhand_click_button.js_click",
                extra={
                    "label": label,
                    "method": method,
                    "text": result.get("text", ""),
                    "url_changed": url_before != url_after,
                    "state_changed": state_changed,
                    "fingerprint_after": fingerprint_after,
                },
            )

            if url_before != url_after:
                return ActionResult(
                    extracted_content=f"DomHand click: clicked '{label}' via JS event sequence. Page navigated to {url_after}.",
                    include_in_memory=True,
                )
            if state_changed:
                return ActionResult(
                    extracted_content=f"DomHand click: clicked '{label}' via JS event sequence. Page content changed (same-page transition). Check if auth succeeded.",
                    include_in_memory=True,
                )

    except Exception as e:
        logger.debug(f"JS find+click failed for '{label}': {e}")
        result = {"found": False, "buttons": [], "topCandidates": []}

    # ── Strategy 2: CSS selector + Element.click() (CDP mouse events) ──
    if result.get("found") and result.get("selector") and not result.get("inShadowRoot"):
        selector = result["selector"]
        try:
            elements = await page.get_elements_by_css_selector(selector)
            if elements:
                await elements[0].click()
                url_after, fingerprint_after, state_changed = await _wait_for_transition()

                logger.info(
                    "domhand_click_button.cdp_click",
                    extra={
                        "label": label,
                        "selector": selector,
                        "url_changed": url_before != url_after,
                        "state_changed": state_changed,
                        "fingerprint_after": fingerprint_after,
                    },
                )

                if url_before != url_after:
                    return ActionResult(
                        extracted_content=f"DomHand click: clicked '{label}' via CDP Element.click(). Page navigated to {url_after}.",
                        include_in_memory=True,
                    )
                if state_changed:
                    return ActionResult(
                        extracted_content=f"DomHand click: clicked '{label}' via CDP Element.click(). Page content changed (same-page transition). Check if auth succeeded.",
                        include_in_memory=True,
                    )
        except Exception as e:
            logger.debug(f"CDP Element click failed for '{label}': {e}")

    # ── Strategy 3: form.submit() as last resort ───────────────────
    if result.get("found") and result.get("ffId"):
        try:
            submit_json = await page.evaluate(_SUBMIT_FORM_JS, result["ffId"])
            submit_result = json.loads(submit_json)

            if submit_result.get("submitted") or submit_result.get("clicked"):
                url_after, fingerprint_after, state_changed = await _wait_for_transition()

                logger.info(
                    "domhand_click_button.form_submit",
                    extra={
                        "label": label,
                        "method": submit_result.get("method"),
                        "url_changed": url_before != url_after,
                        "state_changed": state_changed,
                        "fingerprint_after": fingerprint_after,
                    },
                )

                if url_before != url_after:
                    return ActionResult(
                        extracted_content=f"DomHand click: submitted form for '{label}'. Page navigated to {url_after}.",
                        include_in_memory=True,
                    )
                if state_changed:
                    return ActionResult(
                        extracted_content=f"DomHand click: submitted form for '{label}'. Page content changed (same-page transition). Check if auth succeeded.",
                        include_in_memory=True,
                    )
        except Exception as e:
            logger.debug(f"Form submit failed for '{label}': {e}")

    # ── Strategy 4: Keyboard Enter on the focused button ─────────────
    # React forms often only respond to keyboard Enter, not synthetic clicks.
    # Focus the button (or last input in the form) and press Enter via Playwright.
    if result.get("found") and result.get("ffId"):
        try:
            focus_result = json.loads(await page.evaluate(_FOCUS_BUTTON_JS, result["ffId"]))

            if focus_result.get("focused"):
                await page.keyboard.press("Enter")
                url_after, fingerprint_after, state_changed = await _wait_for_transition()

                logger.info(
                    "domhand_click_button.enter_key",
                    extra={
                        "label": label,
                        "url_changed": url_before != url_after,
                        "state_changed": state_changed,
                        "fingerprint_after": fingerprint_after,
                    },
                )

                if url_before != url_after:
                    return ActionResult(
                        extracted_content=f"DomHand click: pressed Enter on '{label}'. Page navigated to {url_after}.",
                        include_in_memory=True,
                    )
                if state_changed:
                    return ActionResult(
                        extracted_content=f"DomHand click: pressed Enter on '{label}'. Page content changed (same-page transition). Check if auth succeeded.",
                        include_in_memory=True,
                    )
        except Exception as e:
            logger.debug(f"Enter key failed for '{label}': {e}")

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
        auth_inputs = diagnostics.get("authInputs", {})

        unchecked_boxes = [cb for cb in checkboxes if not cb.get("checked")]
        agreement_keywords = [
            "agree",
            "accept",
            "consent",
            "terms",
            "privacy",
            "acknowledge",
            "i understand",
            "certify",
        ]
        agreement_unchecked = [
            cb
            for cb in unchecked_boxes
            if any(keyword in (cb.get("label", "") or "").lower() for keyword in agreement_keywords)
        ]

        # ── Auto-fix: only agreement-style checkboxes should be auto-checked.
        if agreement_unchecked:
            logger.info(
                "domhand_click_button.auto_checking_boxes",
                extra={
                    "label": label,
                    "unchecked": [cb.get("label", "?")[:60] for cb in agreement_unchecked],
                },
            )
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
                    url_after, _fingerprint_after, _state_changed = await _wait_for_transition()

                    if url_before != url_after:
                        logger.info(
                            "domhand_click_button.retry_after_checkbox_success",
                            extra={
                                "label": label,
                            },
                        )
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
        detail_parts.append(
            f"Chosen candidate: text='{result.get('text', '')}', scope={result.get('scope')}, "
            f"match={result.get('matchKind')}, score={result.get('score')}, "
            f"in_shadow_root={result.get('inShadowRoot')}."
        )

        alternatives = [c.get("text", "")[:60] for c in result.get("topCandidates", [])[1:4] if c.get("text")]
        if alternatives:
            detail_parts.append(f"Other matching candidates seen: {alternatives}")

        if hidden_errors:
            error_texts = [e.get("text", "")[:80] for e in hidden_errors[:5]]
            detail_parts.append(f"Hidden errors found: {error_texts}")

        if agreement_unchecked:
            labels = [cb.get("label", "?")[:60] for cb in agreement_unchecked]
            detail_parts.append(
                f"UNCHECKED AGREEMENT CHECKBOXES DETECTED: {labels}. "
                "An unchecked agreement checkbox is the most likely cause. "
                "Use domhand_check_agreement or click the checkbox manually, "
                "then try domhand_click_button again."
            )
        elif unchecked_boxes:
            labels = [cb.get("label", "?")[:60] for cb in unchecked_boxes]
            detail_parts.append(
                f"Unchecked non-agreement checkboxes seen: {labels}. "
                "They were not auto-checked."
            )

        invalid_forms = [f for f in forms if not f.get("valid")]
        if invalid_forms:
            for f in invalid_forms:
                invalid_fields = [fld.get("name", "?") + ": " + fld.get("message", "?") for fld in f.get("fields", [])]
                detail_parts.append(f"Form validation errors: {invalid_fields}")

        if auth_inputs:
            detail_parts.append(
                f"Auth inputs still visible after click: passwords={auth_inputs.get('passwords', 0)}, "
                f"emails={auth_inputs.get('emails', 0)}."
            )

        if not unchecked_boxes and not hidden_errors and not invalid_forms:
            detail_parts.append(
                "The button was clicked but the page did not transition. "
                "Possible causes: the site blocks automated clicks, or a "
                "required field is missing. Check for error messages."
            )

        logger.warning(
            "domhand_click_button.no_transition",
            extra={
                "label": label,
                "chosen_candidate": _candidate_log_payload(result),
                "hidden_errors": hidden_errors[:3],
                "checkboxes": checkboxes,
                "forms": forms,
                "auth_inputs": auth_inputs,
            },
        )

        return ActionResult(
            extracted_content="\n".join(detail_parts),
            include_in_memory=True,
        )

    # Button not found at all
    visible_buttons = [b.get("text", "?")[:60] for b in result.get("buttons", []) if not b.get("disabled")]

    logger.warning(
        "domhand_click_button.not_found",
        extra={
            "label": label,
            "visible_buttons": visible_buttons,
            "top_candidates": result.get("topCandidates", [])[:5],
        },
    )

    return ActionResult(
        error=f"Could not find button '{label}' on page. "
        f"Visible buttons: {visible_buttons[:10]}. "
        f"Try using the exact text shown on the button.",
    )
