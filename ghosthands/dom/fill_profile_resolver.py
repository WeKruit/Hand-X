"""Profile data resolution, QA matching, screening, and semantic intent.

Extracted from ``ghosthands.actions.domhand_fill``.  This module owns:
- Profile evidence parsing and answer map building
- QA matching and entry data resolution
- Employer history screening
- Semantic intent classification
- Known profile value resolution chain
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, TYPE_CHECKING

import structlog

from ghosthands.actions.views import (
    FormField,
    get_stable_field_key,
    is_placeholder_value,
    normalize_name,
    split_dropdown_value_hierarchy,
)
from ghosthands.dom.fill_resolution import (
    _education_slot_name,
    _entry_date_text_and_source,
    _entry_text_and_source,
    _extract_date_component,
    _field_binding_identity,
    _infer_entry_data_from_scope,
    _is_education_like_section,
    _is_structured_education_candidate,
    _is_structured_education_field,
    _is_structured_language_field,
    _language_entry_index_for_field,
    _language_slot_name,
    _match_entry_by_slot_value,
    _resolve_repeater_binding,
    _resolve_structured_language_value,
    _structured_education_raw_value_and_source_from_entry,
    _structured_education_raw_value_from_entry,
    _structured_education_value_from_entry,
    _structured_field_missing_reason,
    _structured_language_raw_value_and_source_from_entry,
    _structured_language_raw_value_from_entry,
    _structured_language_value_from_entry,
)
from ghosthands.profile.canonical import build_canonical_profile
from ghosthands.runtime_learning import (
    SemanticQuestionIntent,
    cache_semantic_alias,
    confirm_learned_question_alias,
    get_cached_semantic_alias,
    get_learned_question_alias,
    get_repeater_field_binding,
    has_cached_semantic_alias,
    record_repeater_field_binding,
    stage_learned_question_alias,
)

if TYPE_CHECKING:
    from ghosthands.actions.domhand_fill import ResolvedFieldValue

logger = structlog.get_logger(__name__)


# ── Late-import delegates ────────────────────────────────────────────────

def _preferred_field_label(field: FormField) -> str:
    from ghosthands.dom.fill_label_match import _preferred_field_label as _impl
    return _impl(field)


def _field_label_candidates(field: FormField) -> list[str]:
    from ghosthands.dom.fill_label_match import _field_label_candidates as _impl
    return _impl(field)


def _coerce_answer_to_field(field: FormField, answer: str | None) -> str | None:
    from ghosthands.dom.fill_label_match import _coerce_answer_to_field as _impl
    return _impl(field, answer)


def _normalize_match_label(text: str) -> str:
    from ghosthands.dom.fill_label_match import _normalize_match_label as _impl
    return _impl(text)


def _label_match_confidence(label: str, candidate: str) -> str | None:
    from ghosthands.dom.fill_label_match import _label_match_confidence as _impl
    return _impl(label, candidate)


def _meets_match_confidence(confidence: str | None, minimum_confidence: str) -> bool:
    from ghosthands.dom.fill_label_match import _meets_match_confidence as _impl
    return _impl(confidence, minimum_confidence)


def _normalize_binary_match_value(value: str | None) -> str | None:
    from ghosthands.dom.fill_label_match import _normalize_binary_match_value as _impl
    return _impl(value)


def _normalize_bool_text(value: Any) -> str | None:
    from ghosthands.dom.fill_label_match import _normalize_bool_text as _impl
    return _impl(value)


def _normalize_yes_no_answer(answer: str | None) -> str | None:
    from ghosthands.dom.fill_label_match import _normalize_yes_no_answer as _impl
    return _impl(answer)


def _MATCH_CONFIDENCE_RANKS_getter():
    from ghosthands.dom.fill_label_match import _MATCH_CONFIDENCE_RANKS
    return _MATCH_CONFIDENCE_RANKS


def _is_effectively_unset_field_value(value: str | None) -> bool:
    from ghosthands.actions.domhand_fill import _is_effectively_unset_field_value as _impl
    return _impl(value)


def _looks_like_internal_widget_value(value: str | None) -> bool:
    from ghosthands.actions.domhand_fill import _looks_like_internal_widget_value as _impl
    return _impl(value)


def _trace_profile_resolution(event: str, *, field_label: str, **extra: Any) -> None:
    from ghosthands.actions.domhand_fill import _trace_profile_resolution as _impl
    return _impl(event, field_label=field_label, **extra)


def _profile_debug_enabled() -> bool:
    from ghosthands.actions.domhand_fill import _profile_debug_enabled as _impl
    return _impl()


def _profile_debug_preview(value: Any) -> str:
    from ghosthands.actions.domhand_fill import _profile_debug_preview as _impl
    return _impl(value)


def _should_trace_profile_label(label: str) -> bool:
    from ghosthands.actions.domhand_fill import _should_trace_profile_label as _impl
    return _impl(label)


def _strip_required_marker(label: str | None) -> str:
    from ghosthands.actions.domhand_fill import _strip_required_marker as _impl
    return _impl(label)


def _is_non_guess_name_fragment(field_name: str | None) -> bool:
    from ghosthands.actions.domhand_fill import _is_non_guess_name_fragment as _impl
    return _impl(field_name)


def _profile_skill_values(profile_data: dict[str, Any] | None) -> list[str]:
    from ghosthands.actions.domhand_fill import _profile_skill_values as _impl
    return _impl(profile_data)


def _get_profile_data() -> dict[str, Any]:
    from ghosthands.actions.domhand_fill import _get_profile_data as _impl
    return _impl()


def _get_profile_text() -> str | None:
    from ghosthands.actions.domhand_fill import _get_profile_text as _impl
    return _impl()


def _get_ResolvedFieldValue():
    from ghosthands.actions.domhand_fill import ResolvedFieldValue
    return ResolvedFieldValue


def _is_skill_like(field_name: str) -> bool:
    from ghosthands.actions.domhand_fill import _is_skill_like as _impl
    return _impl(field_name)


def _set_structured_repeater_binding(*args, **kwargs):
    from ghosthands.actions.domhand_fill import _set_structured_repeater_binding as _impl
    return _impl(*args, **kwargs)


def _set_structured_repeater_resolved_value(*args, **kwargs):
    from ghosthands.actions.domhand_fill import _set_structured_repeater_resolved_value as _impl
    return _impl(*args, **kwargs)


def _structured_repeater_fill_result(*args, **kwargs):
    from ghosthands.actions.domhand_fill import _structured_repeater_fill_result as _impl
    return _impl(*args, **kwargs)


def _structured_repeater_failure_reason(stage: str) -> str:
    from ghosthands.actions.domhand_fill import _structured_repeater_failure_reason as _impl
    return _impl(stage)


def _structured_repeater_takeover_suggestion(repeater_group: str) -> str:
    from ghosthands.actions.domhand_fill import _structured_repeater_takeover_suggestion as _impl
    return _impl(repeater_group)


def _trace_structured_repeater_resolution(**kwargs):
    from ghosthands.actions.domhand_fill import _trace_structured_repeater_resolution as _impl
    return _impl(**kwargs)


def _structured_repeater_debug_enabled() -> bool:
    from ghosthands.actions.domhand_fill import _structured_repeater_debug_enabled as _impl
    return _impl()


def _get_ResolvedFieldBinding():
    from ghosthands.actions.domhand_fill import ResolvedFieldBinding
    return ResolvedFieldBinding


def _get_StructuredRepeaterDiagnostic():
    from ghosthands.actions.domhand_fill import StructuredRepeaterDiagnostic
    return StructuredRepeaterDiagnostic


def _MAX_QA_ENTRIES():
    from ghosthands.actions.domhand_fill import MAX_QA_ENTRIES
    return MAX_QA_ENTRIES


def _QA_CONFIDENCE_RANKS_getter():
    from ghosthands.actions.domhand_fill import _QA_CONFIDENCE_RANKS
    return _QA_CONFIDENCE_RANKS


def _QA_QUESTION_SYNONYMS_getter():
    from ghosthands.actions.domhand_fill import _QA_QUESTION_SYNONYMS
    return _QA_QUESTION_SYNONYMS


def _EEO_DECLINE_DEFAULTS_getter():
    from ghosthands.actions.domhand_fill import _EEO_DECLINE_DEFAULTS
    return _EEO_DECLINE_DEFAULTS


def _is_self_identify_date_field(field: FormField) -> bool:
    from ghosthands.dom.fill_executor import _is_self_identify_date_field as _impl
    return _impl(field)


def _parse_profile_evidence(profile_text: str) -> dict[str, str | None]:
    """Extract structured fields from profile text for direct field matching."""
    stripped = profile_text.strip()

    def _score_date(value: Any) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        parts = text.split("-")
        try:
            year = int(parts[0])
        except (TypeError, ValueError):
            return 0
        month = 1
        day = 1
        if len(parts) > 1:
            try:
                month = int(parts[1])
            except (TypeError, ValueError):
                month = 1
        if len(parts) > 2:
            try:
                day = int(parts[2])
            except (TypeError, ValueError):
                day = 1
        return (year * 10_000) + (month * 100) + day

    def _pick_latest_education_entry(raw_entries: Any) -> dict[str, Any] | None:
        if not isinstance(raw_entries, list):
            return None
        entries = [entry for entry in raw_entries if isinstance(entry, dict)]
        if not entries:
            return None
        ranked = sorted(
            entries,
            key=lambda entry: _score_date(
                entry.get("endDate")
                or entry.get("end_date")
                or entry.get("graduationDate")
                or entry.get("graduation_date")
                or entry.get("startDate")
                or entry.get("start_date")
            ),
            reverse=True,
        )
        return ranked[0] if ranked else None

    def _format_graduation_date(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        parts = text.split("-")
        if len(parts) < 2:
            return text
        try:
            month_index = int(parts[1])
        except (TypeError, ValueError):
            return text
        if month_index < 1 or month_index > 12:
            return text
        month_labels = [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
        return f"{month_labels[month_index - 1]} {parts[0]}"

    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):

            def _read_text(*keys: str) -> str | None:
                for key in keys:
                    text = _entry_text_value(data.get(key))
                    if text:
                        return text
                return None

            name = str(data.get("name") or "").strip() or None
            first_name = _read_text("first_name", "firstName")
            last_name = _read_text("last_name", "lastName")
            if name and not first_name:
                first_name = name.split()[0] if name.split() else None
            if name and not last_name and len(name.split()) > 1:
                last_name = " ".join(name.split()[1:])

            location = data.get("location")
            raw_address = data.get("address")
            address_data = raw_address if isinstance(raw_address, dict) else {}

            def _read_address_text(*keys: str) -> str | None:
                for key in keys:
                    if key in address_data:
                        text = _entry_text_value(address_data.get(key))
                        if text:
                            return text
                return None

            address_line_1 = (
                _read_address_text(
                    "street",
                    "line1",
                    "address1",
                    "address_1",
                    "addressLine1",
                    "street1",
                    "street_line_1",
                )
                or (str(raw_address).strip() if isinstance(raw_address, str) and str(raw_address).strip() else None)
            )
            address_line_2 = (
                _read_text("address_line_2", "addressLine2")
                or _read_address_text(
                    "line2",
                    "address2",
                    "address_2",
                    "addressLine2",
                    "street2",
                    "street_line_2",
                    "apartment",
                    "unit",
                    "suite",
                )
            )
            city = (
                str(data.get("city") or "").strip()
                or _read_address_text("city", "town")
                or None
            )
            state = (
                str(data.get("state") or data.get("province") or "").strip()
                or _read_address_text("state", "province", "region")
                or None
            )
            zip_code = (
                str(data.get("zip") or data.get("zip_code") or data.get("postal_code") or "").strip()
                or _read_address_text("zip", "zipCode", "zip_code", "postalCode", "postal_code")
                or None
            )
            county = str(data.get("county") or "").strip() or _read_address_text("county") or None
            country = _read_text("country") or _read_address_text("country", "countryCode", "country_code")
            if isinstance(location, str) and location.strip() and (not city or not state or not zip_code):
                parts = [p.strip() for p in location.split(",") if p.strip()]
                if len(parts) >= 2:
                    city = city or parts[0]
                    state_zip = parts[1].split()
                    state = state or (state_zip[0] if state_zip else None)
                    zip_code = zip_code or (state_zip[1] if len(state_zip) > 1 else None)

            latest_education = _pick_latest_education_entry(data.get("education"))
            latest_degree = _entry_text_value((latest_education or {}).get("degree")) or None
            latest_degree_type = _read_text("degree_type", "degreeType")
            latest_field_of_study = (
                _entry_text_value(
                    (latest_education or {}).get("field")
                    or (latest_education or {}).get("fieldOfStudy")
                    or (latest_education or {}).get("field_of_study")
                    or (latest_education or {}).get("major")
                    or (latest_education or {}).get("majors")
                    or (latest_education or {}).get("majorName")
                    or (latest_education or {}).get("majorNames")
                    or (latest_education or {}).get("major_name")
                    or (latest_education or {}).get("major_names")
                    or (latest_education or {}).get("area_of_study")
                    or (latest_education or {}).get("areaOfStudy")
                    or (latest_education or {}).get("discipline")
                    or ""
                )
                or None
            )
            latest_minor = _entry_text_value(
                (latest_education or {}).get("minor")
                or (latest_education or {}).get("minors")
                or (latest_education or {}).get("minorName")
                or (latest_education or {}).get("minorNames")
                or (latest_education or {}).get("minor_name")
                or (latest_education or {}).get("minor_names")
                or ""
            )
            latest_honors = _entry_text_value(
                (latest_education or {}).get("honors")
                or (latest_education or {}).get("honours")
                or (latest_education or {}).get("honor")
                or (latest_education or {}).get("honorsList")
                or (latest_education or {}).get("honoursList")
                or (latest_education or {}).get("honors_list")
                or (latest_education or {}).get("honours_list")
                or ""
            )
            latest_graduation_date = _format_graduation_date(
                (latest_education or {}).get("graduationDate")
                or (latest_education or {}).get("graduation_date")
                or (latest_education or {}).get("endDate")
                or (latest_education or {}).get("end_date")
            )

            github = str(data.get("github") or data.get("github_url") or "").strip() or None
            if not github:
                github_match = re.search(r"https?://(?:www\.)?github\.com/[^\s)]+", profile_text, re.IGNORECASE)
                github = github_match.group(0) if github_match else None

            twitter = (
                str(data.get("twitter") or data.get("twitter_url") or data.get("x") or data.get("x_url") or "").strip()
                or None
            )
            if not twitter:
                twitter_match = re.search(
                    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s)]+", profile_text, re.IGNORECASE
                )
                twitter = twitter_match.group(0) if twitter_match else None

            return {
                "first_name": first_name,
                "last_name": last_name,
                "email": str(data.get("email") or "").strip() or None,
                "phone": str(data.get("phone") or "").strip() or None,
                "address": address_line_1,
                "address_line_2": address_line_2,
                "city": city,
                "state": state,
                "zip": zip_code,
                "county": county,
                "country": country,
                "phone_device_type": _read_text("phone_device_type", "phone_type", "phoneDeviceType"),
                "phone_country_code": _read_text("phone_country_code", "phoneCountryCode"),
                "linkedin": _read_text("linkedin", "linkedin_url", "linkedIn"),
                "portfolio": str(
                    data.get("portfolio")
                    or data.get("website")
                    or data.get("personal_website")
                    or data.get("personalWebsite")
                    or ""
                ).strip()
                or None,
                "github": github,
                "twitter": twitter,
                # Workday-relevant fields
                "work_authorization": _read_text("work_authorization", "workAuthorization"),
                "available_start_date": _read_text("available_start_date", "availableStartDate"),
                "availability_window": _read_text("availability_window", "availabilityWindow"),
                "notice_period": _read_text("notice_period", "noticePeriod"),
                "salary_expectation": _read_text("salary_expectation", "salaryExpectation"),
                "current_school_year": _read_text("current_school_year", "currentSchoolYear"),
                "graduation_date": _read_text("graduation_date", "graduationDate") or latest_graduation_date,
                "degree_seeking": _read_text("degree_seeking", "degreeSeeking") or latest_degree,
                "degree_type": _read_text("degree_type", "degreeType") or latest_degree_type,
                "certifications_licenses": _read_text("certifications_licenses", "certificationsLicenses"),
                "field_of_study": latest_field_of_study,
                "minor": latest_minor,
                "honors": latest_honors,
                "spoken_languages": _read_text("spoken_languages", "spokenLanguages"),
                "english_proficiency": _read_text("english_proficiency", "englishProficiency"),
                "languages": data.get("languages") if isinstance(data.get("languages"), list) else None,
                "country_of_residence": _read_text("country_of_residence", "countryOfResidence"),
                "how_did_you_hear": _read_text("how_did_you_hear", "referral_source", "howDidYouHear"),
                "willing_to_relocate": _read_text("willing_to_relocate", "willingToRelocate"),
                "relocation_preference": _read_text(
                    "relocation_preference",
                    "relocationPreference",
                    "relocate_preference",
                    "relocatePreference",
                    "relocate_ok",
                    "relocateOk",
                    "open_to_relocation",
                    "openToRelocation",
                ),
                "preferred_work_mode": _read_text("preferred_work_mode", "preferredWorkMode"),
                "preferred_locations": _read_text("preferred_locations", "preferredLocations"),
            }

    def read_line(label: str) -> str | None:
        m = re.search(rf"^\s*{re.escape(label)}:\s*(.+)$", profile_text, re.MULTILINE | re.IGNORECASE)
        val = m.group(1).strip() if m else None
        return val if val else None

    first_name = read_line("First name") or read_line("First Name")
    last_name = read_line("Last name") or read_line("Last Name")
    name = read_line("Full name") or read_line("Name")
    if name and not first_name:
        first_name = name.split()[0] if name.split() else None
    if name and not last_name and len(name.split()) > 1:
        last_name = " ".join(name.split()[1:])

    location = read_line("Location")
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    if location:
        parts = [p.strip() for p in location.split(",") if p.strip()]
        if len(parts) >= 2:
            city = parts[0]
            state_zip = parts[1].split()
            state = state_zip[0] if state_zip else None
            zip_code = state_zip[1] if len(state_zip) > 1 else None

    linkedin = read_line("LinkedIn")
    portfolio = read_line("Portfolio") or read_line("Website")
    github_match = re.search(r"https?://(?:www\.)?github\.com/[^\s)]+", profile_text, re.IGNORECASE)
    twitter_match = re.search(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[^\s)]+", profile_text, re.IGNORECASE)

    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": read_line("Email"),
        "phone": read_line("Phone"),
        "address": read_line("Address"),
        "address_line_2": read_line("Address line 2") or read_line("Address Line 2"),
        "city": city,
        "state": state,
        "zip": zip_code,
        "county": read_line("County"),
        "country": read_line("Country"),
        "phone_device_type": read_line("Phone type") or read_line("Phone device type"),
        "phone_country_code": read_line("Phone country code") or read_line("Country phone code"),
        "linkedin": linkedin,
        "portfolio": portfolio,
        "github": github_match.group(0) if github_match else None,
        "twitter": twitter_match.group(0) if twitter_match else None,
        # Workday-relevant fields
        "work_authorization": read_line("Work authorization"),
        "available_start_date": read_line("Available start date"),
        "availability_window": read_line("Availability to start") or read_line("Earliest start date"),
        "notice_period": read_line("Notice period"),
        "salary_expectation": read_line("Salary expectation"),
        "current_school_year": read_line("Current year in school") or read_line("School year"),
        "graduation_date": read_line("Graduation date") or read_line("Estimated graduation date"),
        "degree_seeking": read_line("Degree seeking") or read_line("What degree are you seeking"),
        "certifications_licenses": read_line("Certifications or licenses")
        or read_line("Relevant certifications or licenses"),
        "field_of_study": read_line("Field of study") or read_line("Major") or read_line("Area of study"),
        "spoken_languages": read_line("Preferred spoken languages")
        or read_line("Spoken languages")
        or read_line("Languages spoken")
        or read_line("Language proficiency"),
        "english_proficiency": read_line("English proficiency")
        or read_line("Overall")
        or read_line("Reading")
        or read_line("Writing")
        or read_line("Speaking")
        or read_line("Comprehension"),
        "country_of_residence": read_line("Country of residence") or read_line("Country"),
        "how_did_you_hear": read_line("How did you hear about us"),
        "willing_to_relocate": read_line("Willing to relocate"),
        "relocation_preference": read_line("Relocation preference") or read_line("Relocate OK"),
        "preferred_work_mode": read_line("Preferred work setup"),
        "preferred_locations": read_line("Preferred locations"),
    }





def _format_entry_profile_text(entry_data: dict[str, Any]) -> str:
    """Format a repeater entry into profile text for scoped LLM answer generation."""
    if not entry_data:
        return ""

    lines: list[str] = []
    used_keys: set[str] = set()
    label_map = [
        ("title", "Job Title"),
        ("company", "Company"),
        ("location", "Location"),
        ("school", "School"),
        ("degree", "Degree"),
        ("degree_type", "Degree Type"),
        ("field_of_study", "Field of Study"),
        ("major", "Major"),
        ("minor", "Minor"),
        ("honors", "Honors"),
        ("gpa", "GPA"),
        ("start_date", "Start Date"),
        ("end_date", "End Date"),
        ("end_date_type", "End Date Status"),
        ("description", "Description"),
    ]

    for key, label in label_map:
        value = _entry_text_value(entry_data.get(key))
        if key == "end_date" and value in (None, "", []):
            value = _entry_text_value(entry_data.get("graduation_date"))
        if value in (None, "", []):
            continue
        used_keys.add(key)
        lines.append(f"{label}: {value}")

    currently_work_here = entry_data.get("currently_work_here")
    if currently_work_here is None:
        currently_work_here = entry_data.get("currently_working")
    if currently_work_here is not None:
        used_keys.add("currently_work_here")
        lines.append("I currently work here: " + ("Yes" if bool(currently_work_here) else "No"))

    for key, value in entry_data.items():
        if key in used_keys or value in (None, "", []):
            continue
        text_value = _entry_text_value(value)
        if text_value is None:
            continue
        lines.append(f"{key.replace('_', ' ').title()}: {text_value}")

    return "\n".join(lines) if lines else json.dumps(entry_data, indent=2, sort_keys=True)


def _entry_text_value(value: Any) -> str | None:
    """Return deterministic text for structured education/profile values."""
    if value in (None, "", []):
        return None
    if isinstance(value, dict):
        for key in ("name", "label", "value", "title"):
            text = _entry_text_value(value.get(key))
            if text:
                return text
        return None
    if isinstance(value, set):
        parts = sorted(part for part in (_entry_text_value(item) for item in value) if part)
        return ", ".join(parts) if parts else None
    if isinstance(value, (list, tuple)):
        parts = [part for part in (_entry_text_value(item) for item in value) if part]
        return ", ".join(parts) if parts else None
    text = str(value).strip()
    return text or None


def _known_entry_value(field_name: str, entry_data: dict[str, Any] | None) -> str | None:
    """Return a scoped repeater-entry value when filling a single experience/education block."""
    if not entry_data:
        return None

    name = normalize_name(field_name)
    if not name:
        return None

    def _entry_string(key: str) -> str | None:
        value = entry_data.get(key)
        return _entry_text_value(value)

    def _entry_string_from_aliases(*keys: str) -> str | None:
        for key in keys:
            value = _entry_string(key)
            if value:
                return value
        return None

    def _entry_month_year_value(
        *,
        direct_keys: tuple[str, ...],
        pair_keys: tuple[tuple[str, str], ...],
    ) -> str | None:
        for key in direct_keys:
            value = _entry_string(key)
            if value:
                return value
        for year_key, month_key in pair_keys:
            year = _entry_string(year_key)
            month = _entry_string(month_key)
            if not year:
                continue
            if not month:
                return year
            month_digits = re.sub(r"\D+", "", month)
            if month_digits:
                return f"{year}-{int(month_digits):02d}"
            return year
        return None

    if any(kw in name for kw in ("job title", "title", "position", "role title")):
        return _entry_string("title")
    if any(kw in name for kw in ("company", "employer", "organization")):
        return _entry_string("company")
    if any(kw in name for kw in ("school", "university", "college", "institution")):
        return _entry_string_from_aliases("school", "institution")
    if any(kw in name for kw in ("degree type", "type of degree", "degree level")):
        return _entry_string_from_aliases("degree_type", "degreeType")
    if "degree" in name:
        return _entry_string_from_aliases("degree", "degree_type", "degreeType")
    if any(kw in name for kw in ("field of study", "major", "discipline", "area of study", "concentration")):
        return _entry_string_from_aliases(
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
    if any(kw in name for kw in ("minor", "minor field", "minor subject")):
        return _entry_string_from_aliases("minor", "minors", "minorName", "minorNames", "minor_name", "minor_names")
    if any(kw in name for kw in ("honors", "honours", "honour", "honor")):
        return _entry_string_from_aliases(
            "honors",
            "honours",
            "honor",
            "honorsList",
            "honoursList",
            "honors_list",
            "honours_list",
        )
    if "gpa" in name:
        return _entry_string("gpa")
    if any(kw in name for kw in ("location", "city")):
        return _entry_string("location")
    if any(kw in name for kw in ("currently work here", "currently employed", "currently working", "still employed")):
        current = entry_data.get("currently_work_here")
        if current is None:
            current = entry_data.get("currently_working")
        if current is None:
            return None
        return "checked" if bool(current) else "unchecked"
    if any(kw in name for kw in ("actual or expected", "actual/expected", "expected or actual", "expected/actual")):
        return _entry_string_from_aliases("end_date_type", "endDateType", "endDateKind")
    if name in {"from", "from date"} or any(
        kw in name for kw in ("start date", "from date", "date from", "begin date", "employment start")
    ):
        return _entry_month_year_value(
            direct_keys=("start_date", "startDate", "fromDate", "from_date"),
            pair_keys=(
                ("start_year", "start_month"),
                ("startYear", "startMonth"),
                ("from_year", "from_month"),
                ("fromYear", "fromMonth"),
            ),
        )
    if name in {"to", "to date"} or any(
        kw in name for kw in ("end date", "to date", "date to", "graduation date", "completion date")
    ):
        return _entry_month_year_value(
            direct_keys=("end_date", "endDate", "graduation_date", "graduationDate", "toDate", "to_date"),
            pair_keys=(
                ("end_year", "end_month"),
                ("endYear", "endMonth"),
                ("graduation_year", "graduation_month"),
                ("graduationYear", "graduationMonth"),
                ("to_year", "to_month"),
                ("toYear", "toMonth"),
            ),
        ) or _entry_string_from_aliases("expectedGraduation", "expected_graduation")
    if any(
        kw in name
        for kw in (
            "description",
            "summary",
            "responsibilities",
            "responsibility",
            "duties",
            "details",
            "accomplishments",
            "achievements",
        )
    ):
        return _entry_string("description")
    return None




def _known_entry_value_for_field(field: FormField, entry_data: dict[str, Any] | None) -> str | None:
    """Try scoped repeater entry matching against all known labels for a field."""
    for label in _field_label_candidates(field):
        value = _known_entry_value(label, entry_data)
        if value:
            return _coerce_answer_to_field(field, value)
    return _coerce_answer_to_field(field, _known_entry_value(field.name, entry_data))


def _field_section_name(field: FormField) -> str:
    return normalize_name(field.section or "")




def _get_nested_profile_value(profile_data: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    """Return the first nested profile value found across the candidate paths."""
    for path in paths:
        current: Any = profile_data
        found = True
        for key in path:
            if not isinstance(current, dict) or key not in current:
                found = False
                break
            current = current[key]
        if found:
            return current
    return None


def _normalize_qa_text(text: str) -> str:
    """Normalize question text for fuzzy Q&A matching."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)  # Remove punctuation
    text = re.sub(r"\s+", " ", text)  # Collapse whitespace
    return text


