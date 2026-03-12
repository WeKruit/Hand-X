"""DomHand Fill — the core action that extracts form fields, generates answers via
a single cheap LLM call, and fills everything via CDP DOM manipulation.

Ported from GHOST-HANDS formFiller.ts.  This is the primary workhorse action for
job application form filling.  It:

1. Injects browser-side helper library (``__ff``) into the page
2. Extracts ALL visible form fields including radio/checkbox groups and button groups
3. Makes a SINGLE cheap LLM call (Haiku) with resume profile + all fields -> answer map
4. Fills each field via the appropriate strategy:
   - Native selects      -> CDP ``HTMLSelectElement.value`` + change event
   - Custom dropdowns     -> click trigger, discover options, click match
   - Searchable combos    -> type to filter, click matching option
   - Radio/checkbox groups -> click the matching item
   - Button groups        -> click the button whose text matches
   - Text/email/tel/etc.  -> native setter + input/change/blur events
   - Textareas            -> same, or ``textContent`` for contenteditable
   - Date fields          -> direct set or Workday-style keyboard fill
   - Checkboxes/toggles   -> click to toggle state
5. Re-extracts to verify fills and catch newly revealed conditional fields
6. Repeats for up to ``MAX_FILL_ROUNDS`` rounds
7. Returns ``ActionResult`` with filled/failed/unfilled counts
"""

import asyncio
import json
import logging
import os
import re
from datetime import date
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession

