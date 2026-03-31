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
from dataclasses import dataclass
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

_ANCHOR_LABELS: dict[str, list[str]] = {
    "experience": ["company", "employer", "organization", "organisation"],
    "education": ["school", "institution", "university", "college"],
    "languages": ["language"],
    "skills": ["skill", "skill name"],
    "licenses": ["certification", "license", "credential"],
}

_PROFILE_ANCHOR_KEYS: dict[str, list[str]] = {
    "experience": ["company", "employer", "organization", "company_name"],
    "education": ["school", "institution", "university", "school_name"],
    "languages": ["language", "language_name"],
    "skills": ["skill_name", "skill", "name"],
    "licenses": ["certification_name", "license_name", "credential_name", "name"],
}


@dataclass
class ObservationResult:
    """Result of observing existing repeater entries on the page."""

    existing_count: int
    matched_profile_indices: list[int]
    unmatched_entries: list[dict[str, Any]]
    page_anchor_values: list[str]


_COUNT_SAVED_TILES_JS = r"""
(profileType) => {
    // ── Greenhouse/Lever: saved tile badges ──
    var tiles = document.querySelectorAll(
        '.apply-flow-profile-item-tile--saved[data-profile-type="' + profileType + '"],' +
        '.apply-flow-profile-item-tile--saved[data-profile-type="' + profileType + 's"]'
    );
    var count = 0;
    for (var i = 0; i < tiles.length; i++) {
        var s = window.getComputedStyle(tiles[i]);
        if (s.display !== 'none' && s.visibility !== 'hidden') count++;
    }
    if (count > 0) return count;

    // ── Workday: count numbered section headings ──
    // Workday pre-fills repeater entries with headings like
    // "Work Experience 1", "Education 1", "Language 1".
    // Map profileType to the heading prefix Workday uses.
    var headingMap = {
        'experience': 'Work Experience',
        'education': 'Education',
        'language': 'Language',
        'skill': 'Skills',
        'license': 'Certification'
    };
    var prefix = headingMap[profileType];
    if (prefix) {
        var pattern = new RegExp('^' + prefix + '\\s+\\d+$', 'i');
        var headings = document.querySelectorAll('h2, h3, h4, [data-automation-id*="sectionHeader"], legend');
        for (var j = 0; j < headings.length; j++) {
            var text = (headings[j].textContent || '').trim();
            if (pattern.test(text)) count++;
        }
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
            btns[i].setAttribute('data-dh-cancel-target', 'true');
            btns[i].scrollIntoView({block: 'center', behavior: 'instant'});
            return JSON.stringify({found: true, text: btns[i].textContent.trim()});
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

    function tagAndReturn(btn, phase, extra) {
        // Tag the button for Playwright locator click (handles Oracle JET
        // display:contents custom elements where getBoundingClientRect=0).
        // Same pattern as domhand_expand's tagAndReturn.
        btn.setAttribute('data-dh-commit-target', 'true');
        btn.scrollIntoView({block: 'center', behavior: 'instant'});
        return JSON.stringify(Object.assign({
            found: true,
            text: btn.textContent.trim(),
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
        if (btn) return tagAndReturn(btn, 'profile_inline_form');
    }
    const allBtns = document.querySelectorAll('.profile-inline-form button');
    for (const btn of allBtns) {
        const text = (btn.textContent || '').trim().toLowerCase();
        if (text === 'save' || text === 'ok' || text === 'done' || text === 'commit' || text.startsWith('save')) {
            return tagAndReturn(btn, 'profile_inline_form_text');
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
                    return tagAndReturn(btn, 'oracle_section_commit', {oracle_commit: true});
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
                    return tagAndReturn(btn, 'oracle_save');
                }
            }
        }
    }

    diag.phase = 'none_found';
    return JSON.stringify({found: false, reason: 'no_save_button_found', diag: diag});
}
"""


