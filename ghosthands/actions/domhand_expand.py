"""DomHand Expand — repeater section expansion.

Handles "Add More", "Add Another", "+" buttons that expand repeater sections
(e.g., multiple work experiences, education entries, references).

Two-phase click approach (ported from GHOST-HANDS):
1. DOM scan: TreeWalker finds section heading, then nearest Add button AFTER it
2. Tag the button with data-dh-add-target, scroll into view
3. Playwright click (trusted mouse events) — Workday React handlers require this
4. Fallback: JS .click() if Playwright fails
5. Scroll to first empty field in the newly expanded entry
"""

import asyncio
import json
import logging
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession

from ghosthands.actions.views import DomHandExpandParams, normalize_name

logger = logging.getLogger(__name__)

# ── JavaScript: heading-first Add button discovery (ported from GHOST-HANDS) ──
# Uses TreeWalker to find all headings + buttons in DOM order.
# Finds the LAST heading matching the section label, then the first Add button
# AFTER that heading. Stops at headings for different sections.

_FIND_ADD_BUTTON_HEADING_FIRST_JS = r"""
(sectionName) => {
	var label = sectionName.toLowerCase().trim();
	var labelWords = label.split(/\s+/);

	// Remove any stale tag from a prior call
	var old = document.querySelector('[data-dh-add-target]');
	if (old) old.removeAttribute('data-dh-add-target');

	// --- Helpers ---
	function textMatchesLabel(text) {
		var t = text.toLowerCase().trim();
		if (t.includes(label)) return true;
		if (labelWords.length > 1) {
			var allFound = true;
			for (var w = 0; w < labelWords.length; w++) {
				if (!t.includes(labelWords[w])) { allFound = false; break; }
			}
			if (allFound) return true;
		}
		return false;
	}
	function isHeadingEl(n) {
		var tag = n.tagName;
		if (tag === 'H1' || tag === 'H2' || tag === 'H3' || tag === 'H4' ||
			tag === 'H5' || tag === 'H6' || tag === 'LEGEND') return true;
		if (n.getAttribute('role') === 'heading') return true;
		var aid = (n.getAttribute('data-automation-id') || '').toLowerCase();
		if (aid && (aid.includes('header') || aid.includes('sectionlabel'))) return true;
		return false;
	}
	function isButtonEl(n) {
		var tag = n.tagName;
		return tag === 'BUTTON' || tag === 'A' || n.getAttribute('role') === 'button';
	}
	function isAddButton(el) {
		var btnText = (el.textContent || '').trim().toLowerCase();
		var ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
		var aid = (el.getAttribute('data-automation-id') || '').toLowerCase();
		var title = (el.getAttribute('title') || '').toLowerCase();
		return btnText.startsWith('add') || btnText === '+' ||
			ariaLabel.startsWith('add') || ariaLabel.includes('add') ||
			aid.includes('add') || title.includes('add');
	}
	function isVisible(el) {
		var rect = el.getBoundingClientRect();
		return rect.width > 0 && rect.height > 0;
	}
	function tagAndReturn(el) {
		el.setAttribute('data-dh-add-target', 'true');
		el.scrollIntoView({ block: 'center', behavior: 'instant' });
		return JSON.stringify({
			found: true,
			text: (el.textContent || '').trim().slice(0, 100),
			tag: el.tagName,
		});
	}

	// ═══ Approach 1: TreeWalker heading-first (primary) ═══
	var walker = document.createTreeWalker(
		document.body,
		NodeFilter.SHOW_ELEMENT,
		{ acceptNode: function(n) {
			if (isButtonEl(n) || isHeadingEl(n)) return NodeFilter.FILTER_ACCEPT;
			return NodeFilter.FILTER_SKIP;
		}}
	);

	var elements = [];
	var el;
	while (el = walker.nextNode()) { elements.push(el); }

	// Find the LAST heading that matches our section label
	var lastHeadingIndex = -1;
	for (var i = 0; i < elements.length; i++) {
		if (!isButtonEl(elements[i]) && isHeadingEl(elements[i])) {
			if (textMatchesLabel(elements[i].textContent || '')) {
				lastHeadingIndex = i;
			}
		}
	}

	if (lastHeadingIndex !== -1) {
		// Starting after the heading, find the first Add button.
		// Stop if we hit a heading for a DIFFERENT section.
		var knownSections = ['work experience', 'education', 'skills', 'websites',
			'certifications', 'references', 'languages', 'social', 'experience'];
		for (var j = lastHeadingIndex + 1; j < elements.length; j++) {
			var el = elements[j];
			if (!isButtonEl(el)) {
				var headText = (el.textContent || '').toLowerCase();
				var isDifferentSection = false;
				for (var s = 0; s < knownSections.length; s++) {
					if (knownSections[s] !== label && headText.includes(knownSections[s])) {
						isDifferentSection = true;
						break;
					}
				}
				if (isDifferentSection) break;
				continue;
			}
			if (!isAddButton(el)) continue;
			if (!isVisible(el)) continue;
			return tagAndReturn(el);
		}
	}

	// ═══ Approach 2: Parent traversal from matching text ═══
	// Find ANY heading-like element with matching text, then walk UP
	// the DOM tree to find an Add button in a parent container.
	var headingSels = 'h1, h2, h3, h4, h5, h6, legend, [role="heading"], ' +
		'[data-automation-id], label, .section-title, .section-header';
	var allHeadings = document.querySelectorAll(headingSels);
	for (var k = 0; k < allHeadings.length; k++) {
		if (!textMatchesLabel(allHeadings[k].textContent || '')) continue;
		// Walk up the DOM tree looking for a container with an Add button
		var container = allHeadings[k].parentElement;
		for (var p = 0; p < 8 && container && container !== document.body; p++) {
			var btns = container.querySelectorAll('button, a, [role="button"]');
			for (var b = 0; b < btns.length; b++) {
				if (!isAddButton(btns[b])) continue;
				if (!isVisible(btns[b])) continue;
				return tagAndReturn(btns[b]);
			}
			container = container.parentElement;
		}
	}

	if (lastHeadingIndex === -1) {
		return JSON.stringify({found: false, reason: 'heading_not_found'});
	}
	return JSON.stringify({found: false, reason: 'no_add_button_after_heading'});
}
"""

