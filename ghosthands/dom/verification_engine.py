"""Unified verification engine for DomHand — deterministic DOM readback + normalize + fuzzy compare.

Aligns with GHOST-HANDS v3 ``VerificationEngine.ts``: after every fill action,
read the DOM, normalize, fuzzy-match against expected.  **No LLM for verification.**

Both ``domhand_fill`` reconciliation and ``domhand_assess_state`` must call this
module so verification semantics never diverge.

Two-axis per-field contract (cross-AI review consensus):
  - ``execution_status``: already_settled | executed | execution_failed | not_attempted | retry_capped
  - ``review_status``: verified | mismatch | unreadable | unsupported
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)

# ── Two-axis enums ────────────────────────────────────────────────────────

ExecutionStatus = Literal[
    "already_settled",  # field matched expected before any action (v3 already_filled)
    "executed",  # fill action was attempted
    "execution_failed",  # fill action threw or returned failure
    "not_attempted",  # field was not targeted (skipped, unsupported type)
    "retry_capped",  # field hit per-field retry limit
]

ReviewStatus = Literal[
    "verified",  # DOM readback matches expected after normalization
    "mismatch",  # DOM readback non-empty but disagrees with expected
    "unreadable",  # DOM readback empty or opaque (custom widget, shadow DOM)
    "unsupported",  # field type has no readback path (file upload, etc.)
]

# ── Result types ──────────────────────────────────────────────────────────


@dataclass
class FieldReviewResult:
    """One field's verification outcome (v3 ReviewResult equivalent)."""

    field_id: str
    label: str
    field_type: str
    required: bool
    execution_status: ExecutionStatus
    review_status: ReviewStatus
    reason: str  # human-readable why (e.g. "DOM readback matched", "readback empty after 2.5s poll")
    actual_read: str = ""  # what DOM says now (may be truncated for PII)
    expected_summary: str = ""  # type + length, NOT raw PII for agent digest
    has_validation_error: bool = False


@dataclass
class PageReviewSummary:
    """Page-level rollup of verification results."""

    verified_count: int = 0
    mismatch_count: int = 0
    unreadable_count: int = 0
    already_settled_count: int = 0
    execution_failed_count: int = 0
    unsupported_count: int = 0
    total_attempted: int = 0
    fields: list[FieldReviewResult] = field(default_factory=list)


# ── Normalization (port v3 VerificationEngine + Hand-X existing) ─────────

# Common placeholder / unset values for selects
_UNSET_PATTERNS = re.compile(
    r"^(select\b|choose\b|-- ?select|—|--|please select|select one|"
    r"select\.\.\.|choose\.\.\.|none selected|pick one|select an option)$",
    re.IGNORECASE,
)

# Opaque widget values (Oracle cx-select internals, UUID-like, etc.)
_OPAQUE_VALUE_RE = re.compile(
    r"^[0-9a-f]{8,}$|"  # hex IDs
    r"^[0-9a-f]{8}-[0-9a-f]{4}-|"  # UUIDs
    r"^\[object\s|"  # JS object toString
    r"^undefined$|^null$|^NaN$",
    re.IGNORECASE,
)

# Phone: v3 strips to digits, compares last 7
_PHONE_DIGITS_RE = re.compile(r"[^0-9]")

# Date: v3 strips to digits
_DATE_DIGITS_RE = re.compile(r"[^0-9]")

# Checkbox truthy set (v3 parity)
_CHECKBOX_TRUTHY = frozenset({"true", "yes", "1", "checked", "on"})

# Country code aliases (common ATS normalization mismatches)
_COUNTRY_ALIASES: dict[str, set[str]] = {
    "united states": {"us", "usa", "united states of america", "u.s.", "u.s.a."},
    "united kingdom": {"uk", "great britain", "gb", "england"},
    "china": {"cn", "people's republic of china", "prc"},
}
# Build reverse map
_COUNTRY_REVERSE: dict[str, str] = {}
for _canon, _aliases in _COUNTRY_ALIASES.items():
    for _alias in _aliases:
        _COUNTRY_REVERSE[_alias] = _canon
    _COUNTRY_REVERSE[_canon] = _canon

