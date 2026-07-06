"""oa_discover — live-DOM field discovery for the GENERIC lane (foreign forms / 官网).

The missing half of the no-adapter promise: known ATSs get their field list from a schema
API; an arbitrary company site has no schema. This enumerator reads the RENDERED page the
same way the Workday extract_step does — CONTROL-first (real input/textarea/select/role
structure decides the type) + LABEL-first (label text is only the human meaning the map
call reasons over) — never keyed on headings/titles (tenants rename & localize).

Returns eng.FormField rows ready for the standard pipeline (map_fields -> observe_act).

# ponytail ceilings (extend when failures.jsonl shows the need): lone consent checkboxes
# skipped; custom div-only widgets without input/role are invisible here (the VLM triage
# will still flag the page APPLICATION_FORM, so misses surface, not vanish).
"""

import contextlib
import json
from typing import Any

import ats_engine as eng

_ENUM_JS = r"""
() => {
  const seen = new Set(); const out = []; const radio = {}; const check = {}; let ckgid = 0;
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 4 && r.height > 4; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const labFor = el => {
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l && clean(l.innerText)) return clean(l.innerText); }
    const al = el.getAttribute && el.getAttribute('aria-label'); if (al) return clean(al);
    const lb = el.getAttribute && el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' ');
      if (clean(t)) return clean(t); }
    const anc = el.closest && el.closest('label'); if (anc && clean(anc.innerText)) return clean(anc.innerText);
    if (el.placeholder) return clean(el.placeholder);
    let p = el.parentElement, hops = 0;
    while (p && hops++ < 4) {
      const c = p.querySelector(':scope > label, :scope > legend, :scope > span, :scope > div, :scope > p');
      const own = c ? clean(c.innerText) : '';
      if (own && own.length > 1 && own.length < 400) return own.split('\n')[0].trim();
      p = p.parentElement;
    }
    return '';
  };
  const push = (name, label, type, source, options, required) => {
    label = clean(label); if (!label || label.length < 2) return;
    const key = (name || '') + '|' + label; if (seen.has(key)) return; seen.add(key);
    out.push({ name: name || label.toLowerCase().replace(/[^a-z0-9]+/g, '_').slice(0, 60),
               label: label.slice(0, 400), type, source, options, required: !!required });
  };
  for (const el of document.querySelectorAll('input, textarea, select, [role=combobox]')) {
    const tag = (el.tagName || '').toLowerCase(); const ty = (el.type || '').toLowerCase();
    // hidden file/checkbox/radio inputs AND hidden <select>s are the NORM (styled widget wrapping
    // a visually-hidden real control): setFileInputFiles / cdp_choose_option / selectedIndex
    // operate on them regardless of visibility. Dropping them made a required consent checkbox
    // (breezy gdprAgreement) and a required screening dropdown (robinhood 'worked here before?',
    // an old-boards hidden <select> under a custom UI) invisible to discovery while the audit
    // still flagged them. Everything else must be visible.
    // react-select's REAL <input> is ~1-3px wide (it grows as you type; the visible control is
    // the wrapper div) — a role=combobox / aria-autocomplete input is structurally a dropdown and
    // must bypass the width gate, same as hidden select/checkbox/radio (robinhood's required
    // 'worked here before?' screening combobox was 3.48px -> dropped -> never fillable).
    const isCombo = el.getAttribute && (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete'));
    if (tag !== 'select' && !isCombo && !['file', 'checkbox', 'radio'].includes(ty) && !vis(el)) continue;
    // type=search stays: duolingo's Location geocomplete is <input type=search> (mega3/22-27,
    // six runs) — the chrome-search case is already killed by the nav/header/[role=search]
    // ancestor check below.
    if (tag === 'input' && ['hidden', 'submit', 'button', 'image', 'reset'].includes(ty)) continue;
    if (el.closest('nav, header, footer, [role=search]')) continue;  // page chrome, not the form
    const req = el.required || (el.getAttribute && el.getAttribute('aria-required') === 'true');
    if (tag === 'select') {
      const opts = [...el.options].map(o => clean(o.text)).filter(t => t && !/^(select|choose|--)/i.test(t)).slice(0, 80);
      push(el.id || el.name, labFor(el), 'single_select', 'select', opts, req); continue;
    }
    if (ty === 'radio') { const g = el.name || 'radio';
      (radio[g] = radio[g] || { opts: [], el }).opts.push(labFor(el) || el.value); continue; }
    if (ty === 'checkbox') {
      // GROUP BY SHARED CONTAINER first, name attr second (ashby mega/37: a 'select all that
      // apply' race list gives every checkbox a UNIQUE name -> 9 singleton groups -> 9 lone
      // boolean fields -> the mapper hallucinated Yes per option label and 6 boxes got checked.
      // ONE question = ONE field: the nearest ancestor holding >=2 checkboxes IS the option
      // list; a genuine lone consent box finds no such container and keeps the lone branch.)
      let g = el.name || 'check';
      // a checkbox whose own label is a SENTENCE (a consent/acknowledgement clause) is its own
      // question — twilio mega3/34: two adjacent lone-consent boxes shared a section ancestor
      // and the container grouping merged them into one field, so only one got checked.
      const ownLab = labFor(el) || '';
      // FIELDSET is a hard question boundary: twilio renders TWO adjacent acknowledgements,
      // each its own <fieldset><legend>question</legend><input labeled 'Acknowledge'> — both
      // labels short, so only the structural boundary separates them (mega3/34-37).
      const fsBound = el.closest('fieldset,[role=group]');
      if (ownLab.length <= 60) {
        let p = el.parentElement, depth = 0;
        while (p && depth < 6) {
          if (fsBound && p !== fsBound && !fsBound.contains(p) && p.contains(fsBound)) break;
          if (p.tagName !== 'FORM' && p.querySelectorAll) {
            const boxes = [...p.querySelectorAll('input[type=checkbox]')];
            // group only when the co-located boxes look like OPTIONS (short labels), not
            // sibling consent sentences
            if (boxes.length >= 2 && boxes.length <= 40 && boxes.every(b => (labFor(b)||'').length <= 60)) {
              // prefer a SHARED name attr as the group id — it is DOM-REF RESOLVABLE, so locate
              // binds the real checkbox (classify_intrinsic -> INTRINSIC_CHECKBOX -> _s_choice,
              // check the box by label). A synthetic ckgrpN is unresolvable, forcing the
              // VLM-marks path which mis-bound a neighbouring combobox and routed the whole
              // 29-checkbox group to a dropdown that never opened (stripe mega4 anticipate-
              // countries escalate — live-CDP-confirmed: name='question_67165646[]', label.click
              // checks 'US' cleanly). Live-proven fill primitive; the miss was pure identity.
              const nm = boxes[0].name;
              const shared = nm && boxes.every(b => b.name === nm);
              g = shared ? nm : (p.__oaGid || (p.__oaGid = 'ckgrp' + (++ckgid)));
              break;
            }
            if (boxes.length >= 2) break;  // mixed container: stop climbing, stay lone
          }
          p = p.parentElement; depth++;
        }
      }
      (check[g] = check[g] || { opts: [], el }).opts.push(labFor(el) || el.value); continue; }
    if (ty === 'file') { push(el.id || el.name, labFor(el) || 'Resume', 'input_file', 'file', null, req); continue; }
    if (tag === 'textarea') { push(el.id || el.name, labFor(el), 'textarea', 'open_ended', null, req); continue; }
    // SELF-LABEL guard: a custom select often exposes its own display text ('Select', 'Search',
    // an error hint) as its nearest label — the QUESTION lives on an ancestor (rippling: mapper
    // got label='Select' -> no value -> required select left empty). Identity comparison only,
    // then climb for the first line that is not the widget's own. 'own' = innerText for
    // button-style, PLACEHOLDER for input-style widgets.
    // GEOMETRIC label: the nearest text sitting DIRECTLY ABOVE the widget, horizontally
    // overlapping it. This is how a human associates a label with a field — and the ONLY way
    // for widgets (rippling) whose question text is a CSS-positioned sibling, NOT a DOM ancestor
    // (7 ancestor levels measured empty). Bounded scan, cheap, no VLM.
    const geomLabel = (e) => {
      const r = e.getBoundingClientRect(); if (!r.width) return '';
      let best = '', bestGap = 140;
      for (const q of document.querySelectorAll('label,legend,p,span,div,h1,h2,h3,h4,h5,h6')) {
        if (q.contains(e) || e.contains(q)) continue;
        // own text only (no descendant-heavy containers) and a real sentence/word
        const direct = [...q.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ');
        const t = clean(direct); if (t.length < 2 || t.length > 400) continue;
        if (/^(select|search|choose|\-\-|\+?\d)/i.test(t)) continue;  // placeholders/values, not questions
        const qr = q.getBoundingClientRect(); if (!qr.width) continue;
        const gap = r.top - qr.bottom;                       // q sits ABOVE e
        const overlap = Math.min(r.right, qr.right) - Math.max(r.left, qr.left);
        if (gap >= -4 && gap < bestGap && overlap > Math.min(r.width, qr.width) * 0.3) { best = t; bestGap = gap; }
      }
      return best;
    };
    const questionLabel = (e, lab) => {
      const own = clean(e.innerText) || clean(e.placeholder) || clean(e.value) || '';
      if (lab && lab !== own && !(own && (own.startsWith(lab) || lab.startsWith(own)))) return lab;
      let p = e.parentElement, h = 0;
      while (p && h++ < 5) {
        for (const line of (p.innerText || '').split('\n')) {
          const t = clean(line);
          if (t && t.length > 1 && t.length < 400 && t !== own && t !== lab && !own.includes(t)) return t;
        }
        p = p.parentElement;
      }
      return geomLabel(e) || lab;  // DOM-climb failed -> geometry (rippling's positioned labels)
    };
    if (el.getAttribute && el.getAttribute('role') === 'combobox') {
      push(el.id || el.getAttribute('name'), questionLabel(el, labFor(el)), 'combobox', 'select', [], req); continue;
    }
    // combo-ISH inputs (rippling's hub-location: an <input placeholder='Select'> with dropdown
    // ARIA but no role=combobox) — STRUCTURAL signals only, so a plain text input whose
    // placeholder IS its true label (breezy 'Full Name') is never touched.
    const comboish = tag === 'input' && (el.getAttribute('aria-haspopup') || el.getAttribute('aria-expanded') !== null
      || el.getAttribute('aria-autocomplete') || el.readOnly || el.closest('[role=combobox],[aria-haspopup=listbox]'));
    if (comboish) {
      push(el.id || el.name, questionLabel(el, labFor(el)), 'combobox', 'select', [], req); continue;
    }
    push(el.id || el.name, labFor(el), ty || 'text', 'input_text', null, req);
  }
  const groupLabel = (el, fallback, optTexts) => {
    const fs = el.closest('fieldset'); const leg = fs && fs.querySelector('legend');
    if (leg && clean(leg.innerText)) return clean(leg.innerText);
    // walk up: the question text is the container's text MINUS its options. The 'first long
    // line' heuristic must skip lines that ARE an option (sierra mega4/46: sentence-length
    // options — "Yes, I am based in one of Sierra's office locations." became the group's
    // label, the real question never became a field, and the mapper had nothing to answer).
    const optSet = new Set((optTexts || []).map(t => clean(t).toLowerCase()));
    let p = el.parentElement, hops = 0;
    while (p && hops++ < 5) {
      const full = clean(p.innerText || '');
      if (full.length > 15 && full.length < 400) {
        const line = full.split('\n').map(clean).find(t =>
          t.length > 10 && !/^(yes|no)$/i.test(t) && !optSet.has(t.toLowerCase()));
        if (line) return line.slice(0, 400);
      }
      p = p.parentElement;
    }
    return fallback;
  };
  // ARIA DISCLOSURE SELECT (duolingo mega4/18-23 false-greens: the whole screener column of
  // 'Select...' widgets has NO input/select/[role=combobox] — just a <button
  // aria-haspopup=listbox aria-controls=...> inside a role=group that carries the label and
  // aria-required). Standard ARIA pattern, generic. Skipped when the group already holds a
  // real control (ashby renders the button NEXT TO its combo input — one question, one field).
  for (const b of document.querySelectorAll('button[aria-haspopup="listbox"],[role=button][aria-haspopup="listbox"]')) {
    if (b.closest('nav, header, footer, [role=search]')) continue;
    const grp = b.closest('[role=group],[aria-required]') || b.parentElement;
    if (grp && grp.querySelector('input:not([type=hidden]), select, textarea, [role=combobox]')) continue;
    let lab = '';
    if (grp && grp.id) { const l = document.querySelector('label[for="' + CSS.escape(grp.id) + '"]'); if (l) lab = clean(l.innerText); }
    if (!lab) lab = labFor(b);
    const rq = !!(grp && grp.getAttribute('aria-required') === 'true') || /[*✱]\s*$/.test(lab || '');
    push((grp && grp.id) || b.getAttribute('aria-controls') || lab, lab, 'single_select', 'select', null, rq);
  }
  // required for a GROUP: any member input carries required/aria-required, the group's
  // question line carries a required star, OR a required-indicator sits in the group's own
  // DOM subtree. The trailing-star-only test (stripe mega4/6) missed 1password mega4/1
  // 'Do you have experience leading people managers?* Yes No' — the star sits AFTER the '?'
  // and BEFORE the appended option text, so it is not trailing. Test: a star right after the
  // question (before options), any aria-required member, or a required-role star element in
  // the enclosing fieldset/[role=group] (a <span class=required>* / [aria-hidden] asterisk).
  const starMark = s => {
    const t = clean(s.innerText || '');
    return (t === '*' || t === '✱' || /(^|[^a-z])required([^a-z]|$)/i.test(s.className || '')
            || s.getAttribute('aria-label') === 'required');
  };
  const grpReq = (el, lab) => {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    const L = lab || '';
    if (/[*✱]\s*$/.test(L)) return true;
    if (/\?\s*[*✱]/.test(L)) return true;              // star right after the question mark
    // CARD-SCOPED STAR (1password mega4/1 ashby pills: the required '*' is a separate <span>
    // next to the question heading, dropped from the captured label, and the pills are NOT in a
    // fieldset). Find the nearest ancestor that is THIS question's card — the smallest one whose
    // text contains the question but does NOT absorb a sibling group (its checkbox/pill count
    // stays == this group's) — then look for a required-marker there. Scoping to the card is what
    // stops a neighbour's star being borrowed.
    const qhead = clean(L).replace(/\s+(yes|no)\b.*$/i, '').slice(0, 40).toLowerCase();
    const myBoxes = el.name ? [...(el.form||document).querySelectorAll('input[type=checkbox][name="'+CSS.escape(el.name)+'"]')].length : 1;
    let p = el.parentElement, card = null;
    for (let i = 0; i < 6 && p; i++) {
      if (p.getAttribute && p.getAttribute('aria-required') === 'true') return true;
      const txt = clean(p.innerText || '').toLowerCase();
      const cbCount = p.querySelectorAll ? p.querySelectorAll('input[type=checkbox],[role=checkbox],button').length : 0;
      // the card = contains the question heading AND has not yet absorbed a second group's controls
      if (qhead && txt.includes(qhead)) {
        if (cbCount > Math.max(myBoxes, 2) + 1) break;   // absorbed a neighbour -> too far
        card = p;
      }
      p = p.parentElement;
    }
    if (card) {
      const mk = [...card.querySelectorAll('span,i,em,abbr,sup,label')].find(
        s => starMark(s) && !s.closest('button,[role=option],[role=radio],[role=checkbox]'));
      if (mk) return true;
    }
    return false;
  };
  for (const [g, v] of Object.entries(radio)) {
    const rlab = groupLabel(v.el, g, v.opts);
    push(g, rlab, 'radio', 'select', v.opts.slice(0, 30), grpReq(v.el, rlab));
  }
  for (const [g, v] of Object.entries(check)) {
    if (v.opts.length < 2) {
      // LONE checkbox (consent / acknowledge): its own boolean field. The v1 skip made required
      // consent boxes invisible to discovery while the audit flagged them (teamtailor
      // candidate[consent_given]). Label: group label, else the box's own text, else the name
      // attr (the consent sentence often exceeds labFor's length cap).
      push(g, groupLabel(v.el, g) || v.opts[0] || g, 'checkbox', 'select', ['Yes', 'No'], v.el.required);
      continue;
    }
    const clab = groupLabel(v.el, g, v.opts);
    push(g, clab, 'multi_select', 'select', v.opts.slice(0, 40), grpReq(v.el, clab));
  }
  // DISCOVERY-BLIND GENERA (mega4 green-audit: 9 widget kinds neither the input/select scan
  // nor the audit sees, so a required one passes as green). Each is a real ATS control with
  // no <input>/<select>/[role=combobox]; detect by ARIA/structural signal, route to the closest
  // existing lane. Generic — ARIA roles, not per-ATS classes.
  const ident = el => el.id || (el.getAttribute && el.getAttribute('name')) ||
    (labFor(el) || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').slice(0, 60);
  for (const el of document.querySelectorAll(
    '[contenteditable="true"],[role=textbox],[role=switch],[role=slider],input[type=range],[role=listbox]:not(select)'
  )) {
    if (el.closest('nav, header, footer, [role=search]')) continue;
    if (!vis(el)) continue;
    // skip if already emitted (a role=textbox that is really a captured input, etc.)
    const role = (el.getAttribute('role') || '').toLowerCase();
    const ce = el.getAttribute('contenteditable') === 'true';
    const lab = labFor(el);
    if (!lab || lab.length < 2) continue;
    const rq2 = el.getAttribute('aria-required') === 'true' || /[*✱]\s*$/.test(lab);
    if (ce || role === 'textbox') {
      push(ident(el), lab, 'textarea', 'open_ended', null, rq2);
    } else if (role === 'switch') {
      push(ident(el), lab, 'checkbox', 'select', ['Yes', 'No'], rq2);
    } else if (role === 'slider' || (el.tagName === 'INPUT')) {
      push(ident(el), lab, 'range', 'select', null, rq2);
    } else if (role === 'listbox') {
      const opts = [...el.querySelectorAll('[role=option]')].map(o => clean(o.innerText)).filter(Boolean).slice(0, 40);
      push(ident(el), lab, 'single_select', 'select', opts, rq2);
    }
  }
  return JSON.stringify(out.slice(0, 60));
}
"""


