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
import json
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
  const vis = e => { const r=e.getBoundingClientRect(); return r.width>=8 && r.height>=8; };
  // required-ness: the `required`/aria-required attr OR a '*' in the field's own label (the common
  // marker plain-attr checks miss). labelText walks the same nearest-label chain discovery uses.
  const labelText = e => { const l = e.id && document.querySelector('label[for="'+CSS.escape(e.id)+'"]');
    let t = (l && l.innerText) || e.getAttribute('aria-label') || '';
    if (!t) { let p=e.closest('label')||e.parentElement, h=0;
      while(p && h++<3 && !t){ const c=p.querySelector(':scope > label,:scope > legend,:scope > span'); t=(c&&c.innerText)||''; p=p.parentElement; } }
    return norm(t); };
  const isReq = e => e.required || e.getAttribute('aria-required')==='true' || /\*/.test(labelText(e));
  const empty = [];
  for (const e of document.querySelectorAll('input,select,textarea')) {
    if (!vis(e) || e.type==='hidden' || e.type==='file') continue;
    if (e.tagName==='SELECT') { const o=e.options[e.selectedIndex];
      const ph = !o || o.value==='' || /^(select|choose|--)/i.test(norm(o.text));
      if (isReq(e) && ph) empty.push(labelText(e)||e.name||'select'); continue; }
    if (e.type==='radio' || e.type==='checkbox') continue;  // groups handled below
    if (isReq(e) && !(e.value||'').trim()) empty.push(labelText(e)||e.name||'?');
  }
  // required radio/checkbox GROUPS (custom Yes/No, ratings) with NOTHING checked: group by name,
  // required if any member is required or the group's question label carries a '*'.
  const groups = {};
  for (const e of document.querySelectorAll('input[type=radio],input[type=checkbox]')) {
    const g = e.name || (e.closest('fieldset,[role=radiogroup]')||{}).id || ''; if(!g) continue;
    (groups[g] = groups[g] || {req:false,checked:false,label:''});
    groups[g].req = groups[g].req || isReq(e);
    groups[g].checked = groups[g].checked || e.checked;
    const fs = e.closest('fieldset'); const lg = fs && fs.querySelector('legend');
    if (lg && !groups[g].label) groups[g].label = norm(lg.innerText);
    // the '*' marker usually sits on the QUESTION text above the pills, not any pill's own label
    // (teamtailor: required séjour radios carried no required attr and each pill read 'Oui'/'Non'
    // — the group slipped the audit and complete:True lied). Read the smallest ancestor holding
    // the WHOLE group (+1 parent for a question label rendered as a sibling above it).
    if (!groups[g].req && e.name) {
      let p = e.parentElement, h = 0, box = null;
      while (p && h++ < 6) { if (p.querySelectorAll('input[name="'+CSS.escape(e.name)+'"]').length >= 2) { box = p; break; } p = p.parentElement; }
      if (box) {
        const t = norm(box.innerText).slice(0,250) + ' ' + norm((box.parentElement||{}).innerText||'').slice(0,250);
        if (/\*/.test(t)) groups[g].req = true;
        if (!groups[g].label) { const q = norm((box.parentElement||box).innerText).split('\n')[0]; if (q) groups[g].label = q.slice(0,80); }
      }
    }
  }
  for (const [g,v] of Object.entries(groups)) if (v.req && !v.checked) empty.push(v.label||g);

  // CUSTOM WIDGETS (no native input/select — rippling is 100% custom; the scans above find
  // NOTHING and a form of empty required questions read complete). Question-centric: a '*'-marked
  // question with a control that shows NO committed answer. Generic via ARIA / placeholder text,
  // never per-ATS.
  const qlabel = box => { const t = norm(box.innerText||''); return (t.split('\n')[0]||t).slice(0,80); };
  // (a) custom comboboxes: [role=combobox] / [aria-haspopup=listbox] with no aria-activedescendant
  //     and a placeholder-looking trigger text (Select…/Choose…/empty).
  for (const c of document.querySelectorAll('[role=combobox],[aria-haspopup=listbox]')) {
    if (!vis(c)) continue;
    const q = c.closest('[class*=field],[class*=question],[class*=form-group],div');
    const reqd = c.getAttribute('aria-required')==='true' || /\*/.test(norm((q||c).innerText).slice(0,200));
    if (!reqd) continue;
    const active = c.getAttribute('aria-activedescendant');
    const shown = norm(c.innerText || c.value || (c.querySelector('*')||{}).innerText || '');
    const placeholder = !shown || /^(select|choose|--|pick)/i.test(shown);
    if (!active && placeholder) empty.push(qlabel(q||c));
  }
  // (b) custom radio/checkbox groups via [role=radiogroup]/[role=group] with no aria-checked member,
  //     or aria-checked elements where none is true.
  for (const grp of document.querySelectorAll('[role=radiogroup],[role=group]')) {
    if (!vis(grp)) continue;
    const opts = [...grp.querySelectorAll('[role=radio],[role=checkbox],[aria-checked]')];
    if (opts.length < 2) continue;
    const reqd = grp.getAttribute('aria-required')==='true' || /\*/.test(norm(grp.innerText).slice(0,200));
    if (!reqd) continue;
    if (!opts.some(o => o.getAttribute('aria-checked')==='true')) empty.push(qlabel(grp));
  }
  return JSON.stringify({adds: [...new Set(adds)], emptyReq: [...new Set(empty)].slice(0,25)});
}"""


# %s = a JSON array of VLM-flagged labels; keep only those whose on-page text has a fillable
# control within a few ancestor hops (page furniture — footer banners, login prompts — has none).
_NEAR_FIELD_FILTER_JS = r"""() => {
  const labels = %s;
  const nrm = s => (s||'').toLowerCase().replace(/\s+/g,' ').trim();
  return JSON.stringify(labels.filter(lab => {
    // token-overlap match: the VLM paraphrases ('travaillez' vs 'travailliez' across runs), so
    // exact containment misses the on-page text and the furniture flag survives forever.
    const toks = nrm(lab).split(' ').filter(w => w.length > 3);
    if (!toks.length) return false;
    const els = [...document.querySelectorAll('*')].filter(e => {
      if (e.children.length >= 6) return false;
      const T = nrm(e.innerText); if (!T) return false;
      let hit = 0; for (const w of toks) if (T.includes(w)) hit++;
      return hit >= Math.ceil(toks.length * 0.6);
    });
    if (!els.length) return true;  // VLM fully paraphrased — keep (tighter verdict, never looser)
    return els.some(e => { let p = e;
      for (let i = 0; i < 5 && p; i++) {
        if (p.querySelector('input,select,textarea,[role=combobox],[role=radiogroup],[role=listbox]')) return true;
        p = p.parentElement;
      }
      return false; });
  }));
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
                        # high: at low detail a dark-themed form's placeholders and unselected
                        # pills are unreadable — the visual gate PASSED a form with a required
                        # radio pair unselected (teamtailor tt7 false-complete).
                        detail="high",
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


async def _vlm_unanswered_required(session: Any) -> list[str]:
    """VISUAL second opinion (user: rely on visuals; DOM alone over-claims): ONE full-page VLM
    read naming required-marked fields that LOOK unanswered. [] = vision agrees the form is
    complete. Best-effort []; disagreement only ever makes the verdict STRICTER, never looser."""
    try:
        import asyncio

        import oa_llm as _oa
        from vision_verify import _parse_str_list, _vlm

        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

        png = None
        with contextlib.suppress(Exception):
            sh = await asyncio.wait_for(session.take_screenshot(full_page=True), timeout=15.0)
            png = base64.b64decode(sh) if isinstance(sh, str) else sh
        if png is None:
            return []
        msg = UserMessage(
            content=[
                ContentPartTextParam(
                    type="text",
                    text=(
                        "This is a job application form AFTER automated filling. Look for REQUIRED "
                        "fields that are still visibly UNANSWERED: empty text boxes, dropdowns still "
                        "showing a placeholder like 'Select an option', yes/no button pairs with "
                        "NEITHER selected, empty upload areas. A field counts as REQUIRED only if "
                        "you can SEE its required marker: an asterisk (*) or the word "
                        "'required'/'Requis' attached to ITS OWN label — an empty but unmarked field "
                        "is NOT a finding. ONLY count questions inside the APPLICATION FORM itself — "
                        "ignore page furniture: headers, footers, cookie banners, employee-referral "
                        "('already work here?') banners, login prompts, newsletter signups. Reply "
                        "ONLY a STRICT JSON array of their labels, [] if every required-marked field "
                        "visibly has an answer."
                    ),
                ),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(
                        url=f"data:image/png;base64,{base64.b64encode(png).decode()}",
                        # high: at low detail a dark-themed form's placeholders and unselected
                        # pills are unreadable — the visual gate PASSED a form with a required
                        # radio pair unselected (teamtailor tt7 false-complete).
                        detail="high",
                        media_type="image/png",
                    ),
                ),
            ]
        )
        res = await _oa.resilient_vlm([msg], primary=_vlm())
        raw = str(getattr(res, "completion", res) or "[]")
        return _parse_str_list(raw)[:15]
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


