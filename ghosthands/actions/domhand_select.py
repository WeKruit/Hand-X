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

All DOM element lookups use browser-use's selector map (get_element_by_index)
and CDP backend_node_id resolution — never data-highlight-index attributes.
"""

import asyncio
import json
import logging
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import (
	ClickElementEvent,
	GetDropdownOptionsEvent,
	SelectDropdownOptionEvent,
)
from browser_use.dom.views import EnhancedDOMTreeNode

from ghosthands.actions.views import DomHandSelectParams, normalize_name
from ghosthands.dom.shadow_helpers import QALL_JS_SNIPPET

logger = logging.getLogger(__name__)


# ── CDP-based JavaScript helpers ─────────────────────────────────────
# These JS functions operate on a node passed directly via Runtime.callFunctionOn
# (i.e., `this` is the DOM element). They never use data-highlight-index.

_DISCOVER_OPTIONS_ON_NODE_JS = "function(){const el=this;const tag=el.tagName.toLowerCase();" + QALL_JS_SNIPPET + r"""
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
		const listbox = document.getElementById(listboxId);
		if (listbox) {
			const options = Array.from(
				listbox.querySelectorAll('[role="option"], li, [class*="option"], [data-value]')
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
		const popups = qAll(
			'[role="listbox"], [role="menu"], [class*="popup"], [class*="dropdown-menu"], [class*="options-list"]'
		);
		for (const popup of popups) {
			const rect = popup.getBoundingClientRect();
			if (rect.width > 0 && rect.height > 0) {
				const options = Array.from(
					popup.querySelectorAll('[role="option"], li, [class*="option"], [data-value]')
				).map((o, i) => ({
					text: o.textContent.trim(),
					value: o.getAttribute('data-value') || o.textContent.trim(),
					index: i,
					selected: o.getAttribute('aria-selected') === 'true',
				}));
				if (options.length > 0) {
					return JSON.stringify({
						type: 'custom_popup',
						options: options,
						currentValue: el.value || (el.textContent ? el.textContent.trim() : ''),
					});
				}
			}
		}
	}

	// Case 4: Generic — look for any visible listbox/menu on the page
	const allListboxes = qAll(
		'[role="listbox"], [role="menu"], ul[class*="dropdown"], ul[class*="options"], div[class*="dropdown-menu"]'
	);
	for (const lb of allListboxes) {
		const rect = lb.getBoundingClientRect();
		if (rect.width > 0 && rect.height > 0) {
			const options = Array.from(
				lb.querySelectorAll('[role="option"], [role="menuitem"], li')
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
_CLICK_OPTION_JS = "function(targetText){const lowerTarget=targetText.toLowerCase().trim();" + QALL_JS_SNIPPET + r"""
	// Collect all visible option-like elements (across shadow roots)
	const selectors = [
		'[role="option"]', '[role="menuitem"]',
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

	// For ARIA/custom: check aria-label, value, or text content
	const value = el.value || el.getAttribute('aria-label') || (el.textContent ? el.textContent.trim() : '');
	return JSON.stringify({value: value});
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
		params={'backendNodeId': node.backend_node_id},
		session_id=session_id,
	)
	object_id = resolve_result.get('object', {}).get('objectId')
	if not object_id:
		raise RuntimeError(f'Could not resolve node (backend_node_id={node.backend_node_id}) to JS object')

	call_params: dict[str, Any] = {
		'objectId': object_id,
		'functionDeclaration': function_declaration,
		'returnByValue': True,
	}
	if arguments:
		call_params['arguments'] = arguments

	call_result = await browser_session.cdp_client.send.Runtime.callFunctionOn(
		params=call_params,
		session_id=session_id,
	)
	raw_value = call_result.get('result', {}).get('value')
	if isinstance(raw_value, str):
		try:
			return json.loads(raw_value)
		except (json.JSONDecodeError, ValueError):
			return raw_value
	return raw_value


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
		if normalize_name(opt.get('text', '')) == target_norm:
			return opt
		if normalize_name(opt.get('value', '')) == target_norm:
			return opt

	# Pass 2: Contains (either direction)
	for opt in options:
		opt_norm = normalize_name(opt.get('text', ''))
		if opt_norm and (target_norm in opt_norm or opt_norm in target_norm):
			return opt

	# Pass 3: Word overlap (at least 1 shared meaningful word)
	target_words = set(target_norm.split()) - {'the', 'a', 'an', 'of', 'for', 'in', 'to'}
	if len(target_words) >= 1:
		best_overlap = 0
		best_opt: dict[str, Any] | None = None
		for opt in options:
			opt_words = set(normalize_name(opt.get('text', '')).split()) - {'the', 'a', 'an', 'of', 'for', 'in', 'to'}
			overlap = len(target_words & opt_words)
			if overlap > best_overlap:
				best_overlap = overlap
				best_opt = opt
		if best_opt is not None and best_overlap >= 1:
			return best_opt

	return None


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
		return ActionResult(error='No active page found in browser session')

	# ── Step 1: Get the trigger element ───────────────────────
	try:
		node = await browser_session.get_element_by_index(params.index)
		if node is None:
			return ActionResult(error=f'Element index {params.index} not available. Page may have changed.')
	except Exception as e:
		return ActionResult(error=f'Failed to find element at index {params.index}: {e}')

	# ── Step 2: Try browser-use event bus first (handles native + ARIA) ──
	# For native <select> elements, try the built-in SelectDropdownOptionEvent
	is_native_select = node.tag_name == 'select'

	if is_native_select:
		try:
			return await _select_via_event_bus(browser_session, node, params)
		except Exception as e:
			logger.debug(f'Event bus select failed for native <select>, falling back to CDP: {e}')

	# ── Step 3: Discover options via CDP (resolve node, run JS on it) ──
	try:
		discovery = await _call_function_on_node(
			browser_session, node, _DISCOVER_OPTIONS_ON_NODE_JS
		)
	except Exception as e:
		discovery = {'type': 'unknown', 'options': [], 'error': str(e)}

	dropdown_type = discovery.get('type', 'unknown') if isinstance(discovery, dict) else 'unknown'
	options = discovery.get('options', []) if isinstance(discovery, dict) else []

	# ── Step 3b: If no options found, click to open and re-discover ──
	if not options or dropdown_type == 'unknown':
		try:
			event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
			await event
			await event.event_result(raise_if_any=True, raise_if_none=False)
			await asyncio.sleep(0.5)  # Wait for dropdown animation

			# Re-discover after clicking
			discovery = await _call_function_on_node(
				browser_session, node, _DISCOVER_OPTIONS_ON_NODE_JS
			)
			dropdown_type = discovery.get('type', 'unknown') if isinstance(discovery, dict) else 'unknown'
			options = discovery.get('options', []) if isinstance(discovery, dict) else []
		except Exception as e:
			logger.warning(f'Failed to click dropdown trigger: {e}')

	# ── Step 3c: If still no options, try GetDropdownOptionsEvent ──
	if not options:
		try:
			event = browser_session.event_bus.dispatch(GetDropdownOptionsEvent(node=node))
			dropdown_data = await event.event_result(timeout=3.0, raise_if_none=False, raise_if_any=False)
			if dropdown_data and isinstance(dropdown_data, dict):
				raw_options = dropdown_data.get('options', [])
				if raw_options:
					options = raw_options
					dropdown_type = dropdown_data.get('type', 'event_bus')
		except Exception as e:
			logger.debug(f'GetDropdownOptionsEvent failed: {e}')

	if not options:
		return ActionResult(
			error=f'No dropdown options found for element {params.index}. '
			f'The element may not be a dropdown, or it may use a custom implementation '
			f'that requires typing to search. Try using the input action instead.',
		)

	# ── Step 4: Match the target value ────────────────────────
	matched = _fuzzy_match_option(params.value, options)

	if not matched:
		available_texts = [opt.get('text', '') for opt in options[:20]]
		return ActionResult(
			error=f'No matching option found for "{params.value}". '
			f'Available options: {available_texts}',
		)

	# ── Step 5: Click the matched option ──────────────────────
	matched_text = matched.get('text', params.value)
	try:
		if dropdown_type == 'native_select':
			# For native selects, use JS to set value directly on the resolved node
			result = await _call_function_on_node(
				browser_session, node, _SELECT_NATIVE_ON_NODE_JS,
				arguments=[{'value': matched_text}],
			)
		else:
			# For custom dropdowns, try SelectDropdownOptionEvent first
			try:
				event = browser_session.event_bus.dispatch(
					SelectDropdownOptionEvent(node=node, text=matched_text)
				)
				selection_data = await event.event_result(timeout=3.0, raise_if_none=False, raise_if_any=False)
				if selection_data and isinstance(selection_data, dict) and selection_data.get('success'):
					result = {'success': True, 'clicked': selection_data.get('selected_text', matched_text)}
				else:
					# Fallback: click the option via page-level JS
					result = await _click_option_via_page_js(page, matched_text, dropdown_type)
			except Exception:
				result = await _click_option_via_page_js(page, matched_text, dropdown_type)
	except Exception as e:
		return ActionResult(error=f'Failed to select option "{matched_text}": {e}')

	if isinstance(result, dict) and not result.get('success'):
		available = result.get('available', [])
		return ActionResult(
			error=f'Failed to select "{matched_text}": {result.get("error", "unknown")}. '
			f'Available: {available}',
		)

	clicked_text = result.get('clicked', matched_text) if isinstance(result, dict) else matched_text

	# ── Step 6: Verify the selection ──────────────────────────
	await asyncio.sleep(0.3)  # Brief wait for UI update
	try:
		verify = await _call_function_on_node(
			browser_session, node, _VERIFY_SELECTION_ON_NODE_JS
		)
		current = verify.get('value', '') if isinstance(verify, dict) else ''
	except Exception:
		current = ''

	memory = f'Selected "{clicked_text}" for dropdown at index {params.index}'
	if current and normalize_name(current) != normalize_name(clicked_text):
		memory += f' (showing: "{current}")'

	logger.info(f'DomHand select: {memory}')

	return ActionResult(
		extracted_content=memory,
		include_extracted_content_only_once=False,
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
		raise ValueError('Failed to get dropdown options from event bus')

	raw_options = dropdown_data.get('options', [])
	if not raw_options:
		raise ValueError('No options returned from event bus')

	# Convert to our option format if needed
	options = raw_options if isinstance(raw_options, list) else []
	matched = _fuzzy_match_option(params.value, options)

	if not matched:
		available_texts = [opt.get('text', '') for opt in options[:20]]
		return ActionResult(
			error=f'No matching option found for "{params.value}". '
			f'Available options: {available_texts}',
		)

	matched_text = matched.get('text', params.value)

	# Select via event bus
	event = browser_session.event_bus.dispatch(
		SelectDropdownOptionEvent(node=node, text=matched_text)
	)
	selection_data = await event.event_result(timeout=3.0, raise_if_none=False, raise_if_any=True)

	clicked_text = matched_text
	if selection_data and isinstance(selection_data, dict):
		clicked_text = selection_data.get('selected_text', matched_text)

	memory = f'Selected "{clicked_text}" for dropdown at index {params.index}'
	logger.info(f'DomHand select: {memory}')

	return ActionResult(
		extracted_content=memory,
		include_extracted_content_only_once=False,
	)


# ── Helper: click option via page-level JS ───────────────────────────

async def _click_option_via_page_js(page: Any, matched_text: str, dropdown_type: str) -> dict[str, Any]:
	"""Click a dropdown option using page.evaluate with a global JS search.

	This is the fallback when the event bus approach doesn't work. It searches
	all visible option-like elements on the page (not tied to any specific
	element index).
	"""
	raw_result = await page.evaluate(_CLICK_OPTION_JS, matched_text)
	if isinstance(raw_result, str):
		return json.loads(raw_result)
	return raw_result if isinstance(raw_result, dict) else {'success': False, 'error': 'Unexpected result type'}