def _cap_qa_entries(
    qa_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cap Q&A bank at _MAX_QA_ENTRIES() entries, prioritized by usage mode,
    times_used descending, then confidence (exact > inferred > learned)."""
    if len(qa_entries) <= _MAX_QA_ENTRIES():
        return qa_entries
    qa_entries.sort(
        key=lambda e: (
            e.get("usageMode", e.get("usage_mode")) != "always_use",  # always_use first
            -int(e.get("timesUsed", e.get("times_used", 0)) or 0),  # most used first
            _QA_CONFIDENCE_RANKS_getter().get(e.get("confidence", "learned"), 3),  # exact > inferred > learned
        )
    )
    return qa_entries[: _MAX_QA_ENTRIES()]


def _match_qa_answer(field_label: str, qa_entries: list[dict[str, Any]]) -> str | None:
    """Try to match a form field label against Q&A bank entries with fuzzy matching.

    Checks exact normalized match first, then synonym-based matching.
    Returns the answer string or None if no match found.
    """
    if not qa_entries or not field_label:
        return None

    normalized_label = _normalize_qa_text(field_label)
    if not normalized_label:
        return None

    for entry in qa_entries:
        stored_q = _normalize_qa_text(entry.get("question", ""))
        if not stored_q:
            continue

        # Exact normalized match
        if normalized_label == stored_q:
            answer = entry.get("answer", "")
            if answer:
                return answer

        # Substring containment (short label inside long stored question or vice versa)
        shorter = min(normalized_label, stored_q, key=len)
        if len(shorter) >= 8 and (shorter in normalized_label and shorter in stored_q):
            answer = entry.get("answer", "")
            if answer:
                return answer

    # Synonym-based matching: both the field label and stored question
    # must match the same canonical synonym group.
    for canonical, synonyms in _QA_QUESTION_SYNONYMS_getter().items():
        all_variants = [canonical, *synonyms]
        label_matches = any(v in normalized_label for v in all_variants)
        if not label_matches:
            continue
        for entry in qa_entries:
            stored_q = _normalize_qa_text(entry.get("question", ""))
            if not stored_q:
                continue
            stored_matches = any(v in stored_q for v in all_variants)
            if stored_matches:
                answer = entry.get("answer", "")
                if answer:
                    return answer

    return None


def _build_profile_answer_map(
    profile_data: dict[str, Any],
    evidence: dict[str, str | None],
) -> dict[str, str]:
    """Build a generic question/answer map from structured profile data."""
    canonical = build_canonical_profile(profile_data, evidence)

    answer_map: dict[str, str] = {}

    def add(value: Any, *labels: str) -> None:
        text = _normalize_bool_text(value)
        if text is None:
            return
        for label in labels:
            answer_map[label] = text

    first_initial = (canonical.get("first_name") or "")[:1].upper()
    last_initial = (canonical.get("last_name") or "")[:1].upper()
    initials = f"{first_initial}{last_initial}".strip()
    if initials:
        add(
            initials,
            "Include your initials",
            "Your initials",
            "Enter your initials",
            "Type your initials",
            "Initials",
        )

    add(
        canonical.get("gender"),
        "Gender",
        "Please select your gender.",
    )
    add(
        canonical.get("race_ethnicity"),
        "Race/Ethnicity",
        "Race",
        "Ethnicity",
        "Please select the ethnicity (or ethnicities) which most accurately describe(s) how you identify yourself.",
    )
    add(
        canonical.get("veteran_status"),
        "Veteran Status",
        "Are you a protected veteran",
        "Please select the veteran status which most accurately describes your status.",
    )
    add(
        canonical.get("disability_status"),
        "Disability",
        "Disability Status",
        "Please indicate if you have a disability",
        "Please check one of the boxes below:",
    )
    add(canonical.get("country"), "Country", "Country/Territory", "Country/Region")
    add(canonical.get("phone_device_type"), "Phone Device Type", "Phone Type")
    add(canonical.get("phone_country_code"), "Country Phone Code", "Phone Country Code")
    add(
        canonical.get("full_name"),
        "Please enter your name",
        "Enter your name",
        "Your name",
        "Full name",
        "Name",
        "Signature",
    )
    add(canonical.get("preferred_name"), "Preferred name", "Nickname")
    add(
        canonical.get("address"),
        "Address",
        "Address Line 1",
        "Address 1",
        "Street",
        "Street Address",
        "Street Line 1",
        "Mailing Address",
    )
    add(
        canonical.get("address_line_2"),
        "Address Line 2",
        "Address 2",
        "Apartment / Unit",
        "Apartment",
        "Suite / Apartment",
        "Suite",
        "Unit",
        "Street Line 2",
        "Mailing Address Line 2",
    )
    add(canonical.get("city"), "City", "Town")
    add(canonical.get("state"), "State", "State/Province", "State / Province", "Province", "Region")
    add(canonical.get("postal_code"), "Postal Code", "Postal/Zip Code", "ZIP", "ZIP Code", "Zip/Postal Code")
    add(canonical.get("county"), "County", "County / Parish / Borough", "Parish", "Borough")
    # Compose location from city + state for combined location fields
    _city = canonical.get("city") or ""
    _state = canonical.get("state") or ""
    _location_val = canonical.get("location") or (f"{_city}, {_state}" if _city and _state else _city or _state)
    if _location_val:
        add(_location_val, "Location", "City/State", "City, State")
    add(
        canonical.get("how_did_you_hear"),
        "How Did You Hear About Us?",
        "How did you hear about this position?",
        "How did you learn about us?",
        "Referral Source",
        "Source",
        "Source of Referral",
    )
    add(canonical.get("linkedin"), "LinkedIn", "LinkedIn URL", "LinkedIn Profile")
    add(
        canonical.get("spoken_languages"),
        "Languages spoken",
        "Spoken languages",
        "Preferred spoken languages",
        "Preferred language",
    )
    add(
        canonical.get("english_proficiency"),
        "English proficiency",
        "English language proficiency",
    )
    add(
        canonical.get("country_of_residence"),
        "Country of residence",
        "Country/Region",
        "Country region",
    )
    add(
        canonical.get("preferred_work_mode"),
        "Preferred work setup",
        "Preferred work arrangement",
        "Work arrangement",
        "Remote/Hybrid preference",
    )
    add(
        canonical.get("preferred_locations"),
        "Preferred locations",
        "Preferred work locations",
        "Preferred city",
        "Preferred office location",
    )
    add(
        canonical.get("availability_window"),
        "Availability to start",
        "Earliest start date",
        "Available start date",
        "Start date",
    )
    add(canonical.get("notice_period"), "Notice period")
    add(
        canonical.get("salary_expectation"),
        "Expected annual salary",
        "Expected salary range",
        "Expected compensation",
        "Compensation expectation",
        "Expectations on Compensation",
        "Total compensation",
        "Salary expectation",
        "Salary",
        "Compensation",
        "Desired salary",
        "Desired compensation",
        "Compensation range",
        "Salary range",
        "Pay expectation",
        "Salary requirement",
        "Hourly compensation requirements",
        "Hourly compensation",
        "Hourly rate expectation",
    )
    add(
        canonical.get("current_school_year"),
        "Current year in school",
        "What is your current year in school?",
        "School year",
        "Academic standing",
        "Class standing",
    )
    add(
        canonical.get("graduation_date"),
        "Estimated graduation date",
        "Expected graduation date",
        "Graduation date",
        "Already graduated date",
        "Degree completion date",
    )
    add(
        canonical.get("degree_seeking"),
        "What degree are you seeking?",
        "Degree seeking",
        "Degree sought",
        "Degree pursuing",
    )
    add(
        canonical.get("degree_type"),
        "Degree Type",
        "Type of Degree",
        "Degree level",
    )
    add(
        canonical.get("field_of_study"),
        "Field of study",
        "Area of study",
        "Major",
        "Majors",
        "Major(s)",
        "Please list your area of study (major).",
    )
    add(
        canonical.get("minor"),
        "Minor",
        "Minors",
        "Minor(s)",
    )
    add(
        canonical.get("honors"),
        "Honors",
        "Honours",
        "Honors / Awards",
    )
    add(
        canonical.get("certifications_licenses", allow_policy=True) or "None",
        "Certifications or licenses",
        "Relevant certifications or licenses",
        "Relevant certifications",
        "Licenses",
    )
    add(
        canonical.get("portfolio"),
        "Website",
        "Website URL",
        "Portfolio",
        "Portfolio URL",
        "Personal Website",
        "Personal Site",
        "Blog",
    )
    add(canonical.get("github"), "GitHub", "GitHub URL", "GitHub Profile")
    add(canonical.get("work_authorization"), "Work Authorization")
    add(
        canonical.get("willing_to_relocate", allow_policy=True),
        "Willing to relocate",
        "Relocation",
        "Open to relocation",
    )
    add(
        canonical.get("relocation_preference", allow_policy=True),
        "Relocation preference",
        "Relocate OK",
    )
    add(
        canonical.get("sponsorship_needed"),
        "Visa Sponsorship",
        "Sponsorship needed",
        "Require sponsorship",
        "Need sponsorship",
        "Will you now or in the future require visa sponsorship by an employer?",
    )
    add(
        canonical.get("authorized_to_work"),
        "Authorized to work",
        "Legally authorized to work",
        "Are you legally permitted to work in the country where this job is located?",
        "Are you legally authorized to work in the country in which this job is located?",
    )

    age_value = profile_data.get("age")
    if age_value not in (None, ""):
        try:
            if int(str(age_value).strip()) >= 18:
                add("Yes", "Are you at least 18 years old?", "Are you 18 years of age or older?")
        except ValueError:
            pass

    skills = _profile_skill_values(profile_data)
    if skills:
        add(
            ", ".join(skills),
            "Skills",
            "Skill",
            "Technical skills",
            "Technologies",
            "Type to Add Skills",
        )

    # ── Inject Q&A bank entries into the answer map ──────────────────
    # The answer bank arrives from Desktop/VALET as profile_data["answerBank"]
    # or profile_data["answer_bank"]. Each entry has {question, answer,
    # intentTag?, usageMode?}. Cap at _MAX_QA_ENTRIES() and add each entry's
    # question as a key in the answer map (profile values take precedence).
    raw_bank = profile_data.get("answerBank") or profile_data.get("answer_bank")
    if isinstance(raw_bank, list) and raw_bank:
        capped = _cap_qa_entries(list(raw_bank))
        for entry in capped:
            if not isinstance(entry, dict):
                continue
            question = str(entry.get("question", "")).strip()
            answer = str(entry.get("answer", "")).strip()
            if question and answer and question not in answer_map:
                answer_map[question] = answer

    return answer_map


def _find_best_profile_answer(
    label: str,
    answer_map: dict[str, str],
    minimum_confidence: str = "medium",
) -> str | None:
    """Find the closest structured-profile answer for a field label."""
    if not label or not answer_map:
        return None

    best_answer: str | None = None
    best_rank = 0
    for question, answer in answer_map.items():
        confidence = _label_match_confidence(label, question)
        if not _meets_match_confidence(confidence, minimum_confidence):
            continue
        rank = _MATCH_CONFIDENCE_RANKS_getter().get(confidence or "", 0)
        if rank > best_rank:
            best_rank = rank
            best_answer = answer

    if best_answer is None:
        return None
    return best_answer


def _is_employer_history_screening_question(norm: str) -> bool:
    """Return True for low-risk yes/no questions about prior employment with an employer."""
    direct_phrases = (
        "previously worked",
        "previously employed",
        "worked for this organization",
        "worked for this company",
        "worked here before",
        "prior employment",
        "prior employer",
        "former employee",
        "current or previous employee",
        "current or former employee",
        "previous employee",
        "government employee",
        "government employment",
        "worked for the government",
        "worked in government",
    )
    if any(phrase in norm for phrase in direct_phrases):
        return True

    if any(
        token in norm
        for token in (
            "years",
            "year",
            "months",
            "month",
            "experience",
            "experienced",
            "skill",
            "skills",
            "technology",
            "technologies",
        )
    ):
        return False

    employer_question_prefixes = (
        "have you worked at ",
        "have you ever worked at ",
        "have you previously worked at ",
        "have you worked for ",
        "have you ever worked for ",
        "have you previously worked for ",
        "have you been employed by ",
        "have you ever been employed by ",
        "have you previously been employed by ",
        "were you previously employed by ",
        "are you a current or former employee",
        "are you a current or previous employee",
        "are you a former employee",
    )
    return any(norm.startswith(prefix) for prefix in employer_question_prefixes)


def _extract_named_employer_from_question(norm: str) -> str | None:
    """Extract a concrete employer name from a screening question when present."""
    patterns = (
        r"have you (?:ever |previously )?worked (?:at|for) (?P<employer>.+)",
        r"have you (?:ever |previously )?been employed by (?P<employer>.+)",
        r"were you previously employed by (?P<employer>.+)",
        r"are you a current or former employee of (?P<employer>.+)",
        r"are you a current or previous employee of (?P<employer>.+)",
        r"are you a former employee of (?P<employer>.+)",
    )
    for pattern in patterns:
        match = re.match(pattern, norm)
        if not match:
            continue
        employer = re.sub(
            r"\b(before|previously|currently|now|today|already|or its subsidiaries|or its affiliates)\b$",
            "",
            match.group("employer").strip(),
        ).strip(" ?.:;!,")
        if employer in {
            "",
            "this company",
            "this organization",
            "this employer",
            "the company",
            "the organization",
            "the employer",
            "here",
        }:
            return None
        return employer
    return None


def _normalized_employer_tokens(text: str) -> set[str]:
    """Normalize an employer name into comparable tokens."""
    ignored = {
        "the",
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "lp",
        "llp",
        "gmbh",
        "ag",
        "sa",
        "pte",
        "pty",
    }
    return {token for token in normalize_name(text).split() if token and token not in ignored}


def _profile_has_employer_history(profile_data: dict[str, Any], employer_name: str) -> bool:
    """Return True when the named employer appears in the applicant's work history."""
    target_tokens = _normalized_employer_tokens(employer_name)
    if not target_tokens:
        return False

    employers: list[str] = []
    experience_entries = profile_data.get("experience")
    if isinstance(experience_entries, list):
        for entry in experience_entries:
            if not isinstance(entry, dict):
                continue
            for key in ("company", "employer", "organization", "company_name"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    employers.append(value.strip())

    for key in ("current_company", "company", "employer", "organization"):
        value = profile_data.get(key)
        if isinstance(value, str) and value.strip():
            employers.append(value.strip())

    for employer in employers:
        employer_tokens = _normalized_employer_tokens(employer)
        if employer_tokens and (target_tokens <= employer_tokens or employer_tokens <= target_tokens):
            return True
    return False


def _default_screening_answer(field: FormField, profile_data: dict[str, Any]) -> str | None:
    """Return a deterministic answer for low-risk screening questions."""
    label = _preferred_field_label(field)
    norm = normalize_name(label)
    options = [normalize_name(choice) for choice in (field.options or field.choices or [])]
    if options and not ({"yes", "no"} & set(options)):
        return None
    canonical = build_canonical_profile(profile_data or {}, {})

    named_employer = _extract_named_employer_from_question(norm)
    if named_employer:
        answer = "Yes" if _profile_has_employer_history(profile_data, named_employer) else "No"
        return _coerce_answer_to_field(field, answer)

    if _is_employer_history_screening_question(norm):
        return _coerce_answer_to_field(field, "No")

    sponsorship_value = profile_data.get("sponsorship_needed")
    if sponsorship_value is None:
        sponsorship_value = profile_data.get("visa_sponsorship")
    if sponsorship_value is not None and any(
        phrase in norm for phrase in ("sponsorship", "visa sponsorship", "require sponsorship", "need sponsorship")
    ):
        return _coerce_answer_to_field(field, _normalize_bool_text(sponsorship_value))

    authorized_value = profile_data.get("authorized_to_work")
    if authorized_value is None:
        authorized_value = profile_data.get("US_citizen")
    if authorized_value is not None and any(
        phrase in norm
        for phrase in ("authorized to work", "legally authorized", "legally permitted to work", "eligible to work")
    ):
        return _coerce_answer_to_field(field, _normalize_bool_text(authorized_value))

    age_value = profile_data.get("age")
    if age_value not in (None, "") and any(phrase in norm for phrase in ("at least 18", "18 years of age or older")):
        try:
            return _coerce_answer_to_field(field, "Yes" if int(str(age_value).strip()) >= 18 else "No")
        except ValueError:
            return None

    cluster, role = _field_conditional_cluster(field)
    if cluster == "relocation" and role == "boolean_parent":
        relocation_pref = canonical.get("relocation_preference", allow_policy=True)
        willing = canonical.get("willing_to_relocate", allow_policy=True)
        answer = willing or relocation_pref
        if answer:
            return _coerce_answer_to_field(field, answer)

    return None


_SEMANTIC_INTENT_DESCRIPTIONS: dict[SemanticQuestionIntent, str] = {
    "work_authorization": "Legal authorization or eligibility to work.",
    "visa_sponsorship": "Need for current or future visa / immigration sponsorship.",
    "how_did_you_hear": "Referral source or how the applicant heard about the role/company.",
    "willing_to_relocate": "Willingness or openness to relocate or move.",
    "salary_expectation": "Expected compensation or salary expectation.",
    "current_school_year": "Current year in school or class standing.",
    "graduation_date": "Estimated, expected, or completed graduation date.",
    "degree_seeking": "Degree sought or currently being pursued.",
    "certifications_licenses": "Relevant certifications or licenses held by the applicant.",
    "spoken_languages": "Languages the applicant speaks or prefers.",
    "english_proficiency": "English or language rubric proficiency such as overall/reading/writing/speaking.",
    "country_of_residence": "Current country or region of residence.",
    "preferred_work_mode": "Remote, hybrid, onsite, or work arrangement preference.",
    "preferred_locations": "Preferred city, office, or work location.",
    "availability_window": "Availability or earliest possible start window/date.",
    "notice_period": "Notice period before the applicant can start.",
    "gender": "Gender self-identification question.",
    "race_ethnicity": "Race or ethnicity self-identification question.",
    "veteran_status": "Veteran self-identification question.",
    "disability_status": "Disability self-identification question.",
    "employer_history": "Whether the applicant previously worked for the named employer or a government organization.",
}


def _resolve_semantic_intent_answer(
    field: FormField,
    intent: SemanticQuestionIntent,
    profile_data: dict[str, Any] | None,
    evidence: dict[str, str | None],
) -> str | None:
    """Resolve a classified semantic intent into an explicit saved answer."""
    canonical = build_canonical_profile(profile_data or {}, evidence)

    if intent == "work_authorization":
        return _coerce_answer_to_field(
            field,
            canonical.get("work_authorization") or canonical.get("authorized_to_work"),
        )
    if intent == "visa_sponsorship":
        return _coerce_answer_to_field(
            field,
            canonical.get("sponsorship_needed"),
        )
    if intent == "how_did_you_hear":
        return _coerce_answer_to_field(field, canonical.get("how_did_you_hear"))
    if intent == "willing_to_relocate":
        return _coerce_answer_to_field(
            field,
            canonical.get("willing_to_relocate", allow_policy=True)
            or canonical.get("relocation_preference", allow_policy=True),
        )
    if intent == "salary_expectation":
        return _coerce_answer_to_field(field, canonical.get("salary_expectation"))
    if intent == "current_school_year":
        return _coerce_answer_to_field(field, canonical.get("current_school_year"))
    if intent == "graduation_date":
        return _coerce_answer_to_field(field, canonical.get("graduation_date"))
    if intent == "degree_seeking":
        return _coerce_answer_to_field(field, canonical.get("degree_seeking"))
    if intent == "certifications_licenses":
        return _coerce_answer_to_field(
            field,
            canonical.get("certifications_licenses", allow_policy=True) or "None",
        )
    if intent == "spoken_languages":
        return _coerce_answer_to_field(field, canonical.get("spoken_languages"))
    if intent == "english_proficiency":
        return _coerce_answer_to_field(field, canonical.get("english_proficiency"))
    if intent == "country_of_residence":
        return _coerce_answer_to_field(
            field,
            canonical.get("country_of_residence") or canonical.get("country"),
        )
    if intent == "preferred_work_mode":
        return _coerce_answer_to_field(field, canonical.get("preferred_work_mode"))
    if intent == "preferred_locations":
        return _coerce_answer_to_field(field, canonical.get("preferred_locations"))
    if intent == "availability_window":
        return _coerce_answer_to_field(
            field,
            _availability_answer_from_evidence(
                _preferred_field_label(field),
                evidence,
                field_type=field.field_type,
            )
            or canonical.get("availability_window")
            or canonical.get("available_start_date"),
        )
    if intent == "notice_period":
        return _coerce_answer_to_field(field, canonical.get("notice_period"))
    if intent == "gender":
        return _coerce_answer_to_field(field, canonical.get("gender"))
    if intent == "race_ethnicity":
        return _coerce_answer_to_field(field, canonical.get("race_ethnicity"))
    if intent == "veteran_status":
        return _coerce_answer_to_field(field, canonical.get("veteran_status"))
    if intent == "disability_status":
        return _coerce_answer_to_field(field, canonical.get("disability_status"))
    if intent == "employer_history":
        return _default_screening_answer(field, profile_data or {})
    return None


def _available_semantic_intent_answers(
    field: FormField,
    profile_data: dict[str, Any] | None,
    evidence: dict[str, str | None],
) -> dict[SemanticQuestionIntent, str]:
    """Return the known semantic intents that have an explicit answer right now."""
    available: dict[SemanticQuestionIntent, str] = {}
    for intent in _SEMANTIC_INTENT_DESCRIPTIONS:
        answer = _resolve_semantic_intent_answer(field, intent, profile_data, evidence)
        if answer:
            available[intent] = answer
    return available


def _field_option_norms(field: FormField) -> set[str]:
    return {normalize_name(str(option)) for option in (field.options or field.choices or []) if str(option).strip()}


_BOOLEAN_QUESTION_PREFIXES = (
    "are you",
    "do you",
    "did you",
    "have you",
    "has the applicant",
    "will you",
    "would you",
    "can you",
    "is the applicant",
)


_BOOLEAN_QUESTION_CLAUSE_RE = re.compile(
    r"\b(?:are|do|did|have|has|will|would|can|is)\s+you\b|\b(?:has|is)\s+the\s+applicant\b"
)

_DETAIL_CHILD_PROMPT_FRAGMENTS = (
    "please specify",
    "type of visa",
    "what visa",
    "which visa",
    "location to which",
    "where would you relocate",
    "preferred location",
    "which most accurately fits",
    "answer below",
    "choose the answer below",
)


def _field_widget_kind_for_debug(field: FormField) -> str:
    if field.field_type == "select":
        return "native_select" if field.is_native else "custom_widget"
    if field.field_type in {"radio", "radio-group"}:
        return "radio_group"
    if field.field_type in {"checkbox", "checkbox-group"}:
        return "checkbox_group"
    if field.field_type == "button-group":
        return "button_group"
    if field.field_type == "textarea":
        return "textarea"
    return "text_input" if field.is_native else "custom_widget"


def _label_starts_boolean_question(label: str) -> bool:
    return any(label.startswith(prefix) for prefix in _BOOLEAN_QUESTION_PREFIXES)


def _label_contains_boolean_clause(label: str) -> bool:
    return bool(_BOOLEAN_QUESTION_CLAUSE_RE.search(label))


def _is_detail_child_prompt(label: str) -> bool:
    return any(fragment in label for fragment in _DETAIL_CHILD_PROMPT_FRAGMENTS)


def _is_employer_history_boolean_prompt(label: str) -> bool:
    return _is_employer_history_screening_question(label)


def _log_boolean_widget_classification(field: FormField, cluster: str | None, role: str | None) -> None:
    if cluster not in {"relocation", "visa_sponsorship", "work_authorization", "employer_history"}:
        return
    logger.debug(
        "domhand.boolean_widget_classification",
        extra={
            "field_id": field.field_id,
            "field_label": _preferred_field_label(field),
            "field_type": field.field_type,
            "widget_kind": _field_widget_kind_for_debug(field),
            "visible_options_count": len(field.options or field.choices or []),
            "cluster": cluster,
            "inferred_role": role or "",
        },
    )


def _field_conditional_cluster(field: FormField) -> tuple[str | None, str | None]:
    label = normalize_name(_preferred_field_label(field))
    option_norms = _field_option_norms(field)
    is_binary = bool(option_norms & {"yes", "no"}) or field.field_type in {
        "checkbox",
        "checkbox-group",
        "radio",
        "radio-group",
        "toggle",
        "button-group",
    }
    question_stem = _label_starts_boolean_question(label) or _label_contains_boolean_clause(label)

    if any(token in label for token in ("relocation", "relocate")):
        if any(token in label for token in ("location to which", "where would you relocate", "preferred location")):
            cluster, role = "relocation", "location_child"
            _log_boolean_widget_classification(field, cluster, role)
            return cluster, role
        if is_binary or question_stem or any(token in label for token in ("willing to relocate", "open to relocation")):
            cluster, role = "relocation", "boolean_parent"
            _log_boolean_widget_classification(field, cluster, role)
            return cluster, role
        cluster, role = "relocation", "detail_child"
        _log_boolean_widget_classification(field, cluster, role)
        return cluster, role

    if "visa" in label or "sponsorship" in label:
        if _is_detail_child_prompt(label):
            cluster, role = "visa_sponsorship", "detail_child"
            _log_boolean_widget_classification(field, cluster, role)
            return cluster, role
        if (
            is_binary
            or question_stem
            or any(token in label for token in ("require sponsorship", "need sponsorship", "visa sponsorship"))
        ):
            cluster, role = "visa_sponsorship", "boolean_parent"
            _log_boolean_widget_classification(field, cluster, role)
            return cluster, role
        cluster, role = "visa_sponsorship", "detail_child"
        _log_boolean_widget_classification(field, cluster, role)
        return cluster, role

    if any(token in label for token in ("authorized to work", "legally permitted to work")):
        if _is_detail_child_prompt(label):
            cluster, role = "work_authorization", "detail_child"
            _log_boolean_widget_classification(field, cluster, role)
            return cluster, role
        if is_binary or question_stem:
            cluster, role = "work_authorization", "boolean_parent"
            _log_boolean_widget_classification(field, cluster, role)
            return cluster, role
        cluster, role = "work_authorization", "detail_child"
        _log_boolean_widget_classification(field, cluster, role)
        return cluster, role

    if _is_employer_history_boolean_prompt(label):
        cluster, role = "employer_history", "boolean_parent"
        _log_boolean_widget_classification(field, cluster, role)
        return cluster, role

    if "salary" in label or "compensation" in label:
        return "salary", "detail_child"
    if (
        "start date" in label
        or "availability" in label
        or ("dates" in label and "available" in label)
        or ("when" in label and "available" in label)
    ):
        return "availability_window", "detail_child"
    if "notice" in label:
        return "notice_period", "detail_child"
    if any(token in label for token in ("from date", "start date", "date from")) and "education" in normalize_name(
        field.section or ""
    ):
        return "education_dates", "start_child"
    if any(
        token in label for token in ("to date", "end date", "graduation date", "completion date")
    ) and "education" in normalize_name(field.section or ""):
        return "education_dates", "end_child"
    return None, None


def _looks_like_iso_profile_date(value: str | None) -> bool:
    text = str(value or "").strip()
    return bool(re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", text))


def _format_profile_date_for_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split("-")
    if len(parts) < 2:
        return text
    try:
        year = int(parts[0])
        month = int(parts[1])
    except (TypeError, ValueError):
        return text
    if month < 1 or month > 12:
        return text
    month_labels = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    if len(parts) >= 3:
        try:
            day = int(parts[2])
        except (TypeError, ValueError):
            day = None
        if day is not None and 1 <= day <= 31:
            return f"{month_labels[month - 1]} {day}, {year}"
    return f"{month_labels[month - 1]} {year}"


def _availability_prompt_prefers_window_text(field_label: str, field_type: str | None = None) -> bool:
    label = normalize_name(field_label)
    if (field_type or "").strip().lower() not in {"text", "textarea", "search"}:
        return False
    if "what dates" in label or "when are you available" in label:
        return True
    return "dates" in label and "available" in label


def _availability_answer_from_evidence(
    field_label: str,
    evidence: dict[str, str | None],
    *,
    field_type: str | None = None,
) -> str | None:
    explicit_window = str(evidence.get("availability_window") or "").strip()
    if explicit_window:
        return explicit_window

    start_date = str(evidence.get("available_start_date") or "").strip()
    if not start_date:
        return None
    if _availability_prompt_prefers_window_text(field_label, field_type):
        human_date = _format_profile_date_for_text(start_date) or start_date
        return f"Available starting {human_date}"
    return start_date


def _is_binary_value_text(value: str | None) -> bool:
    if not value:
        return False
    normalized = _normalize_binary_match_value(value)
    if normalized in {"Yes", "No"}:
        return True
    return normalize_name(value) in {"checked", "unchecked", "true", "false"}


def _value_shape_is_compatible(field: FormField, value: str | None) -> bool:
    if value in (None, ""):
        return False

    cluster, role = _field_conditional_cluster(field)
    option_norms = _field_option_norms(field)
    expects_binary = bool(option_norms & {"yes", "no"}) or role == "boolean_parent"
    if expects_binary:
        return _is_binary_value_text(value)

    if role in {"location_child", "detail_child", "start_child", "end_child"} and _is_binary_value_text(value):
        return False

    if field.field_type in {"text", "textarea", "search"} and _is_binary_value_text(value):
        label = normalize_name(_preferred_field_label(field))
        if any(token in label for token in ("please specify", "location to which", "type of visa", "what visa")):
            return False

    if (
        cluster == "availability_window"
        and _availability_prompt_prefers_window_text(_preferred_field_label(field), field.field_type)
        and _looks_like_iso_profile_date(value)
    ):
        return False

    return True


def _log_answer_resolution(
    field: FormField,
    *,
    source_candidate: str,
    raw_answer: Any,
    coerced_answer: str | None,
    shape_compatible: bool,
    rejection_reason: str | None,
) -> None:
    logger.debug(
        "domhand.answer_resolution",
        extra={
            "field_id": field.field_id,
            "field_label": _preferred_field_label(field),
            "field_type": field.field_type,
            "widget_kind": _field_widget_kind_for_debug(field),
            "source_candidate": source_candidate,
            "raw_answer": _profile_debug_preview(raw_answer),
            "coerced_answer": _profile_debug_preview(coerced_answer),
            "shape_compatible": shape_compatible,
            "rejection_reason": rejection_reason or "",
        },
    )


def _coerce_answer_if_compatible(
    field: FormField,
    raw_answer: Any,
    *,
    source_candidate: str,
) -> str | None:
    coerced = _coerce_answer_to_field(field, raw_answer)
    if not coerced:
        _log_answer_resolution(
            field,
            source_candidate=source_candidate,
            raw_answer=raw_answer,
            coerced_answer=coerced,
            shape_compatible=False,
            rejection_reason="coercion_failed",
        )
        return None

    compatible = _value_shape_is_compatible(field, coerced)
    _log_answer_resolution(
        field,
        source_candidate=source_candidate,
        raw_answer=raw_answer,
        coerced_answer=coerced,
        shape_compatible=compatible,
        rejection_reason=None if compatible else "shape_incompatible",
    )
    return coerced if compatible else None


def _is_required_custom_widget_boolean_select(field: FormField) -> bool:
    _, role = _field_conditional_cluster(field)
    return field.required and field.field_type == "select" and not field.is_native and role == "boolean_parent"


async def _classify_known_intent_for_field(
    field: FormField,
    profile_data: dict[str, Any] | None,
    evidence: dict[str, str | None],
) -> tuple[SemanticQuestionIntent | None, str | None]:
    """Classify a blocking field into a known intent using a cheap text model."""
    if not field.required:
        return None, None

    label = _preferred_field_label(field)
    if has_cached_semantic_alias(label):
        cached = get_cached_semantic_alias(label)
        return (cached.intent, cached.confidence) if cached else (None, None)

    available = _available_semantic_intent_answers(field, profile_data, evidence)
    if not available:
        cache_semantic_alias(label, None)
        return None, None

    try:
        from browser_use.llm.messages import UserMessage
        from ghosthands.config.settings import settings as _settings
        from ghosthands.llm.client import get_chat_model
    except ImportError:
        return None, None

    model_id = _settings.semantic_match_model or _settings.domhand_model
    llm = get_chat_model(model=model_id)
    allowed_intents = [
        {
            "intent": intent,
            "description": _SEMANTIC_INTENT_DESCRIPTIONS[intent],
        }
        for intent in available.keys()
    ]
    prompt = (
        "You classify one job-application field into a known saved-profile intent.\n"
        "Return ONLY valid JSON with keys intent and confidence.\n"
        'confidence must be one of "high", "medium", or "low".\n'
        "Do not generate answer text.\n\n"
        f"Field label: {label}\n"
        f"Question text: {field.raw_label or field.name}\n"
        f"Section: {field.section or ''}\n"
        f"Field type: {field.field_type}\n"
        f"Options: {json.dumps((field.options or field.choices or [])[:20], ensure_ascii=True)}\n"
        f"Allowed intents: {json.dumps(allowed_intents, ensure_ascii=True)}\n"
    )

    try:
        response = await llm.ainvoke([UserMessage(content=prompt)], max_tokens=200)
        text = response.completion if isinstance(response.completion, str) else ""
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.MULTILINE)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned, flags=re.MULTILINE).strip()
        parsed = json.loads(cleaned)
        intent = parsed.get("intent")
        confidence = str(parsed.get("confidence") or "").strip().lower()
        if intent not in available or confidence != "high":
            _trace_profile_resolution(
                "domhand.profile_semantic_classifier_rejected",
                field_label=label,
                available_intents=",".join(sorted(available.keys())),
                proposed_intent=str(intent or ""),
                confidence=confidence or "missing",
            )
            cache_semantic_alias(label, None)
            return None, None
        alias = get_learned_question_alias(label, profile_data)
        if alias is None or alias.intent != intent:
            stage_learned_question_alias(
                label,
                intent,  # type: ignore[arg-type]
                source="semantic_fallback",
                confidence=confidence,
                profile_data=profile_data,
            )
        cache_semantic_alias(
            label,
            get_learned_question_alias(label, profile_data),
        )
        _trace_profile_resolution(
            "domhand.profile_semantic_classifier_resolved",
            field_label=label,
            available_intents=",".join(sorted(available.keys())),
            intent=intent,
            confidence=confidence,
            model=model_id,
        )
        return intent, confidence
    except Exception as exc:
        _trace_profile_resolution(
            "domhand.profile_semantic_classifier_failed",
            field_label=label,
            available_intents=",".join(sorted(available.keys())),
            error=str(exc)[:160],
            model=model_id,
        )
        cache_semantic_alias(label, None)
        return None, None


async def _semantic_profile_value_for_field(
    field: FormField,
    evidence: dict[str, str | None],
    profile_data: dict[str, Any] | None = None,
    *,
    allow_llm_classification: bool = True,
) -> str | None:
    """Resolve a field via learned aliases and, optionally, LLM semantic classification."""
    for label in _field_label_candidates(field):
        alias = get_learned_question_alias(label, profile_data)
        if alias is None:
            continue
        answer = _resolve_semantic_intent_answer(field, alias.intent, profile_data, evidence)
        coerced = _coerce_answer_if_compatible(
            field,
            answer,
            source_candidate="semantic",
        )
        if coerced:
            _trace_profile_resolution(
                "domhand.profile_learned_alias_match",
                field_label=_preferred_field_label(field),
                source_label=label,
                intent=alias.intent,
                coerced_value=_profile_debug_preview(coerced),
            )
            return coerced

    if not allow_llm_classification:
        return None

    intent, confidence = await _classify_known_intent_for_field(field, profile_data, evidence)
    if intent is None:
        return None
    answer = _resolve_semantic_intent_answer(field, intent, profile_data, evidence)
    coerced = _coerce_answer_if_compatible(
        field,
        answer,
        source_candidate="semantic",
    )
    if coerced:
        _trace_profile_resolution(
            "domhand.profile_semantic_intent_match",
            field_label=_preferred_field_label(field),
            intent=intent,
            confidence=confidence,
            coerced_value=_profile_debug_preview(coerced),
        )
        return coerced
    return None






def _known_profile_value(
    field_name: str,
    evidence: dict[str, str | None],
    profile_data: dict[str, Any] | None = None,
    *,
    field_type: str | None = None,
) -> str | None:
    """Return a profile value if the field name matches a known personal field."""
    name = normalize_name(field_name)
    if not name:
        return None
    if _is_skill_like(field_name):
        skills = _profile_skill_values(profile_data)
        if skills:
            return ", ".join(skills)
    if "suffix" in name:
        return None
    if "preferred name" in name or "nickname" in name:
        canonical = build_canonical_profile(profile_data or {}, evidence)
        return canonical.get("preferred_name")
    if "first name" in name and evidence.get("first_name"):
        return evidence["first_name"]
    if "last name" in name and evidence.get("last_name"):
        return evidence["last_name"]
    if "full name" in name:
        first = evidence.get("first_name", "")
        last = evidence.get("last_name", "")
        if first or last:
            return f"{first} {last}".strip()
    if "email" in name and evidence.get("email"):
        return evidence["email"]
    if "phone extension" in name:
        return None
    if any(kw in name for kw in ("phone device", "phone type")):
        return evidence.get("phone_device_type")
    if any(kw in name for kw in ("country phone code", "phone country code", "country code")):
        return evidence.get("phone_country_code")
    if any(kw in name for kw in ("phone", "mobile", "telephone")) and evidence.get("phone"):
        return evidence["phone"]
    if any(
        kw in name
        for kw in (
            "address line 2",
            "address 2",
            "street line 2",
            "apartment",
            "apt",
            "suite",
            "unit",
            "mailing address line 2",
        )
    ):
        return evidence.get("address_line_2")
    if any(
        kw in name
        for kw in ("address", "street address", "address line 1", "address 1", "street line 1", "mailing address")
    ):
        return evidence.get("address")
    if name == "city" or " city" in name:
        return evidence.get("city")
    if (
        name == "state"
        or "state/province" in name
        or "state / province" in name
        or "province" in name
        or name == "region"
    ):
        return evidence.get("state")
    if any(kw in name for kw in ("county", "parish", "borough")):
        return evidence.get("county")
    if "postal" in name or "zip" in name:
        return evidence.get("zip")
    if any(kw in name for kw in ("country/region", "country region", "country")):
        return evidence.get("country")
    # Combined location fields (e.g. "Location", "City, State")
    if "location" in name:
        city = evidence.get("city", "")
        state = evidence.get("state", "")
        if city and state:
            return f"{city}, {state}"
        return city or state or evidence.get("location", "") or None
    if "linkedin" in name:
        return evidence.get("linkedin")
    if "github" in name:
        return evidence.get("github")
    if any(
        kw in name
        for kw in ("portfolio", "website", "website url", "personal site", "personal website", "blog", "homepage")
    ):
        return evidence.get("portfolio")
    if "twitter" in name or "x handle" in name:
        return evidence.get("twitter")
    # Initials fields (textareas asking for user's initials as consent)
    # must be handled BEFORE the generic consent block so they return the
    # actual initials rather than "checked".
    if any(
        kw in name for kw in ("include your initials", "your initials", "enter your initials", "type your initials")
    ):
        first = (evidence.get("first_name") or "")[:1].upper()
        last = (evidence.get("last_name") or "")[:1].upper()
        initials = f"{first}{last}".strip()
        return initials or "checked"
    # Consent / agreement fields must be checked BEFORE work-auth keywords
    # because labels like "I understand ... authorized to work ... initials"
    # contain both consent phrases and auth phrases.
    if any(
        kw in name
        for kw in (
            "i agree",
            "i accept",
            "i understand",
            "i acknowledge",
            "i consent",
            "i certify",
            "privacy policy",
            "terms of service",
            "terms and conditions",
            "candidate consent",
            "agree to",
        )
    ):
        # Greenhouse / similar: mandatory "Candidate Privacy Policy" is a react-select
        # with options like "Acknowledge/Confirm", not a checkbox — "checked" cannot match.
        ft = (field_type or "").strip().lower()
        if ft == "select" and (
            "candidate privacy" in name
            or ("candidate" in name and "privacy" in name)
            or ("privacy policy" in name and "candidate" in name)
        ):
            return "Acknowledge/Confirm"
        return "checked"
    # Workday-specific field matching
    if any(
        kw in name for kw in ("how did you hear", "learn about us", "referral source", "source of referral", "source")
    ):
        return evidence.get("how_did_you_hear")
    if any(
        kw in name
        for kw in ("work authorization", "authorized to work", "legally authorized", "legally permitted to work")
    ):
        return evidence.get("work_authorization")
    if any(kw in name for kw in ("start date", "earliest start", "available date", "availability")) or (
        "dates" in name and "available" in name
    ):
        return _availability_answer_from_evidence(field_name, evidence, field_type=field_type)
    if "notice period" in name:
        return evidence.get("notice_period")
    if any(kw in name for kw in ("salary", "compensation", "pay expectation")):
        return evidence.get("salary_expectation")
    if any(kw in name for kw in ("current year in school", "school year", "academic standing", "class standing")):
        return evidence.get("current_school_year")
    if any(kw in name for kw in ("graduation date", "graduated date", "completion date")):
        return evidence.get("graduation_date")
    if any(kw in name for kw in ("degree seeking", "degree are you seeking", "degree sought", "degree pursuing")):
        return evidence.get("degree_seeking")
    if any(kw in name for kw in ("field of study", "area of study", "major", "discipline")):
        return evidence.get("field_of_study")
    if "certification" in name or "relevant license" in name or ("licenses" in name and "relevant" in name):
        return evidence.get("certifications_licenses") or "None"
    if any(kw in name for kw in ("languages spoken", "spoken languages", "preferred spoken languages")):
        return evidence.get("spoken_languages")
    if "english proficiency" in name:
        return evidence.get("english_proficiency")
    if any(kw in name for kw in ("country of residence", "country/region", "country region", "country")):
        return evidence.get("country_of_residence") or evidence.get("country")
    if any(kw in name for kw in ("willing to relocate", "relocation")):
        canonical = build_canonical_profile(profile_data or {}, evidence)
        return canonical.get("willing_to_relocate", allow_policy=True) or canonical.get(
            "relocation_preference",
            allow_policy=True,
        )
    if any(
        kw in name
        for kw in (
            "preferred work",
            "work setup",
            "work arrangement",
            "remote",
            "hybrid",
            "onsite",
            "on site",
            "location preference",
            "preferred location",
            "preferred office",
        )
    ):
        return evidence.get("preferred_work_mode") or evidence.get("preferred_locations")
    return None


def _known_profile_value_for_field(
    field: FormField,
    evidence: dict[str, str | None],
    profile_data: dict[str, Any] | None = None,
    minimum_confidence: str = "medium",
) -> str | None:
    """Try direct profile matching against all known labels for a field."""
    field_label = _preferred_field_label(field)
    profile_answer_map = _build_profile_answer_map(profile_data or {}, evidence)
    for label in _field_label_candidates(field):
        value = _find_best_profile_answer(label, profile_answer_map, minimum_confidence=minimum_confidence)
        if value:
            coerced = _coerce_answer_to_field(field, value)
            _trace_profile_resolution(
                "domhand.profile_answer_map_match",
                field_label=field_label,
                source_label=label,
                minimum_confidence=minimum_confidence,
                raw_value=_profile_debug_preview(value),
                coerced_value=_profile_debug_preview(coerced),
            )
            return coerced
    if _MATCH_CONFIDENCE_RANKS_getter().get(minimum_confidence, 0) >= _MATCH_CONFIDENCE_RANKS_getter()["strong"]:
        _trace_profile_resolution(
            "domhand.profile_lookup_miss",
            field_label=field_label,
            minimum_confidence=minimum_confidence,
            reason="strong_match_required",
        )
        return None
    if _is_structured_education_field(field):
        _trace_profile_resolution(
            "domhand.profile_lookup_miss",
            field_label=field_label,
            minimum_confidence=minimum_confidence,
            reason="structured_education_exact_only",
        )
        return None
    for label in _field_label_candidates(field):
        value = _known_profile_value(label, evidence, profile_data, field_type=field.field_type)
        if value:
            coerced = _coerce_answer_to_field(field, value)
            _trace_profile_resolution(
                "domhand.profile_keyword_match",
                field_label=field_label,
                source_label=label,
                raw_value=_profile_debug_preview(value),
                coerced_value=_profile_debug_preview(coerced),
            )
            return coerced

    # ── Q&A bank fuzzy matching (synonym-based) ──────────────────────
    # If the answer bank has an entry whose question is a synonym of the
    # field label, use it. This catches cases like "How did you hear about
    # this position?" matching a stored answer for "How did you hear about us?"
    raw_bank = (profile_data or {}).get("answerBank") or (profile_data or {}).get("answer_bank")
    if isinstance(raw_bank, list) and raw_bank:
        capped = _cap_qa_entries(list(raw_bank))
        for label in _field_label_candidates(field):
            qa_val = _match_qa_answer(label, capped)
            if qa_val:
                coerced = _coerce_answer_to_field(field, qa_val)
                _trace_profile_resolution(
                    "domhand.profile_answer_bank_match",
                    field_label=field_label,
                    source_label=label,
                    raw_value=_profile_debug_preview(qa_val),
                    coerced_value=_profile_debug_preview(coerced),
                    answer_bank_count=len(capped),
                )
                return coerced

    default_answer = _default_screening_answer(field, profile_data or {})
    if default_answer:
        _trace_profile_resolution(
            "domhand.profile_default_answer",
            field_label=field_label,
            raw_value=_profile_debug_preview(default_answer),
        )
        return default_answer
    fallback = _coerce_answer_to_field(
        field, _known_profile_value(field.name, evidence, profile_data, field_type=field.field_type)
    )
    _trace_profile_resolution(
        "domhand.profile_fallback_lookup",
        field_label=field_label,
        raw_value=_profile_debug_preview(
            _known_profile_value(field.name, evidence, profile_data, field_type=field.field_type)
        ),
        coerced_value=_profile_debug_preview(fallback),
    )
    return fallback


def _resolved_field_value(
    value: str,
    *,
    source: str,
    answer_mode: str | None,
    confidence: float,
    state: str = "filled",
) -> ResolvedFieldValue:
    _RFV = _get_ResolvedFieldValue()
    return _RFV(
        value=value,
        source=source,
        answer_mode=answer_mode,
        confidence=max(0.0, min(confidence, 1.0)),
        state=state,
    )


def _resolved_field_value_if_compatible(
    field: FormField,
    value: str | None,
    *,
    source: str,
    source_candidate: str | None = None,
    answer_mode: str | None,
    confidence: float,
    state: str = "filled",
) -> ResolvedFieldValue | None:
    coerced = _coerce_answer_if_compatible(
        field,
        value,
        source_candidate=source_candidate or source,
    )
    if not coerced:
        return None
    return _resolved_field_value(
        coerced,
        source=source,
        answer_mode=answer_mode,
        confidence=confidence,
        state=state,
    )


def _default_answer_mode_for_field(field: FormField, value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    norm = _normalize_match_label(field.name or "")
    if field.required and norm in _EEO_DECLINE_DEFAULTS_getter() and text == _EEO_DECLINE_DEFAULTS_getter()[norm]:
        return "default_decline"
    return None


def _match_confidence_score(confidence: str | None) -> float:
    return {
        "exact": 0.95,
        "strong": 0.85,
        "medium": 0.72,
        "weak": 0.58,
    }.get((confidence or "").strip().lower(), 0.55)


def _resolve_known_profile_value_for_field(
    field: FormField,
    evidence: dict[str, str | None],
    profile_data: dict[str, Any] | None = None,
    minimum_confidence: str = "medium",
) -> ResolvedFieldValue | None:
    field_label = _preferred_field_label(field)
    if _is_structured_education_field(field):
        _trace_profile_resolution(
            "domhand.profile_lookup_miss",
            field_label=field_label,
            minimum_confidence=minimum_confidence,
            reason="structured_education_exact_only",
        )
        return None
    profile_answer_map = _build_profile_answer_map(profile_data or {}, evidence)
    for label in _field_label_candidates(field):
        confidence = _label_match_confidence(label, label)
        value = _find_best_profile_answer(label, profile_answer_map, minimum_confidence=minimum_confidence)
        if value:
            coerced = _coerce_answer_if_compatible(
                field,
                value,
                source_candidate="profile",
            )
            if not coerced:
                continue
            _trace_profile_resolution(
                "domhand.profile_answer_map_match",
                field_label=field_label,
                source_label=label,
                minimum_confidence=minimum_confidence,
                raw_value=_profile_debug_preview(value),
                coerced_value=_profile_debug_preview(coerced),
            )
            return _resolved_field_value(
                coerced,
                source="derived_profile",
                answer_mode="profile_backed",
                confidence=_match_confidence_score(confidence),
            )
    if _MATCH_CONFIDENCE_RANKS_getter().get(minimum_confidence, 0) >= _MATCH_CONFIDENCE_RANKS_getter()["strong"]:
        _trace_profile_resolution(
            "domhand.profile_lookup_miss",
            field_label=field_label,
            minimum_confidence=minimum_confidence,
            reason="strong_match_required",
        )
        return None
    for label in _field_label_candidates(field):
        value = _known_profile_value(label, evidence, profile_data, field_type=field.field_type)
        if value:
            coerced = _coerce_answer_if_compatible(
                field,
                value,
                source_candidate="profile",
            )
            if not coerced:
                continue
            _trace_profile_resolution(
                "domhand.profile_keyword_match",
                field_label=field_label,
                source_label=label,
                raw_value=_profile_debug_preview(value),
                coerced_value=_profile_debug_preview(coerced),
            )
            return _resolved_field_value(
                coerced,
                source="derived_profile",
                answer_mode="profile_backed",
                confidence=0.82,
            )

    raw_bank = (profile_data or {}).get("answerBank") or (profile_data or {}).get("answer_bank")
    if isinstance(raw_bank, list) and raw_bank:
        capped = _cap_qa_entries(list(raw_bank))
        for label in _field_label_candidates(field):
            qa_val = _match_qa_answer(label, capped)
            if qa_val:
                coerced = _coerce_answer_if_compatible(
                    field,
                    qa_val,
                    source_candidate="profile",
                )
                if not coerced:
                    continue
                _trace_profile_resolution(
                    "domhand.profile_answer_bank_match",
                    field_label=field_label,
                    source_label=label,
                    raw_value=_profile_debug_preview(qa_val),
                    coerced_value=_profile_debug_preview(coerced),
                    answer_bank_count=len(capped),
                )
                return _resolved_field_value(
                    coerced,
                    source="derived_profile",
                    answer_mode="profile_backed",
                    confidence=0.8,
                )

    default_answer = _default_screening_answer(field, profile_data or {})
    if default_answer:
        _trace_profile_resolution(
            "domhand.profile_default_answer",
            field_label=field_label,
            raw_value=_profile_debug_preview(default_answer),
        )
        return _resolved_field_value_if_compatible(
            field,
            default_answer,
            source="dom",
            source_candidate="default",
            answer_mode=_default_answer_mode_for_field(field, default_answer),
            confidence=0.68,
        )

    fallback = _coerce_answer_if_compatible(
        field,
        _known_profile_value(field.name, evidence, profile_data, field_type=field.field_type),
        source_candidate="profile",
    )
    _trace_profile_resolution(
        "domhand.profile_fallback_lookup",
        field_label=field_label,
        raw_value=_profile_debug_preview(
            _known_profile_value(field.name, evidence, profile_data, field_type=field.field_type)
        ),
        coerced_value=_profile_debug_preview(fallback),
    )
    if not fallback:
        return None
    return _resolved_field_value(
        fallback,
        source="dom",
        answer_mode="profile_backed",
        confidence=0.75,
    )


def _default_value(field: FormField) -> str:
    """Fallback values allowed by strict-provenance policy only."""
    name_lower = normalize_name(field.name or "")
    if any(token in name_lower for token in ("signature date", "today", "current date")):
        return date.today().isoformat()
    if _is_self_identify_date_field(field):
        return date.today().isoformat()

    # EEO / demographic decline defaults — last resort for required fields
    # that were not matched by _match_answer.
    if field.required:
        norm = _normalize_match_label(field.name or "")
        if norm in _EEO_DECLINE_DEFAULTS_getter():
            return _EEO_DECLINE_DEFAULTS_getter()[norm]
        if "certification" in norm or "relevant license" in norm or ("licenses" in norm and "relevant" in norm):
            return "None"

    return ""