from ghosthands.actions.views import (
	DomHandFillParams,
	FillFieldResult,
	FormField,
	get_stable_field_key,
	is_placeholder_value,
	normalize_name,
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

MAX_FILL_ROUNDS = 3

# Selector for all interactive form elements (matches GH formFiller.ts).
INTERACTIVE_SELECTOR = ', '.join([
	'input', 'select', 'textarea',
	'[role="textbox"]', '[role="combobox"]', '[role="listbox"]',
	'[role="checkbox"]', '[role="radio"]', '[role="switch"]',
	'[role="spinbutton"]', '[role="slider"]', '[role="searchbox"]',
	'[data-uxi-widget-type="selectinput"]',
	'[aria-haspopup="listbox"]',
])

# Regex for fields whose values should never be fabricated.
_SOCIAL_OR_ID_NO_GUESS_RE = re.compile(
	r'\b(twitter|x(\.com)?\s*(handle|username|profile)?|github|gitlab|linkedin'
	r'|instagram|tiktok|facebook|social\s*(media|profile)?|handle|username|user\s*name'
	r"|passport|driver'?s?\s*license|license\s*number|national\s*id|id\s*number"
	r'|tax\s*id|itin|ein|ssn|social security)\b',
	re.IGNORECASE,
)

# Navigation-like button labels to skip when detecting button groups.
_NAV_BUTTON_LABELS = frozenset([
	'save and continue', 'next', 'continue', 'submit', 'submit application',
	'apply', 'add', 'add another', 'replace', 'upload', 'browse', 'remove',
	'delete', 'cancel', 'back', 'previous', 'close', 'save', 'select one',
	'choose file',
])


# ── Browser-side helper injection ────────────────────────────────────

def _build_inject_helpers_js() -> str:
	"""Return the JS that installs ``window.__ff`` — the browser-side helper
	library used by every subsequent ``page.evaluate()`` call.

	Ported 1:1 from GH formFiller.ts ``injectHelpers()``.
	"""
	selector_json = json.dumps(INTERACTIVE_SELECTOR)
	return f"""() => {{
	if (typeof globalThis.__name === 'undefined') {{
		globalThis.__name = function(fn) {{ return fn; }};
	}}
	var _prevNextId = (window.__ff && window.__ff.nextId) || 0;
	window.__ff = {{
		SELECTOR: {selector_json},

		rootParent: function(node) {{
			if (!node) return null;
			if (node.parentElement) return node.parentElement;
			var root = node.getRootNode ? node.getRootNode() : null;
			if (root && root.host) return root.host;
			return null;
		}},

		allRoots: function() {{
			var roots = [document];
			var seen = new Set([document]);
			for (var i = 0; i < roots.length; i++) {{
				var root = roots[i];
				if (!root.querySelectorAll) continue;
				root.querySelectorAll('*').forEach(function(el) {{
					if (el.shadowRoot && !seen.has(el.shadowRoot)) {{
						seen.add(el.shadowRoot);
						roots.push(el.shadowRoot);
					}}
				}});
			}}
			return roots;
		}},

		queryAll: function(selector) {{
			var results = [];
			var seen = new Set();
			window.__ff.allRoots().forEach(function(root) {{
				if (!root.querySelectorAll) return;
				root.querySelectorAll(selector).forEach(function(el) {{
					if (seen.has(el)) return;
					seen.add(el);
					results.push(el);
				}});
			}});
			return results;
		}},

		queryOne: function(selector) {{
			var hits = window.__ff.queryAll(selector);
			return hits.length > 0 ? hits[0] : null;
		}},

		byId: function(id) {{
			return window.__ff.queryOne('[data-ff-id="' + id + '"]');
		}},

		getByDomId: function(id) {{
			if (!id) return null;
			var escapedId = String(id).replace(/"/g, '\\\\"');
			var roots = window.__ff.allRoots();
			for (var i = 0; i < roots.length; i++) {{
				var root = roots[i];
				if (root.getElementById) {{
					var direct = root.getElementById(id);
					if (direct) return direct;
				}}
				if (root.querySelector) {{
					var queried = root.querySelector('[id="' + escapedId + '"]');
					if (queried) return queried;
				}}
			}}
			return null;
		}},

		closestCrossRoot: function(el, selector) {{
			var node = el;
			while (node) {{
				if (node.matches && node.matches(selector)) return node;
				node = window.__ff.rootParent(node);
			}}
			return null;
		}},

		getAccessibleName: function(el) {{
			var lblBy = el.getAttribute('aria-labelledby');
			if (lblBy) {{
				var uxiC = window.__ff.closestCrossRoot(el, '[data-uxi-widget-type]') || window.__ff.closestCrossRoot(el, '[role="combobox"]');
				var t = lblBy.split(/\\s+/)
					.map(function(id) {{
						var r = window.__ff.getByDomId(id);
						if (!r) return '';
						if (uxiC && uxiC.contains(r)) return '';
						if (el.contains(r)) return '';
						return r.textContent.trim();
					}})
					.filter(Boolean).join(' ');
				if (t) return t;
			}}
			var elType = el.type || el.getAttribute('role') || '';
			var al = el.getAttribute('aria-label');
			if (al && elType !== 'radio' && elType !== 'checkbox') {{
				al = al.trim();
				if (el.getAttribute('aria-haspopup') === 'listbox' && el.textContent) {{
					var val = el.textContent.trim();
					if (val && al.includes(val)) {{
						al = al.replace(val, '');
						if (/\\bRequired\\b/i.test(al)) {{
							el.dataset.ffRequired = 'true';
							al = al.replace(/\\s*Required\\s*/gi, ' ');
						}}
						al = al.replace(/\\s+/g, ' ').trim();
					}}
				}}
				if (al) return al;
			}}
			if (el.id) {{
				var lbl = window.__ff.queryOne('label[for="' + el.id + '"]');
				if (lbl) {{
					var c = lbl.cloneNode(true);
					c.querySelectorAll('input, .required, span[aria-hidden]').forEach(function(x) {{ x.remove(); }});
					var tx = c.textContent.trim();
					if (tx) return tx;
				}}
			}}
			var from = el;
			var tp = el.type || el.getAttribute('role') || '';
			if (tp === 'checkbox' || tp === 'radio') {{
				var grp = window.__ff.closestCrossRoot(el, '.checkbox-group, .radio-group, [role=group], [role=radiogroup]');
				var grpParent = grp ? window.__ff.rootParent(grp) : null;
				if (grp && grpParent) from = grpParent;
			}}
			var group = window.__ff.closestCrossRoot(from, '.form-group, .field, .form-field, fieldset') || from;
			var lbl2 = group.querySelector(':scope > label, :scope > legend');
			if (lbl2) {{
				var c2 = lbl2.cloneNode(true);
				c2.querySelectorAll('input, .required, span[aria-hidden]').forEach(function(x) {{ x.remove(); }});
				var tx2 = c2.textContent.trim();
				if (tx2) return tx2;
			}}
			if (el.type === 'file') {{
				var card = window.__ff.closestCrossRoot(el, '.card, .section, [class*="upload"], [class*="drop"]');
				if (card) {{
					var parent = window.__ff.closestCrossRoot(card, '.card, .section') || card;
					var hdr = parent.querySelector('h1, h2, h3, h4, legend, [class*="heading"], [class*="title"]');
					if (hdr) {{
						var ht = hdr.textContent.trim();
						if (ht) return ht;
					}}
				}}
			}}
			return el.placeholder || el.getAttribute('title') || '';
		}},

		isVisible: function(el) {{
			var n = el;
			while (n && n !== document.body) {{
				var s = window.getComputedStyle(n);
				if (s.display === 'none' || s.visibility === 'hidden') return false;
				if (n.getAttribute && n.getAttribute('aria-hidden') === 'true') return false;
				n = window.__ff.rootParent(n);
			}}
			return true;
		}},

		getSection: function(el) {{
			var n = window.__ff.rootParent(el);
			while (n) {{
				var h = n.querySelector(':scope > h1, :scope > h2, :scope > h3, :scope > legend');
				if (h) return h.textContent.trim();
				n = window.__ff.rootParent(n);
			}}
			return '';
		}},

		nextId: _prevNextId,
		tag: function(el) {{
			if (!el.hasAttribute('data-ff-id')) {{
				el.setAttribute('data-ff-id', 'ff-' + (window.__ff.nextId++));
			}}
			return el.getAttribute('data-ff-id');
		}}
	}};
	return 'ok';
}}"""


# ── Field extraction JS ──────────────────────────────────────────────

_EXTRACT_FIELDS_JS = r"""() => {
	var ff = window.__ff;
	if (!ff) return JSON.stringify([]);
	var seen = new Set();
	var out = [];

	var shouldSkip = function(el) {
		if (ff.closestCrossRoot(el, '[class*="select-dropdown"], [class*="select-option"]')) return true;
		if (ff.closestCrossRoot(el, '.iti__dropdown-content')) return true;
		if (ff.closestCrossRoot(el, '[data-automation-id="activeListContainer"]')) return true;
		if (el.getAttribute('role') === 'listbox' && el.closest('[role="combobox"]')) return true;
		if (el.getAttribute('role') === 'listbox' && el.id) {
			var controller = ff.queryOne('[role="combobox"][aria-controls="' + el.id + '"]');
			if (controller) return true;
		}
		if (el.tagName === 'INPUT' && el.type === 'search' && ff.closestCrossRoot(el, '[class*="dropdown"], [role="dialog"]')) return true;
		if (el.tagName === 'INPUT' && (el.type === 'radio' || el.type === 'checkbox') && window.getComputedStyle(el).display === 'none') return true;
		return false;
	};

	var getOptionMainText = function(opt) {
		var clone = opt.cloneNode(true);
		clone.querySelectorAll('[class*="desc"], [class*="sub"], [class*="hint"], .option-desc, small').forEach(function(x) { x.remove(); });
		return clone.textContent ? clone.textContent.trim() : '';
	};

	ff.queryAll(ff.SELECTOR).forEach(function(el) {
		if (seen.has(el)) return;
		seen.add(el);
		if (shouldSkip(el)) return;

		var id = ff.tag(el);
		var type = (function() {
			var role = el.getAttribute('role');
			if (role === 'textbox' && el.getAttribute('aria-multiline') === 'true') return 'textarea';
			if (role === 'textbox') return 'text';
			if (role === 'combobox') return 'select';
			if (role === 'listbox') return 'select';
			if (el.getAttribute('data-uxi-widget-type') === 'selectinput') return 'select';
			if (el.getAttribute('aria-haspopup') === 'listbox') return 'select';
			if (role === 'radio') return 'radio';
			if (role === 'checkbox') return 'checkbox';
			if (role === 'spinbutton') return 'number';
			if (role === 'slider') return 'range';
			if (role === 'searchbox') return 'search';
			if (role === 'switch') return 'toggle';
			if (el.tagName === 'SELECT') return 'select';
			if (el.tagName === 'TEXTAREA') return 'textarea';
			var t = el.type || '';
			var typeMap = {text:'text', email:'email', tel:'tel', url:'url', number:'number', date:'date', file:'file', checkbox:'checkbox', radio:'radio', search:'search', password:'password'};
			return typeMap[t] || t || 'text';
		})();

		if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'image' || type === 'reset') return;

		var visible = (function() {
			if (type === 'file' && !ff.isVisible(el)) {
				var container = el.closest('[class*=upload], [class*=drop], .form-group, .field');
				return container ? ff.isVisible(container) : false;
			}
			return ff.isVisible(el);
		})();
		if (!visible) return;

		var isNative = el.tagName === 'SELECT';
		var isMultiSelect = type === 'select' && !isNative && !!(
			el.querySelector('[class*="multi"]') ||
			el.classList.toString().includes('multi') ||
			el.getAttribute('aria-multiselectable') === 'true'
		);

		var entry = {
			field_id: id,
			name: ff.getAccessibleName(el),
			field_type: type,
			section: ff.getSection(el),
			required: el.required || el.getAttribute('aria-required') === 'true' || el.dataset.required === 'true' || el.dataset.ffRequired === 'true',
			options: [],
			choices: [],
			accept: el.accept || null,
			is_native: isNative || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA',
			is_multi_select: isMultiSelect || el.multiple || el.getAttribute('aria-multiselectable') === 'true',
			visible: true,
			raw_label: ff.getAccessibleName(el),
			synthetic_label: false,
			field_fingerprint: null,
			current_value: ''
		};

		if (type === 'select') {
			var opts = [];
			if (el.tagName === 'SELECT') {
				opts = Array.from(el.options)
					.filter(function(o) { return o.value !== ''; })
					.map(function(o) { return o.textContent ? o.textContent.trim() : ''; })
					.filter(Boolean);
			} else {
				var ctrlId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
				var src = ctrlId ? ff.getByDomId(ctrlId) : null;
				if (!src && el.tagName === 'INPUT') {
					src = ff.closestCrossRoot(el, '[class*="select"], [class*="combobox"], .form-group, .field');
				}
				if (!src) src = el;
				if (src) {
					opts = Array.from(src.querySelectorAll('[role="option"], [role="menuitem"]'))
						.map(function(o) { return getOptionMainText(o); }).filter(Boolean);
				}
			}
			if (opts.length) entry.options = opts;
		}

		if (type === 'checkbox' || type === 'radio') {
			var labelEl = el.querySelector('[class*="label"], .rc-label');
			if (labelEl) {
				entry.itemLabel = labelEl.textContent ? labelEl.textContent.trim() : '';
			} else {
				var wrap = el.closest('label');
				if (wrap) {
					var c = wrap.cloneNode(true);
					c.querySelectorAll('input, [class*=desc], small').forEach(function(x) { x.remove(); });
					entry.itemLabel = c.textContent ? c.textContent.trim() : '';
				} else {
					entry.itemLabel = el.getAttribute('aria-label') || ff.getAccessibleName(el);
				}
			}
			entry.itemValue = el.value || (el.querySelector('input') ? el.querySelector('input').value : '') || '';
		}

		if (el.tagName === 'SELECT') {
			var selOpt = el.options[el.selectedIndex];
			entry.current_value = selOpt ? selOpt.text.trim() : '';
		} else if (type === 'checkbox' || type === 'radio') {
			if (el.tagName === 'INPUT') entry.current_value = el.checked ? 'checked' : '';
			else entry.current_value = el.getAttribute('aria-checked') === 'true' ? 'checked' : '';
		} else if (el.getAttribute('role') === 'checkbox' || el.getAttribute('role') === 'switch') {
			entry.current_value = el.getAttribute('aria-checked') === 'true' ? 'checked' : '';
		} else {
			entry.current_value = el.value || (el.textContent ? el.textContent.trim() : '') || '';
		}

		out.push(entry);
	});
	return JSON.stringify(out);
}"""


# ── Button group extraction JS ───────────────────────────────────────

_EXTRACT_BUTTON_GROUPS_JS = r"""() => {
	var ff = window.__ff;
	if (!ff) return JSON.stringify([]);
	var results = [];
	var allBtnEls = document.querySelectorAll('button, [role="button"]');
	var parentMap = {};

	var navLabels = new Set(""" + json.dumps(list(_NAV_BUTTON_LABELS)) + r""");

	for (var i = 0; i < allBtnEls.length; i++) {
		var btn = allBtnEls[i];
		if (!ff.isVisible(btn)) continue;
		if (btn.disabled) continue;
		if (btn.closest('nav, header, [role="navigation"], [role="menubar"], [role="menu"], [role="toolbar"]')) continue;
		if (btn.tagName === 'A' || btn.closest('a[href]')) continue;
		if (btn.getAttribute('role') === 'combobox') continue;
		if (btn.getAttribute('aria-haspopup') === 'listbox') continue;
		if (btn.tagName.toLowerCase() === 'input') continue;

		var btnText = (btn.textContent || '').trim();
		if (!btnText || btnText.length > 30) continue;
		if (navLabels.has(btnText.toLowerCase()) || btnText.toLowerCase().startsWith('add ') || btnText.toLowerCase().includes('save & continue')) continue;

		var parent = btn.parentElement;
		for (var pu = 0; pu < 3 && parent; pu++) {
			var childBtns = parent.querySelectorAll('button, [role="button"]');
			var visibleCount = 0;
			for (var vc = 0; vc < childBtns.length; vc++) {
				if (ff.isVisible(childBtns[vc])) visibleCount++;
			}
			if (visibleCount >= 2 && visibleCount <= 4) break;
			parent = parent.parentElement;
		}
		if (!parent) continue;

		var parentKey = parent.getAttribute('data-ff-btn-group') || ('btngrp-' + i);
		parent.setAttribute('data-ff-btn-group', parentKey);

		if (!parentMap[parentKey]) {
			parentMap[parentKey] = { parent: parent, buttons: [] };
		}
		var already = false;
		for (var j = 0; j < parentMap[parentKey].buttons.length; j++) {
			if (parentMap[parentKey].buttons[j].text === btnText) { already = true; break; }
		}
		if (!already) {
			parentMap[parentKey].buttons.push({ text: btnText, ffId: ff.tag(btn) });
		}
	}

	for (var groupKey in parentMap) {
		var group = parentMap[groupKey];
		if (group.buttons.length < 2 || group.buttons.length > 4) continue;
		if (group.buttons.some(function(entry) { return entry.text.length > 30; })) continue;

		var container = group.parent;
		var questionLabel = '';
		var prevSib = container.previousElementSibling;
		if (prevSib) {
			var prevText = (prevSib.textContent || '').trim();
			if (prevText.length > 0 && prevText.length < 200) questionLabel = prevText;
		}
		if (!questionLabel) {
			var parentEl = container.parentElement;
			if (parentEl) {
				var labelEl = parentEl.querySelector('label, .label, h3, h4, legend, [class*="question"]');
				if (labelEl) questionLabel = (labelEl.textContent || '').trim();
			}
		}
		if (!questionLabel) questionLabel = 'Button group choice';

		var ffId = ff.tag(container);
		results.push({
			field_id: ffId,
			name: questionLabel.replace(/\*\s*$/, '').trim(),
			field_type: 'button-group',
			section: ff.getSection(container),
			required: false,
			options: [],
			choices: group.buttons.map(function(b) { return b.text; }),
			accept: null,
			is_native: false,
			is_multi_select: false,
			visible: true,
			raw_label: questionLabel,
			synthetic_label: false,
			field_fingerprint: null,
			current_value: '',
			btn_ids: group.buttons.map(function(b) { return b.ffId; })
		});
	}
	return JSON.stringify(results);
}"""


# ── Single-field JS helpers ──────────────────────────────────────────

_FILL_FIELD_JS = r"""(ffId, value, fieldType) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return JSON.stringify({success: false, error: 'Element not found'});

	try {
		if (fieldType === 'select' && el.tagName === 'SELECT') {
			var lowerValue = value.toLowerCase();
			var matched = false;
			for (var i = 0; i < el.options.length; i++) {
				var opt = el.options[i];
				var optText = (opt.text || '').trim().toLowerCase();
				var optVal = (opt.value || '').toLowerCase();
				if (optText === lowerValue || optVal === lowerValue) {
					el.value = opt.value; matched = true; break;
				}
			}
			if (!matched) {
				for (var j = 0; j < el.options.length; j++) {
					var o = el.options[j];
					var oText = (o.text || '').trim().toLowerCase();
					if (oText.includes(lowerValue) || lowerValue.includes(oText)) {
						el.value = o.value; matched = true; break;
					}
				}
			}
			if (!matched) return JSON.stringify({success: false, error: 'No matching option for: ' + value});
			el.dispatchEvent(new Event('change', {bubbles: true}));
			el.dispatchEvent(new Event('input', {bubbles: true}));
			return JSON.stringify({success: true});
		}

		if (fieldType === 'checkbox' || fieldType === 'radio' || fieldType === 'toggle') {
			var shouldCheck = /^(checked|true|yes|on|1)$/i.test(value);
			if (el.tagName === 'INPUT') {
				if (el.checked !== shouldCheck) el.click();
			} else {
				var current = el.getAttribute('aria-checked') === 'true';
				if (current !== shouldCheck) el.click();
			}
			return JSON.stringify({success: true});
		}

		var proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
		var nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value');
		if (nativeSetter && nativeSetter.set) {
			nativeSetter.set.call(el, value);
		} else {
			el.value = value;
		}
		el.dispatchEvent(new Event('input', {bubbles: true}));
		el.dispatchEvent(new Event('change', {bubbles: true}));
		el.dispatchEvent(new Event('blur', {bubbles: true}));
		return JSON.stringify({success: true});
	} catch (e) {
		return JSON.stringify({success: false, error: e.message});
	}
}"""

_FILL_CONTENTEDITABLE_JS = r"""(ffId, value) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return JSON.stringify({success: false, error: 'Element not found'});
	try {
		el.textContent = value;
		el.dispatchEvent(new Event('input', {bubbles: true}));
		el.dispatchEvent(new Event('change', {bubbles: true}));
		return JSON.stringify({success: true});
	} catch (e) {
		return JSON.stringify({success: false, error: e.message});
	}
}"""

_FILL_DATE_JS = r"""(ffId, value) => {
	var ff = window.__ff || {byId: function(id){ return document.querySelector('[data-ff-id="'+id+'"]'); }};
	var el = ff.byId(ffId);
	if (!el) return JSON.stringify({success: false, error: 'Element not found'});
	try {
		var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
		if (setter && setter.set) setter.set.call(el, value);
		else el.value = value;
		el.dispatchEvent(new Event('input', {bubbles: true}));
		el.dispatchEvent(new Event('change', {bubbles: true}));
		return JSON.stringify({success: true});
	} catch (e) {
		return JSON.stringify({success: false, error: e.message});
	}
}"""

_CLICK_RADIO_OPTION_JS = r"""(ffId, text) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({clicked: false, error: 'Element not found'});
	var group = ff.closestCrossRoot(el, '[role="radiogroup"], [role="group"], .radio-cards, .radio-group') || el;
	var items = group.querySelectorAll('[role="radio"], label.radio-card, .radio-card, input[type="radio"]');
	var lower = text.toLowerCase().trim();
	for (var i = 0; i < items.length; i++) {
		var item = items[i];
		var labelEl = item.querySelector('[class*="label"], .rc-label');
		var itemText = labelEl ? (labelEl.textContent || '').trim() : (item.textContent || '').trim();
		var itemLower = itemText.toLowerCase();
		if (itemLower === lower || itemLower.includes(lower) || lower.includes(itemLower)) {
			item.click(); return JSON.stringify({clicked: true, text: itemText});
		}
	}
	if (items.length > 0) { items[0].click(); return JSON.stringify({clicked: true, text: '(first)'}); }
	return JSON.stringify({clicked: false, error: 'No matching radio option'});
}"""

_CLICK_SINGLE_RADIO_JS = r"""(ffId, text) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({clicked: false});
	var normalize = function(v) { return v.trim().toLowerCase(); };
	var target = normalize(text);
	var ownInput = el.tagName === 'INPUT' ? el : el.querySelector('input[type="radio"]');
	var radios = [];
	if (ownInput && ownInput.name) {
		var root = ownInput.form || document;
		radios = Array.from(root.querySelectorAll('input[type="radio"][name="' + CSS.escape(ownInput.name) + '"]'));
		if (radios.length === 0) radios = ff.queryAll('input[type="radio"][name="' + CSS.escape(ownInput.name) + '"]') || [];
	} else {
		var group = ff.closestCrossRoot(el, '[role="radiogroup"], [role="group"], fieldset, .radio-group') || ff.rootParent(el) || el;
		radios = Array.from(group.querySelectorAll('input[type="radio"], [role="radio"]'));
	}
	if (radios.length === 0) radios = [el];
	var getLabel = function(node) {
		if (node.id) { var byFor = ff.queryOne('label[for="' + CSS.escape(node.id) + '"]'); if (byFor && byFor.textContent && byFor.textContent.trim()) return byFor.textContent.trim(); }
		var ariaLabel = node.getAttribute('aria-label'); if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
		var wrap = ff.closestCrossRoot(node, 'label, [role="radio"], .radio-card, .radio-option');
		return (wrap ? wrap.textContent : node.textContent || '').trim();
	};
	for (var i = 0; i < radios.length; i++) {
		var radio = radios[i];
		var label = normalize(getLabel(radio));
		if (!label) continue;
		if (label === target || label.includes(target) || target.includes(label)) {
			var isChecked = radio.checked || radio.getAttribute('aria-checked') === 'true';
			if (isChecked) return JSON.stringify({clicked: true, alreadyChecked: true});
			radio.click(); return JSON.stringify({clicked: true, alreadyChecked: false});
		}
	}
	return JSON.stringify({clicked: false});
}"""

_CLICK_CHECKBOX_GROUP_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({clicked: false});
	var group = ff.closestCrossRoot(el, '.checkbox-group, [role="group"]') || el;
	var cbs = Array.from(group.querySelectorAll('input[type="checkbox"], [role="checkbox"]'));
	if (cbs.length === 0) return JSON.stringify({clicked: false});
	for (var i = 0; i < cbs.length; i++) {
		if (cbs[i].checked || cbs[i].getAttribute('aria-checked') === 'true') return JSON.stringify({clicked: true, alreadyChecked: true});
	}
	cbs[0].click(); return JSON.stringify({clicked: true, alreadyChecked: false});
}"""

_READ_BINARY_STATE_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify(null);
	if (el.tagName === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) return JSON.stringify(el.checked);
	var ac = el.getAttribute('aria-checked');
	if (ac === 'true') return JSON.stringify(true);
	if (ac === 'false') return JSON.stringify(false);
	return JSON.stringify(null);
}"""

