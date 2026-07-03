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
import contextlib
import re
from dataclasses import dataclass, field

KW = ["experience", "education", "skill", "language", "certification", "website", "resume", "social"]
# data-fkit-id section token -> canonical section key (the plan + profile use canonical keys). The DOM
# emits SINGULAR bases for some sections (data-fkit-id="language-222--...", "certification-..."), so map
# BOTH singular and plural — else the section reads as "language" and _plan_skeleton/_PKEY (which key on
# the plural "languages") never plan it (the verified empty-Languages bug).
SEC_FROM_FKIT = {
    "workexperience": "experience",
    "experience": "experience",
    "education": "education",
    "skills": "skills",
    "skill": "skills",
    "languages": "languages",
    "language": "languages",
    "certifications": "certifications",
    "certification": "certifications",
    "resumeattachments": "resume",
    "websitepanelset": "websites",
    "socialnetwork": "social",
}
CANON = {
    "experience": "experience",
    "education": "education",
    "skill": "skills",
    "language": "languages",
    "certification": "certifications",
}


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
    tag: str = ""  # INPUT/SELECT/TEXTAREA/BUTTON/DIV...
    role: str = ""  # listbox/spinbutton/checkbox/radio...
    haspopup: str = ""  # aria-haspopup
    itype: str = ""  # input type
    cid: str = ""  # element id
    aid: str = ""  # data-automation-id
    fkit: str = ""  # data-fkit-id — the ROW-SAFE unique key
    sec: str | None = None  # canonical section (from fkit, else heading)
    row: str = ""  # row instance token ('225'); '' = non-repeating
    field_key: str = ""  # machine field (jobtitle, degree, startdate, skills)
    label: str = ""  # resolved visible label (for LLM map matching)
    wrapper_aid: str = ""  # nearest ancestor data-automation-id (widget group)
    in_multiselect: bool = False  # inside a multiSelectContainer
    doc_index: int = 0  # document order
    section: str | None = None  # heading-proximity section (fallback when no fkit)
    handle: object = None  # live element handle (None offline)

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
        if "selecteditem" in (c.aid or "").lower():  # the pill CONTAINER is not an input
            continue
        if c.field_key in ("", "null"):  # the wrapper row marker, not a real field
            if not c.fkit:  # but keep genuinely fkit-less controls (rare)
                pass
            else:
                continue
        key = c.fkit or f"{c.wrapper_aid or c.cid or c.aid}::{nkey(c.label)}::{c.section}"
        if key not in best:
            best[key] = c
            order.append(key)
        elif _ARCHE_RANK.get(a, 9) < _ARCHE_RANK.get(best[key].archetype(), 9):
            best[key] = c  # richer archetype wins the twin
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
    raw = tree.xpath(
        '//input|//select|//textarea|//*[@role="spinbutton"]|//*[@role="listbox"]|//button[@aria-haspopup="listbox"]'
    )
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
        controls.append(
            Control(
                tag=(el.tag if isinstance(el.tag, str) else "").upper(),
                role=(el.get("role") or "").lower(),
                haspopup=(el.get("aria-haspopup") or "").lower(),
                itype=(el.get("type") or "").lower(),
                cid=el.get("id") or "",
                aid=el.get("data-automation-id") or "",
                fkit=fkit,
                sec=sec,
                row=row,
                field_key=fld,
                label=label,
                wrapper_aid=wrapper_aid,
                in_multiselect=in_ms,
                doc_index=order[rt.getpath(el)],
            )
        )
    _assign_sections(controls, heads)  # heading-proximity fallback for fkit-less controls
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
        return True  # nothing intended -> not a divergence
    if not b:
        return False  # intended something, got empty -> MISSING
    return a == b or a in b or b in a


@dataclass
class FieldDiff:
    section: str
    row: int
    label: str
    intended: str
    status: str  # DONE | MISSING | DIVERGED | SKIP
    control: Control | None = None


@dataclass
class Diff:
    fields: list[FieldDiff] = field(default_factory=list)
    unplanned: list[Control] = field(default_factory=list)  # required controls not in the plan
    row_overflow: dict = field(default_factory=dict)  # section -> extra rows beyond target

    def todo(self) -> list[FieldDiff]:
        return [f for f in self.fields if f.status in ("MISSING", "DIVERGED")]

    def clean(self) -> bool:
        return not self.todo() and not self.unplanned


def _rows_of(controls: list[Control], sec: str) -> list[str]:
    """Ordered distinct MOUNTED row tokens for a section (by first appearance). The count this returns
    IS the dup-guard's ground truth (ensure_rows stops at want == len(profile[section])), so it must
    count ONLY real repeater rows: a fkit-derived row instance is a non-empty token ('225','247'). The
    empty token '' is the NON-repeating / un-rowed control (e.g. 'skills--skills', a TAG, or a
    section's own scaffolding) — counting it as a row would over-report a collapsed section as having
    1 row and make ensure_rows under- or mis-mount. So '' is excluded from the row count."""
    seen: dict[str, int] = {}
    for c in controls:
        if c.sect() == sec and c.row and c.row not in seen:
            seen[c.row] = c.doc_index
    return [r for r, _ in sorted(seen.items(), key=lambda kv: kv[1])]


def _match(controls: list[Control], sec: str, row: str, label: str) -> Control | None:
    lk = nkey(label)
    cand = [c for c in controls if c.sect() == sec and c.row == row]
    for c in cand:  # exact visible-label match
        if nkey(c.label) == lk:
            return c
    fk = re.sub(r"[^a-z]", "", lk)  # else machine field_key alias (jobtitle ~ "job title")
    for c in cand:
        if c.field_key and (fk in c.field_key or c.field_key in fk):
            return c
    return None