async def _cdp_click_cancel(page) -> bool:
    """Find cancel button via JS tag, click via Playwright locator.

    Same pattern as commit button — handles Oracle JET display:contents.
    """
    try:
        raw = await page.evaluate(_FIND_CANCEL_BUTTON_JS)
        info = json.loads(raw) if isinstance(raw, str) else raw
        if not info.get("found"):
            return False
        try:
            btn = page.locator('[data-dh-cancel-target="true"]')
            await btn.click(timeout=3000)
        except Exception:
            # Fallback: JS click
            await page.evaluate("""() => {
                var el = document.querySelector('[data-dh-cancel-target="true"]');
                if (el) el.click();
            }""")
        with contextlib.suppress(Exception):
            await page.evaluate("""() => {
                var el = document.querySelector('[data-dh-cancel-target]');
                if (el) el.removeAttribute('data-dh-cancel-target');
            }""")
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


def _extract_profile_anchor_value(canonical_section: str, entry: dict[str, Any]) -> str:
    """Extract the anchor value from a profile entry dict.

    Tries keys from _PROFILE_ANCHOR_KEYS in order, returns the first
    non-empty value after normalize_name. Returns "" if no anchor found.
    """
    keys = _PROFILE_ANCHOR_KEYS.get(canonical_section, [])
    for key in keys:
        raw = entry.get(key)
        if raw and isinstance(raw, str):
            normalized = normalize_name(raw.strip())
            if normalized:
                return normalized
    return ""


def _is_anchor_field(field_name: str, canonical_section: str) -> bool:
    """Check if a field name matches any anchor label for the section.

    Uses containment check: normalize_name(field_name) must contain
    at least one anchor label substring from _ANCHOR_LABELS[section].
    """
    labels = _ANCHOR_LABELS.get(canonical_section, [])
    if not labels:
        return False
    normalized = normalize_name(field_name)
    if not normalized:
        return False
    return any(label in normalized for label in labels)


