"""Schema-driven ATS application filler — ~$0.01 fill, no agent loop.

Thin CLI over the generic engine (ats_engine) + a per-ATS adapter (ats_greenhouse, …).
Selects the adapter by URL host; the engine runs the invariant MAP -> FILL -> VERIFY ->
ESCALATE -> INSTRUMENT pipeline. Fill-only (never submits).

    GOOGLE_API_KEY=... python greenhouse_fill.py \
        --job-url https://job-boards.greenhouse.io/discord/jobs/8289766002 \
        --profile fixtures/sample_profile.json --resume /path/resume.pdf
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

import ats_engine as eng
from ats_ashby import AshbyAdapter
from ats_greenhouse import GreenhouseAdapter
from ats_lever import LeverAdapter
from ats_workday import WorkdayAdapter

HERE = Path(__file__).resolve().parent

# host -> adapter registry.
ADAPTERS: list[type[eng.ATSAdapter]] = [GreenhouseAdapter, LeverAdapter, AshbyAdapter, WorkdayAdapter]


def _pick_adapter(url: str) -> eng.ATSAdapter:
    host = (urlparse(url).hostname or "").lower()
    for cls in ADAPTERS:
        if any(host == h or host.endswith("." + h) or h in host for h in cls.hosts):
            return cls()
    raise SystemExit(f"No ATS adapter matches host {host!r}. Known: {[h for c in ADAPTERS for h in c.hosts]}")


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(HERE / ".env")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="Schema-driven ATS fill — ~$0.01 cost proof")
    p.add_argument("--job-url", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--resume", default=None)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--screenshot", default=None, help="save a PNG of the filled form to this path")
    args = p.parse_args()

    profile = json.loads(Path(args.profile).read_text())
    adapter = _pick_adapter(args.job_url)
    asyncio.run(
        eng.run(
            adapter,
            url=args.job_url,
            profile=profile,
            resume=args.resume,
            headless=args.headless,
            screenshot_path=args.screenshot,
        )
    )


if __name__ == "__main__":
    main()
