"""Label resolution chain — standalone helper for accessible name resolution.

Ports the label resolution logic from GHOST-HANDS formFiller.ts
``window.__ff.getAccessibleName()`` as a Python-side post-processing layer.

The primary label resolution happens in-browser via the injected
``__ff.getAccessibleName`` (see ``shadow_helpers.py``).  This module provides:

1. ``sanitize_label()`` — strip asterisks, "Required" text, normalize whitespace
2. ``resolve_label_source()`` — determine *where* the label came from
3. ``generate_field_fingerprint()`` — stable identity for cross-extraction tracking
"""

import hashlib
import re


def sanitize_label(raw: str) -> str:
	"""Strip asterisks, 'Required' markers, and normalize whitespace.

	>>> sanitize_label("  First Name *  ")
	'First Name'
	>>> sanitize_label("Email Required")
	'Email'
	>>> sanitize_label("Phone * Required")
	'Phone'
	"""
	result = raw.replace("*", " ")
	result = re.sub(r"\s*Required\s*", " ", result, flags=re.IGNORECASE)
	result = re.sub(r"\s+", " ", result).strip()
	return result


def resolve_label_source(
	raw_label: str,
	aria_labelledby: str,
	aria_label: str,
	label_for: str,
	legend_text: str,
	placeholder: str,
	name_attr: str,
	title_attr: str,
) -> tuple[str, list[str]]:
	"""Walk the label resolution chain and return (resolved_label, sources).

	The chain mirrors ``__ff.getAccessibleName()`` priority:
	  1. aria-labelledby (referenced element text)
	  2. aria-label attribute
	  3. label[for=id] association
	  4. Closest ancestor label or legend
	  5. Placeholder attribute
	  6. Name attribute (fallback)
	  7. Title attribute (final fallback)

	Returns the first non-empty label and a list of all sources that had a
	non-empty value (for observability).
	"""
	sources: list[str] = []
	resolved = ""

	if aria_labelledby.strip():
		sources.append("aria-labelledby")
		if not resolved:
			resolved = sanitize_label(aria_labelledby)

	if aria_label.strip():
		sources.append("aria-label")
		if not resolved:
			resolved = sanitize_label(aria_label)

	if label_for.strip():
		sources.append("label[for]")
		if not resolved:
			resolved = sanitize_label(label_for)

	if legend_text.strip():
		sources.append("legend")
		if not resolved:
			resolved = sanitize_label(legend_text)

	if placeholder.strip():
		sources.append("placeholder")
		if not resolved:
			resolved = placeholder.strip()

	if name_attr.strip():
		sources.append("name")
		if not resolved:
			resolved = name_attr.strip()

	if title_attr.strip():
		sources.append("title")
		if not resolved:
			resolved = title_attr.strip()

	return resolved, sources


def normalize_name(s: str) -> str:
	"""Normalize a field name for comparison — mirrors GH ``normalizeName()``.

	Strips asterisks, collapses whitespace, lowercases.

	>>> normalize_name("First Name *")
	'first name'
	"""
	return re.sub(r"\s+", " ", s.replace("*", "")).strip().lower()


def generate_field_fingerprint(
	field_type: str,
	label: str,
	section: str,
	name_attr: str,
) -> str:
	"""Generate a stable fingerprint for a form field.

	Used to track field identity across re-extractions (e.g. after SPA
	re-renders).  The fingerprint is a truncated SHA-256 of the normalised
	type + label + section + name.

	>>> len(generate_field_fingerprint("text", "First Name", "Personal", "firstName"))
	16
	"""
	parts = "|".join([
		normalize_name(field_type),
		normalize_name(label),
		normalize_name(section),
		normalize_name(name_attr),
	])
	return hashlib.sha256(parts.encode()).hexdigest()[:16]


def is_placeholder_value(value: str) -> bool:
	"""Test whether a value looks like a placeholder/default selection.

	Mirrors the ``PLACEHOLDER_RE`` from formFiller.ts.

	>>> is_placeholder_value("Select...")
	True
	>>> is_placeholder_value("United States")
	False
	>>> is_placeholder_value("-- Select --")
	True
	"""
	pattern = re.compile(
		r"^(select\.{0,3}|select…|please\s+select(\s+one)?|select\s+(one|an?\s+option)"
		r"|choose\.{0,3}|choose…|please\s+choose(\s+one)?|choose\s+one|pick"
		r"|start\s+typing|enter\s+(your|an?)\s+(name|email|phone|address|city|state|zip|value|number|answer|response|text|url|company|title)"
		r"|type\s+here|--+\s*(select|choose)?\s*--*|—)$",
		re.IGNORECASE,
	)
	return bool(pattern.match(value.strip()))
