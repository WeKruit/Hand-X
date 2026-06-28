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
        label = label_for(el)
        rawfld = fkit.split("--")[-1] if "--" in fkit else ""
        # a date segment's own label is just "Month"/"Year" — use the fkit field for the field label
        # (startDate -> "Start Date") so start/end dates are distinct + matchable by the plan.
        if rawfld and (not label or nkey(label) in _SEGMENT_WORDS):
            label = humanize(rawfld)
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
                if not v:
                    status = "DONE"                         # nothing intended
                elif dom_row is None or ctrl is None:
                    status = "MISSING"                      # row not mounted / control absent -> Add+fill
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
