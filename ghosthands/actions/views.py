"""Pydantic models for DomHand action parameters and results."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FormField(BaseModel):
    """A single form field extracted from the page DOM."""

    model_config = ConfigDict(extra="ignore")

    field_id: str = Field(description="Unique DOM-assigned ID (e.g. data-ff-id)")
    name: str = Field(description="Human-readable label for the field")
    field_type: str = Field(description="Input type: text, email, select, checkbox, radio, file, textarea, etc.")
    section: str = Field(default="", description="Section/group this field belongs to")
    required: bool = Field(default=False, description="Whether the field is required")
    options: list[str] = Field(default_factory=list, description="Available options for select/radio/checkbox fields")
    choices: list[str] = Field(default_factory=list, description="Alternative choice list (some ATS platforms)")
    accept: str | None = Field(default=None, description="Accepted file types for file inputs")
    is_native: bool = Field(default=False, description="Whether this is a native HTML element vs custom widget")
    is_multi_select: bool = Field(default=False, description="Whether multiple selections are allowed")
    visible: bool = Field(default=True, description="Whether the field is currently visible")
    raw_label: str | None = Field(default=None, description="Original label text before cleanup")
    synthetic_label: bool = Field(default=False, description="True if label was generated synthetically")
    field_fingerprint: str | None = Field(default=None, description="Stable fingerprint for identity tracking")
    current_value: str = Field(default="", description="Current value in the field")
    widget_kind: str | None = Field(default=None, description="Internal widget classification for special handling")
    component_field_ids: list[str] = Field(
        default_factory=list,
        description="Internal child field ids for grouped widgets such as segmented date inputs",
    )
    has_calendar_trigger: bool = Field(
        default=False,
        description="True when the grouped widget exposes a visible calendar/date trigger affordance",
    )
    placeholder: str = Field(
        default="",
        description="Visible placeholder text captured from extraction for routing and matching.",
    )
    format_hint: str | None = Field(default=None, description="Optional visible format/placeholder hint")
    name_attr: str = Field(
        default="",
        description="HTML name attribute from extraction (used for label/focus matching when ARIA name is camelCase)",
    )


class FillFieldResult(BaseModel):
    """Result of attempting to fill a single field."""

    model_config = ConfigDict(extra="ignore")

    field_id: str
    field_key: str = ""
    name: str
    success: bool
    actor: str = Field(description="Outcome owner: 'dom', 'skipped', or 'unfilled'")
    error: str | None = None
    value_set: str | None = None
    required: bool = False
    control_kind: str = ""
    section: str = ""
    source: str | None = None
    answer_mode: str | None = None
    confidence: float | None = None
    fill_confidence: float = Field(
        default=0.0,
        description=(
            "How confident we are the value actually committed to the DOM. "
            "1.0=DOM verified, 0.8=LLM verified, 0.6=Stagehand verified, 0.0=failed."
        ),
    )
    state: str | None = None
    failure_reason: str | None = None
    takeover_suggestion: str | None = None
    binding_mode: str | None = None
    binding_confidence: str | None = None
    best_effort_guess: bool = False
    repeater_group: str | None = None
    slot_name: str | None = None
    diagnostic_stage: str | None = None


class DomHandFillParams(BaseModel):
    """Fill all visible form fields using fast DOM manipulation."""

    target_section: str | None = Field(
        None,
        description="Optional section name to fill. If null, fills all visible sections.",
    )
    heading_boundary: str | None = Field(
        None,
        description=(
            "Restrict filling to fields BELOW this heading and ABOVE the next sibling heading. "
            'Use for repeater entries, e.g. heading_boundary="Work Experience 2" to fill only '
            "the second work experience entry without touching others."
        ),
    )
    focus_fields: list[str] | None = Field(
        None,
        description=(
            "Optional list of specific field labels to focus on within the current section. "
            "Use this after domhand_assess_state identifies unresolved fields so DomHand can "
            "target those exact blockers instead of refilling the whole page subsection."
        ),
    )
    entry_data: dict | None = Field(
        None,
        description=(
            "Structured data for a single repeater entry. When provided, this overrides the "
            "full profile for LLM answer generation. Example: "
            '{"title": "Software Engineer", "company": "Google", "start_date": "06/2022", '
            '"end_date": "Present", "description": "Built distributed systems..."}'
        ),
    )
    use_auth_credentials: bool = Field(
        False,
        description=(
            "When true, domhand_fill uses GH_EMAIL/GH_PASSWORD for auth-like fields "
            "(email, password, confirm password) instead of the applicant profile."
        ),
    )


class DomHandSelectParams(BaseModel):
    """Select a dropdown option using platform-aware discovery."""

    index: int | None = Field(
        default=None,
        description="Optional element index of the dropdown trigger when already known.",
    )
    value: str = Field(description="Value or text to select")
    field_id: str | None = Field(
        default=None,
        description="Optional exact field id from domhand_assess_state for blocker-aware targeting.",
    )
    field_label: str | None = Field(
        default=None,
        description="Optional field label used for blocker-aware targeting and tracing.",
    )
    target_section: str | None = Field(
        default=None,
        description="Optional section name used for blocker-aware tracing.",
    )


class DomHandInteractControlParams(BaseModel):
    """Interact with a specific non-text control by field label and desired value."""

    field_label: str = Field(description="Exact or near-exact question/field label to target")
    desired_value: str = Field(description="Desired option/value, e.g. 'No', 'LinkedIn', 'United States'")
    field_id: str | None = Field(
        default=None,
        description="Optional exact field id from domhand_assess_state to target one blocker precisely.",
    )
    field_type: str | None = Field(
        default=None,
        description="Optional field type hint paired with field_id for repeated labels.",
    )
    target_section: str | None = Field(
        default=None,
        description="Optional section name used to narrow field matching before interaction.",
    )
    heading_boundary: str | None = Field(
        default=None,
        description="Optional repeater heading boundary to keep control interaction scoped to one entry.",
    )


class DomHandRecordExpectedValueParams(BaseModel):
    """Record the intended value for one visible field after a raw manual recovery action."""

    field_label: str = Field(description="Exact or near-exact field/question label to record")
    expected_value: str = Field(description="Intended visible value after the manual recovery action")
    target_section: str | None = Field(
        default=None,
        description="Optional section name used to narrow field matching before recording.",
    )
    heading_boundary: str | None = Field(
        default=None,
        description="Optional repeater heading boundary to keep field matching scoped to one entry.",
    )
    field_id: str | None = Field(
        default=None,
        description="Optional exact field id from a prior assessment to disambiguate repeated labels.",
    )
    field_type: str | None = Field(
        default=None,
        description="Optional field type hint to disambiguate repeated labels.",
    )


class DomHandUploadParams(BaseModel):
    """Upload a file (resume, cover letter) to a file input."""

    index: int = Field(description="Element index of the file input")
    file_type: str = Field(default="resume", description="Type: 'resume' or 'cover_letter'")


class DomHandExpandParams(BaseModel):
    """Click "Add More" buttons to expand repeater sections."""

    section: str = Field(description="Section name containing the repeater")


ApplicationTerminalState = Literal[
    "editing",
    "advanceable",
    "review",
    "confirmation",
    "presubmit_single_page",
]
ScrollBias = Literal["up", "down", "stay", "none"]
RelativePosition = Literal["above", "in_view", "below", "unknown"]


class ApplicationFieldIssue(BaseModel):
    """A field-level issue surfaced by runtime application-state assessment."""

    model_config = ConfigDict(extra="ignore")

    field_id: str
    name: str
    field_type: str
    section: str = ""
    section_path: str = ""
    required: bool = False
    reason: str = ""
    relative_position: RelativePosition = "unknown"
    takeover_suggestion: str | None = None
    question_text: str | None = None
    current_value: str = ""
    visible_error: str | None = None
    widget_kind: str | None = None
    options: list[str] = Field(default_factory=list)


class ApplicationState(BaseModel):
    """Runtime view of the current application page for planner decisions."""

    model_config = ConfigDict(extra="ignore")

    terminal_state: ApplicationTerminalState
    current_section: str = ""
    unresolved_required_fields: list[ApplicationFieldIssue] = Field(default_factory=list)
    unresolved_optional_fields: list[ApplicationFieldIssue] = Field(default_factory=list)
    mismatched_fields: list[ApplicationFieldIssue] = Field(default_factory=list)
    opaque_fields: list[ApplicationFieldIssue] = Field(default_factory=list)
    unverified_fields: list[ApplicationFieldIssue] = Field(default_factory=list)
    visible_errors: list[str] = Field(default_factory=list)
    scroll_bias: ScrollBias = "none"
    submit_visible: bool = False
    submit_disabled: bool = False
    advance_visible: bool = False
    advance_disabled: bool = False
    advance_allowed: bool = False
    platform_hint: str | None = None


class DomHandAssessStateParams(BaseModel):
    """Assess current application state for scrolling, advancement, or stop decisions."""

    target_section: str | None = Field(
        None,
        description="Optional section name to bias current-section detection around the active area.",
    )


class DomHandClosePopupParams(BaseModel):
    """Dismiss a blocking popup, modal, or interstitial overlay."""

    target_text: str | None = Field(
        None,
        description=(
            "Optional text hint from the popup body, title, or close control. "
            'Examples: "cookies", "not ready to apply", "newsletter", "close".'
        ),
    )


class DomHandRequestUserInputParams(BaseModel):
    """Pause for a user-provided answer for one required field."""

    field_label: str = Field(description="Human-readable label for the missing required field")
    field_id: str | None = Field(
        default=None,
        description="Stable DOM field identifier when available",
    )
    field_type: str = Field(
        default="text",
        description="Field type: text, textarea, select, radio, checkbox, number, date, etc.",
    )
    question_text: str | None = Field(
        default=None,
        description="Exact question text to show the user",
    )
    section: str | None = Field(
        default=None,
        description="Form section this field belongs to",
    )
    options: list[str] = Field(
        default_factory=list,
        description="Available options for select/radio/checkbox style fields",
    )
    timeout_seconds: int | None = Field(
        default=None,
        description="Optional override for how long to wait before giving up",
    )


# ── Matching utilities ──────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(
    r"^(select\.{0,3}|select…|please\s+select(\s+one)?|select\s+(one|an?\s+option)"
    r"|choose\.{0,3}|choose…|please\s+choose(\s+one)?|choose\s+one|pick"
    r"|start\s+typing|enter\s+(your|an?)\s+\S+"
    r"|type\s+here|--+\s*(select|choose)?\s*--*|—"
    r"|no\s+response|no\s+answer|not\s+provided|not\s+specified|not\s+entered|not\s+supplied)$",
    re.IGNORECASE,
)


def is_placeholder_value(value: str) -> bool:
    """Return True if the value looks like a placeholder (e.g. "Select one")."""
    return bool(_PLACEHOLDER_RE.match(value.strip()))


def normalize_name(s: str) -> str:
    """Normalize a field name for comparison: strip asterisks/underscores/apostrophes, collapse whitespace, lowercase."""
    return re.sub(r"\s+", " ", s.replace("*", "").replace("_", " ").replace("'", "").replace("\u2019", "")).strip().lower()


def split_dropdown_value_hierarchy(value: str) -> list[str]:
    """Split hierarchical dropdown labels such as "Category > Option" into ordered segments."""
    raw = re.sub(r"\s+", " ", (value or "").strip())
    if not raw:
        return []
    parts = [part.strip() for part in re.split(r"\s*(?:>|→)\s*", raw) if part.strip()]
    return parts or [raw]


def generate_dropdown_search_terms(value: str) -> list[str]:
    """Build generic fallback search terms for searchable dropdowns and typeaheads."""
    raw = re.sub(r"\s+", " ", (value or "").strip())
    if not raw:
        return []

    seen: set[str] = set()
    terms: list[str] = []
    stop_words = {
        "of",
        "and",
        "in",
        "the",
        "a",
        "an",
        "for",
        "to",
        "with",
        "or",
        "at",
        "by",
        # EEO / decline phrases: never type these alone into react-select (e.g. "answer").
        "answer",
        "wish",
        "disclose",
        "identify",
        "self",
        "provided",
        "supplied",
        "decline",
        "prefer",
    }
    # Do not expand the Mobile/Cell cluster when the value is clearly a phone *number* — typing
    # "Mobile" into the number input is a common failure mode for react-select phone widgets.
    digit_count = sum(1 for ch in raw if ch.isdigit())
    synonym_clusters: list[list[str]] = [
        ["United States +1", "United States", "USA", "US", "+1"],
    ]
    if digit_count < 7:
        synonym_clusters.append(["Mobile", "Mobile phone", "Cell", "Cell phone"])

    def add(term: str) -> None:
        cleaned = re.sub(r"\s+", " ", term.strip())
        if not cleaned:
            return
        key = normalize_name(cleaned)
        if not key or key in seen:
            return
        seen.add(key)
        terms.append(cleaned)

    add(raw)
    raw_norm = normalize_name(raw)
    for cluster in synonym_clusters:
        cluster_norms = [normalize_name(item) for item in cluster]
        if any(raw_norm == item_norm or raw_norm in item_norm or item_norm in raw_norm for item_norm in cluster_norms):
            for item in cluster:
                add(item)
    for part in split_dropdown_value_hierarchy(raw):
        add(part)
        words = [word for word in re.split(r"\s+", part) if len(word) > 1]
        meaningful_words = [word for word in words if word.lower() not in stop_words]
        # Only add word shards when they are long enough to be useful search tokens;
        # short words like "do", "not" pollute react-select filters.
        if len(meaningful_words) > 1:
            for word in meaningful_words:
                if len(word) >= 4:
                    add(word)

    return terms


def get_stable_field_key(field: FormField) -> str:
    """Build a stable key for a field for cross-round identity tracking."""
    fp = normalize_name(field.field_fingerprint or "")
    if fp:
        return f"{normalize_name(field.field_type)}|{fp}"
    semantic_key = "|".join(
        [
            normalize_name(field.field_type),
            normalize_name(field.section),
            normalize_name(field.name or field.raw_label or ""),
        ]
    )
    if semantic_key.replace("|", "").strip():
        return semantic_key
    field_id = normalize_name(field.field_id or "")
    if field_id:
        return f"{normalize_name(field.field_type)}|{field_id}"
    return normalize_name(field.field_type)
