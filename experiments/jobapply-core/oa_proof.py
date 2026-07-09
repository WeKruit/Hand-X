"""oa_proof — the PROOF harness for the generic ``observe_act`` fill primitive.

WHAT IT DOES
  1. GATHER 50+ live SINGLE-PAGE application URLs across Greenhouse / Lever / Ashby via their
     PUBLIC discovery APIs (no scraping, no auth), keeping only OPEN engineering-ish roles that
     expose a real apply form. Deduped, capped per ATS.
  2. Build a (company URL x profile) MATRIX from oa_profiles.PROFILES.
  3. Run each matrix cell in ITS OWN OS SUBPROCESS — ``oa_singlepage.py --url … --profile … --json …
     --screenshot …`` — bounded by a PROCESS Semaphore (``--concurrency N``) and a per-job timeout.
     ``oa_singlepage`` is already 1-process-1-run with a JSON result, so it is REUSED verbatim; this
     harness adds NO fill logic of its own.
  4. Aggregate the per-job JSON result files into the matrix report: ATS, fill-rate (% non-skip
     fields the state machine drove to a DONE/OTHER terminal), outcome histogram, cost, seconds, a
     screenshot path, and the failure taxonomy (ESCALATE traces bucketed by widget-shape/field-kind).
  5. Emit a JSON blob (aggregate + per-run records) + an aggregated markdown report.

WHY SUBPROCESSES (FIX 1B): ``observe_act``'s verify state (the per-page VLM cache + call counter in
``vision_verify``) is a MODULE GLOBAL. That is CORRECT for production — 1 OS process == 1 application
run — but the old harness ran the whole matrix as N coroutines in ONE process behind an asyncio
Semaphore, so all "concurrent" jobs shared (and raced on) that one global counter. The fix is real
process isolation: each (url, profile) job gets its OWN interpreter, so its verify counter is its own.
Concurrency is now N OS PROCESSES, not N coroutines. ``--serial`` forces concurrency=1 (no overlap).

HARD CONSTRAINTS (inherited from oa_singlepage, re-asserted here):
  * FILL-ONLY — never clicks Submit/Apply-final. The single-page adapters have no next_step and
    ``observe_act`` never clicks an advance/submit control. The spawned ``oa_singlepage`` only ever
    calls ``run_single_page_oa``; this harness only reads the JSON it writes.
  * No secrets in CLI args — the GOOGLE_API_KEY comes from .env (inherited into the child env), never
    argv. The profile (synthetic PII) and resume are passed as FILE PATHS, never inline values.
  * ``.venv/bin/python`` — the spawned interpreter is this file's own ``sys.executable`` (the venv
    python that carries the vendored browser_use); the child re-imports browser_use itself.
  * Throwaway data only — oa_profiles carries synthetic PII.

The harness SUPPORTS the full 50x10 sweep (``--companies 50 --profiles 10``); a representative
batch is run with smaller caps and ``--max-runs``.

Usage:
    .venv/bin/python oa_proof.py --companies 18 --profiles 3 --max-runs 18 \
        --concurrency 4 --timeout 200 --out runs/oa_proof_batch1
    .venv/bin/python oa_proof.py --serial …            # concurrency=1, no overlap
    .venv/bin/python oa_proof.py --self-test           # offline argv-builder check, no browsers
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import ssl
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent

# Local convenience: pick up .env (GOOGLE_API_KEY) like jobapply.py does — NEVER from argv.
# The child oa_singlepage processes inherit this environment, so the key reaches them WITHOUT
# ever appearing on a command line (no secrets in argv).
with contextlib.suppress(Exception):
    from dotenv import load_dotenv

    load_dotenv(HERE / ".env")

# Terminal-outcome constants for the failure taxonomy (oa.SKIP / oa.ESCALATE).
import oa_observe_act as oa  # noqa: E402
from oa_profiles import PROFILES  # noqa: E402

# The single-page runner is invoked AS A SUBPROCESS (see build_job_argv), NOT imported and called.
# That isolation is the whole point of FIX 1B: the per-page verify globals in vision_verify
# (_VCACHE / _VLM_CALLS) live in each CHILD interpreter, one per run, so they cannot be shared or
# raced across the matrix. (The parent does transitively import browser_use via oa_observe_act, but
# it never RUNS observe_act / vision_verify — it only reads each child's JSON result file.)
SINGLEPAGE = HERE / "oa_singlepage.py"

# ---------------------------------------------------------------------------------------------
# Company seed lists. These are well-known orgs with PUBLIC, OPEN job boards on each ATS. The
# harness fetches each org's live board and picks OPEN engineering-ish single-page postings; a
# dead/empty board is simply skipped. (No auth, no rate-limited account — public read-only APIs.)
# ---------------------------------------------------------------------------------------------
GREENHOUSE_ORGS = [
    "anthropic",
    "databricks",
    "stripe",
    "airbnb",
    "dropbox",
    "discord",
    "robinhood",
    "instacart",
    "cruise",
    "samsara",
    "gitlab",
    "benchling",
    "ramp",
    "scaleai",
    "figma",
]
LEVER_ORGS = [
    "palantir",
    "netflix",
    "spotify",
    "plaid",
    "brex",
    "match",
    "leagueapps",
    "voleon",
    "kraken",
    "nielsen",
    "swing-education",
    "verkada",
    "attentive",
    "ro",
]
ASHBY_ORGS = [
    "ramp",
    "openai",
    "linear",
    "notion",
    "vanta",
    "runway",
    "deel",
    "mercury",
    "cursor",
    "clay",
    "rippling",
    "loops",
    "watershed",
    "replit",
]

# Keep the proof on real application forms (engineering-ish roles tend to have the richest forms:
# resume + EEO + free-text + repeaters). A loose keyword gate, not a hard requirement.
ENG_HINTS = (
    "engineer",
    "software",
    "developer",
    "swe",
    "backend",
    "frontend",
    "full stack",
    "full-stack",
    "infrastructure",
    "platform",
    "data",
    "machine learning",
    "ml ",
    "security",
    "sre",
    "reliability",
    "devops",
    "ios",
    "android",
    "mobile",
)

_UA = {"User-Agent": "Mozilla/5.0 (oa-proof; research; fill-only)"}


def _ssl_ctx() -> ssl.SSLContext:
    # The vendored .venv python has no system CA bundle (urlopen -> CERTIFICATE_VERIFY_FAILED),
    # so verify against certifi's bundle (a browser_use dependency). Still FULL verification.
    with contextlib.suppress(Exception):
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


_SSL = _ssl_ctx()


def _http_json(
    url: str, *, method: str = "GET", body: bytes | None = None, headers: dict | None = None, timeout: float = 20.0
):
    req = urllib.request.Request(url, data=body, method=method, headers={**_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:  # (trusted ATS hosts only)
        return json.loads(r.read().decode("utf-8"))


def _is_eng(title: str) -> bool:
    t = (title or "").lower()
    return any(h in t for h in ENG_HINTS)


# ---- Greenhouse: boards-api.greenhouse.io/v1/boards/{org}/jobs -> absolute_url -----------------
def gather_greenhouse(org: str, per_org: int) -> list[dict]:
    out: list[dict] = []
    with contextlib.suppress(Exception):
        data = _http_json(f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs")
        for j in data.get("jobs", []):
            url = j.get("absolute_url") or ""
            host = (urlparse(url).hostname or "").lower()
            # Only the canonical greenhouse-hosted board (skip orgs that redirect to their own site,
            # which our single-page adapter may not reach reliably in a headless proof).
            if "greenhouse.io" not in host:
                continue
            if not _is_eng(j.get("title", "")):
                continue
            out.append({"ats": "greenhouse", "org": org, "title": j.get("title", ""), "url": url})
            if len(out) >= per_org:
                break
    return out


# ---- Lever: api.lever.co/v0/postings/{org}?mode=json -> hostedUrl ------------------------------
def gather_lever(org: str, per_org: int) -> list[dict]:
    out: list[dict] = []
    with contextlib.suppress(Exception):
        data = _http_json(f"https://api.lever.co/v0/postings/{org}?mode=json")
        for j in data:
            url = j.get("hostedUrl") or ""
            if "jobs.lever.co" not in (urlparse(url).hostname or "").lower():
                continue
            if not _is_eng(j.get("text", "")):
                continue
            out.append({"ats": "lever", "org": org, "title": j.get("text", ""), "url": url})
            if len(out) >= per_org:
                break
    return out


# ---- Ashby: GraphQL ApiJobBoardWithTeams -> jobs.ashbyhq.com/{org}/{id} ------------------------
# NOTE: the public ApiJobBoardWithTeams board only returns LISTED (open) postings, so no
# isListed filter is needed (and that field isn't queryable on this type).
_ASHBY_QUERY = (
    "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {"
    " jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {"
    " jobPostings { id title locationName employmentType } } }"
)


def gather_ashby(org: str, per_org: int) -> list[dict]:
    out: list[dict] = []
    with contextlib.suppress(Exception):
        body = json.dumps(
            {
                "operationName": "ApiJobBoardWithTeams",
                "variables": {"organizationHostedJobsPageName": org},
                "query": _ASHBY_QUERY,
            }
        ).encode()
        data = _http_json(
            "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
            method="POST",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        jb = (data.get("data") or {}).get("jobBoard") or {}
        for j in jb.get("jobPostings", []) or []:
            if not _is_eng(j.get("title", "")):
                continue
            jid = j.get("id")
            if not jid:
                continue
            out.append(
                {
                    "ats": "ashby",
                    "org": org,
                    "title": j.get("title", ""),
                    "url": f"https://jobs.ashbyhq.com/{org}/{jid}",
                }
            )
            if len(out) >= per_org:
                break
    return out


def gather_companies(n_companies: int, per_org: int = 2) -> list[dict]:
    """Round-robin across the three ATSes so the matrix spans all of them even at a small cap."""
    pools = {
        "greenhouse": [(o, gather_greenhouse) for o in GREENHOUSE_ORGS],
        "lever": [(o, gather_lever) for o in LEVER_ORGS],
        "ashby": [(o, gather_ashby) for o in ASHBY_ORGS],
    }
    # Fetch boards lazily, round-robin, until we have n_companies postings (balanced per ATS).
    fetched: dict[str, list[dict]] = {k: [] for k in pools}
    idx = {k: 0 for k in pools}
    seen: set[str] = set()
    companies: list[dict] = []
    order = ["greenhouse", "lever", "ashby"]
    while len(companies) < n_companies:
        progressed = False
        for ats in order:
            if len(companies) >= n_companies:
                break
            # refill this ATS pool if exhausted
            while not fetched[ats] and idx[ats] < len(pools[ats]):
                org, fn = pools[ats][idx[ats]]
                idx[ats] += 1
                fetched[ats] = fn(org, per_org)
            if fetched[ats]:
                c = fetched[ats].pop(0)
                if c["url"] in seen:
                    continue
                seen.add(c["url"])
                companies.append(c)
                progressed = True
        if not progressed:
            break  # all org pools exhausted
    return companies


# ---------------------------------------------------------------------------------------------
# Matrix runner
# ---------------------------------------------------------------------------------------------
@dataclass
class RunRecord:
    idx: int
    ats: str
    org: str
    title: str
    url: str
    profile: str
    status: str = ""  # FILLED | BLOCKED | NO_ADAPTER | TIMEOUT | ERROR
    fields_total: int = 0
    fill_rate: float = 0.0
    filled: int = 0
    fillable: int = 0
    outcomes: dict | None = None
    cost: float = 0.0
    secs: float = 0.0
    screenshot: str | None = None
    error: str = ""
    fails: list[dict] = field(default_factory=list)  # [{name,label,type,nature,trace}] for ESCALATE


def _failure_bucket(rec_field: dict) -> str:
    """Bucket one ESCALATE field into a coarse failure class for the taxonomy."""
    typ = (rec_field.get("type") or "").lower()
    nature = (rec_field.get("nature") or "").lower()
    label = (rec_field.get("label") or "").lower()
    trace = " ".join(rec_field.get("trace") or []).lower()
    if "exc:" in trace:
        return "exception-in-fill"
    if "file" in typ or "resume" in label or "cv" in label:
        return "file-upload"
    if any(k in label for k in ("location", "city", "country", "state")) or "geo" in trace:
        return "location-geocomplete"
    if any(k in label for k in ("school", "university", "degree", "discipline", "field of study")):
        return "education-typeahead"
    if any(k in label for k in ("skill", "language", "technolog")) or nature == "multi":
        return "multi-value-chips"
    if nature == "search" or "search" in typ or "combobox" in trace:
        return "searchable-combobox"
    if nature in ("closed_list", "select") or "select" in typ:
        return "closed-list-select"
    if nature == "date" or "date" in typ:
        return "date"
    if nature in ("boolean", "radio", "checkbox") or any(k in typ for k in ("radio", "checkbox", "bool")):
        return "radio-checkbox-boolean"
    if nature == "free_text" or "text" in typ:
        return "free-text"
    if "no_locate" in trace or "s1" in trace:
        return "locate-failed"
    return "other-uncategorized"


@dataclass
class Job:
    """One matrix cell = one (company, profile) pair, run in its own OS subprocess."""

    idx: int
    company: dict
    profile: dict


def build_matrix(companies: list[dict], profiles: list[dict], max_runs: int) -> list[Job]:
    """Each company paired with profiles round-robin so all profiles get exercised and we don't run
    the whole 10x for every company when max_runs is small. Offline-pure (no I/O) for testability."""
    jobs: list[Job] = []
    for ci, c in enumerate(companies):
        jobs.append(Job(idx=len(jobs), company=c, profile=profiles[ci % len(profiles)]))
        if len(profiles) > 1:  # a second varied profile per company spreads profile coverage
            jobs.append(Job(idx=len(jobs), company=c, profile=profiles[(ci + 1) % len(profiles)]))
    jobs = jobs[:max_runs]
    for i, j in enumerate(jobs):  # re-index after the cap so idx is dense 0..n-1
        j.idx = i
    return jobs


def build_job_argv(
    job: Job,
    *,
    profile_path: Path,
    json_path: Path,
    screenshot_path: Path,
    resume: str | None,
    python: str | None = None,
) -> list[str]:
    """Construct the EXACT subprocess argv for one job — a single 1-process-1-run invocation of
    ``oa_singlepage.py``. PURE (no spawning, no I/O): the proof's DRY/offline self-test calls this to
    assert the argv shape WITHOUT launching a browser.

    SECURITY: only file paths and the (public) job URL go on argv — the profile (synthetic PII) and
    resume are passed BY PATH, never inline; GOOGLE_API_KEY is inherited via the environment, never
    here. The interpreter is this process's own ``sys.executable`` (the venv python with vendored
    browser_use), so the child re-imports browser_use itself."""
    argv = [
        python or sys.executable,
        str(SINGLEPAGE),
        "--url",
        job.company["url"],
        "--profile",
        str(profile_path),
        "--json",
        str(json_path),
        "--screenshot",
        str(screenshot_path),
    ]
    if resume:
        argv += ["--resume", str(resume)]
    return argv


def _record_from_result(rec: RunRecord, res: dict, *, fallback_secs: float) -> RunRecord:
    """Fold an oa_singlepage JSON result dict into a RunRecord (shared by live + future replay)."""
    rec.status = res.get("status", "?")
    rec.fields_total = res.get("fields_total", 0)
    rec.fill_rate = res.get("fill_rate", 0.0)
    rec.filled = res.get("filled", 0)
    rec.outcomes = res.get("outcomes")
    rec.cost = res.get("cost", 0.0)
    rec.secs = res.get("secs", fallback_secs)
    rec.screenshot = res.get("screenshot")
    results = res.get("results") or []
    rec.fillable = sum(1 for r in results if r.get("outcome") != oa.SKIP)
    rec.fails = [
        {
            "name": r.get("name"),
            "label": r.get("label"),
            "type": r.get("type"),
            "nature": r.get("nature"),
            "trace": r.get("trace"),
        }
        for r in results
        if r.get("outcome") == oa.ESCALATE
    ]
    return rec


# ---------------------------------------------------------------------------------------------
# TIMEOUT POLICY (DEV harness vs PRODUCTION engine) — read this before touching the kill path.
#
# PRODUCTION (the real fill engine, oa_observe_act): a stuck/slow REQUIRED field ESCALATES, it is
# NEVER process-killed. observe_act bounds every field by FIELD_DEADLINE / STEP_CAP; when a field
# overruns, the per-field guard returns ESCALATE (required) or SKIP (optional) so the field is
# handed to the agent of last resort and the rest of the form keeps filling. That per-field
# ESCALATE is the production default and is owned by the engine, not by any process supervisor.
#
# DEV SWEEP (this harness): each (url x profile) job runs in its OWN OS subprocess under a per-JOB
# wall-clock. The subprocess hard-kill on that per-job timeout is a DEV-ONLY convenience — it stops
# one wedged headless browser (the §7 "hangs in teardown" mode) from stalling a batch of dozens of
# jobs. It is NOT how production handles a slow field. The --on-timeout knob makes this explicit:
#   * kill     (default for the dev sweep): SIGKILL the child on per-job timeout, record TIMEOUT.
#   * escalate (production-shaped):         do NOT kill mid-fill — let the child keep running to
#                                           completion (the engine's own per-field ESCALATE/DEADLINE
#                                           bounds it from inside), then record the job as ESCALATE.
# In neither mode does the harness invent fill logic or override the engine's per-field outcome;
# --on-timeout only governs the harness's process-supervision policy at the per-JOB boundary.
# ---------------------------------------------------------------------------------------------
async def run_matrix(
    jobs: list[Job],
    *,
    concurrency: int,
    timeout: float,
    out_dir: Path,
    resume: str | None,
    on_timeout: str = "kill",
) -> list[RunRecord]:
    """Run every job in its OWN OS subprocess, bounded by a PROCESS Semaphore. Concurrency is N
    separate interpreters (each with its own verify globals) — NOT N coroutines sharing one.

    ``on_timeout`` governs ONLY the harness's per-JOB process-supervision policy (see the policy
    block above): ``kill`` SIGKILLs a wedged child (dev sweep default); ``escalate`` leaves the
    child running and records the job as ESCALATE, mirroring the engine's per-field ESCALATE which
    is the real production behavior for a slow/stuck field. It never alters fill logic."""
    shots = out_dir / "shots"
    profiles_dir = out_dir / "profiles"
    perfield = out_dir / "perfield"
    for d in (shots, profiles_dir, perfield):
        d.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(concurrency)
    records: list[RunRecord] = []

    async def one(job: Job) -> RunRecord:
        c, p, idx = job.company, job.profile, job.idx
        rec = RunRecord(idx=idx, ats=c["ats"], org=c["org"], title=c["title"], url=c["url"], profile=p["nick"])
        tag = f"{idx:03d}_{c['ats']}_{c['org']}_{p['nick']}"
        ss = shots / f"{tag}.png"
        out_json = perfield / f"{idx:03d}.json"
        # Write the synthetic profile to a file so it is passed BY PATH, never inline on argv.
        prof_path = profiles_dir / f"{tag}.json"
        prof_path.write_text(json.dumps(p), encoding="utf-8")
        argv = build_job_argv(job, profile_path=prof_path, json_path=out_json, screenshot_path=ss, resume=resume)

        async with sem:
            t0 = time.monotonic()
            print(f"[{idx:03d}] SPAWN {c['ats']}/{c['org']} x {p['nick']}  {c['url']}")
            proc: asyncio.subprocess.Process | None = None
            escalated = False  # set when --on-timeout escalate trips on the per-job wall-clock
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(HERE),
                )
                try:
                    out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except TimeoutError:
                    if on_timeout == "escalate":
                        # PRODUCTION-SHAPED: do NOT kill mid-fill. The engine's own per-field
                        # ESCALATE/FIELD_DEADLINE bounds a stuck field from inside; let the child
                        # finish and record the job as ESCALATE (best-effort). We still wait so the
                        # subprocess is reaped, but with no kill — the per-job timeout only marks it.
                        with contextlib.suppress(Exception):
                            out_bytes, _ = await proc.communicate()
                        escalated = True
                        rec.status = "ESCALATE"
                        rec.secs = round(time.monotonic() - t0, 1)
                        rec.error = f"per-job timeout {timeout}s (escalated, child not killed)"
                        print(f"[{idx:03d}] ESCALATE after {timeout}s — child left to finish (pid {proc.pid})")
                        # fall through to read whatever JSON the child managed to write
                    else:
                        # DEV SWEEP default: kill the child (and let it die) so one stuck headless
                        # browser can't stall the sweep — the §7 "hangs in teardown" failure mode,
                        # now isolated to one process. NOT how production handles a slow field.
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(proc.wait(), timeout=10)
                        rec.status = "TIMEOUT"
                        rec.secs = round(time.monotonic() - t0, 1)
                        rec.error = f"per-job timeout {timeout}s (child killed)"
                        print(f"[{idx:03d}] TIMEOUT after {timeout}s — killed pid {proc.pid}")
                        return rec
            except Exception as exc:  # spawn failure must not abort the sweep
                rec.status = "ERROR"
                rec.secs = round(time.monotonic() - t0, 1)
                rec.error = f"spawn {type(exc).__name__}: {exc}"
                print(f"[{idx:03d}] ERROR {rec.error}")
                return rec

        rc = proc.returncode
        secs = round(time.monotonic() - t0, 1)
        # The child writes its full result to out_json; that file is the source of truth.
        res: dict | None = None
        with contextlib.suppress(Exception):
            res = json.loads(out_json.read_text(encoding="utf-8"))
        if res is None:
            # An escalated job that never wrote JSON stays ESCALATE (the engine was mid-fill at the
            # per-job wall-clock), NOT ERROR — that is the production-shaped outcome we recorded.
            if not escalated:
                rec.status = "ERROR"
                rec.secs = secs
                tail = (out_bytes.decode("utf-8", "replace")[-400:] if out_bytes else "").strip()
                rec.error = f"child rc={rc}, no JSON result. stdout tail: {tail}"
                print(f"[{idx:03d}] ERROR child produced no JSON (rc={rc})")
            return rec

        _record_from_result(rec, res, fallback_secs=secs)
        if escalated:
            rec.status = "ESCALATE"  # per-job timeout policy overrides the child's own terminal
        print(
            f"[{idx:03d}] DONE   {rec.status}  fill_rate={rec.fill_rate:.0%}  "
            f"filled={rec.filled}/{rec.fillable}  ${rec.cost:.4f}  {rec.secs}s"
        )
        return rec

    tasks = [asyncio.create_task(one(j)) for j in jobs]
    for t in asyncio.as_completed(tasks):
        records.append(await t)
    records.sort(key=lambda r: r.idx)
    return records


# ---------------------------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------------------------
def aggregate(records: list[RunRecord]) -> dict:
    by_ats: dict[str, dict] = defaultdict(lambda: {"runs": 0, "filled": 0, "fillable": 0, "cost": 0.0, "blocked": 0})
    fail_counter: Counter = Counter()
    completed = [r for r in records if r.status == "FILLED"]
    for r in records:
        a = by_ats[r.ats]
        a["runs"] += 1
        if r.status == "FILLED":
            a["filled"] += r.filled
            a["fillable"] += r.fillable
            a["cost"] += r.cost
        elif r.status in ("BLOCKED", "TIMEOUT", "ESCALATE", "ERROR", "NO_ADAPTER"):
            a["blocked"] += 1
        for f in r.fails:
            fail_counter[_failure_bucket(f)] += 1

    overall_filled = sum(r.filled for r in completed)
    overall_fillable = sum(r.fillable for r in completed)
    return {
        "by_ats": {
            k: {
                **v,
                "fill_rate": round(v["filled"] / v["fillable"], 3) if v["fillable"] else 0.0,
            }
            for k, v in by_ats.items()
        },
        "overall": {
            "runs_total": len(records),
            "runs_filled": len(completed),
            "runs_blocked": sum(
                1 for r in records if r.status in ("BLOCKED", "TIMEOUT", "ESCALATE", "ERROR", "NO_ADAPTER")
            ),
            "fields_filled": overall_filled,
            "fields_fillable": overall_fillable,
            "fill_rate": round(overall_filled / overall_fillable, 3) if overall_fillable else 0.0,
            "total_cost": round(sum(r.cost for r in completed), 4),
            "avg_cost_per_run": round(sum(r.cost for r in completed) / len(completed), 4) if completed else 0.0,
            "companies": len({r.url for r in records}),
            "profiles": len({r.profile for r in records}),
        },
        "failure_taxonomy": dict(fail_counter.most_common()),
    }


def write_markdown(records: list[RunRecord], agg: dict, out_md: Path) -> None:
    out: list[str] = []
    L = out.append  # noqa: N806  (terse markdown line emitter)
    L("# observe_act — Generic Fill PROOF\n")
    L("> Fill-only, NEVER submitted. Single-page ATS (Greenhouse / Lever / Ashby) — no auth, no rate limit.")
    L("> Each run = one live company posting x one synthetic throwaway profile, filled field-by-field")
    L("> via the generic `observe_act` state machine (NOT the per-archetype `fill_with_ladder`).\n")

    ov = agg["overall"]
    L("## Headline\n")
    L(
        f"- **Runs:** {ov['runs_filled']} filled / {ov['runs_total']} attempted "
        f"({ov['runs_blocked']} blocked/timeout/error)"
    )
    L(f"- **Companies:** {ov['companies']}  |  **Profiles:** {ov['profiles']}")
    L(
        f"- **Overall fill-rate:** **{ov['fill_rate']:.0%}** "
        f"({ov['fields_filled']}/{ov['fields_fillable']} non-skip fields reached a DONE/OTHER terminal)"
    )
    L(f"- **Cost:** ${ov['total_cost']:.4f} total, ${ov['avg_cost_per_run']:.4f}/run avg\n")

    L("## Fill-rate by ATS\n")
    L("| ATS | runs | blocked | fields filled | fillable | fill-rate | cost |")
    L("|---|---|---|---|---|---|---|")
    for ats, v in sorted(agg["by_ats"].items()):
        L(
            f"| {ats} | {v['runs']} | {v['blocked']} | {v['filled']} | {v['fillable']} "
            f"| {v['fill_rate']:.0%} | ${v['cost']:.4f} |"
        )
    L("")

    L("## Failure taxonomy (ESCALATE fields, bucketed by widget shape / field kind)\n")
    if agg["failure_taxonomy"]:
        L("| failure bucket | count |")
        L("|---|---|")
        for k, n in agg["failure_taxonomy"].items():
            L(f"| {k} | {n} |")
    else:
        L("_No ESCALATE fields recorded in this batch._")
    L("")

    L("## Per-run detail\n")
    L("| # | ATS | org | profile | status | fill-rate | filled/fillable | cost | secs | screenshot |")
    L("|---|---|---|---|---|---|---|---|---|---|")
    for r in records:
        ss = Path(r.screenshot).name if r.screenshot else (r.error or "-")
        L(
            f"| {r.idx} | {r.ats} | {r.org} | {r.profile} | {r.status} | {r.fill_rate:.0%} "
            f"| {r.filled}/{r.fillable} | ${r.cost:.4f} | {r.secs} | {ss} |"
        )
    L("")

    out_md.write_text("\n".join(out), encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# Offline self-test — proves the matrix builder + the argv builder WITHOUT spawning any browser
# (no network, no $). This is the DRY check the BUILD task asks for: assert the subprocess argv
# shape is correct and carries NO secrets, and that concurrency is process-based.
# ---------------------------------------------------------------------------------------------
def _self_test() -> int:
    checks: list[tuple[str, bool, object]] = []

    def chk(name: str, passed: bool, detail: object = "") -> None:
        checks.append((name, passed, detail))

    companies = [
        {
            "ats": "greenhouse",
            "org": "acme",
            "title": "Backend Engineer",
            "url": "https://job-boards.greenhouse.io/acme/jobs/123",
        },
        {"ats": "lever", "org": "beta", "title": "SRE", "url": "https://jobs.lever.co/beta/abc-def"},
    ]
    profiles = [{"nick": "pyr_backend"}, {"nick": "maya_ml"}]

    # --- matrix builder: dense idx, round-robin profiles, max_runs cap ---
    jobs = build_matrix(companies, profiles, max_runs=99)
    chk("matrix: 2 companies x2 profiles -> 4 jobs", len(jobs) == 4, len(jobs))
    chk("matrix: idx dense 0..n-1", [j.idx for j in jobs] == [0, 1, 2, 3], [j.idx for j in jobs])
    capped = build_matrix(companies, profiles, max_runs=3)
    chk("matrix: max_runs caps + re-indexes", [j.idx for j in capped] == [0, 1, 2], [j.idx for j in capped])

    # --- argv builder: exact shape, file-paths-only, no secrets ---
    job = jobs[0]
    argv = build_job_argv(
        job,
        profile_path=Path("/out/profiles/000_greenhouse_acme_pyr_backend.json"),
        json_path=Path("/out/perfield/000.json"),
        screenshot_path=Path("/out/shots/000.png"),
        resume="/fixtures/test_resume.pdf",
        python="/venv/bin/python",
    )
    expected = [
        "/venv/bin/python",
        str(SINGLEPAGE),
        "--url",
        "https://job-boards.greenhouse.io/acme/jobs/123",
        "--profile",
        "/out/profiles/000_greenhouse_acme_pyr_backend.json",
        "--json",
        "/out/perfield/000.json",
        "--screenshot",
        "/out/shots/000.png",
        "--resume",
        "/fixtures/test_resume.pdf",
    ]
    chk("argv: exact shape", argv == expected, argv)
    chk("argv: spawns oa_singlepage.py", argv[1].endswith("oa_singlepage.py"), argv[1])
    chk(
        "argv: profile passed BY PATH (file, not inline dict)",
        "--profile" in argv and argv[argv.index("--profile") + 1].endswith(".json"),
    )
    # No secret/value tokens on argv — only flags, paths, and the public URL.
    joined = " ".join(argv).lower()
    chk("argv: no GOOGLE_API_KEY token", "google_api_key" not in joined and "api_key" not in joined)
    chk("argv: no inline email/PII", "@example.com" not in joined and "pyry" not in joined)

    # --- argv builder: resume omitted -> no --resume flag ---
    argv_nores = build_job_argv(
        job,
        profile_path=Path("/p.json"),
        json_path=Path("/j.json"),
        screenshot_path=Path("/s.png"),
        resume=None,
        python="/venv/bin/python",
    )
    chk("argv: --resume omitted when no resume", "--resume" not in argv_nores, argv_nores)

    # --- default interpreter is THIS venv python (carries vendored browser_use) ---
    argv_def = build_job_argv(
        job,
        profile_path=Path("/p.json"),
        json_path=Path("/j.json"),
        screenshot_path=Path("/s.png"),
        resume=None,
    )
    chk("argv: default python == sys.executable", argv_def[0] == sys.executable, argv_def[0])

    ok = all(passed for _, passed, _ in checks)
    print("\n=== oa_proof offline self-test (matrix + argv builder, no spawn, $0) ===")
    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail}")
    print(f"\n{'>>> ALL PASS' if ok else '>>> SOME FAIL'}  ({len(checks)} checks)")
    print(
        "  Concurrency model: N OS PROCESSES (asyncio.create_subprocess_exec + a process "
        "Semaphore), NOT N coroutines — each child has its own verify globals."
    )
    return 0 if ok else 1


def main() -> None:
    p = argparse.ArgumentParser(description="observe_act PROOF harness — FILL-ONLY, NEVER submits")
    p.add_argument("--companies", type=int, default=18, help="how many live postings to gather")
    p.add_argument("--per-org", type=int, default=2, help="max postings per org (board)")
    p.add_argument("--profiles", type=int, default=3, help="how many of the 10 profiles to use")
    p.add_argument("--max-runs", type=int, default=18, help="cap total company x profile runs")
    p.add_argument("--concurrency", type=int, default=4, help="max concurrent child PROCESSES (<=4)")
    p.add_argument("--serial", action="store_true", help="force concurrency=1 (no process overlap)")
    p.add_argument("--timeout", type=float, default=200.0, help="per-job timeout seconds")
    p.add_argument(
        "--on-timeout",
        choices=("kill", "escalate"),
        default="kill",
        help=(
            "per-JOB timeout policy (harness process-supervision only): 'kill' SIGKILLs the wedged "
            "child (dev-sweep default); 'escalate' leaves it running and records ESCALATE, mirroring "
            "the engine's per-FIELD ESCALATE which is the real production default for a stuck field"
        ),
    )
    p.add_argument("--resume", default=None, help="optional resume file path for the file field")
    p.add_argument("--out", default="runs/oa_proof", help="output dir (json + shots + per-field)")
    p.add_argument("--md", default=None, help="markdown report path (default OBSERVE_ACT_PROOF.md)")
    p.add_argument("--urls-only", action="store_true", help="just gather + print URLs, do not run")
    p.add_argument("--self-test", action="store_true", help="offline argv-builder check, spawns nothing")
    args = p.parse_args()

    if args.self_test:
        raise SystemExit(_self_test())

    if not os.environ.get("GOOGLE_API_KEY"):
        raise SystemExit("GOOGLE_API_KEY not set (put it in .env — never in argv)")

    out_dir = (HERE / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Gathering up to {args.companies} live postings across Greenhouse / Lever / Ashby …")
    companies = gather_companies(args.companies, per_org=args.per_org)
    print(f"  gathered {len(companies)} postings: " f"{Counter(c['ats'] for c in companies)}")
    (out_dir / "companies.json").write_text(json.dumps(companies, indent=2), encoding="utf-8")

    if args.urls_only:
        for c in companies:
            print(f"  {c['ats']:11} {c['org']:18} {c['title'][:50]:50} {c['url']}")
        return

    profiles = PROFILES[: args.profiles]
    concurrency = 1 if args.serial else min(args.concurrency, 4)  # --serial wins; else HARD cap <=4
    jobs = build_matrix(companies, profiles, args.max_runs)
    print(
        f"  matrix: {len(jobs)} jobs, concurrency={concurrency} "
        f"({'serial — no overlap' if concurrency == 1 else f'up to {concurrency} child processes'})"
    )

    records = asyncio.run(
        run_matrix(
            jobs,
            concurrency=concurrency,
            timeout=args.timeout,
            out_dir=out_dir,
            resume=args.resume,
            on_timeout=args.on_timeout,
        )
    )

    agg = aggregate(records)
    (out_dir / "records.json").write_text(
        json.dumps({"aggregate": agg, "records": [asdict(r) for r in records]}, indent=2, default=str),
        encoding="utf-8",
    )
    md = Path(args.md).resolve() if args.md else (HERE / "OBSERVE_ACT_PROOF.md")
    write_markdown(records, agg, md)

    ov = agg["overall"]
    print("\n" + "=" * 84)
    print(
        f"  PROOF COMPLETE — {ov['runs_filled']}/{ov['runs_total']} runs filled, "
        f"overall fill-rate {ov['fill_rate']:.0%}, ${ov['total_cost']:.4f}"
    )
    print(
        "  by ATS: "
        + "  ".join(
            f"{k}={v['fill_rate']:.0%}({v['filled']}/{v['fillable']})" for k, v in sorted(agg["by_ats"].items())
        )
    )
    print(f"  failure taxonomy: {agg['failure_taxonomy']}")
    print(f"  report: {md}")
    print("=" * 84)


if __name__ == "__main__":
    main()