# US state abbreviations
_US_STATE_ABBREV: dict[str, str] = {
    "al": "alabama",
    "ak": "alaska",
    "az": "arizona",
    "ar": "arkansas",
    "ca": "california",
    "co": "colorado",
    "ct": "connecticut",
    "de": "delaware",
    "fl": "florida",
    "ga": "georgia",
    "hi": "hawaii",
    "id": "idaho",
    "il": "illinois",
    "in": "indiana",
    "ia": "iowa",
    "ks": "kansas",
    "ky": "kentucky",
    "la": "louisiana",
    "me": "maine",
    "md": "maryland",
    "ma": "massachusetts",
    "mi": "michigan",
    "mn": "minnesota",
    "ms": "mississippi",
    "mo": "missouri",
    "mt": "montana",
    "ne": "nebraska",
    "nv": "nevada",
    "nh": "new hampshire",
    "nj": "new jersey",
    "nm": "new mexico",
    "ny": "new york",
    "nc": "north carolina",
    "nd": "north dakota",
    "oh": "ohio",
    "ok": "oklahoma",
    "or": "oregon",
    "pa": "pennsylvania",
    "ri": "rhode island",
    "sc": "south carolina",
    "sd": "south dakota",
    "tn": "tennessee",
    "tx": "texas",
    "ut": "utah",
    "vt": "vermont",
    "va": "virginia",
    "wa": "washington",
    "wv": "west virginia",
    "wi": "wisconsin",
    "wy": "wyoming",
    "dc": "district of columbia",
}
_US_STATE_REVERSE: dict[str, str] = {}
for _abbr, _full in _US_STATE_ABBREV.items():
    _US_STATE_REVERSE[_abbr] = _full
    _US_STATE_REVERSE[_full] = _full


def normalize_basic(s: str) -> str:
    """Trim, lowercase, collapse whitespace (v3 parity)."""
    return " ".join(s.strip().split()).lower()


def normalize_phone_digits(s: str) -> str:
    """Strip to digits only (v3 parity)."""
    return _PHONE_DIGITS_RE.sub("", s)


def normalize_date_digits(s: str) -> str:
    """Strip to digits only for date comparison (v3 parity)."""
    return _DATE_DIGITS_RE.sub("", s)


def is_value_opaque(value: str) -> bool:
    """Check if value is an opaque widget internal (Oracle cx-select, UUID, etc.)."""
    v = value.strip()
    if not v:
        return False
    return bool(_OPAQUE_VALUE_RE.match(v))


def is_value_unset(value: str) -> bool:
    """Check if value is a placeholder or effectively empty."""
    v = value.strip()
    if not v:
        return True
    return bool(_UNSET_PATTERNS.match(v))


# ── Deterministic fuzzy matching (v3 + Hand-X unified) ───────────────────


def values_match(
    actual: str,
    expected: str,
    field_type: str = "",
    matched_label: str | None = None,
    *,
    semantic: bool = False,
) -> bool:
    """Deterministic value comparison — exhausts all normalization rules.

    Delegates to existing ``selection_matches_desired`` for dropdown-family
    fields, and adds v3 parity rules (phone last-7, date digits, country
    aliases, state abbreviations) where the existing matcher doesn't cover.

    When ``semantic=True``, also tries number overlap (salary ranges) and
    token overlap (open-ended questions) — ported from assess_state.
    """
    # 1. Delegate to the existing canonical matcher first
    try:
        from ghosthands.dom.dropdown_verify import selection_matches_desired

        if selection_matches_desired(actual, expected, matched_label=matched_label):
            return True
    except Exception:
        pass

    # 2. Additional v3-parity rules not in existing matcher
    a_norm = normalize_basic(actual)
    e_norm = normalize_basic(expected)

    if not a_norm or not e_norm:
        return False

    # Exact after basic normalization
    if a_norm == e_norm:
        return True

    # Matched label check
    if matched_label:
        ml_norm = normalize_basic(matched_label)
        if ml_norm and a_norm == ml_norm:
            return True

    # Phone: compare last 7 digits (v3 parity)
    if field_type in ("tel", "phone", "text") or "phone" in (field_type or "").lower():
        a_digits = normalize_phone_digits(actual)
        e_digits = normalize_phone_digits(expected)
        if len(a_digits) >= 7 and len(e_digits) >= 7 and a_digits[-7:] == e_digits[-7:]:
            return True

    # Date: compare digit-only versions (v3 parity)
    if field_type in ("date",) or "date" in (field_type or "").lower():
        a_date = normalize_date_digits(actual)
        e_date = normalize_date_digits(expected)
        if a_date and e_date and a_date == e_date:
            return True

    # Country alias normalization
    a_country = _COUNTRY_REVERSE.get(a_norm)
    e_country = _COUNTRY_REVERSE.get(e_norm)
    if a_country and e_country and a_country == e_country:
        return True

    # US state abbreviation normalization
    a_state = _US_STATE_REVERSE.get(a_norm)
    e_state = _US_STATE_REVERSE.get(e_norm)
    if a_state and e_state and a_state == e_state:
        return True

    # Checkbox truthy set (v3 parity)
    if field_type in ("checkbox", "toggle"):
        if a_norm in _CHECKBOX_TRUTHY and e_norm in _CHECKBOX_TRUTHY:
            return True
        if a_norm not in _CHECKBOX_TRUTHY and e_norm not in _CHECKBOX_TRUTHY:
            # Both falsy
            return True

    # Select/radio: contains or starts-with (v3 parity)
    if field_type in ("select", "radio", "radio-group", "button-group") and (a_norm in e_norm or e_norm in a_norm):
        return True

    # General substring fallback (v3 general fallback)
    if len(a_norm) > 3 and len(e_norm) > 3 and (a_norm in e_norm or e_norm in a_norm):
        return True

    # Semantic: number overlap (from assess_state — salary ranges, years, etc.)
    if semantic:
        a_numbers = [int(m) for m in re.findall(r"\d+", actual or "")]
        e_numbers = [int(m) for m in re.findall(r"\d+", expected or "")]
        if a_numbers and e_numbers:
            if set(a_numbers) & set(e_numbers):
                return True
            if len(e_numbers) >= 2:
                low = min(e_numbers[0], e_numbers[1])
                high = max(e_numbers[0], e_numbers[1])
                if any(low <= n <= high for n in a_numbers):
                    return True

        # Semantic: token overlap (from assess_state — open-ended questions)
        a_tokens = _semantic_tokens(a_norm)
        e_tokens = _semantic_tokens(e_norm)
        if a_tokens and e_tokens:
            overlap = a_tokens & e_tokens
            smaller = min(len(a_tokens), len(e_tokens))
            if overlap and (len(overlap) >= min(2, smaller) or (len(overlap) / max(smaller, 1)) >= 0.5):
                return True

    return False


