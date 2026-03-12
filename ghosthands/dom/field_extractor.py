"""Main form field extraction — runs Playwright page.evaluate() scripts to
extract all interactive form fields from the current page.

Ports ``extractFields()`` and the button-group detection from GHOST-HANDS
formFiller.ts.  The flow is:

1. Ensure ``window.__ff`` helpers are injected (shadow_helpers)
2. Run the main extraction JS to find all input/select/textarea/role elements
3. Run the button-group detection JS for Yes/No and multi-choice button questions
4. Group radio/checkbox siblings into ``-group`` pseudo-fields
5. Enrich with label metadata, fingerprints, and current values
6. Optionally read validation errors
7. Return an ``ExtractionResult`` with Pydantic models
"""

import logging

from playwright.async_api import Page

from ghosthands.dom.label_resolver import (
	generate_field_fingerprint,
	normalize_name,
	sanitize_label,
)
from ghosthands.dom.shadow_helpers import ensure_helpers
from ghosthands.dom.validation_reader import capture_validation_errors
from ghosthands.dom.views import (
	ExtractionResult,
	FieldOption,
	FormField,
)

logger = logging.getLogger("ghosthands.dom.field_extractor")


# ── Main extraction JS ───────────────────────────────────────────────────
# This runs inside the browser and returns raw dicts for each interactive
# element it finds.

_EXTRACT_FIELDS_JS = """
() => {
	const ff = window.__ff;
	const seen = new Set();
	const out = [];

	const shouldSkip = (el) => {
		if (ff.closestCrossRoot(el, '[class*="select-dropdown"], [class*="select-option"]')) return true;
		if (ff.closestCrossRoot(el, '.iti__dropdown-content')) return true;
		if (ff.closestCrossRoot(el, '[data-automation-id="activeListContainer"]')) return true;
		if (el.getAttribute('role') === 'listbox' && el.closest('[role="combobox"]')) return true;
		if (el.getAttribute('role') === 'listbox' && el.id) {
			const controller = ff.queryOne('[role="combobox"][aria-controls="' + el.id + '"]');
			if (controller) return true;
		}
		if (el.tagName === 'INPUT' && el.type === 'search' && ff.closestCrossRoot(el, '[class*="dropdown"], [role="dialog"]')) return true;
		if (el.tagName === 'INPUT' && (el.type === 'radio' || el.type === 'checkbox') && window.getComputedStyle(el).display === 'none') return true;
		return false;
	};

	const getOptionMainText = (opt) => {
		const clone = opt.cloneNode(true);
		clone.querySelectorAll('[class*="desc"], [class*="sub"], [class*="hint"], .option-desc, small').forEach(x => x.remove());
		return clone.textContent?.trim() || '';
	};

	ff.queryAll(ff.SELECTOR).forEach((el) => {
		if (seen.has(el)) return;
		seen.add(el);
		if (shouldSkip(el)) return;

		const id = ff.tag(el);

		/* Resolve field type */
		const type = (() => {
			const role = el.getAttribute('role');
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
			const t = el.type || '';
			return ({
				text:'text', email:'email', tel:'tel', url:'url', number:'number',
				date:'date', file:'file', checkbox:'checkbox', radio:'radio',
				search:'search', password:'password'
			})[t] || t || 'text';
		})();

		/* Visibility — file inputs get special container-level check */
		const visible = (() => {
			if (type === 'file' && !ff.isVisible(el)) {
				const container = el.closest('[class*=upload], [class*=drop], .form-group, .field');
				return container ? ff.isVisible(container) : false;
			}
			return ff.isVisible(el);
		})();

		const isNative = el.tagName === 'SELECT';
		const isMultiSelect = type === 'select' && !isNative && !!(
			el.querySelector('[class*="multi"]') ||
			el.classList.toString().includes('multi') ||
			el.getAttribute('aria-multiselectable') === 'true' ||
			el.querySelector('[aria-selected]')?.closest('[class*="multi"]')
		);

		/* Required detection */
		const required = el.required ||
			el.getAttribute('aria-required') === 'true' ||
			el.dataset.required === 'true' ||
			el.dataset.ffRequired === 'true';

		const entry = {
			id: id,
			name: ff.getAccessibleName(el),
			type: type,
			section: ff.getSection(el),
			required: required,
			visible: visible,
			isNative: isNative,
			isMultiSelect: isMultiSelect,
			placeholder: el.placeholder || '',
			disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
		};

		if (el.accept) entry.accept = el.accept;

		/* Read options for select fields */
		if (type === 'select') {
			let opts = [];
			if (el.tagName === 'SELECT') {
				opts = Array.from(el.options)
					.filter(o => o.value !== '')
					.map(o => ({
						value: o.value,
						text: o.textContent?.trim() || '',
						selected: o.selected,
					}));
			} else {
				const ctrlId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
				let src = ctrlId ? ff.getByDomId(ctrlId) : null;
				if (!src && el.tagName === 'INPUT') {
					src = ff.closestCrossRoot(el, '[class*="select"], [class*="combobox"], .form-group, .field');
				}
				if (!src) src = el;
				if (src) {
					opts = Array.from(src.querySelectorAll('[role="option"], [role="menuitem"]'))
						.map(o => ({
							value: o.getAttribute('data-value') || o.textContent?.trim() || '',
							text: getOptionMainText(o),
							selected: o.getAttribute('aria-selected') === 'true',
						}))
						.filter(o => o.text);
				}
			}
			if (opts.length) entry.options = opts;
		}

		/* Checkbox/radio item label */
		if (type === 'checkbox' || type === 'radio') {
			const labelEl = el.querySelector('[class*="label"], .rc-label');
			if (labelEl) {
				entry.itemLabel = labelEl.textContent?.trim() || '';
			} else {
				const wrap = el.closest('label');
				if (wrap) {
					const c = wrap.cloneNode(true);
					c.querySelectorAll('input, [class*=desc], small').forEach(x => x.remove());
					entry.itemLabel = c.textContent?.trim() || '';
				} else {
					entry.itemLabel = el.getAttribute('aria-label') || ff.getAccessibleName(el);
				}
			}
			entry.itemValue = el.value || el.querySelector('input')?.value || '';
		}

		/* Current value */
		if (type === 'checkbox' || type === 'radio') {
			entry.currentValue = el.checked ? 'checked' : '';
		} else if (el.tagName === 'SELECT') {
			const selected = el.options[el.selectedIndex];
			entry.currentValue = selected ? selected.textContent?.trim() || '' : '';
		} else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
			entry.currentValue = el.value || '';
		} else if (el.getAttribute('contenteditable') === 'true') {
			entry.currentValue = el.textContent?.trim() || '';
		} else {
			entry.currentValue = '';
		}

		out.push(entry);
	});
	return out;
}
"""


