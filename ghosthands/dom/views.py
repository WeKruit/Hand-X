"""Pydantic models for DOM form field extraction.

Ports the FormField, FieldOption, and extraction result types from
GHOST-HANDS formFiller.ts into Pydantic v2 models for the Python layer.
"""

from pydantic import BaseModel, ConfigDict, Field


class FieldOption(BaseModel):
	"""A single option inside a select/radio/button group."""

	model_config = ConfigDict(extra='forbid')

	value: str
	text: str
	selected: bool = False


class FormField(BaseModel):
	"""A form field extracted from the DOM via page.evaluate().

	Maps 1:1 to the GH FormField interface in formFiller.ts.  The ``index``
	field carries the browser-use element index when available; ``ff_id`` is
	the ``data-ff-id`` tag assigned during extraction.
	"""

	model_config = ConfigDict(extra='forbid')

	ff_id: str = Field(description="data-ff-id tag assigned during extraction (e.g. 'ff-0')")
	index: int = Field(default=-1, description="Element index from browser-use DOM, -1 if unresolved")
	selector: str = Field(default="", description="CSS selector for targeting (data-ff-id based)")
	field_type: str = Field(description=(
		"Resolved type: text | email | tel | url | number | date | password | "
		"select | textarea | checkbox | radio | radio-group | checkbox-group | "
		"button-group | file | search | toggle | range | hidden | unknown"
	))
	label: str = Field(description="Resolved accessible name (after label chain resolution)")
	raw_label: str = Field(default="", description="Original label text before sanitization")
	name: str = Field(default="", description="HTML name attribute or accessible name")
	value: str = Field(default="", description="Current value of the field")
	placeholder: str = Field(default="", description="Placeholder text")
	required: bool = Field(default=False)
	required_signals: list[str] = Field(default_factory=list, description=(
		"Signals that contributed to required detection "
		"(e.g. 'aria_required', 'label_asterisk', 'label_required_text')"
	))
	options: list[FieldOption] = Field(default_factory=list, description="For selects/radios — available choices")
	choices: list[str] = Field(default_factory=list, description="For radio/checkbox groups and button groups")
	section: str = Field(default="", description="Form section heading resolved from ancestor h1-h3/legend")
	visible: bool = Field(default=True)
	is_native: bool = Field(default=False, description="True for native <select> elements")
	is_multi_select: bool = Field(default=False, description="True for multi-select custom dropdowns")
	accept: str = Field(default="", description="File input accept attribute")
	label_sources: list[str] = Field(default_factory=list, description=(
		"Where the label was sourced from "
		"(e.g. 'aria-labelledby', 'aria-label', 'label[for]', 'legend', 'placeholder')"
	))
	synthetic_label: bool = Field(default=False, description="True if the label was generated synthetically")
	observation_warnings: list[str] = Field(default_factory=list, description="Warnings about observation quality")
	field_fingerprint: str = Field(default="", description="Stable identity fingerprint for tracking")
	item_label: str = Field(default="", description="For checkbox/radio individual items — the item's own label")
	item_value: str = Field(default="", description="For checkbox/radio individual items — the item's value")
	btn_ids: list[str] = Field(default_factory=list, description="For button groups — ff_ids of member buttons")
	disabled: bool = Field(default=False)


class ValidationError(BaseModel):
	"""A validation error associated with a form field."""

	model_config = ConfigDict(extra='forbid')

	field_ff_id: str = Field(default="", description="ff_id of the field this error relates to")
	field_key: str = Field(default="", description="Stable key of the field")
	messages: list[str] = Field(default_factory=list)


class ValidationSnapshot(BaseModel):
	"""Captured validation errors — both page-level summaries and per-field inline errors."""

	model_config = ConfigDict(extra='forbid')

	summary_items: list[str] = Field(default_factory=list, description="Page-level error messages")
	inline_by_field_key: dict[str, list[str]] = Field(
		default_factory=dict,
		description="Mapping of field_key -> list of inline error messages",
	)


class ExtractionResult(BaseModel):
	"""Result of extracting all form fields from a page."""

	model_config = ConfigDict(extra='forbid')

	fields: list[FormField]
	page_title: str = ""
	page_url: str = ""
	form_count: int = 0
	has_submit_button: bool = False
	validation: ValidationSnapshot = Field(default_factory=ValidationSnapshot)