def _semantic_tokens(text: str) -> set[str]:
    """Extract meaningful tokens for semantic comparison (ported from assess_state)."""
    stop_words = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "for",
        "of",
        "is",
        "am",
        "are",
        "was",
        "were",
        "be",
        "been",
        "to",
        "with",
    }
    return {token for token in text.split() if len(token) > 2 and token not in stop_words}


# ── Field reading (delegates to existing primitives) ─────────────────────


async def read_field_actual(page: Any, field: Any) -> tuple[str, str]:
    """Read the current DOM value for a field, respecting field type.

    Returns (actual_value, read_method) where read_method is one of:
    'dom_value', 'binary_state', 'group_selection', 'multi_select', 'grouped_date'.
    """
    ft = getattr(field, "field_type", "") or ""

    if ft in ("checkbox", "toggle"):
        try:
            from ghosthands.dom.fill_executor import _read_binary_state

            state = await _read_binary_state(page, field.field_id)
            return ("checked" if state else "", "binary_state")
        except Exception:
            return ("", "binary_state")

    if ft in ("radio-group", "radio", "button-group"):
        try:
            from ghosthands.dom.fill_executor import _read_group_selection

            selection = await _read_group_selection(page, field.field_id)
            return (str(selection or "").strip(), "group_selection")
        except Exception:
            return ("", "group_selection")

    if ft == "checkbox-group":
        # Exclusive-choice checkbox groups read like radio groups
        try:
            from ghosthands.dom.fill_executor import (
                _checkbox_group_is_exclusive_choice,
                _read_binary_state,
                _read_group_selection,
            )

            if _checkbox_group_is_exclusive_choice(field):
                selection = await _read_group_selection(page, field.field_id)
                return (str(selection or "").strip(), "group_selection")
            else:
                state = await _read_binary_state(page, field.field_id)
                return ("checked" if state else "", "binary_state")
        except Exception:
            return ("", "group_selection")

    # Multi-select (skill fields, etc.)
    try:
        from ghosthands.dom.fill_verify import _uses_multi_select_observation

        if await _uses_multi_select_observation(page, field):
            from ghosthands.dom.fill_executor import _read_multi_select_selection

            selection = await _read_multi_select_selection(page, field.field_id)
            tokens = [str(t or "").strip() for t in (selection.get("tokens") or []) if str(t or "").strip()]
            return (", ".join(tokens), "multi_select")
    except Exception:
        pass

    # Default: use field-type-aware reader
    try:
        from ghosthands.dom.fill_executor import _read_field_value_for_field

        value = await _read_field_value_for_field(page, field)
        return (str(value or "").strip(), "dom_value")
    except Exception:
        return ("", "dom_value")


# ── Core verification ────────────────────────────────────────────────────


