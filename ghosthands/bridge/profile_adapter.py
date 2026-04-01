"""Profile adaptation layer for the Desktop bridge.

Converts camelCase profile keys from the Desktop app (TypeScript conventions)
to the snake_case format expected by DomHand and the rest of the Python
codebase, and fills in only structural defaults that the Desktop bridge omits.
Survey-backed answers must flow from the saved profile; they are not invented
here.
"""

from __future__ import annotations

from typing import Any

# Structural defaults duplicated here to avoid importing the integrations
# package (which pulls in asyncpg/database). These are safe shape defaults, not
# survey-backed answers.
DOMHAND_PROFILE_DEFAULTS: dict[str, Any] = {
    "phone_device_type": "Mobile",
    "phone_country_code": "+1",
    "address": {
        "street": "",
        "city": "",
        "state": "",
        "zip": "",
        "county": "",
        "country": "United States of America",
    },
}


CAMEL_TO_SNAKE_SCALAR: dict[str, str] = {
    "firstName": "first_name",
    "lastName": "last_name",
    "preferredName": "preferred_name",
    "linkedIn": "linkedin",
    "zipCode": "zip",
    "workAuthorization": "work_authorization",
    "visaSponsorship": "visa_sponsorship",
    "authorizedToWorkInUs": "authorized_to_work_in_us",
    "needsVisaSponsorship": "needs_visa_sponsorship",
    "citizenshipStatus": "citizenship_status",
    "usCitizen": "us_citizen",
    "exportControlEligible": "export_control_eligible",
    "raceEthnicity": "race_ethnicity",
    "veteranStatus": "veteran_status",
    "disabilityStatus": "disability_status",
    "phoneDeviceType": "phone_device_type",
    "phoneCountryCode": "phone_country_code",
    "salaryExpectation": "salary_expectation",
    "spokenLanguages": "spoken_languages",
    "englishProficiency": "english_proficiency",
    "countryOfResidence": "country_of_residence",
    "preferredWorkMode": "preferred_work_mode",
    "preferredLocations": "preferred_locations",
    "willingToRelocate": "willing_to_relocate",
    "howDidYouHear": "how_did_you_hear",
    "availabilityWindow": "availability_window",
    "noticePeriod": "notice_period",
    "currentSchoolYear": "current_school_year",
    "graduationDate": "graduation_date",
    "degreeSeeking": "degree_seeking",
    "certificationsLicenses": "certifications_licenses",
}

CAMEL_TO_SNAKE_NESTED: dict[str, str] = {
    "fieldOfStudy": "field_of_study",
    "degreeType": "degree_type",
    "majorName": "major_name",
    "majorNames": "major_names",
    "minorName": "minor_name",
    "minorNames": "minor_names",
    "honorsList": "honors_list",
    "honoursList": "honours_list",
    "startDate": "start_date",
    "endDate": "end_date",
    "graduationDate": "graduation_date",
}


def camel_to_snake_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Convert known camelCase keys from the Desktop bridge to snake_case.

    The Desktop app sends profile data with camelCase keys (matching the
    VALET API's TypeScript conventions).  DomHand and the rest of the
    Python codebase expect snake_case.

    This function adds snake_case equivalents for known camelCase keys
    without removing the originals, so both formats work downstream.

    Also handles ``zipCode`` -> ``zip`` *and* ``postal_code`` (for prompts).
    """
    out = dict(profile)

    # ── Scalar fields ────────────────────────────────────────────
    for camel, snake in CAMEL_TO_SNAKE_SCALAR.items():
        if camel in out and snake not in out:
            out[snake] = out[camel]

    # zipCode also maps to postal_code for prompt templates
    if "zipCode" in out and "postal_code" not in out:
        out["postal_code"] = out["zipCode"]

    # Runtime-learned registry payloads used by the desktop claim path
    if "learnedQuestionAliases" in out and "learned_question_aliases" not in out:
        out["learned_question_aliases"] = out["learnedQuestionAliases"]
    if "learnedInteractionRecipes" in out and "learned_interaction_recipes" not in out:
        out["learned_interaction_recipes"] = out["learnedInteractionRecipes"]

    # ── Nested arrays: education / experience ────────────────────
    for array_key in ("education", "experience"):
        items = out.get(array_key)
        if not isinstance(items, list):
            continue
        converted = []
        for item in items:
            if not isinstance(item, dict):
                converted.append(item)
                continue
            new_item = dict(item)
            for camel, snake in CAMEL_TO_SNAKE_NESTED.items():
                if camel in new_item and snake not in new_item:
                    new_item[snake] = new_item[camel]
            converted.append(new_item)
        out[array_key] = converted

    return out


def normalize_profile_defaults(profile: dict[str, Any]) -> dict[str, Any]:
    """Add DomHand-expected structural fields that the Desktop bridge omits.

    When the Desktop app passes a raw ``UserProfile`` via ``--profile`` or
    ``GH_USER_PROFILE_TEXT``, it may be missing fields that the old
    TypeScript ``toWorkdayProfile()`` transformation would have added.
    DomHand's ``_parse_profile_evidence`` and ``_known_profile_value``
    rely on these fields being present in the profile.

    This function only fills non-sensitive structural defaults needed for
    consistent downstream parsing. Existing values in the profile are never
    overwritten, and missing survey-backed answers remain missing so
    propagation bugs stay visible.
    """
    defaults = DOMHAND_PROFILE_DEFAULTS
    normalized = dict(profile)

    # ── Scalar defaults ──────────────────────────────────────────
    for key in (
        "phone_device_type",
        "phone_country_code",
    ):
        if key not in normalized or normalized[key] is None or normalized[key] == "":
            normalized[key] = defaults[key]

    # ── Address defaults (merge, don't overwrite) ────────────────
    default_address = defaults["address"]
    existing_address = normalized.get("address")

    if existing_address is None or existing_address == "":
        normalized["address"] = dict(default_address)
    elif isinstance(existing_address, dict):
        merged = dict(default_address)
        for k, v in existing_address.items():
            if v is not None and v != "":
                merged[k] = v
        normalized["address"] = merged
    # If address is a string (e.g. "San Francisco, CA"), leave it as-is —
    # _format_profile_summary handles string addresses fine.

    return normalized
