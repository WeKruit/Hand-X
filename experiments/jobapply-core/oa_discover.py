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
      if (own && own.length > 1 && own.length < 160) return own.split('\n')[0].trim();
      p = p.parentElement;
    }
    return '';
  };
  const push = (name, label, type, source, options, required) => {
    label = clean(label); if (!label || label.length < 2) return;
    const key = (name || '') + '|' + label; if (seen.has(key)) return; seen.add(key);
    out.push({ name: name || label.toLowerCase().replace(/[^a-z0-9]+/g, '_').slice(0, 60),
               label: label.slice(0, 200), type, source, options, required: !!required });
  };
  for (const el of document.querySelectorAll('input, textarea, select, [role=combobox]')) {
    const tag = (el.tagName || '').toLowerCase(); const ty = (el.type || '').toLowerCase();
    // hidden file inputs are the NORM (styled button + display:none input) and CDP
    // setFileInputFiles fills them regardless of visibility — everything else must be visible.
    if (ty !== 'file' && !vis(el)) continue;
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
    if (el.getAttribute && el.getAttribute('role') === 'combobox') {
      push(el.id || el.getAttribute('name'), labFor(el), 'combobox', 'select', [], req); continue;
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
        if (line) return line.slice(0, 200);
      }
      p = p.parentElement;
    }
    return fallback;
  };
  for (const [g, v] of Object.entries(radio)) {
    push(g, groupLabel(v.el, g), 'radio', 'select', v.opts.slice(0, 30), false);
  }
  for (const [g, v] of Object.entries(check)) {
    if (v.opts.length < 2) continue;  // lone consent boxes: v1 skip (ceiling in module docstring)
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
