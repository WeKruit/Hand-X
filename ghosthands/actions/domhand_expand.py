"""DomHand Expand — repeater section expansion.

Handles "Add More", "Add Another", "+" buttons that expand repeater sections
(e.g., multiple work experiences, education entries, references).

Flow:
1. Find the target section on the page
2. Discover "add" buttons within that section
3. Click to expand
4. Wait for new fields to appear
5. Return count of newly added fields
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession

from ghosthands.actions.views import DomHandExpandParams, normalize_name

logger = logging.getLogger(__name__)

# ── JavaScript: find add-more buttons in a section ───────────────────

_FIND_ADD_BUTTONS_JS = r"""
(sectionName) => {
	const sectionNorm = sectionName.toLowerCase().trim();

	// Find the section container
	const sectionSelectors = [
		'[data-section]', 'fieldset', '.form-section', '.section',
		'[class*="section"]', '[role="group"]', '[class*="repeater"]',
		'[class*="experience"]', '[class*="education"]', '[class*="reference"]',
	];

	let sectionEl = null;
	for (const sel of sectionSelectors) {
		for (const el of document.querySelectorAll(sel)) {
			const text = (
				el.getAttribute('data-section') ||
				el.querySelector('legend, .section-title, .section-header, h2, h3, h4')?.textContent ||
				''
			).toLowerCase().trim();
			if (text.includes(sectionNorm) || sectionNorm.includes(text)) {
				sectionEl = el;
				break;
			}
		}
		if (sectionEl) break;
	}

	// Fallback: search the whole page if no section found
	const searchRoot = sectionEl || document.body;

	// Find add-more buttons
	const addButtonPatterns = [
		/add\s*(more|another|new|entry|item|additional)/i,
		/\+\s*(add|new|more)/i,
		/create\s*(new|another)/i,
		/insert\s*(new|another|entry)/i,
	];
	const addButtonSelectors = [
		'button', 'a', '[role="button"]',
		'[class*="add"]', '[class*="plus"]',
		'[data-automation-id*="add"]', '[data-testid*="add"]',
	];

	const candidates = [];
	for (const sel of addButtonSelectors) {
		for (const el of searchRoot.querySelectorAll(sel)) {
			const rect = el.getBoundingClientRect();
			if (rect.width === 0 || rect.height === 0) continue;

			const text = (el.textContent || '').trim();
			const ariaLabel = el.getAttribute('aria-label') || '';
			const title = el.getAttribute('title') || '';
			const combinedText = [text, ariaLabel, title].join(' ');

			// Check if it looks like an add button
			const isAddButton = addButtonPatterns.some(p => p.test(combinedText)) ||
				text === '+' ||
				text === 'Add' ||
				(el.classList && (
					el.classList.contains('add-button') ||
					el.classList.contains('add-more') ||
					el.classList.contains('btn-add')
				));

			if (isAddButton) {
				// Tag it for later clicking
				const tag = 'dh-add-' + candidates.length;
				el.setAttribute('data-dh-add-btn', tag);
				candidates.push({
					tag: tag,
					text: text.slice(0, 100),
					ariaLabel: ariaLabel.slice(0, 100),
					x: Math.round(rect.x + rect.width / 2),
					y: Math.round(rect.y + rect.height / 2),
				});
			}
		}
	}

	return JSON.stringify({
		sectionFound: !!sectionEl,
		sectionText: sectionEl ? (sectionEl.querySelector('legend, h2, h3, h4')?.textContent || '').trim() : '',
		buttons: candidates,
	});
}
"""

# JavaScript: count interactive fields in the section
_COUNT_FIELDS_JS = r"""
(sectionName) => {
	const INTERACTIVE = 'input, select, textarea, [role="textbox"], [role="combobox"], [role="listbox"], [role="checkbox"], [role="radio"]';
	const sectionNorm = sectionName.toLowerCase().trim();

	const sectionSelectors = [
		'[data-section]', 'fieldset', '.form-section', '.section',
		'[class*="section"]', '[role="group"]', '[class*="repeater"]',
	];

	let sectionEl = null;
	for (const sel of sectionSelectors) {
		for (const el of document.querySelectorAll(sel)) {
			const text = (
				el.getAttribute('data-section') ||
				el.querySelector('legend, .section-title, .section-header, h2, h3, h4')?.textContent ||
				''
			).toLowerCase().trim();
			if (text.includes(sectionNorm) || sectionNorm.includes(text)) {
				sectionEl = el;
				break;
			}
		}
		if (sectionEl) break;
	}

	const searchRoot = sectionEl || document.body;
	const fields = searchRoot.querySelectorAll(INTERACTIVE);
	let visibleCount = 0;
	for (const f of fields) {
		if (f.type === 'hidden' || f.type === 'submit' || f.type === 'button') continue;
		const rect = f.getBoundingClientRect();
		if (rect.width > 0 && rect.height > 0) visibleCount++;
	}

	return JSON.stringify({count: visibleCount});
}
"""

# JavaScript: click a tagged add button
_CLICK_ADD_BUTTON_JS = r"""
(tag) => {
	const el = document.querySelector('[data-dh-add-btn="' + tag + '"]');
	if (!el) return JSON.stringify({success: false, error: 'Add button not found: ' + tag});

	try {
		el.scrollIntoView({block: 'center', behavior: 'instant'});
		el.click();
		return JSON.stringify({success: true, text: (el.textContent || '').trim().slice(0, 100)});
	} catch (e) {
		return JSON.stringify({success: false, error: e.message});
	}
}
"""


# ── Core action function ─────────────────────────────────────────────

async def domhand_expand(params: DomHandExpandParams, browser_session: BrowserSession) -> ActionResult:
	"""Click 'Add More' buttons to expand repeater sections.

	1. Find the target section
	2. Discover add-more buttons
	3. Count fields before clicking
	4. Click the add button
	5. Wait for new fields to appear
	6. Return count of new fields added
	"""
	page = await browser_session.get_current_page()
	if not page:
		return ActionResult(error='No active page found in browser session')

	# ── Step 1-2: Find section and add buttons ────────────────
	try:
		raw_buttons = await page.evaluate(_FIND_ADD_BUTTONS_JS, params.section)
		button_info: dict[str, Any] = json.loads(raw_buttons) if isinstance(raw_buttons, str) else raw_buttons
	except Exception as e:
		return ActionResult(error=f'Failed to search for add buttons in section "{params.section}": {e}')

	buttons = button_info.get('buttons', [])
	section_found = button_info.get('sectionFound', False)

	if not buttons:
		if not section_found:
			return ActionResult(
				error=f'Section "{params.section}" not found on the page. '
				f'The section may have a different name or may not exist.',
			)
		return ActionResult(
			error=f'No "Add More" buttons found in section "{params.section}". '
			f'The section may not support adding more entries.',
		)

	# ── Step 3: Count fields before expansion ─────────────────
	try:
		raw_before = await page.evaluate(_COUNT_FIELDS_JS, params.section)
		before_info = json.loads(raw_before) if isinstance(raw_before, str) else raw_before
		fields_before = before_info.get('count', 0)
	except Exception:
		fields_before = 0

	# ── Step 4: Click the first add button ────────────────────
	target_button = buttons[0]
	try:
		raw_click = await page.evaluate(_CLICK_ADD_BUTTON_JS, target_button['tag'])
		click_result = json.loads(raw_click) if isinstance(raw_click, str) else raw_click
	except Exception as e:
		return ActionResult(error=f'Failed to click add button: {e}')

	if isinstance(click_result, dict) and not click_result.get('success'):
		return ActionResult(error=f'Failed to click add button: {click_result.get("error", "unknown")}')

	button_text = click_result.get('text', target_button.get('text', 'Add')) if isinstance(click_result, dict) else 'Add'

	# ── Step 5: Wait for new fields to appear ─────────────────
	await asyncio.sleep(1.0)  # Wait for DOM to settle (animation, rendering)

	# Count fields after expansion
	try:
		raw_after = await page.evaluate(_COUNT_FIELDS_JS, params.section)
		after_info = json.loads(raw_after) if isinstance(raw_after, str) else raw_after
		fields_after = after_info.get('count', 0)
	except Exception:
		fields_after = fields_before  # Assume no change if counting fails

	new_fields = max(0, fields_after - fields_before)

	# ── Build result ──────────────────────────────────────────
	if new_fields > 0:
		memory = (
			f'Expanded section "{params.section}" by clicking "{button_text}". '
			f'{new_fields} new field(s) appeared (total: {fields_after}).'
		)
		logger.info(f'DomHand expand: {memory}')
		return ActionResult(
			extracted_content=memory,
			include_extracted_content_only_once=False,
		)
	else:
		# Button was clicked but no new fields detected
		memory = (
			f'Clicked "{button_text}" in section "{params.section}", '
			f'but no new fields were detected (fields before: {fields_before}, after: {fields_after}). '
			f'The section may have expanded off-screen, or the button may require multiple clicks.'
		)
		logger.warning(f'DomHand expand: {memory}')
		return ActionResult(
			extracted_content=memory,
			include_extracted_content_only_once=False,
		)
