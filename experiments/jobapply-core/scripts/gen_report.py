"""Generate a clearly-named local report for a sweep dir.
Usage: python scripts/gen_report.py <sweep_dir> <report_name>
Writes to runs/reports/<report_name>/:
  - report.md   : per-row table (PASS/FAIL, filled, esc, missing, cost, latency) + per-platform aggregate
  - report.csv  : same, spreadsheet-friendly
  - <row>_<platform>_<VERDICT>.png : each row's screenshot RENAMED so pass/fail is obvious at a glance
"""
import csv
import glob
import json
import os
import shutil
import sys


def plat(u):
    for k in ("ashbyhq", "lever.co", "samsara", "airbnb", "stripe", "doordash", "flexport", "duolingo",
              "smartrecruiters", "bamboohr", "workday", "job-boards.greenhouse", "boards.greenhouse", "greenhouse"):
        if k in u:
            return "greenhouse" if "greenhouse" in k else k
    return (u.split("/")[2] if "//" in u else "other").replace("www.", "")[:18]


def main():
    sweep = sys.argv[1] if len(sys.argv) > 1 else "runs/newats/mega6"
    name = sys.argv[2] if len(sys.argv) > 2 else os.path.basename(sweep.rstrip("/"))
    outdir = os.path.join("runs/reports", name)
    os.makedirs(outdir, exist_ok=True)

    rows = []
    for f in sorted(glob.glob(sweep + "/*.json"), key=lambda p: int(os.path.basename(p)[:-5]) if os.path.basename(p)[:-5].isdigit() else 0):
        rid = os.path.basename(f)[:-5]
        if not rid.isdigit():
            continue
        d = json.load(open(f))
        c = d.get("completeness") or {}
        res = d.get("results") or []
        done = sum(1 for e in res if e.get("outcome") == "DONE")
        esc = sum(1 for e in res if e.get("outcome") == "ESCALATE")
        url = d.get("url") or ""
        p = plat(url)
        verdict = "PASS" if c.get("complete") else (d.get("status") or "FILLED")
        miss = (c.get("missing_required") or [])
        rev = c.get("choice_reverted") or []
        rows.append({
            "row": rid, "platform": p, "verdict": verdict, "filled": done, "esc": esc,
            "total": len(res), "cost": round(d.get("cost") or 0, 4), "latency_s": d.get("secs") or 0,
            "missing": "; ".join(str(x)[:40] for x in miss[:2]), "reverted": "; ".join(str(x)[:30] for x in rev[:2]),
            "url": url,
        })
        # rename screenshot so pass/fail is obvious in Finder
        png = f[:-5] + ".png"
        if os.path.exists(png):
            shutil.copy(png, os.path.join(outdir, f"{int(rid):02d}_{p}_{verdict}.png"))

    # CSV
    with open(os.path.join(outdir, "report.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["row"])
        w.writeheader()
        w.writerows(rows)

    # Markdown — "reached" = actually got a fillable form (DONE>0); a 0-field/instant status is a
    # failed-to-load custom-domain wrapper, NOT reached (counting it inflates reached/fill).
    reached = [r for r in rows if r["filled"] > 0]
    passed = [r for r in rows if r["verdict"] == "PASS"]
    tot_cost = sum(r["cost"] for r in rows)
    avg_lat = sum(r["latency_s"] for r in rows) / max(len(rows), 1)
    slots = sum(r["filled"] + r["esc"] for r in reached)
    tot_esc = sum(r["esc"] for r in reached)
    fill_pct = 100 * sum(r["filled"] for r in reached) // max(slots, 1)
    esc_pct = 100 * tot_esc // max(slots, 1)
    forms_esc = sum(1 for r in reached if r["esc"] > 0)
    false_green = sum(1 for r in rows if r["reverted"])
    lines = [f"# {name} — sweep report", ""]
    lines.append(f"**{len(passed)}/{len(reached)} PASS (finish {100*len(passed)//max(len(reached),1)}%)** | reached {len(reached)}/{len(rows)} | field-fill {fill_pct}% | **escalation {esc_pct}%** ({tot_esc} fields, {forms_esc} forms) | **false-green {false_green}** | cost ${tot_cost:.3f} | avg latency {avg_lat:.0f}s | {len(rows)} rows")
    lines.append("")
    lines.append("| row | platform | verdict | filled | esc | cost | latency | missing/reverted | url |")
    lines.append("|----|----|----|----|----|----|----|----|----|")
    for r in rows:
        flag = "✅" if r["verdict"] == "PASS" else "❌"
        note = r["missing"] or r["reverted"] or ""
        lines.append(f"| {r['row']} | {r['platform']} | {flag} {r['verdict']} | {r['filled']} | {r['esc']} | ${r['cost']:.4f} | {r['latency_s']}s | {note} | {r['url'][:48]} |")
    # per-platform aggregate
    lines += ["", "## Per-platform", "", "| platform | rows | reached | PASS | finish% | avg cost | avg latency |", "|----|----|----|----|----|----|----|"]
    import collections
    agg = collections.defaultdict(lambda: collections.Counter())
    cost = collections.defaultdict(float); lat = collections.defaultdict(float)
    for r in rows:
        a = agg[r["platform"]]; a["rows"] += 1
        a["reached"] += r["verdict"] not in ("BLOCKED", "NONE", "NOT_REACHED")
        a["pass"] += r["verdict"] == "PASS"
        cost[r["platform"]] += r["cost"]; lat[r["platform"]] += r["latency_s"]
    for p in sorted(agg):
        a = agg[p]
        lines.append(f"| {p} | {a['rows']} | {a['reached']} | {a['pass']} | {100*a['pass']//max(a['reached'],1)}% | ${cost[p]/max(a['rows'],1):.4f} | {lat[p]/max(a['rows'],1):.0f}s |")
    open(os.path.join(outdir, "report.md"), "w").write("\n".join(lines))
    print(f"wrote {outdir}/report.md + report.csv + {len(rows)} renamed screenshots")
    print("\n".join(lines[:4]))


if __name__ == "__main__":
    main()
