"""oa_proof — the PROOF harness for the generic ``observe_act`` fill primitive.

WHAT IT DOES
  1. GATHER 50+ live SINGLE-PAGE application URLs across Greenhouse / Lever / Ashby via their
     PUBLIC discovery APIs (no scraping, no auth), keeping only OPEN engineering-ish roles that
     expose a real apply form. Deduped, capped per ATS.
  2. Build a (company URL x profile) MATRIX from oa_profiles.PROFILES.
  3. Run ``oa_singlepage.run_single_page_oa`` over the matrix, FILL-ONLY / NEVER SUBMIT, with a
     CONCURRENCY CAP (<=4 headless browsers via an asyncio.Semaphore) and a PER-JOB TIMEOUT.
  4. Record per run: ATS, fill-rate (% non-skip required+optional fields the state machine drove to
     a DONE/OTHER terminal), outcome histogram, cost, seconds, a screenshot path, and the failure
     taxonomy (ESCALATE traces bucketed by widget-shape / field-kind).
  5. Emit a JSON blob (full per-field detail) + an aggregated markdown report.

HARD CONSTRAINTS (inherited from oa_singlepage, re-asserted here):
  * FILL-ONLY — never clicks Submit/Apply-final. The single-page adapters have no next_step and
    ``observe_act`` never clicks an advance/submit control. This harness only ever calls
    ``run_single_page_oa`` and reads its result dict.
  * No secrets in CLI args — the GOOGLE_API_KEY comes from .env via load_dotenv, never argv.
  * ``.venv/bin/python`` — the vendored browser_use import.
  * Throwaway data only — oa_profiles carries synthetic PII.

The harness SUPPORTS the full 50x10 sweep (``--companies 50 --profiles 10``); a representative
batch is run with smaller caps and ``--max-runs``.

Usage:
    .venv/bin/python oa_proof.py --companies 18 --profiles 3 --max-runs 18 \
        --concurrency 4 --timeout 200 --out runs/oa_proof_batch1
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import ssl
import time
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent

# Local convenience: pick up .env (GOOGLE_API_KEY) like jobapply.py does — NEVER from argv.
with contextlib.suppress(Exception):
    from dotenv import load_dotenv

    load_dotenv(HERE / ".env")

import oa_observe_act as oa  # noqa: E402  (after dotenv, before the heavy browser import)
from oa_profiles import PROFILES  # noqa: E402
from oa_singlepage import run_single_page_oa  # noqa: E402

# ---------------------------------------------------------------------------------------------
# Company seed lists. These are well-known orgs with PUBLIC, OPEN job boards on each ATS. The
# harness fetches each org's live board and picks OPEN engineering-ish single-page postings; a
# dead/empty board is simply skipped. (No auth, no rate-limited account — public read-only APIs.)
# ---------------------------------------------------------------------------------------------
GREENHOUSE_ORGS = [
    "anthropic", "databricks", "stripe", "airbnb", "dropbox", "discord", "robinhood",
    "instacart", "cruise", "samsara", "gitlab", "benchling", "ramp", "scaleai", "figma",
]
LEVER_ORGS = [
    "palantir", "netflix", "spotify", "plaid", "brex", "match", "leagueapps", "voleon",
    "kraken", "nielsen", "swing-education", "verkada", "attentive", "ro",
]
ASHBY_ORGS = [
    "ramp", "openai", "linear", "notion", "vanta", "runway", "deel", "mercury",
    "cursor", "clay", "rippling", "loops", "watershed", "replit",
]

# Keep the proof on real application forms (engineering-ish roles tend to have the richest forms:
# resume + EEO + free-text + repeaters). A loose keyword gate, not a hard requirement.
ENG_HINTS = (
    "engineer", "software", "developer", "swe", "backend", "frontend", "full stack",
    "full-stack", "infrastructure", "platform", "data", "machine learning", "ml ",
    "security", "sre", "reliability", "devops", "ios", "android", "mobile",
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


def _http_json(url: str, *, method: str = "GET", body: bytes | None = None, headers: dict | None = None, timeout: float = 20.0):
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
        jb = ((data.get("data") or {}).get("jobBoard") or {})
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
    status: str = ""           # FILLED | BLOCKED | NO_ADAPTER | TIMEOUT | ERROR
    fields_total: int = 0
    fill_rate: float = 0.0
    filled: int = 0
    fillable: int = 0
    outcomes: dict | None = None
    cost: float = 0.0
    secs: float = 0.0
    screenshot: str | None = None
    error: str = ""
    fails: list[dict] = field(default_factory=list)   # [{name,label,type,nature,trace}] for ESCALATE


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


async def run_matrix(
    companies: list[dict],
    profiles: list[dict],
    *,
    concurrency: int,
    timeout: float,
    max_runs: int,
    out_dir: Path,
    resume: str | None,
) -> list[RunRecord]:
    shots = out_dir / "shots"
    shots.mkdir(parents=True, exist_ok=True)

    # Build the matrix: each company paired with profiles round-robin so all profiles get exercised
    # and we don't run the whole 10x for every company when max_runs is small.
    pairs: list[tuple[dict, dict]] = []
    for i, c in enumerate(companies):
        p = profiles[i % len(profiles)]
        pairs.append((c, p))
        # if we have headroom, add a second varied profile per company (spreads profile coverage)
        if len(profiles) > 1:
            pairs.append((c, profiles[(i + 1) % len(profiles)]))
    pairs = pairs[:max_runs]

    sem = asyncio.Semaphore(concurrency)
    records: list[RunRecord] = []

    async def one(idx: int, c: dict, p: dict) -> RunRecord:
        rec = RunRecord(idx=idx, ats=c["ats"], org=c["org"], title=c["title"], url=c["url"], profile=p["nick"])
        ss = str(shots / f"{idx:03d}_{c['ats']}_{c['org']}_{p['nick']}.png")
        async with sem:
            t0 = time.monotonic()
            print(f"[{idx:03d}] START {c['ats']}/{c['org']} x {p['nick']}  {c['url']}")
            try:
                res = await asyncio.wait_for(
                    run_single_page_oa(
                        url=c["url"], profile=p, resume=resume, headless=True, screenshot_path=ss
                    ),
                    timeout=timeout,
                )
            except TimeoutError:
                rec.status = "TIMEOUT"
                rec.secs = round(time.monotonic() - t0, 1)
                rec.error = f"per-job timeout {timeout}s"
                print(f"[{idx:03d}] TIMEOUT after {timeout}s")
                return rec
            except Exception as exc:  # one bad URL must not abort the sweep
                rec.status = "ERROR"
                rec.secs = round(time.monotonic() - t0, 1)
                rec.error = f"{type(exc).__name__}: {exc}"
                print(f"[{idx:03d}] ERROR {rec.error}")
                return rec

        rec.status = res.get("status", "?")
        rec.fields_total = res.get("fields_total", 0)
        rec.fill_rate = res.get("fill_rate", 0.0)
        rec.filled = res.get("filled", 0)
        rec.outcomes = res.get("outcomes")
        rec.cost = res.get("cost", 0.0)
        rec.secs = res.get("secs", round(time.monotonic() - t0, 1))
        rec.screenshot = res.get("screenshot")
        results = res.get("results") or []
        rec.fillable = sum(1 for r in results if r.get("outcome") != oa.SKIP)
        rec.fails = [
            {"name": r.get("name"), "label": r.get("label"), "type": r.get("type"),
             "nature": r.get("nature"), "trace": r.get("trace")}
            for r in results
            if r.get("outcome") == oa.ESCALATE
        ]
        print(
            f"[{idx:03d}] DONE   {rec.status}  fill_rate={rec.fill_rate:.0%}  "
            f"filled={rec.filled}/{rec.fillable}  ${rec.cost:.4f}  {rec.secs}s"
        )
        # dump the full per-field detail next to the screenshot for later inspection
        with contextlib.suppress(Exception):
            (out_dir / "perfield").mkdir(exist_ok=True)
            with open(out_dir / "perfield" / f"{idx:03d}.json", "w", encoding="utf-8") as fh:
                json.dump(res, fh, indent=2, default=str)
        return rec

    tasks = [asyncio.create_task(one(i, c, p)) for i, (c, p) in enumerate(pairs)]
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
        elif r.status in ("BLOCKED", "TIMEOUT", "ERROR", "NO_ADAPTER"):
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
            "runs_blocked": sum(1 for r in records if r.status in ("BLOCKED", "TIMEOUT", "ERROR", "NO_ADAPTER")),
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
    L(f"- **Runs:** {ov['runs_filled']} filled / {ov['runs_total']} attempted "
             f"({ov['runs_blocked']} blocked/timeout/error)")
    L(f"- **Companies:** {ov['companies']}  |  **Profiles:** {ov['profiles']}")
    L(f"- **Overall fill-rate:** **{ov['fill_rate']:.0%}** "
             f"({ov['fields_filled']}/{ov['fields_fillable']} non-skip fields reached a DONE/OTHER terminal)")
    L(f"- **Cost:** ${ov['total_cost']:.4f} total, ${ov['avg_cost_per_run']:.4f}/run avg\n")

    L("## Fill-rate by ATS\n")
    L("| ATS | runs | blocked | fields filled | fillable | fill-rate | cost |")
    L("|---|---|---|---|---|---|---|")
    for ats, v in sorted(agg["by_ats"].items()):
        L(f"| {ats} | {v['runs']} | {v['blocked']} | {v['filled']} | {v['fillable']} "
                 f"| {v['fill_rate']:.0%} | ${v['cost']:.4f} |")
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
        L(f"| {r.idx} | {r.ats} | {r.org} | {r.profile} | {r.status} | {r.fill_rate:.0%} "
                 f"| {r.filled}/{r.fillable} | ${r.cost:.4f} | {r.secs} | {ss} |")
    L("")

    out_md.write_text("\n".join(out), encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser(description="observe_act PROOF harness — FILL-ONLY, NEVER submits")
    p.add_argument("--companies", type=int, default=18, help="how many live postings to gather")
    p.add_argument("--per-org", type=int, default=2, help="max postings per org (board)")
    p.add_argument("--profiles", type=int, default=3, help="how many of the 10 profiles to use")
    p.add_argument("--max-runs", type=int, default=18, help="cap total company x profile runs")
    p.add_argument("--concurrency", type=int, default=4, help="max concurrent headless browsers (<=4)")
    p.add_argument("--timeout", type=float, default=200.0, help="per-job timeout seconds")
    p.add_argument("--resume", default=None, help="optional resume file path for the file field")
    p.add_argument("--out", default="runs/oa_proof", help="output dir (json + shots + per-field)")
    p.add_argument("--md", default=None, help="markdown report path (default OBSERVE_ACT_PROOF.md)")
    p.add_argument("--urls-only", action="store_true", help="just gather + print URLs, do not run")
    args = p.parse_args()

    if not os.environ.get("GOOGLE_API_KEY"):
        raise SystemExit("GOOGLE_API_KEY not set (put it in .env — never in argv)")

    out_dir = (HERE / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Gathering up to {args.companies} live postings across Greenhouse / Lever / Ashby …")
    companies = gather_companies(args.companies, per_org=args.per_org)
    print(f"  gathered {len(companies)} postings: "
          f"{Counter(c['ats'] for c in companies)}")
    (out_dir / "companies.json").write_text(json.dumps(companies, indent=2), encoding="utf-8")

    if args.urls_only:
        for c in companies:
            print(f"  {c['ats']:11} {c['org']:18} {c['title'][:50]:50} {c['url']}")
        return

    profiles = PROFILES[: args.profiles]
    concurrency = min(args.concurrency, 4)  # HARD cap <=4

    records = asyncio.run(
        run_matrix(
            companies, profiles,
            concurrency=concurrency, timeout=args.timeout, max_runs=args.max_runs,
            out_dir=out_dir, resume=args.resume,
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
    print(f"  PROOF COMPLETE — {ov['runs_filled']}/{ov['runs_total']} runs filled, "
          f"overall fill-rate {ov['fill_rate']:.0%}, ${ov['total_cost']:.4f}")
    print("  by ATS: " + "  ".join(f"{k}={v['fill_rate']:.0%}({v['filled']}/{v['fillable']})"
                                     for k, v in sorted(agg['by_ats'].items())))
    print(f"  failure taxonomy: {agg['failure_taxonomy']}")
    print(f"  report: {md}")
    print("=" * 84)


if __name__ == "__main__":
    main()
