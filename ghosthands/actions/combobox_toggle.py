"""Additive helpers: open react-select / combobox UIs via chevron / flyout toggle when present.

Many ATS widgets (e.g. Greenhouse react-select) expose a separate toggle button
(`aria-label` contains "Toggle", indicator divs) that opens the menu more reliably
than clicking the text input alone. DomHand already handles selection once the menu
is open; this module only improves *opening* without changing matching semantics.
"""

from __future__ import annotations

import json
from typing import Any

# CDP Runtime.callFunctionOn — `this` is the indexed trigger (usually the combobox input).
CLICK_COMBOBOX_TOGGLE_ON_NODE_JS = r"""function() {
	const el = this;
	const visible = (node) => {
		if (!node) return false;
		const style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		const rect = node.getBoundingClientRect();
		return rect.width > 0 && rect.height > 0;
	};
	const classStr = (n) => {
		const c = n.getAttribute && n.getAttribute('class');
		return (c || '').toLowerCase();
	};
	const isClearOrRemove = (n) => {
		const al = (n.getAttribute('aria-label') || '').toLowerCase();
		const cls = classStr(n);
		return al.includes('clear') || al.includes('remove') || cls.includes('clear-indicator');
	};
	const tryClick = (n) => {
		if (!n || !visible(n) || isClearOrRemove(n)) return false;
		try {
			n.click();
			return true;
		} catch (e) {
			return false;
		}
	};
	const root =
		el.closest('[class*="select__control"]')
		|| el.closest('[class*="Select__control"]')
		|| el.closest('[class*="css-"][class*="-control"]')
		|| el.closest('[class*="react-select"]')
		|| el.closest('[role="group"]')
		|| el.parentElement;
	if (!root) {
		return JSON.stringify({ clicked: false, reason: 'no_root' });
	}
	const orderedSelectors = [
		'button[aria-label*="Toggle" i]',
		'div[role="button"][aria-label*="Toggle" i]',
		'button[aria-label*="Open" i]',
		'button[aria-label*="Menu" i]',
		'[class*="dropdown-indicator"]',
		'[class*="DropdownIndicator"]',
		'[class*="indicator-container"]',
		'[class*="IndicatorsContainer"] [class*="indicator"]:not([class*="clear"])',
	];
	for (const sel of orderedSelectors) {
		for (const btn of root.querySelectorAll(sel)) {
			if (tryClick(btn)) {
				return JSON.stringify({ clicked: true, via: 'toggle', selector: sel });
			}
		}
	}
	return JSON.stringify({ clicked: false, reason: 'no_toggle' });
}"""

# page.evaluate(ffId) — uses __ff.byId like other domhand_fill snippets.
CLICK_COMBOBOX_TOGGLE_BY_FFID_JS = r"""(ffId) => {
	const ff = window.__ff;
	const el = ff ? ff.byId(ffId) : null;
	if (!el) {
		return JSON.stringify({ clicked: false, reason: 'no_el' });
	}
	const visible = (node) => {
		if (!node) return false;
		const style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		const rect = node.getBoundingClientRect();
		return rect.width > 0 && rect.height > 0;
	};
	const classStr = (n) => {
		const c = n.getAttribute && n.getAttribute('class');
		return (c || '').toLowerCase();
	};
	const isClearOrRemove = (n) => {
		const al = (n.getAttribute('aria-label') || '').toLowerCase();
		const cls = classStr(n);
		return al.includes('clear') || al.includes('remove') || cls.includes('clear-indicator');
	};
	const tryClick = (n) => {
		if (!n || !visible(n) || isClearOrRemove(n)) return false;
		try {
			n.click();
			return true;
		} catch (e) {
			return false;
		}
	};
	const root =
		el.closest('[class*="select__control"]')
		|| el.closest('[class*="Select__control"]')
		|| el.closest('[class*="css-"][class*="-control"]')
		|| el.closest('[class*="react-select"]')
		|| el.closest('[role="group"]')
		|| el.parentElement;
	if (!root) {
		return JSON.stringify({ clicked: false, reason: 'no_root' });
	}
	const orderedSelectors = [
		'button[aria-label*="Toggle" i]',
		'div[role="button"][aria-label*="Toggle" i]',
		'button[aria-label*="Open" i]',
		'button[aria-label*="Menu" i]',
		'[class*="dropdown-indicator"]',
		'[class*="DropdownIndicator"]',
		'[class*="indicator-container"]',
		'[class*="IndicatorsContainer"] [class*="indicator"]:not([class*="clear"])',
	];
	for (const sel of orderedSelectors) {
		for (const btn of root.querySelectorAll(sel)) {
			if (tryClick(btn)) {
				return JSON.stringify({ clicked: true, via: 'toggle', selector: sel });
			}
		}
	}
	return JSON.stringify({ clicked: false, reason: 'no_toggle' });
}"""


def combobox_toggle_clicked(result: Any) -> bool:
    """True when JS reported a successful toggle/chevron click."""
    if isinstance(result, dict):
        return bool(result.get("clicked"))
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return False
        return bool(isinstance(data, dict) and data.get("clicked"))
    return False


CLICK_INPUT_BY_FFID_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (el) el.click();
	return 'ok';
}"""
