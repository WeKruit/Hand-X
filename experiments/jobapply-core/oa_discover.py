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
  const seen = new Set(); const out = []; const radio = {}; const check = {};
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
    if (tag === 'input' && ['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(ty)) continue;
    if (el.closest('nav, header, footer, [role=search]')) continue;  // page chrome, not the form
    const req = el.required || (el.getAttribute && el.getAttribute('aria-required') === 'true');
    if (tag === 'select') {
      const opts = [...el.options].map(o => clean(o.text)).filter(t => t && !/^(select|choose|--)/i.test(t)).slice(0, 80);
      push(el.id || el.name, labFor(el), 'single_select', 'select', opts, req); continue;
    }
    if (ty === 'radio') { const g = el.name || 'radio';
      (radio[g] = radio[g] || { opts: [], el }).opts.push(labFor(el) || el.value); continue; }
    if (ty === 'checkbox') { const g = el.name || 'check';
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
  const groupLabel = (el, fallback) => {
    const fs = el.closest('fieldset'); const leg = fs && fs.querySelector('legend');
    if (leg && clean(leg.innerText)) return clean(leg.innerText);
    // walk up: the question text is the container's text MINUS its options (workable Yes/No)
    let p = el.parentElement, hops = 0;
    while (p && hops++ < 5) {
      const full = clean(p.innerText || '');
      if (full.length > 15 && full.length < 400) {
        const line = full.split('\n').map(clean).find(t => t.length > 10 && !/^(yes|no)$/i.test(t));
        if (line) return line.slice(0, 400);
      }
      p = p.parentElement;
    }
    return fallback;
  };
  for (const [g, v] of Object.entries(radio)) {
    push(g, groupLabel(v.el, g), 'radio', 'select', v.opts.slice(0, 30), false);
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
    push(g, groupLabel(v.el, g), 'multi_select', 'select', v.opts.slice(0, 40), false);
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