_CLICK_BUTTON_GROUP_JS = r"""(ffId, text) => {
	var ff = window.__ff;
	var container = ff ? ff.byId(ffId) : null;
	if (!container) return JSON.stringify({clicked: false});
	var textLower = text.toLowerCase().trim();
	var btns = container.querySelectorAll('button, [role="button"]');
	for (var i = 0; i < btns.length; i++) { var bt = (btns[i].textContent || '').trim().toLowerCase(); if (bt === textLower) { btns[i].click(); return JSON.stringify({clicked: true}); } }
	for (var j = 0; j < btns.length; j++) { var btt = (btns[j].textContent || '').trim().toLowerCase(); if (btt.includes(textLower) || textLower.includes(btt)) { btns[j].click(); return JSON.stringify({clicked: true}); } }
	return JSON.stringify({clicked: false});
}"""

_IS_SEARCHABLE_DROPDOWN_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify(false);
	return JSON.stringify(
		el.getAttribute('role') === 'combobox' ||
		el.getAttribute('data-uxi-widget-type') === 'selectinput' ||
		el.getAttribute('data-automation-id') === 'searchBox' ||
		(el.getAttribute('autocomplete') === 'off' && el.getAttribute('aria-controls'))
	);
}"""

_CLICK_DROPDOWN_OPTION_JS = r"""(text) => {
	var lowerText = text.toLowerCase();
	var roleEls = document.querySelectorAll('[role="option"], [role="menuitem"], [role="treeitem"], [data-automation-id*="promptOption"], [data-automation-id*="menuItem"]');
	for (var i = 0; i < roleEls.length; i++) {
		var o = roleEls[i]; var rect = o.getBoundingClientRect();
		if (rect.width === 0 || rect.height === 0) continue;
		var t = (o.textContent || '').trim().toLowerCase();
		if (t === lowerText || t.includes(lowerText)) { o.click(); return JSON.stringify({clicked: true, text: (o.textContent || '').trim()}); }
	}
	var allVisible = document.querySelectorAll('div[tabindex], div[data-automation-id], span[data-automation-id], li, a, button, [role="button"]');
	for (var j = 0; j < allVisible.length; j++) {
		var el = allVisible[j]; var r = el.getBoundingClientRect();
		if (r.width === 0 || r.height === 0) continue;
		var directText = '';
		for (var k = 0; k < el.childNodes.length; k++) { var n = el.childNodes[k]; directText += (n.textContent || ''); }
		directText = directText.trim().toLowerCase();
		if (directText && (directText === lowerText || directText.includes(lowerText))) { el.click(); return JSON.stringify({clicked: true, text: directText}); }
	}
	return JSON.stringify({clicked: false});
}"""

_ELEMENT_EXISTS_JS = r"""(ffId, fieldType) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify(false);
	if (ff.isVisible(el)) return JSON.stringify(true);
	if (fieldType === 'file') {
		var container = ff.closestCrossRoot(el, '[class*=upload], [class*=drop], .form-group, .field');
		return JSON.stringify(container ? ff.isVisible(container) : false);
	}
	return JSON.stringify(false);
}"""

_REVEAL_SECTIONS_JS = r"""() => {
	document.querySelectorAll('[data-section], .form-section, .form-step, .step-content, .tab-pane, .accordion-content, .panel-body, [role="tabpanel"]').forEach(function(el) {
		el.style.display = ''; el.classList.add('active'); el.removeAttribute('hidden'); el.setAttribute('aria-hidden', 'false');
	});
	return 'ok';
}"""

_DISMISS_DROPDOWN_JS = r"""() => {
	var active = document.activeElement;
	if (active) active.blur();
	document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
	document.body.dispatchEvent(new MouseEvent('click', {bubbles: true}));
	return 'ok';
}"""

_FOCUS_AND_CLEAR_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return JSON.stringify({ok: false, error: 'not found'});
	el.focus();
	if (el.select) el.select();
	return JSON.stringify({ok: true});
}"""


