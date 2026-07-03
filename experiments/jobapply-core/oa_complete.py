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


def _norm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


async def retry_missing(session: Any, page: Any, profile: dict, resume: str | None, llm: Any, missing: list[str]) -> int:
    """RE-FILL fields the audit found still-empty (React re-render wiped a committed value —
    the workable class). Re-discover the live DOM, match each still-empty REQUIRED field by
    label, re-map + re-run observe_act on just those. Returns how many got re-filled. Generic."""
    import oa_observe_act as oa
    from oa_discover import discover_fields
    from oa_singlepage import _field_dict

    want = {_norm(m) for m in missing}
    refilled = 0
    with contextlib.suppress(Exception):
        fields = [f for f in await discover_fields(page) if _norm(f.label) in want or _norm(f.name) in want]
        if not fields:
            return 0
        rows = [f for f in fields if f.needs_map]
        mapped = await eng.map_fields(llm, rows, profile, "") if rows else {}
        for f in fields:
            value, _ = eng._resolve(f, mapped, resume)
            if not (value or "").strip():
                continue
            fd = _field_dict(f, value, resume=resume, llm=llm, adapter=None, page=page)
            with contextlib.suppress(Exception):
                out = await oa.observe_act(session, fd)
                if out == oa.DONE:
                    refilled += 1
    if refilled:
        print(f"   [complete] retry re-filled {refilled} wiped/empty required field(s)")
    return refilled


async def complete(
    session: Any, page: Any, profile: dict, resume: str | None, *, allow_agent: bool, llm: Any = None
) -> dict:
    """Audit the form for unfilled repeater sections + empty required fields; fill repeaters via the
    proven agent_fill_section and RE-FILL wiped required fields (retry). Returns
    {complete, missing_required, sections_filled, sections_skipped, retried}. Never raises."""
    verdict: dict = {
        "complete": True, "missing_required": [], "sections_filled": [], "sections_skipped": [], "retried": 0,
    }
    with contextlib.suppress(Exception):
        a = await audit(session, page)
        verdict["missing_required"] = a.get("emptyReq", [])
        # RETRY: re-fill required fields a React re-render wiped after commit (workable class),
        # BEFORE deciding completeness — one bounded pass, then re-audit below.
        if verdict["missing_required"] and llm is not None:
            verdict["retried"] = await retry_missing(
                session, page, profile, resume, llm, verdict["missing_required"]
            )
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


# ---------------------------------------------------------------------------
# Consent-overlay dismissal (a blocker on many sites: cookie/privacy banners intercept
# focus/pointer events and wipe fills — the workable case). Mechanical dismissal of a known
# affordance kind, not a semantic form decision.
# ponytail: curated common-consent handles + generic accept-text; add a VLM read only if
# failures.jsonl shows a banner this misses.
# ---------------------------------------------------------------------------
_DISMISS_JS = r"""() => {
  const sels = ['#onetrust-accept-btn-handler',
    '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
    '#CybotCookiebotDialogBodyButtonAccept',
    '[aria-label*="accept" i]','[data-testid*="accept" i]','[id*="cookie" i] button'];
  for (const s of sels) { const el=document.querySelector(s);
    if (el && el.getBoundingClientRect().width>0) { el.click(); return 'sel:'+s; } }
  const rx = /^(accept all|accept cookies|accept|agree|i agree|got it|allow all|ok)$/i;
  const btn = [...document.querySelectorAll('button,[role=button],a')].find(e => {
    const r=e.getBoundingClientRect(); if(r.width<8||r.height<8) return false;
    return rx.test((e.innerText||'').trim()); });
  if (btn) { btn.click(); return 'text:'+(btn.innerText||'').trim().slice(0,24); }
  return '';
}"""


async def dismiss_consent(session: Any, page: Any) -> bool:
    """Click a cookie/consent accept button if one is blocking the page. False if none found."""
    with contextlib.suppress(Exception):
        r = await page.evaluate(_DISMISS_JS)
        if r:
            print(f"   [consent] dismissed overlay ({r})")
            return True
    return False
