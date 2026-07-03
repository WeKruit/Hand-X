"""oa_repeater — DETERMINISTIC generic repeater filler (Experience / Education / ... behind an
'Add another' button). Replaces the slow, crash-prone completeness-agent for repeaters.

User: a real form is 10-20 fields — experience, education, various sections — not 5. Collapsed
repeater sections (hibob: 6 of them, each just an 'Add' button until clicked) are invisible to
flat discovery, so we under-count AND under-fill. This fills them the deterministic way the
Workday adapter does, but host-agnostic:

  1. find every 'Add' affordance + the SECTION it belongs to (its nearest heading — section
     IDENTITY, the one place heading text is legitimate: we must know Education vs Experience to
     route the right profile rows).
  2. for each section we have profile data for (experience / education), for each profile entry:
     click Add -> wait for the new row -> discover the row's NEW fields -> map that ONE entry ->
     fill each via observe_act.
  bounded: max entries/section, per-section time budget.

Reuses discover_fields + map_fields + observe_act — no new fill primitives.
"""

import asyncio
import contextlib
import time
from typing import Any

import ats_engine as eng

_SECTION_KEYS = {
    "experience": "experience",
    "employment": "experience",
    "work": "experience",
    "education": "education",
    "school": "education",
    "academic": "education",
}

# find Add affordances + their section heading (nearest preceding heading-ish text). Structural
# affordance + heading only for SECTION IDENTITY (which profile list to place), never for fields.
_FIND_SECTIONS_JS = r"""() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const addRx = /^\+?\s*add\b/i;
  const out = [];
  const adds = [...document.querySelectorAll('button,a,[role=button]')].filter(e => {
    const r=e.getBoundingClientRect(); if(r.width<8||r.height<8) return false;
    const t=norm(e.innerText||e.getAttribute('aria-label')||'');
    return t.length<24 && addRx.test(t); });
  const addWords = /\s*(add file|add another|add more|add)\s*$/i;
  for (const a of adds) {
    // The section label is the Add's nearest ancestor whose text is "<SectionName> Add"
    // (hibob renders 'Education Add' / 'Experience Add' as a div, no h-tag). Walk up, take the
    // smallest ancestor whose text is short and ends with an add-verb, minus that verb.
    let sec='', p=a;
    for (let i=0; i<6 && p; i++) {
      p = p.parentElement; if (!p) break;
      const t = norm(p.innerText || '');
      if (t && t.length < 60 && addWords.test(t)) { sec = norm(t.replace(addWords, '')); if (sec) break; }
    }
    const r=a.getBoundingClientRect();
    out.push({section: sec, x: r.left+r.width/2, y: r.top+r.height/2});
  }
  return JSON.stringify(out);
}"""


def _entry_profile(profile: dict, key: str, entry: dict) -> dict:
    """A ONE-entry mini-profile the flat map_fields can map (labels School/Company/Degree/... ->
    this entry's values)."""
    base = {k: v for k, v in profile.items() if not isinstance(v, (list, dict))}
    if key == "education":
        base.update({
            "school": entry.get("school", ""), "university": entry.get("school", ""),
            "degree": entry.get("degree", ""), "field_of_study": entry.get("field_of_study", ""),
            "major": entry.get("field_of_study", ""),
            "start_date": entry.get("start_date", ""),
            "end_date": entry.get("graduation_date", entry.get("end_date", "")),
            "graduation_date": entry.get("graduation_date", ""), "gpa": entry.get("gpa", ""),
        })
    else:
        base.update({
            "company": entry.get("company", ""), "employer": entry.get("company", ""),
            "title": entry.get("title", ""), "job_title": entry.get("title", ""),
            "location": entry.get("location", ""),
            "start_date": entry.get("start_date", ""),
            "end_date": "Present" if entry.get("current") else entry.get("end_date", ""),
            "description": "; ".join(entry.get("highlights", []))[:400],
        })
    return base


async def fill_repeaters(session: Any, page: Any, profile: dict, resume: str | None, llm: Any, *, budget_s: float = 150.0) -> dict:
    """Fill Experience/Education repeater sections deterministically. Returns
    {sections: {name: rows_filled}}. Never raises; bounded by budget_s."""
    import oa_observe_act as oa
    from oa_discover import discover_fields
    from oa_singlepage import _field_dict

    result: dict = {"sections": {}}
    deadline = time.monotonic() + budget_s
    with contextlib.suppress(Exception):
        raw = await page.evaluate(_FIND_SECTIONS_JS)
        import json as _json

        adds = _json.loads(raw) if raw else []
    for add in adds:
        if time.monotonic() > deadline:
            break
        sec_text = str(add.get("section", "")).lower()
        key = next((v for k, v in _SECTION_KEYS.items() if k in sec_text), None)
        if not key:
            continue
        entries = profile.get(key) or (profile.get("work_experience") if key == "experience" else None) or []
        if not entries:
            continue
        rows_filled = 0
        for entry in entries[:4]:
            if time.monotonic() > deadline:
                break
            with contextlib.suppress(Exception):
                # snapshot field ids BEFORE Add, click Add, re-discover -> the NEW fields are the row
                before = {f.name for f in await discover_fields(page)}
                sid = await page.session_id
                for ev in (
                    {"type": "mouseMoved", "x": add["x"], "y": add["y"], "buttons": 0},
                    {"type": "mousePressed", "x": add["x"], "y": add["y"], "button": "left", "buttons": 1, "clickCount": 1},
                    {"type": "mouseReleased", "x": add["x"], "y": add["y"], "button": "left", "buttons": 0, "clickCount": 1},
                ):
                    await session.cdp_client.send.Input.dispatchMouseEvent(params=ev, session_id=sid)
                await asyncio.sleep(1.5)
                new_fields = [f for f in await discover_fields(page) if f.name not in before]
                if not new_fields:
                    break  # Add didn't add a row (section full / not a repeater) — stop this section
                mini = _entry_profile(profile, key, entry)
                rows = [f for f in new_fields if f.needs_map]
                mapped = await eng.map_fields(llm, rows, mini, "") if rows else {}
                for f in new_fields:
                    value, _ = eng._resolve(f, mapped, resume)
                    if not (value or "").strip():
                        continue
                    fd = _field_dict(f, value, resume=resume, llm=llm, adapter=None, page=page)
                    with contextlib.suppress(Exception):
                        await oa.observe_act(session, fd)
                rows_filled += 1
        if rows_filled:
            result["sections"][sec_text[:30] or key] = rows_filled
            print(f"   [repeater] filled {rows_filled} row(s) in '{key}' section")
    return result