async def _orphan_pass(session: Any, page: Any, profile: dict, llm: Any, filled_names: set) -> int:
    """Fill fields that APPEARED AFTER the flat pass ran (e.g. breezy's resume-parse creates a
    Work History row whose Company input the parser left empty — flat discovery never saw it).
    Re-discover, map the never-seen fields against the profile, run the normal engine on each.
    Returns how many were filled. Never raises."""
    n = 0
    with contextlib.suppress(Exception):
        import oa_observe_act as oa
        from oa_discover import discover_fields
        from oa_singlepage import _field_dict

        orphans = [f for f in await discover_fields(page) if str(f.name) not in filled_names]
        print(f"   [complete] orphan candidates: {[str(f.name)[:24] for f in orphans][:8]}")
        if not orphans:
            return 0
        mapping = await eng.map_fields(orphans, profile, llm=llm) if llm is not None else {}
        for f in orphans:
            val = (mapping or {}).get(f.name) or ""
            if not val:
                continue
            with contextlib.suppress(Exception):
                fd = _field_dict(f, val, resume=None, llm=llm, adapter=None, page=page)
                out = await oa.observe_act(session, fd)
                if str(getattr(out, "outcome", out)).upper().find("DONE") >= 0:
                    n += 1
        if n:
            print(f"   [complete] orphan pass filled {n} late-appearing field(s)")
    return n


