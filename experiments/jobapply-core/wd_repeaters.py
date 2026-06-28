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
# canonical section keys used by the plan + profile
SECTION_KEYS = {"experience": "experience", "education": "education", "skill": "skills",
                "language": "languages", "certification": "certifications"}


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def nkey(s: str | None) -> str:
    """Normalised match key: lowercase, collapse ws, drop a trailing required '*' and punctuation."""
    return re.sub(r"[\s*:]+$", "", norm(s).lower()).strip()


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
    label: str = ""               # resolved visible label
    wrapper_aid: str = ""         # nearest ancestor data-automation-id (widget group)
    in_multiselect: bool = False  # inside a multiSelectContainer
    doc_index: int = 0            # document order
    section: str | None = None    # assigned section keyword
    handle: object = None         # live element handle (None offline)

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


def dedup(controls: list[Control]) -> list[Control]:
    """Collapse raw handles to ONE entry per logical widget. Fixes the 2 offline-found nits:
      (1) Degree dup: a select (button[haspopup]) + a hidden text input share a wrapper -> keep the
          select, drop the text twin.
      (2) selectedItemList false-chip: the pill CONTAINER ('items selected') is not an input -> drop
          any control whose aid/wrapper marks it the selectedItem(List) container, keep the typeahead."""
    out: list[Control] = []
    seen: set[str] = set()
    # group by wrapper to resolve select+text twins
    wrapper_has_rich: set[str] = set()
    for c in controls:
        if c.archetype() in ("select", "chip", "date", "check", "radio") and c.wrapper_aid:
            wrapper_has_rich.add(c.wrapper_aid)
    for c in controls:
        a = c.archetype()
        if a is None:
            continue
        # nit 2: drop the pills container masquerading as a control
        if "selecteditem" in (c.aid or "").lower():
            continue
        # nit 1: a plain text twin in a wrapper that already has a rich control is the hidden backing input
        if a == "text" and c.wrapper_aid and c.wrapper_aid in wrapper_has_rich:
            continue
        # one logical field == one Workday formField wrapper; collapse visible+hidden input twins by
        # wrapper. (Multi-row disambiguation is added by the row layer via the row index in the key.)
        gid = c.wrapper_aid or c.cid or c.aid
        key = f"{a}::{gid}::{nkey(c.label)}::{c.section}"
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


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
        wrapper_aid, in_ms = "", False
        for anc in el.iterancestors():
            aid = anc.get("data-automation-id")
            if aid == "multiSelectContainer":
                in_ms = True
            if aid and not wrapper_aid:
                wrapper_aid = aid
        controls.append(Control(
            tag=(el.tag if isinstance(el.tag, str) else "").upper(),
            role=(el.get("role") or "").lower(),
            haspopup=(el.get("aria-haspopup") or "").lower(),
            itype=(el.get("type") or "").lower(),
            cid=el.get("id") or "",
            aid=el.get("data-automation-id") or "",
            label=label_for(el),
            wrapper_aid=wrapper_aid,
            in_multiselect=in_ms,
            doc_index=order[rt.getpath(el)],
        ))
    _assign_sections(controls, heads)
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


def reconcile(plan: dict, controls: list[Control], readback: dict | None = None) -> Diff:
    """plan = {section: {"rows": [ {label: value} ], "count": N}} (skills/langs flattened to rows too).
    readback = {(section,row,label_key): committed_value} from the live DOM ('' if unread/empty).
    Pure: classifies each planned field; flags unplanned required controls + row overflow."""
    readback = readback or {}
    d = Diff()
    # index controls by (section, label_key) for unplanned detection
    planned_keys: set[tuple] = set()
    for sec, blk in plan.items():
        for ri, row in enumerate(blk.get("rows", [])):
            for label, value in row.items():
                planned_keys.add((sec, nkey(label)))
                got = readback.get((sec, ri, nkey(label)), "")
                if not str(value).strip():
                    status = "DONE"                       # nothing to fill
                elif semantic_equal(str(value), got):
                    status = "DONE" if got else "MISSING"
                else:
                    status = "DIVERGED" if got else "MISSING"
                d.fields.append(FieldDiff(sec, ri, label, str(value), status))
    # unplanned REQUIRED controls present in the DOM but absent from the plan
    for c in controls:
        sec = SECTION_KEYS.get(c.section or "", c.section)
        if sec and (sec, nkey(c.label)) not in planned_keys and c.label.endswith("*"):
            d.unplanned.append(c)
    # row overflow (dup-guard signal): more rows present than planned
    by_sec_rows: dict = {}
    for c in controls:
        sec = SECTION_KEYS.get(c.section or "", c.section)
        if sec:
            by_sec_rows.setdefault(sec, set())
    return d
