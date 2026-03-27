"""DomHand Fill Repeaters — orchestrate multi-entry repeater sections end-to-end.

A single action call that reads the user profile, determines how many entries are
needed, clicks Add for each missing entry, fills the inline form, and clicks Save.
Eliminates the need for the LLM planner to orchestrate each entry individually.

Supported sections: Education, Work Experience, Skills, Languages, Licenses.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession

from ghosthands.actions.views import (
    DomHandExpandParams,
    DomHandFillParams,
    DomHandFillRepeatersParams,
    normalize_name,
)

logger = logging.getLogger(__name__)

_SECTION_ALIASES: dict[str, str] = {
    "education": "education",
    "college / university": "education",
    "college/university": "education",
    "work experience": "experience",
    "experience": "experience",
    "skills": "skills",
    "skill": "skills",
    "languages": "languages",
    "language": "languages",
    "licenses": "licenses",
    "license": "licenses",
    "licenses & certifications": "licenses",
    "certifications": "licenses",
}

_PROFILE_KEY_MAP: dict[str, str] = {
    "education": "education",
    "experience": "experience",
    "skills": "skills",
    "languages": "languages",
    "licenses": "certifications",
}

_EXPAND_SECTION_NAMES: dict[str, list[str]] = {
    "education": ["Education", "education", "College / University"],
    "experience": ["Work Experience", "Experience", "experience", "work experience"],
    "skills": ["Skills", "Skill", "skills"],
    "languages": ["Languages", "Language", "languages"],
    "licenses": ["Licenses", "License", "licenses", "Licenses & Certifications"],
}

_COUNT_SAVED_TILES_JS = r"""
(profileType) => {
    const tiles = document.querySelectorAll(
        `.apply-flow-profile-item-tile--saved[data-profile-type="${profileType}"],` +
        `.apply-flow-profile-item-tile--saved[data-profile-type="${profileType}s"]`
    );
    let count = 0;
    for (const t of tiles) {
        const style = window.getComputedStyle(t);
        if (style.display !== 'none' && style.visibility !== 'hidden') count++;
    }
    return count;
}
"""

_COUNT_OPEN_INLINE_FORMS_JS = r"""
() => {
    const forms = document.querySelectorAll('.profile-inline-form');
    let count = 0;
    for (const f of forms) {
        const style = window.getComputedStyle(f);
        if (style.display !== 'none' && style.visibility !== 'hidden') count++;
    }
    return count;
}
"""

_CLICK_SAVE_BUTTON_JS = r"""
() => {
    const selectors = [
        '.profile-inline-form .profile-inline-form__save',
        '.profile-inline-form button[type="submit"]',
        '.profile-inline-form .btn-save',
        '.profile-inline-form button.save',
    ];
    for (const sel of selectors) {
        const btn = document.querySelector(sel);
        if (btn) {
            btn.click();
            return JSON.stringify({clicked: true, text: btn.textContent.trim()});
        }
    }
    const allBtns = document.querySelectorAll('.profile-inline-form button');
    for (const btn of allBtns) {
        const text = (btn.textContent || '').trim().toLowerCase();
        if (text === 'save' || text === 'ok' || text === 'done' || text === 'commit' || text.startsWith('save')) {
            btn.click();
            return JSON.stringify({clicked: true, text: btn.textContent.trim()});
        }
    }
    return JSON.stringify({clicked: false, reason: 'no_save_button_found'});
}
"""


def _normalize_section(section: str) -> str:
    """Normalize section name to canonical key."""
    key = normalize_name(section).strip().lower()
    return _SECTION_ALIASES.get(key, key)


def _get_profile_data() -> dict[str, Any]:
    """Load profile data from env vars."""
    from ghosthands.bridge.profile_adapter import camel_to_snake_profile

    path = os.environ.get("GH_USER_PROFILE_PATH", "")
    if path:
        try:
            import pathlib

            p = pathlib.Path(path)
            if p.is_file():
                parsed = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    return camel_to_snake_profile(parsed)
        except Exception as e:
            logger.warning(f"Failed to parse profile JSON from {path}: {e}")

    raw_json = os.environ.get("GH_USER_PROFILE_JSON", "")
    if raw_json.strip():
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                return camel_to_snake_profile(parsed)
        except Exception:
            pass
    return {}


def _get_entries_for_section(
    profile_data: dict[str, Any],
    canonical_section: str,
    max_entries: int | None,
) -> list[dict[str, Any]]:
    """Extract repeater entries from profile data for a section."""
    profile_key = _PROFILE_KEY_MAP.get(canonical_section, canonical_section)

    if canonical_section == "skills":
        skills = profile_data.get("skills", [])
        if isinstance(skills, list):
            entries = []
            for s in skills:
                if isinstance(s, str) and s.strip():
                    entries.append({"skill_name": s.strip()})
                elif isinstance(s, dict):
                    entries.append(s)
            if max_entries:
                entries = entries[:max_entries]
            return entries
        return []

    if canonical_section == "licenses":
        for key in ("certifications", "licenses", "certifications_licenses", "licenses_certifications"):
            val = profile_data.get(key)
            if isinstance(val, list):
                entries = [e for e in val if isinstance(e, dict) and e]
                if max_entries:
                    entries = entries[:max_entries]
                return entries
            if isinstance(val, str):
                parts = [p.strip() for p in re.split(r"[,;\n]+", val) if p.strip()]
                entries = [{"license_name": p} for p in parts]
                if max_entries:
                    entries = entries[:max_entries]
                return entries
        return []

    raw = profile_data.get(profile_key, [])
    if not isinstance(raw, list):
        return []
    entries = [e for e in raw if isinstance(e, dict) and e]
    if max_entries:
        entries = entries[:max_entries]
    return entries


async def domhand_fill_repeaters(
    params: DomHandFillRepeatersParams,
    browser_session: BrowserSession,
) -> ActionResult:
    """Fill all repeater entries for a section using profile data.

    Orchestrates the full loop: reads profile -> clicks Add N times ->
    fills each entry inline -> saves each entry. One action call replaces
    N * (expand + fill + save) agent planner steps.
    """
    from ghosthands.actions.domhand_expand import domhand_expand
    from ghosthands.actions.domhand_fill import domhand_fill

    page = await browser_session.get_current_page()
    if not page:
        return ActionResult(error="No active page found in browser session")

    canonical = _normalize_section(params.section)
    if canonical not in _PROFILE_KEY_MAP:
        return ActionResult(
            error=f"Unknown repeater section: {params.section!r}. "
            f"Supported: Education, Work Experience, Skills, Languages, Licenses."
        )

    profile_data = _get_profile_data()
    if not profile_data:
        return ActionResult(
            error="No user profile data found. Set GH_USER_PROFILE_PATH or GH_USER_PROFILE_JSON."
        )

    entries = _get_entries_for_section(profile_data, canonical, params.max_entries)
    if not entries:
        return ActionResult(
            extracted_content=f"No {params.section} entries found in user profile. Section skipped."
        )

    profile_type_map: dict[str, str] = {
        "education": "education",
        "experience": "experience",
        "skills": "skill",
        "languages": "language",
        "licenses": "license",
    }
    profile_type = profile_type_map.get(canonical, canonical)

    try:
        existing_count = await page.evaluate(_COUNT_SAVED_TILES_JS, profile_type)
    except Exception:
        existing_count = 0
    existing_count = int(existing_count or 0)

    needed = len(entries) - existing_count
    if needed <= 0:
        return ActionResult(
            extracted_content=(
                f"DomHand: {params.section} — all {len(entries)} entries already exist "
                f"({existing_count} saved tiles)."
            ),
            metadata={
                "tool": "domhand_fill_repeaters",
                "section": canonical,
                "entries_total": len(entries),
                "existing_count": existing_count,
                "entries_filled": 0,
                "all_present": True,
            },
        )

    expand_names = _EXPAND_SECTION_NAMES.get(canonical, [params.section])
    results: list[str] = []
    filled_count = 0
    failed_entries: list[str] = []

    for i in range(existing_count, len(entries)):
        entry = entries[i]
        entry_label = _entry_summary(canonical, entry, i + 1)

        try:
            open_forms = await page.evaluate(_COUNT_OPEN_INLINE_FORMS_JS)
        except Exception:
            open_forms = 0

        if int(open_forms or 0) == 0:
            expand_success = False
            for name in expand_names:
                expand_result = await domhand_expand(
                    DomHandExpandParams(section=name),
                    browser_session,
                )
                if expand_result.error and "already open" in (expand_result.error or "").lower():
                    expand_success = True
                    break
                if not expand_result.error:
                    expand_success = True
                    break

            if not expand_success:
                failed_entries.append(f"{entry_label}: could not click Add button")
                logger.warning(
                    "domhand.fill_repeaters.expand_failed",
                    section=canonical,
                    entry_index=i,
                )
                continue

            await asyncio.sleep(0.5)

        fill_result = await domhand_fill(
            DomHandFillParams(
                heading_boundary=None,
                target_section=params.section,
                entry_data=entry,
            ),
            browser_session,
        )

        fill_meta = (fill_result.metadata or {}).get("domhand_fill_json", {})
        entry_filled = fill_meta.get("filled_count", 0) if isinstance(fill_meta, dict) else 0

        try:
            save_raw = await page.evaluate(_CLICK_SAVE_BUTTON_JS)
            save_result = json.loads(save_raw) if isinstance(save_raw, str) else save_raw
            if save_result.get("clicked"):
                await asyncio.sleep(0.8)
        except Exception:
            pass

        if entry_filled > 0 or not fill_result.error:
            filled_count += 1
            results.append(f"{entry_label}: {entry_filled} fields filled")
        else:
            failed_entries.append(f"{entry_label}: fill failed ({fill_result.error or 'unknown'})")

    summary_parts = [
        f"{params.section}: {filled_count}/{needed} entries filled "
        f"(profile has {len(entries)}, {existing_count} already existed)."
    ]
    if results:
        summary_parts.append("Filled entries:")
        summary_parts.extend(f"  - {r}" for r in results)
    if failed_entries:
        summary_parts.append(f"Failed entries: {len(failed_entries)}.")
        summary_parts.extend(f"  - {f}" for f in failed_entries)
    if not failed_entries:
        summary_parts.append("All entries filled successfully.")

    return ActionResult(
        extracted_content="\n".join(summary_parts),
        include_extracted_content_only_once=True,
        metadata={
            "tool": "domhand_fill_repeaters",
            "section": canonical,
            "entries_needed": needed,
            "entries_filled": filled_count,
            "entries_failed": len(failed_entries),
            "entries_total": len(entries),
            "existing_count": existing_count,
        },
    )


def _entry_summary(canonical: str, entry: dict[str, Any], index: int) -> str:
    """Human-readable summary of an entry for logging."""
    match canonical:
        case "education":
            school = entry.get("school", entry.get("school_university", ""))
            degree = entry.get("degree", "")
            return f"Education #{index}: {degree} @ {school}" if school else f"Education #{index}"
        case "experience":
            title = entry.get("job_title", entry.get("title", ""))
            company = entry.get("company", "")
            return f"Experience #{index}: {title} @ {company}" if company else f"Experience #{index}"
        case "skills":
            name = entry.get("skill_name", entry.get("name", str(entry)))
            return f"Skill #{index}: {name}"
        case "languages":
            lang = entry.get("language", entry.get("language_name", ""))
            return f"Language #{index}: {lang}" if lang else f"Language #{index}"
        case "licenses":
            name = entry.get("license_name", entry.get("name", ""))
            return f"License #{index}: {name}" if name else f"License #{index}"
        case _:
            return f"{canonical} #{index}"
