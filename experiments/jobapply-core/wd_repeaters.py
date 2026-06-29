"""Deterministic Workday repeater engine (Experience / Education / Skills / Languages).

第一性原理: the DOM is a state machine; the plan is a hypothesis; the live DOM + the app's own
validation are ground truth. So this is a FIXPOINT reconcile-and-repair loop, not a one-shot fill.

Design (see PLAN_WORKDAY_REPEATERS.md):
- ONE `Control` dataclass + ONE shared logic layer (classify / section / reconcile), fed by TWO
  extractors: `extract_offline` (lxml, for ms tests over a saved DOM) and `extract_live` (CDP handles,
  for production). The decisions (detect / plan / reconcile) are pure + offline-verifiable; only `put`
  (click/type) touches the live browser.
- `put(control, value)` = one verb, multi-level observe-act; archetype is the mechanical tail.
- `make_plan` = 1 semantic map call (label->value + row counts); never picks a DOM element.
- `reconcile` = diff plan vs DOM read-back -> {DONE, MISSING, DIVERGED, SKIP, UNPLANNED}; ledger blocks
  redo/dup and feeds the agent context on escalation.

North-star: anything a human can do, the agent decides, the deterministic layer replays via CDP.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

KW = ["experience", "education", "skill", "language", "certification", "website", "resume", "social"]
# data-fkit-id section token -> canonical section key (the plan + profile use canonical keys)
SEC_FROM_FKIT = {"workexperience": "experience", "education": "education", "skills": "skills",
                 "languages": "languages", "certifications": "certifications",
                 "resumeattachments": "resume", "websitepanelset": "websites", "socialnetwork": "social"}
CANON = {"experience": "experience", "education": "education", "skill": "skills",
         "language": "languages", "certification": "certifications"}


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def nkey(s: str | None) -> str:
    """Normalised match key: lowercase, collapse ws, drop a trailing required '*' and punctuation."""
    return re.sub(r"[\s*:]+$", "", norm(s).lower()).strip()


def humanize(camel: str) -> str:
    """'startDate' -> 'Start Date' — recover a readable label from a camelCase fkit field."""
    return norm(re.sub(r"([a-z])([A-Z])", r"\1 \2", camel).replace("-", " ").replace("_", " ")).title()


_SEGMENT_WORDS = {"month", "year", "day", ""}


def _fix_label(fkit: str, label: str) -> str:
    """A date segment's own label is just 'Month'/'Year' — recover the field label from the fkit
    field (startDate -> 'Start Date') so start/end dates are distinct + matchable by the plan."""
    rawfld = fkit.split("--")[-1] if "--" in fkit else ""
    if rawfld and (not label or nkey(label) in _SEGMENT_WORDS):
        return humanize(rawfld)
    return label


def parse_fkit(fkit: str) -> tuple[str | None, str, str]:
    """'workExperience-225--jobTitle' -> ('experience','225','jobtitle'). The ROW-SAFE identity:
    section base + row instance + machine field. 'skills--skills' -> ('skills','','skills')."""
    if not fkit or "--" not in fkit:
        return (None, "", "")
    secrow, _, fld = fkit.partition("--")
    m = re.match(r"^(.*?)-(\d+)$", secrow)
    base, row = (m.group(1), m.group(2)) if m else (secrow, "")
    return (SEC_FROM_FKIT.get(base.lower(), base.lower()), row, fld.lower())


# ---------------------------------------------------------------------------
# Control: the unit both extractors produce; all logic operates on this.
# ---------------------------------------------------------------------------
@dataclass
class Control:
    tag: str = ""                 # INPUT/SELECT/TEXTAREA/BUTTON/DIV...
    role: str = ""                # listbox/spinbutton/checkbox/radio...
    haspopup: str = ""            # aria-haspopup
    itype: str = ""               # input type
    cid: str = ""                 # element id
    aid: str = ""                 # data-automation-id
    fkit: str = ""                # data-fkit-id — the ROW-SAFE unique key
    sec: str | None = None        # canonical section (from fkit, else heading)
    row: str = ""                 # row instance token ('225'); '' = non-repeating
    field_key: str = ""           # machine field (jobtitle, degree, startdate, skills)
    label: str = ""               # resolved visible label (for LLM map matching)
    wrapper_aid: str = ""         # nearest ancestor data-automation-id (widget group)
    in_multiselect: bool = False  # inside a multiSelectContainer
    doc_index: int = 0            # document order
    section: str | None = None    # heading-proximity section (fallback when no fkit)
    handle: object = None         # live element handle (None offline)

    def sect(self) -> str | None:
        return self.sec or (CANON.get(self.section or "", self.section) if self.section else None)

    def archetype(self) -> str | None:
        if self.in_multiselect:
            return "chip"
        if self.role == "spinbutton":
            return "date"
        if self.tag == "SELECT" or self.role == "listbox" or self.haspopup == "listbox":
            return "select"
        if self.itype == "checkbox" or self.role == "checkbox":
            return "check"
        if self.itype == "radio" or self.role == "radio":
            return "radio"
        if self.tag == "TEXTAREA":
            return "textarea"
        if self.tag == "INPUT" and self.itype in ("text", "email", "tel", "url", ""):
            return "text"
        return None


# ---------------------------------------------------------------------------
# Shared logic: section assignment + de-dup (the 2 nit fixes live here).
# ---------------------------------------------------------------------------
def _assign_sections(controls: list[Control], headings: list[tuple[int, str]]) -> None:
    """heading = (doc_index, lowercased-text). Each control -> nearest heading BEFORE it."""
    headings = sorted(headings)
    for c in controls:
        best = None
        for hpos, t in headings:
            if hpos < c.doc_index:
                best = t
        c.section = next((k for k in KW if best and k in best), None) if best else None


_ARCHE_RANK = {"chip": 0, "select": 1, "date": 2, "check": 3, "radio": 4, "textarea": 5, "text": 6}


def dedup(controls: list[Control]) -> list[Control]:
    """Collapse raw handles to ONE entry per logical field, keyed by data-fkit-id (the ROW-SAFE key —
    so workExperience-225 and -226 stay distinct, while a field's visible+hidden input twins and a
    date's MM/YYYY spinbuttons collapse to one). Controls without a fkit-id fall back to wrapper key.
    Among twins sharing a fkit-id, keep the RICHEST archetype (a select beats its hidden text input)."""
    best: dict[str, Control] = {}
    order: list[str] = []
    for c in controls:
        a = c.archetype()
        if a is None:
            continue
        if "selecteditem" in (c.aid or "").lower():   # the pill CONTAINER is not an input
            continue
        if c.field_key in ("", "null"):               # the wrapper row marker, not a real field
            if not c.fkit:                            # but keep genuinely fkit-less controls (rare)
                pass
            else:
                continue
        key = c.fkit or f"{c.wrapper_aid or c.cid or c.aid}::{nkey(c.label)}::{c.section}"
        if key not in best:
            best[key] = c
            order.append(key)
        elif _ARCHE_RANK.get(a, 9) < _ARCHE_RANK.get(best[key].archetype(), 9):
            best[key] = c                              # richer archetype wins the twin
    return [best[k] for k in order]


# ---------------------------------------------------------------------------
# Offline extractor (lxml) — the test path. Mirrors extract_live's Control fields.
# ---------------------------------------------------------------------------
def extract_offline(html: str) -> list[Control]:
    import lxml.html

    tree = lxml.html.fromstring(html)
    rt = tree.getroottree()
    by_id = {el.get("id"): el for el in tree.iter() if el.get("id")}
    order = {rt.getpath(el): i for i, el in enumerate(tree.iter())}

    def label_for(el) -> str:
        fid = el.get("id")
        if fid:
            labs = tree.xpath("//label[@for=$f]", f=fid)
            if labs:
                return norm(labs[0].text_content())
        if el.get("aria-label"):
            return norm(el.get("aria-label"))
        lb = el.get("aria-labelledby")
        if lb and by_id.get(lb.split()[0]) is not None:
            return norm(by_id[lb.split()[0]].text_content())
        for anc in el.iterancestors():
            if anc.get("data-automation-id"):
                labs = anc.xpath(".//label")
                if labs:
                    return norm(labs[0].text_content())
                break
        return ""

    heads: list[tuple[int, str]] = []
    for n in tree.xpath('//h1|//h2|//h3|//h4|//*[@role="heading"]|//*[contains(@data-automation-id,"Title")]'):
        t = norm(n.text_content()).lower()
        if t and len(t) <= 40 and any(k in t for k in KW):
            heads.append((order[rt.getpath(n)], t))

    controls: list[Control] = []
    raw = tree.xpath('//input|//select|//textarea|//*[@role="spinbutton"]|//*[@role="listbox"]'
                     '|//button[@aria-haspopup="listbox"]')
    for el in raw:
        wrapper_aid, in_ms, fkit = "", False, el.get("data-fkit-id") or ""
        for anc in el.iterancestors():
            aid = anc.get("data-automation-id")
            if aid == "multiSelectContainer":
                in_ms = True
            if aid and not wrapper_aid:
                wrapper_aid = aid
            if not fkit and anc.get("data-fkit-id"):
                fkit = anc.get("data-fkit-id")
        sec, row, fld = parse_fkit(fkit)
        label = _fix_label(fkit, label_for(el))
        controls.append(Control(
            tag=(el.tag if isinstance(el.tag, str) else "").upper(),
            role=(el.get("role") or "").lower(),
            haspopup=(el.get("aria-haspopup") or "").lower(),
            itype=(el.get("type") or "").lower(),
            cid=el.get("id") or "",
            aid=el.get("data-automation-id") or "",
            fkit=fkit, sec=sec, row=row, field_key=fld,
            label=label,
            wrapper_aid=wrapper_aid,
            in_multiselect=in_ms,
            doc_index=order[rt.getpath(el)],
        ))
    _assign_sections(controls, heads)   # heading-proximity fallback for fkit-less controls
    return dedup(controls)


# ---------------------------------------------------------------------------
# Reconcile: diff a plan against the live (or saved) controls. Pure.
# ---------------------------------------------------------------------------
def semantic_equal(intended: str, actual: str) -> bool:
    """Equivalence, NOT string equality: app may commit a canonical/closest form of our value
    ('Computer Science' -> 'Computer Science & Engineering'). Bidirectional contains; LLM/vision
    rungs handle the hard cases upstream of here."""
    a, b = nkey(intended), nkey(actual)
    if not a:
        return True            # nothing intended -> not a divergence
    if not b:
        return False           # intended something, got empty -> MISSING
    return a == b or a in b or b in a


@dataclass
class FieldDiff:
    section: str
    row: int
    label: str
    intended: str
    status: str                # DONE | MISSING | DIVERGED | SKIP
    control: Control | None = None


@dataclass
class Diff:
    fields: list[FieldDiff] = field(default_factory=list)
    unplanned: list[Control] = field(default_factory=list)   # required controls not in the plan
    row_overflow: dict = field(default_factory=dict)         # section -> extra rows beyond target

    def todo(self) -> list[FieldDiff]:
        return [f for f in self.fields if f.status in ("MISSING", "DIVERGED")]

    def clean(self) -> bool:
        return not self.todo() and not self.unplanned


def _rows_of(controls: list[Control], sec: str) -> list[str]:
    """Ordered distinct row tokens mounted for a section (by first appearance)."""
    seen: dict[str, int] = {}
    for c in controls:
        if c.sect() == sec and c.row not in seen:
            seen[c.row] = c.doc_index
    return [r for r, _ in sorted(seen.items(), key=lambda kv: kv[1])]


def _match(controls: list[Control], sec: str, row: str, label: str) -> Control | None:
    lk = nkey(label)
    cand = [c for c in controls if c.sect() == sec and c.row == row]
    for c in cand:                                  # exact visible-label match
        if nkey(c.label) == lk:
            return c
    fk = re.sub(r"[^a-z]", "", lk)                   # else machine field_key alias (jobtitle ~ "job title")
    for c in cand:
        if c.field_key and (fk in c.field_key or c.field_key in fk):
            return c
    return None


def reconcile(plan: dict, controls: list[Control], readback: dict | None = None) -> Diff:
    """plan = {section: {"count": N, "rows": [ {label: value} ]}} (skills/langs are rows of one value).
    readback = {fkit_id: committed_value} from the live DOM ('' if empty). ROW-AWARE: aligns plan row j
    to DOM row j (via fkit row tokens); a plan row beyond the mounted rows => MISSING (needs Add Another).
    Pure: classifies DONE / MISSING / DIVERGED; flags unplanned-required + row overflow (dup-guard)."""
    readback = readback or {}
    d = Diff()
    for sec, blk in plan.items():
        plan_rows = blk.get("rows", [])
        dom_rows = _rows_of(controls, sec)
        for j, prow in enumerate(plan_rows):
            dom_row = dom_rows[j] if j < len(dom_rows) else None
            for label, value in prow.items():
                v = str(value).strip()
                ctrl = _match(controls, sec, dom_row, label) if dom_row is not None else None
                got = readback.get(ctrl.fkit, "") if ctrl else ""
                arche = ctrl.archetype() if ctrl else None
                if not v:
                    status = "DONE"                         # nothing intended
                elif dom_row is None or ctrl is None:
                    status = "MISSING"                      # row not mounted / control absent -> Add+fill
                elif arche in ("date", "textarea"):
                    # a date reads back reformatted (ISO '2021-06' -> display '06/2021') and free-text
                    # won't match verbatim — so NON-EMPTY = filled (the Ashby read-back lesson). Don't
                    # string-compare, or we loop re-typing an already-filled field.
                    status = "DONE" if got else "MISSING"
                elif arche == "chip" and "," in v:
                    # multi-pill tag (Skills): DONE only when EVERY item is a committed pill.
                    items = [nkey(x) for x in v.split(",") if x.strip()]
                    status = "DONE" if got and all(it in nkey(got) for it in items) else "MISSING"
                elif semantic_equal(v, got):
                    status = "DONE" if got else "MISSING"
                else:
                    status = "DIVERGED" if got else "MISSING"
                d.fields.append(FieldDiff(sec, j, label, v, status, ctrl))
        if len(dom_rows) > len(plan_rows):                  # dup-guard signal
            d.row_overflow[sec] = dom_rows[len(plan_rows):]
    planned = {(f.section, nkey(f.label)) for f in d.fields}
    for c in controls:                                      # unplanned REQUIRED controls (conditional reveals)
        s = c.sect()
        if s and c.label.endswith("*") and (s, nkey(c.label)) not in planned:
            d.unplanned.append(c)
    return d


# ===========================================================================
# LIVE layer (CDP). extract_live mirrors extract_offline; put = the act; the
# fixpoint loop is fill_deterministic. Pure decisions stay in the functions above.
# ===========================================================================
# NB: ONE ordered query of just headings+controls (~50 nodes), NOT querySelectorAll('*') (which walked
# ALL ~2400 elements on every call x ~20 calls/run — needless load that helped crash the headless CDP on
# a heavy SPA). querySelectorAll preserves document order, so doc_index = position in this one list.
_SEL_HEAD = 'h1,h2,h3,h4,[role="heading"],[data-automation-id*="Title"]'
_SEL_CTRL = 'input,select,textarea,[role="spinbutton"],[role="listbox"],button[aria-haspopup="listbox"]'
EXTRACT_JS = (
    r"""
() => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const SEL_H = '"""
    + _SEL_HEAD
    + r"""', SEL_C = '"""
    + _SEL_CTRL
    + r"""';
  const labelFor = (el) => {
    if (el.id){ const l=document.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if(l) return norm(l.textContent); }
    if (el.getAttribute('aria-label')) return norm(el.getAttribute('aria-label'));
    const lb=el.getAttribute('aria-labelledby'); if(lb){ const n=document.getElementById(lb.split(' ')[0]); if(n) return norm(n.textContent); }
    const p=el.closest('[data-automation-id]'); if(p){ const l=p.querySelector('label'); if(l) return norm(l.textContent); }
    return '';
  };
  const heads=[], out=[];
  [...document.querySelectorAll(SEL_H+','+SEL_C)].forEach((el,i)=>{   // document order
    if (el.matches(SEL_H)) { const t=norm(el.textContent).toLowerCase(); if(t && t.length<=40) heads.push({i,t}); return; }
    let w='',ms=false,fk=el.getAttribute('data-fkit-id')||'',a=el;
    while(a && a.getAttribute){ const aid=a.getAttribute('data-automation-id');
      if(aid==='multiSelectContainer') ms=true; if(aid && !w) w=aid;
      if(!fk && a.getAttribute('data-fkit-id')) fk=a.getAttribute('data-fkit-id'); a=a.parentElement; }
    out.push({tag:el.tagName, role:(el.getAttribute('role')||'').toLowerCase(),
      haspopup:(el.getAttribute('aria-haspopup')||'').toLowerCase(), itype:(el.getAttribute('type')||'').toLowerCase(),
      cid:el.id||'', aid:el.getAttribute('data-automation-id')||'', fkit:fk, label:labelFor(el),
      wrapper_aid:w, in_ms:ms, doc_index:i});
  });
  return JSON.stringify({headings:heads, controls:out});
}
"""
)