async def discover_fields(page: Any) -> list[eng.FormField]:
    """Enumerate the live page's fillable controls as FormField rows. [] = nothing found."""
    try:
        raw = await page.evaluate(_ENUM_JS)
        rows = json.loads(raw) if raw else []
    except Exception as exc:
        print(f"   [discover] enumeration failed: {exc}")
        return []
    fields: list[eng.FormField] = []
    for r in rows:
        fields.append(
            eng.FormField(
                name=str(r.get("name") or ""),
                label=str(r.get("label") or ""),
                type=str(r.get("type") or "text"),
                source=str(r.get("source") or "input_text"),
                required=bool(r.get("required")),
                options=list(r["options"])[:80] if r.get("options") else None,
            )
        )
    return fields


# ---------------------------------------------------------------------------
# VISUAL discovery (user: '难道不能用 visuals+dom 吗' — yes, both): ONE full-page VLM read
# lists EVERY question the applicant must answer. Diffed against the DOM enum; anything the
# DOM missed (pure-div Yes/No cards, custom widgets with no input/role) becomes a FormField
# that observe_act's label-driven tiered locate (question text / spatial / marks) can still
# bind and commit. DOM = precise & cheap; VISION = complete. Union = the honest field list.
# ---------------------------------------------------------------------------
_KIND_MAP = {
    "text": ("text", "input_text"),
    "textarea": ("textarea", "open_ended"),
    "dropdown": ("combobox", "select"),
    "choice": ("radio", "select"),
    "rating": ("radio", "select"),
    "checkbox": ("multi_select", "select"),
    "date": ("date", "input_text"),
    "file": ("input_file", "file"),
}