# ── Profile evidence extraction ──────────────────────────────────────

def _parse_profile_evidence(profile_text: str) -> dict[str, str | None]:
	"""Extract structured fields from profile text for direct field matching."""
	def read_line(label: str) -> str | None:
		m = re.search(rf'^\s*{re.escape(label)}:\s*(.+)$', profile_text, re.MULTILINE | re.IGNORECASE)
		val = m.group(1).strip() if m else None
		return val if val else None

	name = read_line('Name')
	first_name = name.split()[0] if name else None
	last_name = ' '.join(name.split()[1:]) if name and len(name.split()) > 1 else None

	location = read_line('Location')
	city: str | None = None
	state: str | None = None
	zip_code: str | None = None
	if location:
		parts = [p.strip() for p in location.split(',') if p.strip()]
		if len(parts) >= 2:
			city = parts[0]
			state_zip = parts[1].split()
			state = state_zip[0] if state_zip else None
			zip_code = state_zip[1] if len(state_zip) > 1 else None

	linkedin = read_line('LinkedIn')
	portfolio = read_line('Portfolio') or read_line('Website')
	github_match = re.search(r'https?://(?:www\.)?github\.com/[^\s)]+', profile_text, re.IGNORECASE)
	twitter_match = re.search(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s)]+', profile_text, re.IGNORECASE)

	return {
		'first_name': first_name, 'last_name': last_name,
		'email': read_line('Email'), 'phone': read_line('Phone'),
		'city': city, 'state': state, 'zip': zip_code,
		'phone_device_type': 'Mobile', 'phone_country_code': '+1',
		'linkedin': linkedin, 'portfolio': portfolio,
		'github': github_match.group(0) if github_match else None,
		'twitter': twitter_match.group(0) if twitter_match else None,
	}


def _known_profile_value(field_name: str, evidence: dict[str, str | None]) -> str | None:
	"""Return a profile value if the field name matches a known personal field."""
	name = normalize_name(field_name)
	if not name:
		return None
	if 'first name' in name and evidence.get('first_name'):
		return evidence['first_name']
	if 'last name' in name and evidence.get('last_name'):
		return evidence['last_name']
	if 'full name' in name:
		first = evidence.get('first_name', '')
		last = evidence.get('last_name', '')
		if first or last:
			return f'{first} {last}'.strip()
	if 'email' in name and evidence.get('email'):
		return evidence['email']
	if 'phone extension' in name:
		return None
	if any(kw in name for kw in ('phone device', 'phone type')):
		return evidence.get('phone_device_type')
	if any(kw in name for kw in ('country phone code', 'phone country code', 'country code')):
		return evidence.get('phone_country_code')
	if any(kw in name for kw in ('phone', 'mobile', 'telephone')) and evidence.get('phone'):
		return evidence['phone']
	if name == 'city' or ' city' in name:
		return evidence.get('city')
	if name == 'state' or 'state/province' in name or 'province' in name:
		return evidence.get('state')
	if 'postal' in name or 'zip' in name:
		return evidence.get('zip')
	if 'linkedin' in name:
		return evidence.get('linkedin')
	if 'github' in name:
		return evidence.get('github')
	if any(kw in name for kw in ('portfolio', 'website', 'personal site', 'blog')):
		return evidence.get('portfolio')
	if 'twitter' in name or 'x handle' in name:
		return evidence.get('twitter')
	return None


