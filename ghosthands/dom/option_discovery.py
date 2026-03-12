"""Dropdown option extraction — discovers choices for select/combobox fields.

Ports the option discovery logic from GHOST-HANDS formFiller.ts including:
- Native ``<select>`` → read ``<option>`` children
- Custom combobox → aria-controls/aria-owns → find listbox → read options
- Workday activeListContainer portal → floating listbox in body
- React Select → ``.css-*`` or ``[class*="menu"]`` → options
- MUI Autocomplete → ``[role="listbox"]`` → options
- Button group → sibling buttons with role or data attributes
- Hierarchical Workday dropdowns with chevron sub-categories

The in-browser scripts are designed to be called via ``page.evaluate()``
from the Python extraction layer.
"""

from playwright.async_api import Page


# ── JS for reading options already visible in the DOM ────────────────────

_READ_INLINE_OPTIONS_JS = """
(ffId) => {
	const ff = window.__ff;
	const el = ff?.byId(ffId);
	if (!el) return [];

	const getOptionMainText = (opt) => {
		const clone = opt.cloneNode(true);
		clone.querySelectorAll('[class*="desc"], [class*="sub"], [class*="hint"], .option-desc, small').forEach(x => x.remove());
		return clone.textContent?.trim() || '';
	};

	let opts = [];

	/* Native <select> */
	if (el.tagName === 'SELECT') {
		opts = Array.from(el.options)
			.filter(o => o.value !== '')
			.map(o => o.textContent?.trim() || '')
			.filter(Boolean);
		return opts;
	}

	/* Custom combobox — aria-controls / aria-owns */
	const ctrlId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
	let src = ctrlId ? ff.getByDomId(ctrlId) : null;

	/* Input inside a select-like wrapper */
	if (!src && el.tagName === 'INPUT') {
		src = ff.closestCrossRoot(el, '[class*="select"], [class*="combobox"], .form-group, .field');
	}
	if (!src) src = el;

	if (src) {
		opts = Array.from(src.querySelectorAll('[role="option"], [role="menuitem"]'))
			.map(o => getOptionMainText(o))
			.filter(Boolean);
	}

	return [...new Set(opts)];
}
"""


_READ_ACTIVE_LIST_OPTIONS_JS = """
() => {
	const ff = window.__ff;
	const results = [];

	function collect(items) {
		for (const o of items) {
			const r = o.getBoundingClientRect();
			if (r.width === 0 || r.height === 0) continue;
			const t = (o.textContent || '').trim();
			if (t && t.length < 200) results.push(t);
		}
	}

	/* 1. Workday activeListContainer portal */
	const container = ff?.queryOne('[data-automation-id="activeListContainer"]');
	if (container) {
		let items = Array.from(container.querySelectorAll('[role="option"]'));
		if (items.length === 0) {
			items = Array.from(container.querySelectorAll(
				'[data-automation-id="promptOption"], [data-automation-id="menuItem"]'
			));
		}
		collect(items);
	}

	/* 2. Any visible [role="listbox"] */
	if (results.length === 0) {
		const listboxes = ff?.queryAll('[role="listbox"]') ?? [];
		for (const lb of listboxes) {
			const r = lb.getBoundingClientRect();
			if (r.height > 0) {
				collect(Array.from(lb.querySelectorAll('[role="option"]')));
			}
		}
	}

	/* 3. Any visible standalone [role="option"] */
	if (results.length === 0) {
		collect((ff?.queryAll('[role="option"]') ?? []).filter(el => {
			const r = el.getBoundingClientRect();
			return r.height > 0;
		}));
	}

	/* 4. Generic dropdown li items */
	if (results.length === 0) {
		const dropdowns = ff?.queryAll(
			'[class*="dropdown"]:not([style*="display: none"]), ' +
			'[class*="menu"]:not([style*="display: none"]), ' +
			'[class*="listbox"]:not([style*="display: none"])'
		) ?? [];
		for (const dd of dropdowns) {
			const r = dd.getBoundingClientRect();
			if (r.height > 0) {
				collect(Array.from(dd.querySelectorAll('li')));
			}
		}
	}

	return [...new Set(results)];
}
"""


_READ_FALLBACK_OPTIONS_JS = """
(ffId) => {
	const ff = window.__ff;
	const el = ff?.byId(ffId);
	if (!el) return [];
	const results = [];

	function collect(container) {
		const opts = container.querySelectorAll('[role="option"], [role="menuitem"], li');
		for (const o of opts) {
			const r = o.getBoundingClientRect();
			if (r.width === 0 || r.height === 0) continue;
			const t = o.textContent?.trim();
			if (t && t.length < 200) results.push(t);
		}
	}

	collect(el);

	const ctrlId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
	if (ctrlId) {
		const popup = ff?.getByDomId(ctrlId);
		if (popup) collect(popup);
	}

	if (el.tagName === 'INPUT') {
		const container = ff?.closestCrossRoot(el, '[class*="select"], [class*="combobox"], .form-group');
		if (container) collect(container);
	}

	/* Workday visible listboxes */
	const listboxes = ff?.queryAll('[role="listbox"]') ?? [];
	for (const lb of listboxes) {
		const r = lb.getBoundingClientRect();
		if (r.height > 0) collect(lb);
	}

	return [...new Set(results)];
}
"""