_FLAGGED: set[str] = set()


def set_flagged(errors: list[str]) -> None:
    """Validation-error text from the CURRENT blocked advance. reconcile must never SKIP a cell
    whose label appears in one of these (the chewy School bug: the autofill-owned-row heuristic
    silently skipped a REQUIRED empty cell the tenant had just flagged). Set by the engine's
    fix loop right before the repeater fixpoint re-run; consumed (cleared) by reconcile."""
    global _FLAGGED
    _FLAGGED = {nkey(e) for e in errors if e}


def reconcile(plan: dict, controls: list[Control], readback: dict | None = None) -> Diff:
    """plan = {section: {"count": N, "rows": [ {label: value} ]}} (skills/langs are rows of one value).
    readback = {fkit_id: committed_value} from the live DOM ('' if empty). ROW-AWARE: aligns plan row j
    to DOM row j (via fkit row tokens); a plan row beyond the mounted rows => MISSING (needs Add Another).
    Pure: classifies DONE / MISSING / DIVERGED; flags unplanned-required + row overflow (dup-guard)."""
    readback = readback or {}
    d = Diff()
    # AUTOFILL-OWNED rows: résumé parse pre-fills rows that need NOT correspond to the plan's entries
    # (verified live: 6 mounted rows vs a 2-entry plan — plan row j lands on a stranger's row; writing
    # the plan's End Date there is WRONG DATA, and flagging it as MISSING bought a no-op agent per
    # tenant). A row with ≥2 committed fields belongs to the résumé — its empty cells are SKIP.
    row_fill: dict = {}
    for c in controls:
        if c.sect() and c.row:
            k = (c.sect(), c.row)
            row_fill[k] = row_fill.get(k, 0) + (1 if norm(readback.get(c.fkit, "")) else 0)
    for sec, blk in plan.items():
        plan_rows = blk.get("rows", [])
        dom_rows = _rows_of(controls, sec)
        # TAG section (Skills / Certifications): its control is UNNUMBERED (row="", one typeahead holding
        # many pills), so _rows_of finds no numbered rows and returned []. Without this the plan row never
        # links to the chip control (ctrl=None, archetype=None) and put() can't dispatch -> Skills is
        # NEVER typed (the verified bug). One unnumbered control = one row "".
        if not dom_rows and any(c.sect() == sec and c.row == "" for c in controls):
            dom_rows = [""]
        for j, prow in enumerate(plan_rows):
            dom_row = dom_rows[j] if j < len(dom_rows) else None
            for label, value in prow.items():
                v = str(value).strip()
                ctrl = _match(controls, sec, dom_row, label) if dom_row is not None else None
                got = readback.get(ctrl.fkit, "") if ctrl else ""
                arche = ctrl.archetype() if ctrl else None
                # RESPECT AUTOFILL — fill GAPS only. autofillWithResume pre-parses experience/education
                # rows; a NON-EMPTY field is already done (resume's truth) and must NOT be re-filled or
                # overwritten (that was the slowness + the dup). Only a genuinely EMPTY field gets filled.
                if not v:
                    status = "DONE"  # nothing intended
                elif dom_row is None or ctrl is None:
                    status = "MISSING"  # row not mounted / control absent -> Add+fill
                elif arche == "chip" and "," in v and got:
                    # multi-pill tag (Skills): DONE only when EVERY item is a pill (else add the rest).
                    items = [nkey(x) for x in v.split(",") if x.strip()]
                    status = "DONE" if all(it in nkey(got) for it in items) else "MISSING"
                elif got:
                    status = "DONE"  # filled (any value) = leave it (respect autofill)
                elif (
                    dom_row
                    and row_fill.get((sec, dom_row), 0) >= 2
                    and not any(nkey(label) and nkey(label) in f for f in _FLAGGED)
                ):
                    status = "SKIP"  # autofill-owned row — its gaps are the résumé's, not the plan's
                else:
                    status = "MISSING"
                d.fields.append(FieldDiff(sec, j, label, v, status, ctrl))
        if len(dom_rows) > len(plan_rows):  # dup-guard signal
            d.row_overflow[sec] = dom_rows[len(plan_rows) :]
    planned = {(f.section, nkey(f.label)) for f in d.fields}
    for c in controls:  # unplanned REQUIRED controls (conditional reveals)
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
        Control(
            tag=(c["tag"] or "").upper(),
            role=c["role"],
            haspopup=c["haspopup"],
            itype=c["itype"],
            cid=c["cid"],
            aid=c["aid"],
            fkit=c["fkit"],
            label=_fix_label(c["fkit"], c["label"]),
            wrapper_aid=c["wrapper_aid"],
            in_multiselect=c["in_ms"],
            doc_index=c["doc_index"],
            **dict(zip(("sec", "row", "field_key"), parse_fkit(c["fkit"]), strict=False)),
        )
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
    // The multiSelectContainer can be an ANCESTOR (fkit on the input) OR a DESCENDANT (fkit on the
    // formField wrapper — every education searchable field: School/Degree/Field of Study). .closest()
    // only walks UP, so it MISSED the descendant case -> a pre-filled pill read as '' (false-empty) ->
    // reconcile re-committed a field the resume-autofill already filled -> leaked to the slow agent.
    const ms = root.closest('[data-automation-id="multiSelectContainer"]')
            || root.querySelector('[data-automation-id="multiSelectContainer"]');
    if (ms) return [...ms.querySelectorAll('[data-automation-id="selectedItem"],[data-automation-id="multiSelectPill"]')].map(p=>norm(p.textContent)).join(', ');
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
ARCHE2TYPE = {
    "text": "input_text",
    "textarea": "textarea",
    "select": "single_select",
    "chip": "multi_select",
    "date": "date",
    "check": "checkbox",
    "radio": "radio",
}


