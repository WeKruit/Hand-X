"""Canonical applicant-profile normalization with provenance tracking."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ProfileProvenance = Literal["explicit", "derived", "policy"]


class CanonicalValue(BaseModel):
    """A normalized applicant value with provenance metadata."""

    model_config = ConfigDict(extra="ignore")

    key: str
    value: str
    provenance: ProfileProvenance
    source_path: str | None = None


class CanonicalProfile(BaseModel):
    """Normalized applicant profile used by apply-flow automation."""

    model_config = ConfigDict(extra="ignore")

    values: dict[str, CanonicalValue] = Field(default_factory=dict)
    education: list[dict[str, Any]] = Field(default_factory=list)
    experience: list[dict[str, Any]] = Field(default_factory=list)
    languages: list[dict[str, Any]] = Field(default_factory=list)

    def get(self, key: str, *, allow_policy: bool = False) -> str | None:
        entry = self.values.get(key)
        if not entry:
            return None
        if entry.provenance == "policy" and not allow_policy:
            return None
        return entry.value


def _as_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list, tuple, set)):
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = str(value).strip()
    return text or None


def _get_nested(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _copy_profile_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict) and item:
            out.append(dict(item))
    return out


def _copy_language_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        language = _as_text(item.get("language"))
        overall_proficiency = _as_text(
            item.get("overallProficiency") or item.get("overall_proficiency") or item.get("proficiency")
        )
        reading_writing = _as_text(item.get("readingWriting") or item.get("reading_writing"))
        speaking_listening = _as_text(item.get("speakingListening") or item.get("speaking_listening"))
        is_fluent_raw = item.get("isFluent", item.get("is_fluent"))
        if language:
            out.append(
                {
                    "language": language,
                    "overall_proficiency": overall_proficiency or "",
                    "reading_writing": reading_writing or "",
                    "speaking_listening": speaking_listening or "",
                    "is_fluent": bool(is_fluent_raw) if isinstance(is_fluent_raw, bool) else None,
                }
            )
    return out


def _text_value(value: Any) -> str | None:
    if value in (None, "", []):
        return None
    if isinstance(value, dict):
        for key in ("name", "label", "value", "title"):
            text = _text_value(value.get(key))
            if text:
                return text
        return None
    if isinstance(value, set):
        parts = sorted(part for part in (_text_value(item) for item in value) if part)
        return ", ".join(parts) if parts else None
    if isinstance(value, (list, tuple)):
        parts = [part for part in (_text_value(item) for item in value) if part]
        return ", ".join(parts) if parts else None
    text = str(value).strip()
    return text or None


def _education_value(entry: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        text = _text_value(entry.get(key))
        if text:
            return text
    return None


def _score_profile_date(value: Any) -> int:
    text = _as_text(value)
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


def _latest_education_entry(education: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not education:
        return None
    ranked = sorted(
        education,
        key=lambda entry: _score_profile_date(
            entry.get("end_date") or entry.get("graduation_date") or entry.get("start_date")
        ),
        reverse=True,
    )
    return ranked[0] if ranked else None


def _format_graduation_date(value: Any) -> str | None:
    text = _as_text(value)
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


def build_canonical_profile(
    profile_data: dict[str, Any] | None,
    evidence: dict[str, str | None] | None = None,
) -> CanonicalProfile:
    """Normalize profile data and text evidence into a canonical profile."""

    profile = profile_data or {}
    observed = evidence or {}
    canonical = CanonicalProfile(
        education=_copy_profile_list(profile.get("education")),
        experience=_copy_profile_list(profile.get("experience")),
        languages=_copy_language_list(profile.get("languages")),
    )

    def register(
        key: str,
        value: Any,
        *,
        source_path: str | None,
        provenance: ProfileProvenance = "explicit",
    ) -> None:
        text = _as_text(value)
        if not text:
            return
        existing = canonical.values.get(key)
        if existing and existing.provenance == "explicit" and provenance != "explicit":
            return
        canonical.values[key] = CanonicalValue(
            key=key,
            value=text,
            provenance=provenance,
            source_path=source_path,
        )

    def register_profile_alias(key: str, *paths: tuple[str, ...]) -> None:
        for path in paths:
            value = _get_nested(profile, path)
            if _as_text(value):
                register(key, value, source_path=".".join(path))
                return

    register_profile_alias("first_name", ("first_name",))
    register_profile_alias("last_name", ("last_name",))
    register_profile_alias("full_name", ("full_name",), ("name",))
    register_profile_alias("preferred_name", ("preferred_name",))
    register_profile_alias("email", ("email",))
    register_profile_alias("phone", ("phone",))
    register_profile_alias("address", ("address",), ("address", "street"))
    register_profile_alias("address_line_2", ("address_line_2",), ("address", "line2"), ("address", "street2"))
    register_profile_alias("city", ("city",), ("address", "city"))
    register_profile_alias("state", ("state",), ("province",), ("address", "state"))
    register_profile_alias("postal_code", ("postal_code",), ("zip",), ("zip_code",), ("address", "zip"))
    register_profile_alias("county", ("county",), ("address", "county"))
    register_profile_alias("country", ("country",), ("address", "country"))
    register_profile_alias("phone_device_type", ("phone_device_type",), ("phone_type",))
    register_profile_alias("phone_country_code", ("phone_country_code",))
    register_profile_alias("linkedin", ("linkedin",), ("linkedin_url",))
    register_profile_alias("portfolio", ("portfolio",), ("website",), ("website_url",), ("personal_website",))
    register_profile_alias("github", ("github",), ("github_url",))
    register_profile_alias("twitter", ("twitter",), ("twitter_url",), ("x",), ("x_url",))
    register_profile_alias("work_authorization", ("work_authorization",))
    register_profile_alias("available_start_date", ("available_start_date",))
    register_profile_alias("availability_window", ("availability_window",), ("available_start_date",))
    register_profile_alias("notice_period", ("notice_period",))
    register_profile_alias("salary_expectation", ("salary_expectation",))
    register_profile_alias("current_school_year", ("current_school_year",), ("currentSchoolYear",))
    register_profile_alias("graduation_date", ("graduation_date",), ("graduationDate",))
    register_profile_alias("degree_seeking", ("degree_seeking",), ("degreeSeeking",))
    register_profile_alias("degree_type", ("degree_type",), ("degreeType",))
    register_profile_alias(
        "field_of_study",
        ("field_of_study",),
        ("fieldOfStudy",),
        ("major",),
        ("majors",),
        ("majorName",),
        ("majorNames",),
        ("major_name",),
        ("major_names",),
    )
    register_profile_alias(
        "minor",
        ("minor",),
        ("minors",),
        ("minorName",),
        ("minorNames",),
        ("minor_name",),
        ("minor_names",),
    )
    register_profile_alias(
        "honors",
        ("honors",),
        ("honours",),
        ("honorsList",),
        ("honoursList",),
        ("honors_list",),
        ("honours_list",),
    )
    register_profile_alias("certifications_licenses", ("certifications_licenses",), ("certificationsLicenses",))
    register_profile_alias("spoken_languages", ("spoken_languages",))
    register_profile_alias("english_proficiency", ("english_proficiency",))
    register_profile_alias("country_of_residence", ("country_of_residence",))
    register_profile_alias("preferred_work_mode", ("preferred_work_mode",))
    register_profile_alias("preferred_locations", ("preferred_locations",))
    register_profile_alias("how_did_you_hear", ("how_did_you_hear",), ("referral_source",))
    register_profile_alias("willing_to_relocate", ("willing_to_relocate",))
    register_profile_alias("gender", ("gender",))
    register_profile_alias("race_ethnicity", ("race_ethnicity",), ("race",))
    register_profile_alias("veteran_status", ("veteran_status",), ("Veteran_status",))
    register_profile_alias("disability_status", ("disability_status",))
    register_profile_alias("authorized_to_work", ("authorized_to_work",), ("US_citizen",))
    register_profile_alias("sponsorship_needed", ("sponsorship_needed",), ("visa_sponsorship",))

    language_entries = canonical.languages
    if language_entries:
        if "spoken_languages" not in canonical.values:
            register(
                "spoken_languages",
                ", ".join(
                    f"{entry['language']} ({entry['overall_proficiency']})".strip()
                    if entry.get("overall_proficiency")
                    else entry["language"]
                    for entry in language_entries
                ),
                source_path="languages",
                provenance="derived",
            )
        if "english_proficiency" not in canonical.values:
            for entry in language_entries:
                if entry.get("language", "").strip().lower() == "english" and entry.get("overall_proficiency"):
                    register(
                        "english_proficiency",
                        entry["overall_proficiency"],
                        source_path="languages.english.proficiency",
                        provenance="derived",
                    )
                    break

    latest_education = _latest_education_entry(canonical.education)
    if latest_education:
        if "degree_seeking" not in canonical.values:
            register(
                "degree_seeking",
                _education_value(latest_education, "degree"),
                source_path="education.latest.degree",
                provenance="derived",
            )
        if "degree_type" not in canonical.values:
            register(
                "degree_type",
                _education_value(latest_education, "degree_type", "degreeType"),
                source_path="education.latest.degree_type",
                provenance="derived",
            )
        if "graduation_date" not in canonical.values:
            register(
                "graduation_date",
                _format_graduation_date(latest_education.get("graduation_date") or latest_education.get("end_date")),
                source_path="education.latest.graduation_date",
                provenance="derived",
            )
        if "field_of_study" not in canonical.values:
            register(
                "field_of_study",
                _education_value(
                    latest_education,
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
                ),
                source_path="education.latest.field_of_study",
                provenance="derived",
            )
        if "minor" not in canonical.values:
            register(
                "minor",
                _education_value(
                    latest_education,
                    "minor",
                    "minors",
                    "minorName",
                    "minorNames",
                    "minor_name",
                    "minor_names",
                ),
                source_path="education.latest.minor",
                provenance="derived",
            )
        if "honors" not in canonical.values:
            register(
                "honors",
                _education_value(
                    latest_education,
                    "honors",
                    "honours",
                    "honor",
                    "honorsList",
                    "honoursList",
                    "honors_list",
                    "honours_list",
                ),
                source_path="education.latest.honors",
                provenance="derived",
            )

    for key, source_key in {
        "first_name": "first_name",
        "last_name": "last_name",
        "email": "email",
        "phone": "phone",
        "address": "address",
        "address_line_2": "address_line_2",
        "city": "city",
        "state": "state",
        "postal_code": "zip",
        "county": "county",
        "country": "country",
        "phone_device_type": "phone_device_type",
        "phone_country_code": "phone_country_code",
        "linkedin": "linkedin",
        "portfolio": "portfolio",
        "github": "github",
        "twitter": "twitter",
        "work_authorization": "work_authorization",
        "available_start_date": "available_start_date",
        "availability_window": "availability_window",
        "notice_period": "notice_period",
        "salary_expectation": "salary_expectation",
        "current_school_year": "current_school_year",
        "graduation_date": "graduation_date",
        "degree_seeking": "degree_seeking",
        "certifications_licenses": "certifications_licenses",
        "field_of_study": "field_of_study",
        "spoken_languages": "spoken_languages",
        "english_proficiency": "english_proficiency",
        "country_of_residence": "country_of_residence",
        "preferred_work_mode": "preferred_work_mode",
        "preferred_locations": "preferred_locations",
        "how_did_you_hear": "how_did_you_hear",
        "willing_to_relocate": "willing_to_relocate",
    }.items():
        if key not in canonical.values and observed.get(source_key):
            register(key, observed[source_key], source_path=f"evidence.{source_key}")

    first_name = canonical.get("first_name")
    last_name = canonical.get("last_name")
    if "full_name" not in canonical.values and (first_name or last_name):
        register(
            "full_name",
            " ".join(part for part in (first_name, last_name) if part),
            source_path="derived.first_name_last_name",
            provenance="derived",
        )

    return canonical
