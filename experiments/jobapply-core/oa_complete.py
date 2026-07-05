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
    // POSITIONAL fallback: take the container's FIRST line — the question renders ABOVE the
    // control, validation errors BELOW. The child-element pick used to grab the error span, so
    // the audit reported 'This field is required' as the LABEL and the retry mapper had nothing
    // to map (rippling phone). Also fires when the label is the widget's OWN display text
    // ('Select' placeholder — identity check), so retry gets the QUESTION. Position, not
    // text semantics.
    const own = norm(e.innerText||'') || norm(e.placeholder||'') || norm(e.value||'');
    if (!t || /required/i.test(t) || (own && (norm(t)===own || own.startsWith(norm(t))))) {
      let p=e.parentElement, h=0;
      while(p && h++<4){ const line=(p.innerText||'').split('\n').map(norm).find(x=>x.length>1 && x.length<120 && x!==own);
        if(line && !/required/i.test(line)){ t=line; break; } p=p.parentElement; } }
    return norm(t); };
  const isReq = e => e.required || e.getAttribute('aria-required')==='true' || /[*\u2731](?!\s*indicates)/i.test(labelText(e));
  const empty = [];
  for (const e of document.querySelectorAll('input,select,textarea')) {
    if (!vis(e) || e.type==='hidden' || e.type==='file') continue;
    if (e.tagName==='SELECT') { const o=e.options[e.selectedIndex];
      const fold = x => (x||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'');
      const ph = !o || o.value==='' || /^(select|choose|--|pick|choisir|elegir|wahlen)/i.test(fold(norm(o.text)));
      if (isReq(e) && ph) empty.push(labelText(e)||e.name||'select'); continue; }
    if (e.type==='radio' || e.type==='checkbox') continue;  // groups handled below
    if (isReq(e) && !(e.value||'').trim()) {
      // RENDERED-DISPLAY CHECK (duolingo mega2/43-46 false flags): a geocomplete/react-select
      // keeps the committed text in a SIBLING display element while the input's own .value is
      // empty — the screen shows 'San Francisco, CA, USA' and this scan called it missing,
      // killing the COMPLETE verdict. Before flagging, scan two wrapper levels for a short
      // rendered text that is neither the label nor a placeholder.
      const lab = norm(labelText(e)); const ph = norm(e.placeholder||'');
      let filledVisually = false, p = e.parentElement;
      for (let h = 0; h < 2 && p && !filledVisually; h++, p = p.parentElement) {
        for (const s of p.querySelectorAll('[class*="single-value"],[class*="singleValue"],[class*="chip"],[class*="tag"],span,div')) {
          if (s === e || s.contains(e) || (s.querySelector && s.querySelector('input,select,textarea'))) continue;
          const t = norm(s.innerText||'');
          if (t && t.length <= 80 && t !== lab && t !== ph && !/[*✱]$/.test(t)
              && !/^(select|choose|--|pick|start typing|search)/i.test(t.normalize('NFD').replace(/[̀-ͯ]/g,''))) {
            filledVisually = true; break;
          }
        }
      }
      if (!filledVisually) empty.push(labelText(e)||e.name||'?');
    }
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
        if (/[*\u2731](?!\s*indicates)/i.test(t)) groups[g].req = true;
        if (!groups[g].label) { const q = norm((box.parentElement||box).innerText).split('\n')[0]; if (q) groups[g].label = q.slice(0,80); }
      }
    }
  }
  for (const [g,v] of Object.entries(groups)) if (v.req && !v.checked) empty.push(v.label||g);

  // CUSTOM WIDGETS (no native input/select — rippling is 100% custom; the scans above find
  // NOTHING and a form of empty required questions read complete). Question-centric: a '*'-marked
  // question with a control that shows NO committed answer. Generic via ARIA / placeholder text,
  // never per-ATS.
  const qlabel = box => { const lines = norm(box.innerText||'').split('\n').map(x=>x.trim()).filter(Boolean);
    // prefer the line carrying the required star / question mark — the first line can be a
    // help banner ('Click Here (If you encounter an issue…)' labeled palantir's university
    // dropdown, mega/63)
    return ((lines.find(l=>/[*\u2731?]/.test(l) && !/[*\u2731]\s*indicates/i.test(l) && !/^create a job alert/i.test(l))
      || lines.find(l=>!/^create a job alert/i.test(l)) || lines[0] || '').slice(0,80)); };
  // (a) custom comboboxes: [role=combobox] / [aria-haspopup=listbox] with no aria-activedescendant
  //     and a placeholder-looking trigger text (Select…/Choose…/empty).
  for (const c of document.querySelectorAll('[role=combobox],[aria-haspopup=listbox]')) {
    if (!vis(c)) continue;
    const q = c.closest('[class*=field],[class*=question],[class*=form-group],div');
    const h1a = document.querySelector('h1');
    if (q && h1a && q.contains(h1a)) continue;  // container swallowed the page header
    const reqd = c.getAttribute('aria-required')==='true' || /[*\u2731](?!\s*indicates)/i.test(norm((q||c).innerText).slice(0,200));
    if (!reqd) continue;
    const active = c.getAttribute('aria-activedescendant');
    const shown = norm(c.innerText || c.value || (c.querySelector('*')||{}).innerText || '');
    const placeholder = !shown || /^(select|choose|--|pick|choisir|elegir|wahlen|start typing|search)/i.test((shown||'').normalize('NFD').replace(/[\u0300-\u036f]/g,''));
    // sibling rendered-display (duolingo mega3/22-25: the geocomplete keeps the committed text
    // beside a role=combobox input whose own value stays empty — same false flag the plain-input
    // branch already guards against)
    let sib = false, sp = c.parentElement;
    for (let h = 0; h < 2 && sp && !sib; h++, sp = sp.parentElement) {
      for (const el of sp.querySelectorAll('[class*="single-value"],[class*="singleValue"],span,div')) {
        if (el === c || el.contains(c) || (el.querySelector && el.querySelector('input,select,textarea,[role=combobox]'))) continue;
        const t2 = norm(el.innerText||'');
        if (t2 && t2.length <= 80 && !/[*\u2731]$/.test(t2)
            && !/^(select|choose|--|pick|start typing|search)/i.test(t2.normalize('NFD').replace(/[\u0300-\u036f]/g,''))) { sib = true; break; }
      }
    }
    if (!active && placeholder && !sib) empty.push(qlabel(q||c));
  }
  // (c) BUTTON-PILL questions (ashby 'Yes'/'No' <button>s — no input, no ARIA group): a
  //     '*'-marked question block holding 2-12 SHORT-text buttons none of which reads
  //     selected (aria-pressed/aria-selected/checked-ish class). Invisible to every scan
  //     above, so the required flag never reached retry/agent (replit mega2/3-4: the Foster
  //     City commitment stayed blank through the whole pipeline).
  const btnSel = 'button,[role=button]';
  const seenPill = new Set();
  for (const b of document.querySelectorAll(btnSel)) {
    if (!vis(b)) continue;
    const t = norm(b.innerText||''); if (!t || t.length > 30) continue;
    let p = b.parentElement, d = 0, box = null;
    while (p && d++ < 5) {
      const n = [...p.querySelectorAll(btnSel)].filter(x => vis(x) && norm(x.innerText||'').length && norm(x.innerText||'').length <= 30).length;
      if (n >= 2 && n <= 12) { box = p; break; }
      p = p.parentElement;
    }
    if (!box || seenPill.has(box)) continue;
    seenPill.add(box);
    if (box.querySelector('input[type=file]')) continue;  // upload widget (duolingo: Dropbox/Drive/Enter-manually buttons)
    const h1 = document.querySelector('h1');
    if (h1 && box.contains(h1)) continue;  // page header block (affirm mega3/40: job title became the flag label)
    const qtxt = norm(box.innerText).slice(0,250) + ' ' + norm((box.parentElement||{}).innerText||'').slice(0,250);
    if (!/[*✱](?!\s*indicates)/i.test(qtxt)) continue;
    const pills = [...box.querySelectorAll(btnSel)].filter(x => vis(x) && norm(x.innerText||'').length && norm(x.innerText||'').length <= 30);
    const picked = pills.some(x => x.getAttribute('aria-pressed')==='true' || x.getAttribute('aria-selected')==='true' || /selected|active|checked/.test(x.className||''));
    if (!picked) empty.push(qlabel(box.parentElement||box));
  }
  // (b) custom radio/checkbox groups via [role=radiogroup]/[role=group] with no aria-checked member,
  //     or aria-checked elements where none is true.
  for (const grp of document.querySelectorAll('[role=radiogroup],[role=group]')) {
    if (!vis(grp)) continue;
    const opts = [...grp.querySelectorAll('[role=radio],[role=checkbox],[aria-checked]')];
    if (opts.length < 2) continue;
    const reqd = grp.getAttribute('aria-required')==='true' || /[*\u2731](?!\s*indicates)/i.test(norm(grp.innerText).slice(0,200));
    if (!reqd) continue;
    if (!opts.some(o => o.getAttribute('aria-checked')==='true')) empty.push(qlabel(grp));
  }
  const h1t = norm(((document.querySelector('h1')||{}).innerText)||'').toLowerCase();
  const keep = [...new Set(empty)].filter(t => {
    const nt = norm(t||'').toLowerCase();
    if (/^(create a job alert|apply for this job)/i.test(nt)) return false;
    // ANY branch that anchored on the page header names the job title — universal exit filter
    // (affirm mega3/40-42: three header variants from three different branches)
    if (h1t.length >= 8 && nt.includes(h1t.slice(0, 40))) return false;
    return true;
  });
  return JSON.stringify({adds: [...new Set(adds)], emptyReq: keep.slice(0,25)});
}"""


# label -> DOCUMENT-coordinate rect of {question text + its control} for doubt-crop verification.
# Token-overlap match to the tightest text element, then the nearest control at/below it — the
# same geometry a human uses. Returns '' when nothing matches.
_RECT_FOR_LABEL_JS = r"""(lab) => {
  const norm=s=>(s||'').toLowerCase().replace(/[^a-z0-9 ]+/g,' ').replace(/\s+/g,' ').trim();
  const toks=norm(lab).split(' ').filter(w=>w.length>3);
  if(!toks.length) return '';
  const all=[]; const walk=r=>{for(const e of r.querySelectorAll('*')){all.push(e); if(e.shadowRoot) walk(e.shadowRoot);}}; walk(document);
  let best=null,bestScore=0;
  for(const e of all){
    if(e.children.length>5) continue;
    const t=norm(e.innerText); if(!t||t.length>500) continue;
    let hit=0; for(const w of toks) if(t.includes(w)) hit++;
    if(hit < Math.max(1,Math.ceil(toks.length*0.5))) continue;
    const r=e.getBoundingClientRect(); if(!r.width||!r.height) continue;
    const score=hit/toks.length - t.length/3000;
    if(score>bestScore){ best=e; bestScore=score; }
  }
  if(!best) return '';
  const lr=best.getBoundingClientRect();
  let ctl=null,gap=1e9;
  for(const c of all){
    if(!c.matches||!c.matches('input,select,textarea,button,[role=combobox],[role=radiogroup],[role=checkbox],[role=radio],[aria-haspopup]')) continue;
    const cr=c.getBoundingClientRect(); if(!cr.width&&!cr.height) continue;
    const dv=cr.top-lr.top; if(dv<-10||dv>320) continue;
    const d=Math.max(0,dv)+Math.abs(cr.left-lr.left)*0.3;
    if(d<gap){gap=d;ctl=c;}
  }
  const cr=ctl?ctl.getBoundingClientRect():lr;
  const x1=Math.min(lr.left,cr.left), y1=Math.min(lr.top,cr.top);
  const x2=Math.max(lr.right,cr.right), y2=Math.max(lr.bottom,cr.bottom);
  return JSON.stringify({x:x1+window.scrollX, y:y1+window.scrollY, w:x2-x1, h:y2-y1});
}"""


async def _crop_check(session: Any, page: Any, label: str, expected: str = "") -> str:
    """Verify ONE field by its own full-resolution CLIP screenshot: 'yes' (answered) / 'no' /
    'unsure'. Uses browser-use's native take_screenshot(clip=...) (CDP captureScreenshot with a
    document-coordinate clip) — Chrome renders exactly that region at native resolution, immune
    to the full-page capture pitfalls (16384px height cap, lazy-rendered blanks below the fold,
    DPR coordinate drift). Machine version of the manual crop judgment that ran 5/5 where
    full-page/banded sampling flickered. 'unsure' on ANY failure — never flips a verdict alone."""
    try:
        import asyncio

        import oa_llm as _oa
        from vision_verify import _vlm

        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

        raw = await asyncio.wait_for(page.evaluate(_RECT_FOR_LABEL_JS, str(label)[:200]), timeout=6.0)
        if not raw:
            return "unsure"
        r = json.loads(raw)
        pad = 30
        clip = {
            "x": max(0, int(r["x"]) - pad),
            "y": max(0, int(r["y"]) - pad),
            "width": max(60, int(r["w"]) + pad * 2),
            "height": max(40, int(r["h"]) + pad * 3),
        }
        shot = await asyncio.wait_for(session.take_screenshot(full_page=True, clip=clip), timeout=12.0)
        crop_png = base64.b64decode(shot) if isinstance(shot, str) else shot
        if not crop_png:
            return "unsure"
        msg = UserMessage(
            content=[
                ContentPartTextParam(
                    type="text",
                    text=(
                        "This is a cropped region of a job application form showing ONE field "
                        "(its label and its control). Is this field ANSWERED — a value typed, an "
                        "option selected, or a file attached? An empty box, a choice pair with "
                        "neither picked, a bare placeholder like 'Select', or an empty upload "
                        "area means NOT answered. The field's own label or placeholder text "
                        "re-printed inside the control does NOT count as an answer. "
                        "EXCEPTION — a CONDITIONAL field: if the label says it only applies when a "
                        "previous answer was a certain way (for example it begins 'If yes,…' or 'If "
                        "applicable,…') and there is no sign that condition was met, its being empty "
                        "is CORRECT — answer 'yes'."
                        + (
                            f" The value we intended is roughly: {expected[:120]!r} — if the "
                            "control clearly shows a DIFFERENT unrelated value, answer 'no'."
                            if expected
                            else ""
                        )
                        + ' Reply STRICT JSON {"answer": "yes"|"no"|"unsure"}.'
                    ),
                ),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(
                        url=f"data:image/png;base64,{base64.b64encode(crop_png).decode()}",
                        detail="high",
                        media_type="image/png",
                    ),
                ),
            ]
        )
        res = await _oa.resilient_vlm([msg], primary=_vlm())
        raw2 = str(getattr(res, "completion", res) or "").lower()
        if '"answer": "yes"' in raw2 or '"answer":"yes"' in raw2:
            return "yes"
        if '"answer": "no"' in raw2 or '"answer":"no"' in raw2:
            return "no"
        return "unsure"
    except Exception:
        return "unsure"


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
    // keep the flag only when the nearby control is genuinely EMPTY in the DOM — the VLM reads
    // gray-rendered values as placeholders and re-flags filled fields (teamtailor Prénom/E-mail).
    const controlEmpty = (c) => {
      if (c.tagName === 'SELECT') { const o = c.options[c.selectedIndex]; return !o || o.value === ''; }
      if (c.type === 'radio' || c.type === 'checkbox') {
        const g = c.name ? [...document.querySelectorAll('input[name="'+CSS.escape(c.name)+'"]')] : [c];
        return !g.some(x => x.checked);
      }
      return !nrm(c.value);
    };
    return els.some(e => { let p = e;
      for (let i = 0; i < 5 && p; i++) {
        const cs = p.querySelectorAll('input:not([type=hidden]),select,textarea');
        if (cs.length) return [...cs].every(controlEmpty);
        if (p.querySelector('[role=combobox],[role=radiogroup],[role=listbox]')) return true;
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


# AGENT-CORRUPTION REPAIR: restore main-loop-committed values the escalation agent overwrote.
# STRICT label identity only (label[for] / label-wrapped / aria-labelledby) — repairing a
# token-guessed box could itself clobber a sibling, which is the exact disease being cured.
# Plain text inputs/textareas only: a combobox filter box or select needs the ladder, not a set.
_REPAIR_JS_TMPL = r"""(() => {
  const pairs = __PAIRS__;
  const nrm = s => (s||'').replace(/\s+/g,' ').trim();
  const toks = s => nrm(s).toLowerCase().replace(/[*:]/g,'').split(' ').filter(w=>w.length>1);
  const isPlainText = c => {
    if (!c) return false;
    if (c.tagName === 'TEXTAREA') return true;
    if (c.tagName !== 'INPUT') return false;
    const ty = (c.type||'text').toLowerCase();
    if (!['text','email','url','tel','search',''].includes(ty)) return false;
    if ((c.getAttribute('role')||'') === 'combobox') return false;
    const aa = c.getAttribute('aria-autocomplete');
    if (aa && aa !== 'none') return false;
    return true;
  };
  const controlFor = want => {
    for (const L of document.querySelectorAll('label')) {
      const T = toks(L.innerText);
      if (!T.length || T.length > want.length + 3) continue;
      const hit = want.filter(t => T.includes(t)).length;
      if (hit < Math.max(1, Math.ceil(want.length * 0.8))) continue;
      let c = L.htmlFor ? document.getElementById(L.htmlFor) : null;
      if (!c) c = L.querySelector('input,textarea');
      if (!c && L.id) c = document.querySelector('input[aria-labelledby~="'+L.id+'"],textarea[aria-labelledby~="'+L.id+'"]');
      if (c) return c;
    }
    return null;
  };
  const setVal = (c, v) => {
    const proto = c.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(c, v);
    c.dispatchEvent(new Event('input', {bubbles: true}));
    c.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const repaired = [];
  for (const [lab, val] of pairs) {
    if (!nrm(val)) continue;
    const c = controlFor(toks(lab));
    if (!isPlainText(c)) continue;
    if (nrm(c.value).toLowerCase() === nrm(val).toLowerCase()) continue;
    setVal(c, val);
    repaired.push(lab);
  }
  return JSON.stringify(repaired);
})()"""


async def repair_overwritten(session: Any, page: Any, committed_by_label: dict) -> list[str]:
    """Re-assert every ledger-committed value on its STRICTLY label-bound plain-text control;
    returns the labels whose live value had been changed (robinhood mega/16: the escalation
    agent typed 'Cisgender man' over First Name and an office city over LinkedIn AFTER both
    were committed+verified — post-verify corruption the ledger cannot see). [] on failure."""
    import json

    with contextlib.suppress(Exception):
        js = _REPAIR_JS_TMPL.replace("__PAIRS__", json.dumps([[k, v] for k, v in committed_by_label.items()]))
        return json.loads(await page.evaluate(js))
    return []


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
        _PROMPT = (
            # principle + one example each, NOT a rule taxonomy (user: keep prompts
            # generic and let the model decide; enumerated ignore-lists are the same
            # anti-pattern as regex and break on the next tenant/language).
            "This is a section of a job application form AFTER automated filling. List the "
            "REQUIRED questions a candidate would still have to answer before submitting: empty "
            "text boxes, dropdowns still on a placeholder, choice pairs with neither "
            "option selected, empty upload areas. Judge by what the form itself "
            "communicates, in whatever language/convention it uses: only count a field "
            "the form VISIBLY marks as required (for example an asterisk by its label); "
            "an empty but unmarked field is not a finding. Only count questions that are "
            "part of the application form a candidate fills in — for example, a footer "
            "banner asking 'Do you already work here?' with a login button is page "
            "furniture, not a form question. Reply ONLY a STRICT JSON array of their "
            "labels, [] if nothing required remains."
        )
        # BAND-TILED verification (the resolution fix): a 5000px-tall page squeezed into one
        # image budget leaves each form row a few pixels tall — the model physically cannot see
        # an unticked box (hibob resume false-green: the same VLM caught it instantly on a CROP).
        # Slice into <=1100px bands (120px overlap so a field on a seam appears whole in one
        # band), ask the SAME verification question per band, union the answers.
        bands: list[bytes] = [png]
        with contextlib.suppress(Exception):
            import io

            from PIL import Image

            im = Image.open(io.BytesIO(png))
            if im.height > 1400:
                bands = []
                step, ov = 1100, 120
                y = 0
                while y < im.height and len(bands) < 8:
                    buf = io.BytesIO()
                    im.crop((0, y, im.width, min(y + step + ov, im.height))).save(buf, format="PNG")
                    bands.append(buf.getvalue())
                    y += step
        out: list[str] = []
        for band in bands:
            msg = UserMessage(
                content=[
                    ContentPartTextParam(type="text", text=_PROMPT),
                    ContentPartImageParam(
                        type="image_url",
                        image_url=ImageURL(
                            url=f"data:image/png;base64,{base64.b64encode(band).decode()}",
                            # high + banded: full per-widget resolution (teamtailor tt7 missed an
                            # unselected required radio pair at full-page scale even on high).
                            detail="high",
                            media_type="image/png",
                        ),
                    ),
                ]
            )
            with contextlib.suppress(Exception):
                res = await _oa.resilient_vlm([msg], primary=_vlm())
                for lab in _parse_str_list(str(getattr(res, "completion", res) or "[]")):
                    out.append(lab)
        # NORMALIZED dedup: bands overlap 120px so the same question surfaces twice with
        # whitespace/invisible-char drift ('Can you legally work in Europe?*' flagged twice,
        # mega/28) — exact-string `in` missed it. Key on collapsed lowercase.
        seen_keys: set = set()
        deduped: list[str] = []
        for lab in out:
            k = " ".join(str(lab).split()).lower().strip(" *\u2731")
            if k and k not in seen_keys:
                seen_keys.add(k)
                deduped.append(lab)
        return deduped[:15]
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


async def retry_missing(
    session: Any, page: Any, profile: dict, resume: str | None, llm: Any, missing: list[str],
    committed_by_label: dict | None = None,
) -> int:
    """RE-FILL fields the audit found still-empty (React re-render wiped a committed value —
    the workable class). Re-discover the live DOM, match each still-empty REQUIRED field by
    label, and re-commit. Returns how many got re-filled. Generic.

    CORRUPTION GUARD: prefer the value we ALREADY committed to this field (keyed by label) over a
    fresh out-of-context re-map — re-mapping an isolated 'First Name' next to a location mismapped
    it to 'San Francisco' and OVERWROTE the good 'Jordan' (figma). Also skip a field whose LIVE
    value already matches what we committed (the VLM false-flagged a correctly-filled field)."""
    import oa_observe_act as oa
    from oa_discover import discover_fields
    from oa_singlepage import _field_dict

    committed_by_label = committed_by_label or {}

    # token-overlap match: the audit carries the FULL question text while discovery truncates its
    # label — exact equality never matched long screening questions (robinhood 'Have you ever
    # worked…' stayed empty through 4 retries). >=50% of the audit label's tokens appearing in
    # the field's label+name counts as the same field.
    def _tok(s: str) -> set:
        return {w for w in _norm(s).split() if len(w) > 3}

    want = {_norm(m) for m in missing}
    want_toks = [t for t in (_tok(m) for m in missing) if t]
    refilled = 0
    with contextlib.suppress(Exception):
        fields = []
        for f in await discover_fields(page):
            if _norm(f.label) in want or _norm(f.name) in want:
                fields.append(f)
                continue
            ft = _tok(str(f.label or "") + " " + str(f.name or ""))
            if any(len(wt & ft) >= max(2, len(wt) // 2) for wt in want_toks):
                fields.append(f)
        if not fields:
            return 0

        def _prior(f: Any) -> str:
            # the value we already committed to this field, matched by label (exact then token)
            lab = _norm(f.label or "")
            if lab in {_norm(k): v for k, v in committed_by_label.items()}:
                return {_norm(k): v for k, v in committed_by_label.items()}[lab]
            ftok = _tok(str(f.label or ""))
            for k, v in committed_by_label.items():
                kt = _tok(k)
                if kt and len(kt & ftok) >= max(2, len(kt) // 2):
                    return v
            return ""

        # only re-map fields we have NO prior committed value for (avoids the out-of-context mismap)
        no_prior = [f for f in fields if f.needs_map and not _prior(f)]
        mapped = await eng.map_fields(llm, no_prior, profile, "") if no_prior else {}
        for f in fields:
            # PRIOR committed value wins over a fresh re-map (idempotent re-type of the KNOWN-good
            # value; even if the field was already correct, re-typing 'Jordan' can't corrupt it —
            # whereas re-mapping produced 'San Francisco').
            value = _prior(f) or eng._resolve(f, mapped, resume)[0]
            if not (value or "").strip():
                continue
            # PHONE FORMAT VARIANT (doordash mega3/16: '+1 415 555 0142' committed and
            # dom-verified, but the tenant mask underlines it red and vision flags it on all
            # five doordash forms). Being IN this retry means the judge already rejected the
            # international form once — try the national digits instead.
            if "phone" in _norm(f.label or "") and value.replace(" ", "").startswith("+1"):
                _digits = "".join(ch for ch in value if ch.isdigit())
                if len(_digits) == 11 and _digits.startswith("1"):
                    value = _digits[1:]
            fd = _field_dict(f, value, resume=resume, llm=llm, adapter=None, page=page)
            with contextlib.suppress(Exception):
                out = await oa.observe_act(session, fd)
                if out == oa.DONE:
                    refilled += 1
    if refilled:
        print(f"   [complete] retry re-filled {refilled} wiped/empty required field(s)")
    return refilled


async def _section_has_entry(session: Any, page: Any, section: str) -> bool:
    """True when a COMMITTED entry (a saved card / row with actual content) renders under the
    named repeater section — as opposed to just its heading + empty 'Add' button. Generic: find
    the section heading, then look below it (before the next section heading) for a block carrying
    real profile-shaped text (a company/school/title line), NOT only the 'Add' affordance. Best-
    effort False on read failure — an unverifiable section must not be recorded as filled."""
    with contextlib.suppress(Exception):
        got = await page.evaluate(
            "(sec) => { const norm=s=>(s||'').replace(/\\s+/g,' ').trim();"
            " const secl=sec.toLowerCase();"
            " const heads=[...document.querySelectorAll('h1,h2,h3,h4,legend,label,div,span,p')]"
            "   .filter(e=>{const t=norm(e.innerText).toLowerCase(); return t.length<40 && t.includes(secl.split(' ')[0]);});"
            " for(const h of heads){ const hr=h.getBoundingClientRect(); if(!hr.width) continue;"
            "   const near=[...document.querySelectorAll('*')].filter(e=>{const r=e.getBoundingClientRect();"
            "     return r.top>hr.top && r.top<hr.top+380 && r.width>120 && e.children.length<12;});"
            "   for(const e of near){ const t=norm(e.innerText);"
            "     if(t.length>12 && !/^\\+?\\s*add\\b/i.test(t) && !/^(add|cancel|save|delete|edit)$/i.test(t)) return true; } }"
            " return false; }",
            str(section)[:60],
        )
        return bool(got)
    return False


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

        # SETTLE-UNION: the late row is created ASYNCHRONOUSLY (breezy parses the uploaded resume
        # server-side, then renders a Work History row) — a single discover races it. Poll a few
        # times and union what appears.
        import asyncio as _aio

        seen: dict = {}
        for wait_s in (0.0, 2.0, 4.0):
            if wait_s:
                await _aio.sleep(wait_s)
            with contextlib.suppress(Exception):
                for f in await discover_fields(page):
                    if str(f.name) not in filled_names:
                        seen.setdefault(str(f.name), f)
        orphans = list(seen.values())
        # DIAGNOSTIC (anthropic mega/25: audit saw empty References/Logistics, orphan saw []):
        # raw control count vs what discovery kept — separates "not in DOM yet" (raw small)
        # from "discovery gate filtered it" (raw big, orphans 0) without a live probe.
        with contextlib.suppress(Exception):
            raw = await page.evaluate(
                "document.querySelectorAll('input,textarea,select,[role=combobox],[role=checkbox],[role=radio]').length"
            )
            print(f"   [complete] orphan: raw controls in DOM={raw}, already-filled={len(filled_names)}, new={len(orphans)}")
        print(f"   [complete] orphan candidates: {[str(f.name)[:24] for f in orphans][:8]}")
        if not orphans:
            return 0
        # SIGNATURE BUG (found mega/84: orphan candidates ['company','summary',…] yet the row
        # stayed empty): map_fields is (llm, fields, profile, title, …) — the old call passed
        # (orphans, profile, llm=llm), raised TypeError, and the pass-wide suppress swallowed it,
        # so the WHOLE orphan mapping had been silently dead since it shipped. Also unwrap the
        # FieldFill — the loop compared/typed the OBJECT, not its .value.
        mapping = await eng.map_fields(llm, orphans, profile, "") if llm is not None else {}
        for f in orphans:
            _fill = (mapping or {}).get(f.name)
            val = (getattr(_fill, "value", None) or "").strip()
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
    filled_names: set | None = None, required_labels: list | None = None, committed_by_label: dict | None = None,
    form_url: str = "",
) -> dict:
    """Audit the form for unfilled repeater sections + empty required fields; fill repeaters via the
    proven agent_fill_section and RE-FILL wiped required fields (retry). Returns
    {complete, missing_required, sections_filled, sections_skipped, retried}. Never raises."""
    verdict: dict = {
        "complete": True, "missing_required": [], "sections_filled": [], "sections_skipped": [], "retried": 0,
    }
    # prefer the runner's PRE-FILL capture: a mid-fill click can navigate away before
    # complete() even starts (samsara mega3/28) and the entry snapshot would already be wrong.
    _form_url = form_url or ""
    if not _form_url:
        with contextlib.suppress(Exception):
            _form_url = await page.get_url()
    # immediate drift check at ENTRY too — the audit/retry/repeater phases all judge the page.
    with contextlib.suppress(Exception):
        _now0 = await page.get_url()
        if _form_url and _now0 and _now0.split("#")[0] != _form_url.split("#")[0]:
            print(f"   [complete] entry drift {_now0[:60]} -> back to form")
            await session.navigate_to(_form_url)
            import asyncio as _aio

            await _aio.sleep(2.0)
            page = await session.must_get_current_page()
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
                session, page, profile, resume, llm, verdict["missing_required"], committed_by_label
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
            # token-overlap dedup: the VLM says 'Work Experience' while the fill ledger says
            # 'experience' — the old contiguous-substring check never matched, so a JUST-FILLED
            # section got re-flagged skipped and complete stayed False forever (breezy).
            filled_tokens = {w for k in verdict["sections_filled"] for w in str(k).lower().split() if len(w) > 3}
            sections = [
                s for s in await _vlm_unfilled_sections(session)
                if _profile_has(profile, s)
                and not ({w for w in s.lower().split() if len(w) > 3} & filled_tokens)
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
                # VERIFY the agent actually SAVED an entry — never trust its self-report. A repeater
                # section is 'filled' only if a committed entry card now renders (hibob 'Add' clicked
                # -> 0 rows, the agent then CLAIMED success while the section stayed empty — the
                # 'Experience 没加' the human judge caught). No visible entry -> skipped, honest.
                if await _section_has_entry(session, page, sec):
                    verdict["sections_filled"].append(sec)
                    print(f"   [complete] agent-filled repeater section '{sec}' (entry verified)")
                else:
                    verdict["sections_skipped"].append(sec)
                    verdict["complete"] = False
                    print(f"   [complete] agent claimed '{sec}' but NO entry card rendered -> skipped")
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
                # CORE PROFILE FACTS: the agent answered location/eligibility questions BLIND —
                # gitlab mega/33 'Where are you currently based?' -> 'Canada' (candidate is in SF),
                # agility mega/24 'authorized to work in the US?' -> 'No'. Facts, not guesses.
                _facts = (
                    f"CANDIDATE FACTS (answer FROM these, never guess): "
                    f"location: {profile.get('city','')}, {profile.get('state','')}, {profile.get('country','')}; "
                    f"authorized to work in the US: {'Yes' if profile.get('authorized_to_work_us') else 'No'}; "
                    f"requires visa sponsorship: {'Yes' if profile.get('requires_sponsorship') else 'No'}; "
                    f"willing to relocate: {'Yes' if profile.get('willing_to_relocate') else 'No'}; "
                    f"notice period: {profile.get('notice_period','2 weeks')}; "
                    f"earliest start: {profile.get('available_start_date','')}; "
                    f"desired salary: {profile.get('desired_salary','')} USD/year (a salary question "
                    f"in another currency gets the approximate converted NUMBER, never prose)."
                )
                # EXACT SCOPE: 'answer EVERY remaining required question' let the agent freelance
                # into OPTIONAL demographics it should never touch (scaleai mega/35: optional
                # Disability Status — engine honestly SKIPped it — agent picked 'Yes, I have a
                # disability', the first option). The audit's own missing_required list IS the
                # scope; everything else is off-limits.
                _scope = json.dumps([str(x)[:90] for x in verdict["missing_required"][:12]])
                instr = (
                    f"Answer ONLY these required questions, exactly this list and NOTHING else: "
                    f"{_scope}. Every other field on the page is off-limits — especially optional "
                    f"demographic questions and fields that already show a value. {_facts} "
                    "For yes/no eligibility thresholds (18 or older -> Yes; meets a stated "
                    "years-of-experience threshold -> Yes). POLICY ACKNOWLEDGEMENTS (a question asking "
                    "you to confirm you read/understood/agree to a policy or guideline) -> the "
                    "affirmative option ('Yes' / 'I acknowledge' / 'I agree'). For skill self-ratings "
                    f"(1-5), rate from this candidate's background — skills: {skills}; senior engineer "
                    "with ~5 years — rate core/listed skills 4-5, unfamiliar ones 2-3. Pick the closest "
                    "option in every dropdown and select the option by its TEXT, never type its list "
                    "number. SANCTIONED DEFAULTS (never invent a disqualifying or "
                    "protected status): veteran status -> 'I am not a protected veteran'; disability -> "
                    "'No, I don't have a disability'; government official / previously worked for this "
                    "company / conflicts of interest -> No; voluntary demographics (gender, race, LGBTQ) "
                    "-> 'I don't wish to answer' / 'Decline to self identify'. NEVER type into a field "
                    "that already shows a value. Do NOT submit; do NOT navigate away."
                )
                await eng.agent_fill_section(
                    session, page, section="Remaining required questions", instructions=instr, resume=resume, max_steps=18
                )
                verdict["agent_answered_required"] = True
                with contextlib.suppress(Exception):
                    verdict["missing_required"] = (await audit(session, page)).get("emptyReq", [])
        # AGENT-CORRUPTION REPAIR: retry_missing / agent_fill_section act AFTER the main loop's
        # verify, so they can silently overwrite committed fields (mega/16: the remaining-required
        # agent typed 'Cisgender man' over the committed 'Jordan' in First Name and an office city
        # over LinkedIn — post-verify corruption the ledger cannot see). Re-assert the ledger's
        # committed values on strictly label-bound text controls BEFORE the visual gate judges the
        # page; record what changed (audit trail for the human reviewer).
        if committed_by_label:
            with contextlib.suppress(Exception):
                fixed = await repair_overwritten(session, page, committed_by_label)
                if fixed:
                    verdict["repaired_overwritten"] = fixed
                    print(f"   [complete] repaired agent-overwritten fields: {fixed}")
        # DRIFT GUARD (samsara mega2/49-51: 18-19 fields filled, then something clicked a nav
        # link and every judge — audit, vision, screenshot — ran on /company/belonging instead
        # of the form): if the URL changed since complete() started, navigate BACK before
        # judging.
        with contextlib.suppress(Exception):
            _now_url = await page.get_url()
            if _form_url and _now_url and _now_url.split("#")[0] != _form_url.split("#")[0]:
                print(f"   [complete] page drifted to {_now_url[:60]} -> navigating back to the form")
                await session.navigate_to(_form_url)
                import asyncio as _aio

                await _aio.sleep(2.0)
                page = await session.must_get_current_page()
        # SETTLE BEFORE JUDGING: close any dropdown menu a fill/retry left open — an open
        # overlay at judge-time hides committed values from the vision gate and the crops
        # (mega/28 final screenshot still had a menu hanging open). Escape closes menus;
        # committed values survive it.
        with contextlib.suppress(Exception):
            import oa_action as _act

            await _act.press_key(session, "Escape")
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
                    # CLOSE THE LOOP: the VLM is our 'really filled?' judge — when it flags a field
                    # the DOM audit thought was fine, that is a WRONG-VALUE signal (rippling's
                    # CV-parse clobbered a correct email with a garbage partial AFTER we filled and
                    # verified). Re-fill exactly those labels, then re-ask vision ONCE. The audit
                    # alone is blind to wrong-but-non-empty; this is the fix for the wrong-value class.
                    if llm is not None:
                        with contextlib.suppress(Exception):
                            fixed = await retry_missing(session, page, profile, resume, llm, seen, committed_by_label)
                            if fixed:
                                verdict["revalued"] = fixed
                                seen2 = await _vlm_unanswered_required(session)
                                with contextlib.suppress(Exception):
                                    if seen2:
                                        seen2 = json.loads(await page.evaluate(_NEAR_FIELD_FILTER_JS % json.dumps(seen2)))
                                verdict["visually_unanswered"] = seen2 or []
                                print(f"   [complete] revalued {fixed} field(s); vision now: {(seen2 or [])[:5]}")
                                # POST-REVALUE REPAIR (doordash mega3/17: the revalue pass wrote
                                # the location string into the PHONE box — repair ran before
                                # revalue and the corruption escaped every judge). Re-assert the
                                # ledger once more after any revalue write.
                                if committed_by_label:
                                    with contextlib.suppress(Exception):
                                        fixed2 = await repair_overwritten(session, page, committed_by_label)
                                        if fixed2:
                                            verdict.setdefault("repaired_overwritten", []).extend(fixed2)
                                            print(f"   [complete] post-revalue repair: {fixed2}")
        # DOUBT-CROP verification (the reliable-visual rung): a per-field crop at full resolution
        # judged 5/5 manually where full-page/banded sampling flickered (~50%). (a) every banded
        # flag is CONFIRMED by its own crop — a flag the crop sees as answered is a sampling
        # false-positive and drops; (b) a would-be GREEN gets its REQUIRED anchors spot-checked —
        # a crop that sees an anchor unanswered kills the false-green. 'unsure' never flips a
        # verdict on its own (flags stay, greens stay).
        with contextlib.suppress(Exception):
            flags = list(verdict.get("visually_unanswered") or [])
            def _committed_for(lab: str) -> str:
                lt = {w for w in str(lab).lower().replace("*", " ").split() if len(w) > 2}
                for k, v in (committed_by_label or {}).items():
                    kt = {w for w in str(k).lower().replace("*", " ").split() if len(w) > 2}
                    if kt and lt and len(kt & lt) >= max(1, len(kt) // 2):
                        return v
                return ""
            if flags:
                kept = []
                for lab in flags[:6]:
                    if await _crop_check(session, page, lab, (committed_by_label or {}).get(lab, "")) != "yes":
                        kept.append(lab)
                # DOM CORROBORATION (doordash mega3/16-19: filled + dom-verified fields held
                # hostage by a lone vision flag — the crop anchor lands on the vision-union
                # TWIN block, which renders nothing). A flag survives only if the DOM side
                # agrees something is wrong: either the audit still lists it as missing, or
                # there is NO committed value for that label. Vision alone tightens the
                # verdict only when the DOM cannot see the field at all.
                _missing_now = " ".join(str(x).lower() for x in verdict.get("missing_required") or [])
                kept2 = []
                for lab in kept:
                    lt = {w for w in str(lab).lower().replace("*", " ").split() if len(w) > 2}
                    in_missing = lt and len(lt & set(_missing_now.split())) >= max(1, len(lt) // 2)
                    if in_missing or not _committed_for(lab):
                        kept2.append(lab)
                if len(kept2) != len(kept):
                    print(f"   [complete] dom-corroboration dropped {len(kept) - len(kept2)} vision-only flag(s)")
                verdict["visually_unanswered"] = kept2
                if len(kept2) != len(flags):
                    print(f"   [complete] crop-confirm dropped {len(flags) - len(kept2)} banded false-positive(s)")
            if not verdict["missing_required"] and not verdict["sections_skipped"] and not verdict.get("visually_unanswered"):
                for lab in (required_labels or [])[:6]:
                    if await _crop_check(session, page, lab, (committed_by_label or {}).get(lab, "")) == "no":
                        # same DOM corroboration as the vision flags (flexport mega3/46: the
                        # anchor spot-check re-flagged a dom-verified 'No' the whole chain had
                        # just cleared — the crop anchor keeps landing on the vision-union twin)
                        if _committed_for(lab):
                            continue
                        verdict.setdefault("crop_flagged", []).append(lab)
                if verdict.get("crop_flagged"):
                    verdict["visually_unanswered"] = list(verdict["crop_flagged"])
                    print(f"   [complete] crop-anchor CAUGHT unanswered required: {verdict['crop_flagged'][:3]}")
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
        # DEEP walk (hibob renders the form in shadow DOM) collecting EVERY clickable's text —
        # then the LLM decides which one uploads a resume (principle + example, no regex list of
        # button labels: the next tenant says 'Attach CV' in another language and a list breaks).
        cands_json = await page.evaluate(
            "() => { const all=[]; const walk=(root)=>{ for(const e of root.querySelectorAll('*')){ all.push(e);"
            "   if(e.shadowRoot) walk(e.shadowRoot); } }; walk(document);"
            " const out=[]; for(const e of all){ if(!e.matches||!e.matches('button,a,[role=button],label')) continue;"
            "   const r=e.getBoundingClientRect(); if(r.width<8||r.height<8) continue;"
            "   const t=(e.innerText||e.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' ').slice(0,48);"
            "   if(t) out.push(t); }"
            " return JSON.stringify([...new Set(out)].slice(0,40)); }"
        )
        import json as _jj

        texts = _jj.loads(cands_json or "[]")
        chosen = ""
        if texts:
            with contextlib.suppress(Exception):
                import oa_llm as _oa
                from browser_use.llm.messages import UserMessage as _UM
                from vision_verify import _vlm as _v

                menu = "\n".join(f"{i}: {t}" for i, t in enumerate(texts))
                q = (
                    "A job application form needs the candidate's resume/CV uploaded. Which control "
                    "label below opens the file picker for that? (for example 'Add file' under an "
                    "'Upload your resume' heading — but judge by meaning, any language).\n"
                    f"{menu}\n"
                    'Reply STRICT JSON {"idx": <list number, or -1 if none>}.'
                )
                r = await _oa.resilient_vlm([_UM(content=q)], primary=_v())
                import re as _re3

                m3 = _re3.search(r'"idx"\s*:\s*(-?\d+)', str(getattr(r, "completion", r) or ""))
                i3 = int(m3.group(1)) if m3 else -1
                if 0 <= i3 < len(texts):
                    chosen = texts[i3]
        if not chosen:
            return False
        find_btn = await page.evaluate(
            "(want) => { const all=[]; const walk=(root)=>{ for(const e of root.querySelectorAll('*')){ all.push(e);"
            "   if(e.shadowRoot) walk(e.shadowRoot); } }; walk(document);"
            " const el=all.find(e=>{ if(!e.matches||!e.matches('button,a,[role=button],label')) return false;"
            "   const r=e.getBoundingClientRect(); if(r.width<8||r.height<8) return false;"
            "   return (e.innerText||e.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' ').slice(0,48)===want;});"
            " if(!el) return ''; el.scrollIntoView({block:'center'}); const r=el.getBoundingClientRect();"
            " return JSON.stringify({x:r.left+r.width/2,y:r.top+r.height/2}); }",
            chosen,
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
