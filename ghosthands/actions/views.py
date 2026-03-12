"""Pydantic models for DomHand action parameters and results."""

import re

from pydantic import BaseModel, ConfigDict, Field


class FormField(BaseModel):
	"""A single form field extracted from the page DOM."""

	model_config = ConfigDict(extra='ignore')

	field_id: str = Field(description='Unique DOM-assigned ID (e.g. data-ff-id)')
	name: str = Field(description='Human-readable label for the field')
	field_type: str = Field(description='Input type: text, email, select, checkbox, radio, file, textarea, etc.')
	section: str = Field(default='', description='Section/group this field belongs to')
	required: bool = Field(default=False, description='Whether the field is required')
	options: list[str] = Field(default_factory=list, description='Available options for select/radio/checkbox fields')
	choices: list[str] = Field(default_factory=list, description='Alternative choice list (some ATS platforms)')
	accept: str | None = Field(default=None, description='Accepted file types for file inputs')
	is_native: bool = Field(default=True, description='Whether this is a native HTML element vs custom widget')
	is_multi_select: bool = Field(default=False, description='Whether multiple selections are allowed')
	visible: bool = Field(default=True, description='Whether the field is currently visible')
	raw_label: str | None = Field(default=None, description='Original label text before cleanup')
	synthetic_label: bool = Field(default=False, description='True if label was generated synthetically')
	field_fingerprint: str | None = Field(default=None, description='Stable fingerprint for identity tracking')
	current_value: str = Field(default='', description='Current value in the field')


class FillFieldResult(BaseModel):
	"""Result of attempting to fill a single field."""

	model_config = ConfigDict(extra='ignore')

	field_id: str
	name: str
	success: bool
	actor: str = Field(description="Who filled it: 'dom' or 'unfilled'")
	error: str | None = None
	value_set: str | None = None


class DomHandFillParams(BaseModel):
	"""Fill all visible form fields using fast DOM manipulation."""

	target_section: str | None = Field(
		None,
		description='Optional section name to fill. If null, fills all visible sections.',
	)
	heading_boundary: str | None = Field(
		None,
		description=(
			'Restrict filling to fields BELOW this heading and ABOVE the next sibling heading. '
			'Use for repeater entries, e.g. heading_boundary="Work Experience 2" to fill only '
			'the second work experience entry without touching others.'
		),
	)
	entry_data: dict | None = Field(
		None,
		description=(
			'Structured data for a single repeater entry. When provided, this overrides the '
			'full profile for LLM answer generation. Example: '
			'{"title": "Software Engineer", "company": "Google", "start_date": "06/2022", '
			'"end_date": "Present", "description": "Built distributed systems..."}'
		),
	)


class DomHandSelectParams(BaseModel):
	"""Select a dropdown option using platform-aware discovery."""

	index: int = Field(description='Element index of the dropdown trigger')
	value: str = Field(description='Value or text to select')


class DomHandUploadParams(BaseModel):
	"""Upload a file (resume, cover letter) to a file input."""

	index: int = Field(description='Element index of the file input')
	file_type: str = Field(default='resume', description="Type: 'resume' or 'cover_letter'")


class DomHandExpandParams(BaseModel):
	"""Click "Add More" buttons to expand repeater sections."""

	section: str = Field(description='Section name containing the repeater')


# ── Matching utilities ──────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(
	r'^(select\.{0,3}|select…|please\s+select(\s+one)?|select\s+(one|an?\s+option)'
	r'|choose\.{0,3}|choose…|please\s+choose(\s+one)?|choose\s+one|pick'
	r'|start\s+typing|enter\s+(your|an?)\s+\S+'
	r'|type\s+here|--+\s*(select|choose)?\s*--*|—)$',
	re.IGNORECASE,
)


def is_placeholder_value(value: str) -> bool:
	"""Return True if the value looks like a placeholder (e.g. "Select one")."""
	return bool(_PLACEHOLDER_RE.match(value.strip()))


def normalize_name(s: str) -> str:
	"""Normalize a field name for comparison: strip asterisks/underscores, collapse whitespace, lowercase."""
	return re.sub(r'\s+', ' ', s.replace('*', '').replace('_', ' ')).strip().lower()


def split_dropdown_value_hierarchy(value: str) -> list[str]:
	"""Split hierarchical dropdown labels such as "Category > Option" into ordered segments."""
	raw = re.sub(r'\s+', ' ', (value or '').strip())
	if not raw:
		return []
	parts = [part.strip() for part in re.split(r'\s*(?:>|→)\s*', raw) if part.strip()]
	return parts or [raw]


def generate_dropdown_search_terms(value: str) -> list[str]:
	"""Build generic fallback search terms for searchable dropdowns and typeaheads."""
	raw = re.sub(r'\s+', ' ', (value or '').strip())
	if not raw:
		return []

	seen: set[str] = set()
	terms: list[str] = []
	stop_words = {'of', 'and', 'in', 'the', 'a', 'an', 'for', 'to', 'with', 'or', 'at', 'by'}

	def add(term: str) -> None:
		cleaned = re.sub(r'\s+', ' ', term.strip())
		if not cleaned:
			return
		key = normalize_name(cleaned)
		if not key or key in seen:
			return
		seen.add(key)
		terms.append(cleaned)

	add(raw)
	for part in split_dropdown_value_hierarchy(raw):
		add(part)
		words = [word for word in re.split(r'\s+', part) if len(word) > 1]
		meaningful_words = [word for word in words if word.lower() not in stop_words]
		if len(meaningful_words) > 1:
			for word in meaningful_words:
				add(word)

	return terms


def get_stable_field_key(field: FormField) -> str:
	"""Build a stable key for a field for cross-round identity tracking."""
	fp = normalize_name(field.field_fingerprint or '')
	if fp:
		return f'{normalize_name(field.field_type)}|{fp}'
	return '|'.join([
		normalize_name(field.field_type),
		normalize_name(field.section),
		normalize_name(field.name or field.raw_label or ''),
	])
