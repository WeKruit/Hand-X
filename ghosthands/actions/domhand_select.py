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
"""

import asyncio
import json
import logging
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession

from ghosthands.actions.views import DomHandSelectParams, normalize_name

logger = logging.getLogger(__name__)

# ── JavaScript: discover dropdown options ────────────────────────────

_DISCOVER_OPTIONS_JS = r"""
(triggerIndex) => {
	// Find the element by browser-use index (data-highlight-index attribute)
	const el = document.querySelector('[data-highlight-index="' + triggerIndex + '"]');
	if (!el) return JSON.stringify({error: 'Trigger element not found at index ' + triggerIndex});

	const tag = el.tagName.toLowerCase();

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
			currentValue: el.options[el.selectedIndex]?.text?.trim() || '',
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
				currentValue: el.value || el.textContent?.trim() || '',
			});
		}
	}

	// Case 3: Workday-style custom dropdowns (data-uxi-widget-type)
	const widgetType = el.getAttribute('data-uxi-widget-type');
	if (widgetType === 'selectinput' || el.getAttribute('role') === 'combobox') {
		// Options may be in a popup/portal element
		const popups = document.querySelectorAll(
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
						currentValue: el.value || el.textContent?.trim() || '',
					});
				}
			}
		}
	}

	// Case 4: Generic — look for any visible listbox/menu on the page
	const allListboxes = document.querySelectorAll(
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
		currentValue: el.value || el.textContent?.trim() || '',
		error: 'Could not discover dropdown options. The dropdown may need to be clicked first.',
	});
}
"""

# JavaScript: click an option by its text content within a visible listbox/popup
_CLICK_OPTION_JS = r"""
(targetText, listboxType) => {
	const lowerTarget = targetText.toLowerCase().trim();

	// Collect all visible option-like elements
	const selectors = [
		'[role="option"]', '[role="menuitem"]',
		'li', '[class*="option"]', '[data-value]',
	];
	const candidates = [];
	for (const selector of selectors) {
		for (const el of document.querySelectorAll(selector)) {
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
}
"""

# JavaScript: select a native <select> option by value or text
_SELECT_NATIVE_JS = r"""
(triggerIndex, targetValue) => {
	const el = document.querySelector('[data-highlight-index="' + triggerIndex + '"]');
	if (!el || el.tagName.toLowerCase() !== 'select') {
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
}
"""

# JavaScript: verify current selection
_VERIFY_SELECTION_JS = r"""
(triggerIndex) => {
	const el = document.querySelector('[data-highlight-index="' + triggerIndex + '"]');
	if (!el) return JSON.stringify({value: '', error: 'Element not found'});

	if (el.tagName.toLowerCase() === 'select') {
		const sel = el.options[el.selectedIndex];
		return JSON.stringify({value: sel ? sel.text.trim() : ''});
	}

	// For ARIA/custom: check aria-label, value, or text content
	const value = el.value || el.getAttribute('aria-label') || el.textContent?.trim() || '';
	return JSON.stringify({value: value});
}
"""


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

	# Pass 3: Word overlap (at least 2 shared words)
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

	# ── Step 1: Click the trigger to open dropdown ────────────
	try:
		node = await browser_session.get_element_by_index(params.index)
		if node is None:
			return ActionResult(error=f'Element index {params.index} not available. Page may have changed.')
	except Exception as e:
		return ActionResult(error=f'Failed to find element at index {params.index}: {e}')

	# ── Step 2: Discover options (try before clicking — some are pre-rendered) ──
	try:
		raw_discovery = await page.evaluate(_DISCOVER_OPTIONS_JS, params.index)
		discovery: dict[str, Any] = json.loads(raw_discovery) if isinstance(raw_discovery, str) else raw_discovery
	except Exception as e:
		discovery = {'type': 'unknown', 'options': [], 'error': str(e)}

	dropdown_type = discovery.get('type', 'unknown')
	options = discovery.get('options', [])

	# ── Step 2b: If no options found, click to open and re-discover ──
	if not options or dropdown_type == 'unknown':
		try:
			# Use the browser_session event bus to click the element
			from browser_use.browser.events import ClickElementEvent
			event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
			await event
			await event.event_result(raise_if_any=True, raise_if_none=False)
			await asyncio.sleep(0.5)  # Wait for dropdown animation

			# Re-discover after clicking
			raw_discovery = await page.evaluate(_DISCOVER_OPTIONS_JS, params.index)
			discovery = json.loads(raw_discovery) if isinstance(raw_discovery, str) else raw_discovery
			dropdown_type = discovery.get('type', 'unknown')
			options = discovery.get('options', [])
		except Exception as e:
			logger.warning(f'Failed to click dropdown trigger: {e}')

	if not options:
		return ActionResult(
			error=f'No dropdown options found for element {params.index}. '
			f'The element may not be a dropdown, or it may use a custom implementation '
			f'that requires typing to search. Try using the input action instead.',
		)

	# ── Step 3: Match the target value ────────────────────────
	matched = _fuzzy_match_option(params.value, options)

	if not matched:
		available_texts = [opt.get('text', '') for opt in options[:20]]
		return ActionResult(
			error=f'No matching option found for "{params.value}". '
			f'Available options: {available_texts}',
		)

	# ── Step 4: Click the matched option ──────────────────────
	matched_text = matched.get('text', params.value)
	try:
		if dropdown_type == 'native_select':
			# For native selects, use JavaScript to set value directly
			result_json = await page.evaluate(_SELECT_NATIVE_JS, params.index, matched_text)
			result = json.loads(result_json) if isinstance(result_json, str) else result_json
		else:
			# For custom dropdowns, click the option element
			result_json = await page.evaluate(_CLICK_OPTION_JS, matched_text, dropdown_type)
			result = json.loads(result_json) if isinstance(result_json, str) else result_json
	except Exception as e:
		return ActionResult(error=f'Failed to select option "{matched_text}": {e}')

	if isinstance(result, dict) and not result.get('success'):
		available = result.get('available', [])
		return ActionResult(
			error=f'Failed to select "{matched_text}": {result.get("error", "unknown")}. '
			f'Available: {available}',
		)

	clicked_text = result.get('clicked', matched_text) if isinstance(result, dict) else matched_text

	# ── Step 5: Verify the selection ──────────────────────────
	await asyncio.sleep(0.3)  # Brief wait for UI update
	try:
		verify_json = await page.evaluate(_VERIFY_SELECTION_JS, params.index)
		verify = json.loads(verify_json) if isinstance(verify_json, str) else verify_json
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
