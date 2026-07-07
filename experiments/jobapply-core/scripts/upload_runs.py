"""Upload job-fill run records to Supabase/Postgres.

Reads every runs/<sweep>/*.json, computes the metrics + committed data, and upserts one row per
(batch, row) into job_fill_runs. Idempotent (unique on run_batch,row_num). Screenshots are stored
by PATH here; run with --upload-shots to also push the PNGs to a Supabase Storage bucket and store
the public URL instead.

Usage:
  DATABASE_URL='postgres://…supabase…' python scripts/upload_runs.py runs/newats/gen gen_sweep_newurls
  (optional) SUPABASE_URL=… SUPABASE_SERVICE_KEY=… python scripts/upload_runs.py … --upload-shots

Secrets: never hard-code. DATABASE_URL / SUPABASE_* come from the env (ATM/Infisical), never argv.
"""
import asyncio
import glob
import json
import os
import re
import sys


def _host(u: str) -> str:
    m = re.search(r"//([^/]+)/", u or "")
    return m.group(1).replace("www.", "") if m else ""


def _plat(u: str) -> str:
    for k in ("ashbyhq", "lever.co", "greenhouse"):
        if k in (u or ""):
            return k.replace(".co", "")
    return "custom"


def _row(path: str, batch: str) -> dict | None:
    n = os.path.basename(path)[:-5]
    if not n.isdigit():
        return None
    d = json.load(open(path))
    c = d.get("completeness") or {}
    res = d.get("results") or []
    dn = sum(1 for e in res if e.get("outcome") == "DONE")
    es = sum(1 for e in res if e.get("outcome") == "ESCALATE")
    url = d.get("url", "")
    verdict = "PASS" if c.get("complete") else ("NO_LOAD" if dn == 0 else (d.get("status") or "FILLED"))
    committed = [
        {"label": (e.get("label") or e.get("name") or "")[:120], "type": e.get("type"),
         "outcome": e.get("outcome"), "committed": str(e.get("committed") or "")[:200]}
        for e in res if e.get("committed")
    ]
    return {
        "run_batch": batch, "row_num": int(n), "url": url, "host": _host(url), "ats_platform": _plat(url),
        "profile_name": "Jordan Avery (rich_profile.json)", "verdict": verdict,
        "fields_filled": dn, "fields_escalated": es, "fields_total": len(res),
        "field_fill_rate": round(100 * dn / max(dn + es, 1), 2), "cost_usd": round(d.get("cost") or 0, 5),
        "latency_s": int(d.get("secs") or 0), "false_green": bool(c.get("choice_reverted")),
        "committed_data": json.dumps(committed), "screenshot_path": path[:-5] + ".png",
    }


async def main():
    sweep = sys.argv[1] if len(sys.argv) > 1 else "runs/newats/gen"
    batch = sys.argv[2] if len(sys.argv) > 2 else os.path.basename(sweep.rstrip("/"))
    dburl = os.environ.get("DATABASE_URL")
    if not dburl:
        print("ERROR: set DATABASE_URL to your Supabase/Postgres connection string (never pass secrets in argv).")
        sys.exit(1)

    import asyncpg  # already a Hand-X dependency

    rows = [r for r in (_row(f, batch) for f in sorted(glob.glob(sweep + "/*.json"))) if r]
    print(f"{len(rows)} run rows from {sweep} -> batch '{batch}'")

    conn = await asyncpg.connect(dburl)
    try:
        await conn.execute(open(os.path.join(os.path.dirname(__file__), "db_schema.sql")).read())
        cols = ["run_batch", "row_num", "url", "host", "ats_platform", "profile_name", "verdict",
                "fields_filled", "fields_escalated", "fields_total", "field_fill_rate", "cost_usd",
                "latency_s", "false_green", "committed_data", "screenshot_path"]
        ph = ", ".join(f"${i+1}" for i in range(len(cols)))
        upd = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("run_batch", "row_num"))
        sql = (f"insert into job_fill_runs ({', '.join(cols)}) values ({ph}) "
               f"on conflict (run_batch, row_num) do update set {upd}")
        for r in rows:
            await conn.execute(sql, *[r[c] for c in cols])
        n = await conn.fetchval("select count(*) from job_fill_runs where run_batch=$1", batch)
        print(f"upserted {len(rows)} rows; batch '{batch}' now has {n} rows in job_fill_runs.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
