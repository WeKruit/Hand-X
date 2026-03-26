"""Education, language, and repeater-binding resolution for DomHand fill.

Extracted from ``domhand_fill.py`` — these pure functions resolve profile
data (education entries, language entries) to form-field values via slot-name
classification and structured entry lookups.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ghosthands.actions.views import FormField, get_stable_field_key, normalize_name


def _field_section_name(field: FormField) -> str:
    return normalize_name(field.section or "")


def _field_label_candidates(field: FormField) -> list[str]:
    """Delegates to domhand_fill (late import to break circular dependency)."""
    from ghosthands.actions.domhand_fill import _field_label_candidates as _impl
    return _impl(field)


def _preferred_field_label(field: FormField) -> str:
    from ghosthands.actions.domhand_fill import _preferred_field_label as _impl
    return _impl(field)


def _coerce_answer_to_field(field: FormField, answer: str | None) -> str | None:
    from ghosthands.actions.domhand_fill import _coerce_answer_to_field as _impl
    return _impl(field, answer)


def _field_value_matches_expected(current: str, expected: str) -> bool:
    from ghosthands.actions.domhand_fill import _field_value_matches_expected as _impl
    return _impl(current, expected)


def _parse_heading_index(scope: str | None) -> int | None:
    from ghosthands.actions.domhand_fill import _parse_heading_index as _impl
    return _impl(scope)


def _row_order_binding_index(
    *,
    field: FormField,
    visible_fields: list[FormField],
    slot_name: str,
    slot_resolver: Any,
) -> int | None:
    from ghosthands.actions.domhand_fill import _row_order_binding_index as _impl
    return _impl(field=field, visible_fields=visible_fields, slot_name=slot_name, slot_resolver=slot_resolver)


def _entry_text_value(value: Any) -> str | None:
    from ghosthands.actions.domhand_fill import _entry_text_value as _impl
    return _impl(value)


def get_repeater_field_binding(*args: Any, **kwargs: Any) -> Any:
    """Late import to avoid circular dependency."""
    from ghosthands.runtime_learning import get_repeater_field_binding as _impl
    return _impl(*args, **kwargs)


def record_repeater_field_binding(*args: Any, **kwargs: Any) -> Any:
    """Late import to avoid circular dependency."""
    from ghosthands.runtime_learning import record_repeater_field_binding as _impl
    return _impl(*args, **kwargs)


def _is_education_like_section(value: str | None) -> bool:
    section_norm = normalize_name(value or "")
    return "education" in section_norm or any(token in section_norm for token in ("school", "university", "college"))


def _looks_like_experience_years_prompt(label: str | None) -> bool:
    norm = normalize_name(label or "")
    if not norm:
        return False
    return (
        "work experience" in norm
        and "year" in norm
        and "degree" in norm
    )


def _field_has_education_context(field: FormField, visible_fields: list[FormField] | None = None) -> bool:
    if _is_education_like_section(field.section):
        return True

    labels = [normalize_name(label) for label in _field_label_candidates(field)]
    if any(
        label in {"degree", "degree type", "type of degree", "degree level", "school", "major", "minor"}
        or any(
            token in label
            for token in (
                "field of study",
                "area of study",
                "discipline",
                "concentration",
                "college",
                "university",
                "institution",
                "gpa",
                "grading system",
                "grading scale",
                "honors",
                "honours",
                "honour",
                "start date",
                "end date",
                "graduation date",
            )
        )
        for label in labels
    ):
        return True

    if not visible_fields:
        return False

    section_norm = _field_section_name(field)
    sibling_tokens: set[str] = set()
    for candidate in visible_fields:
        if candidate.field_id == field.field_id:
            continue
        candidate_section = _field_section_name(candidate)
        if section_norm and candidate_section and candidate_section != section_norm:
            continue
        for label in _field_label_candidates(candidate):
            label_norm = normalize_name(label)
            if not label_norm:
                continue
            sibling_tokens.add(label_norm)
    return any(
        any(
            token in label
            for token in (
                "school",
                "university",
                "college",
                "field of study",
                "major",
                "minor",
                "gpa",
                "grading system",
                "grading scale",
                "degree",
                "honors",
                "honours",
                "honour",
            )
        )
        for label in sibling_tokens
    )


def _infer_gpa_scale_from_text(value: Any) -> str | None:
    text = _entry_text_value(value)
    if not text:
        return None
    norm = normalize_name(text)
    if not norm:
        return None

    if "alphabet" in norm:
        return "Alphabetical (A+ to P)"
    if "pass fail" in norm:
        return "Pass/Fail"
    if "percentage" in norm or "percent" in norm:
        return "GPA (out of 100)/Percentage"
    if "out of 12" in norm or "/12" in text:
        return "GPA/Grade (out of 12)"
    if "out of 10" in norm or "/10" in text:
        return "GPA/Grade (out of 10)"
    if "out of 5" in norm or "/5" in text:
        return "GPA/Grade (out of 5)"
    if "out of 4" in norm or "/4" in text:
        return "GPA/Grade (out of 4)"

    match = re.search(r"\b(\d+(?:\.\d+)?)\b", text)
    if not match:
        return None
    try:
        numeric = float(match.group(1))
    except ValueError:
        return None
    if numeric <= 4.3:
        return "GPA/Grade (out of 4)"
    if numeric <= 5.3:
        return "GPA/Grade (out of 5)"
    if numeric <= 10.3:
        return "GPA/Grade (out of 10)"
    if numeric <= 12.3:
        return "GPA/Grade (out of 12)"
    if numeric <= 100.0:
        return "GPA (out of 100)/Percentage"
    return None



def _is_structured_education_field(field: FormField) -> bool:
    labels = [normalize_name(label) for label in _field_label_candidates(field)]
    section_norm = _field_section_name(field)
    if "education" not in section_norm and not any(
        token in section_norm for token in ("school", "university", "college")
    ):
        return False
    if any(label in {"month", "year"} for label in labels):
        return True
    return any(
        any(
            token in label
            for token in (
                "field of study",
                "major",
                "minor",
                "honor",
                "honour",
                "degree type",
                "discipline",
                "gpa",
                "actual or expected",
                "actual expected",
                "expected or actual",
                "from",
                "from date",
                "to",
                "to date",
                "start date",
                "end date",
                "graduation date",
                "completion date",
                "month",
                "year",
            )
        )
        for label in labels
    )



def _is_structured_education_candidate(field: FormField, visible_fields: list[FormField] | None = None) -> bool:
    if _is_structured_education_field(field):
        return True
    slot_name = _education_slot_name(field, visible_fields)
    if slot_name in {
        "field_of_study",
        "gpa",
        "gpa_scale",
        "end_date_type",
        "school",
        "degree",
        "degree_type",
        "minor",
        "honors",
    }:
        return _field_has_education_context(field, visible_fields)
    if slot_name not in {"start_date", "end_date"}:
        return False
    target_section = normalize_name(field.section or "")
    sibling_candidates = [
        candidate
        for candidate in (visible_fields or [])
        if candidate.field_id != field.field_id
        and (
            not target_section
            or not normalize_name(candidate.section or "")
            or normalize_name(candidate.section or "") == target_section
        )
    ]
    for candidate in sibling_candidates:
        if _is_structured_education_field(candidate):
            return True
        candidate_slot = _education_slot_name(candidate, visible_fields)
        if candidate_slot in {
            "school",
            "degree",
            "degree_type",
            "field_of_study",
            "minor",
            "honors",
            "gpa",
            "gpa_scale",
            "end_date_type",
        }:
            return True
    return False



def _language_entry_index_for_field(field: FormField) -> int | None:
    section = field.section or ""
    index = _parse_heading_index(section)
    if index is not None:
        return index - 1
    return None



def _field_binding_identity(field: FormField) -> str:
    return str(field.field_id or get_stable_field_key(field)).strip()


def _structured_language_entry_value_and_source(
    slot_name: str | None,
    entry: dict[str, Any],
) -> tuple[str | None, str | None]:
    if slot_name == "language":
        return _entry_text_and_source(entry, "language", "language_name", "languageName")
    if slot_name == "is_fluent":
        if isinstance(entry.get("isFluent"), bool):
            return ("Yes" if bool(entry.get("isFluent")) else "No"), "isFluent"
        if isinstance(entry.get("is_fluent"), bool):
            return ("Yes" if bool(entry.get("is_fluent")) else "No"), "is_fluent"
        return None, None
    if slot_name == "comprehension":
        speaking_value, speaking_source = _entry_text_and_source(
            entry,
            "speakingListening",
            "speaking_listening",
        )
        if speaking_value:
            return speaking_value, speaking_source
        return _entry_text_and_source(
            entry,
            "overallProficiency",
            "overall_proficiency",
            "lang_proficiency",
            "language_proficiency",
            "languageProficiency",
            "proficiency_level",
            "proficiencyLevel",
        )
    if slot_name == "reading_writing":
        return _entry_text_and_source(entry, "readingWriting", "reading_writing")
    if slot_name == "speaking_listening":
        return _entry_text_and_source(entry, "speakingListening", "speaking_listening")
    if slot_name == "overall_proficiency":
        return _entry_text_and_source(
            entry,
            "overallProficiency",
            "overall_proficiency",
            "lang_proficiency",
            "language_proficiency",
            "languageProficiency",
            "proficiency_level",
            "proficiencyLevel",
        )
    return None, None



def _language_slot_name(field: FormField) -> str | None:
    for label in _field_label_candidates(field):
        name = normalize_name(label)
        if not name:
            continue
        if name in {"language", "language name"} or "preferred language" in name:
            return "language"
        if "i am fluent in this language" in name or name == "fluent" or name == "is fluent":
            return "is_fluent"
        if "comprehension" in name:
            return "comprehension"
        if "reading" in name or "writing" in name:
            return "reading_writing"
        if "speaking" in name or "listening" in name:
            return "speaking_listening"
        if "overall" in name or "language proficiency" in name or "proficiency level" in name:
            return "overall_proficiency"
    return None



def _education_slot_name(field: FormField, visible_fields: list[FormField] | None = None) -> str | None:
    for label in _field_label_candidates(field):
        name = normalize_name(label)
        if not name:
            continue
        if any(token in name for token in ("school", "university", "college", "institution")):
            return "school"
        if any(token in name for token in ("degree type", "type of degree", "degree level")):
            return "degree_type"
        if _looks_like_experience_years_prompt(name):
            return None
        if "degree" in name:
            return "degree"
        if "field of study" in name:
            return "field_of_study"
        if any(token in name for token in ("discipline", "area of study", "concentration")):
            return "field_of_study"
        # "major" matches EEO/demographic copy ("major life activities"); only treat as education major otherwise.
        if "major" in name and "life activit" not in name and "major life" not in name:
            return "field_of_study"
        if any(token in name for token in ("minor", "minor field", "minor subject")):
            return "minor"
        if any(token in name for token in ("honors", "honours", "honour", "honor")):
            return "honors"
        if any(token in name for token in ("grading system", "grading scale", "grade system")):
            return "gpa_scale"
        if "gpa" in name:
            return "gpa"
        if any(token in name for token in ("actual or expected", "actual expected", "expected or actual")):
            return "end_date_type"
        if name in {"from", "from date"} or any(token in name for token in ("start date", "date from", "begin date")):
            return "start_date"
        if name in {"to", "to date"} or any(
            token in name for token in ("end date", "date to", "graduation date", "completion date")
        ):
            return "end_date"
        if name in {"month", "year"} and visible_fields:
            target_section = normalize_name(field.section or "")
            generic_date_fields: list[FormField] = []
            for candidate in visible_fields:
                candidate_section = normalize_name(candidate.section or "")
                if target_section and candidate_section and candidate_section != target_section:
                    continue
                candidate_labels = [normalize_name(text) for text in _field_label_candidates(candidate)]
                if not any(text in {"month", "year"} for text in candidate_labels):
                    continue
                generic_date_fields.append(candidate)
            for index, candidate in enumerate(generic_date_fields):
                if candidate.field_id != field.field_id:
                    continue
                split_index = 1 if len(generic_date_fields) <= 2 else max(2, len(generic_date_fields) // 2)
                return "start_date" if index < split_index else "end_date"
    return None



def _extract_date_component(value: Any, component: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    if component == "year":
        year_match = re.search(r"\b(19|20)\d{2}\b", text)
        return year_match.group(0) if year_match else None

    normalized = text.replace("/", "-").replace(".", "-")
    parts = [part.strip() for part in normalized.split("-") if part.strip()]
    if len(parts) >= 2 and re.fullmatch(r"\d{4}", parts[0]) and re.fullmatch(r"\d{1,2}", parts[1]):
        return str(int(parts[1]))
    if len(parts) >= 2 and re.fullmatch(r"\d{1,2}", parts[0]) and re.fullmatch(r"\d{4}", parts[1]):
        return str(int(parts[0]))

    month_names = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }
    lowered = normalize_name(text)
    for token, month in month_names.items():
        if token in lowered:
            return str(month)
    return None



def _entry_text_and_source(entry: dict[str, Any], *keys: str) -> tuple[str | None, str | None]:
    for key in keys:
        value = entry.get(key)
        text = _entry_text_value(value)
        if text:
            return text, key
    return None, None



def _entry_date_text_and_source(
    entry: dict[str, Any],
    *,
    direct_keys: tuple[str, ...],
    pair_keys: tuple[tuple[str, str], ...] = (),
) -> tuple[str | None, str | None]:
    for key in direct_keys:
        value = entry.get(key)
        if isinstance(value, dict):
            year = str(value.get("year") or value.get("YYYY") or "").strip()
            month = str(value.get("month") or value.get("MM") or "").strip()
            if year and month:
                month_digits = re.sub(r"\D+", "", month)
                if month_digits:
                    return f"{year}-{int(month_digits):02d}", key
                return year, key
            if year:
                return year, key
        if value in (None, "", []):
            continue
        text = str(value).strip()
        if text:
            return text, key

    for year_key, month_key in pair_keys:
        year = str(entry.get(year_key) or "").strip()
        month = str(entry.get(month_key) or "").strip()
        if not year:
            continue
        if not month:
            return year, year_key
        month_digits = re.sub(r"\D+", "", month)
        if month_digits:
            return f"{year}-{int(month_digits):02d}", f"{year_key}+{month_key}"
        return year, year_key

    return None, None



def _structured_education_raw_value_from_entry(
    field: FormField,
    entry: dict[str, Any],
    visible_fields: list[FormField] | None = None,
) -> str | None:
    value, _ = _structured_education_raw_value_and_source_from_entry(field, entry, visible_fields)
    return value



def _structured_education_raw_value_and_source_from_entry(
    field: FormField,
    entry: dict[str, Any],
    visible_fields: list[FormField] | None = None,
) -> tuple[str | None, str | None]:
    slot_name = _education_slot_name(field, visible_fields)
    if not slot_name:
        return None, None

    label_norm = normalize_name(_preferred_field_label(field))
    if slot_name == "school":
        return _entry_text_and_source(entry, "school", "institution")
    if slot_name == "degree":
        return _entry_text_and_source(entry, "degree", "degreeType", "degree_type")
    if slot_name == "degree_type":
        return _entry_text_and_source(entry, "degree_type", "degreeType")
    if slot_name == "field_of_study":
        return _entry_text_and_source(
            entry,
            "field_of_study",
            "fieldOfStudy",
            "field",
            "major",
            "majors",
            "majorName",
            "majorNames",
            "major_name",
            "major_names",
            "area_of_study",
            "areaOfStudy",
            "discipline",
            "concentration",
            "specialization",
        )
    if slot_name == "minor":
        return _entry_text_and_source(entry, "minor", "minors", "minorName", "minorNames", "minor_name", "minor_names")
    if slot_name == "honors":
        return _entry_text_and_source(
            entry,
            "honors",
            "honours",
            "honor",
            "honorsList",
            "honoursList",
            "honors_list",
            "honours_list",
        )
    if slot_name == "gpa":
        return _entry_text_and_source(entry, "gpa")
    if slot_name == "gpa_scale":
        explicit_scale, explicit_source = _entry_text_and_source(
            entry,
            "gpa_scale",
            "gpaScale",
            "grading_system",
            "gradingSystem",
            "gpa_grading_system",
            "gpaGradingSystem",
        )
        if explicit_scale:
            inferred = _infer_gpa_scale_from_text(explicit_scale)
            return inferred or explicit_scale, explicit_source
        inferred_from_gpa = _infer_gpa_scale_from_text(entry.get("gpa"))
        return inferred_from_gpa, "gpa"
    if slot_name == "end_date_type":
        return _entry_text_and_source(entry, "end_date_type", "endDateType", "endDateKind")

    if slot_name == "start_date":
        raw_value, source_key = _entry_date_text_and_source(
            entry,
            direct_keys=("start_date", "startDate", "fromDate", "from_date"),
            pair_keys=(
                ("start_year", "start_month"),
                ("startYear", "startMonth"),
                ("from_year", "from_month"),
                ("fromYear", "fromMonth"),
            ),
        )
    else:
        raw_value, source_key = _entry_date_text_and_source(
            entry,
            direct_keys=("end_date", "endDate", "graduation_date", "graduationDate", "toDate", "to_date"),
            pair_keys=(
                ("end_year", "end_month"),
                ("endYear", "endMonth"),
                ("graduation_year", "graduation_month"),
                ("graduationYear", "graduationMonth"),
                ("to_year", "to_month"),
                ("toYear", "toMonth"),
            ),
        )
        if not raw_value:
            raw_value, source_key = _entry_text_and_source(entry, "expectedGraduation", "expected_graduation")

    if label_norm == "year":
        return _extract_date_component(raw_value, "year"), source_key
    if label_norm == "month":
        return _extract_date_component(raw_value, "month"), source_key
    text = str(raw_value or "").strip()
    return (text or None), source_key



def _structured_education_value_from_entry(
    field: FormField,
    entry: dict[str, Any],
    visible_fields: list[FormField] | None = None,
) -> str | None:
    return _coerce_answer_to_field(
        field,
        _structured_education_raw_value_and_source_from_entry(field, entry, visible_fields)[0],
    )



def _match_entry_by_slot_value(
    *,
    field: FormField,
    slot_name: str,
    entries: list[dict[str, Any]],
    current_value: str,
) -> int | None:
    current_text = str(current_value or "").strip()
    if not current_text:
        return None
    matched: list[int] = []
    for index, entry in enumerate(entries):
        if slot_name in {
            "language",
            "is_fluent",
            "comprehension",
            "reading_writing",
            "speaking_listening",
            "overall_proficiency",
        }:
            expected = str(_structured_language_entry_value_and_source(slot_name, entry)[0] or "").strip()
        elif slot_name == "school":
            expected = str(entry.get("school") or "").strip()
        elif slot_name == "degree":
            expected = _entry_text_value(entry.get("degree")) or ""
        elif slot_name == "degree_type":
            expected = _entry_text_value(entry.get("degree_type") or entry.get("degreeType")) or ""
        elif slot_name == "field_of_study":
            expected = (
                _entry_text_value(
                    entry.get("field_of_study")
                    or entry.get("fieldOfStudy")
                    or entry.get("field")
                    or entry.get("major")
                    or entry.get("majors")
                    or entry.get("majorName")
                    or entry.get("majorNames")
                    or entry.get("major_name")
                    or entry.get("major_names")
                    or entry.get("area_of_study")
                    or entry.get("areaOfStudy")
                    or entry.get("discipline")
                    or entry.get("concentration")
                )
                or ""
            )
        elif slot_name == "minor":
            expected = (
                _entry_text_value(
                    entry.get("minor")
                    or entry.get("minors")
                    or entry.get("minorName")
                    or entry.get("minorNames")
                    or entry.get("minor_name")
                    or entry.get("minor_names")
                )
                or ""
            )
        elif slot_name == "honors":
            expected = (
                _entry_text_value(
                    entry.get("honors")
                    or entry.get("honours")
                    or entry.get("honor")
                    or entry.get("honorsList")
                    or entry.get("honoursList")
                    or entry.get("honors_list")
                    or entry.get("honours_list")
                )
                or ""
            )
        elif slot_name == "gpa":
            expected = _entry_text_value(entry.get("gpa")) or ""
        elif slot_name == "gpa_scale":
            expected = (
                _infer_gpa_scale_from_text(
                    entry.get("gpa_scale")
                    or entry.get("gpaScale")
                    or entry.get("grading_system")
                    or entry.get("gradingSystem")
                    or entry.get("gpa")
                )
                or ""
            )
        elif slot_name == "start_date":
            expected, _ = _entry_date_text_and_source(
                entry,
                direct_keys=("start_date", "startDate", "fromDate", "from_date"),
                pair_keys=(
                    ("start_year", "start_month"),
                    ("startYear", "startMonth"),
                    ("from_year", "from_month"),
                    ("fromYear", "fromMonth"),
                ),
            )
            expected = str(expected or "").strip()
        elif slot_name == "end_date":
            expected, _ = _entry_date_text_and_source(
                entry,
                direct_keys=("end_date", "endDate", "graduation_date", "graduationDate", "toDate", "to_date"),
                pair_keys=(
                    ("end_year", "end_month"),
                    ("endYear", "endMonth"),
                    ("graduation_year", "graduation_month"),
                    ("graduationYear", "graduationMonth"),
                    ("to_year", "to_month"),
                    ("toYear", "toMonth"),
                ),
            )
            if not expected:
                expected = str(entry.get("expectedGraduation") or entry.get("expected_graduation") or "").strip()
        elif slot_name == "end_date_type":
            expected = str(
                entry.get("end_date_type") or entry.get("endDateKind") or entry.get("endDateType") or ""
            ).strip()
        else:
            expected = ""
        if expected and _field_value_matches_expected(current_text, expected):
            matched.append(index)
    return matched[0] if len(matched) == 1 else None



def _resolve_repeater_binding(
    *,
    host: str,
    repeater_group: str,
    field: FormField,
    visible_fields: list[FormField],
    entries: list[dict[str, Any]],
    numeric_index: int | None,
    slot_name: str | None,
    current_value: str,
    slot_resolver,
) -> Any:
    from ghosthands.actions.domhand_fill import ResolvedFieldBinding
    if not entries:
        return None
    if len(entries) == 1:
        return ResolvedFieldBinding(
            entry_index=0,
            binding_mode="exact",
            binding_confidence="high",
            best_effort_guess=False,
        )

    field_binding_key = _field_binding_identity(field)
    cached = get_repeater_field_binding(
        host=host,
        repeater_group=repeater_group,
        field_binding_key=field_binding_key,
    )
    if cached and 0 <= cached.entry_index < len(entries):
        return ResolvedFieldBinding(
            entry_index=cached.entry_index,
            binding_mode=cached.binding_mode,
            binding_confidence=cached.binding_confidence,
            best_effort_guess=cached.best_effort_guess,
        )

    if numeric_index is not None and 0 <= numeric_index < len(entries):
        binding = ResolvedFieldBinding(
            entry_index=numeric_index,
            binding_mode="exact",
            binding_confidence="high",
            best_effort_guess=False,
        )
        record_repeater_field_binding(
            host=host,
            repeater_group=repeater_group,
            field_binding_key=field_binding_key,
            entry_index=binding.entry_index,
            binding_mode="exact",
            binding_confidence="high",
            best_effort_guess=False,
        )
        return binding

    section_hint = normalize_name(field.section or "")
    if section_hint:
        section_matches: list[int] = []
        for index, entry in enumerate(entries):
            candidate_texts: list[str] = []
            if repeater_group == "languages":
                candidate_texts.append(str(entry.get("language") or "").strip())
            elif repeater_group == "education":
                candidate_texts.extend(
                    [
                        _entry_text_value(entry.get("school")) or "",
                        _entry_text_value(entry.get("degree")) or "",
                        _entry_text_value(
                            entry.get("field_of_study")
                            or entry.get("field")
                            or entry.get("major")
                            or entry.get("majors")
                            or entry.get("majorName")
                            or entry.get("majorNames")
                            or entry.get("major_name")
                            or entry.get("major_names")
                        )
                        or "",
                        _entry_text_value(entry.get("degree_type") or entry.get("degreeType")) or "",
                        _entry_text_value(
                            entry.get("minor")
                            or entry.get("minors")
                            or entry.get("minorName")
                            or entry.get("minor_name")
                            or entry.get("minor_names")
                        )
                        or "",
                        _entry_text_value(
                            entry.get("honors")
                            or entry.get("honours")
                            or entry.get("honor")
                            or entry.get("honorsList")
                            or entry.get("honoursList")
                            or entry.get("honors_list")
                            or entry.get("honours_list")
                        )
                        or "",
                    ]
                )
            if any(text and normalize_name(text) and normalize_name(text) in section_hint for text in candidate_texts):
                section_matches.append(index)
        if len(section_matches) == 1:
            binding = ResolvedFieldBinding(
                entry_index=section_matches[0],
                binding_mode="similarity",
                binding_confidence="medium",
                best_effort_guess=False,
            )
            record_repeater_field_binding(
                host=host,
                repeater_group=repeater_group,
                field_binding_key=field_binding_key,
                entry_index=binding.entry_index,
                binding_mode="similarity",
                binding_confidence="medium",
                best_effort_guess=False,
            )
            return binding

    if slot_name:
        similarity_index = _match_entry_by_slot_value(
            field=field,
            slot_name=slot_name,
            entries=entries,
            current_value=current_value,
        )
        if similarity_index is not None:
            binding = ResolvedFieldBinding(
                entry_index=similarity_index,
                binding_mode="similarity",
                binding_confidence="medium",
                best_effort_guess=False,
            )
            record_repeater_field_binding(
                host=host,
                repeater_group=repeater_group,
                field_binding_key=field_binding_key,
                entry_index=binding.entry_index,
                binding_mode="similarity",
                binding_confidence="medium",
                best_effort_guess=False,
            )
            return binding

        row_order_index = _row_order_binding_index(
            field=field,
            visible_fields=visible_fields,
            slot_name=slot_name,
            slot_resolver=slot_resolver,
        )
        if row_order_index is not None and 0 <= row_order_index < len(entries):
            binding = ResolvedFieldBinding(
                entry_index=row_order_index,
                binding_mode="row_order",
                binding_confidence="low",
                best_effort_guess=True,
            )
            record_repeater_field_binding(
                host=host,
                repeater_group=repeater_group,
                field_binding_key=field_binding_key,
                entry_index=binding.entry_index,
                binding_mode="row_order",
                binding_confidence="low",
                best_effort_guess=True,
            )
            return binding

    return None



def _structured_language_raw_value_from_entry(field: FormField, entry: dict[str, Any]) -> str | None:
    value, _ = _structured_language_raw_value_and_source_from_entry(field, entry)
    return value



def _structured_language_raw_value_and_source_from_entry(
    field: FormField,
    entry: dict[str, Any],
) -> tuple[str | None, str | None]:
    return _structured_language_entry_value_and_source(_language_slot_name(field), entry)



def _structured_language_value_from_entry(field: FormField, entry: dict[str, Any]) -> str | None:
    return _coerce_answer_to_field(field, _structured_language_raw_value_and_source_from_entry(field, entry)[0])



def _resolve_structured_language_value(
    field: FormField,
    profile_data: dict[str, Any] | None,
) -> str | None:
    if not profile_data:
        return None
    raw_languages = profile_data.get("languages")
    if not isinstance(raw_languages, list) or not raw_languages:
        return None
    index = _language_entry_index_for_field(field)
    if index is None or not (0 <= index < len(raw_languages)):
        return None
    entry = raw_languages[index]
    if not isinstance(entry, dict):
        return None
    return _structured_language_value_from_entry(field, entry)



def _is_structured_language_field(field: FormField) -> bool:
    section_norm = _field_section_name(field)
    if "language" not in section_norm:
        return False
    labels = [normalize_name(label) for label in _field_label_candidates(field)]
    return any(
        any(
            token in label
            for token in (
                "language",
                "i am fluent in this language",
                "comprehension",
                "reading",
                "writing",
                "speaking",
                "listening",
                "overall",
                "language proficiency",
            )
        )
        for label in labels
    )



def _structured_field_missing_reason(field: FormField) -> str:
    label = _preferred_field_label(field)
    return f"Structured profile data is required for {label} and no exact value was available."



def _infer_entry_data_from_scope(
    profile_data: dict[str, Any],
    heading_boundary: str | None,
    target_section: str | None,
) -> dict[str, Any] | None:
    """Infer repeater entry data from the full profile when entry_data is omitted."""
    if not profile_data:
        return None

    scope_norm = normalize_name(heading_boundary or target_section or "")
    if not scope_norm:
        return None

    if any(token in scope_norm for token in ("education", "college", "university", "school", "degree")):
        entries = profile_data.get("education")
    elif any(token in scope_norm for token in ("work experience", "experience", "employment")):
        entries = profile_data.get("experience")
    else:
        return None

    if not isinstance(entries, list) or len(entries) == 0:
        return None
    indexed_heading = _parse_heading_index(heading_boundary or target_section)
    if indexed_heading is not None:
        entry_index = indexed_heading - 1
    elif len(entries) == 1:
        entry_index = 0
    else:
        return None
    if not (0 <= entry_index < len(entries)):
        return None
    entry = entries[entry_index]
    return entry if isinstance(entry, dict) and entry else None
