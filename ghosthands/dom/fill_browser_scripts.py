"""Browser-side JavaScript constants for DomHand form filling.

Extracted from ``domhand_fill.py`` to keep the orchestrator file focused on
Python logic.  Every constant is a raw JS string intended for
``page.evaluate()``.

Import pattern in ``domhand_fill.py``::

    from ghosthands.dom.fill_browser_scripts import (
        _FILL_FIELD_JS,
        _READ_FIELD_VALUE_JS,
        ...
    )
"""

from ghosthands.dom.dropdown_match import CLICK_DROPDOWN_OPTION_ENHANCED_JS

_PAGE_CONTEXT_SCAN_JS = r"""() => {
	const visible = (el) => {
		if (!el) return false;
		const style = window.getComputedStyle(el);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		const rect = el.getBoundingClientRect();
		return rect.width > 0 && rect.height > 0;
	};
	const normalize = (text) => (text || '').replace(/\s+/g, ' ').trim();
	const allText = Array.from(document.querySelectorAll('h1, h2, h3, button, a, [role="button"], label, p, span, div'))
		.filter((el) => visible(el))
		.map((el) => normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || ''))
		.filter(Boolean);
	const hasText = (patterns) => allText.some((text) => patterns.some((pattern) => pattern.test(text.toLowerCase())));
	const headingTexts = Array.from(document.querySelectorAll('h1, h2, h3, [role="heading"]'))
		.filter((el) => visible(el))
		.map((el) => normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || ''))
		.filter(Boolean)
		.slice(0, 5);
	const passwordCount = document.querySelectorAll('input[type="password"]').length;
	const emailCount = document.querySelectorAll('input[type="email"], input[name*="email" i], input[id*="email" i]').length;
	const confirmPasswordVisible =
		passwordCount >= 2 ||
		document.querySelector('[data-automation-id="verifyPassword"]') !== null;
	const createAccountSignals = hasText([/\bcreate account\b/, /\bregister\b/, /\bsign up\b/]);
	const signInSignals = hasText([/\bsign in\b/, /\blog in\b/, /\blogin\b/]);
	const startDialogSignals = hasText([/\bstart your application\b/, /\bautofill with resume\b/, /\bapply manually\b/, /\buse my last application\b/]);
	// Detect active stepper/wizard step for SPA multi-page forms
	let stepperLabel = '';
	const stepperSelectors = [
		'[aria-current="step"]',
		'[aria-current="page"]',
		'.apply-flow-stepper [aria-selected="true"]',
		'.apply-flow-pagination [aria-selected="true"]',
		'.apply-flow-stepper .active',
		'.apply-flow-pagination .active',
		'[role="tablist"] [aria-selected="true"]',
		'.stepper .step.active',
		'.stepper .step.current',
		'.progress-step.active',
		'.progress-step.current',
	];
	for (const sel of stepperSelectors) {
		const el = document.querySelector(sel);
		if (el && visible(el)) {
			const text = normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
			if (text && text.length <= 80) {
				stepperLabel = text;
				break;
			}
		}
	}
	if (!stepperLabel) {
		const stepPattern = /\b(?:step\s+\d+|page\s+\d+|\d+\s+of\s+\d+)\b/i;
		for (const text of allText) {
			if (stepPattern.test(text) && text.length <= 40) {
				stepperLabel = text;
				break;
			}
		}
	}

	let pageMarker = '';
	if (confirmPasswordVisible || createAccountSignals) {
		pageMarker = 'auth create account';
	} else if (emailCount >= 1 && passwordCount >= 1 && signInSignals) {
		pageMarker = 'auth native login';
	} else if (startDialogSignals) {
		pageMarker = 'auth entry';
	} else if (stepperLabel && headingTexts.length > 0) {
		pageMarker = stepperLabel + ' :: ' + headingTexts[0];
	} else if (stepperLabel) {
		pageMarker = stepperLabel;
	} else if (headingTexts.length > 0) {
		pageMarker = headingTexts[0];
	}
	return JSON.stringify({
		page_marker: pageMarker,
		stepper_label: stepperLabel,
		heading_texts: headingTexts,
	});
}"""

