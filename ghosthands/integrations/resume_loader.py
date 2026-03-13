"""Resume profile loading — normalizes VALET's parsed resume data for form filling.

Mirrors ResumeProfileLoader from GH's TypeScript codebase, producing a flat
profile dictionary that DomHand uses to match form fields to user data.
"""

from __future__ import annotations

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


def load_resume_from_file(path: str) -> dict[str, Any]:
    """Load a resume profile from a JSON file (for testing/development).

    The file should contain either:
    - A ``parsed_data`` dict matching VALET's camelCase format, or
    - A pre-normalized profile dict (will be returned as-is).

    Args:
            path: Path to a JSON file.

    Returns:
            Normalized profile dictionary.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    # If the file contains VALET-format parsed_data, normalize it
    if "fullName" in data or "workHistory" in data or "education" in data:
        return _map_to_profile(data)

    # If it looks like it wraps parsed_data
    if "parsed_data" in data:
        return _map_to_profile(data["parsed_data"])

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
                "school": edu.get("school", ""),
                "degree": edu.get("degree", ""),
                "field_of_study": edu.get("fieldOfStudy") or "",
                "gpa": edu.get("gpa"),
                "start_date": edu.get("startDate") or "",
                "end_date": edu.get("endDate") or edu.get("expectedGraduation") or "",
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
