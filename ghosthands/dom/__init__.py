"""DOM module — DomHand: DOM-first form fill gate before browser-use generic fallback.

Public API:
- ``extract_form_fields()`` — main entry point, extracts all form fields from a page
- ``inject_helpers()`` / ``ensure_helpers()`` — inject/re-inject shadow DOM traversal
- ``capture_validation_errors()`` — read validation errors from the page
- ``FormField``, ``FieldOption``, ``ExtractionResult`` — Pydantic models
"""

from ghosthands.dom.field_extractor import extract_form_fields
from ghosthands.dom.shadow_helpers import ensure_helpers, inject_helpers
from ghosthands.dom.validation_reader import capture_validation_errors
from ghosthands.dom.views import ExtractionResult, FieldOption, FormField, ValidationSnapshot

__all__ = [
	"extract_form_fields",
	"inject_helpers",
	"ensure_helpers",
	"capture_validation_errors",
	"ExtractionResult",
	"FieldOption",
	"FormField",
	"ValidationSnapshot",
]
