"""OFFLINE verification of the Workday-repeater design assumptions (A1-A4, A13) against a SAVED
My-Experience DOM — PURE lxml, no browser, no hang. Ports the structural detector (section / control /
label / archetype) to Python and asserts on the real captured DOM in milliseconds."""

import re
import sys
from pathlib import Path

import lxml.html

HTML = Path(sys.argv[1] if len(sys.argv) > 1 else "wd_step03_nojs.html").resolve()
KW = ["experience", "education", "skill", "language", "certification", "website", "resume", "social"]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def archetype(el) -> str | None:
    tag = (el.tag if isinstance(el.tag, str) else "").upper()
    role = (el.get("role") or "").lower()
    pop = (el.get("aria-haspopup") or "").lower()
    t = (el.get("type") or "").lower()
    for anc in el.iterancestors():
        if anc.get("data-automation-id") == "multiSelectContainer":
            return "chip"
    if role == "spinbutton":
        return "date"
    if tag == "SELECT" or role == "listbox" or pop == "listbox":
        return "select"
    if t == "checkbox" or role == "checkbox":
        return "check"
    if t == "radio" or role == "radio":
        return "radio"
    if tag == "TEXTAREA":
        return "textarea"
    if tag == "INPUT" and t in ("text", "email", "tel", "url", ""):
        return "text"
    return None


def main():
    tree = lxml.html.fromstring(HTML.read_text(encoding="utf-8", errors="ignore"))
    rt = tree.getroottree()
    by_id = {el.get("id"): el for el in tree.iter() if el.get("id")}
    order = {rt.getpath(el): i for i, el in enumerate(tree.iter())}  # stable doc-order index

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

    # headings: short + keyword
    heads = []
    for n in tree.xpath('//h1|//h2|//h3|//h4|//*[@role="heading"]|//*[contains(@data-automation-id,"Title")]'):
        t = norm(n.text_content()).lower()
        if t and len(t) <= 40 and any(k in t for k in KW):
            heads.append((order[rt.getpath(n)], t, n))
    heads.sort()

    def section_of(el) -> str | None:
        pos = order[rt.getpath(el)]
        best = None
        for hpos, t, _ in heads:
            if hpos < pos:
                best = t
        return next((k for k in KW if best and k in best), None) if best else None

    # controls
    raw = tree.xpath('//input|//select|//textarea|//*[@role="spinbutton"]|//*[@role="listbox"]'
                     '|//button[@aria-haspopup="listbox"]')
    seen, controls = set(), []
    for el in raw:
        a = archetype(el)
        if not a:
            continue
        lab = label_for(el)
        # collapse date-segments + chip inputs to ONE widget entry
        gid = ""
        if a in ("date", "chip"):
            for anc in el.iterancestors():
                if anc.get("data-automation-id"):
                    gid = anc.get("data-automation-id")
                    break
        key = f"{a}::{gid}::{lab}::{section_of(el)}"
        if key in seen:
            continue
        seen.add(key)
        controls.append({"section": section_of(el), "arche": a, "label": lab,
                         "aid": el.get("data-automation-id") or ""})

    # ---- report ----
    print(f"\nHEADINGS: {[t for _, t, _ in heads]}")
    by_sec: dict = {}
    for c in controls:
        by_sec.setdefault(c["section"], []).append(c)
    print(f"\n{'SECTION':<13}{'ARCHETYPE':<10}{'LABEL':<34}{'aid'}")
    print("-" * 92)
    for sec in sorted(by_sec, key=lambda x: (x is None, x or "")):
        for c in sorted(by_sec[sec], key=lambda x: x["arche"]):
            print(f"  {str(sec):<11}{c['arche']:<10}{(c['label'] or '·')[:32]:<34}{c['aid'][:26]}")

    # ---- assertions A1-A4, A13 ----
    secs = {c["section"] for c in controls if c["section"]}
    arche: dict = {}
    for c in controls:
        arche[c["arche"]] = arche.get(c["arche"], 0) + 1
    lang_sel = [c for c in controls if c["section"] == "language" and c["arche"] == "select"]
    labeled = sum(1 for c in controls if c["label"])
    checks = [
        ("A1 sections (>=3 of exp/edu/skill/lang)",
         len(secs & {"experience", "education", "skill", "language"}) >= 3, sorted(secs)),
        ("A4 archetypes (select+chip+date+check present)",
         all(k in arche for k in ("select", "chip", "date", "check")), arche),
        ("A3 labels resolved (>=50% controls)",
         labeled >= 0.5 * max(1, len(controls)), f"{labeled}/{len(controls)}"),
        ("A13 language proficiency = selects (>=1)", len(lang_sel) >= 1, [c["label"] for c in lang_sel]),
    ]
    print("\n=== ASSERTIONS (offline, real DOM) ===")
    ok = True
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail}")
    print(f"\n{'>>> ALL PASS' if ok else '>>> SOME FAIL'}  ({len(controls)} controls, {len(secs)} sections)")


if __name__ == "__main__":
    main()