_EXTRACT_FIELDS_JS = r"""() => {
	var ff = window.__ff;
	if (!ff) return JSON.stringify([]);
	var seen = new Set();
	var out = [];

	var normalizeFieldText = function(text) {
		return (text || '').replace(/\s+/g, ' ').trim();
	};

	var cleanFieldLabelText = function(node) {
		if (!node) return '';
		var clone = node.cloneNode(true);
		clone.querySelectorAll(
			'input, textarea, select, button, ul, ol, li, [role="radio"], [role="checkbox"], [role="switch"], [role="textbox"], [role="combobox"], [role="option"], [role="listbox"], [role="dialog"], [role="grid"], [class*="desc"], [class*="sub"], [class*="hint"], .option-desc, small'
		).forEach(function(x) { x.remove(); });
		return normalizeFieldText(clone.textContent || '');
	};

	var firstMeaningfulWrapperLabel = function(wrapper) {
		if (!wrapper) return '';
		var candidates = [];
		var push = function(node) {
			if (!node) return;
			var text = cleanFieldLabelText(node);
			if (!text || isGenericSearchLabel(text) || candidates.indexOf(text) !== -1) return;
			candidates.push(text);
		};
		var pushPrevious = function(node) {
			if (!node || !node.previousElementSibling) return;
			var prev = node.previousElementSibling;
			if (
				prev.matches &&
				prev.matches('[data-automation-id="formField"], [data-automation-id*="formField"], fieldset, .form-group, .field')
			) {
				return;
			}
			push(prev);
		};
		push(wrapper.querySelector(':scope > legend'));
		push(wrapper.querySelector(':scope > [data-automation-id="fieldLabel"]'));
		push(wrapper.querySelector(':scope > [data-automation-id*="fieldLabel"]'));
		push(wrapper.querySelector(':scope > label'));
		push(wrapper.querySelector(':scope > [class*="question"]'));
		push(wrapper.querySelector('[data-automation-id="fieldLabel"]'));
		push(wrapper.querySelector('[data-automation-id*="fieldLabel"]'));
		push(wrapper.querySelector('label'));
		push(wrapper.querySelector('[class*="question"]'));
		pushPrevious(wrapper);
		var formField = ff.closestCrossRoot(
			wrapper,
			'[data-automation-id="formField"], [data-automation-id*="formField"]'
		);
		if (formField && formField !== wrapper) {
			push(formField.querySelector(':scope > legend'));
			push(formField.querySelector(':scope > [data-automation-id="fieldLabel"]'));
			push(formField.querySelector(':scope > [data-automation-id*="fieldLabel"]'));
			push(formField.querySelector(':scope > label'));
			push(formField.querySelector(':scope > [class*="question"]'));
			push(formField.querySelector('[data-automation-id="fieldLabel"]'));
			push(formField.querySelector('[data-automation-id*="fieldLabel"]'));
			push(formField.querySelector('label'));
			push(formField.querySelector('[class*="question"]'));
			pushPrevious(formField);
		}
		return candidates.length ? candidates[0] : '';
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
		return firstMeaningfulWrapperLabel(wrapper);
	};

	var shouldSkip = function(el) {
		if (ff.closestCrossRoot(el, '[class*="select-dropdown"], [class*="select-option"]')) return true;
		if (ff.closestCrossRoot(el, '.iti__dropdown-content')) return true;
		if (ff.closestCrossRoot(el, '[data-automation-id="activeListContainer"]')) return true;
		if (ff.closestCrossRoot(el, '[role="listbox"], [role="menu"]')) return true;
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
		return clone.textContent ? clone.textContent.trim() : '';
	};

	var cleanControlText = function(node) {
		if (!node) return '';
		var clone = node.cloneNode(true);
		clone.querySelectorAll(
			'input, textarea, select, button, [role="radio"], [role="checkbox"], [role="switch"], [class*="desc"], [class*="sub"], [class*="hint"], .option-desc, small'
		).forEach(function(x) { x.remove(); });
		return clone.textContent ? clone.textContent.replace(/\s+/g, ' ').trim() : '';
	};

	var getGroupQuestionLabel = function(el, itemLabel) {
		var itemNorm = (itemLabel || '').replace(/\s+/g, ' ').trim().toLowerCase();
		var container = ff.closestCrossRoot(
			el,
			'fieldset, [role="radiogroup"], [role="group"], .radio-group, .checkbox-group, .radio-cards, [data-automation-id="formField"], [data-automation-id*="formField"]'
		) || ff.rootParent(el) || el;

		var candidates = [];
		var push = function(node) {
			if (!node) return;
			var text = cleanControlText(node);
			if (!text) return;
			var norm = text.toLowerCase();
			if (itemNorm && (norm === itemNorm || norm === ('* ' + itemNorm) || norm === (itemNorm + ' *'))) return;
			if (text.length > 200) return;
			candidates.push(text);
		};

		push(container.querySelector(':scope > legend'));
		push(container.querySelector(':scope > [data-automation-id="fieldLabel"]'));
		push(container.querySelector(':scope > [data-automation-id*="fieldLabel"]'));
		push(container.querySelector(':scope > label'));
		push(container.querySelector(':scope > [class*="question"]'));
		push(container.previousElementSibling);

		var formField = ff.closestCrossRoot(container, '[data-automation-id="formField"], [data-automation-id*="formField"]');
		if (formField && formField !== container) {
			push(formField.querySelector('legend'));
			push(formField.querySelector('[data-automation-id="fieldLabel"]'));
			push(formField.querySelector('[data-automation-id*="fieldLabel"]'));
			push(formField.querySelector('label'));
			push(formField.querySelector('[class*="question"]'));
			push(formField.previousElementSibling);
		}

		for (var i = 0; i < candidates.length; i++) {
			if (candidates[i]) return candidates[i];
		}
		return '';
	};

	var getFieldWrapperMeta = function(el) {
		var wrapper = ff.closestCrossRoot(
			el,
			'[data-automation-id="formField"], [data-automation-id*="formField"], fieldset, .form-group, .field'
		) || el.parentElement || el;
		var cleanText = function(node) {
			if (!node) return '';
			var clone = node.cloneNode(true);
			clone.querySelectorAll(
				'input, textarea, select, button, ul, ol, li, [role="radio"], [role="checkbox"], [role="switch"], [role="textbox"], [role="combobox"], [role="option"], [role="listbox"], [role="dialog"], [role="grid"], [class*="desc"], [class*="sub"], [class*="hint"], .option-desc, small'
			).forEach(function(x) { x.remove(); });
			return clone.textContent ? clone.textContent.replace(/\s+/g, ' ').trim() : '';
		};
		var label = firstMeaningfulWrapperLabel(wrapper);
		var hasCalendarTrigger = !!wrapper.querySelector(
			'button[aria-label*="calendar" i], button[title*="calendar" i], [data-automation-id*="datePicker"], [data-automation-id*="dateIcon"], [data-automation-id*="dateTrigger"]'
		);
		var formatHint = '';
		var placeholders = [];
		var placeholderNodes = wrapper.querySelectorAll('input[placeholder]');
		for (var j = 0; j < placeholderNodes.length; j++) {
			var placeholder = (placeholderNodes[j].getAttribute('placeholder') || '').trim();
			if (!placeholder) continue;
			if (placeholders.indexOf(placeholder) === -1) placeholders.push(placeholder);
		}
		if (placeholders.length >= 2) {
			formatHint = placeholders.join('/');
		} else if (placeholders.length === 1) {
			formatHint = placeholders[0];
		}
		return {
			wrapperId: wrapper ? ff.tag(wrapper) : '',
			wrapperLabel: label,
			hasCalendarTrigger: hasCalendarTrigger,
			formatHint: formatHint
		};
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
			name_attr: el.getAttribute('name') || '',
			placeholder: el.getAttribute('placeholder') || el.getAttribute('aria-placeholder') || '',
			required: el.required || el.getAttribute('aria-required') === 'true' || el.dataset.required === 'true' || el.dataset.ffRequired === 'true',
			options: [],
			choices: [],
			accept: el.accept || null,
			is_native: isNative,
			is_multi_select: isMultiSelect || el.multiple || el.getAttribute('aria-multiselectable') === 'true',
			visible: true,
			raw_label: ff.getAccessibleName(el),
			synthetic_label: false,
			field_fingerprint: null,
			current_value: ''
		};
		var isSelectLike = type === 'select';
		var looksLikeOpaqueValue = function(text) {
			var value = (text || '').replace(/\s+/g, ' ').trim();
			if (!value) return false;
			return /^(?:[0-9a-f]{16,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/i.test(value);
		};
		var isUnsetLike = function(text) {
			var value = (text || '').replace(/\s+/g, ' ').trim();
			if (!value) return true;
			if (/^(select one|choose one|please select)$/i.test(value)) return true;
			if (/\b(select one|choose one|please select)\b/i.test(value)) return true;
			return looksLikeOpaqueValue(value);
		};
		var readVisibleSelectValue = function(node) {
			if (!node) return '';
			var localVisibleText = function(target) {
				if (!target) return '';
				var style = window.getComputedStyle(target);
				if (!style || style.visibility === 'hidden' || style.display === 'none') return '';
				var rect = target.getBoundingClientRect();
				if (!rect || rect.width === 0 || rect.height === 0) return '';
				return (target.textContent || '').replace(/\s+/g, ' ').trim();
			};
			var collectControlledIds = function(target) {
				var ids = [];
				var addIds = function(raw) {
					if (!raw) return;
					String(raw)
						.split(/\s+/)
						.map(function(part) { return part.trim(); })
						.filter(Boolean)
						.forEach(function(part) {
							if (ids.indexOf(part) === -1) ids.push(part);
						});
				};
				if (!target || !target.getAttribute) return ids;
				addIds(target.getAttribute('aria-controls'));
				addIds(target.getAttribute('aria-owns'));
				return ids;
			};
			var visibleTextWithoutPopups = function(target, controlledIds) {
				if (!target || !target.cloneNode) return '';
				var clone = target.cloneNode(true);
				var removeSelector = [
					'label',
					'legend',
					'small',
					'[aria-live]',
					'[role="listbox"]',
					'[role="menu"]',
					'[role="grid"]',
					'[role="tree"]',
					'[role="dialog"]',
					'[data-list]',
					'[data-automation-id="fieldLabel"]',
					'[data-automation-id*="fieldLabel"]',
					'[class*="hint"]',
					'[class*="Hint"]'
				].join(', ');
				var doomed = Array.from(clone.querySelectorAll(removeSelector));
				(controlledIds || []).forEach(function(id) {
					if (!id) return;
					var byId = clone.querySelector('#' + CSS.escape(id));
					if (byId) doomed.push(byId);
				});
				doomed.forEach(function(candidate) {
					if (candidate && candidate.remove) candidate.remove();
				});
				return (clone.textContent || '').replace(/\s+/g, ' ').trim();
			};
			var selectLikeWrapper = function(n) {
				if (!ff || !ff.closestCrossRoot) return n.parentElement || n;
				return (
					ff.closestCrossRoot(n, '.input-field-container') ||
					ff.closestCrossRoot(n, '.cx-select-container') ||
					ff.closestCrossRoot(n, '.input-row__control-container') ||
					ff.closestCrossRoot(n, '.input-row') ||
					ff.closestCrossRoot(
						n,
						'[data-automation-id="formField"], [data-automation-id*="formField"], .form-group, .field'
					) ||
					n.parentElement ||
					n
				);
			};
			if ((node.tagName === 'INPUT' || node.tagName === 'TEXTAREA') && typeof node.value === 'string') {
				var directValue = node.value.replace(/\s+/g, ' ').trim();
				if (directValue && !isUnsetLike(directValue)) return directValue;
			}
			var ownText = localVisibleText(node);
			if (ownText && !isUnsetLike(ownText)) return ownText;
			var wrapper = selectLikeWrapper(node);
			var controlledIds = collectControlledIds(node);
			var tokenSelectors = [
				'[data-automation-id*="selected"]',
				'[data-automation-id*="Selected"]',
				'[data-automation-id*="token"]',
				'[data-automation-id*="Token"]',
				'[class*="token"]',
				'[class*="Token"]',
				'[class*="pill"]',
				'[class*="Pill"]',
				'[class*="chip"]',
				'[class*="Chip"]',
				'[class*="tag"]',
				'[class*="Tag"]'
			];
			for (var si = 0; si < tokenSelectors.length; si++) {
				var tokenNodes = wrapper.querySelectorAll(tokenSelectors[si]);
				for (var sj = 0; sj < tokenNodes.length; sj++) {
					var tokenText = localVisibleText(tokenNodes[sj]);
					if (!tokenText || isUnsetLike(tokenText)) continue;
					return tokenText;
				}
			}
			var wrapperText = visibleTextWithoutPopups(wrapper, controlledIds);
			if (wrapperText && !isUnsetLike(wrapperText) && wrapperText.length <= 120) {
				return wrapperText;
			}
			var ariaLabel = (node.getAttribute('aria-label') || '').trim();
			if (ariaLabel && !isUnsetLike(ariaLabel)) return ariaLabel;
			return '';
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
					src = ff.closestCrossRoot(
						el,
						'[class*="cx-select"], [class*="select"], [class*="combobox"], .form-group, .field'
					);
				}
				if (!src) src = el;
				if (src) {
					opts = Array.from(
						src.querySelectorAll('[role="option"], [role="menuitem"], [role="gridcell"], [role="listitem"]')
					)
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
			entry.questionLabel = getGroupQuestionLabel(el, entry.itemLabel);
			entry.groupKey = el.name || el.getAttribute('name') || entry.questionLabel || entry.itemLabel || id;
			if (entry.questionLabel) {
				entry.name = entry.questionLabel;
				entry.raw_label = entry.questionLabel;
			}
		}

		if (el.tagName === 'SELECT') {
			var selOpt = el.options[el.selectedIndex];
			entry.current_value = selOpt ? selOpt.text.trim() : '';
		} else if (isSelectLike) {
			entry.current_value = readVisibleSelectValue(el);
		} else if (type === 'checkbox' || type === 'radio') {
			if (el.tagName === 'INPUT') entry.current_value = el.checked ? 'checked' : '';
			else entry.current_value = el.getAttribute('aria-checked') === 'true' ? 'checked' : '';
		} else if (el.getAttribute('role') === 'checkbox' || el.getAttribute('role') === 'switch') {
			entry.current_value = el.getAttribute('aria-checked') === 'true' ? 'checked' : '';
		} else {
			entry.current_value = el.value || (el.textContent ? el.textContent.trim() : '') || '';
		}

			var wrapperMeta = getFieldWrapperMeta(el);
			var usesSearchStyleLabel =
				type === 'search' ||
				(type === 'select' && el.getAttribute('data-uxi-widget-type') === 'selectinput');
			if (usesSearchStyleLabel && wrapperMeta.wrapperLabel) {
				var currentLabel = normalizeFieldText(entry.name || '');
				var placeholderLabel = normalizeFieldText(entry.placeholder || '');
				if (!currentLabel || isGenericSearchLabel(currentLabel) || (placeholderLabel && currentLabel.toLowerCase() === placeholderLabel.toLowerCase())) {
					entry.name = wrapperMeta.wrapperLabel;
					entry.raw_label = wrapperMeta.wrapperLabel;
					entry.synthetic_label = false;
				}
			}
			if (wrapperMeta.wrapperId) entry.wrapper_id = wrapperMeta.wrapperId;
			if (wrapperMeta.wrapperLabel) entry.wrapper_label = wrapperMeta.wrapperLabel;
		if (wrapperMeta.hasCalendarTrigger) entry.has_calendar_trigger = true;
		if (wrapperMeta.formatHint) entry.format_hint = wrapperMeta.formatHint;
		var labelNorm = (entry.name || '').replace(/\s+/g, ' ').trim().toLowerCase();
		if ((type === 'number' || type === 'text' || type === 'date') && (labelNorm === 'month' || labelNorm === 'day' || labelNorm === 'year')) {
			entry.date_component = labelNorm;
			entry.date_group_key = wrapperMeta.wrapperId || wrapperMeta.wrapperLabel || entry.section || '';
			if (wrapperMeta.wrapperLabel) entry.group_label = wrapperMeta.wrapperLabel;
		}

		out.push(entry);
	});
	return JSON.stringify(out);
}"""

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
		// TEXTAREA: many ATS UIs (Workday/React) listen for InputEvent; a plain Event('input') can
		// leave controlled state empty while the native value still displays.
		if (el.tagName === 'TEXTAREA') {
			try {
				el.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'insertFromPaste', data: String(value) }));
			} catch (e) {
				el.dispatchEvent(new Event('input', {bubbles: true}));
			}
		} else {
			el.dispatchEvent(new Event('input', {bubbles: true}));
		}
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
	var group = ff.closestCrossRoot(
		el,
		'fieldset, [role="radiogroup"], [role="group"], .radio-cards, .radio-group, .checkbox-group, [data-automation-id="formField"], [data-automation-id*="formField"]'
	) || ff.rootParent(el) || el;
	var items = group.querySelectorAll(
		'[role="radio"], [role="checkbox"], [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"], label[for], label.radio-card, .radio-card, .radio-option, label.checkbox-card, .checkbox-card, .checkbox-option, [role="button"], [role="cell"], input[type="radio"], input[type="checkbox"]'
	);
	var lower = text.toLowerCase().trim();
	var getLabelFor = function(item) {
		if (!item || !item.id) return null;
		return ff.queryOne('label[for="' + CSS.escape(item.id) + '"]');
	};
	var getClickable = function(item) {
		var byFor = getLabelFor(item);
		if (byFor) return byFor;
		return ff.closestCrossRoot(
			item,
			'button, label, [role="row"], [role="cell"], [role="button"], [role="radio"], [role="checkbox"], .radio-card, .radio-option, .checkbox-card, .checkbox-option, [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"], [data-automation-id*="radio"], [data-automation-id*="checkbox"]'
		) || item.parentElement || item;
	};
	var getText = function(item) {
		var byFor = getLabelFor(item);
		if (byFor && byFor.textContent) return byFor.textContent.trim();
		var labelEl = item.querySelector('[class*="label"], .rc-label');
		if (labelEl && labelEl.textContent) return labelEl.textContent.trim();
		var clickable = getClickable(item);
		return (clickable && clickable.textContent ? clickable.textContent : item.textContent || '').trim();
	};
	for (var i = 0; i < items.length; i++) {
		var item = items[i];
		var itemText = getText(item);
		var itemLower = itemText.toLowerCase();
		if (itemLower === lower || itemLower.includes(lower) || lower.includes(itemLower)) {
			var clickable = getClickable(item);
			if (clickable && clickable.scrollIntoView) {
				clickable.scrollIntoView({block: 'center', inline: 'center'});
			}
			if (clickable && clickable.focus) clickable.focus();
			try {
				var ptrOpts = { bubbles: true, cancelable: true, view: window, pointerType: 'mouse', isPrimary: true };
				clickable.dispatchEvent(new PointerEvent('pointerdown', ptrOpts));
				clickable.dispatchEvent(new PointerEvent('pointerup', ptrOpts));
			} catch(e) {}
			var mOpts = { bubbles: true, cancelable: true, view: window };
			clickable.dispatchEvent(new MouseEvent('mousedown', mOpts));
			clickable.dispatchEvent(new MouseEvent('mouseup', mOpts));
			clickable.click();
			return JSON.stringify({clicked: true, text: itemText});
		}
	}
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
			var clickTarget = radio.id ? ff.queryOne('label[for="' + CSS.escape(radio.id) + '"]') : null;
			(clickTarget || radio).click(); return JSON.stringify({clicked: true, alreadyChecked: false});
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