# ── JavaScript: scroll to the first empty field in a section ─────────
# After clicking Add, finds the first empty text input in the section
# and scrolls it into view. This targets the newly created entry.

_SCROLL_TO_NEW_ENTRY_JS = r"""
(sectionName) => {
	var label = sectionName.toLowerCase().trim();

	// Find the section container by heading
	var headings = document.querySelectorAll('h2, h3, h4, h5, legend, [data-automation-id]');
	var sectionEl = null;
	for (var i = 0; i < headings.length; i++) {
		var text = (headings[i].textContent || '').toLowerCase();
		if (text.includes(label)) {
			sectionEl = headings[i].parentElement;
			for (var u = 0; u < 5 && sectionEl; u++) {
				var inputs = sectionEl.querySelectorAll('input[type="text"], input:not([type]), textarea');
				if (inputs.length >= 2) break;
				sectionEl = sectionEl.parentElement;
			}
			break;
		}
	}

	var searchArea = sectionEl || document.body;
	var inputs = searchArea.querySelectorAll('input[type="text"], input:not([type]), textarea');
	var firstEmpty = null;
	for (var j = 0; j < inputs.length; j++) {
		var inp = inputs[j];
		if (inp.disabled || inp.readOnly || inp.type === 'hidden') continue;
		var rect = inp.getBoundingClientRect();
		if (rect.width < 20 || rect.height < 10) continue;
		var ph = (inp.placeholder || '').toUpperCase();
		if (ph === 'MM' || ph === 'DD' || ph === 'YYYY') continue;
		if (inp.closest && inp.closest('[role="listbox"]')) continue;
		if (!inp.value || inp.value.trim() === '') {
			firstEmpty = inp;
			break;
		}
	}

	if (firstEmpty) {
		firstEmpty.scrollIntoView({ block: 'center', behavior: 'instant' });
		return JSON.stringify({scrolled: true, target: 'empty_field'});
	} else if (sectionEl) {
		sectionEl.scrollIntoView({ block: 'center', behavior: 'instant' });
		return JSON.stringify({scrolled: true, target: 'section'});
	}
	return JSON.stringify({scrolled: false});
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


# ── Core action function ─────────────────────────────────────────────

async def domhand_expand(params: DomHandExpandParams, browser_session: BrowserSession) -> ActionResult:
	"""Click 'Add' buttons to expand repeater sections (Work Experience, Education, etc.).

	Two-phase approach (ported from GHOST-HANDS):
	1. DOM scan: TreeWalker finds section heading → nearest Add button after it
	2. Tag button with data-dh-add-target, scroll into view
	3. Playwright click (trusted events for React) with JS fallback
	4. Scroll to first empty field in the newly expanded entry
	5. Return result with field counts
	"""
	page = await browser_session.get_current_page()
	if not page:
		return ActionResult(error='No active page found in browser session')

	try:
		open_inline_forms = await page.evaluate("""() => {
			const visible = (el) => {
				if (!el) return false;
				const style = window.getComputedStyle(el);
				if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
				const rect = el.getBoundingClientRect();
				return rect.width > 0 && rect.height > 0;
			};
			return Array.from(document.querySelectorAll('.profile-inline-form'))
				.filter((el) => visible(el))
				.length;
		}""")
	except Exception:
		open_inline_forms = 0
	if int(open_inline_forms or 0) > 0:
		return ActionResult(
			error=(
				'A profile inline editor is already open. Finish the current entry and wait for its '
				'saved tile before clicking another Add button.'
			),
		)

	# ── Step 1: Count fields before expansion ─────────────────
	try:
		raw_before = await page.evaluate(_COUNT_FIELDS_JS, params.section)
		before_info = json.loads(raw_before) if isinstance(raw_before, str) else raw_before
		fields_before = before_info.get('count', 0)
	except Exception:
		fields_before = 0

	# ── Step 2: Find the Add button via heading-first TreeWalker ──
	try:
		raw_result = await page.evaluate(_FIND_ADD_BUTTON_HEADING_FIRST_JS, params.section)
		find_result: dict[str, Any] = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
	except Exception as e:
		return ActionResult(error=f'Failed to search for Add button in section "{params.section}": {e}')

	if not find_result.get('found'):
		reason = find_result.get('reason', 'unknown')
		if reason == 'heading_not_found':
			return ActionResult(
				error=f'Section "{params.section}" heading not found on the page. '
				f'The section may have a different name or may not be visible yet.',
			)
		return ActionResult(
			error=f'No "Add" button found after "{params.section}" heading. '
			f'The section may already have entries or not support adding more.',
		)

	button_text = find_result.get('text', 'Add')

	# ── Step 3: Playwright click (trusted mouse events for React) ──
	# Workday and other React-based ATS platforms require trusted events
	# from real mouse clicks. JS el.click() is untrusted and gets ignored.
	clicked = False
	try:
		btn = page.locator('[data-dh-add-target="true"]')
		await btn.click(timeout=3000)
		clicked = True
		logger.info(f'Clicked Add button via Playwright: "{button_text}" in {params.section}')
	except Exception as e:
		logger.debug(f'Playwright click failed for Add button, trying JS fallback: {e}')

	# Fallback: JS click if Playwright couldn't reach it
	if not clicked:
		try:
			await page.evaluate("""() => {
				const el = document.querySelector('[data-dh-add-target="true"]');
				if (el) el.click();
			}""")
			logger.info(f'Clicked Add button via JS fallback: "{button_text}" in {params.section}')
		except Exception as e:
			return ActionResult(error=f'Failed to click Add button "{button_text}": {e}')

	# Clean up the tag
	try:
		await page.evaluate("""() => {
			const el = document.querySelector('[data-dh-add-target]');
			if (el) el.removeAttribute('data-dh-add-target');
		}""")
	except Exception:
		pass

	# ── Step 4: Wait for new fields + scroll to them ──────────
	await asyncio.sleep(1.0)  # Wait for DOM to settle (React state update, animation)

	# Scroll to the first empty field in the section
	try:
		await page.evaluate(_SCROLL_TO_NEW_ENTRY_JS, params.section)
	except Exception:
		pass

	await asyncio.sleep(0.5)

	# ── Step 5: Count fields after expansion ──────────────────
	try:
		raw_after = await page.evaluate(_COUNT_FIELDS_JS, params.section)
		after_info = json.loads(raw_after) if isinstance(raw_after, str) else raw_after
		fields_after = after_info.get('count', 0)
	except Exception:
		fields_after = fields_before

	new_fields = max(0, fields_after - fields_before)

	# ── Build result ──────────────────────────────────────────
	if new_fields > 0:
		memory = (
			f'Expanded "{params.section}" by clicking "{button_text}". '
			f'{new_fields} new field(s) appeared (total: {fields_after}). '
			f'Now call domhand_fill to fill the new entry fields.'
		)
		logger.info(f'DomHand expand: {memory}')
		return ActionResult(
			extracted_content=memory,
			include_extracted_content_only_once=False,
		)
	else:
		memory = (
			f'Clicked "{button_text}" in "{params.section}". '
			f'Fields before: {fields_before}, after: {fields_after}. '
			f'The new entry may have expanded off-screen — call domhand_fill '
			f'to fill any new fields that appeared.'
		)
		logger.warning(f'DomHand expand: {memory}')
		return ActionResult(
			extracted_content=memory,
			include_extracted_content_only_once=False,
		)