async def verify_field(
    page: Any,
    field: Any,
    expected_value: str,
    *,
    execution_status: ExecutionStatus = "executed",
    matched_label: str | None = None,
) -> FieldReviewResult:
    """Verify a single field — the v3 VerificationEngine.verify() equivalent.

    Reads actual from DOM, normalizes, fuzzy-matches.  No LLM.

    Args:
        page: Playwright page handle
        field: FormField with field_id, field_type, name, required
        expected_value: What we tried to set
        execution_status: Pre-set if known (e.g. already_settled, execution_failed)
        matched_label: Actual option text clicked (may differ from expected after fuzzy match)

    Returns:
        FieldReviewResult with two-axis status
    """
    label = getattr(field, "name", "") or getattr(field, "raw_label", "") or ""
    ft = getattr(field, "field_type", "") or ""
    required = bool(getattr(field, "required", False))
    fid = getattr(field, "field_id", "") or ""

    # Build expected summary (PII-safe)
    expected_summary = _make_expected_summary(expected_value, ft)

    # If execution already known to have failed, skip readback
    if execution_status in ("execution_failed", "retry_capped", "not_attempted"):
        return FieldReviewResult(
            field_id=fid,
            label=_truncate(label, 50),
            field_type=ft,
            required=required,
            execution_status=execution_status,
            review_status="unsupported" if execution_status == "not_attempted" else "mismatch",
            reason=f"execution_status={execution_status}",
            expected_summary=expected_summary,
        )

    # File upload fields — no DOM readback path
    if ft == "file":
        return FieldReviewResult(
            field_id=fid,
            label=_truncate(label, 50),
            field_type=ft,
            required=required,
            execution_status=execution_status,
            review_status="unsupported",
            reason="file upload — no DOM readback",
            expected_summary=expected_summary,
        )

    # Read actual value from DOM
    actual, _read_method = await read_field_actual(page, field)

    # Check validation error
    has_error = False
    try:
        from ghosthands.dom.fill_executor import _field_has_validation_error

        has_error = await _field_has_validation_error(page, fid)
    except Exception:
        pass

    # Classify
    if not actual.strip() or is_value_unset(actual):
        return FieldReviewResult(
            field_id=fid,
            label=_truncate(label, 50),
            field_type=ft,
            required=required,
            execution_status=execution_status,
            review_status="unreadable",
            reason="DOM readback empty or placeholder",
            actual_read=_truncate(actual, 30),
            expected_summary=expected_summary,
            has_validation_error=has_error,
        )

    if is_value_opaque(actual):
        return FieldReviewResult(
            field_id=fid,
            label=_truncate(label, 50),
            field_type=ft,
            required=required,
            execution_status=execution_status,
            review_status="unreadable",
            reason="DOM readback is opaque widget value",
            actual_read=_truncate(actual, 30),
            expected_summary=expected_summary,
            has_validation_error=has_error,
        )

    # Fuzzy match
    matched = values_match(actual, expected_value, field_type=ft, matched_label=matched_label)

    if matched and not has_error:
        return FieldReviewResult(
            field_id=fid,
            label=_truncate(label, 50),
            field_type=ft,
            required=required,
            execution_status=execution_status,
            review_status="verified",
            reason="DOM readback matches expected",
            actual_read=_truncate(actual, 30),
            expected_summary=expected_summary,
            has_validation_error=False,
        )

    if has_error:
        return FieldReviewResult(
            field_id=fid,
            label=_truncate(label, 50),
            field_type=ft,
            required=required,
            execution_status=execution_status,
            review_status="mismatch",
            reason=f"validation error present{'; value also mismatched' if not matched else ''}",
            actual_read=_truncate(actual, 30),
            expected_summary=expected_summary,
            has_validation_error=True,
        )

    return FieldReviewResult(
        field_id=fid,
        label=_truncate(label, 50),
        field_type=ft,
        required=required,
        execution_status=execution_status,
        review_status="mismatch",
        reason=f"DOM readback '{_truncate(actual, 40)}' does not match expected",
        actual_read=_truncate(actual, 30),
        expected_summary=expected_summary,
        has_validation_error=False,
    )


def build_review_summary(results: list[FieldReviewResult]) -> PageReviewSummary:
    """Aggregate per-field results into page-level summary."""
    summary = PageReviewSummary(total_attempted=len(results))
    for r in results:
        if r.review_status == "verified":
            summary.verified_count += 1
        elif r.review_status == "mismatch":
            summary.mismatch_count += 1
        elif r.review_status == "unreadable":
            summary.unreadable_count += 1
        elif r.review_status == "unsupported":
            summary.unsupported_count += 1

        if r.execution_status == "already_settled":
            summary.already_settled_count += 1
        elif r.execution_status == "execution_failed":
            summary.execution_failed_count += 1

    summary.fields = results
    return summary


