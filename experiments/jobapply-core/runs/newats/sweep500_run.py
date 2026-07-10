"""500-URL x 5-profile tracked live sweep. Each row (profile, url, ats, company) -> oa_singlepage
--generic with its assigned profile. Concurrency-capped (unique user-data-dir per run, NO global
pkill -- oa_singlepage self-cleans its own children). Per-run: json + png + log + per-field ledger.
Incremental results so a crash never loses progress. Retry once on CRASH/NO-RESULT (live-page flake)."""
import json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent      # runs/newats
CORE = HERE.parent.parent                    # jobapply-core
CONC = int(sys.argv[1]) if len(sys.argv) > 1 else 5
TSV = sys.argv[2] if len(sys.argv) > 2 else "sweep500.tsv"     # smoke: pass a small tsv
TAG = Path(TSV).stem                                            # out dir + results keyed by tsv stem
OUT = HERE / TAG
OUT.mkdir(exist_ok=True)
import os as _os
TIMEOUT = int(_os.environ.get("OA_RUN_TIMEOUT", "170"))
RESUME = "fixtures/resumes/test_resume.pdf"

rows = []
for ln in (HERE / TSV).read_text().splitlines():
    if not ln.strip():
        continue
    profile, url, ats, company = (ln.split("\t") + ["", "", ""])[:4]
    rows.append({"profile": profile, "url": url, "ats": ats, "company": company})

done_path = HERE / f"{TAG}_results.json"
results = {}
if done_path.exists():
    with __import__("contextlib").suppress(Exception):
        results = {r["i"]: r for r in json.loads(done_path.read_text())}


def run_one(idx):
    r = rows[idx]
    oj, ss, lg = OUT / f"{idx:03d}.json", OUT / f"{idx:03d}.png", OUT / f"{idx:03d}.log"
    env = {**__import__("os").environ, "OA_NO_SANDBOX": "1", "PYTHONUNBUFFERED": "1"}
    cmd = [sys.executable, "oa_singlepage.py", "--url", r["url"], "--generic",
           "--profile", r["profile"], "--resume", RESUME, "--json", str(oj), "--screenshot", str(ss)]
    rec = {"i": idx, **r, "status": "CRASH", "fill_rate": None, "filled": None,
           "fields_total": None, "secs": None, "complete": None,
           "missing_required": [], "visually_unanswered": [], "not_reached": None,
           "blocker": None, "results": []}
    for attempt in range(2):
        t0 = time.monotonic()
        try:
            with lg.open("w") as f:
                subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(CORE),
                               env=env, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            rec["status"] = "TIMEOUT"
        rec["secs"] = round(time.monotonic() - t0, 1)
        if oj.exists():
            try:
                d = json.loads(oj.read_text())
                comp = d.get("completeness") or {}
                rec.update(status=d.get("status", "CRASH"), fill_rate=d.get("fill_rate"),
                           filled=d.get("filled"), fields_total=d.get("fields_total"),
                           complete=comp.get("complete"),
                           missing_required=comp.get("missing_required") or [],
                           visually_unanswered=comp.get("visually_unanswered") or [],
                           not_reached=comp.get("not_reached"), blocker=d.get("blocker"),
                           results=[{"label": x.get("label"), "outcome": x.get("outcome"),
                                     "value": x.get("value"), "trace": x.get("trace", [])[-2:]}
                                    for x in (d.get("results") or [])])
                if rec["status"] not in ("CRASH", "TIMEOUT"):
                    break  # got a real result
            except Exception:
                pass
        # else retry once (live-page flake)
    return rec


todo = [i for i in range(len(rows)) if i not in results]
print(f"sweep500: {len(rows)} rows, {len(todo)} to run, conc={CONC}", flush=True)
n = 0
with ThreadPoolExecutor(max_workers=CONC) as ex:
    futs = {ex.submit(run_one, i): i for i in todo}
    for fut in as_completed(futs):
        rec = fut.result()
        results[rec["i"]] = rec
        n += 1
        if n % 5 == 0 or n == len(todo):
            done_path.write_text(json.dumps([results[k] for k in sorted(results)], indent=1))
        fr = rec["fill_rate"]
        print(f"[{n}/{len(todo)}] {rec['ats']:<11} {rec['company'][:16]:<17} "
              f"{rec['status']:<8} rate={fr if fr is not None else '-':<5} "
              f"{rec['filled']}/{rec['fields_total']} {rec['secs']}s", flush=True)

done_path.write_text(json.dumps([results[k] for k in sorted(results)], indent=1))
print("SWEEP500_DONE", len(results))