async def _locate(page, c: Control):
    from ats_engine import first

    return await first(page, f'[data-fkit-id="{c.fkit}"]') if c.fkit else None


async def _vlm_filled(session, label: str, value: str) -> bool:
    """Cheap-VLM ground-truth read-back (~$0.0006/call, cached per field+url+value): does the field
    labeled `label` visibly contain `value`? VALUE-AWARE, not presence-only — the shared option portal
    commits the WRONG option (e.g. 'Computer Science' into Skills), and a presence-only 'filled' would
    RUBBER-STAMP that wrong value as done. We ask `matches` (right value present?), so a field is DONE
    only when the VALUE the user intended is the one on screen — never merely non-blank."""
    import contextlib

    with contextlib.suppress(Exception):
        from vision_verify import _matches, visual_check

        v = (await visual_check(session, label, want=value, use_cache=False)) or ""
        return _matches(v)
    return False


_MAX_PICK_OPTS = 60  # BOUNDED LLM input (directive): type-to-filter already narrows a searchable taxonomy
# (School/Field/Skills — thousands of entries) to a handful; this HARD-caps the pathological unfiltered
# listbox so the pick prompt can never blow up. The correct option for a searchable field is always in the
# filtered top, so the cap never drops it in practice.
_PICK_CACHE: dict = {}  # (value, options) -> chosen text. The 5 language-proficiency selects share the
# SAME value ("Native or Bilingual") AND options ("1-Beginner".."5-Native") — without this each fires its
# own ~3s LLM call (the 15s/field the user hit). Identical (value,options) -> ONE call, reused.


async def _llm_pick(llm, value: str, options: list[str]) -> str | None:
    """The agent's 'which option' decision, made CHEAPLY by a text LLM over the READ options (no vision):
    pick the closest by meaning/abbreviation ('BS'->Bachelor's, 'Python'->nearest skill). Memoised on
    (value, options) so repeated identical picks (language proficiencies) cost ONE LLM call, not N."""
    import contextlib

    if llm is None or not options:
        print(f"  [llm_pick] want={value!r} -> no llm ({llm is None}) / no options ({not options})", flush=True)
        return None
    options = options[:_MAX_PICK_OPTS]  # bound the prompt payload (see _MAX_PICK_OPTS)
    ckey = (norm(value).lower(), tuple(options))
    if ckey in _PICK_CACHE:
        return _PICK_CACHE[ckey]
    from pydantic import BaseModel

    from browser_use.llm.messages import SystemMessage, UserMessage

    class _Pick(BaseModel):
        choice: str  # EXACT option text from the list, or "NONE"

    with contextlib.suppress(Exception):
        # BOUNDED + fallback-capable: route the per-field pick through oa_llm so a stalled gemini fails
        # fast (OA_LLM_TIMEOUT) and a second provider answers, instead of an unbounded ainvoke hanging
        # the field. None -> treated as no-pick (caller's Other-guard / revalue), never a hang.
        import oa_llm as _oa_llm

        res = await _oa_llm.resilient_text(
            [
                SystemMessage(
                    content="Pick the option a human applicant would select for the wanted value — "
                    "closest meaning or abbreviation (e.g. 'B.S.' -> \"Bachelor's Degree\"; 'Python' -> "
                    "the nearest skill). If no option matches directly, still pick the CLOSEST REASONABLE "
                    "one (e.g. wanted 'Mobile' with only ['Home','Home Cellular'] -> 'Home Cellular'). "
                    "If the options look like CATEGORIES of a hierarchical menu, pick the category that "
                    "would CONTAIN the wanted value (e.g. wanted 'LinkedIn' -> 'Social Media'). Reply the "
                    "EXACT option text from the list. Reply 'NONE' ONLY when every option is clearly "
                    "unrelated to the wanted value (a placeholder like 'Select One' never counts as a match)."
                ),
                UserMessage(content=f"wanted: {value!r}\noptions: {options}"),
            ],
            output_format=_Pick,
            primary=llm,
        )
        if res is None:
            print(f"  [llm_pick] want={value!r} -> LLM unanswered (timeout/None)", flush=True)
            return None
        c = (res.completion.choice or "").strip()
        out = None if c.upper() == "NONE" or not c else c
        print(f"  [llm_pick] want={value!r} opts[:6]={options[:6]} -> {out!r}", flush=True)
        _PICK_CACHE[ckey] = out  # memoise so identical later picks (proficiency selects) are instant
        return out
    print(f"  [llm_pick] want={value!r} -> EXCEPTION in pick call (suppressed)", flush=True)
    return None


# ONLY currently-VISIBLE options of the OPEN dropdown — the option portal (activeListContainer) is a
# SHARED body singleton, so a closed widget's options linger hidden AND a freshly-typed filter re-renders
# it. offsetParent is null for hidden/detached nodes; a committed pill's sub-elements carry promptOption
# too, so skip anything inside a selectedItemList. (Mirrors ats_workday._VISIBLE_TEXT_JS — the proven read.)
_PICK_VISIBLE_JS = (
    "() => { if (this.closest('[data-automation-id=\"selectedItemList\"]')) return '';"
    " const r=this.getBoundingClientRect();"
    " return (r.width>0 && r.height>0 && this.offsetParent!==null) ? (this.textContent||'') : ''; }"
)
_OPT_SEL = (
    '[data-automation-id="activeListContainer"] [role="option"], [data-automation-id="promptOption"], '
    '[data-automation-id="menuItem"], [role="listbox"] [role="option"]'
)