_IS_DROPDOWN_OPEN_JS = """
() => {
	const ff = window.__ff;
	const visible = (el) => {
		if (!el) return false;
		const rect = el.getBoundingClientRect();
		const style = window.getComputedStyle(el);
		return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
	};

	if (visible(ff?.queryOne?.('[data-automation-id="activeListContainer"]') ?? null)) {
		return true;
	}

	const candidates = document.querySelectorAll(
		'[role="listbox"], [role="dialog"], [aria-modal="true"], ' +
		'[data-automation-id="activeListContainer"], ' +
		'[class*="dropdown"], [class*="select-dropdown"]'
	);
	return Array.from(candidates).some(c => visible(c));
}
"""


_HAS_CHEVRONS_JS = """
() => {
	const container = window.__ff?.queryOne('[data-automation-id="activeListContainer"]');
	if (!container) return false;
	return container.querySelector('svg.wd-icon-chevron-right-small') !== null ||
		container.querySelector('[data-uxi-multiselectlistitem-hassidecharm="true"]') !== null;
}
"""


_READ_VIRTUALIZED_OPTIONS_JS = """
() => {
	const c = window.__ff?.queryOne('[data-automation-id="activeListContainer"]');
	if (!c) return { setsize: 0, texts: [] };
	const items = c.querySelectorAll('[role="option"]');
	const setsize = parseInt(items[0]?.getAttribute('aria-setsize') || '0', 10);
	const texts = Array.from(items)
		.filter(o => o.getBoundingClientRect().height > 0)
		.map(o => (o.textContent || '').trim())
		.filter(Boolean);
	return { setsize: setsize, texts: [...new Set(texts)] };
}
"""


_SCROLL_ACTIVE_LIST_JS = """
() => {
	const c = window.__ff?.queryOne('[data-automation-id="activeListContainer"]');
	if (c) c.scrollTop += 300;
}
"""


_READ_SCROLL_OPTIONS_JS = """
() => {
	const c = window.__ff?.queryOne('[data-automation-id="activeListContainer"]');
	if (!c) return [];
	return Array.from(c.querySelectorAll('[role="option"]'))
		.filter(o => o.getBoundingClientRect().height > 0)
		.map(o => (o.textContent || '').trim())
		.filter(Boolean);
}
"""


_DISMISS_DROPDOWN_JS = """
() => {
	const ae = document.activeElement;
	if (ae && ae.blur) ae.blur();
	document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
	document.body.dispatchEvent(new MouseEvent('click', { bubbles: true }));
}
"""


# ── Python async wrappers ────────────────────────────────────────────────

async def read_inline_options(page: Page, ff_id: str) -> list[str]:
	"""Read options already present in the DOM for a given field.

	Handles native ``<select>``, aria-controls referenced listboxes,
	and options within the field's ancestor container.
	"""
	result: list[str] = await page.evaluate(_READ_INLINE_OPTIONS_JS, ff_id)
	return result


async def read_active_list_options(page: Page) -> list[str]:
	"""Read de-duplicated option texts from any visible dropdown portal.

	Checks in priority order:
	1. Workday activeListContainer portal
	2. Any visible ``[role="listbox"]``
	3. Any visible standalone ``[role="option"]``
	4. Generic dropdown ``li`` items
	"""
	result: list[str] = await page.evaluate(_READ_ACTIVE_LIST_OPTIONS_JS)
	return result


async def read_fallback_options(page: Page, ff_id: str) -> list[str]:
	"""Read options after opening a dropdown — broader search than inline.

	Collects from the element itself, aria-controls popup, ancestor
	container, and any visible listboxes on the page.
	"""
	result: list[str] = await page.evaluate(_READ_FALLBACK_OPTIONS_JS, ff_id)
	return result


async def is_dropdown_open(page: Page) -> bool:
	"""Check if any dropdown portal/popup is currently visible."""
	try:
		result: bool = await page.evaluate(_IS_DROPDOWN_OPEN_JS)
		return result
	except Exception:
		return False


async def has_chevron_subcategories(page: Page) -> bool:
	"""Check if the active Workday dropdown has hierarchical sub-categories."""
	try:
		result: bool = await page.evaluate(_HAS_CHEVRONS_JS)
		return result
	except Exception:
		return False


async def read_virtualized_options(page: Page) -> dict:
	"""Read options from a virtualized Workday list, including setsize metadata."""
	result = await page.evaluate(_READ_VIRTUALIZED_OPTIONS_JS)
	return result


async def scroll_active_list(page: Page) -> None:
	"""Scroll the active Workday list container down by 300px."""
	await page.evaluate(_SCROLL_ACTIVE_LIST_JS)


async def read_scroll_options(page: Page) -> list[str]:
	"""Read currently visible options after scrolling the active list."""
	result: list[str] = await page.evaluate(_READ_SCROLL_OPTIONS_JS)
	return result


async def dismiss_dropdown(page: Page) -> None:
	"""Dismiss any open dropdown by blurring focus and clicking body."""
	try:
		await page.keyboard.press("Escape")
	except Exception:
		pass
	try:
		await page.evaluate(_DISMISS_DROPDOWN_JS)
	except Exception:
		pass


async def dismiss_dropdown_if_open(page: Page) -> None:
	"""Dismiss the dropdown only if one is currently open."""
	if await is_dropdown_open(page):
		await dismiss_dropdown(page)