# ── Button group detection JS ────────────────────────────────────────────

_EXTRACT_BUTTON_GROUPS_JS = """
() => {
	const ff = window.__ff;
	const results = [];

	const allBtnEls = document.querySelectorAll('button, [role="button"]');
	const parentMap = {};

	for (let i = 0; i < allBtnEls.length; i++) {
		const btn = allBtnEls[i];
		if (!ff.isVisible(btn)) continue;
		if (btn.disabled) continue;
		if (btn.closest('nav, header, [role="navigation"], [role="menubar"], [role="menu"], [role="toolbar"], [data-automation-id*="header"], [data-automation-id*="navigation"]')) continue;
		if (btn.tagName === 'A' || btn.closest('a[href]')) continue;
		if (btn.getAttribute('role') === 'combobox') continue;
		if (btn.getAttribute('aria-haspopup') === 'listbox') continue;
		if (btn.tagName.toLowerCase() === 'input') continue;

		const btnText = (btn.textContent || '').trim();
		if (!btnText || btnText.length > 30) continue;

		const btnLower = btnText.toLowerCase();
		if ([
			'save and continue', 'next', 'continue', 'submit', 'submit application',
			'apply', 'add', 'add another', 'replace', 'upload', 'browse', 'remove',
			'delete', 'cancel', 'back', 'previous', 'close', 'save', 'select one',
			'choose file',
		].includes(btnLower) || btnLower.startsWith('add ') || btnLower.includes('save & continue')) {
			continue;
		}

		let parent = btn.parentElement;
		for (let pu = 0; pu < 3 && parent; pu++) {
			const childBtns = parent.querySelectorAll('button, [role="button"]');
			let visibleCount = 0;
			for (let vc = 0; vc < childBtns.length; vc++) {
				if (ff.isVisible(childBtns[vc])) visibleCount++;
			}
			if (visibleCount >= 2 && visibleCount <= 4) break;
			parent = parent.parentElement;
		}
		if (!parent) continue;

		const parentKey = parent.getAttribute('data-ff-btn-group') || ('btngrp-' + i);
		parent.setAttribute('data-ff-btn-group', parentKey);

		if (!parentMap[parentKey]) {
			parentMap[parentKey] = { parent: parent, buttons: [] };
		}
		if (!parentMap[parentKey].buttons.some(entry => entry.el === btn)) {
			parentMap[parentKey].buttons.push({ el: btn, text: btnText });
		}
	}

	for (const groupKey in parentMap) {
		const group = parentMap[groupKey];
		if (group.buttons.length < 2 || group.buttons.length > 4) continue;
		if (group.buttons.some(entry => entry.text.length > 30)) continue;

		const container = group.parent;
		let questionLabel = '';

		/* Resolve label from preceding sibling */
		const prevSib = container.previousElementSibling;
		if (prevSib) {
			const prevText = (prevSib.textContent || '').trim();
			if (prevText && prevText.length > 5 && prevText.length < 300) {
				questionLabel = prevText;
			}
		}

		/* Walk up ancestors looking for preceding text */
		if (!questionLabel) {
			let labelContainer = container.parentElement;
			for (let lc = 0; lc < 5 && labelContainer && !questionLabel; lc++) {
				const children = labelContainer.children;
				for (let ch = 0; ch < children.length; ch++) {
					const child = children[ch];
					if (child === container || child.contains(container)) break;
					const childText = (child.textContent || '').trim();
					if (childText && childText.length > 5 && childText.length < 300) {
						questionLabel = childText;
					}
				}
				if (questionLabel) break;
				labelContainer = labelContainer.parentElement;
			}
		}

		/* aria-label on container */
		if (!questionLabel) {
			const ariaContainer = container.closest('[aria-label]');
			if (ariaContainer) {
				const ariaText = ariaContainer.getAttribute('aria-label');
				if (ariaText && ariaText.length > 5) questionLabel = ariaText;
			}
		}

		if (!questionLabel) continue;

		const rawQuestionLabel = questionLabel;

		questionLabel = questionLabel
			.replace(/\\s*\\*\\s*/g, ' ')
			.replace(/\\s*Required\\s*/gi, '')
			.replace(/\\s+/g, ' ')
			.trim();
		if (/\\b(follow us|privacy policy|job alerts)\\b/i.test(questionLabel)) continue;
		if (questionLabel.length > 200) {
			questionLabel = questionLabel.substring(0, 200).trim();
		}

		const choices = [];
		const btnIds = [];
		for (const entry of group.buttons) {
			const id = ff.tag(entry.el);
			choices.push(entry.text);
			btnIds.push(id);
		}

		const navChoiceHits = choices.filter(c =>
			/\\b(careers|search for jobs|candidate home|job alerts|privacy policy|follow us|about us)\\b/i.test(c)
		).length;
		if (navChoiceHits >= 2) continue;

		const containerId = ff.tag(container);

		/* Multi-signal required detection */
		const requiredSignals = [];
		if (rawQuestionLabel.includes('*')) requiredSignals.push('label_asterisk');
		if (prevSib && (prevSib.textContent || '').includes('*')) requiredSignals.push('sibling_asterisk');
		if (container.getAttribute('aria-required') === 'true') requiredSignals.push('aria_required');
		if (container.closest('[aria-required="true"]')) requiredSignals.push('ancestor_aria_required');
		if (/required/i.test(rawQuestionLabel)) requiredSignals.push('label_required_text');
		const isRequired = requiredSignals.length > 0;

		results.push({
			id: containerId,
			name: questionLabel,
			rawLabel: rawQuestionLabel,
			type: 'button-group',
			section: ff.getSection(container),
			required: isRequired,
			requiredSignals: requiredSignals,
			visible: true,
			isNative: false,
			choices: choices,
			btnIds: btnIds,
		});
	}

	return results;
}
"""


