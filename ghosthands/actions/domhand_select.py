"""DomHand Select — dropdown selection using platform-aware discovery.

Handles complex dropdowns that domhand_fill can't handle: custom widgets,
Workday portals with hierarchical dropdowns, combobox/listbox patterns, etc.

Flow:
1. Click the dropdown trigger element
2. Wait for listbox/options to appear
3. Discover available options (native <select>, ARIA listbox, or custom widgets)
4. Fuzzy-match the target value against available options
5. Click the matching option
6. Verify the selection stuck

Trigger discovery uses browser-use's selector map (get_element_by_index) plus
CDP ``Runtime.callFunctionOn`` on the indexed node. Option picking prefers
Playwright locators (full user-gesture pipeline), then page-level JS click
fallbacks that include Oracle-style ``role=gridcell`` list rows.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import (
    ClickElementEvent,
    GetDropdownOptionsEvent,
    SelectDropdownOptionEvent,
)
from browser_use.dom.views import EnhancedDOMTreeNode
from ghosthands.actions.combobox_toggle import CLICK_COMBOBOX_TOGGLE_ON_NODE_JS, combobox_toggle_clicked
from ghosthands.actions.domhand_fill import _get_page_context_key, _record_expected_value_if_settled
from ghosthands.actions.views import (
    DomHandSelectParams,
    FormField,
    generate_dropdown_search_terms,
    normalize_name,
    split_dropdown_value_hierarchy,
)
from ghosthands.runtime_learning import (
    DOMHAND_RETRY_CAP,
    clear_domhand_failure,
    detect_host_from_url,
    get_domhand_failure_count,
    is_domhand_retry_capped,
    record_domhand_failure,
)
from ghosthands.step_trace import publish_browser_session_trace, update_blocker_attempt_state

logger = logging.getLogger(__name__)

FAIL_OVER_NATIVE_SELECT = "[FAIL-OVER:NATIVE_SELECT]"
FAIL_OVER_CUSTOM_WIDGET = "[FAIL-OVER:CUSTOM_WIDGET]"
DOMHAND_RETRY_CAPPED = "domhand_retry_capped"


def _profile_debug_enabled() -> bool:
    return os.getenv("GH_DEBUG_PROFILE_PASS_THROUGH") == "1"


# ── CDP-based JavaScript helpers ─────────────────────────────────────
# These JS functions operate on a node passed directly via Runtime.callFunctionOn
# (i.e., `this` is the DOM element). They never use data-highlight-index.

_DISCOVER_OPTIONS_ON_NODE_JS = r"""function() {
	const el = this;
	const tag = el.tagName.toLowerCase();
	const visible = (node) => {
		if (!node) return false;
		const style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		const rect = node.getBoundingClientRect();
		return rect.width > 0 && rect.height > 0;
	};
	const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
	const visibleText = (node) => {
		if (!visible(node)) return '';
		return clean(node.textContent || node.getAttribute('aria-label') || '');
	};

	// Helper: use __ff.queryAll for cross-shadow-root traversal, fallback to document
	const qAll = (sel) => (window.__ff && window.__ff.queryAll)
		? window.__ff.queryAll(sel)
		: Array.from(document.querySelectorAll(sel));
	const qById = (id) => (window.__ff && window.__ff.getByDomId)
		? window.__ff.getByDomId(id)
		: document.getElementById(id);
	const hasInvalidState = (node) => {
		if (!node) return false;
		const wrapper = node.closest('[aria-invalid], [data-automation-id="formField"], [data-automation-id*="formField"], .form-group, .field, fieldset, [role="group"], [role="radiogroup"]') || node.parentElement || node;
		if (node.getAttribute && node.getAttribute('aria-invalid') === 'true') return true;
		if (wrapper && wrapper.getAttribute && wrapper.getAttribute('aria-invalid') === 'true') return true;
		return !!(wrapper && wrapper.querySelector && wrapper.querySelector('[aria-invalid="true"]'));
	};

	// Case 1: Native <select>
	if (tag === 'select') {
		const options = Array.from(el.options).map((o, i) => ({
			text: o.text.trim(),
			value: o.value,
			index: i,
			selected: o.selected,
		}));
		return JSON.stringify({
			type: 'native_select',
			options: options,
			currentValue: el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : '',
		});
	}

	// Case 2: ARIA combobox/listbox
	const listboxId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
	if (listboxId) {
		const listbox = qById(listboxId);
		if (listbox) {
			const options = Array.from(
				listbox.querySelectorAll('[role="option"], [role="gridcell"], [role="listitem"], li, [class*="option"], [data-value]')
			).map((o, i) => ({
				text: o.textContent.trim(),
				value: o.getAttribute('data-value') || o.getAttribute('value') || o.textContent.trim(),
				index: i,
				selected: o.getAttribute('aria-selected') === 'true' || o.classList.contains('selected'),
			}));
			return JSON.stringify({
				type: 'aria_listbox',
				listboxId: listboxId,
				options: options,
				currentValue: el.value || (el.textContent ? el.textContent.trim() : ''),
			});
		}
	}

	// Case 3: Workday-style custom dropdowns (data-uxi-widget-type)
	const widgetType = el.getAttribute('data-uxi-widget-type');
	if (widgetType === 'selectinput' || el.getAttribute('role') === 'combobox') {
		const currentValue = (() => {
			let value = clean(el.value || '');
			if (value) return value;
			const wrapper = el.closest('[data-automation-id="formField"], [data-automation-id*="formField"], .form-group, .field') || el.parentElement || el;
			const tokenSelectors = [
				'[data-automation-id*="selected"]',
				'[data-automation-id*="Selected"]',
				'[data-automation-id*="token"]',
				'[class*="token"]',
				'[class*="pill"]',
				'[class*="chip"]',
				'[class*="tag"]'
			];
			for (const selector of tokenSelectors) {
				for (const node of wrapper.querySelectorAll(selector)) {
					const text = visibleText(node);
					if (!text || /^(select one|choose one|required)$/i.test(text)) continue;
					return text;
				}
			}
			const wrapperText = visibleText(wrapper);
			if (wrapperText && wrapperText.length <= 120 && !/^(select one|choose one|required)$/i.test(wrapperText)) {
				return wrapperText;
			}
			return '';
		})();
		if (currentValue && !hasInvalidState(el)) {
			return JSON.stringify({
				type: 'custom_popup',
				options: [],
				currentValue: currentValue,
			});
		}
		const anchorRect = (el.closest('[data-automation-id="formField"], [data-automation-id*="formField"], .form-group, .field') || el.parentElement || el).getBoundingClientRect();
		const popups = qAll(
			'[role="listbox"], [role="menu"], [class*="popup"], [class*="dropdown-menu"], [class*="options-list"]'
		);
		const scoredPopups = [];
		for (const popup of popups) {
			if (!visible(popup)) continue;
			const rect = popup.getBoundingClientRect();
			const overlapX = Math.max(0, Math.min(rect.right, anchorRect.right) - Math.max(rect.left, anchorRect.left));
			const distanceY = Math.min(
				Math.abs(rect.top - anchorRect.bottom),
				Math.abs(anchorRect.top - rect.bottom)
			);
			let score = 0;
			if (listboxId && popup.id === listboxId) score += 100;
			if (overlapX > 0) score += 20;
			if (distanceY < 120) score += 10;
			score -= Math.floor(distanceY / 10);
			scoredPopups.push({ popup, score });
		}
		scoredPopups.sort((a, b) => b.score - a.score);
		for (const entry of scoredPopups) {
			const popup = entry.popup;
			const options = Array.from(
				popup.querySelectorAll('[role="option"], [role="gridcell"], [role="listitem"], li, [class*="option"], [data-value]')
			).filter(visible).map((o, i) => ({
				text: clean(o.textContent),
				value: o.getAttribute('data-value') || clean(o.textContent),
				index: i,
				selected: o.getAttribute('aria-selected') === 'true',
			})).filter((option) => option.text);
			if (options.length > 0) {
				return JSON.stringify({
					type: 'custom_popup',
					options: options,
					currentValue: currentValue,
				});
			}
		}
	}

	// Case 4: Generic — look for any visible listbox/menu across all roots
	const allListboxes = qAll(
		'[role="listbox"], [role="menu"], ul[class*="dropdown"], ul[class*="options"], div[class*="dropdown-menu"]'
	);
	for (const lb of allListboxes) {
		const rect = lb.getBoundingClientRect();
		if (rect.width > 0 && rect.height > 0) {
			const options = Array.from(
				lb.querySelectorAll('[role="option"], [role="menuitem"], [role="gridcell"], [role="listitem"], li')
			).filter(o => {
				const r = o.getBoundingClientRect();
				return r.width > 0 && r.height > 0;
			}).map((o, i) => ({
				text: o.textContent.trim(),
				value: o.getAttribute('data-value') || o.textContent.trim(),
				index: i,
				selected: o.getAttribute('aria-selected') === 'true' || o.classList.contains('selected'),
			}));
			if (options.length > 0) {
				return JSON.stringify({
					type: 'generic_listbox',
					options: options,
					currentValue: '',
				});
			}
		}
	}

	return JSON.stringify({
		type: 'unknown',
		options: [],
		currentValue: el.value || (el.textContent ? el.textContent.trim() : ''),
		error: 'Could not discover dropdown options. The dropdown may need to be clicked first.',
	});
}"""

# Click an option by its text content within any visible listbox/popup on the page
_CLICK_OPTION_JS = r"""(targetText) => {
	const lowerTarget = targetText.toLowerCase().trim();

	// Helper: use __ff.queryAll for cross-shadow-root traversal, fallback to document
	const qAll = (sel) => (window.__ff && window.__ff.queryAll)
		? window.__ff.queryAll(sel)
		: Array.from(document.querySelectorAll(sel));

	// Collect all visible option-like elements (across shadow roots)
	const selectors = [
		'[role="option"]', '[role="menuitem"]', '[role="gridcell"]', '[role="listitem"]',
		'li', '[class*="option"]', '[data-value]',
	];
	const candidates = [];
	for (const selector of selectors) {
		for (const el of qAll(selector)) {
			const rect = el.getBoundingClientRect();
			if (rect.width > 0 && rect.height > 0) {
				candidates.push(el);
			}
		}
	}

	// Deduplicate
	const seen = new Set();
	const unique = [];
	for (const el of candidates) {
		if (seen.has(el)) continue;
		seen.add(el);
		unique.push(el);
	}

	// Exact match first
	for (const el of unique) {
		const text = el.textContent.trim();
		if (text.toLowerCase() === lowerTarget) {
			el.click();
			return JSON.stringify({success: true, clicked: text});
		}
	}

	// Partial match (target contained in option or option contained in target)
	for (const el of unique) {
		const text = el.textContent.trim().toLowerCase();
		if (text.includes(lowerTarget) || lowerTarget.includes(text)) {
			el.click();
			return JSON.stringify({success: true, clicked: el.textContent.trim()});
		}
	}

	const availableTexts = unique.slice(0, 20).map(el => el.textContent.trim());
	return JSON.stringify({
		success: false,
		error: 'No matching option found for: ' + targetText,
		available: availableTexts,
	});
}"""

# Select a native <select> option by value or text — operates on `this` = the <select> element
_SELECT_NATIVE_ON_NODE_JS = r"""function(targetValue) {
	const el = this;
	if (el.tagName.toLowerCase() !== 'select') {
		return JSON.stringify({success: false, error: 'Not a native select element'});
	}

	const lowerTarget = targetValue.toLowerCase().trim();
	let matched = false;

	for (const opt of el.options) {
		const optText = opt.text.trim().toLowerCase();
		const optValue = opt.value.toLowerCase();
		if (optText === lowerTarget || optValue === lowerTarget) {
			el.value = opt.value;
			matched = true;
			break;
		}
	}

	// Fuzzy fallback
	if (!matched) {
		for (const opt of el.options) {
			const optText = opt.text.trim().toLowerCase();
			if (optText.includes(lowerTarget) || lowerTarget.includes(optText)) {
				el.value = opt.value;
				matched = true;
				break;
			}
		}
	}

	if (!matched) {
		const available = Array.from(el.options).map(o => o.text.trim()).slice(0, 20);
		return JSON.stringify({success: false, error: 'No match', available: available});
	}

	el.dispatchEvent(new Event('change', {bubbles: true}));
	el.dispatchEvent(new Event('input', {bubbles: true}));
	const selected = el.options[el.selectedIndex];
	return JSON.stringify({success: true, clicked: selected ? selected.text.trim() : targetValue});
}"""

# Verify current selection — operates on `this` = the trigger element
_VERIFY_SELECTION_ON_NODE_JS = r"""function() {
	const el = this;
	if (el.tagName.toLowerCase() === 'select') {
		const sel = el.options[el.selectedIndex];
		return JSON.stringify({value: sel ? sel.text.trim() : ''});
	}

	const looksLikeOpaqueValue = (text) => {
		const value = (text || '').replace(/\s+/g, ' ').trim();
		if (!value) return false;
		return /^(?:[0-9a-f]{16,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/i.test(value);
	};

	const isUnsetLike = (text) => {
		const value = (text || '').replace(/\s+/g, ' ').trim();
		if (!value) return true;
		if (/^(select one|choose one|please select)$/i.test(value)) return true;
		if (/\b(select one|choose one|please select)\b/i.test(value)) return true;
		return looksLikeOpaqueValue(value);
	};

	const visibleText = (node) => {
		if (!node) return '';
		const style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return '';
		const rect = node.getBoundingClientRect();
		if (!rect || rect.width === 0 || rect.height === 0) return '';
		return (node.textContent || '').replace(/\s+/g, ' ').trim();
	};

	let comboHost = null;
	let walk = el;
	for (let depth = 0; depth < 8 && walk; depth++) {
		if (walk.getAttribute && walk.getAttribute('role') === 'combobox') {
			comboHost = walk;
			break;
		}
		walk = walk.parentElement;
	}
	if (comboHost === el) {
		comboHost = null;
	}
	let isSelectLike = el.getAttribute('role') === 'combobox'
		|| el.getAttribute('data-uxi-widget-type') === 'selectinput'
		|| el.getAttribute('aria-haspopup') === 'listbox'
		|| el.getAttribute('aria-haspopup') === 'grid';
	if (!isSelectLike && comboHost) {
		isSelectLike = true;
	}

	let value = '';
	if (!isSelectLike && typeof el.value === 'string' && el.value.trim()) {
		value = el.value.trim();
	}
	if (!value && isSelectLike) {
		const fieldAnchor = comboHost || el;
		const ownText = visibleText(el);
		if (ownText && !isUnsetLike(ownText)) {
			value = ownText;
		}
		const wrapper =
			fieldAnchor.closest('.input-field-container')
			|| fieldAnchor.closest('.cx-select-container')
			|| fieldAnchor.closest('.input-row__control-container')
			|| fieldAnchor.closest('.input-row')
			|| fieldAnchor.closest(
				'[data-automation-id="formField"], [data-automation-id*="formField"], .form-group, .field'
			)
			|| fieldAnchor.parentElement
			|| fieldAnchor;
		const tokenSelectors = [
			'[data-automation-id*="selected"]',
			'[data-automation-id*="Selected"]',
			'[data-automation-id*="token"]',
			'[class*="token"]',
			'[class*="pill"]',
			'[class*="chip"]',
			'[class*="tag"]'
		];
		for (const selector of tokenSelectors) {
			const nodes = wrapper.querySelectorAll(selector);
			for (const node of nodes) {
				const text = visibleText(node);
				if (!text || isUnsetLike(text) || /^required$/i.test(text)) continue;
				value = text;
				break;
			}
			if (value) break;
		}
		if (!value) {
			const scanTargets = [];
			const addT = (n) => {
				if (n && !scanTargets.includes(n)) scanTargets.push(n);
			};
			addT(fieldAnchor);
			addT(wrapper);
			const jetSel = [
				'[class*="oj-text-field-middle"]',
				'[class*="TextFieldMiddle"]',
				'[class*="oj-text-field-container"]',
				'[class*="oj-text-field"]',
				'[role="textbox"][aria-readonly="true"]'
			];
			for (const st of scanTargets) {
				if (!st || !st.querySelectorAll) continue;
				for (const sel of jetSel) {
					for (const hit of st.querySelectorAll(sel)) {
						const tx = visibleText(hit);
						if (!tx || isUnsetLike(tx) || /^required$/i.test(tx) || tx.length > 120) continue;
						value = tx;
						break;
					}
					if (value) break;
				}
				if (value) break;
			}
		}
		if (!value) {
			const wrapperText = visibleText(wrapper);
			if (wrapperText && wrapperText.length <= 120 && !isUnsetLike(wrapperText) && !/^required$/i.test(wrapperText)) {
				value = wrapperText;
			}
		}
	}
	if (!value) {
		const ariaLabel = el.getAttribute('aria-label') || '';
		if (!isUnsetLike(ariaLabel)) {
			value = ariaLabel;
		}
	}
	if (!value && el.textContent) {
		value = el.textContent.trim();
	}
	if (isUnsetLike(value)) value = '';
	return JSON.stringify({value: value});
}"""

_READ_LABEL_CONTEXT_ON_NODE_JS = r"""function() {
	const el = this;
	const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
	const visibleText = (node) => {
		if (!node) return '';
		const style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return '';
		const rect = node.getBoundingClientRect();
		if (!rect || rect.width === 0 || rect.height === 0) return '';
		return clean(node.textContent || '');
	};
	const ownValue = visibleText(el);
	const prune = (text) => {
		let next = clean(text);
		if (!next) return '';
		if (ownValue && next.endsWith(ownValue)) {
			next = clean(next.slice(0, -ownValue.length));
		}
		next = next.replace(/\brequired\b$/i, '').trim();
		next = next.replace(/\*+\s*$/, '').trim();
		return next;
	};
	let label = "";

	const labelledBy = el.getAttribute("aria-labelledby");
	if (labelledBy) {
		const parts = labelledBy.split(/\s+/).filter(Boolean);
		const texts = parts.map((id) => {
			const target = document.getElementById(id);
			return target ? prune(target.textContent) : "";
		}).filter((text) => text && text !== ownValue);
		if (texts.length > 0) {
			label = texts[0];
		}
	}

	if (!label && el.id) {
		const escaped = window.CSS && window.CSS.escape ? window.CSS.escape(el.id) : el.id;
		const externalLabel = document.querySelector(`label[for="${escaped}"]`);
		if (externalLabel) {
			label = prune(externalLabel.textContent);
		}
	}

	if (!label) {
		const ancestorLabel = el.closest("label");
		if (ancestorLabel && ancestorLabel !== el) {
			label = prune(ancestorLabel.textContent);
		}
	}

	if (!label) {
		const wrapper = el.closest("fieldset,[role='group'],[data-automation-id='formField'],[data-automation-id*='formField']");
		if (wrapper) {
			const labelNodes = wrapper.querySelectorAll(
				"legend,[data-automation-id='fieldLabel'],[data-automation-id*='fieldLabel'],label,[class*='question']"
			);
			for (const node of labelNodes) {
				if (!node || node === el) continue;
				const text = prune(node.textContent || node.getAttribute("aria-label"));
				if (text && text !== ownValue) {
					label = text;
					break;
				}
			}
		}
	}

	if (!label) {
		const ariaLabel = prune(el.getAttribute("aria-label"));
		if (ariaLabel && ariaLabel !== ownValue) {
			label = ariaLabel;
		}
	}

	const wrapper = el.closest("[aria-invalid],[data-automation-id='formField'],[data-automation-id*='formField'],.form-group,.field,fieldset,[role='group'],[role='radiogroup']") || el.parentElement || el;
	const invalid = Boolean(
		(el.getAttribute && el.getAttribute("aria-invalid") === "true")
		|| (wrapper && wrapper.getAttribute && wrapper.getAttribute("aria-invalid") === "true")
		|| (wrapper && wrapper.querySelector && wrapper.querySelector('[aria-invalid="true"]'))
	);

	return JSON.stringify({
		label: label,
		tag: el.tagName ? el.tagName.toLowerCase() : "",
		widgetType: el.getAttribute("data-uxi-widget-type") || "",
		invalid: invalid,
	});
}"""


# ── CDP helper to call JS on a resolved node ────────────────────────


async def _call_function_on_node(
    browser_session: BrowserSession,
    node: EnhancedDOMTreeNode,
    function_declaration: str,
    arguments: list[dict[str, Any]] | None = None,
) -> Any:
    """Resolve a node via CDP and call a JS function on it.

    Uses DOM.resolveNode with backend_node_id, then Runtime.callFunctionOn.
    Returns the parsed JSON result or raw value.
    """
    session_id = node.session_id
    if not session_id:
        cdp_session = await browser_session.get_or_create_cdp_session()
        session_id = cdp_session.session_id

    # Resolve the backend node to a JS remote object
    resolve_result = await browser_session.cdp_client.send.DOM.resolveNode(
        params={"backendNodeId": node.backend_node_id},
        session_id=session_id,
    )
    object_id = resolve_result.get("object", {}).get("objectId")
    if not object_id:
        raise RuntimeError(f"Could not resolve node (backend_node_id={node.backend_node_id}) to JS object")

    call_params: dict[str, Any] = {
        "objectId": object_id,
        "functionDeclaration": function_declaration,
        "returnByValue": True,
    }
    if arguments:
        call_params["arguments"] = arguments

    call_result = await browser_session.cdp_client.send.Runtime.callFunctionOn(
        params=call_params,
        session_id=session_id,
    )
    raw_value = call_result.get("result", {}).get("value")
    if isinstance(raw_value, str):
        try:
            return json.loads(raw_value)
        except (json.JSONDecodeError, ValueError):
            return raw_value
    return raw_value


async def _try_click_combobox_toggle(browser_session: BrowserSession, node: EnhancedDOMTreeNode) -> bool:
    """Click react-select chevron / Toggle flyout when present (additive open path)."""
    try:
        raw = await _call_function_on_node(browser_session, node, CLICK_COMBOBOX_TOGGLE_ON_NODE_JS)
        return combobox_toggle_clicked(raw)
    except Exception:
        return False


# ── Fuzzy matching helper ────────────────────────────────────────────


def _fuzzy_match_option(
    target: str,
    options: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the best matching option for a target value using multi-pass fuzzy matching."""
    target_norm = normalize_name(target)
    if not target_norm:
        return None

    # Pass 1: Exact match
    for opt in options:
        if normalize_name(opt.get("text", "")) == target_norm:
            return opt
        if normalize_name(opt.get("value", "")) == target_norm:
            return opt

    # Pass 2: Contains (either direction)
    for opt in options:
        opt_norm = normalize_name(opt.get("text", ""))
        if opt_norm and (target_norm in opt_norm or opt_norm in target_norm):
            return opt

    # Pass 3: Word overlap (at least 1 shared meaningful word)
    target_words = set(target_norm.split()) - {"the", "a", "an", "of", "for", "in", "to"}
    if len(target_words) >= 1:
        best_overlap = 0
        best_opt: dict[str, Any] | None = None
        for opt in options:
            opt_words = set(normalize_name(opt.get("text", "")).split()) - {"the", "a", "an", "of", "for", "in", "to"}
            overlap = len(target_words & opt_words)
            if overlap > best_overlap:
                best_overlap = overlap
                best_opt = opt
        if best_opt is not None and best_overlap >= 1:
            return best_opt

    return None


# React-select / async combobox often reports placeholder rows like "No options" before the menu
# is focused or before options load. Treat those as "no real options yet" so we always click-open
# and poll (see Step 3b in domhand_select).
_PLACEHOLDER_EXACT = frozenset(
    {
        "no options",
        "no option",
        "no results",
        "no results found",
        "loading",
        "loading...",
        "search...",
        "select...",
        "choose...",
        "select one",
        "choose one",
    }
)


def _is_placeholder_option_text(primary: str) -> bool:
    low = re.sub(r"\s+", " ", primary).strip().lower()
    if not low:
        return True
    if low in _PLACEHOLDER_EXACT:
        return True
    if low.startswith("type to search"):
        return True
    return bool(re.match(r"^select\s*\.+\s*$", low) or re.match(r"^loading\.+$", low))


def _meaningful_dropdown_options(options: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Filter discovery noise so 'No options' / loading rows don't skip the open+wait loop."""
    if not options:
        return []
    out: list[dict[str, Any]] = []
    for opt in options:
        text = (opt.get("text") or "").strip()
        value = (opt.get("value") or "").strip()
        primary = text or value
        if _is_placeholder_option_text(primary):
            continue
        out.append(opt)
    return out


def _needs_dropdown_open_trigger(is_native: bool, dropdown_type: str, options: list[dict[str, Any]]) -> bool:
    """Whether to click the trigger and wait before matching (React-select, closed listbox, etc.)."""
    if is_native:
        return False
    if dropdown_type == "unknown":
        return True
    return not _meaningful_dropdown_options(options)


def _options_for_fuzzy_match(is_native: bool, options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if is_native:
        return options
    meaningful = _meaningful_dropdown_options(options)
    return meaningful if meaningful else options


async def _clear_dropdown_search(page: Any) -> None:
    """Clear the current typed query for an open searchable dropdown."""
    for shortcut in ("Meta+A", "Control+A"):
        with contextlib.suppress(Exception):
            await page.keyboard.press(shortcut)
    with contextlib.suppress(Exception):
        await page.keyboard.press("Backspace")
    await asyncio.sleep(0.15)


async def _focus_dropdown_filter_input(page: Any) -> None:
    """Move focus to the open combobox or listbox search field before typing.

    Without this, ``page.keyboard`` may target ``body`` and searchable menus
    never filter — the UI looks unchanged while options stay unmatchable.
    """
    js = """() => {
	const tryFocus = (el) => {
		if (!el) return false;
		try {
			el.focus();
			return true;
		} catch (e) {
			return false;
		}
	};
	const expanded = document.querySelector('[role="combobox"][aria-expanded="true"]');
	if (tryFocus(expanded)) return true;
	const lb = document.querySelector('[role="listbox"]');
	if (lb) {
		const inp = lb.querySelector(
			'input:not([type="hidden"]):not([disabled]), textarea, [role="searchbox"]'
		);
		if (tryFocus(inp)) return true;
	}
	const auto = document.querySelector(
		'input[aria-autocomplete="list"], input[aria-autocomplete="both"], input[aria-autocomplete="inline"]'
	);
	return tryFocus(auto);
}"""
    with contextlib.suppress(Exception):
        await page.evaluate(js)


async def _click_option_via_playwright(page: Any, matched_text: str) -> dict[str, Any]:
    """Click a visible menu row using Playwright (preferred over raw ``element.click()`` in evaluate).

    Oracle Fusion and similar UIs expose choices as ``gridcell`` / ``listitem``;
    synthetic React handlers often need the full pointer pipeline.
    """
    raw = (matched_text or "").strip()
    if not raw:
        return {"success": False, "error": "empty match text"}
    safe = raw[:200]
    try:
        pattern = re.compile(re.escape(safe), re.IGNORECASE)
    except re.error:
        return {"success": False, "error": "invalid match text for locator"}
    for role in ("option", "gridcell", "menuitem", "listitem"):
        try:
            loc = page.get_by_role(role, name=pattern)
            if await loc.count() == 0:
                continue
            first = loc.first
            await first.scroll_into_view_if_needed(timeout=2000)
            await first.click(timeout=4000)
            return {"success": True, "clicked": raw, "via": f"playwright:{role}"}
        except Exception:
            continue
    return {"success": False, "error": "playwright_locator_miss"}


async def _search_and_click_dropdown_option(page: Any, value: str) -> dict[str, Any]:
    """Type generic fallback search terms into an open dropdown and click a match."""
    for idx, term in enumerate(generate_dropdown_search_terms(value)):
        try:
            if idx > 0:
                await _clear_dropdown_search(page)
            await _focus_dropdown_filter_input(page)
            await page.keyboard.type(term, delay=45)
            await asyncio.sleep(0.3)
            result = await _click_option_via_page_js(page, value, "typed_search")
            if result.get("success"):
                return result
            await page.keyboard.press("Enter")
            await asyncio.sleep(1.1)
            result = await _click_option_via_page_js(page, value, "typed_search")
            if result.get("success"):
                return result
            result = await _click_option_via_page_js(page, term, "typed_search")
            if result.get("success"):
                return result
        except Exception as e:
            logger.debug(f'Typed dropdown search failed for "{term}": {e}')
    return {"success": False, "error": f"No matching option found for: {value}"}


async def _search_and_click_dropdown_path(page: Any, value: str) -> dict[str, Any]:
    """Handle hierarchical dropdown labels such as "Website > workday.com"."""
    segments = split_dropdown_value_hierarchy(value)
    if len(segments) <= 1:
        return await _search_and_click_dropdown_option(page, value)

    last_result: dict[str, Any] = {"success": False, "error": f"No matching option found for: {value}"}
    for segment in segments:
        result = await _click_option_via_page_js(page, segment, "hierarchy")
        if not result.get("success"):
            result = await _search_and_click_dropdown_option(page, segment)
        if not result.get("success"):
            return result
        last_result = result
        await asyncio.sleep(0.8)
    return last_result


def _looks_like_internal_widget_value(value: str | None) -> bool:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return False
    return bool(re.fullmatch(r"(?:[0-9a-f]{16,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", text, re.IGNORECASE))


def _is_effectively_unset_select_value(value: str | None) -> bool:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return True
    normalized = normalize_name(text)
    if "select one" in normalized or "choose one" in normalized or "please select" in normalized:
        return True
    return _looks_like_internal_widget_value(text)


def _selection_matches_value(current: str, expected: str) -> bool:
    """Return True when the widget visibly reflects the intended selection."""
    current_norm = normalize_name(current or "")
    expected_norm = normalize_name(expected or "")
    if _is_effectively_unset_select_value(current):
        return False
    # Binary Yes/No: substring match is unsafe — "no" appears inside "not", "none", "know", etc.
    if expected_norm in {"yes", "no"}:
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(expected_norm)}(?![a-z0-9])",
                current_norm,
            )
        )
    # Gender / short identity tokens: "male" must not match inside "female" (substring bug).
    gender_or_identity_tokens = frozenset(
        {"male", "female", "man", "woman", "non-binary", "nonbinary", "other"}
    )
    if expected_norm in gender_or_identity_tokens:
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(expected_norm)}(?![a-z0-9])",
                current_norm,
            )
        )
    if expected_norm and (expected_norm in current_norm or current_norm in expected_norm):
        return True
    segments = split_dropdown_value_hierarchy(expected)
    if not segments:
        return False
    final_segment = normalize_name(segments[-1])
    return bool(final_segment and final_segment in current_norm)