async def extract_live(page) -> list[Control]:
    """Production extractor — same Control shape + shared dedup/section logic as extract_offline."""
    import json

    data = json.loads(await page.evaluate(EXTRACT_JS))
    controls = [
        Control(tag=(c["tag"] or "").upper(), role=c["role"], haspopup=c["haspopup"], itype=c["itype"],
                cid=c["cid"], aid=c["aid"], fkit=c["fkit"], label=_fix_label(c["fkit"], c["label"]),
                wrapper_aid=c["wrapper_aid"], in_multiselect=c["in_ms"], doc_index=c["doc_index"],
                **dict(zip(("sec", "row", "field_key"), parse_fkit(c["fkit"]), strict=False)))
        for c in data["controls"]
    ]
    _assign_sections(controls, [(h["i"], h["t"]) for h in data["headings"] if any(k in h["t"] for k in KW)])
    return dedup(controls)


# Batched read-back: ONE eval for ALL controls (a per-control eval x 19 controls x 5 rounds is what made
# fill_deterministic so slow it got cancelled mid-run on a live page). Returns {fkit: committed value}.
READ_ALL_JS = r"""
(fkits) => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const readOne = (fkit) => {
    const root = document.querySelector('[data-fkit-id="'+fkit+'"]'); if (!root) return '';
    const ms = root.closest('[data-automation-id="multiSelectContainer"]');
    if (ms) return [...ms.querySelectorAll('[data-automation-id="selectedItem"]')].map(p=>norm(p.textContent)).join(', ');
    const el = root.querySelector('input,textarea,select,[role="spinbutton"]') || root;
    if (el.getAttribute && (el.getAttribute('type')==='checkbox' || el.getAttribute('role')==='checkbox'))
      return (el.checked||el.getAttribute('aria-checked')==='true') ? 'true' : '';
    const btn = root.querySelector('button');
    if (btn && (el.tagName==='BUTTON' || (el.getAttribute&&el.getAttribute('aria-haspopup')==='listbox')))
      return norm(btn.textContent);
    const spins=[...root.querySelectorAll('[role="spinbutton"]')];
    if (spins.length) return spins.map(s=>norm(s.textContent||s.value)).filter(Boolean).join('/');
    return norm(el.value!==undefined ? el.value : el.textContent);
  };
  const out={}; for (const f of fkits) out[f]=readOne(f); return JSON.stringify(out);
}
"""