async def _read_visible_options(page) -> list:
    """Read the OPEN dropdown's visible options as [(handle, normtext)] — the active widget's only."""
    import contextlib

    raw = await page.get_elements_by_css_selector(_OPT_SEL)
    opts: list = []
    for o in raw:
        with contextlib.suppress(Exception):
            t = norm(await o.evaluate(_PICK_VISIBLE_JS))
            if t:
                opts.append((o, t))
    return opts


async def pick_smart(adapter, page, llm, value: str, session=None, tries: int = 8, verify_label: str = "") -> bool:
    """Pick the best typeahead/listbox option for `value` and COMMIT it. The shared option portal returns
    a FROZEN/stale list when the widget hasn't re-rendered for the typed filter, so:
      1. POLL (bounded) until the visible option set CHANGES from the first read (the filter landed) — not
         a fixed sleep that buys nothing against a stale list.
      2. FROZEN-LIST EARLY-ABORT: if the visible list is IDENTICAL 3 reads in a row, the widget is dead —
         the DOM read is hopeless, so HAND OFF to the SHARED visual primitive (eng.pick_dropdown), which
         reads the ACTUALLY-rendered options from a screenshot (no DOM lag) and commits via trusted Enter.
      3. Once the DOM list reflects the filter: MATCH exact -> contains -> CHEAP LLM over the READ options.
      4. COMMIT via a TRUSTED CDP Enter on the pre-highlighted match (session given) — the same primitive
         Greenhouse react-select uses; a synthetic .click() on a portal node can land on a stale/detached
         element. Falls back to .click() only when no trusted-Enter session is available.

    The frozen-portal + the per-read "DOM doesn't contain the typed token" cases are exactly when the
    VLM screenshot read wins, so both route through ONE shared primitive (eng.pick_dropdown). When a
    session is available the whole pick goes through pick_dropdown so vision is always the fallback and
    the committed value is VLM-verified; the no-session path keeps the legacy DOM-click for offline use.
    Returns True iff an option was committed (and, when a session is given, VLM-verified)."""
    import contextlib

    # SESSION PATH: the shared visual primitive. DOM read first; on stale/empty/lagged -> VLM screenshot
    # read; match -> trusted-Enter commit -> value-aware VLM verify. This is the matching fix.
    if session is not None:
        from ats_engine import pick_dropdown

        async def _dom(_page):
            return [t for _, t in await _read_visible_options(_page)]

        # let the filter settle a touch so the DOM read isn't the pre-type frozen list on the first look
        await _wait_options_change(page, [])
        ok = await pick_dropdown(
            session,
            page,
            value,
            read_dom_options=_dom,
            llm=llm,
            verify_label=verify_label or None,
            vis_key=f"{verify_label or 'pick'}:{value}",
        )
        with contextlib.suppress(Exception):
            await _wait_options_change(page, [t for _, t in await _read_visible_options(page)])
        return ok

    # NO-SESSION (offline / legacy): DOM-only poll + click the matched node; no trusted Enter available.
    want = norm(value)
    prev: list[str] | None = None
    frozen = 0
    for _ in range(tries):
        opts = await _read_visible_options(page)
        texts = [t for _, t in opts]
        if prev is not None and texts == prev:  # list unchanged since last read
            frozen += 1
            if frozen >= 2:  # identical 3 reads total -> dead widget
                print(f"  [pick] want={value!r} FROZEN list ({texts[:5]}) -> abort", flush=True)
                return False
        else:
            frozen = 0
        prev = texts
        if not opts:
            await _wait_options_change(page, [])  # bounded wait for the menu to mount
            continue
        # MATCHING = LLM ONLY (directive 3): no substring/contains. An EXACT option-text equality is the
        # only deterministic shortcut (it is not a substring test); everything else is the LLM's single
        # best-option pick over the READ option strings (it canonicalizes 'BS' -> "Bachelor's", etc.).
        target = next((o for o, t in opts if t == want), None)
        choice = None
        if target is None and llm is not None:
            choice = await _llm_pick(llm, value, texts)  # the agent's decision, replayed
            if choice:
                target = next((o for o, t in opts if t == norm(choice)), None)
        print(f"  [pick] want={value!r} opts={texts[:5]} choice={choice!r} hit={target is not None}", flush=True)
        if target is not None:
            with contextlib.suppress(Exception):
                await target.click()
                await _wait_options_change(page, texts)  # bounded: menu closes / re-renders on commit
                return True
        else:
            # no match in THIS read — wait (bounded) for the filter to settle, then re-read
            await _wait_options_change(page, texts)
    return False