_CLICK_BINARY_FIELD_JS = r"""(ffId, desiredChecked) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({clicked: false, error: 'Element not found'});

	var getControl = function(node) {
		if (!node) return null;
		if (
			node.matches &&
			node.matches(
				'input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"], [role="switch"], [aria-checked], [aria-pressed], [aria-selected]'
			)
		) {
			return node;
		}
		return node.querySelector
			? node.querySelector(
				'input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"], [role="switch"], [aria-checked], [aria-pressed], [aria-selected]'
			)
			: null;
	};
	var getState = function(control) {
		if (!control) return null;
		if (control.tagName === 'INPUT' && (control.type === 'checkbox' || control.type === 'radio')) return control.checked;
		var ariaChecked = control.getAttribute('aria-checked');
		if (ariaChecked === 'true') return true;
		if (ariaChecked === 'false') return false;
		var ariaPressed = control.getAttribute('aria-pressed');
		if (ariaPressed === 'true') return true;
		if (ariaPressed === 'false') return false;
		var ariaSelected = control.getAttribute('aria-selected');
		if (ariaSelected === 'true') return true;
		if (ariaSelected === 'false') return false;
		return null;
	};
	var dispatchClickSequence = function(node) {
		if (!node) return;
		var mouseOpts = { bubbles: true, cancelable: true, view: window };
		node.dispatchEvent(new MouseEvent('mouseover', mouseOpts));
		node.dispatchEvent(new MouseEvent('mouseenter', mouseOpts));
		node.dispatchEvent(new MouseEvent('mousedown', mouseOpts));
		node.dispatchEvent(new MouseEvent('mouseup', mouseOpts));
		node.dispatchEvent(new MouseEvent('click', mouseOpts));
	};
	var control = getControl(el) || el;
	var wrapper = ff.closestCrossRoot(
		control,
		'label, [role="row"], [role="cell"], [role="button"], .checkbox-card, .checkbox-option, .radio-card, .radio-option, [data-automation-id*="checkbox"], [data-automation-id*="radio"], [data-automation-id*="promptOption"]'
	) || control.parentElement || control;

	var currentState = getState(control);
	if (currentState === desiredChecked) {
		return JSON.stringify({clicked: true, alreadyChecked: true});
	}

	if (wrapper && wrapper.scrollIntoView) {
		wrapper.scrollIntoView({block: 'center', behavior: 'instant'});
	}
	if (wrapper && wrapper !== control) {
		if (wrapper.focus) wrapper.focus();
		if (wrapper.click) wrapper.click();
		dispatchClickSequence(wrapper);
		if (getState(control) === desiredChecked) {
			return JSON.stringify({clicked: true, strategy: 'wrapper'});
		}
	}

	if (control.focus) control.focus();
	if (control.click) control.click();
	dispatchClickSequence(control);
	if (control.tagName === 'INPUT' && (control.type === 'checkbox' || control.type === 'radio')) {
		control.dispatchEvent(new Event('input', {bubbles: true}));
		control.dispatchEvent(new Event('change', {bubbles: true}));
	}
	if (getState(control) === desiredChecked) {
		return JSON.stringify({clicked: true, strategy: 'control'});
	}

	return JSON.stringify({clicked: true, strategy: 'unconfirmed'});
}"""

