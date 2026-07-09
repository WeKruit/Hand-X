"""Offline DOM harness — test detectors/extractors against SAVED Workday DOMs in SECONDS, no live runs.

Captured via `GH_DUMP=<dir>` on a live run (dumps each wizard step's outerHTML at mount). This loads a
saved step, STRIPS <script> tags (so Workday's React can't re-hydrate and mangle the static DOM), and
runs the adapter's JS detectors against the real structure. This is how you iterate the deterministic
repeater fill without 14-min blind live runs.

Run:  python tools/offline_dom_harness.py     # runs _EXTRACT_STEP_JS over the bundled dom_intel fixtures
"""
import asyncio
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from playwright.async_api import async_playwright  # noqa: E402

import ats_workday as wd  # noqa: E402

FIX = BASE / "fixtures" / "dom_intel"


def load(fname: str) -> str:
    html = (FIX / fname).read_text()
    return re.sub(r"<script[\s\S]*?</script>", "", html)  # strip scripts: keep the static DOM intact


async def extract(pg, fname: str) -> None:
    await pg.set_content(load(fname), wait_until="domcontentloaded")
    res = json.loads(await pg.evaluate(wd._EXTRACT_STEP_JS))
    flds = res.get("fields", [])
    print(f"\n=== {fname}: {len(flds)} fields classified ===")
    for f in flds:
        print(f"   [{f['type']:<14}] {f['label'][:40]:<40} aid={f['name'][:32]}")


async def main() -> None:
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page()
        for f in sorted(x.name for x in FIX.glob("*.html")):
            await extract(pg, f)
        await b.close()


if __name__ == "__main__":
    asyncio.run(main())
