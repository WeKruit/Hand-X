"""Post-fill verification for dropdown selections — single source of truth.

Centralizes the "did the UI actually commit the value we wanted?" check that
was previously duplicated between ``_field_value_matches_expected`` in
``domhand_fill.py`` and the CDP-first wait paths.

Key insight: after a dropdown click the **committed UI text** may differ from
the original *desired answer* — e.g. we wanted "Male" but the option is
"Man".  The verify layer accepts an optional *matched_label* (the label
actually clicked) and considers the selection successful when the UI shows
either the desired answer OR the matched label.  Synonym equivalence from
``dropdown_match`` is also honoured.
"""

from __future__ import annotations

import re
from typing import Any

from ghosthands.actions.views import normalize_name, split_dropdown_value_hierarchy
from ghosthands.dom.dropdown_match import are_synonyms

# ── Placeholder / unset detection ─────────────────────────────────────

_SELECT_PLACEHOLDER_FRAGMENT_RE = re.compile(
    r"^(?:select|choose|pick|-- ?select|-- ?choose|-- ?please|please select|-- ?none ?--)",
    re.IGNORECASE,
)


def _is_effectively_unset(value: str | None) -> bool:
    """True for empty, whitespace-only, or placeholder-style values."""
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return True
    if _SELECT_PLACEHOLDER_FRAGMENT_RE.search(text):
        return True
    return False


# ── Binary normalisation (Yes/No) ────────────────────────────────────

def _normalize_binary(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    norm = normalize_name(str(value))
    if not norm:
        return None
    if norm in {"yes", "y", "true", "checked", "on", "1"}:
        return "Yes"
    if norm in {"no", "n", "false", "unchecked", "off", "0"}:
        return "No"
    if re.search(r"\b(do not|dont|don t|disagree|decline|reject)\b", norm):
        return "No"
    if re.search(r"\b(acknowledge|agree|accept|consent|confirm)\b", norm):
        return "Yes"
    return None


# ── Date normalisation ────────────────────────────────────────────────

def _parse_date(value: str | None) -> tuple[int, int, int] | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = re.sub(r"[.\-]", "/", text)
    parts = [p.strip() for p in normalized.split("/") if p.strip()]
    if len(parts) != 3:
        return None
    try:
        if len(parts[0]) == 4:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts[2]) == 4:
            month, day, year = int(parts[0]), int(parts[1]), int(parts[2])
        else:
            return None
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 9999):
        return None
    return (year, month, day)


# ── Country + phone code helpers ──────────────────────────────────────

_PHONE_CODE_SUFFIX_RE = re.compile(r"\s*\+\d{1,4}\s*$")


def _strip_phone_code(text: str) -> str:
    """Remove trailing phone-code suffix like '+1', '+44', '+91' from a country name."""
    return _PHONE_CODE_SUFFIX_RE.sub("", text).strip()


def _country_phone_code_match(current_norm: str, desired_norm: str) -> bool:
    """Check if *current* is *desired* with an appended phone code.

    Handles cases like "united states 1" matching "united states" after
    normalization strips the '+'.  Also handles raw text comparisons.
    """
    if current_norm.startswith(desired_norm) and len(current_norm) > len(desired_norm):
        remainder = current_norm[len(desired_norm):].strip()
        if re.fullmatch(r"\+?\d{1,4}", remainder):
            return True
    return False


# ── Public API ────────────────────────────────────────────────────────

def selection_matches_desired(
    current: str,
    desired: str,
    matched_label: str | None = None,
) -> bool:
    """Return ``True`` when the visible field value reflects the intended selection.

    Parameters
    ----------
    current:
        The value currently shown in the UI (read from the DOM).
    desired:
        The answer we originally wanted to commit.
    matched_label:
        Optional — the **actual option text** that was clicked (which may
        differ from *desired* after fuzzy matching, e.g. "Man" when desired
        was "Male").

    The function checks (in order):
    1. Direct normalized match with desired or matched_label.
    2. Binary equivalence (Yes/No collapse).
    3. Date equivalence.
    4. Substring containment (either direction) for desired or matched_label.
    5. Synonym equivalence between current and desired (via ``dropdown_match``).
    6. Hierarchical segment match (last segment of "Category > Option").
    """
    current_text = (current or "").strip()
    desired_text = (desired or "").strip()
    if not current_text or not desired_text:
        return False
    if _is_effectively_unset(current_text):
        return False

    current_norm = normalize_name(current_text)
    desired_norm = normalize_name(desired_text)
    if not current_norm or not desired_norm:
        return False

    matched_norm = normalize_name(matched_label) if matched_label else None

    # Direct exact
    if current_norm == desired_norm:
        return True
    if matched_norm and current_norm == matched_norm:
        return True

    # Country + phone code: "United States +1" matches "United States"
    if _country_phone_code_match(current_norm, desired_norm):
        return True
    if matched_norm and _country_phone_code_match(current_norm, matched_norm):
        return True

    # Binary collapse
    cur_bin = _normalize_binary(current_text)
    des_bin = _normalize_binary(desired_text)
    if cur_bin and des_bin:
        return cur_bin == des_bin

    # Date
    cd = _parse_date(current_text)
    dd = _parse_date(desired_text)
    if cd and dd and cd == dd:
        return True

    # Substring containment
    if desired_norm in current_norm or current_norm in desired_norm:
        return True
    if matched_norm and (matched_norm in current_norm or current_norm in matched_norm):
        return True

    # Synonym
    if are_synonyms(current_text, desired_text):
        return True
    if matched_label and are_synonyms(current_text, matched_label):
        return True

    # Hierarchical segment
    segments = split_dropdown_value_hierarchy(desired_text)
    if segments:
        final = normalize_name(segments[-1])
        if final and final in current_norm:
            return True

    return False