_GET_BINARY_CLICK_TARGET_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({found: false, error: 'Element not found'});

	var getControl = function(node) {
		if (!node) return null;
		if (
			node.matches &&
			node.matches(
				'input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"], [role="switch"], [aria-checked], [aria-pressed], [aria-selected]'
			)
		) {
			return node;
		}
		return node.querySelector
			? node.querySelector(
				'input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"], [role="switch"], [aria-checked], [aria-pressed], [aria-selected]'
			)
			: null;
	};
	var control = getControl(el) || el;
	var byFor = control && control.id ? ff.queryOne('label[for="' + CSS.escape(control.id) + '"]') : null;
	var target = ff.closestCrossRoot(
		byFor || control,
		'label, [role="row"], [role="cell"], [role="button"], .checkbox-card, .checkbox-option, .radio-card, .radio-option, [data-automation-id*="checkbox"], [data-automation-id*="radio"], [data-automation-id*="promptOption"]'
	) || byFor || control.parentElement || control;
	if (target && target.scrollIntoView) {
		target.scrollIntoView({block: 'center', inline: 'center'});
	}
	var rect = target && target.getBoundingClientRect ? target.getBoundingClientRect() : null;
	if (!rect || rect.width === 0 || rect.height === 0) {
		if (control && control.scrollIntoView) {
			control.scrollIntoView({block: 'center', inline: 'center'});
		}
		rect = control && control.getBoundingClientRect ? control.getBoundingClientRect() : null;
		target = control;
	}
	if (!rect || rect.width === 0 || rect.height === 0) {
		return JSON.stringify({found: false, error: 'No visible click target'});
	}
	return JSON.stringify({
		found: true,
		text: ((target && target.textContent) || (control && control.getAttribute && control.getAttribute('aria-label')) || '').trim(),
		x: Math.round(rect.left + (rect.width / 2)),
		y: Math.round(rect.top + (rect.height / 2))
	});
}"""

_READ_GROUP_SELECTION_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({selected: ''});
	var exactGroupSelectors =
		'ul.cx-select-pills-container, .oracle-pill-group, fieldset, [role="radiogroup"], [role="group"], .radio-group, .radio-cards, .checkbox-group';
	var broadGroupSelectors =
		exactGroupSelectors + ', [data-automation-id="formField"], [data-automation-id*="formField"], .input-row__control-container';
	var resolveGroup = function(node) {
		if (!node) return null;
		var descendant = node.querySelector ? node.querySelector(exactGroupSelectors) : null;
		if (descendant) return descendant;
		if (node.matches && node.matches(exactGroupSelectors)) return node;
		var ancestor = ff.closestCrossRoot(node, exactGroupSelectors);
		if (ancestor) return ancestor;
		var broad = ff.closestCrossRoot(node, broadGroupSelectors);
		if (broad) {
			descendant = broad.querySelector ? broad.querySelector(exactGroupSelectors) : null;
			if (descendant) return descendant;
			if (broad.matches && broad.matches(exactGroupSelectors)) return broad;
			return broad;
		}
		return ff.rootParent(node) || node;
	};
	var group = resolveGroup(el);
	var nodes = Array.from(
		group.querySelectorAll(
			'[role="radio"], [role="checkbox"], input[type="radio"], input[type="checkbox"], label.radio-card, .radio-card, .radio-option, label.checkbox-card, .checkbox-card, .checkbox-option, button, [role="button"], [role="cell"], [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"]'
		)
	);
	var seen = new Set();
	var filtered = [];
	for (var i = 0; i < nodes.length; i++) {
		var node = nodes[i];
		if (seen.has(node)) continue;
		seen.add(node);
		filtered.push(node);
	}
	var getLabel = function(node) {
		if (!node) return '';
		if (node.id) {
			var byFor = ff.queryOne('label[for="' + CSS.escape(node.id) + '"]');
			if (byFor && byFor.textContent && byFor.textContent.trim()) return byFor.textContent.trim();
		}
		var ariaLabel = node.getAttribute('aria-label');
		if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
		var wrap = ff.closestCrossRoot(
			node,
			'label, [role="radio"], [role="checkbox"], .radio-card, .radio-option, .checkbox-card, .checkbox-option, [role="button"], [role="cell"], [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"]'
		) || node;
		return (wrap.textContent || '').trim();
	};
	var isSelected = function(node) {
		if (!node) return false;
		if (node.matches && node.matches('input[type="radio"], input[type="checkbox"]') && node.checked) return true;
		var ariaChecked = node.getAttribute && node.getAttribute('aria-checked');
		if (ariaChecked === 'true') return true;
		var ariaPressed = node.getAttribute && node.getAttribute('aria-pressed');
		if (ariaPressed === 'true') return true;
		var ariaSelected = node.getAttribute && node.getAttribute('aria-selected');
		if (ariaSelected === 'true') return true;
		var nested = node.querySelector && node.querySelector('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]');
		if (nested) {
			if (nested.matches && nested.matches('input[type="radio"], input[type="checkbox"]') && nested.checked) return true;
			if (nested.getAttribute && nested.getAttribute('aria-checked') === 'true') return true;
		}
		var className = typeof node.className === 'string' ? node.className : '';
		return /\b(selected|checked|active)\b/i.test(className);
	};
	for (var j = 0; j < filtered.length; j++) {
		var candidate = filtered[j];
		if (isSelected(candidate)) return JSON.stringify({selected: getLabel(candidate)});
	}
	return JSON.stringify({selected: ''});
}"""