async def _wait_options_change(page, baseline: list[str], timeout: float = 2.0, step: float = 0.2) -> bool:
    """Bounded wait-for-condition: poll the visible options until they DIFFER from `baseline` (the menu
    re-rendered for the typed filter / committed / closed). Returns True on change, False on timeout —
    replaces fixed asyncio.sleep so a settled widget proceeds immediately and a dead one bails in ~2s."""
    elapsed = 0.0
    while elapsed < timeout:
        await asyncio.sleep(step)
        elapsed += step
        cur = [t for _, t in await _read_visible_options(page)]
        if cur != baseline:
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
                await el.evaluate(
                    "() => { if(!this.checked){ this.click(); "
                    "this.dispatchEvent(new Event('change',{bubbles:true})); } }"
                )
                return True
        return False
    if a == "select":  # button-listbox: DELEGATE to the adapter's _listbox — trusted trigger click +
        # no-blur typing + commit-by-node + Escape disarm (the proven path). This branch previously
        # re-implemented it with a SYNTHETIC trig.click() that often never opened the menu (verified
        # live: Degree -> 'NO options (dom+vision both empty — menu not open?)' -> residual -> agent).
        from ats_engine import FormField

        fld = FormField(name=c.fkit, type="single_select", label=c.label, source="standard")
        return await adapter._listbox(session, page, fld, value)
    if a == "chip":  # typeahead TAG (Skills) / education searchable chip: REUSE the proven adapter
        # multiselect commit (type -> filter -> TRUSTED ENTER on the highlighted top -> read_back). One
        # pill per comma-item. No bespoke re-implementation — _multiselect already handles the widget.
        from ats_engine import FormField

        fld = FormField(name=c.fkit, type="multi_select", label=c.label, source="standard")
        items = [x.strip() for x in value.split(",")] if "," in value else [value]
        # DELTA-ONLY: a Workday multiselect TOGGLES — re-committing an already-present pill REMOVES it
        # (verified live: a second pass wiped Python/Kubernetes/PostgreSQL while chasing the missing
        # 'Go'). Read the pills ALREADY there and type only the genuinely missing items. Pill text is
        # CANONICALISED by the taxonomy ('Go' -> 'Go Programming Language'), so presence is judged by
        # the LLM (exact-equality shortcut first) — never substring (matching directive).
        have: list[str] = []
        got = ""
        try:
            got = await adapter._committed_value(page, fld)
        except Exception as exc:  # NEVER silent: a broken existing-pills read is what enabled the wipe
            print(f"  [chip {c.fkit}] _committed_value ERROR {type(exc).__name__}: {exc}", flush=True)
        if not got and session is not None:
            # DOM read '' does not mean empty — the VISUAL read is the other half of the committed-value
            # oracle (a busy SPA serializes filled widgets blank). Ask the VLM what the field SHOWS.
            with contextlib.suppress(Exception):
                got = await adapter._visual_value(session, fld, value)
        print(f"  [chip {c.fkit}] existing={got!r}", flush=True)
        have = [p.strip() for p in got.split(",") if p.strip()]

        async def _present(it: str) -> bool:
            from ats_workday import _bare_eq, _llm_value_matches

            # deterministic first: 'Python (Programming Language)' / '... (Suggested)' IS 'Python'
            if any(p.lower() == it.lower() or _bare_eq(p, it) for p in have):
                return True
            for p in have:
                if await _llm_value_matches(p, it):  # cached; 2 short strings
                    return True
            return False

        # RESPECT AUTOFILL on single-value chips (agreed policy — the agent prompt already codifies
        # it): a chip that HOLDS a committed value is COMPLETE even when it differs from the profile
        # ('University of California, Davis' from the résumé vs profile 'Berkeley'). Re-typing burned
        # 2 rounds + a verify-noop agent per tenant and can only replace résumé truth with a guess.
        if have and len(items) == 1 and not await _present(items[0]):
            print(f"  [chip {c.fkit}] autofill kept: {have[0]!r} (profile wanted {items[0]!r})", flush=True)
            return True

        added = 0
        misses = 0
        for it in items:
            # exclusive=False: adding one skill must NEVER trim sibling pills (suggested skills are legit)
            if not it or await _present(it):
                continue
            if await adapter._multiselect(session, page, fld, it, exclusive=False):
                added += 1
                misses = 0
            else:
                misses += 1
                if misses >= 2:
                    # TAXONOMY DEAD (suggestion-only list wiped / 'No Items' for every query — verified
                    # live): two consecutive unfillable items means the rest fail identically; stop
                    # burning ~35s per sibling and let the residual report own it.
                    print(f"  [chip {c.fkit}] taxonomy dead after 2 misses — skipping remaining items", flush=True)
                    break
        return added > 0 or bool(have)
    if a == "date":
        from ats_engine import FormField

        return await adapter._date(
            session, page, FormField(name=c.fkit, type="date", label=c.label, source="standard"), value
        )
    return False


