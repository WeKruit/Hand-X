"""Additive helpers: open react-select / combobox UIs via chevron / flyout toggle when present.

Many ATS widgets (e.g. Greenhouse react-select) expose a separate toggle button
(`aria-label` contains "Toggle", indicator divs) that opens the menu more reliably
than clicking the text input alone. DomHand already handles selection once the menu
is open; this module only improves *opening* without changing matching semantics.
"""

from __future__ import annotations

import asyncio
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
		'[data-automation-id="promptSearchButton"]',
		'[data-uxi-widget-type="selectinputicon"]:not([data-automation-id*="clear" i])',
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
		'[data-automation-id="promptSearchButton"]',
		'[data-uxi-widget-type="selectinputicon"]:not([data-automation-id*="clear" i])',
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


GET_COMBOBOX_TOGGLE_TARGET_BY_FFID_JS = r"""(ffId) => {
	const ff = window.__ff;
	const el = ff ? ff.byId(ffId) : document.querySelector('[data-ff-id="' + ffId + '"]');
	if (!el) {
		return JSON.stringify({ found: false, already_open: false, reason: 'no_el' });
	}
	const visible = (node) => {
		if (!node) return false;
		const style = window.getComputedStyle(node);
		if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
		const rect = node.getBoundingClientRect();
		return rect.width > 0 && rect.height > 0;
	};
	const classStr = (n) => {
		const c = n && n.getAttribute ? n.getAttribute('class') : '';
		return (c || '').toLowerCase();
	};
	const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
	const collectIds = (node, attr) => {
		if (!node || !node.getAttribute) return [];
		return String(node.getAttribute(attr) || '')
			.split(/\s+/)
			.map((part) => part.trim())
			.filter(Boolean);
	};
	const qAll = (sel) => (window.__ff && window.__ff.queryAll)
		? window.__ff.queryAll(sel)
		: Array.from(document.querySelectorAll(sel));
	const isClearOrRemove = (n) => {
		const al = ((n && n.getAttribute && n.getAttribute('aria-label')) || '').toLowerCase();
		const cls = classStr(n);
		return al.includes('clear') || al.includes('remove') || cls.includes('clear-indicator');
	};
	const root =
		el.closest('[class*="select__control"]')
		|| el.closest('[class*="Select__control"]')
		|| el.closest('[class*="css-"][class*="-control"]')
		|| el.closest('[class*="react-select"]')
		|| el.closest('[role="group"]')
		|| el.closest('[data-automation-id="formField"], [data-automation-id*="formField"], .form-group, .field')
		|| el.parentElement
		|| el;
	const combo =
		el.closest('[role="combobox"]')
		|| (el.getAttribute && el.getAttribute('role') === 'combobox' ? el : null)
		|| (root && root.querySelector ? root.querySelector('[role="combobox"]') : null);
	const controlledPopupVisible = () => {
		const ids = [
			...collectIds(el, 'aria-controls'),
			...collectIds(el, 'aria-owns'),
			...collectIds(combo, 'aria-controls'),
			...collectIds(combo, 'aria-owns'),
		];
		for (const id of ids) {
			const popup = document.getElementById(id);
			if (visible(popup)) return true;
		}
		const descendants = root && root.querySelectorAll
			? root.querySelectorAll('[role="listbox"], [role="menu"], [role="grid"], [class*="listbox"], [class*="dropdown-menu"], [class*="options-list"]')
			: [];
		for (const popup of descendants) {
			if (visible(popup)) return true;
		}
		return false;
	};
	if ((combo && combo.getAttribute('aria-expanded') === 'true') || controlledPopupVisible()) {
		return JSON.stringify({ found: false, already_open: true, reason: 'already_open' });
	}
	const orderedSelectors = [
		'[data-automation-id="promptSearchButton"]',
		'[data-uxi-widget-type="selectinputicon"]:not([data-automation-id*="clear" i])',
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
			if (!visible(btn) || isClearOrRemove(btn)) continue;
			const rect = btn.getBoundingClientRect();
			return JSON.stringify({
				found: true,
				already_open: false,
				via: 'toggle',
				selector: sel,
				x: rect.left + rect.width / 2,
				y: rect.top + rect.height / 2,
			});
		}
	}
	if (visible(el)) {
		const rect = el.getBoundingClientRect();
		return JSON.stringify({
			found: true,
			already_open: false,
			via: 'input',
			x: rect.left + rect.width / 2,
			y: rect.top + rect.height / 2,
		});
	}
	return JSON.stringify({ found: false, already_open: false, reason: 'no_target' });
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


def combobox_toggle_target(result: Any) -> dict[str, Any]:
    """Normalize helper payloads describing where a trusted combobox click should land."""
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, ValueError):
            return {"found": False, "already_open": False}
        return data if isinstance(data, dict) else {"found": False, "already_open": False}
    return {"found": False, "already_open": False}


async def trusted_open_combobox_by_ffid(page: Any, ff_id: str) -> dict[str, Any]:
    """Open a combobox with a real mouse click so React/Greenhouse sees ``isTrusted=true``."""
    if not str(ff_id).strip():
        return {"clicked": False, "found": False, "already_open": False, "reason": "missing_ff_id"}

    try:
        target = combobox_toggle_target(await page.evaluate(GET_COMBOBOX_TOGGLE_TARGET_BY_FFID_JS, ff_id))
    except Exception as exc:
        return {"clicked": False, "found": False, "already_open": False, "reason": f"evaluate_failed:{exc}"}

    if target.get("already_open"):
        target["clicked"] = False
        return target
    if not target.get("found"):
        target["clicked"] = False
        return target

    try:
        await page.evaluate(
            """(ffId) => {
                const ff = window.__ff;
                const el = ff ? ff.byId(ffId) : document.querySelector(`[data-ff-id="${ffId}"]`);
                if (!el || !el.scrollIntoView) return false;
                el.scrollIntoView({ block: 'center', inline: 'nearest' });
                return true;
            }""",
            ff_id,
        )
        await asyncio.sleep(0.12)
        refreshed = combobox_toggle_target(await page.evaluate(GET_COMBOBOX_TOGGLE_TARGET_BY_FFID_JS, ff_id))
        if refreshed.get("found"):
            target = refreshed
        mouse = await page.mouse
        await mouse.move(float(target["x"]), float(target["y"]))
        await asyncio.sleep(0.05)
        await mouse.click(float(target["x"]), float(target["y"]))
    except Exception as exc:
        return {
            **target,
            "clicked": False,
            "reason": f"mouse_click_failed:{exc}",
        }

    post: dict[str, Any] = {}
    opened = False
    for _ in range(5):
        await asyncio.sleep(0.12)
        try:
            post = combobox_toggle_target(await page.evaluate(GET_COMBOBOX_TOGGLE_TARGET_BY_FFID_JS, ff_id))
        except Exception:
            post = {}
        opened = bool(post.get("already_open"))
        if opened:
            break
    return {
        **target,
        "clicked": opened,
        "already_open": opened,
        "post_check": post,
    }


CLICK_INPUT_BY_FFID_JS = r"""(ffId) => {
	var ff = window.__ff;
	var el = ff ? ff.byId(ffId) : null;
	if (el) el.click();
	return 'ok';
}"""