_GET_GROUP_OPTION_TARGET_JS = r"""(ffId, text) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({found: false, error: 'Element not found'});
	var exactGroupSelectors =
		'ul.cx-select-pills-container, .oracle-pill-group, fieldset, [role="radiogroup"], [role="group"], .radio-group, .radio-cards, .checkbox-group';
	var broadGroupSelectors =
		exactGroupSelectors + ', [data-automation-id="formField"], [data-automation-id*="formField"], .input-row__control-container';
	var resolveGroup = function(node) {
		if (!node) return null;
		var descendant = node.querySelector ? node.querySelector(exactGroupSelectors) : null;
		if (descendant) return descendant;
		if (node.matches && node.matches(exactGroupSelectors)) return node;
		var ancestor = ff.closestCrossRoot(node, exactGroupSelectors);
		if (ancestor) return ancestor;
		var broad = ff.closestCrossRoot(node, broadGroupSelectors);
		if (broad) {
			descendant = broad.querySelector ? broad.querySelector(exactGroupSelectors) : null;
			if (descendant) return descendant;
			if (broad.matches && broad.matches(exactGroupSelectors)) return broad;
			return broad;
		}
		return ff.rootParent(node) || node;
	};
	var group = resolveGroup(el);
	var nodes = Array.from(
		group.querySelectorAll(
			'[role="radio"], [role="checkbox"], input[type="radio"], input[type="checkbox"], label[for], label.radio-card, .radio-card, .radio-option, label.checkbox-card, .checkbox-card, .checkbox-option, button, [role="button"], [role="cell"], [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"]'
		)
	);
	var lower = text.toLowerCase().trim();
	var best = null;
	var getLabelFor = function(node) {
		if (!node || !node.id) return null;
		return ff.queryOne('label[for="' + CSS.escape(node.id) + '"]');
	};
	var getClickable = function(node) {
		var byFor = getLabelFor(node);
		if (byFor) return byFor;
		return ff.closestCrossRoot(
			node,
			'button, label, [role="row"], [role="cell"], [role="button"], [role="radio"], [role="checkbox"], .radio-card, .radio-option, .checkbox-card, .checkbox-option, [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"], [data-automation-id*="radio"], [data-automation-id*="checkbox"]'
		) || node.parentElement || node;
	};
	var getLabel = function(node) {
		if (!node) return '';
		var byFor = getLabelFor(node);
		if (byFor && byFor.textContent && byFor.textContent.trim()) return byFor.textContent.trim();
		var ariaLabel = node.getAttribute('aria-label');
		if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
		var wrap = getClickable(node);
		return (wrap.textContent || '').trim();
	};
	for (var i = 0; i < nodes.length; i++) {
		var node = nodes[i];
		var label = getLabel(node);
		if (!label) continue;
		var labelLower = label.toLowerCase().trim();
		var score = 0;
		if (labelLower === lower) score = 3;
		else if (labelLower.includes(lower) || lower.includes(labelLower)) score = 2;
		if (score === 0) continue;
		var clickable = getClickable(node);
		if (clickable && clickable.scrollIntoView) {
			clickable.scrollIntoView({block: 'center', inline: 'center'});
		}
		var rect = clickable.getBoundingClientRect();
		if (!rect || rect.width === 0 || rect.height === 0) continue;
		if (!best || score > best.score) {
			best = {
				found: true,
				score: score,
				text: label,
				optionFfId: (clickable && clickable.getAttribute && clickable.getAttribute('data-ff-id')) || '',
				x: Math.round(rect.left + (rect.width / 2)),
				y: Math.round(rect.top + (rect.height / 2)),
			};
		}
	}
	return JSON.stringify(best || {found: false, error: 'No matching option'});
}"""