async def read_live(page, controls: list[Control]) -> dict:
    """Read-back: {fkit: committed value} for every control — ground truth reconcile diffs against. ONE
    batched eval (not per-control) so the loop stays fast enough to finish within the step budget."""
    import contextlib
    import json

    fkits = [c.fkit for c in controls if c.fkit]
    if not fkits:
        return {}
    with contextlib.suppress(Exception):
        raw = await page.evaluate(READ_ALL_JS, fkits)
        return {k: norm(v) for k, v in json.loads(raw).items()}
    return {}


# archetype -> the WorkdayAdapter.fill() type that drives it
ARCHE2TYPE = {"text": "input_text", "textarea": "textarea", "select": "single_select",
              "chip": "multi_select", "date": "date", "check": "checkbox", "radio": "radio"}


async def _locate(page, c: Control):
    from ats_engine import first

    return await first(page, f'[data-fkit-id="{c.fkit}"]') if c.fkit else None


async def _llm_pick(llm, value: str, options: list[str]) -> str | None:
    """The agent's 'which option' decision, made CHEAPLY by a text LLM over the READ options (no vision):
    pick the closest by meaning/abbreviation ('BS'->Bachelor's, 'Python'->nearest skill). Cached upstream."""
    import contextlib

    if llm is None or not options:
        return None
    from pydantic import BaseModel

    from browser_use.llm.messages import SystemMessage, UserMessage

    class _Pick(BaseModel):
        choice: str  # EXACT option text from the list, or "NONE"

    with contextlib.suppress(Exception):
        res = await llm.ainvoke(
            [SystemMessage(content="Pick the option that best matches the wanted value — closest meaning "
                           "or abbreviation (e.g. 'B.S.' -> \"Bachelor's Degree\"; 'Python' -> the nearest "
                           "skill). Reply the EXACT option text from the list, or 'NONE' if truly none fit."),
             UserMessage(content=f"wanted: {value!r}\noptions: {options}")],
            output_format=_Pick)
        c = (res.completion.choice or "").strip()
        return None if c.upper() == "NONE" or not c else c
    return None


