"""Honest COVERAGE benchmark: coverage = filled / expected_total_fields (planner denominator),
NOT fill_rate. 2 URLs per platform, agent OFF (deterministic repeaters on), per-job 200s cap."""
import json, subprocess, sys, time
from collections import defaultdict
from pathlib import Path
HERE=Path(__file__).resolve().parent.parent.parent
meta=json.loads((HERE/"runs/newats/newats_meta.json").read_text())
SKIP={"avature","smartrecruiters"}  # account-gated / antibot (Tier-2)
pick=defaultdict(list)
for i,j in enumerate(meta):
    if j["ats"] not in SKIP and len(pick[j["ats"]])<2: pick[j["ats"]].append((i,j))
jobs=[x for v in pick.values() for x in v]
out=HERE/"runs/newats/cov"; out.mkdir(exist_ok=True); res=[]
def reap(): subprocess.run(["pkill","-9","-f","ms-playwright/chromium"],capture_output=True)
for i,job in jobs:
    reap(); time.sleep(3); oj=out/f"{i:02d}.json"
    cmd=[sys.executable,"oa_singlepage.py","--url",job["apply_url"],"--generic","--profile","fixtures/rich_profile.json",
         "--resume","fixtures/test_resume.pdf","--json",str(oj),"--screenshot",str(out/f"{i:02d}.png")]
    print(f"{job['ats']:<14}{job['company'][:14]:<15}",end="",flush=True); t0=time.monotonic()
    try:
        with (out/f"{i:02d}.log").open("w") as f: subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,cwd=str(HERE),timeout=220)
    except subprocess.TimeoutExpired: reap()
    d={}
    if oj.exists():
        try: d=json.loads(oj.read_text())
        except: pass
    rec={"ats":job["ats"],"status":d.get("status"),"filled":d.get("filled"),
         "expected":d.get("expected_total_fields"),"coverage":d.get("coverage"),
         "complete":(d.get("completeness") or {}).get("complete"),"secs":round(time.monotonic()-t0,1)}
    res.append(rec); print(f" -> {rec['status']:<11}filled={rec['filled']}/{rec['expected']} cov={rec['coverage']} complete={rec['complete']} {rec['secs']}s",flush=True)
    (HERE/"runs/newats/coverage_results.json").write_text(json.dumps(res,indent=1))
reap()
print("\n=== HONEST COVERAGE (filled/expected) BY PLATFORM ===")
by=defaultdict(list)
for r in res: by[r["ats"]].append(r)
covs=[]
for ats,rs in sorted(by.items()):
    cs=[r["coverage"] for r in rs if r["coverage"] is not None]
    comp=[r for r in rs if r["complete"] is True]
    m=f"{sum(cs)/len(cs):.0%}" if cs else "-"
    print(f"  {ats:<14} coverage={m:<6} complete={len(comp)}/{len(rs)}")
    covs+=cs
if covs: print(f"\n  OVERALL mean coverage = {sum(covs)/len(covs):.0%}  (n={len(covs)})")
print("COVERAGE_DONE")