_HAS_FIELD_VALIDATION_ERROR_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify(false);
	var nodes = [];
	var seen = new Set();
	var push = function(node) {
		if (!node || seen.has(node)) return;
		seen.add(node);
		nodes.push(node);
	};
	push(el);
	push(ff.closestCrossRoot(el, '[aria-invalid], [role="group"], [role="radiogroup"], fieldset, label, [role="row"], [role="cell"], .form-group, .field, [data-automation-id*="formField"]'));
	if (el.querySelector) {
		push(el.querySelector('[aria-invalid], input, textarea, select, [role="checkbox"], [role="radio"], [role="switch"], [role="textbox"], [role="combobox"]'));
	}
	for (var i = 0; i < nodes.length; i++) {
		var node = nodes[i];
		if (!node) continue;
		if (node.getAttribute && node.getAttribute('aria-invalid') === 'true') return JSON.stringify(true);
		if (node.querySelector && node.querySelector('[aria-invalid="true"]')) return JSON.stringify(true);
	}
	return JSON.stringify(false);
}"""

_READ_FIELD_VALUE_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify('');
	var looksLikeOpaqueValue = function(text) {
		var value = (text || '').replace(/\s+/g, ' ').trim();
		if (!value) return false;
		return /^(?:[0-9a-f]{16,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/i.test(value);
	};
	var isUnsetLike = function(text) {
		var value = (text || '').replace(/\s+/g, ' ').trim();
		if (!value) return true;
		if (/^(select one|choose one|please select)$/i.test(value)) return true;
		if (/\b(select one|choose one|please select)\b/i.test(value)) return true;
		return looksLikeOpaqueValue(value);
	};
	var visibleText = function(node) {
		if (!node) return '';
		var style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return '';
		var rect = node.getBoundingClientRect();
		if (!rect || rect.width === 0 || rect.height === 0) return '';
		return (node.textContent || '').replace(/\s+/g, ' ').trim();
	};
	var collectControlledIds = function(node) {
		var ids = [];
		var addIds = function(raw) {
			if (!raw) return;
			String(raw)
				.split(/\s+/)
				.map(function(part) { return part.trim(); })
				.filter(Boolean)
				.forEach(function(part) {
					if (ids.indexOf(part) === -1) ids.push(part);
				});
		};
		if (!node || !node.getAttribute) return ids;
		addIds(node.getAttribute('aria-controls'));
		addIds(node.getAttribute('aria-owns'));
		return ids;
	};
	var hasVisibleControlledPopup = function(ids) {
		var list = Array.isArray(ids) ? ids : [];
		for (var i = 0; i < list.length; i++) {
			var controlled = ff && ff.getByDomId ? ff.getByDomId(list[i]) : document.getElementById(list[i]);
			if (!controlled) continue;
			var style = window.getComputedStyle(controlled);
			if (!style || style.visibility === 'hidden' || style.display === 'none') continue;
			var rect = controlled.getBoundingClientRect();
			if (rect && rect.width > 0 && rect.height > 0) return true;
		}
		return false;
	};
	var visibleTextWithoutPopups = function(node, controlledIds) {
		if (!node || !node.cloneNode) return '';
		var clone = node.cloneNode(true);
		var removeSelector = [
			'label',
			'legend',
			'small',
			'[aria-live]',
			'[role="listbox"]',
			'[role="menu"]',
			'[role="grid"]',
			'[role="tree"]',
			'[role="dialog"]',
			'[data-list]',
			'[data-automation-id="fieldLabel"]',
			'[data-automation-id*="fieldLabel"]',
			'[class*="hint"]',
			'[class*="Hint"]'
		].join(', ');
		var doomed = Array.from(clone.querySelectorAll(removeSelector));
		(controlledIds || []).forEach(function(id) {
			if (!id) return;
			var byId = clone.querySelector('#' + CSS.escape(id));
			if (byId) doomed.push(byId);
		});
		doomed.forEach(function(candidate) {
			if (candidate && candidate.remove) candidate.remove();
		});
		return (clone.textContent || '').replace(/\s+/g, ' ').trim();
	};
	var comboHost = (ff && ff.closestCrossRoot)
		? ff.closestCrossRoot(el, '[role="combobox"]')
		: null;
	if (comboHost === el) {
		comboHost = null;
	}
	var isSelectLike = el.getAttribute('role') === 'combobox'
		|| el.getAttribute('role') === 'listbox'
		|| el.getAttribute('data-uxi-widget-type') === 'selectinput'
		|| el.getAttribute('aria-haspopup') === 'listbox'
		|| el.getAttribute('aria-haspopup') === 'grid';
	if (!isSelectLike && comboHost) {
		isSelectLike = true;
	}
	if (el.tagName === 'SELECT') {
		var selOpt = el.options[el.selectedIndex];
		return JSON.stringify(selOpt ? (selOpt.textContent || '').trim() : '');
	}
	var committedAttr = (el.getAttribute && el.getAttribute('data-committed-value')) || '';
	if (committedAttr && !isUnsetLike(committedAttr)) {
		return JSON.stringify(committedAttr.trim());
	}
	if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && !isSelectLike) {
		var directValue = (el.value || '').trim();
		if (directValue) return JSON.stringify(directValue);
	}
	if (isSelectLike) {
		var comboControlledIds = collectControlledIds(el);
		if (comboHost && comboHost !== el) {
			collectControlledIds(comboHost).forEach(function(id) {
				if (comboControlledIds.indexOf(id) === -1) comboControlledIds.push(id);
			});
		}
		var comboPopupVisible = hasVisibleControlledPopup(comboControlledIds);
		var comboAriaExpanded = el.getAttribute('aria-expanded') === 'true'
			|| (comboHost && comboHost.getAttribute && comboHost.getAttribute('aria-expanded') === 'true');
		var comboPopupOpen = comboPopupVisible || (!comboControlledIds.length && comboAriaExpanded);
		var comboInput = null;
		if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.getAttribute('role') === 'combobox') {
			comboInput = el;
		} else if ((comboHost && (comboHost.tagName === 'INPUT' || comboHost.tagName === 'TEXTAREA'))) {
			comboInput = comboHost;
		} else if (el.querySelector) {
			comboInput = el.querySelector('[role="combobox"], input, textarea');
		}
		if (comboInput && typeof comboInput.value === 'string') {
			var comboDirect = comboInput.value.trim();
			if (comboDirect && !isUnsetLike(comboDirect) && !comboPopupOpen) {
				return JSON.stringify(comboDirect);
			}
		}
		var comboParent = comboInput ? comboInput.parentElement : el.parentElement;
		if (comboParent && comboParent.getAttribute) {
			var dataAttrVal = (comboParent.getAttribute('data-value') || '').trim();
			if (dataAttrVal && !isUnsetLike(dataAttrVal) && !comboPopupOpen) {
				return JSON.stringify(dataAttrVal);
			}
		}
		var rsControl = null;
		var rsAnchor = comboInput || comboHost || el;
		if (ff && ff.closestCrossRoot) {
			rsControl = ff.closestCrossRoot(rsAnchor, '.select__control') || ff.closestCrossRoot(rsAnchor, '.select-shell');
		}
		if (!rsControl && rsAnchor.closest) {
			rsControl = rsAnchor.closest('.select__control') || rsAnchor.closest('.select-shell');
		}
		if (rsControl && rsControl.querySelector) {
			var rsSingle = rsControl.querySelector('.select__single-value');
			if (rsSingle) {
				var rsSingleTxt = visibleText(rsSingle);
				if (rsSingleTxt && !isUnsetLike(rsSingleTxt)) {
					return JSON.stringify(rsSingleTxt);
				}
			}
		}
	}
	var value = '';
	if (!isSelectLike && typeof el.value === 'string' && el.value.trim()) {
		value = el.value.trim();
	}
	if (!value && isSelectLike) {
		var fieldAnchor = comboHost || el;
		var controlledIds = collectControlledIds(el);
		if (comboHost && comboHost !== el) {
			collectControlledIds(comboHost).forEach(function(id) {
				if (controlledIds.indexOf(id) === -1) controlledIds.push(id);
			});
		}
		var ownText = visibleText(el);
		if (ownText && !isUnsetLike(ownText)) {
			value = ownText;
		}
		var wrapper =
			(ff && ff.closestCrossRoot && ff.closestCrossRoot(fieldAnchor, '.input-field-container')) ||
			(ff && ff.closestCrossRoot && ff.closestCrossRoot(fieldAnchor, '.cx-select-container')) ||
			(ff && ff.closestCrossRoot && ff.closestCrossRoot(fieldAnchor, '.input-row__control-container')) ||
			(ff && ff.closestCrossRoot && ff.closestCrossRoot(fieldAnchor, '.input-row')) ||
			(ff && ff.closestCrossRoot
				? ff.closestCrossRoot(
					fieldAnchor,
					'[data-automation-id="formField"], [data-automation-id*="formField"], .form-group, .field'
				)
				: null) ||
			fieldAnchor.parentElement ||
			fieldAnchor;
		var tokenSelectors = [
			'[data-automation-id*="selected"]',
			'[data-automation-id*="Selected"]',
			'[data-automation-id*="token"]',
			'[data-automation-id*="Token"]',
			'[class*="token"]',
			'[class*="Token"]',
			'[class*="pill"]',
			'[class*="Pill"]',
			'[class*="chip"]',
			'[class*="Chip"]',
			'[class*="tag"]',
			'[class*="Tag"]'
		];
		for (var i = 0; i < tokenSelectors.length && !value; i++) {
			var tokenNodes = wrapper.querySelectorAll(tokenSelectors[i]);
			for (var j = 0; j < tokenNodes.length; j++) {
				var tokenText = visibleText(tokenNodes[j]);
				if (!tokenText) continue;
				if (isUnsetLike(tokenText) || /^required$/i.test(tokenText)) continue;
				value = tokenText;
				break;
			}
		}
		// Oracle JET / Fusion LOV: selected label is often outside the inner <input> DomHand tags.
		if (!value && ff) {
			var scanTargets = [];
			var addT = function(n) {
				if (n && scanTargets.indexOf(n) === -1) scanTargets.push(n);
			};
			addT(fieldAnchor);
			addT(wrapper);
			var jetSel = [
				'[class*="oj-text-field-middle"]',
				'[class*="TextFieldMiddle"]',
				'[class*="oj-text-field-container"]',
				'[class*="oj-text-field"]',
				'[role="textbox"][aria-readonly="true"]'
			];
			for (var si = 0; si < scanTargets.length && !value; si++) {
				var st = scanTargets[si];
				if (!st || !st.querySelectorAll) continue;
				for (var ej = 0; ej < jetSel.length && !value; ej++) {
					var hits = st.querySelectorAll(jetSel[ej]);
					for (var hk = 0; hk < hits.length; hk++) {
						var tx = visibleTextWithoutPopups(hits[hk], controlledIds);
						if (!tx || isUnsetLike(tx) || /^required$/i.test(tx) || tx.length > 120) continue;
						value = tx;
						break;
					}
				}
			}
		}
		if (!value) {
			var wrapperText = visibleTextWithoutPopups(wrapper, controlledIds);
			if (wrapperText && !isUnsetLike(wrapperText) && !/^required$/i.test(wrapperText) && wrapperText.length <= 120) {
				value = wrapperText;
			}
		}
	}
	if (!value) {
		var ariaLabel = el.getAttribute('aria-label');
		if (ariaLabel && ariaLabel.trim() && !isUnsetLike(ariaLabel)) {
			value = ariaLabel.trim();
		}
	}
	if (!value && el.textContent) {
		value = el.textContent.trim();
	}
	if (isUnsetLike(value)) value = '';
	return JSON.stringify(value || '');
}"""