async def pick_smart(adapter, page, llm, value: str, tries: int = 8) -> bool:
    """Pick the best typeahead/listbox option for `value`: exact -> contains -> CHEAP LLM choice over the
    READ options (the replayable agent decision). Clicks the chosen option. Returns True if one was clicked."""
    import contextlib

    want = norm(value)
    for _ in range(tries):
        await asyncio.sleep(0.3)
        raw = await page.get_elements_by_css_selector(
            '[data-automation-id="activeListContainer"] [role="option"], [data-automation-id="promptOption"], '
            '[data-automation-id="menuItem"], [role="listbox"] [role="option"]')
        opts: list = []
        for o in raw:
            with contextlib.suppress(Exception):
                t = norm(await o.evaluate("() => this.textContent || ''"))
                if t:
                    opts.append((o, t))
        if not opts:
            continue
        target = (next((o for o, t in opts if norm(t) == want), None)
                  or next((o for o, t in opts if want and (want in norm(t) or norm(t) in want)), None))
        choice = None
        if target is None and llm is not None:
            choice = await _llm_pick(llm, value, [t for _, t in opts])  # the agent's decision, replayed
            if choice:
                target = next((o for o, t in opts if norm(t) == norm(choice)), None)
        print(f"  [pick] want={value!r} opts={[t for _, t in opts][:5]} choice={choice!r} hit={target is not None}",
              flush=True)
        if target is not None:
            with contextlib.suppress(Exception):
                await target.click()
                await asyncio.sleep(0.3)
                return True
    return False


