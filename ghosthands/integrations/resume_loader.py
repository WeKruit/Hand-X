"""Resume profile loading — normalizes VALET's parsed resume data for form filling.

Mirrors ResumeProfileLoader from GH's TypeScript codebase, producing a flat
profile dictionary that DomHand uses to match form fields to user data.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import structlog

from ghosthands.integrations.database import Database

logger = structlog.get_logger()

# ── Default values for fields not present in parsed resume data ───────────

PROFILE_DEFAULTS: dict[str, Any] = {
    "phone_device_type": "",
    "phone_country_code": "",
    "address": {
        "street": "",
        "city": "",
        "state": "",
        "zip": "",
        "country": "",
    },
    "work_authorization": "",
    "visa_sponsorship": "",
    "veteran_status": "",
    "disability_status": "",
    "gender": "",
    "race_ethnicity": "",
}


def _text_value(value: Any) -> str | None:
    """Return a deterministic text representation for resume fields."""
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


def _education_text(edu: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = _text_value(edu.get(key))
        if text:
            return text
    return ""


# ── Public API ────────────────────────────────────────────────────────────


async def load_resume(db: Database, user_id: str) -> dict[str, Any]:
    """Load and normalize a user's resume profile from the database.

    Queries VALET's ``resumes`` table for the user's default parsed resume,
    then maps the camelCase ``parsed_data`` JSONB into a flat profile dict
    suitable for form filling.

    Args:
            db: Connected Database instance.
            user_id: VALET user UUID.

    Returns:
            Normalized profile dictionary with keys like ``first_name``,
            ``last_name``, ``email``, ``education``, ``experience``, etc.

    Raises:
            ValueError: If no parsed resume exists for the user.
    """
    raw = await db.load_resume_profile(user_id)
    parsed_data = raw["parsed_data"]
    profile = _map_to_profile(parsed_data)

    # Attach resume metadata for the caller
    profile["_resume_id"] = raw["resume_id"]
    profile["_file_key"] = raw.get("file_key")
    profile["_parsing_confidence"] = raw.get("parsing_confidence")
    profile["_raw_text"] = raw.get("raw_text")

    logger.info(
        "resume_loader.loaded",
        user_id=user_id,
        resume_id=raw["resume_id"],
        name=f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip(),
        confidence=raw.get("parsing_confidence"),
    )

    return profile


async def load_runtime_profile(
    db: Database,
    user_id: str,
    resume_id: str | None = None,
) -> dict[str, Any]:
    """Load a desktop-like runtime profile from VALET data sources.

    This reconstructs the profile shape Hand-X expects when testing against a
    real VALET user:

    1. Parsed resume data provides the base profile.
    2. Global user application profile supplies survey-backed defaults.
    3. Resume-specific application profile overrides win over global values.
    4. QA bank entries are attached as answer-bank context.
    """
    if resume_id:
        raw_resume = await db.load_resume_profile_by_id(user_id, resume_id)
    else:
        raw_resume = await db.load_resume_profile(user_id)

    user_profile, global_application_profile, resume_application_profile, answer_bank = await asyncio.gather(
        db.load_user_profile(user_id),
        db.load_user_application_profile(user_id),
        db.load_resume_application_profile(user_id, raw_resume["resume_id"]),
        db.load_answer_bank(user_id),
    )

    profile = _map_to_profile(raw_resume["parsed_data"])
    _apply_user_fallbacks(profile, user_profile or {})
    merged_application_profile = _merge_application_profiles(
        global_application_profile,
        resume_application_profile,
    )
    _apply_application_profile_overrides(profile, merged_application_profile)

    answer_bank_entries = [
        {
            "question": row["question"],
            "answer": row["answer"],
            "canonical_question": row.get("canonical_question"),
            "intent_tag": row.get("intent_tag"),
            "usage_mode": row.get("usage_mode"),
            "source": row.get("source"),
            "confidence": row.get("confidence"),
            "synonyms": row.get("synonyms"),
        }
        for row in answer_bank
    ]
    if answer_bank_entries:
        profile["answer_bank"] = answer_bank_entries
        profile["answerBank"] = answer_bank_entries

    profile["_resume_id"] = raw_resume["resume_id"]
    profile["_file_key"] = raw_resume.get("file_key")
    profile["_parsing_confidence"] = raw_resume.get("parsing_confidence")
    profile["_raw_text"] = raw_resume.get("raw_text")

    return profile


async def load_runtime_profile_from_api(
    api_url: str,
    runtime_grant: str,
    resume_id: str | None = None,
) -> dict[str, Any]:
    """Load profile from VALET API — same normalization as load_runtime_profile().

    The VALET API endpoint returns raw DB data (user, applicationProfile,
    resumeParsedData, answerBank).  This function feeds that data through
    the SAME _map_to_profile() + _apply_user_fallbacks() +
    _apply_application_profile_overrides() code that the DB-direct path uses,
    guaranteeing identical output regardless of entry point.
    """
    import httpx

    url = f"{api_url.rstrip('/')}/api/v1/local-workers/profile"
    if resume_id:
        url += f"?resumeId={resume_id}"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers={"x-api-key": runtime_grant})
        resp.raise_for_status()
        data = resp.json()

    user_profile = data.get("user") or {}
    application_profile = data.get("applicationProfile") or {}
    resume_parsed_data = data.get("resumeParsedData") or {}
    answer_bank_rows = data.get("answerBank") or []
    api_resume_id = data.get("resumeId")

    # ── Same normalization as load_runtime_profile() ──────────────
    if resume_parsed_data:
        profile = _map_to_profile(resume_parsed_data)
    else:
        # No resume parsed data — build minimal profile from user table
        profile = _map_to_profile(user_profile)

    _apply_user_fallbacks(profile, user_profile)
    _apply_application_profile_overrides(profile, application_profile)

    answer_bank_entries = [
        {
            "question": row.get("question", ""),
            "answer": row.get("answer", ""),
            "canonical_question": row.get("canonicalQuestion") or row.get("canonical_question"),
            "intent_tag": row.get("intentTag") or row.get("intent_tag"),
            "usage_mode": row.get("usageMode") or row.get("usage_mode"),
            "source": row.get("source"),
            "confidence": row.get("confidence"),
            "synonyms": row.get("synonyms"),
        }
        for row in answer_bank_rows
    ]
    if answer_bank_entries:
        profile["answer_bank"] = answer_bank_entries
        profile["answerBank"] = answer_bank_entries

    if api_resume_id:
        profile["_resume_id"] = api_resume_id

    return profile


def _is_already_flat_profile(data: dict[str, Any]) -> bool:
    """Return True when *data* looks like an already-normalized flat profile.

    Flat profiles use ``first_name`` / ``last_name`` (snake_case) and may
    include ``education`` or ``experience`` as structured arrays.  VALET's
    camelCase parsed-data instead uses ``fullName`` and ``workHistory``.

    Without this guard, a flat JSON file that contains ``education`` would
    incorrectly trigger ``_map_to_profile`` which only reads ``fullName``
    for names — destroying ``first_name`` / ``last_name``.
    """
    return bool(data.get("first_name") or data.get("last_name"))


def load_resume_from_file(path: str) -> dict[str, Any]:
    """Load a resume profile from a JSON file (for testing/development).

    The file should contain either:
    - A ``parsed_data`` dict matching VALET's camelCase format, or
    - A pre-normalized flat profile dict (will be returned as-is).

    Args:
            path: Path to a JSON file.

    Returns:
            Normalized profile dictionary.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    # If it looks like it wraps parsed_data
    if "parsed_data" in data:
        return _map_to_profile(data["parsed_data"])

    # Already a flat profile with first_name/last_name — return as-is
    if _is_already_flat_profile(data):
        return data

    # VALET camelCase format (fullName, workHistory, education)
    if "fullName" in data or "workHistory" in data or "education" in data:
        return _map_to_profile(data)

    # Assume it's already a normalized profile
    return data


