"""Score sweep500_results.json with the ESTABLISHED verdict (final_scorecard.py):
COMPLETE = complete OR (no missing_required AND no visually_unanswered).  DEAD/HITL excluded
from the confidence denominator. Confidence = COMPLETE / scoreable. Reports overall, per-profile,
per-ATS, and lists every non-COMPLETE scoreable run (the swarm-fix worklist)."""
import json, sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys
res = json.loads((HERE / (sys.argv[1] if len(sys.argv) > 1 else "sweep500b_results.json")).read_text())


def verdict(r):
    if r["status"] in ("CRASH", "TIMEOUT") or r.get("not_reached"):
        return "DEAD"
    if r["status"] in ("needs_human", "NEEDS_HUMAN"):
        return "HITL"
    if r["status"] == "BLOCKED":
        return "DEAD"
    miss = r.get("missing_required") or []
    vlm = r.get("visually_unanswered") or []
    if r.get("complete") or (not miss and not vlm):
        return "COMPLETE"
    return "NEAR" if (len(miss) + len(vlm)) <= 2 else "FAIL"


for r in res:
    r["verdict"] = verdict(r)

prof = lambda p: p.split("/")[-1].replace(".json", "").replace("profile_", "").replace("_profile", "")


def conf(rows):
    scoreable = [r for r in rows if r["verdict"] not in ("DEAD", "HITL")]
    comp = [r for r in scoreable if r["verdict"] == "COMPLETE"]
    return len(comp), len(scoreable), (len(comp) / len(scoreable) if scoreable else 0.0)


print(f"=== SWEEP500 CONFIDENCE ({len(res)} runs) ===")
c, s, pct = conf(res)
from collections import Counter
print(f"OVERALL: {c}/{s} scoreable COMPLETE = {pct:.1%}   verdicts={dict(Counter(r['verdict'] for r in res))}")

print("\n--- BY PROFILE ---")
byp = defaultdict(list)
for r in res:
    byp[prof(r["profile"])].append(r)
for p, rows in sorted(byp.items()):
    c, s, pct = conf(rows)
    print(f"  {p:<12} {c}/{s} = {pct:.1%}  ({dict(Counter(r['verdict'] for r in rows))})")

print("\n--- BY ATS ---")
bya = defaultdict(list)
for r in res:
    bya[r["ats"]].append(r)
for a, rows in sorted(bya.items()):
    c, s, pct = conf(rows)
    print(f"  {a:<12} {c}/{s} = {pct:.1%}  ({dict(Counter(r['verdict'] for r in rows))})")

# worklist: non-COMPLETE scoreable runs
work = [r for r in res if r["verdict"] in ("NEAR", "FAIL")]
print(f"\n--- SWARM-FIX WORKLIST: {len(work)} non-COMPLETE scoreable runs ---")
for r in sorted(work, key=lambda r: (r["verdict"], r["ats"])):
    miss = [m if isinstance(m, str) else (m.get("label") if isinstance(m, dict) else str(m))
            for m in (r.get("missing_required") or [])]
    vlm = r.get("visually_unanswered") or []
    print(f"  [{r['verdict']:<4}] {r['ats']:<10} {r['company'][:16]:<17} i={r['i']:<3} "
          f"miss={miss[:4]} vlm={len(vlm)}  {r['url'][:54]}")
(HERE / "sweep500_scored.json").write_text(json.dumps(res, indent=1))