async def _chip_commit_visual(session, page, label: str, item: str, llm=None) -> bool:
    """Commit ONE chip pill (School / Field of Study / Skills) by a MATCHED TRUSTED-CLICK — never blind.
    The Workday typeahead does NOT auto-highlight the best match, so ArrowDown+Enter lands the FIRST
    suggestion (e.g. 'Acupuncture & Integrative Medicine College, Berkeley' for 'University of
    California, Berkeley' — wrong). Instead: POLL the rendered suggestions as (element, text) until the
    menu settles on the typed filter (render gate, not a match), let the LLM pick the RIGHT text (no
    regex/substring), TRUSTED-CLICK that SPECIFIC element, then value-aware VLM-verify the pill. Returns
    True iff committed AND the VLM confirms the pill. False with no session (offline -> caller fallback).
    """
    import contextlib

    if session is None:
        return False
    from ats_engine import hover_trusted
    from vision_verify import _matches, visual_check

    # RENDER GATE: poll the DOM suggestions (element,text) until the menu has re-rendered for the typed
    # filter (a non-empty list that CHANGED from the previous read) — not a fixed sleep, not a match.
    opts: list = []
    prev: list | None = None
    for _ in range(12):
        opts = await _read_visible_options(page)
        texts = [t for _, t in opts]
        if texts and texts != prev:
            break
        prev = texts
        await asyncio.sleep(0.3)
    if not opts:
        return False
    texts = [t for _, t in opts]
    choice = await _llm_pick(llm, item, texts)  # LLM picks the RIGHT suggestion (no substring/regex)
    if not choice:
        return False
    el = next((e for e, t in opts if nkey(t) == nkey(choice)), None)  # locate the chosen element (equality)
    if el is None:
        return False
    # COMMIT the PROVEN way (reuse _multiselect's mechanism): HOVER the matched option to highlight it,
    # then a TRUSTED ENTER commits it into a pill AND closes the menu. A trusted CLICK on the multi-pill
    # Skills suggestion leaves the menu OPEN (verified: the agent saw 'menu is still open') — Enter is
    # what commits. Hover targets the RIGHT option (not the blind top), so School lands UC-Berkeley.
    await hover_trusted(session, page, el)
    await asyncio.sleep(0.12)
    await eng_press_enter(session, page)
    await asyncio.sleep(0.4)
    with contextlib.suppress(Exception):
        return _matches(await visual_check(session, label, want=item, use_cache=False))
    return True


async def eng_press_enter(session, page) -> None:
    from ats_engine import press_enter_trusted

    with __import__("contextlib").suppress(Exception):
        await press_enter_trusted(session, page)


# Workday section headings carry the section keyword (KW); every section's Add control shares the
# IDENTICAL text "Add Another" (verified in the Intel fixture: Work-Experience and Education both
# read "Add Another"), so TEXT can never disambiguate which section's Add to click. The ROW-SAFE
# anchor is DOCUMENT ORDER: a section's Add control is the FIRST add-control whose position is AFTER
# that section's heading and BEFORE the NEXT section's heading. This mirrors _assign_sections
# (nearest-heading-before), the proven section-assignment logic, applied to the add controls.
_ADD_SEL = '[data-automation-id="add-button"], button, [role="button"]'
_HEAD_KW = {
    "experience": "experience",
    "education": "education",
    "skills": "skill",
    "languages": "language",
    "certifications": "certification",
}


# Find the RIGHT section's Add control (label-agnostic) and return its on-screen CENTER coords (for a
# trusted click), or '' if none. Section-scoped by DOCUMENT ORDER (heading kw -> next section heading),
# never by button text. The add control is matched GENERICALLY so any tenant label works: its
# data-automation-id contains 'add' (add-button / addButton / ...) OR its visible text STARTS WITH 'add'
# ('Add', 'Add Another', 'Add Experience', '+ Add', 'Add another work experience' ...).
_FIND_ADD_JS = r"""(args) => {
  const [sec, kw] = args;  // sec = canonical section key; kw = heading keyword (fallback only)
  const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const isAdd = el => {
    const aid=(el.getAttribute('data-automation-id')||'').toLowerCase();
    const t=(el.textContent||'').trim().toLowerCase();
    return (aid.includes('add') || /^\+?\s*add\b/.test(t)) && t.length<40;
  };
  const center = el => { el.scrollIntoView({block:'center'});
    const r=el.getBoundingClientRect();
    return (r.width && r.height) ? JSON.stringify({x:r.left+r.width/2, y:r.top+r.height/2}) : ''; };
  // STRUCTURAL (title-ignorant, verified live on nvidia): an Add control belongs to the section whose
  // fkit rows live in its NEAREST fkit-holding ancestor (experience Add's parent panel holds
  // workExperience-*; education Add's holds education-*). Heading text never consulted.
  const SEC = {workexperience:'experience',experience:'experience',education:'education',
               skills:'skills',skill:'skills',languages:'languages',language:'languages',
               certifications:'certifications',certification:'certifications'};
  const secOf = fk => { const m=(fk||'').split('--')[0].replace(/-\d+$/,'').toLowerCase(); return SEC[m]||m; };
  const adds = [...document.querySelectorAll('button,[role="button"]')].filter(isAdd);
  for (const b of adds) {
    let a = b.parentElement, hops = 0;
    while (a && a !== document.body && hops < 12) {
      const n = a.querySelector && a.querySelector('[data-fkit-id]');
      if (n) { if (secOf(n.getAttribute('data-fkit-id')) === sec) { const c = center(b); if (c) return c; } break; }
      a = a.parentElement; hops++;
    }
  }
  // FALLBACK (zero-row collapsed section: no fkit anywhere to anchor on): nearest-heading document
  // order — the only signal left. Known ceiling: a tenant that RENAMES the heading AND starts the
  // section collapsed defeats this; structural above covers every mounted/autofilled case.
  const KW = ['experience','education','skill','language','certification'];
  const nodes = [...document.querySelectorAll('*')];
  const isHead = el => /^(H1|H2|H3|H4)$/.test(el.tagName) || el.getAttribute('role')==='heading'
                       || (el.getAttribute('data-automation-id')||'').includes('Title');
  let myPos=-1, nextPos=nodes.length;
  for (let i=0;i<nodes.length;i++){ const el=nodes[i];
    if(!isHead(el)) continue; const t=norm(el.textContent); if(t.length>40) continue;
    if(myPos===-1 && t.includes(kw)) { myPos=i; continue; }
    if(myPos!==-1 && KW.some(k=>k!==kw && t.includes(k))) { nextPos=i; break; }
  }
  if(myPos===-1) return '';
  for (let i=myPos+1;i<nextPos && i<nodes.length;i++){ const el=nodes[i];
    if((el.tagName==='BUTTON'||el.getAttribute('role')==='button') && isAdd(el)){
      const c = center(el); if (c) return c;
    }
  }
  return ''; }"""