async def put(adapter, session, page, c: Control, value: str, llm=None) -> bool:
    """ONE verb: drive a control to `value`, dispatching by archetype to the proven WorkdayAdapter
    primitives. Locates row-safe by data-fkit-id. Observe-then-verify is inside each primitive.
    Searchable typeaheads (Degree/School/Field/Skills) pick via pick_smart (LLM closest-match)."""
    from ats_engine import first

    a = c.archetype()
    if not (value or "").strip() or a is None:
        return True
    base = f'[data-fkit-id="{c.fkit}"]'
    if a in ("text", "textarea"):
        el = await first(page, f"{base} input") or await first(page, f"{base} textarea")
        if not el:
            return False
        with __import__("contextlib").suppress(Exception):
            await el.click()
            await el.fill(value)
            return True
        return False
    if a == "check":
        el = await first(page, f'{base} input[type="checkbox"]') or await first(page, f"{base} input")
        if el and str(value).strip().lower() in ("yes", "true", "1", "y", c.field_key.lower()):
            with __import__("contextlib").suppress(Exception):
                await el.evaluate("() => { if(!this.checked){ this.click(); "
                                  "this.dispatchEvent(new Event('change',{bubbles:true})); } }")
                return True
        return False
    if a == "select":  # button-listbox: open, TYPE-to-filter if searchable, then pick from the portal
        import contextlib

        trig = await first(page, f"{base} button")
        if not trig:
            return False
        with contextlib.suppress(Exception):
            await trig.click()
        await asyncio.sleep(0.4)
        # a SEARCHABLE listbox (e.g. Degree) shows NO options until you type — find the revealed input
        # and type the value to filter; an inline listbox just shows them (no input -> skip).
        inp = (await first(page, f"{base} input")
               or await first(page, '[data-automation-id="activeListContainer"] input')
               or await first(page, 'input[aria-autocomplete="list"]'))
        if inp:
            with contextlib.suppress(Exception):
                await inp.fill(value)
                await asyncio.sleep(0.8)
        return await pick_smart(adapter, page, llm, value)  # exact -> contains -> LLM closest
    if a == "chip":  # typeahead TAG: add EACH comma-item as its own pill (type -> filter -> pick)
        import contextlib

        items = [x.strip() for x in value.split(",")] if "," in value else [value]
        added = 0
        for it in items:
            if not it:
                continue
            inp = await first(page, f"{base} input") or await first(page, f'{base} [role="combobox"]')
            if not inp:
                break
            with contextlib.suppress(Exception):
                await inp.click()
                await inp.fill(it)
            await asyncio.sleep(0.9)
            picked = await pick_smart(adapter, page, llm, it, tries=5)  # exact -> contains -> LLM closest
            if not picked:
                await eng_press_enter(session, page)  # fallback: trusted Enter on the highlight
            await asyncio.sleep(0.3)
            added += 1
        return added > 0
    if a == "date":
        from ats_engine import FormField

        return await adapter._date(page, FormField(name=c.fkit, type="date", label=c.label, source="standard"), value)
    return False


