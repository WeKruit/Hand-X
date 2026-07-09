"""Definitive generic-lane confidence measurement: all manifest non-adapter ATS URLs through
the generic lane with the completeness audit ON (agent OFF for speed — audit still honest).
Reports per-platform FILLED + COMPLETE rates = the launch confidence number."""
import json, subprocess, sys, time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent.parent
meta = json.loads((HERE/"runs/newats/newats_meta.json").read_text())
out = HERE/"runs/newats/conf"; out.mkdir(exist_ok=True)
res = []
def reap(): subprocess.run(["pkill","-9","-f","ms-playwright/chromium"],capture_output=True)
for i, job in enumerate(meta):
    reap(); time.sleep(3)
    oj = out/f"{i:02d}.json"
    cmd = [sys.executable,"oa_singlepage.py","--url",job["apply_url"],"--generic",
           "--profile","fixtures/rich_profile.json","--resume","fixtures/test_resume.pdf",
           "--json",str(oj),"--screenshot",str(out/f"{i:02d}.png")]
    t0=time.monotonic()
    print(f"[{i+1}/{len(meta)}] {job['ats']:<16}{job['company'][:18]:<19}",end="",flush=True)
    try:
        with (out/f"{i:02d}.log").open("w") as f:
            subprocess.run(cmd,stdout=f,stderr=subprocess.STDOUT,cwd=str(HERE),timeout=180)
    except subprocess.TimeoutExpired:
        reap()  # per-job wall-clock hit — kill browser, record TIMEOUT, keep sweeping
    d={}
    if oj.exists():
        try: d=json.loads(oj.read_text())
        except: pass
    c=d.get("completeness") or {}
    rec={"ats":job["ats"],"status":d.get("status","CRASH"),"fill_rate":d.get("fill_rate"),
         "complete":c.get("complete"),"missing":len(c.get("missing_required",[]) or []),
         "kind":d.get("page_kind"),"secs":round(time.monotonic()-t0,1)}
    res.append(rec)
    print(f" -> {rec['status']:<11}rate={rec['fill_rate']} complete={rec['complete']} miss={rec['missing']} {rec['secs']}s",flush=True)
    (HERE/"runs/newats/confidence_results.json").write_text(json.dumps(res,indent=1))
reap()
print("\n=== CONFIDENCE BY ATS (generic lane, completeness audit ON) ===")
by=defaultdict(list)
for r in res: by[r["ats"]].append(r)
tf=tc=tn=0
for ats,rs in sorted(by.items()):
    reached=[r for r in rs if r["status"] in ("FILLED","NEEDS_HUMAN") and r["fill_rate"] is not None]
    filled=[r for r in rs if r["status"]=="FILLED"]
    good=[r for r in filled if (r["fill_rate"] or 0)>=0.8]  # substantially filled
    kinds=",".join(sorted({r["kind"] or r["status"] for r in rs if r["status"]!="FILLED"}))
    print(f"  {ats:<16} FILLED {len(filled)}/{len(rs)}  >=0.8: {len(good)}/{len(rs)}  {kinds}")
    tf+=len(filled); tn+=len(rs)
print(f"\n  OVERALL FILLED {tf}/{tn} = {tf/tn:.0%}")
print("CONFIDENCE_DONE")
