"""Validation error reader — extracts visible form errors from the DOM.

Ports the ``captureValidationErrors`` function from GHOST-HANDS
formFiller.ts.  Finds page-level error summaries and per-field inline
error messages, then maps them back to field keys.
"""

from playwright.async_api import Page

from ghosthands.dom.views import FormField, ValidationSnapshot


_CAPTURE_VALIDATION_JS = """
(fieldMeta) => {
	const ff = window.__ff;
	const roots = ff?.allRoots?.() ?? [document];
	const errorSelector = [
		'[role="alert"]',
		'[aria-live="assertive"]',
		'[data-automation-id*="error"]',
		'[class*="error"]',
		'[class*="Error"]',
	].join(', ');

	const visible = (node) => {
		if (!node) return false;
		const rect = node.getBoundingClientRect();
		const style = window.getComputedStyle(node);
		return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
	};

	const summarizeText = (text) =>
		text
			.split(/\\n+/)
			.map(line => line.trim())
			.filter(line => line.length > 5)
			.filter(line => /error|required|invalid|must|enter|select/i.test(line));

	/* Page-level error summaries */
	const summaryItems = [];
	const seenSummary = new Set();
	for (const root of roots) {
		if (!root.querySelectorAll) continue;
		for (const node of Array.from(root.querySelectorAll(errorSelector))) {
			if (!visible(node)) continue;
			const lines = summarizeText((node.textContent || '').trim());
			for (const line of lines) {
				if (seenSummary.has(line)) continue;
				seenSummary.add(line);
				summaryItems.push(line);
			}
		}
	}

	/* Per-field inline errors */
	const inlineByFieldKey = {};
	for (const { fieldId, fieldKey } of fieldMeta) {
		const el = ff?.byId?.(fieldId);
		if (!el) continue;
		const scope =
			ff?.closestCrossRoot?.(el, '[data-automation-id], .form-group, .field, .form-field, fieldset')
			|| el;
		const errors = Array.from(scope.querySelectorAll(errorSelector))
			.filter(node => visible(node))
			.flatMap(node => summarizeText((node.textContent || '').trim()));
		if (errors.length > 0) {
			inlineByFieldKey[fieldKey] = [...new Set(errors)];
		}
	}

	return { summaryItems: summaryItems, inlineByFieldKey: inlineByFieldKey };
}
"""


def _get_stable_field_key(field: FormField) -> str:
	"""Compute the stable field key used for cross-extraction identity.

	Mirrors ``getStableFieldKey()`` from formFiller.ts.
	"""
	from ghosthands.dom.label_resolver import normalize_name

	fingerprint = normalize_name(field.field_fingerprint)
	if fingerprint:
		return f"{normalize_name(field.field_type)}|{fingerprint}"
	return "|".join([
		normalize_name(field.field_type),
		normalize_name(field.section),
		normalize_name(field.label or field.raw_label),
	])


async def capture_validation_errors(
	page: Page,
	fields: list[FormField],
) -> ValidationSnapshot:
	"""Extract visible validation errors from the page.

	Scans all roots (including shadow roots) for elements matching error
	selectors (``[role="alert"]``, ``[class*="error"]``, etc.) and:

	1. Collects page-level error summary messages
	2. Maps inline errors to specific fields by searching within each
	   field's closest form-group ancestor

	Returns a ``ValidationSnapshot`` with both summary and per-field errors.
	"""
	field_meta = [
		{"fieldId": f.ff_id, "fieldKey": _get_stable_field_key(f)}
		for f in fields
	]

	try:
		result = await page.evaluate(_CAPTURE_VALIDATION_JS, field_meta)
	except Exception:
		return ValidationSnapshot()

	return ValidationSnapshot(
		summary_items=result.get("summaryItems", []),
		inline_by_field_key=result.get("inlineByFieldKey", {}),
	)


async def read_field_errors(
	page: Page,
	field: FormField,
) -> list[str]:
	"""Read inline validation errors for a single field.

	Convenience wrapper around ``capture_validation_errors`` for
	single-field queries.
	"""
	snapshot = await capture_validation_errors(page, [field])
	key = _get_stable_field_key(field)
	return snapshot.inline_by_field_key.get(key, [])
