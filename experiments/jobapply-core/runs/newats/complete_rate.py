"""COMPLETE-rate measurement (agent ON): the honest 'did we fully fill it' number.
2 URLs per non-account-gated platform, completeness agent ON (fills repeaters/resume/screening),
per-job 420s watchdog. Reports COMPLETE (audit+visual both pass) not just FILLED."""
import json, subprocess, sys, time
from collections import defaultdict
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent.parent
meta = json.loads((HERE/"runs/newats/newats_meta.json").read_text())
# skip account-gated (avature=Password) + antibot (smartrecruiters); 2/platform
SKIP={"avature","smartrecruiters"}
pick=defaultdict(list)
for i,j in enumerate(meta):
    if j["ats"] not in SKIP and len(pick[j["ats"]])<2: pick[j["ats"]].append((i,j))
jobs=[x for v in pick.values() for x in v]
out=HERE/"runs/newats/crate"; out.mkdir(exist_ok=True)
res=[]
def reap(): subprocess.run(["pkill","-9","-f","ms-playwright/chromium"],capture_output=True)
for i,job in jobs:
    reap(); time.sleep(3)
    oj=out/f"{i:02d}.json"
    cmd=[sys.executable,"oa_singlepage.py","--url",job["apply_url"],"--generic",
         "--profile","fixtures/rich_profile.json","--resume","fixtures/test_resume.pdf",
         "--json",str(oj),"--screenshot",str(out/f"{i:02d}.png")]
    print(f"{job['ats']:<15}{job['company'][:16]:<17}",end="",flush=True); t0=time.monotonic()
    try:
        with (out/f"{i:02d}.log").open("w") as f:
            subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,cwd=str(HERE),timeout=440)
    except subprocess.TimeoutExpired: reap()
    d={}
    if oj.exists():
        try: d=json.loads(oj.read_text())
        except: pass
    c=d.get("completeness") or {}
    rec={"ats":job["ats"],"status":d.get("status","CRASH"),"rate":d.get("fill_rate"),
         "complete":c.get("complete"),"miss":c.get("missing_required"),"vis":c.get("visually_unanswered"),
         "secs":round(time.monotonic()-t0,1)}
    res.append(rec)
    print(f" -> {rec['status']:<11}rate={rec['rate']} COMPLETE={rec['complete']} {rec['secs']}s",flush=True)
    (HERE/"runs/newats/complete_rate_results.json").write_text(json.dumps(res,indent=1))
reap()
print("\n=== COMPLETE-RATE (agent ON, audit+visual) ===")
by=defaultdict(list)
for r in res: by[r["ats"]].append(r)
tc=tn=0
for ats,rs in sorted(by.items()):
    comp=[r for r in rs if r["complete"] is True]
    print(f"  {ats:<15} COMPLETE {len(comp)}/{len(rs)}")
    tc+=len(comp); tn+=len(rs)
print(f"\n  OVERALL COMPLETE {tc}/{tn} = {tc/tn:.0%}" if tn else "none")
print("CRATE_DONE")
