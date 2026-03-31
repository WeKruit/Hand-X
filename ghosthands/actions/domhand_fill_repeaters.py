"""DomHand Fill Repeaters — orchestrate multi-entry repeater sections end-to-end.

A single action call that reads the user profile, determines how many entries are
needed, clicks Add for each missing entry, fills the inline form, and clicks Save.
Eliminates the need for the LLM planner to orchestrate each entry individually.

Supported sections: Education, Work Experience, Skills, Languages, Licenses.
"""

import asyncio
import json
import structlog
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

logger = structlog.get_logger(__name__)

_SECTION_ALIASES: dict[str, str] = {
    "education": "education",
    "college / university": "education",
    "college/university": "education",
    "work experience": "experience",
    "experience": "experience",
    "skills": "skills",
    "skill": "skills",
    "technical skills": "skills",
    "languages": "languages",
    "language": "languages",
    "language skills": "languages",
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
    "skills": ["Technical Skills", "Skill", "Skills"],
    "languages": ["Language Skills", "Language", "Languages"],
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

_CLEAR_COMBOBOX_TEXT_JS = r"""
() => {
    var combos = document.querySelectorAll('[role="combobox"] input, input[aria-autocomplete]');
    for (var i = 0; i < combos.length; i++) {
        var el = combos[i];
        var s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') continue;
        if (el.value && el.value.trim()) {
            el.focus();
            el.select();
            document.execCommand('delete');
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            return JSON.stringify({cleared: true, field: el.getAttribute('id') || ''});
        }
    }
    return JSON.stringify({cleared: false});
}
"""

_FIND_CANCEL_BUTTON_JS = r"""
() => {
    var btns = document.querySelectorAll('button, [role="button"]');
    for (var i = 0; i < btns.length; i++) {
        var text = (btns[i].textContent || '').trim().toLowerCase();
        var s = getComputedStyle(btns[i]);
        if (s.display === 'none' || s.visibility === 'hidden') continue;
        if (text === 'cancel' || text === 'discard' || text === 'remove') {
            var rect = btns[i].getBoundingClientRect();
            return JSON.stringify({
                found: true,
                text: btns[i].textContent.trim(),
                x: Math.round(rect.left + rect.width / 2),
                y: Math.round(rect.top + rect.height / 2)
            });
        }
    }
    return JSON.stringify({found: false});
}
"""

_FIND_SAVE_BUTTON_JS = r"""
(sectionHint) => {
    var hint = (sectionHint || '').toLowerCase();
    var diag = {hint: hint, phase: '', visible_buttons: []};

    // Collect all visible button texts for diagnostics
    var allPageBtns = document.querySelectorAll('button, [role="button"]');
    for (var b = 0; b < allPageBtns.length && diag.visible_buttons.length < 20; b++) {
        var bs = getComputedStyle(allPageBtns[b]);
        if (bs.display !== 'none' && bs.visibility !== 'hidden') {
            var bt = (allPageBtns[b].textContent || '').trim().slice(0, 40);
            if (bt) diag.visible_buttons.push(bt);
        }
    }

    function btnRect(btn, phase, extra) {
        var rect = btn.getBoundingClientRect();
        return JSON.stringify(Object.assign({
            found: true,
            text: btn.textContent.trim(),
            x: Math.round(rect.left + rect.width / 2),
            y: Math.round(rect.top + rect.height / 2),
            phase: phase,
            diag: diag
        }, extra || {}));
    }

    // ── Phase 1: .profile-inline-form save (Greenhouse, Lever, etc.) ──
    const selectors = [
        '.profile-inline-form .profile-inline-form__save',
        '.profile-inline-form button[type="submit"]',
        '.profile-inline-form .btn-save',
        '.profile-inline-form button.save',
    ];
    for (const sel of selectors) {
        const btn = document.querySelector(sel);
        if (btn) return btnRect(btn, 'profile_inline_form');
    }
    const allBtns = document.querySelectorAll('.profile-inline-form button');
    for (const btn of allBtns) {
        const text = (btn.textContent || '').trim().toLowerCase();
        if (text === 'save' || text === 'ok' || text === 'done' || text === 'commit' || text.startsWith('save')) {
            return btnRect(btn, 'profile_inline_form_text');
        }
    }

    // ── Phase 2: Oracle section-specific commit button ──
    var sectionCommitPattern = null;
    if (hint === 'skills')     sectionCommitPattern = /^add\s+skill$/i;
    if (hint === 'languages')  sectionCommitPattern = /^add\s+language$/i;
    if (hint === 'licenses')   sectionCommitPattern = /^add\s+(certification|license)$/i;
    if (hint === 'education')  sectionCommitPattern = /^add\s+education$/i;
    if (hint === 'experience') sectionCommitPattern = /^add\s+(work\s+)?experience$/i;

    var preferSave = (hint === 'education' || hint === 'experience');

    var pageBtns = document.querySelectorAll('button, [role="button"]');

    if (sectionCommitPattern) {
        for (const btn of pageBtns) {
            const text = (btn.textContent || '').trim();
            if (sectionCommitPattern.test(text)) {
                var s = getComputedStyle(btn);
                if (s.display !== 'none' && s.visibility !== 'hidden') {
                    return btnRect(btn, 'oracle_section_commit', {oracle_commit: true});
                }
            }
        }
    }

    if (preferSave) {
        for (const btn of pageBtns) {
            const text = (btn.textContent || '').trim().toLowerCase();
            if (text === 'save' || text === 'ok' || text === 'done') {
                var s = getComputedStyle(btn);
                if (s.display !== 'none' && s.visibility !== 'hidden') {
                    return btnRect(btn, 'oracle_save');
                }
            }
        }
    }

    diag.phase = 'none_found';
    return JSON.stringify({found: false, reason: 'no_save_button_found', diag: diag});
}
"""


async def _cdp_click_cancel(page) -> bool:
    """Find cancel button via JS, click via CDP mouse for proper Oracle handling."""
    try:
        raw = await page.evaluate(_FIND_CANCEL_BUTTON_JS)
        info = json.loads(raw) if isinstance(raw, str) else raw
        if info.get("found"):
            await page.mouse.click(info["x"], info["y"])
            return True
    except Exception:
        pass
    return False


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
            seen: set[str] = set()
            for s in skills:
                name = ""
                if isinstance(s, str):
                    name = s.strip()
                elif isinstance(s, dict):
                    name = (s.get("skill_name") or s.get("skill") or s.get("name") or "").strip()
                if not name:
                    continue
                key = name.casefold()
                if key in seen:
                    continue
                seen.add(key)
                entries.append({"skill_name": name})
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

    # Cap skills to avoid timeout — Oracle combobox fill is slow per entry
    _MAX_SKILL_ENTRIES = 10
    if canonical == "skills" and len(entries) > _MAX_SKILL_ENTRIES:
        logger.info(
            "domhand.fill_repeaters.skills_capped",
            original_count=len(entries),
            capped_to=_MAX_SKILL_ENTRIES,
        )
        entries = entries[:_MAX_SKILL_ENTRIES]

    expand_names = _EXPAND_SECTION_NAMES.get(canonical, [params.section])
    results: list[str] = []
    committed_labels: list[str] = []
    filled_count = 0
    failed_entries: list[str] = []
    skills_taxonomy_miss_labels: list[str] = []
    consecutive_skill_misses = 0
    _MAX_CONSECUTIVE_SKILL_MISSES = 3
    consecutive_same_value_count = 0

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
                strict_scope=True,
            ),
            browser_session,
        )

        fill_meta = (fill_result.metadata or {}).get("domhand_fill_json", {})
        entry_filled = fill_meta.get("filled_count", 0) if isinstance(fill_meta, dict) else 0
        entry_failed = fill_meta.get("dom_failure_count", 0) if isinstance(fill_meta, dict) else 0

        # Detect stuck loop: if fill found nothing new (all already_filled),
        # the entry_data isn't reaching the combobox.  Break to avoid looping forever.
        already_filled = fill_meta.get("already_filled_count", 0) if isinstance(fill_meta, dict) else 0
        if entry_filled == 0 and entry_failed == 0 and already_filled > 0:
            consecutive_same_value_count += 1
            logger.warning(
                "domhand.fill_repeaters.stuck_no_new_fields",
                section=canonical,
                entry_index=i,
                entry_label=entry_label,
                already_filled=already_filled,
                consecutive=consecutive_same_value_count,
            )
            if consecutive_same_value_count >= 2:
                logger.warning(
                    "domhand.fill_repeaters.breaking_stuck_loop",
                    section=canonical,
                    entry_index=i,
                    reason="consecutive entries filled nothing new — combobox not accepting entry_data",
                )
                break
            continue
        else:
            consecutive_same_value_count = 0

        # Skills: check if the Skill combobox value was actually committed.
        # If domhand_fill reported 0 new fills and the Skill field is in the
        # failed or already_filled set, the combobox didn't accept this skill.
        if canonical == "skills" and entry_filled == 0 and already_filled >= 1:
            skill_name = entry.get("skill_name", "") if isinstance(entry, dict) else str(entry)
            logger.info(
                "domhand.fill_repeaters.skill_not_committed",
                section=canonical,
                entry_index=i,
                skill_name=skill_name[:40],
                entry_filled=entry_filled,
                already_filled=already_filled,
            )
            skills_taxonomy_miss_labels.append(skill_name)
            consecutive_skill_misses += 1
            if consecutive_skill_misses >= _MAX_CONSECUTIVE_SKILL_MISSES:
                logger.info(
                    "domhand.fill_repeaters.skill_early_termination",
                    section=canonical,
                    entry_index=i,
                    consecutive_misses=consecutive_skill_misses,
                    reason="employer taxonomy likely doesn't have remaining profile skills",
                )
                # Clean up: clear stale combobox text and cancel the open form
                # so the agent sees a clean state (no open skill editor).
                try:
                    await page.evaluate(_CLEAR_COMBOBOX_TEXT_JS)
                    await asyncio.sleep(0.3)
                    await _cdp_click_cancel(page)
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
                break
            # Clear the Skill combobox text so the form stays open
            # (Skill Type already filled). Next iteration reuses the same form.
            try:
                await page.evaluate(_CLEAR_COMBOBOX_TEXT_JS)
                await asyncio.sleep(0.3)
            except Exception:
                pass
            continue

        # Do NOT save/commit when fields failed — entry is incomplete
        if entry_failed > 0:
            failed_entries.append(
                f"{entry_label}: {entry_failed} field(s) failed to fill, skipping save"
            )
            logger.warning(
                "domhand.fill_repeaters.skip_save_due_to_failures",
                section=canonical,
                entry_index=i,
                filled=entry_filled,
                failed=entry_failed,
            )
            try:
                await _cdp_click_cancel(page)
                await asyncio.sleep(0.5)
            except Exception:
                pass
            continue

        try:
            # Find button position via JS, then CDP mouse click for proper
            # Oracle event handler firing (JS btn.click() doesn't commit).
            find_raw = await page.evaluate(_FIND_SAVE_BUTTON_JS, canonical)
            save_result = json.loads(find_raw) if isinstance(find_raw, str) else find_raw
            diag = save_result.get("diag", {})
            if save_result.get("found"):
                bx, by = save_result["x"], save_result["y"]
                await page.mouse.click(bx, by)
                logger.info(
                    "domhand.fill_repeaters.commit_clicked",
                    section=canonical,
                    entry_index=i,
                    button_text=save_result.get("text", ""),
                    oracle_commit=save_result.get("oracle_commit", False),
                    phase=diag.get("phase", ""),
                    click_coords=f"{bx},{by}",
                    visible_buttons=diag.get("visible_buttons", [])[:10],
                )
                await asyncio.sleep(0.8)
            else:
                logger.warning(
                    "domhand.fill_repeaters.no_commit_button",
                    section=canonical,
                    entry_index=i,
                    reason=save_result.get("reason", ""),
                    phase=diag.get("phase", ""),
                    visible_buttons=diag.get("visible_buttons", [])[:10],
                )
        except Exception:
            pass

        if entry_filled > 0 or not fill_result.error:
            filled_count += 1
            consecutive_skill_misses = 0  # reset on success
            results.append(f"{entry_label}: {entry_filled} fields filled")
            # Track short name for the summary message
            if canonical == "skills":
                committed_labels.append(entry.get("skill_name", "") if isinstance(entry, dict) else str(entry))
            else:
                committed_labels.append(entry_label)
        else:
            failed_entries.append(f"{entry_label}: fill failed ({fill_result.error or 'unknown'})")

    # Build concise summary — keep it short to avoid context bloat for the LLM.
    summary_parts = [f"{params.section}: {filled_count}/{needed} entries filled."]
    if committed_labels:
        summary_parts.append(f"Committed: {', '.join(committed_labels)}")
    if skills_taxonomy_miss_labels:
        summary_parts.append(
            f"Skipped (not in employer taxonomy): {', '.join(skills_taxonomy_miss_labels)}"
        )
    if failed_entries:
        summary_parts.append(f"Failed: {'; '.join(failed_entries)}")
    summary_parts.append(
        f"{params.section} section is COMPLETE — do NOT call domhand_fill on this section again. "
        f"If other repeater sections remain (Languages, Licenses, etc.), "
        f"call domhand_fill_repeaters for each before clicking Next."
    )

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
