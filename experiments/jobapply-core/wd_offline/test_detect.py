"""Offline asserts for wd_repeaters: fkit-id detector (row-aware) + reconcile, against the real Intel
DOM. Pure lxml, no browser, ms. Run: python wd_offline/test_detect.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wd_repeaters as wr

FIX = Path(__file__).resolve().parent / "fixtures" / "intel_step03_my_experience.html"


def _ensure_rows_invariant() -> tuple[bool, str]:
    """The 9-EDUCATION-ROW regression guard (directive 4): drive the REAL ensure_rows over a simulated
    live page and assert mounted rows NEVER exceed want, and a dead/wrong Add control STOPS instead of
    spinning. Monkeypatches extract_live/_add_row (the only browser touches) so the dup-guard + hard cap
    + no-spin logic run offline. The bug was: starting from 0 rows, a wrong-section/dead Add 'succeeds'
    but mounts nothing, and the old blind want+2 cap fired to 9 empty Education rows."""
    import asyncio

    orig_extract, orig_add = wr.extract_live, wr._add_row
    try:
        for mode, start, want in [
            ("good", 0, 1), ("good", 0, 2), ("good", 0, 3),
            ("dead_true", 0, 1), ("dead_true", 0, 9),  # the 9-row case: must NOT reach 9
            ("dead_false", 0, 9), ("good", 5, 1),       # already-mounted: dup-guard, no shrink/over-add
        ]:
            state = {"rows": list(range(start)), "adds": 0}

            async def _ex(_page, _s=state):
                cs = [wr.Control(tag="INPUT", itype="text", fkit=f"education-{r}--degree", sec="education",
                                 row=str(r), field_key="degree", label="Degree", doc_index=r)
                      for r in _s["rows"]]
                cs.append(wr.Control(tag="INPUT", itype="text", fkit="skills--skills", sec="skills",
                                     row="", field_key="skills", label="Skills", in_multiselect=True, doc_index=999))
                return cs

            async def _add(_session, _page, _sec, _s=state, _m=mode):
                _s["adds"] += 1
                if _m == "good":
                    _s["rows"].append(100 + len(_s["rows"]))
                    return True
                if _m == "dead_true":  # click 'succeeds' but mounts nothing -> must STOP
                    return True
                return False  # dead_false: no Add control -> must STOP

            wr.extract_live, wr._add_row = _ex, _add
            profile = {"education": [{"d": "x"}] * want}
            asyncio.new_event_loop().run_until_complete(wr.ensure_rows(None, None, None, profile))
            final = len(state["rows"])
            if final > max(want, start):
                return False, f"{mode} start={start} want={want} -> {final} rows (EXCEEDS)"
            if state["adds"] > want:
                return False, f"{mode} start={start} want={want} -> {state['adds']} adds (SPUN)"
        return True, "never exceeds want; dead/wrong Add stops (no 9-row spin)"
    finally:
        wr.extract_live, wr._add_row = orig_extract, orig_add


def main() -> int:
    controls = wr.extract_offline(FIX.read_text(encoding="utf-8", errors="ignore"))
    by_arche: dict = {}
    for c in controls:
        by_arche.setdefault(c.archetype(), []).append(c)
    secs = {c.sect() for c in controls if c.sect()}

    print(f"{'SECTION':<11}{'ROW':<5}{'FIELD_KEY':<14}{'ARCHE':<9}{'LABEL':<26}")
    print("-" * 78)
    for c in sorted(controls, key=lambda x: (x.sect() or "~", x.row, x.doc_index)):
        print(f"  {c.sect()!s:<9}{c.row:<5}{c.field_key:<14}{c.archetype():<9}{(c.label or '·')[:24]:<26}")

    exp_rows = wr._rows_of(controls, "experience")
    edu_rows = wr._rows_of(controls, "education")
    degree = [c for c in controls if "degree" in wr.nkey(c.label)]
    chips = by_arche.get("chip", [])
    false_chip = [c for c in chips if "selected" in wr.nkey(c.label)]
    pairs = [(c.sect(), c.row, c.field_key) for c in controls if c.field_key]

    # ---- detector asserts (fkit-id row-aware + the 3 dedup nit fixes) ----
    checks = [
        ("A1 sections via fkit (exp+edu+skill)", {"experience", "education", "skills"} <= secs, sorted(secs)),
        ("A2 rows: experience has 2 (225,226), education 1",
         len(exp_rows) == 2 and len(edu_rows) == 1, {"exp": exp_rows, "edu": edu_rows}),
        ("A4 archetypes (select/chip/date/check/text/textarea)",
         all(a in by_arche for a in ("select", "chip", "date", "check", "text", "textarea")),
         {k: len(v) for k, v in by_arche.items()}),
        ("A3 labels (>=80% resolved)",
         sum(1 for c in controls if c.label) >= 0.8 * len(controls),
         f"{sum(1 for c in controls if c.label)}/{len(controls)}"),
        ("NIT1 Degree once per row (no select+text twin)",
         len([c for c in degree if c.row == "247"]) == 1, [(c.archetype(), c.label) for c in degree]),
        ("NIT2 no selectedItemList false-chip", len(false_chip) == 0, [c.label for c in false_chip]),
        ("NIT3 no (sec,row,field_key) duplicates", len(pairs) == len(set(pairs)),
         sorted([p for p in pairs if pairs.count(p) > 1])),
        ("DATE labels distinct (startDate->Start Date, endDate->End Date)",
         {wr.nkey(c.label) for c in controls if c.field_key in ("startdate", "enddate")}
         == {"start date", "end date"},
         sorted({c.label for c in controls if c.field_key in ("startdate", "enddate")})),
    ]

    # ---- reconcile asserts: ROW-AWARE plan vs DOM ----
    plan = {
        "experience": {"count": 2, "rows": [
            {"Job Title": "Senior Software Engineer", "Company": "Acme"},
            {"Job Title": "Software Engineer", "Company": "Beta"}]},
        "education": {"count": 1, "rows": [{
            "School or University": "UC Berkeley", "Degree": "Bachelor's Degree",
            "Field of Study": "Computer Science"}]},
    }
    # empty DOM (no readback) -> all intended MISSING
    diff_empty = wr.reconcile(plan, controls, readback={})
    miss = [f for f in diff_empty.fields if f.status == "MISSING"]

    # post-fill readback keyed by REAL fkit ids: row 225 done, edu field-of-study equivalent, degree wrong
    def fkit(sec, row, fkey):
        c = next((c for c in controls if c.sect() == sec and c.row == row and fkey in c.field_key), None)
        return c.fkit if c else f"missing::{sec}:{row}:{fkey}"

    rb = {
        fkit("experience", "225", "jobtitle"): "Senior Software Engineer",        # exact -> DONE
        fkit("education", "247", "fieldofstudy"): "Computer Science & Engineering",  # equiv -> DONE
        fkit("education", "247", "degree"): "Doctorate",                          # wrong -> DIVERGED
    }
    diff_mixed = wr.reconcile(plan, controls, readback=rb)

    def st(sec, lab):
        return next((f.status for f in diff_mixed.fields if f.section == sec and wr.nkey(f.label) == wr.nkey(lab)), "?")

    # plan has 1 experience row beyond mounted? no — DOM has 2 (225,226), plan has 2 -> aligned, no overflow.
    # shrink plan to 1 exp row to exercise overflow:
    diff_overflow = wr.reconcile({"experience": {"count": 1, "rows": [{"Job Title": "X"}]}}, controls, {})

    checks += [
        ("RECONCILE empty -> all intended MISSING", len(miss) >= 6, f"{len(miss)} missing"),
        ("RECONCILE exact -> DONE", st("experience", "Job Title") == "DONE", st("experience", "Job Title")),
        ("RECONCILE semantic-equiv -> DONE", st("education", "Field of Study") == "DONE",
         st("education", "Field of Study")),
        ("RECONCILE respects autofill: filled (any value) -> DONE, not overwritten",
         st("education", "Degree") == "DONE", st("education", "Degree")),
        ("RECONCILE row overflow detects extra DOM row (226)",
         diff_overflow.row_overflow.get("experience") == ["226"], diff_overflow.row_overflow),
        ("ENSURE_ROWS never exceeds want (9-row guard); dead Add stops, no spin", *(_ensure_rows_invariant())),
    ]

    print("\n=== ASSERTIONS (offline, real DOM) ===")
    ok = True
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail}")
    print(f"\n{'>>> ALL PASS' if ok else '>>> SOME FAIL'}  ({len(controls)} controls, {len(secs)} sections)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
