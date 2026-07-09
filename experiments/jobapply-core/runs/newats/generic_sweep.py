"""Generic-lane breadth sweep: run each URL through oa_singlepage --generic (no adapter),
one browser at a time, summarize by ATS. This is the launch-readiness breadth proof."""
import asyncio, json, subprocess, sys, time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent.parent  # jobapply-core
meta = json.loads((HERE / "runs/newats/newats_meta.json").read_text())
outdir = HERE / "runs/newats/breadth"
outdir.mkdir(exist_ok=True)
results = []

def reap():
    subprocess.run(["pkill", "-9", "-f", "ms-playwright/chromium"], capture_output=True)

for i, job in enumerate(meta):
    reap(); time.sleep(3)
    oj = outdir / f"{i:02d}.json"
    ss = outdir / f"{i:02d}.png"
    lg = outdir / f"{i:02d}.log"
    cmd = [sys.executable, "oa_singlepage.py", "--url", job["apply_url"], "--generic",
           "--profile", "fixtures/rich_profile.json", "--resume", "fixtures/test_resume.pdf",
           "--json", str(oj), "--screenshot", str(ss)]
    t0 = time.monotonic()
    print(f"[{i+1}/{len(meta)}] {job['ats']:<16} {job['company'][:20]:<21}", flush=True, end="")
    with lg.open("w") as f:
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(HERE))
    secs = round(time.monotonic() - t0, 1)
    d = {}
    if oj.exists():
        try: d = json.loads(oj.read_text())
        except Exception: pass
    rec = {"ats": job["ats"], "company": job["company"], "url": job["apply_url"],
           "status": d.get("status", "CRASH"), "fill_rate": d.get("fill_rate"),
           "filled": d.get("filled"), "fields_total": d.get("fields_total"),
           "page_kind": d.get("page_kind"), "secs": secs}
    results.append(rec)
    print(f" -> {rec['status']:<10} rate={rec['fill_rate']} {rec['filled']}/{rec['fields_total']} {secs}s", flush=True)
    (HERE / "runs/newats/breadth_results.json").write_text(json.dumps(results, indent=1))

reap()
print("\n=== BY ATS ===")
by = defaultdict(list)
for r in results: by[r["ats"]].append(r)
for ats, rs in sorted(by.items()):
    filled = [r for r in rs if r["status"] == "FILLED"]
    rates = [r["fill_rate"] for r in filled if r["fill_rate"] is not None]
    mean = f"{sum(rates)/len(rates):.2f}" if rates else "-"
    kinds = ",".join(sorted({r["page_kind"] or r["status"] for r in rs if r["status"] != "FILLED"}))
    print(f"  {ats:<16} FILLED {len(filled)}/{len(rs)}  mean_rate={mean:<5} {kinds}")
print("BREADTH_DONE")
