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
    register_profile_alias("country", ("country",), ("address", "country"))
    register_profile_alias("phone_device_type", ("phone_device_type",), ("phone_type",))
    register_profile_alias("phone_country_code", ("phone_country_code",))
    register_profile_alias("linkedin", ("linkedin",), ("linkedin_url",))
    register_profile_alias("portfolio", ("portfolio",), ("website",), ("website_url",), ("personal_website",))
    register_profile_alias("github", ("github",), ("github_url",))
    register_profile_alias("twitter", ("twitter",), ("twitter_url",), ("x",), ("x_url",))
    register_profile_alias("work_authorization", ("work_authorization",))
    register_profile_alias("available_start_date", ("available_start_date",))
    register_profile_alias("salary_expectation", ("salary_expectation",))
    register_profile_alias("how_did_you_hear", ("how_did_you_hear",), ("referral_source",))
    register_profile_alias("willing_to_relocate", ("willing_to_relocate",))
    register_profile_alias("gender", ("gender",))
    register_profile_alias("race_ethnicity", ("race_ethnicity",), ("race",))
    register_profile_alias("veteran_status", ("veteran_status",), ("Veteran_status",))
    register_profile_alias("disability_status", ("disability_status",))
    register_profile_alias("authorized_to_work", ("authorized_to_work",), ("US_citizen",))
    register_profile_alias("sponsorship_needed", ("sponsorship_needed",), ("visa_sponsorship",))

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
        "country": "country",
        "phone_device_type": "phone_device_type",
        "phone_country_code": "phone_country_code",
        "linkedin": "linkedin",
        "portfolio": "portfolio",
        "github": "github",
        "twitter": "twitter",
        "work_authorization": "work_authorization",
        "available_start_date": "available_start_date",
        "salary_expectation": "salary_expectation",
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