async def _add_row(session, page, sec: str) -> bool:
    """Mount the next row for `sec` by a TRUSTED CDP click on that section's Add control. Generic: the
    button is located by document order + a label-AGNOSTIC match (aid contains 'add' OR text starts with
    'add'), and committed with a TRUSTED mouse click — a synthetic .click() does NOT fire a React
    'Add Another' button (verified: HP mounted 0 rows via .click(); the agent's real click worked).
    Returns True iff clicked; the caller (ensure_rows) re-reads the count to confirm a row mounted."""
    import contextlib
    import json

    kw = _HEAD_KW.get(sec, sec)
    box = None
    with contextlib.suppress(Exception):
        raw = await page.evaluate(_FIND_ADD_JS, [sec, kw])
        box = json.loads(raw) if isinstance(raw, str) and raw else None
    if not isinstance(box, dict):
        return False
    with contextlib.suppress(Exception):
        sid = await page.session_id
        for ev in (
            {"type": "mouseMoved", "x": box["x"], "y": box["y"], "buttons": 0},
            {"type": "mousePressed", "x": box["x"], "y": box["y"], "button": "left", "buttons": 1, "clickCount": 1},
            {"type": "mouseReleased", "x": box["x"], "y": box["y"], "button": "left", "buttons": 0, "clickCount": 1},
        ):
            await session.cdp_client.send.Input.dispatchMouseEvent(params=ev, session_id=sid)
        await asyncio.sleep(1.2)  # let the new row mount
        return True
    return False


# ROW-repeater sections (each entry = an Add-Another row). Skills is a TAG (pills via chip put), NOT here.
_ROW_SECTIONS = {
    "experience": ("experience", "work_experience"),
    "education": ("education",),
    "languages": ("languages",),
    "certifications": ("certifications",),
}


async def ensure_rows(adapter, session, page, profile: dict) -> bool:
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
        # HARD CAP + NO-SPIN dup-guard (the 9-row fix): re-read the live row COUNT each iteration and
        # add ONLY while have < want. The loop bound is `want` (never more clicks than the gap to fill),
        # and if a click does NOT increase the count (wrong/dead Add control, or count can't advance) we
        # STOP this section immediately — never spin firing Adds the count can't see. So mounted rows for
        # the section can NEVER exceed `want` (= len(profile[section])).
        for _ in range(want):
            controls = await extract_live(page)
            have = len(_rows_of(controls, sec))
            if have >= want:  # dup-guard: at/over target -> never add past the profile count
                break
            if not await _add_row(session, page, sec):  # section not on this page / no Add control -> stop
                break
            controls = await extract_live(page)
            if len(_rows_of(controls, sec)) <= have:  # click did NOT mount a new row -> stop (no spin)
                break
            added = True
    return added