async def complete(
    session: Any, page: Any, profile: dict, resume: str | None, *, allow_agent: bool, llm: Any = None, planner_keys: list | None = None,
    filled_names: set | None = None,
) -> dict:
    """Audit the form for unfilled repeater sections + empty required fields; fill repeaters via the
    proven agent_fill_section and RE-FILL wiped required fields (retry). Returns
    {complete, missing_required, sections_filled, sections_skipped, retried}. Never raises."""
    verdict: dict = {
        "complete": True, "missing_required": [], "sections_filled": [], "sections_skipped": [], "retried": 0,
    }
    with contextlib.suppress(Exception):
        if resume:  # button-triggered resume upload (hibob/bamboohr 'Add file' has no <input> yet)
            await upload_via_button(session, page, resume)
        if filled_names:  # fields that appeared AFTER the flat pass (resume-parse rows etc.)
            await _orphan_pass(session, page, profile, llm, filled_names)
        a = await audit(session, page)
        verdict["missing_required"] = a.get("emptyReq", [])
        # RETRY: re-fill required fields a React re-render wiped after commit (workable class),
        # BEFORE deciding completeness — one bounded pass, then re-audit below.
        if verdict["missing_required"] and llm is not None:
            verdict["retried"] = await retry_missing(
                session, page, profile, resume, llm, verdict["missing_required"]
            )
        # REPEATER SECTIONS (Experience / Education): fill DETERMINISTICALLY (click Add per profile
        # entry, fill the new row) — fast + reliable, unlike the slow crash-prone agent. Runs whenever
        # the DOM shows an add-row affordance and the profile has history; no agent gate needed.
        if (a.get("adds") or planner_keys) and (profile.get("experience") or profile.get("education")) and llm is not None:
            with contextlib.suppress(Exception):
                import oa_repeater

                rep = await oa_repeater.fill_repeaters(session, page, profile, resume, llm, planner_keys=planner_keys)
                if rep.get("sections"):
                    verdict["sections_filled"] = list(rep["sections"].keys())
                    verdict["repeater_fields_filled"] = rep.get("fields_filled", 0)
        # any repeater the deterministic pass could NOT fill (unusual layout) -> agent fallback,
        # only when allowed; else flag it incomplete (never a silent pass).
        sections = []
        with contextlib.suppress(Exception):
            sections = [
                s for s in await _vlm_unfilled_sections(session)
                if _profile_has(profile, s) and s.lower() not in " ".join(verdict["sections_filled"]).lower()
            ]
        for sec in sections:
            if not allow_agent:
                verdict["sections_skipped"].append(sec)
                verdict["complete"] = False
                print(f"   [complete] '{sec}' section still unfilled after deterministic pass (agent off)")
                continue
            with contextlib.suppress(Exception):
                await eng.agent_fill_section(
                    session, page, section=sec, instructions=_instructions(profile, sec), resume=resume, max_steps=14
                )
                verdict["sections_filled"].append(sec)
                print(f"   [complete] agent-filled repeater section '{sec}'")
        # re-audit after retry + section fill
        with contextlib.suppress(Exception):
            verdict["missing_required"] = (await audit(session, page)).get("emptyReq", [])
        # REMAINING REQUIRED (screening Yes/No, skill-rating dropdowns): these have no profile
        # value to map — they need judgement (authorized-to-work -> Yes, rate Python 1-5 from the
        # candidate's skills). Hand them to the agent, which carries the truthful-default +
        # skill-rating logic. Gated to allow_agent; otherwise they stay flagged for HITL.
        if verdict["missing_required"] and allow_agent:
            with contextlib.suppress(Exception):
                skills = ", ".join(profile.get("skills") or [])[:200]
                instr = (
                    "Answer EVERY remaining required question on this page. For yes/no eligibility "
                    "(authorized to work in the US -> Yes; require visa sponsorship -> No; 18 or older "
                    "-> Yes; meets a stated years-of-experience threshold -> Yes; on-site/relocation "
                    "commitment -> Yes unless the profile says otherwise). For skill self-ratings "
                    f"(1-5), rate from this candidate's background — skills: {skills}; senior engineer "
                    "with ~5 years — rate core/listed skills 4-5, unfamiliar ones 2-3. Pick the closest "
                    "option in every dropdown. Do NOT submit; do NOT navigate away."
                )
                await eng.agent_fill_section(
                    session, page, section="Remaining required questions", instructions=instr, resume=resume, max_steps=18
                )
                verdict["agent_answered_required"] = True
                with contextlib.suppress(Exception):
                    verdict["missing_required"] = (await audit(session, page)).get("emptyReq", [])
        # VISUAL SECOND OPINION (final gate): the DOM audit alone has over-claimed before —
        # vision must AGREE the form looks complete. Disagreement only tightens the verdict.
        if not verdict["missing_required"] and not verdict["sections_skipped"]:
            with contextlib.suppress(Exception):
                seen = await _vlm_unanswered_required(session)
                if seen:
                    # PAGE-FURNITURE filter: keep a flag only when its on-page text sits near a
                    # FILLABLE control. The VLM kept flagging the footer employee-referral banner
                    # ('Vous travaillez déjà chez … ?' — a heading + login button, no field)
                    # despite the prompt exclusion. Text match = normalized containment
                    # (deterministic identity, not a semantic pattern).
                    with contextlib.suppress(Exception):
                        seen = json.loads(await page.evaluate(_NEAR_FIELD_FILTER_JS % json.dumps(seen)))
                if seen:
                    verdict["visually_unanswered"] = seen
                    print(f"   [complete] VISION disagrees — looks unanswered: {seen[:5]}")
        if verdict["missing_required"] or verdict["sections_skipped"] or verdict.get("visually_unanswered"):
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