def _default_value(field: FormField) -> str:
	"""Fallback default values for fields that the LLM didn't answer."""
	match field.field_type:
		case 'number':
			return '1'
		case 'date':
			return '2025-01-01'
		case _:
			return ''


def _is_explicit_false(val: str | None) -> bool:
	"""Return True if the value explicitly indicates unchecked/off/no."""
	if not val:
		return False
	return bool(re.match(r'^(unchecked|false|no|off|0)$', val.strip(), re.IGNORECASE))


# ── LLM answer generation ───────────────────────────────────────────

def _sanitize_no_guess_answer(
	field_name: str, required: bool, answer: str | None, evidence: dict[str, str | None],
) -> str:
	"""Prevent fabrication of sensitive identity fields not in profile."""
	proposed = (answer or '').strip()
	known = _known_profile_value(field_name, evidence)
	if known:
		return known
	if not _SOCIAL_OR_ID_NO_GUESS_RE.search(field_name or ''):
		return proposed
	if not proposed:
		return 'N/A' if required else ''
	if is_placeholder_value(proposed) or re.match(r'^(n/a|na|none|unknown|not applicable|prefer not|decline)', proposed, re.IGNORECASE):
		return proposed if required else ''
	return 'N/A' if required else ''


async def _generate_answers(
	fields: list[FormField], profile_text: str,
) -> tuple[dict[str, str], int, int]:
	"""Call Haiku to generate answers for all fields in a single batch."""
	try:
		import anthropic
	except ImportError:
		logger.error('anthropic package not installed — cannot generate answers')
		return {}, 0, 0

	api_key = os.environ.get('GH_ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_API_KEY', '')
	if not api_key:
		logger.error('No Anthropic API key found (GH_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY)')
		return {}, 0, 0

	client = anthropic.AsyncAnthropic(api_key=api_key)
	evidence = _parse_profile_evidence(profile_text)

	name_counts: dict[str, int] = {}
	disambiguated_names: list[str] = []
	for i, field in enumerate(fields):
		base_name = (field.name or '').strip() or f'Field {i + 1}'
		norm = normalize_name(base_name) or f'field-{i + 1}'
		count = name_counts.get(norm, 0) + 1
		name_counts[norm] = count
		disambiguated_names.append(f'{base_name} #{count}' if count > 1 else base_name)

	field_descriptions = '\n'.join(
		_build_field_description(field, disambiguated_names[i])
		for i, field in enumerate(fields)
	)

	today = date.today().isoformat()
	prompt = f"""You are filling out a job application form on behalf of an applicant. Today's date is {today}.

Here is their profile:

{profile_text}

Here are the form fields to fill:

{field_descriptions}

Rules:
- For each field, decide what value to put based on the profile.
- Fields marked with * are REQUIRED. NEVER return "" for required fields. Use profile-backed values first; for non-identity fields you may use careful contextual inference.
- For optional fields, still fill them in if the profile has any relevant info. Only return "" for optional fields where there is truly nothing relevant to put.
- NEVER fabricate personal identifiers or social handles/URLs not explicitly in the profile. If missing: return "" for optional, "N/A" for required.
- For dropdowns/radio groups with listed options, pick the EXACT text of one of the available options.
- For hierarchical dropdown options (format "Category > SubOption"), pick the EXACT full path including the " > " separator.
- For dropdowns WITHOUT listed options, provide your best guess for the value.
- For skill typeahead fields, return an ARRAY of relevant skills from the applicant profile.
- For multi-select fields, return a JSON array of ALL matching options (e.g., ["Python", "Java"]).
- For checkboxes/toggles, respond with "checked" or "unchecked".
- For file upload fields, skip them (don't include in output).
- For textarea fields, write 2-4 thoughtful sentences using the applicant's real background. NEVER return a single letter or placeholder.
- For demographic/EEO fields, use the applicant's actual info. If no info, choose the most neutral "decline" option.
- NEVER select a default placeholder value like "Select One", "Please select", etc.
- For salary fields, provide a realistic number based on role and experience level.
- Use the EXACT field names shown above (including any "#N" suffix) as JSON keys.
- Respond with ONLY a valid JSON object. No explanation, no markdown fences.

Example: {{"First Name": "Alex", "Cover Letter": "I am excited to apply because..."}}"""

	try:
		response = await client.messages.create(
			model='claude-haiku-4-5-20251001',
			max_tokens=4096,
			messages=[{'role': 'user', 'content': prompt}],
		)
		text = response.content[0].text if response.content and response.content[0].type == 'text' else ''
		input_tokens = response.usage.input_tokens if response.usage else 0
		output_tokens = response.usage.output_tokens if response.usage else 0
		if response.stop_reason == 'max_tokens':
			logger.warning('LLM response was truncated (hit max_tokens).')
		logger.info(f'LLM answer response: {text[:200]}{"..." if len(text) > 200 else ""}')

		cleaned = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.MULTILINE)
		cleaned = re.sub(r'\n?```\s*$', '', cleaned, flags=re.MULTILINE).strip()
		parsed: dict[str, Any] = json.loads(cleaned)

		for k, v in list(parsed.items()):
			if isinstance(v, list):
				parsed[k] = ','.join(str(item) for item in v)
			elif isinstance(v, (int, float)):
				parsed[k] = str(v)

		_replace_placeholder_answers(parsed, fields, disambiguated_names)

		for i, field in enumerate(fields):
			key = disambiguated_names[i]
			if key in parsed and isinstance(parsed[key], str):
				parsed[key] = _sanitize_no_guess_answer(field.name, field.required, parsed[key], evidence)

		return parsed, input_tokens, output_tokens
	except json.JSONDecodeError:
		logger.warning('Failed to parse LLM response as JSON, using empty answers')
		return {}, 0, 0
	except Exception as e:
		logger.error(f'LLM answer generation failed: {e}')
		return {}, 0, 0


def _build_field_description(field: FormField, display_name: str) -> str:
	type_label = 'multi-select' if field.is_multi_select else field.field_type
	req_marker = ' *' if field.required else ''
	desc = f'- "{display_name}"{req_marker} (type: {type_label})'
	if field.options:
		desc += f' options: [{", ".join(field.options[:50])}]'
	if field.choices:
		desc += f' choices: [{", ".join(field.choices[:30])}]'
	if field.section:
		desc += f' [section: {field.section}]'
	return desc


def _replace_placeholder_answers(
	parsed: dict[str, Any], fields: list[FormField], disambiguated_names: list[str],
) -> None:
	placeholder_re = re.compile(
		r'^(select one|choose one|please select|-- ?select ?--|— ?select ?—|\(select\)|select\.{0,3})$', re.IGNORECASE,
	)
	decline_patterns = [
		re.compile(p, re.IGNORECASE)
		for p in [r'not declared', r'prefer not', r'decline', r'do not wish', r'choose not', r'rather not', r'not specified', r'not applicable', r'n/?a']
	]
	for key, val in list(parsed.items()):
		if not isinstance(val, str) or not placeholder_re.match(val.strip()):
			continue
		idx = disambiguated_names.index(key) if key in disambiguated_names else -1
		field = fields[idx] if idx >= 0 else None
		if field and not field.required:
			parsed[key] = ''
			continue
		options = (field.options or field.choices or []) if field else []
		neutral = next((o for o in options if any(p.search(o) for p in decline_patterns)), None)
		if neutral:
			logger.info(f'Replaced placeholder "{val}" -> "{neutral}" for field "{key}"')
			parsed[key] = neutral
		elif options:
			non_placeholder = [o for o in options if not placeholder_re.match(o.strip())]
			if non_placeholder:
				parsed[key] = non_placeholder[-1]


# ── Field-answer matching ────────────────────────────────────────────