_CLICK_BUTTON_GROUP_JS = r"""(ffId, text) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (!el) return JSON.stringify({clicked: false, error: 'Element not found'});
	var exactGroupSelectors =
		'ul.cx-select-pills-container, .oracle-pill-group, fieldset, [role="radiogroup"], [role="group"], .radio-group, .radio-cards, .checkbox-group';
	var broadGroupSelectors =
		exactGroupSelectors + ', [data-automation-id="formField"], [data-automation-id*="formField"], .input-row__control-container';
	var resolveGroup = function(node) {
		if (!node) return null;
		var descendant = node.querySelector ? node.querySelector(exactGroupSelectors) : null;
		if (descendant) return descendant;
		if (node.matches && node.matches(exactGroupSelectors)) return node;
		var ancestor = ff.closestCrossRoot(node, exactGroupSelectors);
		if (ancestor) return ancestor;
		var broad = ff.closestCrossRoot(node, broadGroupSelectors);
		if (broad) {
			descendant = broad.querySelector ? broad.querySelector(exactGroupSelectors) : null;
			if (descendant) return descendant;
			if (broad.matches && broad.matches(exactGroupSelectors)) return broad;
			return broad;
		}
		return ff.rootParent(node) || node;
	};
	var group = resolveGroup(el);
	var nodes = Array.from(
		group.querySelectorAll(
			'[role="radio"], [role="checkbox"], input[type="radio"], input[type="checkbox"], label[for], label.radio-card, .radio-card, .radio-option, label.checkbox-card, .checkbox-card, .checkbox-option, button, [role="button"], [role="cell"], [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"]'
		)
	);
	var lower = text.toLowerCase().trim();
	var getLabelFor = function(node) {
		if (!node || !node.id) return null;
		return ff.queryOne('label[for="' + CSS.escape(node.id) + '"]');
	};
	var getClickable = function(node) {
		var byFor = getLabelFor(node);
		if (byFor) return byFor;
		return ff.closestCrossRoot(
			node,
			'button, label, [role="row"], [role="cell"], [role="button"], [role="radio"], [role="checkbox"], .radio-card, .radio-option, .checkbox-card, .checkbox-option, .cx-select-pill-section, .oracle-pill-group li, [data-automation-id*="promptOption"], [data-automation-id*="PromptOption"], [data-automation-id*="radio"], [data-automation-id*="checkbox"]'
		) || node.parentElement || node;
	};
	var getText = function(node) {
		var byFor = getLabelFor(node);
		if (byFor && byFor.textContent) return byFor.textContent.trim();
		var ariaLabel = node.getAttribute && node.getAttribute('aria-label');
		if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
		var labelEl = node.querySelector ? node.querySelector('[class*="label"], .rc-label') : null;
		if (labelEl && labelEl.textContent) return labelEl.textContent.trim();
		var clickable = getClickable(node);
		return (clickable && clickable.textContent ? clickable.textContent : node.textContent || '').trim();
	};
	var dispatchClickSequence = function(node) {
		if (!node || !node.dispatchEvent) return;
		var mouseOpts = { bubbles: true, cancelable: true, view: window };
		try {
			var ptrOpts = { bubbles: true, cancelable: true, view: window, pointerType: 'mouse', isPrimary: true };
			node.dispatchEvent(new PointerEvent('pointerover', ptrOpts));
			node.dispatchEvent(new PointerEvent('pointerenter', Object.assign({}, ptrOpts, {bubbles: false})));
			node.dispatchEvent(new PointerEvent('pointerdown', ptrOpts));
			node.dispatchEvent(new PointerEvent('pointerup', ptrOpts));
		} catch(e) {}
		node.dispatchEvent(new MouseEvent('mouseover', mouseOpts));
		node.dispatchEvent(new MouseEvent('mouseenter', mouseOpts));
		node.dispatchEvent(new MouseEvent('mousedown', mouseOpts));
		node.dispatchEvent(new MouseEvent('mouseup', mouseOpts));
		node.dispatchEvent(new MouseEvent('click', mouseOpts));
	};
	var best = null;
	for (var i = 0; i < nodes.length; i++) {
		var node = nodes[i];
		var itemText = getText(node);
		if (!itemText) continue;
		var itemLower = itemText.toLowerCase().trim();
		var score = 0;
		if (itemLower === lower) score = 3;
		else if (itemLower.includes(lower) || lower.includes(itemLower)) score = 2;
		if (score === 0) continue;
		if (!best || score > best.score) {
			best = { node: node, text: itemText, score: score };
			if (score === 3) break;
		}
	}
	if (!best) return JSON.stringify({clicked: false, error: 'No matching button'});
	var clickable = getClickable(best.node);
	if (clickable && clickable.scrollIntoView) {
		clickable.scrollIntoView({block: 'center', inline: 'center'});
	}
	try {
		if (clickable && clickable.focus) clickable.focus();
		if (clickable && clickable.click) clickable.click();
		dispatchClickSequence(clickable);
	} catch (e) {}
	return JSON.stringify({clicked: true, text: best.text});
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

# Prefer after react-select option pick: synthetic body clicks can race the commit and clear selection.
_DISMISS_DROPDOWN_SOFT_JS = r"""() => {
	var ae = document.activeElement;
	if (ae && ae.blur) ae.blur();
	document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true}));
	return 'ok';
}"""

_COLLAPSE_COMBOBOX_FOR_FF_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return 'no_el';
	var combo = (ff && ff.closestCrossRoot)
		? ff.closestCrossRoot(el, '[role="combobox"]')
		: (el.closest ? el.closest('[role="combobox"]') : null);
	if (!combo) return 'no_combo';
	if (combo.getAttribute('aria-expanded') !== 'true') return 'closed';
	try { combo.focus(); } catch (e) {}
	combo.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true}));
	return 'escape';
}"""