# ── Page metadata JS ─────────────────────────────────────────────────────

_PAGE_METADATA_JS = """
() => {
	const ff = window.__ff;
	const forms = document.querySelectorAll('form');
	const submitButtons = ff?.queryAll(
		'button[type="submit"], input[type="submit"], ' +
		'[data-automation-id="bottom-navigation-next-button"], ' +
		'[data-automation-id="submit-button"]'
	) ?? [];
	const hasSubmit = submitButtons.length > 0 || Array.from(
		ff?.queryAll('button') ?? []
	).some(btn => {
		const t = (btn.textContent || '').trim().toLowerCase();
		return ['submit', 'submit application', 'save and continue', 'next'].includes(t);
	});
	return {
		title: document.title || '',
		url: window.location.href,
		formCount: forms.length,
		hasSubmitButton: hasSubmit,
	};
}
"""


# ── Python extraction API ────────────────────────────────────────────────

def _raw_to_form_field(raw: dict) -> FormField:
	"""Convert a raw dict from the browser JS into a FormField model."""
	raw_label = raw.get("name", "")
	label = sanitize_label(raw_label)
	field_type = raw.get("type", "text")
	section = raw.get("section", "")
	name_attr = raw.get("name", "")

	options: list[FieldOption] = []
	if raw.get("options"):
		for opt in raw["options"]:
			if isinstance(opt, dict):
				options.append(FieldOption(
					value=opt.get("value", ""),
					text=opt.get("text", ""),
					selected=opt.get("selected", False),
				))
			elif isinstance(opt, str):
				options.append(FieldOption(value=opt, text=opt, selected=False))

	return FormField(
		ff_id=raw.get("id", ""),
		selector=f'[data-ff-id="{raw.get("id", "")}"]',
		field_type=field_type,
		label=label,
		raw_label=raw_label,
		name=name_attr,
		value=raw.get("currentValue", ""),
		placeholder=raw.get("placeholder", ""),
		required=raw.get("required", False),
		options=options,
		section=section,
		visible=raw.get("visible", True),
		is_native=raw.get("isNative", False),
		is_multi_select=raw.get("isMultiSelect", False),
		accept=raw.get("accept", ""),
		item_label=raw.get("itemLabel", ""),
		item_value=raw.get("itemValue", ""),
		disabled=raw.get("disabled", False),
		field_fingerprint=generate_field_fingerprint(field_type, label, section, name_attr),
	)


