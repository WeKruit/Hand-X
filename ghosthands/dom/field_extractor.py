"""DOM field extraction — the heart of DomHand.

Runs a large ``page.evaluate()`` JavaScript function that traverses
the full document (including shadow DOMs) to discover every interactive
form field.  The JS is a faithful port of ``extractFields()`` from
GHOST-HANDS ``formFiller.ts``.

The Python layer:
1. Injects ``window.__ff`` helpers (from shadow_helpers).
2. Runs JS extraction to collect raw field descriptors.
3. Runs a second JS pass to detect button-groups (Yes/No choices).
4. Groups radio/checkbox siblings into ``radio-group``/``checkbox-group``.
5. Parses every result into :class:`FormField` via Pydantic.
6. Wraps everything in :class:`ExtractionResult`.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import structlog
from playwright.async_api import Page

from ghosthands.dom.shadow_helpers import (
	PLACEHOLDER_RE_SOURCE,
	ensure_helpers,
	inject_helpers,
)
from ghosthands.dom.views import (
	ExtractionResult,
	FieldOption,
	FormField,
	ValidationSnapshot,
)

log = structlog.get_logger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

# Labels that indicate a Workday internal widget, not a real field.
_WORKDAY_NOISE_LABELS = frozenset({"items selected"})


# ── JavaScript: raw field extraction ─────────────────────────────────────
# Uses ES5-compatible syntax (var, function(){}) for maximum compatibility
# across WebView / browser runtimes.

_JS_EXTRACT_RAW_FIELDS = """
() => {
	var ff = window.__ff;
	if (!ff) return [];

	var seen = new Set();
	var out = [];

	var normalizeFieldText = function(text) {
		return (text || '').replace(/\s+/g, ' ').trim();
	};

	var cleanFieldLabelText = function(node) {
		if (!node) return '';
		var clone = node.cloneNode(true);
		clone.querySelectorAll(
			'input, textarea, select, button, [role="radio"], [role="checkbox"], [role="switch"], [role="textbox"], [role="combobox"], [class*="desc"], [class*="sub"], [class*="hint"], .option-desc, small'
		).forEach(function(x) { x.remove(); });
		return normalizeFieldText(clone.textContent || '');
	};

	var isGenericSearchLabel = function(text) {
		var norm = normalizeFieldText(text).toLowerCase();
		if (!norm) return true;
		return /^(search|search\.\.\.|search here|type to search|filter|find|lookup)$/.test(norm);
	};

	var getSearchFieldWrapperLabel = function(el) {
		var wrapper = ff.closestCrossRoot(
			el,
			'[data-automation-id="formField"], [data-automation-id*="formField"], fieldset, .form-group, .field'
		) || el.parentElement || el;
		var candidates = [
			wrapper.querySelector(':scope > legend'),
			wrapper.querySelector(':scope > [data-automation-id="fieldLabel"]'),
			wrapper.querySelector(':scope > [data-automation-id*="fieldLabel"]'),
			wrapper.querySelector(':scope > label'),
			wrapper.querySelector(':scope > [class*="question"]')
		];
		for (var i = 0; i < candidates.length; i++) {
			var text = cleanFieldLabelText(candidates[i]);
			if (!text || isGenericSearchLabel(text)) continue;
			return text;
		}
		return '';
	};

	var shouldSkip = function(el) {
		if (ff.closestCrossRoot(el, '[class*="select-dropdown"], [class*="select-option"]')) return true;
		if (ff.closestCrossRoot(el, '.iti__dropdown-content')) return true;
		if (ff.closestCrossRoot(el, '[data-automation-id="activeListContainer"]')) return true;
		if (el.getAttribute('role') === 'listbox' && el.closest('[role="combobox"]')) return true;
		if (el.getAttribute('role') === 'listbox' && el.id) {
			var controller = ff.queryOne('[role="combobox"][aria-controls="' + el.id + '"]');
			if (controller) return true;
		}
		if (el.tagName === 'INPUT' && el.type === 'search' && ff.closestCrossRoot(el, '[class*="dropdown"], [role="dialog"]')) {
			return !getSearchFieldWrapperLabel(el);
		}
		if (el.tagName === 'INPUT' && (el.type === 'radio' || el.type === 'checkbox') && window.getComputedStyle(el).display === 'none') return true;
		return false;
	};

	var getOptionMainText = function(opt) {
		var clone = opt.cloneNode(true);
		clone.querySelectorAll('[class*="desc"], [class*="sub"], [class*="hint"], .option-desc, small').forEach(function(x) { x.remove(); });
		return (clone.textContent || '').trim();
	};

	var placeholderRe = new RegExp(""" + repr(PLACEHOLDER_RE_SOURCE) + """, 'i');

	ff.queryAll(ff.SELECTOR).forEach(function(el) {
		if (seen.has(el)) return;
		seen.add(el);
		if (shouldSkip(el)) return;

		var id = ff.tag(el);

		/* ── resolve type ── */
		var role = el.getAttribute('role');
		var type;
		if (role === 'textbox' && el.getAttribute('aria-multiline') === 'true') type = 'textarea';
		else if (role === 'textbox') type = 'text';
		else if (role === 'combobox') type = 'select';
		else if (role === 'listbox') type = 'select';
		else if (el.getAttribute('data-uxi-widget-type') === 'selectinput') type = 'select';
		else if (el.getAttribute('aria-haspopup') === 'listbox') type = 'select';
		else if (role === 'radio') type = 'radio';
		else if (role === 'checkbox') type = 'checkbox';
		else if (role === 'spinbutton') type = 'number';
		else if (role === 'slider') type = 'range';
		else if (role === 'searchbox') type = 'search';
		else if (role === 'switch') type = 'toggle';
		else if (el.tagName === 'SELECT') type = 'select';
		else if (el.tagName === 'TEXTAREA') type = 'textarea';
		else {
			var t = el.type || '';
			var typeMap = {
				text:'text', email:'email', tel:'tel', url:'url',
				number:'number', date:'date', file:'file',
				checkbox:'checkbox', radio:'radio', search:'search',
				password:'password', hidden:'hidden'
			};
			type = typeMap[t] || t || 'text';
		}

		/* ── visibility (file inputs get container check) ── */
		var visible;
		if (type === 'file' && !ff.isVisible(el)) {
			var fileContainer = el.closest('[class*=upload], [class*=drop], .form-group, .field');
			visible = fileContainer ? ff.isVisible(fileContainer) : false;
		} else {
			visible = ff.isVisible(el);
		}

		/* ── native select detection ── */
		var isNative = el.tagName === 'SELECT';
		var isMultiSelect = type === 'select' && !isNative && !!(
			el.querySelector('[class*="multi"]') ||
			(el.classList && el.classList.toString().indexOf('multi') !== -1) ||
			el.getAttribute('aria-multiselectable') === 'true' ||
			(el.querySelector('[aria-selected]') && el.querySelector('[aria-selected]').closest('[class*="multi"]'))
		);

		/* ── label ── */
			var rawLabel = ff.getAccessibleName(el);
			var wrapperLabel = (
				type === 'search' ||
				(type === 'select' && el.getAttribute('data-uxi-widget-type') === 'selectinput')
			) ? getSearchFieldWrapperLabel(el) : '';
			if (wrapperLabel && (!rawLabel || isGenericSearchLabel(rawLabel))) {
				rawLabel = wrapperLabel;
			}

		/* ── required signals ── */
		var requiredSignals = [];
		if (el.required) requiredSignals.push('html_required');
		if (el.getAttribute('aria-required') === 'true') requiredSignals.push('aria_required');
		if (el.dataset && el.dataset.required === 'true') requiredSignals.push('data_required');
		if (el.dataset && el.dataset.ffRequired === 'true') requiredSignals.push('ff_required');
		if (rawLabel && /\\*/.test(rawLabel)) requiredSignals.push('label_asterisk');
		if (rawLabel && /required/i.test(rawLabel)) requiredSignals.push('label_required_text');
		var isRequired = requiredSignals.length > 0;

		/* ── label source tracking ── */
		var labelSources = [];
		if (el.getAttribute('aria-labelledby')) labelSources.push('aria-labelledby');
		else if (el.getAttribute('aria-label')) labelSources.push('aria-label');
		else if (el.id && ff.queryOne('label[for="' + el.id + '"]')) labelSources.push('label[for]');
		else if (el.closest && el.closest('label')) labelSources.push('ancestor-label');
		else if (el.closest && el.closest('fieldset') && el.closest('fieldset').querySelector('legend')) labelSources.push('legend');
		else if (el.placeholder) labelSources.push('placeholder');
		else if (el.getAttribute('title')) labelSources.push('title');

		/* ── sanitize label ── */
		var syntheticLabel = false;
		var label = rawLabel;
		if (!label) {
			label = el.getAttribute('name') || el.getAttribute('data-automation-id') || '';
			if (label) {
				labelSources.push('name');
				syntheticLabel = true;
			}
		}
		label = label.replace(/\\s*\\*\\s*/g, ' ').replace(/\\s*Required\\s*/gi, '').replace(/\\s+/g, ' ').trim();

		/* ── value ── */
		var value = '';
		if (type === 'checkbox' || type === 'toggle') {
			value = el.checked ? 'true' : 'false';
		} else if (type === 'radio') {
			value = el.checked ? 'true' : 'false';
		} else if (isNative) {
			value = el.options && el.selectedIndex >= 0 ? (el.options[el.selectedIndex].text || '').trim() : '';
		} else if (type === 'select') {
			value = (el.textContent || '').trim();
			if (placeholderRe.test(value)) value = '';
		} else {
			value = el.value || '';
		}

		/* ── section ── */
		var section = ff.getSection(el);

		/* ── placeholder ── */
			var placeholder = el.placeholder || el.getAttribute('placeholder') || '';

		/* ── disabled ── */
		var disabled = !!(el.disabled || el.getAttribute('aria-disabled') === 'true');

		/* ── build entry ── */
		var entry = {
			id: id,
			label: label,
			rawLabel: rawLabel,
			type: type,
			section: section,
			required: isRequired,
			requiredSignals: requiredSignals,
			visible: visible,
			isNative: isNative,
			isMultiSelect: isMultiSelect,
			value: value,
			placeholder: placeholder,
			labelSources: labelSources,
			syntheticLabel: syntheticLabel,
			disabled: disabled,
			name: el.getAttribute('name') || ''
		};

		/* ── file accept ── */
		if (el.accept) entry.accept = el.accept;

		/* ── options for selects ── */
		if (type === 'select') {
			var opts = [];
			if (el.tagName === 'SELECT') {
				var nativeOpts = el.options || [];
				for (var oi = 0; oi < nativeOpts.length; oi++) {
					var o = nativeOpts[oi];
					if (o.value === '') continue;
					var optText = (o.textContent || '').trim();
					if (optText) opts.push({ value: o.value, text: optText, selected: o.selected });
				}
			} else {
				var ctrlId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
				var src = ctrlId ? ff.getByDomId(ctrlId) : null;
				if (!src && el.tagName === 'INPUT') {
					src = ff.closestCrossRoot(el, '[class*="select"], [class*="combobox"], .form-group, .field');
				}
				if (!src) src = el;
				if (src) {
					var roleOpts = src.querySelectorAll('[role="option"], [role="menuitem"]');
					for (var ri = 0; ri < roleOpts.length; ri++) {
						var optT = getOptionMainText(roleOpts[ri]);
						if (optT) {
							var isSel = roleOpts[ri].getAttribute('aria-selected') === 'true';
							opts.push({ value: optT, text: optT, selected: isSel });
						}
					}
				}
			}
			if (opts.length) entry.options = opts;
		}

		/* ── checkbox/radio item label ── */
		if (type === 'checkbox' || type === 'radio') {
			var labelEl = el.querySelector('[class*="label"], .rc-label');
			if (labelEl) {
				entry.itemLabel = (labelEl.textContent || '').trim();
			} else {
				var wrap = el.closest('label');
				if (wrap) {
					var wc = wrap.cloneNode(true);
					wc.querySelectorAll('input, [class*=desc], small').forEach(function(x) { x.remove(); });
					entry.itemLabel = (wc.textContent || '').trim();
				} else {
					entry.itemLabel = el.getAttribute('aria-label') || ff.getAccessibleName(el);
				}
			}
			entry.itemValue = el.value || (el.querySelector('input') ? el.querySelector('input').value : '') || '';
		}

		out.push(entry);
	});

	return out;
}
"""


# ── JavaScript: button-group detection ───────────────────────────────────

_NAV_BUTTON_TEXTS_JS = repr([
	"save and continue", "next", "continue", "submit", "submit application",
	"apply", "add", "add another", "replace", "upload", "browse", "remove",
	"delete", "cancel", "back", "previous", "close", "save", "select one",
	"choose file",
])

_JS_DETECT_BUTTON_GROUPS = """
() => {
	var ff = window.__ff;
	if (!ff) return [];

	var results = [];
	var allBtnEls = document.querySelectorAll('button, [role="button"]');
	var parentMap = {};
	var NAV_TEXTS = """ + _NAV_BUTTON_TEXTS_JS + """;
	var normalize = function(text) {
		return (text || '').replace(/\\s+/g, ' ').trim();
	};
	var cleanQuestionText = function(node) {
		if (!node) return '';
		var clone = node.cloneNode(true);
		clone.querySelectorAll(
			'button, [role="button"], [role="radio"], [role="checkbox"], [role="switch"], input, textarea, select, ul, ol, li, [role="option"], [role="listbox"], .cx-select-pills-container, .oracle-pill-group, [class*="hint"], [class*="desc"], [class*="sub"], small'
		).forEach(function(x) { x.remove(); });
		return normalize(clone.textContent || '');
	};
	var isOptionOnlyText = function(text, optionNorms) {
		var norm = normalize(text).toLowerCase();
		if (!norm) return true;
		return optionNorms.has(norm);
	};
	var pushCandidate = function(candidates, text, optionNorms) {
		var clean = normalize(text);
		if (!clean || clean.length > 2000) return;
		if (isOptionOnlyText(clean, optionNorms)) return;
		candidates.push(clean);
	};
	var pushBestPrecedingSiblingText = function(candidates, startNode, stopNode, optionNorms) {
		var cursor = startNode;
		while (cursor && cursor !== stopNode) {
			var sibling = cursor.previousElementSibling;
			while (sibling) {
				pushCandidate(candidates, cleanQuestionText(sibling), optionNorms);
				if (candidates.length) return candidates[candidates.length - 1];
				sibling = sibling.previousElementSibling;
			}
			cursor = cursor.parentElement;
		}
		return '';
	};

	for (var i = 0; i < allBtnEls.length; i++) {
		var btn = allBtnEls[i];
		if (!ff.isVisible(btn)) continue;
		if (btn.disabled) continue;
		if (btn.closest('nav, header, [role="navigation"], [role="menubar"], [role="menu"], [role="toolbar"], [data-automation-id*="header"], [data-automation-id*="navigation"]')) continue;
		if (btn.tagName === 'A' || btn.closest('a[href]')) continue;
		if (btn.getAttribute('role') === 'combobox') continue;
		if (btn.getAttribute('aria-haspopup') === 'listbox') continue;
		if (btn.tagName.toLowerCase() === 'input') continue;
		if (btn.closest('[data-automation-id="selectedItemList"], [data-automation-id="selectedItems"], [data-automation-id="multiSelectContainer"], [data-uxi-widget-type="multiselect"]')) continue;

		var btnText = (btn.textContent || '').trim();
		if (!btnText || btnText.length > 30) continue;

		var btnLower = btnText.toLowerCase();
		if (NAV_TEXTS.indexOf(btnLower) !== -1) continue;
		if (btnLower.indexOf('add ') === 0) continue;
		if (btnLower.indexOf('save & continue') !== -1) continue;

		/* walk up to 3 ancestors to find parent with 2-4 visible buttons */
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
		var alreadyAdded = false;
		for (var ai = 0; ai < parentMap[parentKey].buttons.length; ai++) {
			if (parentMap[parentKey].buttons[ai].el === btn) { alreadyAdded = true; break; }
		}
		if (!alreadyAdded) {
			parentMap[parentKey].buttons.push({ el: btn, text: btnText });
		}
	}

	for (var groupKey in parentMap) {
		var group = parentMap[groupKey];
		if (group.buttons.length < 2 || group.buttons.length > 4) continue;

		var tooLong = false;
		for (var tl = 0; tl < group.buttons.length; tl++) {
			if (group.buttons[tl].text.length > 30) { tooLong = true; break; }
		}
		if (tooLong) continue;

		var container = group.parent;
		var questionLabel = '';
		var choices = [];
		for (var ci = 0; ci < group.buttons.length; ci++) {
			choices.push(group.buttons[ci].text);
		}
		var optionNorms = new Set(
			choices.map(function(text) { return normalize(text).toLowerCase(); }).filter(Boolean)
		);

		/* prefer text that lives in the same owning container before the pills */
		questionLabel = pushBestPrecedingSiblingText([], container, container.parentElement, optionNorms);

		/* try previous sibling */
		var prevSib = container.previousElementSibling;
		if (!questionLabel && prevSib) {
			var prevText = cleanQuestionText(prevSib);
			if (prevText && prevText.length > 5 && prevText.length < 300) {
				questionLabel = prevText;
			}
		}

		/* walk up ancestors looking for preceding text */
		if (!questionLabel) {
			var labelContainer = container.parentElement;
			for (var lc = 0; lc < 5 && labelContainer && !questionLabel; lc++) {
				var children = labelContainer.children;
				for (var ch = 0; ch < children.length; ch++) {
					var child = children[ch];
					if (child === container || child.contains(container)) break;
					var childText = cleanQuestionText(child);
					if (childText && childText.length > 5 && childText.length < 300) {
						questionLabel = childText;
					}
				}
				if (questionLabel) break;
				labelContainer = labelContainer.parentElement;
			}
		}

		/* aria-label on ancestor */
		if (!questionLabel) {
			var ariaContainer = container.closest('[aria-label]');
			if (ariaContainer) {
				var ariaText = ariaContainer.getAttribute('aria-label');
				if (ariaText && ariaText.length > 5) questionLabel = ariaText;
			}
		}

		if (!questionLabel) continue;

		var rawQuestionLabel = questionLabel;

		/* sanitize label */
		questionLabel = questionLabel
			.replace(/\\s*\\*\\s*/g, ' ')
			.replace(/\\s*Required\\s*/gi, '')
			.replace(/\\s+/g, ' ')
			.trim();

		/* skip noise labels */
		if (/\\b(follow us|privacy policy|job alerts)\\b/i.test(questionLabel)) continue;
		if (questionLabel.length > 200) {
			questionLabel = questionLabel.substring(0, 200).trim();
		}

		/* collect tagged choices */
		choices = [];
		var btnIds = [];
		for (var ci = 0; ci < group.buttons.length; ci++) {
			var bid = ff.tag(group.buttons[ci].el);
			choices.push(group.buttons[ci].text);
			btnIds.push(bid);
		}

		/* filter nav-like choice sets */
		var navHits = 0;
		var navRe = /\\b(careers|search for jobs|candidate home|job alerts|privacy policy|follow us|about us)\\b/i;
		for (var ni = 0; ni < choices.length; ni++) {
			if (navRe.test(choices[ni])) navHits++;
		}
		if (navHits >= 2) continue;

		var containerId = ff.tag(container);

		/* required signals */
		var requiredSignals = [];
		if (rawQuestionLabel.indexOf('*') !== -1) requiredSignals.push('label_asterisk');
		if (prevSib && (prevSib.textContent || '').indexOf('*') !== -1) requiredSignals.push('sibling_asterisk');
		if (container.getAttribute('aria-required') === 'true') requiredSignals.push('aria_required');
		if (container.closest('[aria-required="true"]')) requiredSignals.push('ancestor_aria_required');
		if (/required/i.test(rawQuestionLabel)) requiredSignals.push('label_required_text');

		results.push({
			id: containerId,
			label: questionLabel,
			rawLabel: rawQuestionLabel,
			type: 'button-group',
			section: ff.getSection(container),
			required: requiredSignals.length > 0,
			requiredSignals: requiredSignals,
			visible: true,
			isNative: false,
			choices: choices,
			btnIds: btnIds,
			disabled: false,
			labelSources: ['button-group-heuristic'],
			syntheticLabel: false,
			name: '',
			value: '',
			placeholder: '',
			isMultiSelect: false
		});
	}

	return results;
}
"""


# ── JavaScript: page metadata ────────────────────────────────────────────

_JS_PAGE_METADATA = """
() => {
	var formEls = document.querySelectorAll('form');
	var submitBtn = document.querySelector(
		'button[type="submit"], input[type="submit"], ' +
		'[data-automation-id="bottom-navigation-next-button"], ' +
		'[data-automation-id="submit-button"]'
	);
	/* Heuristic: check if any visible button says submit/apply */
	if (!submitBtn) {
		var allBtns = document.querySelectorAll('button');
		for (var i = 0; i < allBtns.length; i++) {
			var txt = (allBtns[i].textContent || '').trim().toLowerCase();
			if (txt === 'submit' || txt === 'submit application' || txt === 'apply' || txt === 'save and continue' || txt === 'next') {
				submitBtn = allBtns[i];
				break;
			}
		}
	}
	return {
		title: document.title || '',
		url: window.location.href,
		formCount: formEls.length,
		hasSubmitButton: !!submitBtn
	};
}
"""


# ── Fingerprint generation ───────────────────────────────────────────────

def _field_fingerprint(
	field_type: str,
	label: str,
	name: str,
	section: str,
) -> str:
	"""Create a stable identity fingerprint for a field.

	Helps track fields across re-extractions even if ``ff_id`` changes
	(e.g. after SPA navigation resets the counter).
	"""
	raw = f"{field_type}|{label.lower().strip()}|{name.lower().strip()}|{section.lower().strip()}"
	return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()[:12]


# ── Sanitize label ───────────────────────────────────────────────────────

_ASTERISK_RE = re.compile(r"\s*\*\s*")
_REQUIRED_RE = re.compile(r"\s*Required\s*", re.IGNORECASE)
_MULTI_SPACE_RE = re.compile(r"\s+")


def _sanitize_label(raw: str) -> str:
	"""Strip asterisks and 'Required' from a label string."""
	s = _ASTERISK_RE.sub(" ", raw)
	s = _REQUIRED_RE.sub("", s)
	s = _MULTI_SPACE_RE.sub(" ", s).strip()
	return s


# ── Raw JS result -> FormField conversion ────────────────────────────────

def _parse_raw_field(raw: dict[str, Any]) -> FormField:
	"""Convert a single raw JS field descriptor into a :class:`FormField`."""
	ff_id: str = raw.get("id", "")
	field_type: str = raw.get("type", "unknown")
	label: str = raw.get("label", "")
	raw_label: str = raw.get("rawLabel", "")
	name: str = raw.get("name", "")
	section: str = raw.get("section", "")
	value: str = raw.get("value", "")
	placeholder: str = raw.get("placeholder", "")

	options: list[FieldOption] = []
	for opt in raw.get("options", []):
		if isinstance(opt, dict):
			options.append(FieldOption(
				value=opt.get("value", ""),
				text=opt.get("text", ""),
				selected=opt.get("selected", False),
			))
		elif isinstance(opt, str):
			options.append(FieldOption(value=opt, text=opt, selected=False))

	choices: list[str] = raw.get("choices", [])
	btn_ids: list[str] = raw.get("btnIds", [])

	return FormField(
		ff_id=ff_id,
		selector=f'[data-ff-id="{ff_id}"]',
		field_type=field_type,
		label=label,
		raw_label=raw_label,
		name=name,
		value=value,
		placeholder=placeholder,
		required=raw.get("required", False),
		required_signals=raw.get("requiredSignals", []),
		options=options,
		choices=choices,
		section=section,
		visible=raw.get("visible", True),
		is_native=raw.get("isNative", False),
		is_multi_select=raw.get("isMultiSelect", False),
		accept=raw.get("accept", ""),
		label_sources=raw.get("labelSources", []),
		synthetic_label=raw.get("syntheticLabel", False),
		item_label=raw.get("itemLabel", ""),
		item_value=raw.get("itemValue", ""),
		btn_ids=btn_ids,
		disabled=raw.get("disabled", False),
		field_fingerprint=_field_fingerprint(field_type, label, name, section),
	)


# ── Grouping logic ──────────────────────────────────────────────────────

def _group_fields(raw_fields: list[dict[str, Any]]) -> list[FormField]:
	"""Group radio/checkbox siblings into ``radio-group`` / ``checkbox-group``.

	Mirrors the post-processing in ``extractFields()`` from formFiller.ts.
	All other field types pass through as-is.
	"""
	fields: list[FormField] = []
	seen_ids: set[str] = set()
	seen_group_keys: set[str] = set()

	for raw in raw_fields:
		fid: str = raw.get("id", "")
		if not fid or fid in seen_ids:
			continue

		ftype: str = raw.get("type", "")
		fname: str = raw.get("label", "")
		fsection: str = raw.get("section", "")

		if ftype in ("checkbox", "radio"):
			group_key = f"group:{fname}:{fsection}"
			if group_key in seen_group_keys:
				continue
			seen_ids.add(fid)

			# Find siblings — same type, same label (question text), same section
			siblings = [
				r for r in raw_fields
				if r.get("type") in ("checkbox", "radio")
				and r.get("label") == fname
				and r.get("section") == fsection
			]

			if len(siblings) > 1:
				# Merge into a group
				seen_group_keys.add(group_key)
				for sib in siblings:
					seen_ids.add(sib.get("id", ""))

				group_type = f"{ftype}-group"
				choices = [
					s.get("itemLabel") or s.get("label", "")
					for s in siblings
				]

				fields.append(FormField(
					ff_id=fid,
					selector=f'[data-ff-id="{fid}"]',
					field_type=group_type,
					label=fname,
					raw_label=raw.get("rawLabel", ""),
					name=raw.get("name", ""),
					section=fsection,
					required=raw.get("required", False),
					required_signals=raw.get("requiredSignals", []),
					choices=choices,
					visible=raw.get("visible", True),
					label_sources=raw.get("labelSources", []),
					field_fingerprint=_field_fingerprint(group_type, fname, raw.get("name", ""), fsection),
				))
			else:
				# Standalone checkbox/radio — use itemLabel as the display label
				item_label = raw.get("itemLabel") or fname
				fields.append(_parse_raw_field({
					**raw,
					"label": item_label,
				}))
		else:
			seen_ids.add(fid)
			fields.append(_parse_raw_field(raw))

	return fields


# ── Public API ───────────────────────────────────────────────────────────

async def extract_form_fields(
	page: Page,
	target_section: str | None = None,
) -> ExtractionResult:
	"""Extract all interactive form fields from the current page.

	Injects shadow DOM helpers, then runs a JavaScript extraction script that:

	1. Traverses all document roots (including shadow DOMs)
	2. Finds all interactive elements (inputs, selects, textareas, ARIA roles)
	3. Resolves accessible names via the label resolution chain
	4. Reads current values, options, checked states
	5. Detects required fields via multi-signal analysis
	6. Groups radio buttons and button groups
	7. Filters by visibility and optional section
	8. Tags each element with ``data-ff-id`` for later targeting

	Parameters
	----------
	page:
		Playwright page instance (must already be navigated).
	target_section:
		If provided, only return fields whose ``section`` matches
		(case-insensitive substring).

	Returns
	-------
	ExtractionResult
		Pydantic model containing the field list and page metadata.
	"""
	# 1. Ensure __ff helpers are present
	await ensure_helpers(page)

	# 2. Extract raw field descriptors from the DOM
	try:
		raw_fields: list[dict[str, Any]] = await page.evaluate(_JS_EXTRACT_RAW_FIELDS)
	except Exception:
		log.warning("field_extraction.raw_fields_failed", exc_info=True)
		raw_fields = []
		# Re-inject and retry once — SPA navigation may have wiped the context
		try:
			await inject_helpers(page)
			raw_fields = await page.evaluate(_JS_EXTRACT_RAW_FIELDS)
		except Exception:
			log.error("field_extraction.raw_fields_retry_failed", exc_info=True)

	# 3. Detect button groups (second JS pass)
	try:
		button_groups: list[dict[str, Any]] = await page.evaluate(_JS_DETECT_BUTTON_GROUPS)
	except Exception:
		log.warning("field_extraction.button_groups_failed", exc_info=True)
		button_groups = []

	# 4. Group radio/checkbox siblings into -group pseudo-fields
	fields = _group_fields(raw_fields)

	# 5. Append button groups (avoid duplicates by ff_id)
	seen_ids = {f.ff_id for f in fields}
	for bg in button_groups:
		bg_id = bg.get("id", "")
		if bg_id and bg_id not in seen_ids:
			seen_ids.add(bg_id)
			fields.append(_parse_raw_field(bg))

	# 6. Filter out Workday noise fields
	fields = [
		f for f in fields
		if f.label.lower().strip() not in _WORKDAY_NOISE_LABELS
	]

	# 7. Apply optional section filter
	if target_section:
		target_lower = target_section.lower()
		fields = [
			f for f in fields
			if target_lower in f.section.lower()
		]

	# 8. Add observation warnings
	for f in fields:
		warnings: list[str] = []
		if not f.label:
			warnings.append("no_label_resolved")
		if f.synthetic_label:
			warnings.append("synthetic_label")
		if f.field_type == "select" and not f.options and f.is_native:
			warnings.append("native_select_no_options")
		if f.field_type in ("radio-group", "checkbox-group") and not f.choices:
			warnings.append("group_no_choices")
		if f.disabled:
			warnings.append("disabled")
		f.observation_warnings = warnings

	# 9. Collect page metadata
	try:
		meta: dict[str, Any] = await page.evaluate(_JS_PAGE_METADATA)
	except Exception:
		log.warning("field_extraction.metadata_failed", exc_info=True)
		meta = {}

	log.info(
		"field_extraction.complete",
		total_raw=len(raw_fields),
		total_button_groups=len(button_groups),
		total_fields=len(fields),
		visible_fields=sum(1 for f in fields if f.visible),
		required_fields=sum(1 for f in fields if f.required),
		section_filter=target_section,
	)

	return ExtractionResult(
		fields=fields,
		page_title=meta.get("title", ""),
		page_url=meta.get("url", ""),
		form_count=meta.get("formCount", 0),
		has_submit_button=meta.get("hasSubmitButton", False),
		validation=ValidationSnapshot(),
	)
