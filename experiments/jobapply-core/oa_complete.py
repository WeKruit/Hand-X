"""oa_complete — post-fill completeness audit + repeater fill for the GENERIC lane.

The gap this closes (user: '你确定都填完了?有时候没有 required 填写 experience 但是我们可以肯定得加'):
discover_fields only sees the flat inputs currently rendered. A multi-row repeater (Work
Experience / Education behind an 'Add another' button) is INVISIBLE to it, so a generic fill
could report 1.0 while silently skipping a whole section. fill_rate = filled/discovered can
never catch a section we never discovered — so we need an independent audit.

Two halves, both host-agnostic:
  1. audit(session, page)  -> {add_affordances, empty_required, sections}
     - DOM scan for 'Add'/'Add another' row affordances (structural: a small button whose
       text/aria is an add-row verb) + still-empty required inputs.
     - ONE cheap VLM glance: which repeatable sections (Work Experience, Education, ...) are
       present but UNFILLED. Vision, not heading-text matching.
  2. complete(...) -> fills each detected repeater section by handing it to the PROVEN
     eng.agent_fill_section (the exact browser-use agent the GH adapter uses for education:
     scrolls to the section, clicks 'Add another', fills each row from the profile). Generic —
     no per-ATS code. Gated by allow_agent so cheap sweeps stay cheap; ON for real fills.

Returns a completeness verdict so the runner reports honestly instead of a blind 1.0.
"""

import base64
import contextlib
from typing import Any

import ats_engine as eng

# add-row affordance: a SMALL clickable whose accessible text is an add-a-row verb. Structural
# (an affordance kind), not a section-title match — a localized 'Add' still reads as add-row.
_AUDIT_JS = r"""() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const addRx = /^\+?\s*add\b/i;
  const adds = [...document.querySelectorAll('button,a,[role=button],[role=link]')]
    .filter(e => { const r=e.getBoundingClientRect(); if(r.width<8||r.height<8) return false;
      const t=norm(e.innerText||e.getAttribute('aria-label')||''); return t.length<40 && addRx.test(t); })
    .map(e => norm(e.innerText||e.getAttribute('aria-label')||'')).slice(0,8);
  const emptyReq = [...document.querySelectorAll('input,select,textarea')]
    .filter(e => { const r=e.getBoundingClientRect(); if(r.width<8||r.height<8) return false;
      const req = e.required || e.getAttribute('aria-required')==='true';
      const v = (e.value||'').trim(); return req && !v && e.type!=='hidden' && e.type!=='file'; })
    .map(e => { const l=document.querySelector('label[for="'+CSS.escape(e.id||'')+'"]');
      return norm((l&&l.innerText)||e.getAttribute('aria-label')||e.name||'?'); }).slice(0,20);
  return JSON.stringify({adds: [...new Set(adds)], emptyReq: [...new Set(emptyReq)]});
}"""


async def audit(session: Any, page: Any) -> dict:
    """DOM scan: add-row affordances + still-empty required fields. {} on failure."""
    import json

    with contextlib.suppress(Exception):
        return json.loads(await page.evaluate(_AUDIT_JS))
    return {"adds": [], "emptyReq": []}


async def _vlm_unfilled_sections(session: Any) -> list[str]:
    """ONE VLM read: which repeatable sections are present but UNFILLED (Work Experience,
    Education, Employment History, ...). [] on miss. Vision, not heading-text matching."""
    try:
        import oa_llm as _oa
        from vision_verify import _parse_str_list, _vlm

        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

        png = await _oa.bounded_screenshot(session)
        if png is None:
            return []
        if isinstance(png, str):
            png = base64.b64decode(png)
        msg = UserMessage(
            content=[
                ContentPartTextParam(
                    type="text",
                    text=(
                        "This is a job application form. Reply ONLY a STRICT JSON array of the names of "
                        "any REPEATABLE history sections that are present but still EMPTY / not yet filled "
                        '(e.g. ["Work Experience", "Education"]). A section counts as empty if it shows an '
                        "'Add' button and no entries, or blank entry fields. If everything is filled or there "
                        "are no such sections, reply []."
                    ),
                ),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(
                        url=f"data:image/png;base64,{base64.b64encode(png).decode()}",
                        detail="low",
                        media_type="image/png",
                    ),
                ),
            ]
        )
        res = await _oa.resilient_vlm([msg], primary=_vlm())
        raw = str(getattr(res, "completion", res) or "[]")
        return _parse_str_list(raw)[:6]
    except Exception:
        return []


def _profile_has(profile: dict, section: str) -> bool:
    s = section.lower()
    if "experience" in s or "employment" in s or "work" in s:
        return bool(profile.get("experience") or profile.get("work_experience"))
    if "education" in s or "school" in s or "degree" in s:
        return bool(profile.get("education"))
    return False


def _instructions(profile: dict, section: str) -> str:
    s = section.lower()
    if "education" in s or "school" in s or "degree" in s:
        rows = profile.get("education") or []
        return "; ".join(
            f"entry {i + 1}: School='{e.get('school', '')}', Degree='{e.get('degree', '')}', "
            f"Field='{e.get('field_of_study', '')}', Start='{e.get('start_date', '')}', "
            f"End='{e.get('graduation_date', e.get('end_date', ''))}', GPA='{e.get('gpa', '')}'"
            for i, e in enumerate(rows)
        )
    rows = profile.get("experience") or profile.get("work_experience") or []
    return "; ".join(
        f"entry {i + 1}: Company='{e.get('company', '')}', Title='{e.get('title', '')}', "
        f"Location='{e.get('location', '')}', Start='{e.get('start_date', '')}', "
        f"End='{'Present' if e.get('current') else e.get('end_date', '')}', "
        f"Description='{'; '.join(e.get('highlights', []))[:300]}'"
        for i, e in enumerate(rows)
    )


async def complete(session: Any, page: Any, profile: dict, resume: str | None, *, allow_agent: bool) -> dict:
    """Audit the form for unfilled repeater sections; fill each via the proven agent_fill_section.
    Returns {complete, missing_required, sections_filled, sections_skipped}. Never raises."""
    verdict: dict = {"complete": True, "missing_required": [], "sections_filled": [], "sections_skipped": []}
    with contextlib.suppress(Exception):
        a = await audit(session, page)
        verdict["missing_required"] = a.get("emptyReq", [])
        # only look for repeater sections if the DOM shows an add-row affordance OR the profile
        # has history to place — cheap gate before spending a VLM call.
        sections = []
        if a.get("adds") or profile.get("experience") or profile.get("education"):
            sections = [s for s in await _vlm_unfilled_sections(session) if _profile_has(profile, s)]
        for sec in sections:
            if not allow_agent:
                verdict["sections_skipped"].append(sec)
                verdict["complete"] = False
                print(f"   [complete] '{sec}' section unfilled — repeater fill needs agent (escalation off)")
                continue
            with contextlib.suppress(Exception):
                await eng.agent_fill_section(
                    session, page, section=sec, instructions=_instructions(profile, sec), resume=resume, max_steps=16
                )
                verdict["sections_filled"].append(sec)
                print(f"   [complete] filled repeater section '{sec}'")
        # re-audit required-empties after any section fill
        with contextlib.suppress(Exception):
            verdict["missing_required"] = (await audit(session, page)).get("emptyReq", [])
        if verdict["missing_required"] or verdict["sections_skipped"]:
            verdict["complete"] = False
    return verdict