def _group_radio_checkbox_fields(raw_fields: list[dict]) -> list[FormField]:
	"""Group radio/checkbox siblings into -group pseudo-fields.

	Mirrors the post-processing in ``extractFields()`` from formFiller.ts.
	Radio/checkbox elements sharing the same ``name`` and ``section`` are
	collapsed into a single ``radio-group`` or ``checkbox-group`` field
	with ``choices`` populated from individual item labels.
	"""
	fields: list[FormField] = []
	seen_ids: set[str] = set()
	seen_groups: set[str] = set()

	for raw in raw_fields:
		fid = raw.get("id", "")
		if not fid or fid in seen_ids:
			continue

		ftype = raw.get("type", "")
		if ftype in ("checkbox", "radio"):
			group_key = f"group:{raw.get('name', '')}:{raw.get('section', '')}"
			if group_key in seen_groups:
				continue
			seen_ids.add(fid)

			siblings = [
				r for r in raw_fields
				if r.get("type") in ("checkbox", "radio")
				and r.get("name") == raw.get("name")
				and r.get("section") == raw.get("section")
			]

			if len(siblings) > 1:
				seen_groups.add(group_key)
				for s in siblings:
					seen_ids.add(s.get("id", ""))

				raw_label = raw.get("name", "")
				label = sanitize_label(raw_label)
				section = raw.get("section", "")
				group_type = f"{ftype}-group"
				choices = [s.get("itemLabel") or s.get("name", "") for s in siblings]

				fields.append(FormField(
					ff_id=fid,
					selector=f'[data-ff-id="{fid}"]',
					field_type=group_type,
					label=label,
					raw_label=raw_label,
					name=raw.get("name", ""),
					required=raw.get("required", False),
					choices=choices,
					section=section,
					visible=raw.get("visible", True),
					is_native=False,
					field_fingerprint=generate_field_fingerprint(group_type, label, section, raw.get("name", "")),
				))
			else:
				item_label = raw.get("itemLabel") or raw.get("name", "")
				fields.append(FormField(
					ff_id=fid,
					selector=f'[data-ff-id="{fid}"]',
					field_type=ftype,
					label=sanitize_label(item_label),
					raw_label=item_label,
					name=raw.get("name", ""),
					required=raw.get("required", False),
					section=raw.get("section", ""),
					visible=raw.get("visible", True),
					is_native=False,
					value="checked" if raw.get("currentValue") == "checked" else "",
					field_fingerprint=generate_field_fingerprint(
						ftype,
						sanitize_label(item_label),
						raw.get("section", ""),
						raw.get("name", ""),
					),
				))
		else:
			seen_ids.add(fid)
			fields.append(_raw_to_form_field(raw))

	return fields