# ── Agent digest (≤1500 chars, PII-redacted) ─────────────────────────────

# PII field patterns — redact actual_read for these
_PII_LABEL_PATTERNS = re.compile(
    r"email|e-mail|phone|mobile|cell|ssn|social security|"
    r"address|street|zip|postal|birth|dob|salary|compensation|"
    r"password|secret|credit card|card number",
    re.IGNORECASE,
)


def _is_pii_field(label: str, field_type: str) -> bool:
    """Check if field likely contains PII based on label or type."""
    if field_type in ("password", "file"):
        return True
    return bool(_PII_LABEL_PATTERNS.search(label))


def _redact_for_digest(value: str, label: str, field_type: str) -> str:
    """Redact PII from actual_read before putting in agent-visible digest."""
    if not value:
        return ""
    if _is_pii_field(label, field_type):
        return f"[{field_type}({len(value)})]"
    if len(value) > 60:
        return value[:30] + "..."
    return value


def _make_expected_summary(expected: str, field_type: str) -> str:
    """Build a PII-safe summary of expected value — type + length, not raw."""
    if not expected:
        return ""
    ft = field_type or "text"
    if len(expected) <= 12 and ft in ("select", "radio", "radio-group", "button-group", "checkbox", "toggle"):
        return expected  # short select options are not PII
    return f"{ft}({len(expected)})"


def build_agent_digest(
    summary: PageReviewSummary,
    max_chars: int = 1500,
) -> str:
    """Build a capped, PII-redacted JSON digest for long_term_memory.

    Structure: totals (~120 chars) + per-field rows for non-verified fields only,
    sorted by required-first + execution_failed first.
    """
    import json

    totals = {
        "verified": summary.verified_count,
        "mismatch": summary.mismatch_count,
        "unreadable": summary.unreadable_count,
        "already_correct": summary.already_settled_count,
        "failed": summary.execution_failed_count,
        "total": summary.total_attempted,
    }

    # Only include non-verified fields in per-field rows
    non_verified = [
        f for f in summary.fields if f.review_status != "verified" and f.execution_status != "already_settled"
    ]
    # Sort: required first, then execution_failed, then mismatch, then unreadable
    status_priority = {"execution_failed": 0, "mismatch": 1, "unreadable": 2, "unsupported": 3}

    def sort_key(r: FieldReviewResult) -> tuple:
        return (
            0 if r.required else 1,
            status_priority.get(r.review_status, 9),
            r.label,
        )

    non_verified.sort(key=sort_key)

    rows = []
    for r in non_verified:
        row = {
            "id": r.field_id,
            "label": _truncate(r.label, 40),
            "type": r.field_type,
            "review": r.review_status,
            "reason": _truncate(r.reason, 60),
        }
        if r.required:
            row["req"] = True
        actual = _redact_for_digest(r.actual_read, r.label, r.field_type)
        if actual:
            row["actual"] = actual
        rows.append(row)

    digest = {"totals": totals, "issues": rows}
    result = json.dumps(digest, ensure_ascii=False, separators=(",", ":"))

    # Truncate if over cap
    if len(result) <= max_chars:
        return result

    # Progressively drop rows from bottom until within cap
    while rows and len(result) > max_chars:
        rows.pop()
        digest["issues"] = rows
        digest["totals"]["issues_truncated"] = True
        result = json.dumps(digest, ensure_ascii=False, separators=(",", ":"))

    return result[:max_chars]  # hard cap


def build_agent_prose(summary: PageReviewSummary) -> str:
    """Build a terse prose summary for extracted_content."""
    parts = []
    if summary.verified_count:
        parts.append(f"{summary.verified_count} verified")
    if summary.already_settled_count:
        parts.append(f"{summary.already_settled_count} already correct")
    if summary.mismatch_count:
        parts.append(f"{summary.mismatch_count} mismatch")
    if summary.unreadable_count:
        parts.append(f"{summary.unreadable_count} unreadable")
    if summary.execution_failed_count:
        parts.append(f"{summary.execution_failed_count} failed")
    if not parts:
        return "DomHand: no fields to fill on this page."
    return f"DomHand review: {', '.join(parts)} of {summary.total_attempted} fields."


# ── Helpers ──────────────────────────────────────────────────────────────


def _truncate(s: str, max_len: int) -> str:
    """Truncate string with ellipsis if needed."""
    if not s:
        return ""
    s = s.strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."