_AUTHORITATIVE_SELECT_KEYS: dict[str, list[str]] = {
	'country phone code': ['Country Phone Code', 'Phone Country Code'],
	'phone country code': ['Phone Country Code', 'Country Phone Code'],
	'phone device type': ['Phone Device Type', 'Phone Type'],
	'phone type': ['Phone Type', 'Phone Device Type'],
}
_AUTHORITATIVE_SELECT_DEFAULTS: dict[str, str] = {
	'country phone code': '+1', 'phone country code': '+1',
	'phone device type': 'Mobile', 'phone type': 'Mobile',
}


def _match_answer(
	field: FormField, answers: dict[str, str], evidence: dict[str, str | None],
) -> str | None:
	norm_name = normalize_name(field.name)
	if field.field_type == 'select' and norm_name in _AUTHORITATIVE_SELECT_KEYS:
		for ck in _AUTHORITATIVE_SELECT_KEYS[norm_name]:
			if ck in answers:
				return answers[ck]
			for key, val in answers.items():
				if normalize_name(key) == normalize_name(ck):
					return val
		if norm_name in _AUTHORITATIVE_SELECT_DEFAULTS:
			return _AUTHORITATIVE_SELECT_DEFAULTS[norm_name]

	profile_val = _known_profile_value(field.name, evidence)
	if profile_val:
		return profile_val

	field_norm = normalize_name(field.name)
	if not field_norm:
		return None

	for key, val in answers.items():
		if normalize_name(key) == field_norm:
			return val
	for key, val in answers.items():
		if field_norm in normalize_name(key):
			return val
	for key, val in answers.items():
		key_norm = normalize_name(key)
		if key_norm and key_norm in field_norm:
			return val

	stop_words = {'the', 'a', 'an', 'of', 'for', 'in', 'to', 'and', 'or', 'your', 'my'}
	field_words = set(field_norm.split()) - stop_words
	if len(field_words) >= 2:
		best_overlap = 0
		best_val: str | None = None
		for key, val in answers.items():
			key_words = set(normalize_name(key).split()) - stop_words
			overlap = len(field_words & key_words)
			if overlap >= 2 and overlap > best_overlap:
				best_overlap = overlap
				best_val = val
		if best_val is not None:
			return best_val

	def stem(word: str) -> str:
		for suffix in ('tion', 'sion', 'ment', 'ness', 'ing', 'ity', 'ous', 'ive', 'ful', 'ly', 'ed', 'er', 'es', 's'):
			if word.endswith(suffix) and len(word) > len(suffix) + 2:
				return word[:-len(suffix)]
		return word

	field_stems = {stem(w) for w in field_words}
	for key, val in answers.items():
		key_words = set(normalize_name(key).split()) - stop_words
		key_stems = {stem(w) for w in key_words}
		if len(field_stems & key_stems) >= 2:
			return val

	return None


def _is_skill_like(field_name: str) -> bool:
	n = normalize_name(field_name)
	return bool(re.search(r'\bskills?\b', n) or re.search(r'\btechnolog(y|ies)\b', n))


def _is_navigation_field(field: FormField) -> bool:
	if field.field_type != 'button-group':
		return False
	choices_lower = [c.lower() for c in (field.choices or [])]
	nav_keywords = {'next', 'continue', 'back', 'previous', 'save', 'cancel', 'submit'}
	return any(c in nav_keywords for c in choices_lower)


# ── Core action function ─────────────────────────────────────────────

async def domhand_fill(params: DomHandFillParams, browser_session: BrowserSession) -> ActionResult:
	"""Fill all visible form fields using fast DOM manipulation."""
	page = await browser_session.get_current_page()
	if not page:
		return ActionResult(error='No active page found in browser session')

	profile_text = _get_profile_text()
	if not profile_text:
		return ActionResult(error='No user profile text found. Set GH_USER_PROFILE_TEXT or GH_USER_PROFILE_PATH env var.')

	evidence = _parse_profile_evidence(profile_text)
	all_results: list[FillFieldResult] = []
	total_input_tokens = 0
	total_output_tokens = 0
	llm_calls = 0
	fields_seen: set[str] = set()

	for round_num in range(1, MAX_FILL_ROUNDS + 1):
		logger.info(f'DomHand fill round {round_num}/{MAX_FILL_ROUNDS}')

		try:
			await page.evaluate(_build_inject_helpers_js())
		except Exception as e:
			logger.warning(f'Helper injection failed (round {round_num}): {e}')

		if round_num == 1:
			try:
				await page.evaluate(_REVEAL_SECTIONS_JS)
			except Exception:
				pass

		try:
			raw_json = await page.evaluate(_EXTRACT_FIELDS_JS)
			raw_fields: list[dict[str, Any]] = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
		except Exception as e:
			logger.error(f'Field extraction failed: {e}')
			return ActionResult(error=f'Failed to extract form fields: {e}')

		fields: list[FormField] = []
		grouped_names: set[str] = set()
		seen_ids: set[str] = set()

		for f_data in raw_fields:
			fid = f_data.get('field_id', '')
			if not fid or fid in seen_ids:
				continue
			ftype = f_data.get('field_type', 'text')
			fname = f_data.get('name', '')

			if ftype in ('checkbox', 'radio'):
				group_key = f'group:{fname}'
				if group_key in grouped_names:
					continue
				seen_ids.add(fid)
				siblings = [
					r for r in raw_fields
					if r.get('field_type') in ('checkbox', 'radio')
					and r.get('name') == fname
					and r.get('section', '') == f_data.get('section', '')
				]
				if len(siblings) > 1:
					grouped_names.add(group_key)
					for s in siblings:
						seen_ids.add(s.get('field_id', ''))
					fields.append(FormField(
						field_id=fid, name=fname, field_type=f'{ftype}-group',
						section=f_data.get('section', ''), required=f_data.get('required', False),
						options=[], choices=[s.get('itemLabel', s.get('name', '')) for s in siblings],
						is_native=False, visible=True, raw_label=f_data.get('raw_label'),
					))
				else:
					fields.append(FormField(
						field_id=fid, name=f_data.get('itemLabel', fname) or fname,
						field_type=ftype, section=f_data.get('section', ''),
						required=f_data.get('required', False), is_native=False, visible=True,
						raw_label=f_data.get('raw_label'),
					))
			else:
				seen_ids.add(fid)
				fields.append(FormField.model_validate(f_data))

		try:
			btn_json = await page.evaluate(_EXTRACT_BUTTON_GROUPS_JS)
			btn_groups: list[dict[str, Any]] = json.loads(btn_json) if isinstance(btn_json, str) else btn_json
			for bg in btn_groups:
				bg_id = bg.get('field_id', '')
				if bg_id and bg_id not in seen_ids:
					seen_ids.add(bg_id)
					fields.append(FormField.model_validate(bg))
		except Exception as e:
			logger.debug(f'Button group extraction failed: {e}')

		if params.target_section:
			section_norm = normalize_name(params.target_section)
			fields = [
				f for f in fields
				if normalize_name(f.section) == section_norm
				or section_norm in normalize_name(f.section)
				or normalize_name(f.section) in section_norm
			]

		fillable_fields: list[FormField] = []
		for f in fields:
			if f.field_type == 'file':
				continue
			key = get_stable_field_key(f)
			if key in fields_seen and f.current_value and not is_placeholder_value(f.current_value):
				continue
			if _is_navigation_field(f):
				continue
			fillable_fields.append(f)

		if not fillable_fields:
			if round_num == 1:
				return ActionResult(
					extracted_content='No fillable form fields found on the page.',
					include_extracted_content_only_once=True,
				)
			break

		logger.info(f'Round {round_num}: {len(fillable_fields)} fillable fields found')

		needs_llm: list[FormField] = []
		direct_fills: dict[str, str] = {}
		for f in fillable_fields:
			if f.current_value and not is_placeholder_value(f.current_value):
				fields_seen.add(get_stable_field_key(f))
				continue
			profile_val = _known_profile_value(f.name, evidence)
			if profile_val:
				direct_fills[f.field_id] = profile_val
			else:
				needs_llm.append(f)

		answers: dict[str, str] = {}
		if needs_llm:
			llm_answers, in_tok, out_tok = await _generate_answers(needs_llm, profile_text)
			answers = llm_answers
			total_input_tokens += in_tok
			total_output_tokens += out_tok
			llm_calls += 1

		round_filled = 0
		round_failed = 0

		for f in fillable_fields:
			if f.field_id in direct_fills:
				value = direct_fills[f.field_id]
				success = await _fill_single_field(page, f, value)
				all_results.append(FillFieldResult(
					field_id=f.field_id, name=f.name, success=success, actor='dom',
					value_set=value if success else None, error=None if success else 'DOM fill failed',
				))
				fields_seen.add(get_stable_field_key(f))
				round_filled += 1 if success else 0
				round_failed += 0 if success else 1

		for f in needs_llm:
			matched_answer = _match_answer(f, answers, evidence)
			if not matched_answer:
				matched_answer = _default_value(f)
			if not matched_answer:
				if f.required:
					all_results.append(FillFieldResult(
						field_id=f.field_id, name=f.name, success=False,
						actor='unfilled', error='No answer generated for required field',
					))
					round_failed += 1
				fields_seen.add(get_stable_field_key(f))
				continue
			success = await _fill_single_field(page, f, matched_answer)
			all_results.append(FillFieldResult(
				field_id=f.field_id, name=f.name, success=success, actor='dom',
				value_set=matched_answer if success else None, error=None if success else 'DOM fill failed',
			))
			fields_seen.add(get_stable_field_key(f))
			round_filled += 1 if success else 0
			round_failed += 0 if success else 1

		logger.info(f'Round {round_num}: filled={round_filled}, failed={round_failed}')
		if round_filled == 0:
			break
		await asyncio.sleep(0.5)

	filled_count = sum(1 for r in all_results if r.success)
	failed_count = sum(1 for r in all_results if not r.success and r.actor == 'dom')
	unfilled_count = sum(1 for r in all_results if r.actor == 'unfilled')
	unfilled_descriptions = [
		f'  - "{r.name}" ({r.error or "no answer"})' for r in all_results if not r.success
	]
	summary_lines = [
		f'DomHand fill complete: {filled_count} filled, {failed_count} DOM failures, {unfilled_count} unfilled.',
		f'LLM calls: {llm_calls} (input: {total_input_tokens} tokens, output: {total_output_tokens} tokens)',
	]
	if unfilled_descriptions:
		summary_lines.append('Unfilled/failed fields:')
		summary_lines.extend(unfilled_descriptions[:20])
		if len(unfilled_descriptions) > 20:
			summary_lines.append(f'  ... and {len(unfilled_descriptions) - 20} more')

	summary = '\n'.join(summary_lines)
	logger.info(summary)
	return ActionResult(extracted_content=summary, include_extracted_content_only_once=False)