# ── Mapping: VALET parsed_data -> flat profile ────────────────────────────


def _map_to_profile(
    data: dict[str, Any],
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map VALET's camelCase parsed_data to a flat profile dict.

    This mirrors the ``mapToWorkdayProfile`` function from GH's
    ``resumeProfileLoader.ts``, adapted for Python.
    """
    defs = defaults or PROFILE_DEFAULTS

    # Name parsing
    full_name = (data.get("fullName") or "").strip()
    name_parts = full_name.split() if full_name else []
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    # Address parsing
    address = _parse_location(data.get("location"), defs["address"])

    # Website extraction
    websites = data.get("websites") or []
    linkedin_url = next((w for w in websites if "linkedin" in w.lower()), "")
    website_url = next((w for w in websites if "linkedin" not in w.lower()), "")
    if linkedin_url and not linkedin_url.startswith("http"):
        linkedin_url = f"https://{linkedin_url}"
    if website_url and not website_url.startswith("http"):
        website_url = f"https://{website_url}"

    # Current job
    work_history = data.get("workHistory") or []
    current_job = work_history[0] if work_history else {}

    # Phone cleanup
    phone = re.sub(r"[^\d+]", "", data.get("phone") or "")

    return {
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "email": data.get("email") or "",
        "phone": phone,
        "phone_device_type": defs["phone_device_type"],
        "phone_country_code": defs["phone_country_code"],
        "address": address,
        "linkedin_url": linkedin_url,
        "website_url": website_url,
        "current_company": current_job.get("company") or "",
        "current_title": current_job.get("title") or "",
        "summary": data.get("summary") or "",
        "education": [
            {
                "school": _education_text(edu, "school", "institution"),
                "degree": _education_text(edu, "degree"),
                "degree_type": _education_text(edu, "degreeType", "degree_type"),
                "field_of_study": _education_text(
                    edu,
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
                "major": _education_text(
                    edu,
                    "major",
                    "majors",
                    "majorName",
                    "majorNames",
                    "major_name",
                    "major_names",
                ),
                "minor": _education_text(
                    edu,
                    "minor",
                    "minors",
                    "minorName",
                    "minorNames",
                    "minor_name",
                    "minor_names",
                ),
                "honors": _education_text(
                    edu,
                    "honors",
                    "honours",
                    "honorsList",
                    "honoursList",
                    "honors_list",
                    "honours_list",
                ),
                "gpa": _text_value(edu.get("gpa")),
                "start_date": _education_text(edu, "startDate", "start_date"),
                "end_date": _education_text(edu, "endDate", "expectedGraduation", "end_date"),
                "end_date_type": (
                    "Actual" if edu.get("endDate") else "Expected" if edu.get("expectedGraduation") else ""
                ),
                "currently_enrolled": bool(edu.get("expectedGraduation") and not edu.get("endDate")),
            }
            for edu in (data.get("education") or [])
        ],
        "experience": [
            {
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "location": job.get("location") or "",
                "currently_work_here": not job.get("endDate"),
                "start_date": job.get("startDate") or "",
                "end_date": job.get("endDate") or "",
                "end_date_type": "Actual" if job.get("endDate") else "Current",
                "description": (job.get("description") or ". ".join(job.get("bullets") or []) or ""),
            }
            for job in work_history
        ],
        "skills": data.get("skills") or [],
        "certifications": data.get("certifications") or [],
        "languages": data.get("languages") or [],
        "projects": [
            {
                "name": proj.get("name", ""),
                "description": proj.get("description") or "",
                "technologies": proj.get("technologies") or [],
                "url": proj.get("url") or "",
            }
            for proj in (data.get("projects") or [])
        ],
        "awards": [
            {
                "title": award.get("title", ""),
                "issuer": award.get("issuer") or "",
                "date": award.get("date") or "",
            }
            for award in (data.get("awards") or [])
        ],
        "volunteer_work": [
            {
                "organization": vol.get("organization", ""),
                "role": vol.get("role") or "",
                "description": vol.get("description") or "",
                "start_date": vol.get("startDate") or "",
                "end_date": vol.get("endDate") or "",
            }
            for vol in (data.get("volunteerWork") or [])
        ],
        "total_years_experience": data.get("totalYearsExperience"),
        "work_authorization": data.get("workAuthorization") or defs["work_authorization"],
        "visa_sponsorship": defs["visa_sponsorship"],
        "veteran_status": defs["veteran_status"],
        "disability_status": defs["disability_status"],
        "gender": defs["gender"],
        "race_ethnicity": defs["race_ethnicity"],
        "resume_path": "",  # Set by caller from file_key
    }


def _parse_location(
    location: str | None,
    default_address: dict[str, str],
) -> dict[str, str]:
    """Best-effort parse of a location string into address components.

    Handles formats like:
    - "San Francisco, CA"
    - "San Francisco, CA 94103"
    - "New York, NY, United States"
    """
    if not location:
        return dict(default_address)

    parts = [p.strip() for p in location.split(",")]
    if len(parts) >= 2:
        city = parts[0]
        state_zip = parts[-1].strip() if len(parts) == 2 else parts[1].strip()
        state_zip_parts = state_zip.split()
        state = state_zip_parts[0] if state_zip_parts else ""
        zip_code = state_zip_parts[1] if len(state_zip_parts) > 1 else default_address.get("zip", "")
        return {
            "street": default_address.get("street", ""),
            "city": city,
            "state": state,
            "zip": zip_code,
            "country": default_address.get("country", "United States of America"),
        }

    return {**default_address, "city": location}


def _has_profile_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return value is not None


def _normalize_application_profile_languages(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return value
    try:
        parsed = json.loads(raw)
    except Exception:
        return value
    return parsed


def _merge_application_profiles(
    global_profile: dict[str, Any] | None,
    resume_specific_profile: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not global_profile and not resume_specific_profile:
        return None
    if not resume_specific_profile:
        return dict(global_profile or {})
    if not global_profile:
        return dict(resume_specific_profile)

    merged = dict(global_profile)
    for key, value in resume_specific_profile.items():
        if key in {"id", "user_id", "resume_id", "created_at", "updated_at"}:
            continue
        if key == "languages":
            value = _normalize_application_profile_languages(value)
        if _has_profile_value(value):
            merged[key] = value
    return merged


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:]) if len(parts) > 1 else ""


def _apply_user_fallbacks(profile: dict[str, Any], user_profile: dict[str, Any]) -> None:
    if not user_profile:
        return

    if not profile.get("email") and user_profile.get("email"):
        profile["email"] = str(user_profile["email"]).strip()
    if not profile.get("phone") and user_profile.get("phone"):
        profile["phone"] = str(user_profile["phone"]).strip()
    if not profile.get("linkedin_url") and user_profile.get("linkedin_url"):
        profile["linkedin_url"] = str(user_profile["linkedin_url"]).strip()
    if not profile.get("website_url") and user_profile.get("portfolio_url"):
        profile["website_url"] = str(user_profile["portfolio_url"]).strip()
    if (not profile.get("skills")) and isinstance(user_profile.get("skills"), list):
        profile["skills"] = user_profile["skills"]

    if (not profile.get("first_name") or not profile.get("last_name")) and user_profile.get("name"):
        first_name, last_name = _split_name(str(user_profile["name"]))
        if not profile.get("first_name"):
            profile["first_name"] = first_name
        if not profile.get("last_name"):
            profile["last_name"] = last_name
        if not profile.get("full_name"):
            profile["full_name"] = str(user_profile["name"]).strip()

    raw_address = profile.get("address")
    address_data = raw_address if isinstance(raw_address, dict) else {}
    parsed_location = _parse_location(
        _text_value(user_profile.get("location")),
        PROFILE_DEFAULTS["address"],
    )
    if isinstance(address_data, dict):
        merged_address = dict(address_data)
        for key in ("street", "city", "state", "zip", "country"):
            if not _has_profile_value(merged_address.get(key)) and _has_profile_value(parsed_location.get(key)):
                merged_address[key] = parsed_location.get(key)
        profile["address"] = merged_address

    if not profile.get("city") and parsed_location.get("city"):
        profile["city"] = parsed_location["city"]
    if not profile.get("state") and parsed_location.get("state"):
        profile["state"] = parsed_location["state"]
    if not profile.get("zip") and parsed_location.get("zip"):
        profile["zip"] = parsed_location["zip"]
        profile["postal_code"] = parsed_location["zip"]
    if not profile.get("country") and parsed_location.get("country"):
        profile["country"] = parsed_location["country"]

    # Merge education from users table when resume parsed_data is missing fields.
    # The resume parser often omits dates, minors, honors that the user filled in
    # the Desktop profile form (stored in users.education JSONB).
    user_edu = user_profile.get("education")
    profile_edu = profile.get("education")
    if isinstance(user_edu, list) and isinstance(profile_edu, list):
        for i, p_entry in enumerate(profile_edu):
            if not isinstance(p_entry, dict):
                continue
            # Match by school name
            p_school = (p_entry.get("school") or "").strip().lower()
            if not p_school:
                continue
            for u_entry in user_edu:
                if not isinstance(u_entry, dict):
                    continue
                u_school = (u_entry.get("school") or "").strip().lower()
                if u_school and u_school in p_school or p_school in u_school:
                    # Backfill missing fields from user table entry
                    for key in ("start_date", "startDate", "end_date", "endDate",
                                "gpa", "minor", "minors", "honors", "field_of_study",
                                "fieldOfStudy", "degree_type", "degreeType"):
                        if not _has_profile_value(p_entry.get(key)) and _has_profile_value(u_entry.get(key)):
                            p_entry[key] = u_entry[key]
                    break


def _set_if_present(profile: dict[str, Any], key: str, value: Any) -> None:
    if _has_profile_value(value):
        profile[key] = value


def _apply_application_profile_overrides(
    profile: dict[str, Any],
    application_profile: dict[str, Any] | None,
) -> None:
    if not application_profile:
        return

    address_value = profile.get("address")
    address_data = address_value if isinstance(address_value, dict) else {}
    if isinstance(address_data, dict):
        merged_address = dict(address_data)
        if _has_profile_value(application_profile.get("address")):
            merged_address["street"] = str(application_profile["address"]).strip()
        if _has_profile_value(application_profile.get("city")):
            merged_address["city"] = str(application_profile["city"]).strip()
        if _has_profile_value(application_profile.get("state")):
            merged_address["state"] = str(application_profile["state"]).strip()
        if _has_profile_value(application_profile.get("zip_code")):
            merged_address["zip"] = str(application_profile["zip_code"]).strip()
        if _has_profile_value(application_profile.get("county")):
            merged_address["county"] = str(application_profile["county"]).strip()
        if _has_profile_value(application_profile.get("country_of_residence")):
            merged_address["country"] = str(application_profile["country_of_residence"]).strip()
        profile["address"] = merged_address

    _set_if_present(profile, "city", application_profile.get("city"))
    _set_if_present(profile, "state", application_profile.get("state"))
    if _has_profile_value(application_profile.get("zip_code")):
        zip_code = str(application_profile["zip_code"]).strip()
        profile["zip"] = zip_code
        profile["postal_code"] = zip_code
    _set_if_present(profile, "county", application_profile.get("county"))
    _set_if_present(profile, "country", application_profile.get("country_of_residence"))
    _set_if_present(profile, "country_of_residence", application_profile.get("country_of_residence"))

    # Helper: DB column names (asyncpg) are snake_case like authorized_to_work_in_us,
    # but legacy code and API paths may use work_authorization. Read both.
    def _ap(primary: str, *fallbacks: str) -> Any:
        v = application_profile.get(primary)
        if _has_profile_value(v):
            return v
        for fb in fallbacks:
            v = application_profile.get(fb)
            if _has_profile_value(v):
                return v
        return None

    scalar_overrides = {
        "work_authorization": _ap("work_authorization", "authorized_to_work_in_us"),
        "authorized_to_work_in_us": _ap("authorized_to_work_in_us", "work_authorization"),
        "visa_sponsorship": _ap("visa_sponsorship", "needs_visa_sponsorship"),
        "needs_visa_sponsorship": _ap("needs_visa_sponsorship", "visa_sponsorship"),
        "gender": _ap("eeo_gender", "gender"),
        "race_ethnicity": _ap("eeo_ethnicity", "race_ethnicity"),
        "veteran_status": _ap("eeo_veteran", "veteran_status"),
        "disability_status": _ap("eeo_disability", "disability_status"),
        "sexual_orientation": _ap("sexual_orientation", "eeo_lgbtq"),
        "citizenship_status": _ap("citizenship_status"),
        "citizenship_country": _ap("citizenship_country"),
        "visa_type": _ap("visa_type"),
        "us_citizen": _ap("us_citizen"),
        "export_control_eligible": _ap("export_control_eligible"),
        "salary_expectation": application_profile.get("salary_expectation"),
        "spoken_languages": application_profile.get("spoken_languages"),
        "english_proficiency": application_profile.get("english_proficiency"),
        "preferred_work_mode": application_profile.get("preferred_work_mode"),
        "preferred_locations": application_profile.get("preferred_locations"),
        "willing_to_relocate": application_profile.get("willing_to_relocate"),
        "how_did_you_hear": application_profile.get("how_did_you_hear"),
        "availability_window": application_profile.get("availability_window"),
        "notice_period": application_profile.get("notice_period"),
        "preferred_name": application_profile.get("preferred_name"),
        "current_school_year": application_profile.get("current_school_year"),
        "graduation_date": application_profile.get("graduation_date"),
        "degree_seeking": application_profile.get("degree_seeking"),
        "certifications_licenses": application_profile.get("certifications_licenses"),
        "secondary_email": application_profile.get("secondary_email"),
        "sponsor_type": application_profile.get("sponsor_type"),
        "transgender": application_profile.get("transgender"),
        "sexual_orientation": application_profile.get("sexual_orientation"),
        "pronouns": application_profile.get("pronouns"),
        "dual_citizenship": application_profile.get("dual_citizenship"),
        "dual_citizenship_country": application_profile.get("dual_citizenship_country"),
        "birthday": application_profile.get("birthday"),
    }
    for key, value in scalar_overrides.items():
        _set_if_present(profile, key, value)

    normalized_languages = _normalize_application_profile_languages(application_profile.get("languages"))
    if _has_profile_value(normalized_languages):
        profile["languages"] = normalized_languages