_CLICK_OTHER_TEXTLIKE_FIELD_JS = r"""(ffId) => {
	var ff = window.__ff;
	var current = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	var isVisible = function(node) {
		if (!node) return false;
		var style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		var rect = node.getBoundingClientRect();
		return !!rect && rect.width > 0 && rect.height > 0;
	};
	var sameFamily = function(a, b) {
		if (!a || !b || !a.contains || !b.contains) return false;
		return a === b || a.contains(b) || b.contains(a);
	};
	var selectors = [
		'input:not([type="hidden"]):not([disabled])',
		'textarea:not([disabled])',
		'select:not([disabled])',
		'[role="textbox"]',
		'[role="combobox"]',
		'[contenteditable="true"]'
	].join(', ');
	var nodes = ff && ff.queryAll ? ff.queryAll(selectors) : Array.from(document.querySelectorAll(selectors));
	for (var i = 0; i < nodes.length; i++) {
		var node = nodes[i];
		if (!node || sameFamily(current, node) || !isVisible(node)) continue;
		try {
			if (node.scrollIntoView) node.scrollIntoView({block: 'center', inline: 'center'});
			node.click();
			return JSON.stringify({clicked: true, fieldId: node.getAttribute('data-ff-id') || ''});
		} catch (e) {}
	}
	if (current && current.blur) current.blur();
	document.body.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
	document.body.dispatchEvent(new MouseEvent('click', {bubbles: true}));
	return JSON.stringify({clicked: false});
}"""

_FOCUS_AND_CLEAR_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return JSON.stringify({ok: false, error: 'not found'});
	el.focus();
	if (el.select) el.select();
	return JSON.stringify({ok: true});
}"""

_FOCUS_FIELD_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return JSON.stringify({ok: false, error: 'not found'});
	if (el.focus) el.focus();
	return JSON.stringify({ok: true});
}"""

_SCROLL_FF_INTO_VIEW_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) return JSON.stringify({ok: false, error: 'not found'});
	if (el.scrollIntoView) {
		el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
	}
	return JSON.stringify({ok: true});
}"""

_CLICK_ALTERNATE_FIELD_JS = r"""(ffId) => {
	var ff = window.__ff;
	var current = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!current) return JSON.stringify({clicked: false, error: 'current_not_found'});
	var isVisible = function(node) {
		if (!node) return false;
		var style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		var rect = node.getBoundingClientRect();
		return !!rect && rect.width > 0 && rect.height > 0;
	};
	var candidates = ff ? ff.queryAll(ff.SELECTOR) : Array.from(document.querySelectorAll('[data-ff-id]'));
	for (var i = 0; i < candidates.length; i++) {
		var node = candidates[i];
		if (!node || node === current) continue;
		var nodeId = (node.getAttribute && node.getAttribute('data-ff-id')) || '';
		if (nodeId && nodeId === ffId) continue;
		if (!isVisible(node)) continue;
		var clickable = (ff && ff.closestCrossRoot)
			? ff.closestCrossRoot(node, 'input, textarea, select, button, [role="textbox"], [role="combobox"], [role="spinbutton"]')
			: null;
		if (!clickable) clickable = node;
		if (!isVisible(clickable)) continue;
		if (clickable.scrollIntoView) {
			clickable.scrollIntoView({block: 'center', inline: 'center'});
		}
		if (clickable.click) clickable.click();
		if (clickable.focus) clickable.focus();
		return JSON.stringify({clicked: true, target_id: nodeId || '', target_tag: clickable.tagName || ''});
	}
	return JSON.stringify({clicked: false, error: 'no_alternate_visible_field'});
}"""

_OPEN_GROUPED_DATE_PICKER_JS = r"""(ffId) => {
	var ff = window.__ff;
	var container = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!container) return JSON.stringify({clicked: false, opened: false, error: 'container_not_found'});
	var all = Array.from(container.querySelectorAll('button, [role="button"], [aria-haspopup]'));
	var trigger = null;
	for (var i = 0; i < all.length; i++) {
		var node = all[i];
		var text = ((node.getAttribute('aria-label') || '') + ' ' + (node.getAttribute('title') || '') + ' ' + (node.textContent || '')).toLowerCase();
		if (text.indexOf('calendar') !== -1 || text.indexOf('date') !== -1) {
			trigger = node;
			break;
		}
	}
	if (!trigger) {
		trigger = container.querySelector('[data-automation-id*="datePicker"], [data-automation-id*="dateIcon"], [data-automation-id*="dateTrigger"]');
	}
	if (!trigger) return JSON.stringify({clicked: false, opened: false, error: 'trigger_not_found'});
	if (trigger.scrollIntoView) trigger.scrollIntoView({block: 'center', inline: 'center'});
	if (trigger.click) trigger.click();
	var dialog = document.querySelector('[role="dialog"], [data-automation-id*="datePicker"], [data-automation-id*="calendar"], [class*="calendar"]');
	return JSON.stringify({clicked: true, opened: !!dialog});
}"""

_SELECT_GROUPED_DATE_PICKER_VALUE_JS = r"""(monthName, dayValue, yearValue) => {
	var dialogs = Array.from(document.querySelectorAll('[role="dialog"], [data-automation-id*="datePicker"], [data-automation-id*="calendar"], [class*="calendar"]'));
	if (dialogs.length === 0) return JSON.stringify({selected: false, error: 'picker_not_open'});
	var roots = dialogs;
	var targetMonth = (monthName || '').toLowerCase();
	var targetDay = String(dayValue || '').trim();
	var targetYear = String(yearValue || '').trim();
	var matches = function(text) {
		var lower = (text || '').replace(/\s+/g, ' ').trim().toLowerCase();
		if (!lower) return false;
		if (targetMonth && lower.indexOf(targetMonth) === -1) return false;
		if (targetYear && lower.indexOf(targetYear) === -1) return false;
		return lower.indexOf(' ' + targetDay + ' ') !== -1
			|| lower.endsWith(' ' + targetDay)
			|| lower.indexOf('/' + targetDay + '/') !== -1
			|| lower.indexOf('-' + targetDay + '-') !== -1;
	};
	for (var i = 0; i < roots.length; i++) {
		var nodes = roots[i].querySelectorAll('button, [role="button"], [role="gridcell"], [role="cell"], td, div');
		for (var j = 0; j < nodes.length; j++) {
			var node = nodes[j];
			var text = (node.getAttribute && node.getAttribute('aria-label')) || node.textContent || '';
			if (!matches(text)) continue;
			if (node.scrollIntoView) node.scrollIntoView({block: 'center', inline: 'center'});
			if (node.click) node.click();
			return JSON.stringify({selected: true});
		}
	}
	return JSON.stringify({selected: false, error: 'target_not_found'});
}"""


_CLICK_DROPDOWN_OPTION_JS = CLICK_DROPDOWN_OPTION_ENHANCED_JS
