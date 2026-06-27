"""Batch fill-only sweep over many ATS job URLs using the deterministic engine.

Reuses ats_engine.run() — the invariant MAP -> FILL -> VERIFY -> (ESCALATE) -> INSTRUMENT
pipeline — across a list of URLs, fill-only, never submits. Defaults to allow_escalation
=False (the handoff's "proof sweep" mode): the ladder caps at L2, so cost is ~1 map call
(~$0.002/job) and FAIL counts the fields the DETERMINISTIC layer can't fill (the true
coverage signal). Pass --escalate to allow the pricey L3 single-field agent.

Robustness: each job is time-boxed (a hung CDP session can't stall the whole sweep) and
stray Chromium is reaped between jobs. Per-job result dicts are written incrementally to
--out so a long run's partial progress always survives.

    GOOGLE_API_KEY=... python sweep.py --urls runs/job_urls.json \
        --profile fixtures/rich_profile.json --resume /path/resume.pdf \
        --out runs/sweep_results.json --screenshots runs/sweep_ss

--urls is either a flat JSON list of URLs or {"greenhouse":[...],"lever":[...],"ashby":[...]}.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import ats_engine as eng
from ats_ashby import AshbyAdapter
from ats_greenhouse import GreenhouseAdapter
from ats_lever import LeverAdapter

HERE = Path(__file__).resolve().parent
ADAPTERS: list[type[eng.ATSAdapter]] = [GreenhouseAdapter, LeverAdapter, AshbyAdapter]  # Workday deferred


def _pick(url: str) -> eng.ATSAdapter | None:
    host = (urlparse(url).hostname or "").lower()
    for cls in ADAPTERS:
        if any(host == h or host.endswith("." + h) or h in host for h in cls.hosts):
            return cls()
    return None


def _reap_chromium() -> None:
    with contextlib.suppress(Exception):
        subprocess.run(["pkill", "-f", "ms-playwright/chromium"], capture_output=True, timeout=10)


async def _one(url: str, profile: dict, resume: str | None, escalate: bool, ss: str | None, timeout: float) -> dict:
    adapter = _pick(url)
    if adapter is None:
        return {"url": url, "status": "NO_ADAPTER"}
    t0 = time.monotonic()
    base = {"url": url, "adapter": adapter.__class__.__name__}
    try:
        res = await asyncio.wait_for(
            eng.run(
                adapter, url=url, profile=profile, resume=resume,
                headless=True, screenshot_path=ss, allow_escalation=escalate,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        res = {**base, "status": "TIMEOUT"}
    except Exception as exc:
        res = {**base, "status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
    res.setdefault("url", url)
    res["secs"] = round(time.monotonic() - t0, 1)
    return res


def _load_urls(path: str) -> list[str]:
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        return [u for v in data.values() for u in (v or [])]
    return list(data)


def _summary(results: list[dict]) -> None:
    by: dict[str, list[dict]] = {}
    for r in results:
        by.setdefault(r.get("adapter", "?"), []).append(r)
    print("\n" + "=" * 92)
    print("  DETERMINISTIC SWEEP SUMMARY (fill-only, escalation off => FAIL = deterministic gap)")
    print("=" * 92)
    hdr = f"  {'ATS':<18}{'jobs':>5}{'FILLED':>7}{'fullcov':>8}{'blocked':>8}{'err/to':>7}{'avgFAIL':>8}{'avg$':>9}{'tot$':>9}"
    print(hdr)
    print("  " + "-" * 88)
    g_full = g_jobs = 0
    g_cost = 0.0
    for ats, rows in sorted(by.items()):
        filled = [r for r in rows if r.get("status") == "FILLED"]
        # full coverage = the form was filled AND no field FAILed (all fields entered incl selects)
        fullcov = [r for r in filled if (r.get("tiers") or {}).get("FAIL", 1) == 0]
        blocked = [r for r in rows if r.get("status") == "BLOCKED"]
        errto = [r for r in rows if r.get("status") in ("ERROR", "TIMEOUT", "NO_ADAPTER")]
        avg_fail = (sum((r.get("tiers") or {}).get("FAIL", 0) for r in filled) / len(filled)) if filled else 0.0
        cost = sum(r.get("cost", 0.0) or 0.0 for r in rows)
        avg_cost = cost / len(rows) if rows else 0.0
        print(f"  {ats.replace('Adapter',''):<18}{len(rows):>5}{len(filled):>7}{len(fullcov):>8}"
              f"{len(blocked):>8}{len(errto):>7}{avg_fail:>8.1f}{avg_cost:>9.4f}{cost:>9.3f}")
        g_full += len(fullcov)
        g_jobs += len(rows)
        g_cost += cost
    print("  " + "-" * 88)
    print(f"  TOTAL: {g_jobs} jobs | full-coverage {g_full} ({(100*g_full/g_jobs if g_jobs else 0):.0f}%) | "
          f"spend ${g_cost:.3f} | avg ${ (g_cost/g_jobs if g_jobs else 0):.4f}/job")
    print("=" * 92)


async def main_async(args: argparse.Namespace) -> None:
    profile = json.loads(Path(args.profile).read_text())
    urls = _load_urls(args.urls)
    if args.limit:
        urls = urls[: args.limit]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ss_dir = args.screenshots
    if ss_dir:
        Path(ss_dir).mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    print(f"[sweep] {len(urls)} urls | escalate={args.escalate} | profile={Path(args.profile).name}", flush=True)
    for i, u in enumerate(urls):
        ss = str(Path(ss_dir) / f"{i:03d}.png") if ss_dir else None
        r = await _one(u, profile, args.resume, args.escalate, ss, args.timeout)
        results.append(r)
        out.write_text(json.dumps(results, indent=1))  # incremental — partial progress survives
        t = r.get("tiers") or {}
        print(
            f"[{i + 1}/{len(urls)}] {r.get('adapter', '?').replace('Adapter', ''):<12} "
            f"{r.get('status', '?'):<8} FAIL={t.get('FAIL', '-')} "
            f"filled={r.get('filled', '-')}/{r.get('fields_total', '-')} "
            f"${r.get('cost', 0) or 0:.4f} {r.get('secs', '?')}s  {u}",
            flush=True,
        )
        _reap_chromium()  # don't let killed-but-lingering Chromium accumulate across jobs
    _summary(results)


def main() -> None:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)
    with contextlib.suppress(Exception):
        from dotenv import load_dotenv

        load_dotenv(HERE / ".env")
    p = argparse.ArgumentParser(description="Fill-only deterministic sweep over many ATS job URLs")
    p.add_argument("--urls", required=True, help="JSON: flat list or {ats: [urls]}")
    p.add_argument("--profile", required=True)
    p.add_argument("--resume", default=None)
    p.add_argument("--out", default="runs/sweep_results.json")
    p.add_argument("--screenshots", default=None, help="dir to save per-job filled-form PNGs")
    p.add_argument("--escalate", action="store_true", help="allow L3 agent (pricey); default off = cheap proof")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--timeout", type=float, default=150.0, help="per-job seconds before TIMEOUT")
    args = p.parse_args()
    asyncio.run(main_async(args))
    sys.stdout.flush()
    os._exit(0)  # reuse jobapply's graceful-exit: don't let keep_alive watchdogs hang the batch


if __name__ == "__main__":
    main()