def _lnorm(s: str) -> str:
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _lnorm2(s: str) -> str:
    """lowercase, punctuation -> spaces (preserves word boundaries for token-overlap dedup)."""
    return "".join(ch if ch.isalnum() else " " for ch in str(s).lower())


async def discover_fields_visual(session: Any, dom_fields: list[eng.FormField]) -> list[eng.FormField]:
    """Fields VISION sees that the DOM enum missed. [] on any failure (never blocks the run)."""
    try:
        import asyncio
        import base64

        import oa_llm as _oa
        from vision_verify import _vlm

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
                        "List EVERY input the applicant must interact with on this job application "
                        "form: text boxes, dropdowns, yes/no or multiple-choice button groups, "
                        "1-5 ratings, checkboxes, date pickers, file uploads. Reply ONLY a STRICT "
                        'JSON array: [{"label": "<exactly as displayed>", "kind": '
                        '"text|textarea|dropdown|choice|rating|checkbox|date|file", "options": '
                        '["..."]}] — options only for choice/rating/checkbox, exactly as displayed. '
                        "No commentary."
                    ),
                ),
                ContentPartImageParam(
                    type="image_url",
                    image_url=ImageURL(
                        url=f"data:image/png;base64,{base64.b64encode(png).decode()}",
                        detail="high",
                        media_type="image/png",
                    ),
                ),
            ]
        )
        res = await _oa.resilient_vlm([msg], primary=_vlm())
        raw = str(getattr(res, "completion", res) or "[]")
        m = None
        with contextlib.suppress(Exception):
            import re

            m = re.search(r"\[.*\]", raw, re.S)
        rows = json.loads(m.group(0)) if m else []
        seen = {_lnorm(f.label) for f in dom_fields} | {_lnorm(f.name) for f in dom_fields}
        # token sets of DOM labels for overlap dedup — the DOM combobox (now discoverable via the
        # width-gate fix) and the VLM twin describe the SAME question in different words; a stale
        # twin false-DONEs committing the label as its value (robinhood). Exact/containment missed
        # them because the two readers phrase long questions differently.
        dom_toks = [t for t in ({w for w in _lnorm2(f.label).split() if len(w) > 3} for f in dom_fields) if len(t) >= 2]
        extra: list[eng.FormField] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            label = str(r.get("label") or "").strip()
            k = _lnorm(label)
            if not label or len(k) < 3:
                continue
            # skip anything the DOM enum already carries (containment both ways — labels get
            # truncated/decorated differently between the two readers)
            if any(k in s or s in k for s in seen if len(s) >= 4):
                continue
            # token-overlap dedup: a VLM label sharing >=60% of its words with a DOM field's label
            # is the SAME question (drop the twin, keep the DOM field the engine can actually fill)
            vtok = {w for w in _lnorm2(label).split() if len(w) > 3}
            if vtok and any(len(vtok & dt) >= max(2, int(len(vtok) * 0.6)) for dt in dom_toks):
                continue
            typ, source = _KIND_MAP.get(str(r.get("kind") or "text").lower(), ("text", "input_text"))
            opts = [str(o)[:80] for o in (r.get("options") or [])][:30] or None
            extra.append(
                eng.FormField(
                    name=label.lower().replace(" ", "_")[:60],
                    label=label[:200],
                    type=typ,
                    source=source,
                    required="*" in label,
                    options=opts,
                )
            )
            seen.add(k)
        return extra[:25]
    except Exception as exc:
        print(f"   [discover] visual pass failed: {exc}")
        return []