# ── Per-field fill dispatch ──────────────────────────────────────────

async def _fill_single_field(page: Any, field: FormField, value: str) -> bool:
	ff_id = field.field_id
	tag = f'[{field.name or field.field_type}]'

	try:
		exists_json = await page.evaluate(_ELEMENT_EXISTS_JS, ff_id, field.field_type)
		if not json.loads(exists_json):
			logger.debug(f'skip {tag} (not visible)')
			return False
	except Exception:
		pass

	match field.field_type:
		case 'text' | 'email' | 'tel' | 'url' | 'number' | 'password' | 'search':
			return await _fill_text_field(page, field, value, tag)
		case 'date':
			return await _fill_date_field(page, field, value, tag)
		case 'textarea':
			return await _fill_textarea_field(page, field, value, tag)
		case 'select':
			return await _fill_select_field(page, field, value, tag)
		case 'radio-group':
			return await _fill_radio_group(page, field, value, tag)
		case 'radio':
			return await _fill_single_radio(page, field, value, tag)
		case 'button-group':
			return await _fill_button_group(page, field, value, tag)
		case 'checkbox-group':
			return await _fill_checkbox_group(page, field, value, tag)
		case 'checkbox':
			return await _fill_checkbox(page, field, value, tag)
		case 'toggle':
			return await _fill_toggle(page, field, value, tag)
		case _:
			return await _fill_text_field(page, field, value, tag)


async def _fill_text_field(page: Any, field: FormField, value: str, tag: str) -> bool:
	ff_id = field.field_id
	try:
		is_search_json = await page.evaluate(_IS_SEARCHABLE_DROPDOWN_JS, ff_id)
		if json.loads(is_search_json):
			return await _fill_searchable_dropdown(page, field, value, tag)
	except Exception:
		pass

	if not value:
		logger.debug(f'skip {tag} (no value)')
		return False

	try:
		result_json = await page.evaluate(_FILL_FIELD_JS, ff_id, value, field.field_type)
		result = json.loads(result_json) if isinstance(result_json, str) else result_json
		if isinstance(result, dict) and result.get('success'):
			logger.debug(f'fill {tag} = "{value[:80]}{"..." if len(value) > 80 else ""}"')
			return True
	except Exception:
		pass

	try:
		await page.evaluate(_FOCUS_AND_CLEAR_JS, ff_id)
		await asyncio.sleep(0.1)
		await page.press('Home')
		await page.press('Shift+End')
		for char in value:
			await page.press(char)
		await page.press('Tab')
		logger.debug(f'fill {tag} = "{value[:80]}..." (keyboard)')
		return True
	except Exception:
		logger.debug(f'skip {tag} (not fillable)')
		return False


async def _fill_searchable_dropdown(page: Any, field: FormField, value: str, tag: str) -> bool:
	ff_id = field.field_id
	if not value:
		logger.debug(f'skip {tag} (searchable dropdown, no answer)')
		return False
	try:
		await page.evaluate(r"""(ffId) => {
			var el = window.__ff ? window.__ff.byId(ffId) : null;
			if (el) el.click();
			return 'ok';
		}""", ff_id)
		await asyncio.sleep(0.4)

		await page.evaluate(_FOCUS_AND_CLEAR_JS, ff_id)
		await asyncio.sleep(0.1)
		await page.evaluate(_FILL_FIELD_JS, ff_id, value, 'text')
		await page.evaluate(r"""(ffId) => {
			var el = window.__ff ? window.__ff.byId(ffId) : null;
			if (el) { el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('keyup', {bubbles: true})); }
			return 'ok';
		}""", ff_id)
		await asyncio.sleep(1.5)

		clicked_json = await page.evaluate(_CLICK_DROPDOWN_OPTION_JS, value)
		clicked = json.loads(clicked_json)
		if clicked.get('clicked'):
			logger.debug(f'search-select {tag} -> "{clicked.get("text", value)}"')
			await asyncio.sleep(0.3)
			return True

		await page.press('ArrowDown')
		await asyncio.sleep(0.2)
		await page.press('Enter')
		logger.debug(f'search-select {tag} -> first result (keyboard)')
		await asyncio.sleep(0.3)
		return True
	except Exception as e:
		logger.debug(f'skip {tag} (searchable dropdown failed: {str(e)[:60]})')
		return False


async def _fill_date_field(page: Any, field: FormField, value: str, tag: str) -> bool:
	val = value or '2025-01-01'
	try:
		result_json = await page.evaluate(_FILL_FIELD_JS, field.field_id, val, 'text')
		result = json.loads(result_json) if isinstance(result_json, str) else result_json
		if isinstance(result, dict) and result.get('success'):
			logger.debug(f'fill {tag} = "{val}"')
			return True
	except Exception:
		pass
	try:
		result_json = await page.evaluate(_FILL_DATE_JS, field.field_id, val)
		result = json.loads(result_json) if isinstance(result_json, str) else result_json
		if isinstance(result, dict) and result.get('success'):
			logger.debug(f'fill {tag} = "{val}" (direct)')
			return True
	except Exception:
		pass
	logger.debug(f'skip {tag} (date not fillable)')
	return False


async def _fill_textarea_field(page: Any, field: FormField, value: str, tag: str) -> bool:
	if not value:
		logger.debug(f'skip {tag} (no value)')
		return False
	try:
		result_json = await page.evaluate(_FILL_FIELD_JS, field.field_id, value, 'textarea')
		result = json.loads(result_json) if isinstance(result_json, str) else result_json
		if isinstance(result, dict) and result.get('success'):
			logger.debug(f'fill {tag} = "{value[:80]}{"..." if len(value) > 80 else ""}"')
			return True
	except Exception:
		pass
	try:
		result_json = await page.evaluate(_FILL_CONTENTEDITABLE_JS, field.field_id, value)
		result = json.loads(result_json) if isinstance(result_json, str) else result_json
		if isinstance(result, dict) and result.get('success'):
			logger.debug(f'fill {tag} = "{value[:80]}..." (contenteditable)')
			return True
	except Exception:
		pass
	logger.debug(f'skip {tag} (textarea not fillable)')
	return False