def _label_suggests_referral_or_source(field_label: str, param_field_label: str) -> bool:
    combined = normalize_name(f"{field_label} {param_field_label}")
    needles = (
        "how did you hear",
        "how did you",
        "referral",
        "source",
        "learned about",
        "where did you hear",
        "application source",
        "hear about us",
    )
    return any(n in combined for n in needles)


def _label_suggests_phone_country_field(field_label: str, param_field_label: str) -> bool:
    combined = normalize_name(f"{field_label} {param_field_label}")
    return any(
        x in combined
        for x in (
            "phone",
            "mobile",
            "country code",
            "calling code",
            "dial code",
            "telephone country",
        )
    )


def _options_look_like_phone_country_menu(options: list[dict[str, Any]]) -> bool:
    """True when visible options look like (+1) / country calling-code lists, not referral sources."""
    if len(options) < 1:
        return False
    texts = [str(o.get("text") or "").strip() for o in options[:20] if isinstance(o, dict)]
    texts = [t for t in texts if t]
    if not texts:
        return False
    phoneish = 0
    for t in texts:
        tl = t.lower().replace(" ", "")
        if re.search(r"\(\+\d", t) or re.search(r"\+\d{1,4}\b", t):
            phoneish += 1
        elif "unitedstates" in tl and "+1" in tl.replace(" ", ""):
            phoneish += 1
        elif re.search(r"\(\+1\)", t):
            phoneish += 1
    return phoneish >= max(1, (len(texts) + 1) // 2)


def _failover_prefix(widget_kind: str) -> str:
    """Return a machine-readable failover token for the widget type."""
    return FAIL_OVER_NATIVE_SELECT if widget_kind == "native_select" else FAIL_OVER_CUSTOM_WIDGET


def _native_select_failover_hint(index: int) -> str:
    """Return the exact native-select fallback instructions for the agent."""
    return (
        f"STOP — do NOT retry domhand_select for this field. This is a native <select>. Use dropdown_options(index={index}) to inspect "
        f"the exact option text/value, then call select_dropdown(index={index}, text=...) "
        "with the exact text/value string. Do NOT use click on this element."
    )


def _custom_widget_failover_hint() -> str:
    """Return the fallback instructions for custom dropdown widgets."""
    return (
        "Open the widget manually, type/search if supported, click the option directly, "
        "follow any secondary menu to the final leaf option, and only continue once the "
        "field visibly changes."
    )


def _build_failover_message(
    widget_kind: str,
    index: int,
    *,
    reason: str,
    available_texts: list[str] | None = None,
    current_value: str | None = None,
) -> str:
    """Build a structured, widget-specific failover message."""
    prefix = _failover_prefix(widget_kind)
    parts = [prefix, reason]
    if available_texts:
        parts.append(f"Options: {available_texts}.")
    if current_value:
        parts.append(f'Current value: "{current_value}".')
    if widget_kind == "native_select":
        parts.append(_native_select_failover_hint(index))
    else:
        parts.append("STOP — do NOT retry domhand_select for this field.")
        parts.append(_custom_widget_failover_hint())
    return " ".join(parts)


def _domhand_select_retry_field_key(field_label: str, widget_signature: str, index: int) -> str:
    return "|".join(
        [
            "select",
            normalize_name(widget_signature or "select"),
            normalize_name(field_label or str(index)),
        ]
    )


def _record_select_failure(host: str, field_key: str, desired_value: str) -> tuple[int, bool]:
    count = record_domhand_failure(host=host, field_key=field_key, desired_value=desired_value)
    capped = count >= DOMHAND_RETRY_CAP
    logger.info(
        "domhand.select.retry_state",
        extra={
            "field_key": field_key,
            "desired_value": desired_value,
            "host": host,
            "failure_count": count,
            "retry_capped": capped,
        },
    )
    return count, capped


def _select_failover_or_retry_cap_message(
    *,
    widget_kind: str,
    index: int,
    host: str,
    field_key: str,
    desired_value: str,
    reason: str,
    available_texts: list[str] | None = None,
    current_value: str | None = None,
) -> str:
    if is_domhand_retry_capped(host=host, field_key=field_key, desired_value=desired_value):
        count = get_domhand_failure_count(host=host, field_key=field_key, desired_value=desired_value)
        return _build_failover_message(
            widget_kind,
            index,
            reason=(
                f"domhand_retry_capped: DomHand retry cap reached after {count or DOMHAND_RETRY_CAP} failed attempts. "
                "Do not use domhand_select on this field again in this run."
            ),
            current_value=current_value,
        )
    return _build_failover_message(
        widget_kind,
        index,
        reason=reason,
        available_texts=available_texts,
        current_value=current_value,
    )


async def _read_current_selection(
    browser_session: BrowserSession,
    node: EnhancedDOMTreeNode,
) -> str:
    """Read the widget's currently visible value."""
    try:
        verify = await _call_function_on_node(browser_session, node, _VERIFY_SELECTION_ON_NODE_JS)
    except Exception:
        return ""
    return verify.get("value", "") if isinstance(verify, dict) else ""


async def _read_field_context(
    browser_session: BrowserSession,
    node: EnhancedDOMTreeNode,
) -> dict[str, Any]:
    """Read a best-effort label and widget context for the dropdown trigger."""
    try:
        context = await _call_function_on_node(browser_session, node, _READ_LABEL_CONTEXT_ON_NODE_JS)
    except Exception:
        return {"label": "", "tag": "", "widgetType": ""}
    return context if isinstance(context, dict) else {"label": "", "tag": "", "widgetType": ""}


async def _confirm_selection(
    page: Any,
    browser_session: BrowserSession,
    node: EnhancedDOMTreeNode,
    dropdown_type: str,
    expected: str,
    clicked_text: str,
) -> tuple[str, str]:
    """Retry searchable/multi-layer dropdowns until the final visible value is confirmed."""
    current = ""
    last_clicked = clicked_text
    for attempt in range(3):
        await asyncio.sleep(0.7 if attempt == 0 else 0.9)
        current = await _read_current_selection(browser_session, node)
        if _selection_matches_value(current, expected):
            return current, last_clicked
        if dropdown_type == "native_select" or attempt == 2:
            break
        try:
            event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
            await event
            await event.event_result(raise_if_any=True, raise_if_none=False)
            await asyncio.sleep(0.45)
        except Exception:
            pass
        retry = await _search_and_click_dropdown_path(page, expected)
        if retry.get("success"):
            last_clicked = retry.get("clicked", last_clicked)
    current = await _read_current_selection(browser_session, node)
    if _profile_debug_enabled():
        logger.info(
            "domhand.select_confirmed",
            extra={
                "expected_value": expected,
                "clicked_text": clicked_text,
                "current_value": current,
                "dropdown_type": dropdown_type,
            },
        )
    return current, last_clicked


# ── Core action function ─────────────────────────────────────────────


async def domhand_select(params: DomHandSelectParams, browser_session: BrowserSession) -> ActionResult:
    """Select a dropdown option using platform-aware discovery.

    1. Click the dropdown trigger to open it
    2. Discover available options (native, ARIA, or custom)
    3. Fuzzy-match the target value
    4. Click the matching option
    5. Verify the selection
    """
    page = await browser_session.get_current_page()
    if not page:
        return ActionResult(error="No active page found in browser session")

    # ── Step 0: Inject __ff shadow-DOM helpers if not present ─
    try:
        from ghosthands.dom.shadow_helpers import ensure_helpers

        await ensure_helpers(page)
    except Exception as e:
        logger.debug(f"Could not inject __ff helpers: {e}")

    # ── Step 1: Get the trigger element ───────────────────────
    try:
        node = await browser_session.get_element_by_index(params.index)
        if node is None:
            return ActionResult(error=f"Element index {params.index} not available. Page may have changed.")
    except Exception as e:
        return ActionResult(error=f"Failed to find element at index {params.index}: {e}")

    # ── Step 2: Try browser-use event bus first (handles native + ARIA) ──
    # For native <select> elements, try the built-in SelectDropdownOptionEvent
    is_native_select = node.tag_name == "select"
    widget_kind = "native_select" if is_native_select else "custom_widget"

    if is_native_select:
        try:
            return await _select_via_event_bus(browser_session, node, params)
        except Exception as e:
            logger.debug(f"Event bus select failed for native <select>, falling back to CDP: {e}")

    # ── Step 3: Discover options via CDP (resolve node, run JS on it) ──
    try:
        discovery = await _call_function_on_node(browser_session, node, _DISCOVER_OPTIONS_ON_NODE_JS)
    except Exception as e:
        discovery = {"type": "unknown", "options": [], "error": str(e)}

    dropdown_type = discovery.get("type", "unknown") if isinstance(discovery, dict) else "unknown"
    options = discovery.get("options", []) if isinstance(discovery, dict) else []
    field_context = await _read_field_context(browser_session, node)
    field_label = str(field_context.get("label") or "").strip()
    widget_signature = (
        str(field_context.get("widgetType") or "").strip()
        or dropdown_type
        or ("native_select" if is_native_select else "custom_widget")
    )
    field_key = _domhand_select_retry_field_key(field_label, widget_signature, params.index)
    field_invalid = bool(field_context.get("invalid"))
    used_action_chain: list[str] = []
    matched_text = params.value
    current_before = await _read_current_selection(browser_session, node)
    page_url = ""
    try:
        page_url = await browser_session.get_current_page_url()
    except Exception:
        page_url = ""
    page_host = detect_host_from_url(page_url)
    logger.info(
        "domhand.select.start "
        f"index={params.index} "
        f"requested_value={params.value!r} "
        f"field_label={field_label!r} "
        f"dropdown_type={dropdown_type!r} "
        f"widget_signature={widget_signature!r} "
        f"option_count={len(options)} "
        f"field_invalid={field_invalid} "
        f"current_value_before={current_before!r}"
    )
    await publish_browser_session_trace(
        browser_session,
        "tool_attempt",
        {
            "tool": "domhand_select",
            "index": params.index,
            "field_id": params.field_id or "",
            "field_label": params.field_label or field_label or "",
            "target_section": params.target_section or "",
            "desired_value": params.value,
            "field_key": field_key,
            "current_value_before": current_before,
        },
    )
    if _selection_matches_value(current_before, params.value) and not field_invalid:
        clear_domhand_failure(host=page_host, field_key=field_key, desired_value=params.value)
        logger.info(
            "domhand.select.already_selected "
            f"index={params.index} "
            f"requested_value={params.value!r} "
            f"field_label={field_label!r} "
            f"current_value_before={current_before!r}"
        )
        return ActionResult(
            extracted_content=(
                f'Dropdown "{field_label or params.index}" already showed "{current_before}". '
                "Immediately call domhand_assess_state for this blocker."
            ),
            include_extracted_content_only_once=False,
            metadata={
                "tool": "domhand_select",
                "field_id": params.field_id,
                "field_key": field_key,
                "strategy": "already_selected",
                "state_change": "unchanged",
                "retry_capped": False,
                "recommended_next_action": "call domhand_assess_state",
            },
        )
    if _selection_matches_value(current_before, params.value) and field_invalid:
        logger.info(
            "domhand.select.already_selected_invalid "
            f"index={params.index} "
            f"requested_value={params.value!r} "
            f"field_label={field_label!r} "
            f"current_value_before={current_before!r}"
        )

    # Closed Workday-style combobox: discovery returns custom_popup + [] options + currentValue from
    # the visible token, while _read_current_selection can still be empty. Skip open-loop churn.
    discovery_value = str(discovery.get("currentValue") or "").strip() if isinstance(discovery, dict) else ""
    if (
        not field_invalid
        and discovery_value
        and dropdown_type == "custom_popup"
        and not _meaningful_dropdown_options(options)
        and _selection_matches_value(discovery_value, params.value)
    ):
        clear_domhand_failure(host=page_host, field_key=field_key, desired_value=params.value)
        logger.info(
            "domhand.select.already_selected_discovery "
            f"index={params.index} "
            f"requested_value={params.value!r} "
            f"discovery_value={discovery_value!r} "
            f"current_value_before={current_before!r}"
        )
        return ActionResult(
            extracted_content=(
                f'Dropdown "{field_label or params.index}" already showed "{discovery_value}" '
                "(from discovery). Immediately call domhand_assess_state for this blocker."
            ),
            include_extracted_content_only_once=False,
            metadata={
                "tool": "domhand_select",
                "field_id": params.field_id,
                "field_key": field_key,
                "strategy": "already_selected",
                "state_change": "unchanged",
                "retry_capped": False,
                "recommended_next_action": "call domhand_assess_state",
            },
        )

    # ── Step 3b: Click to open, then poll until real options appear ──
    # React-select / combobox: options are often absent or only "No options" until the menu opens.
    if _needs_dropdown_open_trigger(is_native_select, dropdown_type, options):
        for click_attempt in range(3):
            try:
                toggled = False
                if not is_native_select:
                    toggled = await _try_click_combobox_toggle(browser_session, node)
                    if toggled:
                        logger.debug(
                            "domhand.select.open_via_toggle",
                            extra={"index": params.index, "attempt": click_attempt + 1},
                        )
                if not toggled:
                    event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
                    await event
                    await event.event_result(raise_if_any=True, raise_if_none=False)
                # Poll: listbox paint + async options (React-select); cap ~1.5s per click wave.
                for _tick in range(10):
                    await asyncio.sleep(0.15)
                    try:
                        discovery = await _call_function_on_node(browser_session, node, _DISCOVER_OPTIONS_ON_NODE_JS)
                        dropdown_type = discovery.get("type", "unknown") if isinstance(discovery, dict) else "unknown"
                        options = discovery.get("options", []) if isinstance(discovery, dict) else []
                    except Exception:
                        pass
                    if is_native_select and options:
                        break
                    if _meaningful_dropdown_options(options):
                        break
                if is_native_select and options:
                    break
                if _meaningful_dropdown_options(options):
                    break
            except Exception as e:
                logger.warning(f"Failed to click dropdown trigger (attempt {click_attempt + 1}): {e}")
                break
        logger.info(
            "domhand.select.after_open "
            f"index={params.index} "
            f"dropdown_type={dropdown_type!r} "
            f"raw_option_count={len(options)} "
            f"meaningful_option_count={len(_meaningful_dropdown_options(options))}"
        )

    # ── Step 3c: If still no options, try GetDropdownOptionsEvent ──
    if not options:
        try:
            event = browser_session.event_bus.dispatch(GetDropdownOptionsEvent(node=node))
            dropdown_data = await event.event_result(timeout=3.0, raise_if_none=False, raise_if_any=False)
            if dropdown_data and isinstance(dropdown_data, dict):
                raw_options = dropdown_data.get("options", [])
                if raw_options:
                    options = raw_options
                    dropdown_type = dropdown_data.get("type", "event_bus")
        except Exception as e:
            logger.debug(f"GetDropdownOptionsEvent failed: {e}")

    if is_domhand_retry_capped(host=page_host, field_key=field_key, desired_value=params.value):
        logger.info(
            "domhand.select.retry_capped",
            extra={
                "index": params.index,
                "field_label": field_label,
                "field_key": field_key,
                "desired_value": params.value,
                "host": page_host,
                "failure_count": get_domhand_failure_count(
                    host=page_host,
                    field_key=field_key,
                    desired_value=params.value,
                ),
            },
        )
        return ActionResult(
            error=_select_failover_or_retry_cap_message(
                widget_kind=widget_kind,
                index=params.index,
                host=page_host,
                field_key=field_key,
                desired_value=params.value,
                reason=f"domhand_select cannot handle element {params.index}.",
                current_value=current_before,
            ),
            metadata={
                "tool": "domhand_select",
                "field_id": params.field_id,
                "field_key": field_key,
                "strategy": "domhand_select",
                "state_change": "no_state_change",
                "retry_capped": True,
                "recommended_next_action": "change strategy for this blocker and reassess immediately",
            },
        )

    if not options:
        current = current_before
        post_invalid = field_invalid
        logger.warning(
            "domhand.select.no_options "
            f"index={params.index} "
            f"requested_value={params.value!r} "
            f"field_label={field_label!r} "
            f"dropdown_type={dropdown_type!r} "
            f"widget_signature={widget_signature!r} "
            f"field_invalid={field_invalid} "
            f"current_value_before={current_before!r}"
        )
        _record_select_failure(page_host, field_key, params.value)
        update_blocker_attempt_state(
            browser_session,
            field_key=field_key,
            field_id=params.field_id or "",
            strategy="domhand_select",
            desired_value=params.value,
            observed_value=current,
            visible_error=str(post_invalid),
            retry_capped=is_domhand_retry_capped(host=page_host, field_key=field_key, desired_value=params.value),
            success=False,
            state_change="no_state_change",
            recommended_next_action="change strategy for this blocker and reassess immediately",
        )
        await publish_browser_session_trace(
            browser_session,
            "tool_result",
            {
                "tool": "domhand_select",
                "index": params.index,
                "field_id": params.field_id or "",
                "field_label": params.field_label or field_label or "",
                "field_key": field_key,
                "desired_value": params.value,
                "observed_value": current,
                "visible_error": str(post_invalid),
                "strategy": "domhand_select",
                "retry_capped": is_domhand_retry_capped(
                    host=page_host, field_key=field_key, desired_value=params.value
                ),
                "state_change": "no_state_change",
                "recommended_next_action": "change strategy for this blocker and reassess immediately",
            },
        )
        return ActionResult(
            error=_select_failover_or_retry_cap_message(
                widget_kind=widget_kind,
                index=params.index,
                host=page_host,
                field_key=field_key,
                desired_value=params.value,
                reason=f"domhand_select cannot handle element {params.index}.",
            ),
        )

    try:
        from ghosthands.actions.domhand_fill import _get_profile_data
        from ghosthands.runtime_learning import detect_platform_from_url, get_interaction_recipe

        profile_data = _get_profile_data()
        recipe = get_interaction_recipe(
            platform=detect_platform_from_url(page_url),
            host=detect_host_from_url(page_url),
            label=field_label,
            widget_signature=widget_signature,
            profile_data=profile_data,
        )
    except Exception:
        recipe = None
        profile_data = None

    if recipe is not None and not is_native_select:
        if _profile_debug_enabled():
            logger.info(
                "domhand.select_recipe_loaded",
                extra={
                    "field_label": field_label,
                    "widget_signature": widget_signature,
                    "preferred_action_chain": recipe.preferred_action_chain,
                    "page_url": page_url,
                },
            )
        if "hierarchy_search" in recipe.preferred_action_chain:
            result = await _search_and_click_dropdown_path(page, params.value)
            if result.get("success"):
                matched_text = result.get("clicked", params.value)
                used_action_chain = ["hierarchy_search"]
        elif "typed_search" in recipe.preferred_action_chain:
            result = await _search_and_click_dropdown_option(page, params.value)
            if result.get("success"):
                matched_text = result.get("clicked", params.value)
                used_action_chain = ["typed_search"]
        elif "page_js_click" in recipe.preferred_action_chain:
            result = await _click_option_via_page_js(page, params.value, dropdown_type)
            if result.get("success"):
                matched_text = result.get("clicked", params.value)
                used_action_chain = ["page_js_click"]
        else:
            result = None
    else:
        result = None

    # ── Step 4: Match the target value ────────────────────────
    match_options = _options_for_fuzzy_match(is_native_select, options)
    if (
        _options_look_like_phone_country_menu(match_options)
        and _label_suggests_referral_or_source(field_label, params.field_label or "")
        and not _label_suggests_phone_country_field(field_label, params.field_label or "")
    ):
        logger.warning(
            "domhand.select.phone_country_menu_mismatch",
            extra={
                "index": params.index,
                "field_label": field_label,
                "param_field_label": params.field_label,
                "requested_value": params.value,
                "sample_options": [str(o.get("text") or "") for o in match_options[:5]],
            },
        )
        return ActionResult(
            error=(
                "WRONG_DROPDOWN: Visible options look like phone country codes (+1 / country list), "
                "not referral/source choices. You likely targeted the wrong control — find the real "
                '"How did you hear about us?" (or similar) field, or open the correct dropdown.'
            ),
            metadata={
                "tool": "domhand_select",
                "field_id": params.field_id,
                "field_key": field_key,
                "strategy": "wrong_dropdown_phone_country",
                "state_change": "no_state_change",
                "retry_capped": False,
                "recommended_next_action": "locate correct referral/source control; do not retry same index",
            },
        )

    matched = _fuzzy_match_option(params.value, match_options)

    if result is None and not matched:
        if dropdown_type != "native_select":
            result = await _search_and_click_dropdown_path(page, params.value)
            if result.get("success"):
                matched_text = result.get("clicked", params.value)
                used_action_chain = (
                    ["hierarchy_search"] if len(split_dropdown_value_hierarchy(params.value)) > 1 else ["typed_search"]
                )
        if result is None or not result.get("success"):
            available_texts = [opt.get("text", "") for opt in match_options[:20]]
            logger.warning(
                "domhand.select.no_match "
                f"index={params.index} "
                f"requested_value={params.value!r} "
                f"field_label={field_label!r} "
                f"dropdown_type={dropdown_type!r} "
                f"widget_signature={widget_signature!r} "
                f"available_texts={available_texts} "
                f"field_invalid={field_invalid} "
                f"current_value_before={current_before!r}"
            )
            _record_select_failure(page_host, field_key, params.value)
            return ActionResult(
                error=_select_failover_or_retry_cap_message(
                    widget_kind=widget_kind,
                    index=params.index,
                    host=page_host,
                    field_key=field_key,
                    desired_value=params.value,
                    reason=f'No match for "{params.value}" in element {params.index}.',
                    available_texts=available_texts,
                ),
            )

    # ── Step 5: Click the matched option ──────────────────────
    if result is None:
        matched_text = matched.get("text", params.value)
        try:
            if dropdown_type == "native_select":
                # For native selects, use JS to set value directly on the resolved node
                result = await _call_function_on_node(
                    browser_session,
                    node,
                    _SELECT_NATIVE_ON_NODE_JS,
                    arguments=[{"value": matched_text}],
                )
            else:
                # For custom dropdowns, try SelectDropdownOptionEvent first
                try:
                    if field_invalid:
                        result = await _click_option_via_page_js(page, matched_text, dropdown_type)
                        if result.get("success"):
                            used_action_chain = ["page_js_click"]
                    else:
                        event = browser_session.event_bus.dispatch(
                            SelectDropdownOptionEvent(node=node, text=matched_text)
                        )
                        selection_data = await event.event_result(timeout=3.0, raise_if_none=False, raise_if_any=False)
                        if selection_data and isinstance(selection_data, dict) and selection_data.get("success"):
                            result = {"success": True, "clicked": selection_data.get("selected_text", matched_text)}
                            used_action_chain = ["event_bus_select"]
                        else:
                            # Fallback: click the option via page-level JS
                            result = await _click_option_via_page_js(page, matched_text, dropdown_type)
                            if result.get("success"):
                                used_action_chain = ["page_js_click"]
                except Exception:
                    result = await _click_option_via_page_js(page, matched_text, dropdown_type)
                    if result.get("success"):
                        used_action_chain = ["page_js_click"]
        except Exception as e:
            return ActionResult(error=f'Failed to select option "{matched_text}": {e}')

    if isinstance(result, dict) and not result.get("success"):
        _record_select_failure(page_host, field_key, params.value)
        available = result.get("available", [])
        return ActionResult(
            error=_select_failover_or_retry_cap_message(
                widget_kind=widget_kind,
                index=params.index,
                host=page_host,
                field_key=field_key,
                desired_value=params.value,
                reason=f'Failed to select "{matched_text}": {result.get("error", "unknown")}.',
                available_texts=available,
                current_value=current_before,
            ),
        )

    clicked_text = result.get("clicked", matched_text) if isinstance(result, dict) else matched_text

    # ── Step 6: Verify the selection ──────────────────────────
    current, clicked_text = await _confirm_selection(
        page,
        browser_session,
        node,
        dropdown_type,
        params.value,
        clicked_text,
    )
    post_context = await _read_field_context(browser_session, node)
    post_invalid = bool(post_context.get("invalid"))
    logger.info(
        "domhand.select.observed "
        f"index={params.index} "
        f"requested_value={params.value!r} "
        f"clicked_text={clicked_text!r} "
        f"current_value={current!r} "
        f"field_label={field_label!r} "
        f"dropdown_type={dropdown_type!r} "
        f"widget_signature={widget_signature!r} "
        f"field_invalid_after={post_invalid} "
        f"used_action_chain={used_action_chain}"
    )

    if not _selection_matches_value(current, params.value) or post_invalid:
        logger.warning(
            "domhand.select.failed "
            f"index={params.index} "
            f"requested_value={params.value!r} "
            f"clicked_text={clicked_text!r} "
            f"current_value={current!r} "
            f"field_label={field_label!r} "
            f"dropdown_type={dropdown_type!r} "
            f"widget_signature={widget_signature!r} "
            f"field_invalid_after={post_invalid} "
            f"used_action_chain={used_action_chain}"
        )
        failure_reason = (
            f'Selection for "{params.value}" still left the field invalid on element {params.index}.'
            if post_invalid
            else f'Selection for "{params.value}" was not confirmed on element {params.index}.'
        )
        _record_select_failure(page_host, field_key, params.value)
        return ActionResult(
            error=_select_failover_or_retry_cap_message(
                widget_kind=widget_kind,
                index=params.index,
                host=page_host,
                field_key=field_key,
                desired_value=params.value,
                reason=failure_reason,
                current_value=current,
            ),
            metadata={
                "tool": "domhand_select",
                "field_id": params.field_id,
                "field_key": field_key,
                "strategy": "domhand_select",
                "state_change": "no_state_change",
                "retry_capped": is_domhand_retry_capped(
                    host=page_host, field_key=field_key, desired_value=params.value
                ),
                "recommended_next_action": "change strategy for this blocker and reassess immediately",
            },
        )

    clear_domhand_failure(host=page_host, field_key=field_key, desired_value=params.value)
    page_context_key = await _get_page_context_key(page)
    settled_field = FormField(
        field_id=params.field_id or f"domhand-select-{params.index}",
        name=(params.field_label or field_label or f"dropdown[{params.index}]"),
        field_type="select",
        section=params.target_section or "",
        is_native=is_native_select,
    )
    await _record_expected_value_if_settled(
        page=page,
        host=page_host,
        page_context_key=page_context_key,
        field=settled_field,
        field_key=field_key,
        expected_value=params.value,
        source="derived_profile",
        log_context="domhand.select",
    )
    update_blocker_attempt_state(
        browser_session,
        field_key=field_key,
        field_id=params.field_id or "",
        strategy="domhand_select",
        desired_value=params.value,
        observed_value=current,
        visible_error="",
        retry_capped=False,
        success=True,
        state_change="changed",
        recommended_next_action="call domhand_assess_state",
    )
    memory = f'Selected "{clicked_text}" for dropdown at index {params.index}. Immediately call domhand_assess_state for this blocker.'
    if current and normalize_name(current) != normalize_name(clicked_text):
        memory += f' (showing: "{current}")'

    logger.info(f"DomHand select: {memory}")
    await publish_browser_session_trace(
        browser_session,
        "tool_result",
        {
            "tool": "domhand_select",
            "index": params.index,
            "field_id": params.field_id or "",
            "field_label": params.field_label or field_label or "",
            "field_key": field_key,
            "desired_value": params.value,
            "observed_value": current,
            "visible_error": "",
            "strategy": "domhand_select",
            "retry_capped": False,
            "state_change": "changed",
            "recommended_next_action": "call domhand_assess_state",
        },
    )
    if _profile_debug_enabled() and used_action_chain:
        logger.info(
            "domhand.select_recipe_applied",
            extra={
                "field_label": field_label,
                "widget_signature": widget_signature,
                "used_action_chain": used_action_chain,
                "selected_value": current or clicked_text,
                "page_url": page_url,
            },
        )

    if not is_native_select and field_label and page_url and used_action_chain:
        try:
            from ghosthands.runtime_learning import (
                detect_platform_from_url,
                record_interaction_recipe,
            )

            record_interaction_recipe(
                platform=detect_platform_from_url(page_url),
                host=detect_host_from_url(page_url),
                label=field_label,
                widget_signature=widget_signature,
                preferred_action_chain=used_action_chain,
                source="visual_fallback",
                profile_data=profile_data,
            )
            if _profile_debug_enabled():
                logger.info(
                    "domhand.select_recipe_recorded",
                    extra={
                        "field_label": field_label,
                        "widget_signature": widget_signature,
                        "used_action_chain": used_action_chain,
                        "page_url": page_url,
                    },
                )
        except Exception:
            pass

    return ActionResult(
        extracted_content=memory,
        include_extracted_content_only_once=False,
        metadata={
            "tool": "domhand_select",
            "field_id": params.field_id,
            "field_key": field_key,
            "strategy": "domhand_select",
            "state_change": "changed",
            "retry_capped": False,
            "recommended_next_action": "call domhand_assess_state",
        },
    )


# ── Helper: select via event bus for native selects ──────────────────


async def _select_via_event_bus(
    browser_session: BrowserSession,
    node: EnhancedDOMTreeNode,
    params: DomHandSelectParams,
) -> ActionResult:
    """Use browser-use's GetDropdownOptionsEvent + SelectDropdownOptionEvent.

    This path is preferred for native <select> elements since browser-use
    handles all the change/input event dispatching correctly.
    """
    # Get options
    event = browser_session.event_bus.dispatch(GetDropdownOptionsEvent(node=node))
    dropdown_data = await event.event_result(timeout=3.0, raise_if_none=True, raise_if_any=True)

    if not dropdown_data or not isinstance(dropdown_data, dict):
        raise ValueError("Failed to get dropdown options from event bus")

    raw_options = dropdown_data.get("options", [])
    if not raw_options:
        raise ValueError("No options returned from event bus")

    # Convert to our option format if needed
    options = raw_options if isinstance(raw_options, list) else []
    matched = _fuzzy_match_option(params.value, options)

    if not matched:
        available_texts = [opt.get("text", "") for opt in options[:20]]
        return ActionResult(
            error=_build_failover_message(
                "native_select",
                params.index,
                reason=f'No match for "{params.value}" in element {params.index}.',
                available_texts=available_texts,
            ),
        )

    matched_text = matched.get("text", params.value)

    # Select via event bus
    event = browser_session.event_bus.dispatch(SelectDropdownOptionEvent(node=node, text=matched_text))
    selection_data = await event.event_result(timeout=3.0, raise_if_none=False, raise_if_any=True)

    clicked_text = matched_text
    if selection_data and isinstance(selection_data, dict):
        clicked_text = selection_data.get("selected_text", matched_text)

    memory = f'Selected "{clicked_text}" for dropdown at index {params.index}'
    logger.info(f"DomHand select: {memory}")

    return ActionResult(
        extracted_content=memory,
        include_extracted_content_only_once=False,
    )


# ── Helper: click option via page-level JS ───────────────────────────


async def _click_option_via_page_js(page: Any, matched_text: str, dropdown_type: str) -> dict[str, Any]:
    """Click a dropdown option: Playwright locators first, then global JS search.

    Playwright issues full pointer/keyboard focus semantics; plain DOM
    ``click()`` inside ``evaluate`` often misses React/Vue synthetic handlers.
    """
    pw = await _click_option_via_playwright(page, matched_text)
    if pw.get("success"):
        return pw
    raw_result = await page.evaluate(_CLICK_OPTION_JS, matched_text)
    if isinstance(raw_result, str):
        return json.loads(raw_result)
    return raw_result if isinstance(raw_result, dict) else {"success": False, "error": "Unexpected result type"}