async def _observe_existing_entries(
    page: Any,
    canonical_section: str,
    profile_entries: list[dict[str, Any]],
) -> ObservationResult:
    """Observe existing repeater entries on the page by anchor field values.

    1. Calls extract_visible_form_fields(page) to get all visible fields.
    2. Filters to fields matching the target section via _section_matches_scope.
    3. Identifies anchor fields using _is_anchor_field.
    4. Keeps only anchor fields with effective values (_field_has_effective_value).
    5. Extracts and normalizes anchor values from the page.
    6. Matches profile entries against page anchors using normalize_name exact match.
    7. Returns ObservationResult with counts, matched indices, unmatched entries, and page values.

    NOTE: This phase uses normalize_name exact match only.
    LLM fuzzy matching is added in Phase 6.
    """
    from ghosthands.dom.fill_executor import extract_visible_form_fields, _field_has_effective_value
    from ghosthands.dom.fill_label_match import _section_matches_scope

    all_fields = await extract_visible_form_fields(page)

    # Step 1: Filter fields to target section
    section_fields = [
        f for f in all_fields
        if _section_matches_scope(f.section, canonical_section)
    ]

    # Step 2: Identify anchor fields with effective values
    page_anchor_values: list[str] = []
    for field in section_fields:
        if not _is_anchor_field(field.name, canonical_section):
            continue
        if not _field_has_effective_value(field):
            continue
        normalized_value = normalize_name(str(field.current_value).strip())
        if normalized_value:
            page_anchor_values.append(normalized_value)

    existing_count = len(page_anchor_values)

    # Step 3: Match profile entries against page anchors (exact match after normalization)
    matched_profile_indices: list[int] = []
    unmatched_entries: list[dict[str, Any]] = []

    for idx, entry in enumerate(profile_entries):
        profile_anchor = _extract_profile_anchor_value(canonical_section, entry)
        if profile_anchor and profile_anchor in page_anchor_values:
            matched_profile_indices.append(idx)
        else:
            unmatched_entries.append(entry)

    logger.info(
        "domhand.observe_existing_entries",
        section=canonical_section,
        total_fields=len(all_fields),
        section_fields=len(section_fields),
        anchor_fields_with_value=existing_count,
        page_anchors=page_anchor_values,
        matched_count=len(matched_profile_indices),
        unmatched_count=len(unmatched_entries),
    )

    return ObservationResult(
        existing_count=existing_count,
        matched_profile_indices=matched_profile_indices,
        unmatched_entries=unmatched_entries,
        page_anchor_values=page_anchor_values,
    )


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
    logger.info(
        "domhand.fill_repeaters.entries_check",
        section=canonical,
        entry_count=len(entries),
        profile_keys=sorted(profile_data.keys())[:15],
        raw_education_type=type(profile_data.get("education")).__name__,
        raw_education_len=len(profile_data.get("education") or []),
    )
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
        already_filled = fill_meta.get("already_filled_count", 0) if isinstance(fill_meta, dict) else 0

        # Guard: if domhand_fill found ZERO fields at all, the section is
        # either pre-populated (Workday resume auto-fill) or not expandable.
        # Stop immediately — clicking "Add" again just creates empty duplicates.
        if entry_filled == 0 and entry_failed == 0 and already_filled == 0:
            logger.info(
                "domhand.fill_repeaters.zero_fields_detected",
                section=canonical,
                entry_index=i,
                reason="section likely pre-filled or expand did not reveal fields",
            )
            break

        # Detect stuck loop: if fill found nothing new (all already_filled),
        # the entry_data isn't reaching the combobox.  Break to avoid looping forever.
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
            # JS tags the commit button → Playwright locator click.
            # Reuses domhand_expand pattern: handles Oracle JET display:contents
            # custom elements where getBoundingClientRect() returns zero.
            find_raw = await page.evaluate(_FIND_SAVE_BUTTON_JS, canonical)
            save_result = json.loads(find_raw) if isinstance(find_raw, str) else find_raw
            diag = save_result.get("diag", {})
            if save_result.get("found"):
                commit_clicked = False
                try:
                    btn = page.locator('[data-dh-commit-target="true"]')
                    await btn.click(timeout=3000)
                    commit_clicked = True
                except Exception:
                    # Fallback: JS click if Playwright can't reach it
                    try:
                        await page.evaluate("""() => {
                            var el = document.querySelector('[data-dh-commit-target="true"]');
                            if (el) el.click();
                        }""")
                        commit_clicked = True
                    except Exception:
                        pass
                # Clean up tag
                with contextlib.suppress(Exception):
                    await page.evaluate("""() => {
                        var el = document.querySelector('[data-dh-commit-target]');
                        if (el) el.removeAttribute('data-dh-commit-target');
                    }""")
                logger.info(
                    "domhand.fill_repeaters.commit_clicked",
                    section=canonical,
                    entry_index=i,
                    button_text=save_result.get("text", ""),
                    oracle_commit=save_result.get("oracle_commit", False),
                    phase=diag.get("phase", ""),
                    clicked=commit_clicked,
                )
                await asyncio.sleep(0.8)
            else:
                logger.warning(
                    "domhand.fill_repeaters.no_commit_button",
                    section=canonical,
                    entry_index=i,
                    reason=save_result.get("reason", ""),
                    phase=diag.get("phase", ""),
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

    # Persist "section COMPLETE" in agent memory so it survives across steps.
    # Without long_term_memory, include_extracted_content_only_once vanishes
    # after 1 step — the agent forgets and retries.
    _long_term = (
        f"{params.section} section COMPLETE ({filled_count} filled"
        f"{', ' + str(len(skills_taxonomy_miss_labels)) + ' skipped' if skills_taxonomy_miss_labels else ''}"
        f"). Do NOT re-enter this section."
    )

    # Programmatic guard: write to session dedup state so domhand_fill
    # blocks re-filling this section even if the agent ignores the message.
    if browser_session is not None:
        completed = getattr(browser_session, "_gh_completed_repeater_sections", set())
        completed.add(canonical)
        browser_session._gh_completed_repeater_sections = completed

    return ActionResult(
        extracted_content="\n".join(summary_parts),
        long_term_memory=_long_term,
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