async def _fill_select_field(page: Any, field: FormField, value: str, tag: str) -> bool:
	if not value:
		logger.debug(f'skip {tag} (no value)')
		return False
	if field.is_native:
		try:
			result_json = await page.evaluate(_FILL_FIELD_JS, field.field_id, value, 'select')
			result = json.loads(result_json) if isinstance(result_json, str) else result_json
			if isinstance(result, dict) and result.get('success'):
				logger.debug(f'select {tag} -> "{value}"')
				return True
		except Exception:
			pass
		logger.debug(f'skip {tag} (native select failed)')
		return False

	is_skill = _is_skill_like(field.name)
	all_values = [v.strip() for v in value.split(',') if v.strip()]
	values = all_values[:3] if is_skill else all_values
	if len(values) > 1 or is_skill:
		return await _fill_multi_select(page, field, values, tag)
	return await _fill_custom_dropdown(page, field, value, tag)


async def _fill_multi_select(page: Any, field: FormField, values: list[str], tag: str) -> bool:
	ff_id = field.field_id
	try:
		await page.evaluate(r"""(ffId) => {
			var ff = window.__ff; var el = ff ? ff.byId(ffId) : null;
			if (el) el.click(); return 'ok';
		}""", ff_id)
		await asyncio.sleep(0.6)

		picked_count = 0
		for val in values:
			await page.evaluate(_FILL_FIELD_JS, ff_id, val, 'text')
			await asyncio.sleep(0.3)
			try:
				clicked_json = await page.evaluate(_CLICK_DROPDOWN_OPTION_JS, val)
				clicked = json.loads(clicked_json)
				if clicked.get('clicked'):
					picked_count += 1
					await asyncio.sleep(0.2)
					continue
			except Exception:
				pass
			await page.press('Enter')
			await asyncio.sleep(0.3)
			picked_count += 1

		try:
			await page.evaluate(_DISMISS_DROPDOWN_JS)
		except Exception:
			pass
		if picked_count > 0:
			logger.debug(f'multi-select {tag} -> {picked_count}/{len(values)} options')
			return True
	except Exception as e:
		logger.debug(f'multi-select {tag} failed: {str(e)[:60]}')
	return False


async def _fill_custom_dropdown(page: Any, field: FormField, value: str, tag: str) -> bool:
	ff_id = field.field_id
	try:
		await page.evaluate(r"""(ffId) => {
			var ff = window.__ff; var el = ff ? ff.byId(ffId) : null;
			if (el) el.click(); return 'ok';
		}""", ff_id)
		await asyncio.sleep(0.6)

		clicked_json = await page.evaluate(_CLICK_DROPDOWN_OPTION_JS, value)
		clicked = json.loads(clicked_json)
		if clicked.get('clicked'):
			logger.debug(f'select {tag} -> "{clicked.get("text", value)}"')
			await asyncio.sleep(0.3)
			return True

		words = value.split()
		if len(words) > 1:
			clicked_json = await page.evaluate(_CLICK_DROPDOWN_OPTION_JS, words[0])
			clicked = json.loads(clicked_json)
			if clicked.get('clicked'):
				logger.debug(f'select {tag} -> "{clicked.get("text", words[0])}" (fuzzy)')
				await asyncio.sleep(0.3)
				return True

		await page.press('ArrowDown')
		await asyncio.sleep(0.2)
		await page.press('Enter')
		logger.debug(f'select {tag} -> first option (keyboard)')
		await asyncio.sleep(0.3)
		try:
			await page.evaluate(_DISMISS_DROPDOWN_JS)
		except Exception:
			pass
		return True
	except Exception as e:
		try:
			await page.evaluate(_DISMISS_DROPDOWN_JS)
		except Exception:
			pass
		logger.debug(f'skip {tag} (custom dropdown failed: {str(e)[:60]})')
		return False


async def _fill_radio_group(page: Any, field: FormField, value: str, tag: str) -> bool:
	choice = value or (field.choices[0] if field.choices else '')
	if not choice:
		logger.debug(f'skip {tag} (radio-group, no answer)')
		return False
	try:
		result_json = await page.evaluate(_CLICK_RADIO_OPTION_JS, field.field_id, choice)
		result = json.loads(result_json)
		if result.get('clicked'):
			logger.debug(f'radio {tag} -> "{choice}"')
			return True
	except Exception:
		pass
	logger.debug(f'skip {tag} (no matching radio option)')
	return False


async def _fill_single_radio(page: Any, field: FormField, value: str, tag: str) -> bool:
	if not value:
		logger.debug(f'skip {tag} (radio, no answer)')
		return False
	try:
		result_json = await page.evaluate(_CLICK_SINGLE_RADIO_JS, field.field_id, value)
		result = json.loads(result_json)
		if result.get('clicked'):
			if result.get('alreadyChecked'):
				logger.debug(f'skip {tag} (already selected)')
			else:
				logger.debug(f'radio {tag} -> "{value}"')
			return True
	except Exception:
		pass
	logger.debug(f'skip {tag} (no matching radio for "{value}")')
	return False


async def _fill_button_group(page: Any, field: FormField, value: str, tag: str) -> bool:
	choice = value or (field.choices[0] if field.choices else '')
	if not choice:
		logger.debug(f'skip {tag} (button-group, no answer)')
		return False
	try:
		result_json = await page.evaluate(_CLICK_BUTTON_GROUP_JS, field.field_id, choice)
		result = json.loads(result_json)
		if result.get('clicked'):
			logger.debug(f'button-group {tag} -> "{choice}"')
			return True
	except Exception:
		pass
	logger.debug(f'skip {tag} (button-group, no matching button)')
	return False


async def _fill_checkbox_group(page: Any, field: FormField, value: str, tag: str) -> bool:
	if _is_explicit_false(value):
		logger.debug(f'check {tag} -> skip (answer=unchecked)')
		return True
	try:
		result_json = await page.evaluate(_CLICK_CHECKBOX_GROUP_JS, field.field_id)
		result = json.loads(result_json)
		if result.get('clicked'):
			if result.get('alreadyChecked'):
				logger.debug(f'skip {tag} (already checked)')
			else:
				logger.debug(f'check {tag} -> first')
			return True
	except Exception:
		pass
	logger.debug(f'skip {tag} (checkbox-group)')
	return False


async def _fill_checkbox(page: Any, field: FormField, value: str, tag: str) -> bool:
	desired_checked = not _is_explicit_false(value)
	if not desired_checked:
		logger.debug(f'check {tag} -> skip (answer=unchecked)')
		return True
	try:
		state_json = await page.evaluate(_READ_BINARY_STATE_JS, field.field_id)
		state = json.loads(state_json)
		if state is True:
			logger.debug(f'skip {tag} (already checked)')
			return True
	except Exception:
		pass
	try:
		await page.evaluate(r"""(ffId) => {
			var ff = window.__ff; var el = ff ? ff.byId(ffId) : null;
			if (!el) return 'not found';
			var label = ff.closestCrossRoot(el, 'label') || el;
			label.click(); return 'ok';
		}""", field.field_id)
		await asyncio.sleep(0.2)
		state_json = await page.evaluate(_READ_BINARY_STATE_JS, field.field_id)
		state = json.loads(state_json)
		if state is True or state is None:
			logger.debug(f'check {tag}')
			return True
	except Exception:
		pass
	logger.debug(f'skip {tag} (did not remain checked)')
	return False


async def _fill_toggle(page: Any, field: FormField, value: str, tag: str) -> bool:
	desired_on = not _is_explicit_false(value)
	if not desired_on:
		logger.debug(f'toggle {tag} -> skip (answer=off)')
		return True
	try:
		state_json = await page.evaluate(_READ_BINARY_STATE_JS, field.field_id)
		state = json.loads(state_json)
		if state is True:
			logger.debug(f'skip {tag} (already on)')
			return True
	except Exception:
		pass
	try:
		await page.evaluate(r"""(ffId) => {
			var el = window.__ff ? window.__ff.byId(ffId) : null;
			if (el) el.click(); return 'ok';
		}""", field.field_id)
		await asyncio.sleep(0.2)
		state_json = await page.evaluate(_READ_BINARY_STATE_JS, field.field_id)
		state = json.loads(state_json)
		if state is True or state is None:
			logger.debug(f'toggle {tag} -> on')
			return True
	except Exception:
		pass
	logger.debug(f'skip {tag} (did not remain on)')
	return False


def _get_profile_text() -> str | None:
	text = os.environ.get('GH_USER_PROFILE_TEXT', '')
	if text.strip():
		return text.strip()
	path = os.environ.get('GH_USER_PROFILE_PATH', '')
	if path:
		try:
			import pathlib
			p = pathlib.Path(path)
			if p.is_file():
				return p.read_text(encoding='utf-8').strip()
		except Exception as e:
			logger.warning(f'Failed to read profile from {path}: {e}')
	return None