async def eng_press_enter(session, page) -> None:
    from ats_engine import press_enter_trusted

    with __import__("contextlib").suppress(Exception):
        await press_enter_trusted(session, page)


async def _add_row(page, sec: str) -> bool:
    """Click the section's Add/Add Another control so the next row mounts. Generic: match by the
    section keyword in the button text, else a bare 'Add'."""
    label_kw = {"experience": "experience", "education": "education", "skills": "skill",
                "languages": "language", "certifications": "certification"}.get(sec, sec)
    clicked = await page.evaluate(
        """(kw) => { const bs=[...document.querySelectorAll('button,[role=button]')];
          const re=new RegExp('add (another|'+kw+')','i');
          let b=bs.find(x=>re.test((x.textContent||'').trim()));
          if(!b) b=bs.find(x=>/^add$/i.test((x.textContent||'').trim()) &&
                    (x.closest('[data-automation-id]')||{}).getAttribute &&
                    new RegExp(kw,'i').test((x.closest('[data-automation-id]').getAttribute('data-automation-id')||'')));
          if(b){ b.scrollIntoView({block:'center'}); b.click(); return true; } return false; }""",
        label_kw,
    )
    if clicked:
        await asyncio.sleep(1.2)  # let the new row mount
    return bool(clicked)


# ROW-repeater sections (each entry = an Add-Another row). Skills is a TAG (pills via chip put), NOT here.
_ROW_SECTIONS = {"experience": ("experience", "work_experience"), "education": ("education",),
                 "languages": ("languages",), "certifications": ("certifications",)}


