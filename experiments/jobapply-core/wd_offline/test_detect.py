"""Offline asserts for wd_repeaters: fkit-id detector (row-aware) + reconcile, against the real Intel
DOM. Pure lxml, no browser, ms. Run: python wd_offline/test_detect.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wd_repeaters as wr  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "intel_step03_my_experience.html"


def main() -> int:
    controls = wr.extract_offline(FIX.read_text(encoding="utf-8", errors="ignore"))
    by_arche: dict = {}
    for c in controls:
        by_arche.setdefault(c.archetype(), []).append(c)
    secs = {c.sect() for c in controls if c.sect()}

    print(f"{'SECTION':<11}{'ROW':<5}{'FIELD_KEY':<14}{'ARCHE':<9}{'LABEL':<26}")
    print("-" * 78)
    for c in sorted(controls, key=lambda x: (x.sect() or "~", x.row, x.doc_index)):
        print(f"  {str(c.sect()):<9}{c.row:<5}{c.field_key:<14}{c.archetype():<9}{(c.label or '·')[:24]:<26}")

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
        ("RECONCILE wrong -> DIVERGED", st("education", "Degree") == "DIVERGED", st("education", "Degree")),
        ("RECONCILE row overflow detects extra DOM row (226)",
         diff_overflow.row_overflow.get("experience") == ["226"], diff_overflow.row_overflow),
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
