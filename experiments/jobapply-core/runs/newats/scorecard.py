"""Per-ATS fill-confidence scorecard over the mega/mega2 sweeps.

Buckets per run (json + completeness verdict, honest by construction):
  DEAD        — posting gone / error page / NOT_REACHED with no form evidence
  HITL        — NEEDS_HUMAN (auth wall, captcha, anti-bot) — excluded from fill confidence
  COMPLETE    — completeness.complete True (machine green)
  NEAR        — filled but required_escalated/missing/flags (honest incomplete)
  FAIL        — reached a form and filled ~nothing

fill-confidence = COMPLETE / (COMPLETE + NEAR + FAIL)   [DEAD + HITL excluded]
Machine-green still needs the eyeball pass for the human-standard number; this
scorecard is the live tracker between eyeballs.
"""

import collections
import glob
import json


def plat(u: str) -> str:
    for k in (
        "greenhouse", "lever.co", "ashbyhq", "comeet", "workable", "breezy",
        "bamboohr", "hibob", "rippling", "lydia-app", "teamtailor",
        "smartrecruiters", "icims", "avature", "phenom", "successfactors",
        "oracle", "adp", "eightfold", "amazon",
    ):
        if k in u:
            return k
    return "other"


def bucket(d: dict) -> str:
    c = d.get("completeness") or {}
    status = d.get("status") or ""
    url = (d.get("final_url") or "") + (d.get("url") or "")
    if status == "NEEDS_HUMAN":
        return "HITL"
    if c.get("not_reached") or "error=true" in url:
        return "DEAD" if (d.get("filled") or 0) == 0 else "NEAR"
    if status == "BLOCKED":
        return "DEAD"
    if c.get("complete"):
        return "COMPLETE"
    if (d.get("filled") or 0) >= 3:
        return "NEAR"
    return "FAIL"


def main() -> None:
    rows = collections.defaultdict(collections.Counter)
    near_examples = collections.defaultdict(list)
    for p in sorted(glob.glob("runs/newats/mega/*.json")) + sorted(glob.glob("runs/newats/mega2/*.json")):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        u = d.get("url") or ""
        b = bucket(d)
        rows[plat(u)][b] += 1
        if b in ("NEAR", "FAIL"):
            c = d.get("completeness") or {}
            why = (c.get("required_escalated") or c.get("missing_required") or c.get("visually_unanswered") or ["?"])[:1]
            near_examples[plat(u)].append(f"{p.split('/')[-1]}:{str(why[0])[:40]}")
    print(f"{'ATS':16s} {'n':>3s} {'COMP':>4s} {'NEAR':>4s} {'FAIL':>4s} {'HITL':>4s} {'DEAD':>4s}  conf")
    tot = collections.Counter()
    for k in sorted(rows, key=lambda x: -sum(rows[x].values())):
        c = rows[k]
        tot.update(c)
        denom = c["COMPLETE"] + c["NEAR"] + c["FAIL"]
        conf = c["COMPLETE"] / denom if denom else float("nan")
        print(f"{k:16s} {sum(c.values()):3d} {c['COMPLETE']:4d} {c['NEAR']:4d} {c['FAIL']:4d} {c['HITL']:4d} {c['DEAD']:4d}  {conf:.0%}" if denom else f"{k:16s} {sum(c.values()):3d} {c['COMPLETE']:4d} {c['NEAR']:4d} {c['FAIL']:4d} {c['HITL']:4d} {c['DEAD']:4d}   n/a")
    denom = tot["COMPLETE"] + tot["NEAR"] + tot["FAIL"]
    print(f"{'TOTAL':16s} {sum(tot.values()):3d} {tot['COMPLETE']:4d} {tot['NEAR']:4d} {tot['FAIL']:4d} {tot['HITL']:4d} {tot['DEAD']:4d}  {tot['COMPLETE']/denom:.0%}")
    print("\nNEAR/FAIL causes (first flag per run):")
    for k, v in near_examples.items():
        print(f"  {k}: " + " | ".join(v[:6]))


if __name__ == "__main__":
    main()