async def fill_deterministic(adapter, session, page, profile: dict, llm, title: str = "", max_rounds: int = 3) -> dict:
    """The fixpoint reconcile-and-repair loop. FIRST mount rows from the profile (so collapsed sections'
    fields exist), THEN one semantic map call, THEN loop: reconcile(read-back) -> put() MISSING/DIVERGED
    -> until the DOM is stable. Returns a ledger summary. NEVER submits. Agent escalation is the backstop.

    ensure_rows + make_plan run ONCE upfront (rows persist + labels don't change), NOT per round — the
    per-round re-mount + re-LLM made the loop too slow to finish within the step budget (timeout)."""
    import time

    t0 = time.monotonic()
    rows_added = await ensure_rows(adapter, session, page, profile)  # bootstrap ONCE: mount rows so fields appear
    t_ensure = time.monotonic() - t0
    controls = await extract_live(page)  # now collapsed sections have controls
    plan = await make_plan(llm, controls, profile, title)  # ONE semantic map: labels known -> values
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
                # hard typeaheads: DOM read-back is flaky, so ask the cheap VALUE-AWARE VLM whether the
                # INTENDED value is already on screen ($0.0006, cached) before re-filling — skips a
                # genuinely-correct field, but does NOT rubber-stamp a wrong committed value as done.
                if fd.control.archetype() in ("select", "chip") and await _vlm_filled(
                    session, fd.control.label, fd.intended
                ):
                    continue
                tp = time.monotonic()
                try:
                    ok = await put(adapter, session, page, fd.control, fd.intended, llm)
                except Exception as exc:
                    # CRASH CONTAINMENT (generic): one field's exception must never kill the page
                    # engine — paypal lost Degree+Overall because ONE msel diagnostic raised and the
                    # whole fixpoint loop died. Blast radius = this field (residual), loop continues.
                    print(
                        f"  [wd] put ERROR {fd.section}[{fd.row}].{fd.label}: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    ok = False
                dt = time.monotonic() - tp
                slow[fd.control.archetype()] = slow.get(fd.control.archetype(), 0.0) + dt
                if ok:
                    summary["filled"] += 1
        print(
            f"  [wd] round {rnd + 1}: {len(todo)} todo, {time.monotonic() - tr:.1f}s, "
            f"put-time-by-type={ {k: round(v, 1) for k, v in slow.items()} }",
            flush=True,
        )
        await asyncio.sleep(0.5)
    # SETTLE before the final read + VLM residual check: the last chip can leave its suggestion menu
    # OPEN, so the VLM would read the open menu (not the committed pill) and FALSE-flag a filled chip as
    # residual. The Salesforce/Workday typeahead does NOT close on Escape — it closes on CLICK-OUTSIDE.
    # So dispatch a TRUSTED click in the empty left margin (x=6, no field there), then Escape + blur.
    import contextlib

    with contextlib.suppress(Exception):
        from ats_engine import press_key_trusted

        sid = await page.session_id
        for ev in (
            {"type": "mousePressed", "x": 6, "y": 320, "button": "left", "buttons": 1, "clickCount": 1},
            {"type": "mouseReleased", "x": 6, "y": 320, "button": "left", "buttons": 0, "clickCount": 1},
        ):
            await session.cdp_client.send.Input.dispatchMouseEvent(params=ev, session_id=sid)
        await press_key_trusted(session, page, key="Escape", code="Escape", vk=27)
        await page.evaluate(
            "() => { if(document.activeElement&&document.activeElement.blur) document.activeElement.blur(); }"
        )
        await asyncio.sleep(0.8)
    final_controls = await extract_live(page)
    final_diff = reconcile(plan, final_controls, await read_live(page, final_controls))
    # VLM-confirm the residual: a typeahead the DOM reads empty may actually be filled on screen —
    # the cheap VLM is the ground truth, so the residual reflects what the user would SEE, not a flaky read.
    real_residual = []
    for f in final_diff.todo():
        if (
            f.control
            and f.control.archetype() in ("select", "chip")
            and await _vlm_filled(session, f.label, f.intended)
        ):
            summary["filled"] += 1
            continue
        real_residual.append(f"{f.section}[{f.row}].{f.label}")
    summary["residual"] = real_residual
    summary["secs"] = round(time.monotonic() - t0, 1)
    # VERIFICATION AID: capture the filled My-Experience page (the heavy SPA errors the per-step
    # screenshot, so grab it here, robustly, via CDP) so the fill can be checked VISUALLY.
    import base64
    import contextlib
    import os
    import pathlib

    shot_path = os.environ.get("WD_MYEXP_SHOT")
    if shot_path:
        # SETTLE first: close any open suggestion menu (Escape) + blur, so the capture is a clean
        # end-state (not a chip mid-commit), then wait a beat for the re-render.
        with contextlib.suppress(Exception):
            from ats_engine import press_key_trusted

            await press_key_trusted(session, page, key="Escape", code="Escape", vk=27)
            await page.evaluate(
                "() => { if(document.activeElement&&document.activeElement.blur) document.activeElement.blur(); }"
            )
            await asyncio.sleep(0.8)
        with contextlib.suppress(Exception):
            sid = page.session_id
            if hasattr(sid, "__await__"):
                sid = await sid
            r = await session.cdp_client.send.Page.captureScreenshot(
                params={"format": "png", "captureBeyondViewport": True}, session_id=sid
            )
            pathlib.Path(shot_path).write_bytes(base64.b64decode(r["data"]))
            print(f"  [wd] My-Experience screenshot -> {shot_path}", flush=True)
        # OFFLINE DIAGNOSIS dump: save the live My-Experience DOM so language/skill detection can be
        # debugged with pure-lxml asserts (offline-first), not more blind live runs.
        dump_path = os.environ.get("WD_MYEXP_DOM")
        if dump_path:
            with contextlib.suppress(Exception):
                html = await page.evaluate("() => document.documentElement.outerHTML")
                if isinstance(html, str) and html:
                    pathlib.Path(dump_path).write_text(html, encoding="utf-8")
                    print(f"  [wd] My-Experience DOM -> {dump_path}", flush=True)
    print(f"  [wd] TOTAL {summary['secs']}s filled={summary['filled']} residual={len(summary['residual'])}", flush=True)
    return summary


_PKEY = {
    "experience": ("experience", "work_experience"),
    "education": ("education",),
    "skills": ("skills",),
    "languages": ("languages",),
    "certifications": ("certifications",),
}
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
        if not items and sec == "languages":
            # DEFAULT (per spec): if a Languages section is present but the profile names no languages,
            # assume the applicant is a PROFICIENT English speaker. Only skipped when the profile DOES
            # specify languages (then those are used verbatim). Proficiency values map via the LLM/VLM
            # to the form's scale ('Native or Bilingual' -> '5 - Native…' / 'Fluent').
            items = [
                {
                    "language": "English",
                    "fluent": "yes",
                    "comprehension": "Native or Bilingual",
                    "overall": "Native or Bilingual",
                    "reading": "Native or Bilingual",
                    "speaking": "Native or Bilingual",
                    "writing": "Native or Bilingual",
                }
            ]
        if not items:
            continue
        labels = list(labelset.values())
        if sec in _TAG_SECTIONS:
            # TAG: ONE typeahead control holds MANY pills (Skills) — NOT N Add-Another rows. Plan a
            # single row whose value is the comma-joined list; put() adds each as a pill.
            joined = ", ".join(str(it) for it in items)
            plan[sec] = {
                "count": 1,
                "rows": [{labels[0]: ""}] if labels else [],
                "_items": [joined],
                "_labels": labels[:1],
            }
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
                    row[lbl] = next(
                        (
                            str(v)
                            for k, v in item.items()
                            if re.sub(r"[^a-z]", "", k.lower()) in lk or lk in re.sub(r"[^a-z]", "", k.lower())
                        ),
                        "",
                    )
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
    res = await llm.ainvoke(
        [SystemMessage(content=_PLAN_SYS), UserMessage(content=json.dumps(ctx, ensure_ascii=False))], output_format=_Out
    )
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