# ---------------------------------------------------------------------------
# Button-triggered file upload (hibob/bamboohr 'Add file': no <input type=file> in the DOM
# until the button is clicked / it opens a native picker). Intercept the file chooser so the
# OS dialog never blocks, then set the file on the node the chooser reports. Generic.
# ---------------------------------------------------------------------------
async def upload_via_button(session: Any, page: Any, resume_path: str) -> bool:
    """Upload resume when there's no file input yet — click an upload affordance and intercept
    the file chooser. False if no upload button / no resume. NEVER raises."""
    import os

    if not resume_path or not os.path.exists(resume_path):
        return False
    with contextlib.suppress(Exception):
        # already have a file input? use the direct path
        has_input = await page.evaluate("() => document.querySelectorAll('input[type=file]').length")
        # DEEP query: hibob renders the whole form in shadow DOM — document.querySelectorAll
        # never sees the 'Add file' button, so the chooser stage was unreachable there.
        find_btn = await page.evaluate(
            "() => { const rx=/^(add file|upload|choose file|attach|upload your resume|add attachment|upload cv)$/i;"
            " const all=[]; const walk=(root)=>{ for(const e of root.querySelectorAll('*')){ all.push(e);"
            "   if(e.shadowRoot) walk(e.shadowRoot); } }; walk(document);"
            " const el=all.find(e=>{ if(!e.matches('button,a,[role=button]')) return false;"
            "   const r=e.getBoundingClientRect(); if(r.width<8||r.height<8) return false;"
            "   return rx.test((e.innerText||e.getAttribute('aria-label')||'').trim());});"
            " if(!el) return ''; el.scrollIntoView({block:'center'}); const r=el.getBoundingClientRect();"
            " return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2}); }"
        )
        if not find_btn:
            return False
        import asyncio
        import json as _j

        sid = await page.session_id
        abspath = os.path.abspath(resume_path)

        # FILE-CHOOSER EVENT on the PAGE-target CDP session (hibob opens a native picker with NO
        # DOM input — the event carries the input's backendNodeId; root-client register misses it).
        with contextlib.suppress(Exception):
            cdp_sess = await session.get_or_create_cdp_session()
            captured: dict = {}

            # EventRegistry invokes callback(event_data, session_id) with TWO positionals — the
            # old `(evt, _c=captured)` signature bound _c to the session-id STRING, `_c["bn"]`
            # threw, the suppress ate it, and captured stayed empty every time (the whole reason
            # hibob's native picker 'could not be captured'). Proven fixed by scratch_chooser_toy.
            def _on_chooser(evt: Any, _session_id: Any = None) -> None:
                with contextlib.suppress(Exception):
                    bn = evt.get("backendNodeId") if isinstance(evt, dict) else getattr(evt, "backendNodeId", None)
                    if bn:
                        captured["bn"] = int(bn)

            with contextlib.suppress(Exception):
                await cdp_sess.cdp_client.send.Page.enable(session_id=cdp_sess.session_id)
            cdp_sess.cdp_client.register.Page.fileChooserOpened(_on_chooser)
            await cdp_sess.cdp_client.send.Page.setInterceptFileChooserDialog(
                params={"enabled": True}, session_id=cdp_sess.session_id
            )
            c = _j.loads(find_btn)
            for ev in (
                {"type": "mousePressed", "x": c["x"], "y": c["y"], "button": "left", "buttons": 1, "clickCount": 1},
                {"type": "mouseReleased", "x": c["x"], "y": c["y"], "button": "left", "buttons": 0, "clickCount": 1},
            ):
                await cdp_sess.cdp_client.send.Input.dispatchMouseEvent(params=ev, session_id=cdp_sess.session_id)
            for _ in range(20):
                if captured.get("bn"):
                    break
                await asyncio.sleep(0.2)
            with contextlib.suppress(Exception):
                await cdp_sess.cdp_client.send.Page.setInterceptFileChooserDialog(
                    params={"enabled": False}, session_id=cdp_sess.session_id
                )
            if captured.get("bn"):
                await cdp_sess.cdp_client.send.DOM.setFileInputFiles(
                    params={"files": [abspath], "backendNodeId": captured["bn"]}, session_id=cdp_sess.session_id
                )
                print(f"   [upload] attached resume via file-chooser event -> {os.path.basename(abspath)}")
                return True

        async def _pierce_file_backend() -> int | None:
            """Find a file input's backendNodeId even in SHADOW DOM (pierce), where
            document.querySelector can't reach it (hibob 'Add file')."""
            with contextlib.suppress(Exception):
                doc = await session.cdp_client.send.DOM.getDocument(
                    params={"depth": -1, "pierce": True}, session_id=sid
                )

                def walk(node: Any) -> int | None:
                    if node.get("nodeName", "").upper() == "INPUT":
                        attrs = node.get("attributes") or []
                        for i in range(0, len(attrs) - 1, 2):
                            if attrs[i] == "type" and attrs[i + 1] == "file":
                                return node.get("backendNodeId")
                    for kid in (node.get("children") or []) + (
                        [node["contentDocument"]] if node.get("contentDocument") else []
                    ):
                        for sr in node.get("shadowRoots") or []:
                            r = walk(sr)
                            if r:
                                return r
                        r = walk(kid)
                        if r:
                            return r
                    for sr in node.get("shadowRoots") or []:
                        r = walk(sr)
                        if r:
                            return r
                    return None

                return walk(doc["root"])
            return None

        # click the button to MOUNT the input (hibob mounts it on click), then pierce-find it
        c = _j.loads(find_btn)
        with contextlib.suppress(Exception):
            await session.cdp_client.send.Page.setInterceptFileChooserDialog(params={"enabled": True}, session_id=sid)
        for ev in (
            {"type": "mousePressed", "x": c["x"], "y": c["y"], "button": "left", "buttons": 1, "clickCount": 1},
            {"type": "mouseReleased", "x": c["x"], "y": c["y"], "button": "left", "buttons": 0, "clickCount": 1},
        ):
            await session.cdp_client.send.Input.dispatchMouseEvent(params=ev, session_id=sid)
        await asyncio.sleep(1.2)
        bn = await _pierce_file_backend()
        ok = False
        if bn:
            with contextlib.suppress(Exception):
                await session.cdp_client.send.DOM.setFileInputFiles(
                    params={"files": [abspath], "backendNodeId": int(bn)}, session_id=sid
                )
                print(f"   [upload] attached resume (pierced file input) -> {os.path.basename(abspath)}")
                ok = True
        with contextlib.suppress(Exception):
            await session.cdp_client.send.Page.setInterceptFileChooserDialog(params={"enabled": False}, session_id=sid)
        return ok
    return False