def _raw_button_group_to_form_field(raw: dict) -> FormField:
	"""Convert a raw button-group dict from JS into a FormField."""
	raw_label = raw.get("rawLabel", raw.get("name", ""))
	label = sanitize_label(raw.get("name", ""))
	section = raw.get("section", "")
	choices = raw.get("choices", [])
	btn_ids = raw.get("btnIds", [])
	required_signals = raw.get("requiredSignals", [])

	return FormField(
		ff_id=raw.get("id", ""),
		selector=f'[data-ff-id="{raw.get("id", "")}"]',
		field_type="button-group",
		label=label,
		raw_label=raw_label,
		name=label,
		required=raw.get("required", False),
		required_signals=required_signals,
		choices=choices,
		btn_ids=btn_ids,
		section=section,
		visible=True,
		is_native=False,
		field_fingerprint=generate_field_fingerprint("button-group", label, section, label),
	)


async def extract_form_fields(
	page: Page,
	*,
	include_validation: bool = False,
) -> ExtractionResult:
	"""Extract all form fields from the current page via Playwright evaluate.

	This is the main entry point for DOM extraction.  It:

	1. Ensures ``window.__ff`` helpers are injected
	2. Runs the field extraction JS (inputs, selects, textareas, ARIA roles)
	3. Runs button-group detection (Yes/No, multi-choice button questions)
	4. Groups radio/checkbox siblings into ``-group`` pseudo-fields
	5. Enriches fields with label metadata and fingerprints
	6. Optionally captures validation errors
	7. Filters out Workday internal elements (``items selected``)

	Args:
		page: Playwright Page instance.
		include_validation: If True, also capture validation errors.

	Returns:
		ExtractionResult with all extracted fields and page metadata.
	"""
	assert page is not None, "page must not be None"

	# Step 1: Ensure helpers are injected
	await ensure_helpers(page)

	# Step 2: Extract raw field data
	raw_fields: list[dict] = await page.evaluate(_EXTRACT_FIELDS_JS)

	# Step 3: Extract button groups
	raw_button_groups: list[dict] = await page.evaluate(_EXTRACT_BUTTON_GROUPS_JS)

	# Step 4: Group radio/checkbox and convert to models
	fields = _group_radio_checkbox_fields(raw_fields)

	# Step 5: Add button groups (dedup by ff_id)
	seen_ids = {f.ff_id for f in fields}
	for bg in raw_button_groups:
		bg_id = bg.get("id", "")
		if bg_id and bg_id not in seen_ids:
			seen_ids.add(bg_id)
			fields.append(_raw_button_group_to_form_field(bg))

	# Step 6: Filter out Workday internal artifacts
	fields = [f for f in fields if normalize_name(f.label) != "items selected"]

	# Step 7: Mark synthetic labels and observation warnings
	for field in fields:
		if not field.label.strip():
			field.synthetic_label = True
			field.observation_warnings = [*field.observation_warnings, "missing_label"]

	# Step 8: Read page metadata
	meta: dict = await page.evaluate(_PAGE_METADATA_JS)

	# Step 9: Optionally capture validation errors
	validation = None
	if include_validation:
		validation = await capture_validation_errors(page, fields)

	result = ExtractionResult(
		fields=fields,
		page_title=meta.get("title", ""),
		page_url=meta.get("url", ""),
		form_count=meta.get("formCount", 0),
		has_submit_button=meta.get("hasSubmitButton", False),
	)
	if validation is not None:
		result.validation = validation

	_log_extraction_summary(result)
	return result


def _log_extraction_summary(result: ExtractionResult) -> None:
	"""Log a summary of what was extracted."""
	visible_count = sum(1 for f in result.fields if f.visible)
	required_count = sum(1 for f in result.fields if f.required)
	select_count = sum(1 for f in result.fields if f.field_type == "select")
	btn_group_count = sum(1 for f in result.fields if f.field_type == "button-group")
	logger.debug(
		"Extracted %d fields (%d visible, %d required, %d selects, %d button groups) "
		"from %s [%d forms, submit=%s]",
		len(result.fields),
		visible_count,
		required_count,
		select_count,
		btn_group_count,
		result.page_url,
		result.form_count,
		result.has_submit_button,
	)
