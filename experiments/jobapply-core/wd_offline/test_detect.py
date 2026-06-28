"""Offline asserts for wd_repeaters: detector (nits fixed) + reconcile, against the real Intel DOM.
Pure lxml, no browser, ms. Run: python wd_offline/test_detect.py"""

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
    secs = {c.section for c in controls if c.section}
    labels = [wr.nkey(c.label) for c in controls]

    print(f"{'SECTION':<12}{'ARCHE':<9}{'LABEL':<30}{'wrapper_aid'}")
    print("-" * 80)
    for c in sorted(controls, key=lambda x: (x.section or "~", x.archetype() or "")):
        print(f"  {str(c.section):<10}{c.archetype():<9}{(c.label or '·')[:28]:<30}{c.wrapper_aid[:24]}")

    degree = [c for c in controls if "degree" in wr.nkey(c.label)]
    chips = by_arche.get("chip", [])
    false_chip = [c for c in chips if "selected" in wr.nkey(c.label) or "items selected" in wr.nkey(c.label)]

    # ---- detector asserts (incl. the 2 nit fixes) ----
    checks = [
        ("A1 sections (exp+edu+skill detected)",
         {"experience", "education", "skill"} <= secs, sorted(secs)),
        ("A4 archetypes (select/chip/date/check/text/textarea)",
         all(a in by_arche for a in ("select", "chip", "date", "check", "text", "textarea")),
         {k: len(v) for k, v in by_arche.items()}),
        ("A3 labels (>=80% resolved)",
         sum(1 for c in controls if c.label) >= 0.8 * len(controls),
         f"{sum(1 for c in controls if c.label)}/{len(controls)}"),
        ("NIT1 Degree appears ONCE (no select+text dup)", len(degree) == 1,
         [(c.archetype(), c.label) for c in degree]),
        ("NIT2 no selectedItemList false-chip", len(false_chip) == 0,
         [c.label for c in false_chip]),
        ("NIT3 no twin duplicates (each label once per section)",
         len([(c.section, wr.nkey(c.label)) for c in controls])
         == len({(c.section, wr.nkey(c.label)) for c in controls}),
         sorted([wr.nkey(c.label) for c in controls
                 if [wr.nkey(x.label) for x in controls].count(wr.nkey(c.label)) > 1])),
    ]

    # ---- reconcile asserts: a hand plan vs the initial (empty) DOM ----
    plan = {
        "experience": {"count": 1, "rows": [{
            "Job Title": "Senior Software Engineer", "Company": "Acme", "Location": "San Francisco, CA",
            "Role Description": "Built things.", "From": "2021-06", "To": "2024-01"}]},
        "education": {"count": 1, "rows": [{
            "School or University": "UC Berkeley", "Degree": "Bachelor's Degree",
            "Field of Study": "Computer Science", "Overall Result (GPA)": "3.8"}]},
        "skills": {"count": 1, "rows": [{"Skills": "Python"}]},
    }
    # initial DOM is empty -> readback all '' -> everything intended should be MISSING
    diff_empty = wr.reconcile(plan, controls, readback={})
    miss = [f for f in diff_empty.fields if f.status == "MISSING"]
    # simulate a post-fill readback where one field DIVERGED and one is an acceptable equivalent
    rb = {
        ("education", 0, wr.nkey("Field of Study")): "Computer Science & Engineering",  # equivalent -> DONE
        ("education", 0, wr.nkey("Degree")): "Doctorate",                               # wrong -> DIVERGED
        ("experience", 0, wr.nkey("Job Title")): "Senior Software Engineer",            # exact -> DONE
    }
    diff_mixed = wr.reconcile(plan, controls, readback=rb)
    fos = next(f for f in diff_mixed.fields if wr.nkey(f.label) == wr.nkey("Field of Study"))
    deg = next(f for f in diff_mixed.fields if wr.nkey(f.label) == wr.nkey("Degree"))
    jt = next(f for f in diff_mixed.fields if wr.nkey(f.label) == wr.nkey("Job Title"))

    checks += [
        ("RECONCILE empty DOM -> all intended MISSING",
         len(miss) >= 8 and all(f.status == "MISSING" for f in diff_empty.fields if f.intended),
         f"{len(miss)} missing"),
        ("RECONCILE semantic-equiv -> DONE (CS == CS & Engineering)", fos.status == "DONE", fos.status),
        ("RECONCILE wrong value -> DIVERGED (Doctorate != Bachelor's)", deg.status == "DIVERGED", deg.status),
        ("RECONCILE exact -> DONE", jt.status == "DONE", jt.status),
        ("RECONCILE clean() false while work remains", diff_mixed.clean() is False, diff_mixed.clean()),
    ]

    print("\n=== ASSERTIONS (offline, real DOM) ===")
    ok = True
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail}")
    print(f"\n{'>>> ALL PASS' if ok else '>>> SOME FAIL'}  ({len(controls)} controls)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