async def ensure_rows(adapter, page, profile: dict) -> bool:
    """Add Another until each ROW-repeater section has at least as many rows as the PROFILE wants.
    dup-guard: never add PAST the profile count. Returns True if any row was added (caller re-reconciles).

    PROFILE-DRIVEN, not plan-driven: a COLLAPSED/empty section (0 mounted controls, e.g. experience
    before any Add) has no detected fields, so a control-derived plan can't see it — the only way its
    fields appear is to click Add. So we mount `len(profile[section])` rows per ROW-repeater section,
    independent of current detection. Tag sections (skills) are filled by chip put(), not Add-Another."""
    added = False
    for sec, pkeys in _ROW_SECTIONS.items():
        items = next((profile.get(k) for k in pkeys if profile.get(k)), None) or []
        want = len(items)
        if not want:
            continue
        for _ in range(want + 2):
            controls = await extract_live(page)
            have = len(_rows_of(controls, sec))
            if have >= want:
                break
            if not await _add_row(page, sec):  # section not on this page / no Add control -> stop
                break
            added = True
    return added


async def fill_deterministic(adapter, session, page, profile: dict, llm, title: str = "",
                             max_rounds: int = 3) -> dict:
    """The fixpoint reconcile-and-repair loop. FIRST mount rows from the profile (so collapsed sections'
    fields exist), THEN one semantic map call, THEN loop: reconcile(read-back) -> put() MISSING/DIVERGED
    -> until the DOM is stable. Returns a ledger summary. NEVER submits. Agent escalation is the backstop.

    ensure_rows + make_plan run ONCE upfront (rows persist + labels don't change), NOT per round — the
    per-round re-mount + re-LLM made the loop too slow to finish within the step budget (timeout)."""
    import time

    t0 = time.monotonic()
    rows_added = await ensure_rows(adapter, page, profile)  # bootstrap ONCE: mount rows so fields appear
    t_ensure = time.monotonic() - t0
    controls = await extract_live(page)                     # now collapsed sections have controls
    plan = await make_plan(llm, controls, profile, title)   # ONE semantic map: labels known -> values
    t_plan = time.monotonic() - t0 - t_ensure
    print(f"  [wd] ensure_rows={t_ensure:.1f}s plan={t_plan:.1f}s (rows_added={rows_added})", flush=True)
    summary = {"rounds": 0, "filled": 0, "rows_added": int(rows_added)}
    last_todo = None
    for rnd in range(max_rounds):
        summary["rounds"] = rnd + 1
        tr = time.monotonic()
        controls = await extract_live(page)
        readback = await read_live(page, controls)
        diff = reconcile(plan, controls, readback)
        todo = diff.todo()
        if not todo:
            break
        if last_todo is not None and len(todo) >= last_todo:  # no progress -> stop the deterministic loop
            print(f"  [wd] round {rnd + 1}: no progress ({len(todo)} todo), stop", flush=True)
            break
        last_todo = len(todo)
        slow: dict = {}
        for fd in todo:
            if fd.control:
                tp = time.monotonic()
                ok = await put(adapter, session, page, fd.control, fd.intended, llm)
                dt = time.monotonic() - tp
                slow[fd.control.archetype()] = slow.get(fd.control.archetype(), 0.0) + dt
                if ok:
                    summary["filled"] += 1
        print(f"  [wd] round {rnd + 1}: {len(todo)} todo, {time.monotonic() - tr:.1f}s, "
              f"put-time-by-type={ {k: round(v, 1) for k, v in slow.items()} }", flush=True)
        await asyncio.sleep(0.5)
    final_controls = await extract_live(page)
    final_diff = reconcile(plan, final_controls, await read_live(page, final_controls))
    summary["residual"] = [f"{f.section}[{f.row}].{f.label}" for f in final_diff.todo()]
    summary["secs"] = round(time.monotonic() - t0, 1)
    print(f"  [wd] TOTAL {summary['secs']}s filled={summary['filled']} residual={len(summary['residual'])}",
          flush=True)
    return summary


_PKEY = {"experience": ("experience", "work_experience"), "education": ("education",),
         "skills": ("skills",), "languages": ("languages",), "certifications": ("certifications",)}
_TAG_SECTIONS = {"skills", "certifications"}  # one typeahead, many pills (NOT Add-Another rows)
_PLAN_SYS = (
    "You map an applicant profile onto a job application's repeater sections. You are given, per "
    "section, the rows to fill (one per profile entry) and each field's visible LABEL. For every "
    "(section,row,label) return a value: copy the matching profile value VERBATIM; '' if the profile "
    "lacks it (never fabricate). For a closed-list field (e.g. Degree) map to the closest CANONICAL "
    "form the form likely offers (e.g. 'B.S.' -> \"Bachelor's Degree\"). Dates stay ISO 'YYYY-MM'. "
    "Return one cell per requested (section,row,label), nothing else."
)


def _plan_skeleton(controls: list[Control], profile: dict) -> dict:
    """Deterministic STRUCTURE: which sections + labels are present, and how many rows (= profile
    entries). Values are filled afterwards (LLM semantic, or structural fallback)."""
    sections: dict = {}
    for c in controls:
        s = c.sect()
        if s in ("experience", "education", "skills", "languages", "certifications") and c.label and c.label != "·":
            sections.setdefault(s, {})[nkey(c.label)] = c.label
    plan: dict = {}
    for sec, labelset in sections.items():
        items: list = []
        for pk in _PKEY.get(sec, (sec,)):
            items = profile.get(pk) or items
        if not items:
            continue
        labels = list(labelset.values())
        if sec in _TAG_SECTIONS:
            # TAG: ONE typeahead control holds MANY pills (Skills) — NOT N Add-Another rows. Plan a
            # single row whose value is the comma-joined list; put() adds each as a pill.
            joined = ", ".join(str(it) for it in items)
            plan[sec] = {"count": 1, "rows": [{labels[0]: ""}] if labels else [],
                         "_items": [joined], "_labels": labels[:1]}
        else:
            rows = [{lbl: "" for lbl in labels} for _ in items]
            plan[sec] = {"count": len(rows), "rows": rows, "_items": items, "_labels": labels}
    return plan


def _fill_values_struct(plan: dict) -> None:
    """Structural value fill: fuzzy field_key match (no LLM). Used offline + as fallback."""
    for _sec, blk in plan.items():
        for row, item in zip(blk["rows"], blk["_items"], strict=False):
            for lbl in blk["_labels"]:
                if isinstance(item, dict):
                    lk = re.sub(r"[^a-z]", "", nkey(lbl))
                    row[lbl] = next((str(v) for k, v in item.items()
                                     if re.sub(r"[^a-z]", "", k.lower()) in lk
                                     or lk in re.sub(r"[^a-z]", "", k.lower())), "")
                else:  # scalar (a skill/language string) -> the single label
                    row[lbl] = str(item)


async def _fill_values_llm(llm, plan: dict, title: str) -> None:
    """The ONE semantic call: map every (section,row,label) cell to a profile value."""
    import json

    from pydantic import BaseModel

    from browser_use.llm.messages import SystemMessage, UserMessage

    class _Cell(BaseModel):
        section: str
        row: int
        label: str
        value: str

    class _Out(BaseModel):
        cells: list[_Cell]

    to_fill, prof = [], {}
    for sec, blk in plan.items():
        prof[sec] = blk["_items"]
        for ri, _ in enumerate(blk["rows"]):
            for lbl in blk["_labels"]:
                to_fill.append({"section": sec, "row": ri, "label": lbl})
    ctx = {"job_title": title, "profile": prof, "to_fill": to_fill}
    res = await llm.ainvoke([SystemMessage(content=_PLAN_SYS),
                             UserMessage(content=json.dumps(ctx, ensure_ascii=False))], output_format=_Out)
    for cell in res.completion.cells:
        blk = plan.get(cell.section)
        if blk and 0 <= cell.row < len(blk["rows"]) and cell.label in blk["rows"][cell.row]:
            blk["rows"][cell.row][cell.label] = cell.value


async def make_plan(llm, controls: list[Control], profile: dict, title: str = "") -> dict:
    """Structure (counts + labels) is deterministic; VALUES are the ONE semantic LLM call (label->value,
    closed-list canonicalisation), with a structural fuzzy-key fallback when no llm / on error."""
    plan = _plan_skeleton(controls, profile)
    if llm is not None:
        try:
            await _fill_values_llm(llm, plan, title)
        except Exception:
            _fill_values_struct(plan)
    else:
        _fill_values_struct(plan)
    return {sec: {"count": blk["count"], "rows": blk["rows"]} for sec, blk in plan.items()}
