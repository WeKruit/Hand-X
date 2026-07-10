#!/usr/bin/env python3
"""fixture_miner — mine a fixture's HTML from the REAL DOM of a sweep run's live URL (never hand-write).

Given a sweep run json (url) + the failing field's ledger label, revisit the page with the engine's own
browser stack, locate the field's card STRUCTURALLY (label text is only the anchor — test tooling, not
engine logic), snapshot its outerHTML (plus optional out-of-card portals), sanitize (scripts/handlers/
token blobs/comments stripped), wrap it as a self-describing playground fixture and append it to
all_fixtures.json. Interaction behavior observed live is inlined via --script (the fixture must
reproduce the ORIGINAL failure against the engine: trace_one.py <kind> must be RED at mine time).

Usage:
  .venv/bin/python runs/fixtures/fixture_miner.py \
    --run runs/newats/sweep500b/255.json --label "AI Policy for the Application process" \
    --kind airwallex_ai_policy_pill --expected Yes --profile-value Yes \
    --read "..js expr over f.." [--actuate "..js stmts over f.."] [--script behavior.js] \
    [--expand 1] [--container-sel CSS] [--extra-sel CSS,CSS] [--probe-js probe.js] [--dry]
"""
import argparse
import asyncio
import contextlib
import datetime
import html as _h
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

# Bare arrow (browser_use wraps as `(fn)()` — see memory bu_evaluate_bare_arrow); args JSON-baked.
# Anchor = the DEEPEST element whose text contains the label; card = its smallest ancestor that also
# contains an interactive control; --expand climbs N more wrappers when siblings matter (eyeball --dry).
_CAPTURE_JS = r"""() => {
  const LABEL=__LABEL__, SEL=__SEL__, EXPAND=__EXPAND__, EXTRAS=__EXTRAS__;
  const norm = s => (s||'').replace(/[*✱✲]/g,' ').replace(/\s+/g,' ').trim().toLowerCase();
  const L = norm(LABEL);
  const hasCtrl = e => !!(e.querySelector && e.querySelector(
    'input,select,textarea,button,[role=combobox],[role=listbox],[role=radio],[role=checkbox],[contenteditable=true]'));
  let card = SEL ? document.querySelector(SEL) : null;
  if(!card){
    let anchor=null;
    for(const el of document.querySelectorAll('body *')){
      if(!el.textContent || el.children.length>50) continue;
      if(norm(el.textContent).includes(L) && (!anchor || anchor.contains(el))) anchor=el;  // doc order -> deepest
    }
    if(!anchor) return {err:'no-anchor for '+LABEL};
    card=anchor;
    while(card && card!==document.body && !hasCtrl(card)) card=card.parentElement;
    for(let i=0;i<EXPAND && card && card.parentElement && card.parentElement!==document.body;i++) card=card.parentElement;
  }
  if(!card || card===document.body) return {err:'no-card'};
  const extra = EXTRAS.map(s=>{ const n=document.querySelector(s); return n?n.outerHTML:('<!-- extra miss: '+s+' -->'); });
  return {html:card.outerHTML, extra:extra, tag:card.tagName, cls:String(card.className).slice(0,120),
          nctrl:card.querySelectorAll('input,select,textarea,button').length, textlen:(card.textContent||'').length};
}"""


def _sanitize(h: str) -> str:
    h = re.sub(r"(?s)<!--.*?-->", "", h)
    h = re.sub(r"(?is)<(script|noscript|style)\b.*?</\1>", "", h)
    h = re.sub(r"(?is)<iframe\b[^>]*>.*?</iframe>", '<div data-mined="iframe-placeholder"></div>', h)
    h = re.sub(r"(?i)\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*')", "", h)  # inline handlers
    h = re.sub(r"(?i)\s(src|srcset|poster|action|href)\s*=\s*\"(https?:)?//[^\"]*\"", r' \1="#"', h)
    h = re.sub(r"(?is)(<svg\b[^>]*>).*?(</svg>)", r"\1\2", h)  # keep the node, drop vector art
    h = re.sub(r"(?i)value=\"[^\"]{80,}\"", 'value=""', h)  # csrf/token blobs
    return h.strip()


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run"); p.add_argument("--url"); p.add_argument("--label", required=True)
    p.add_argument("--kind", required=True); p.add_argument("--expected", default="")
    p.add_argument("--profile-value", default=None); p.add_argument("--read", default="")
    p.add_argument("--actuate", default=""); p.add_argument("--script", default="")
    p.add_argument("--why", default=""); p.add_argument("--fx-label", default=None)
    p.add_argument("--expand", type=int, default=0); p.add_argument("--container-sel", default="")
    p.add_argument("--extra-sel", default=""); p.add_argument("--probe-js", default="")
    p.add_argument("--settle", type=float, default=6.0); p.add_argument("--dry", action="store_true")
    p.add_argument("--from-cache", action="store_true", help="re-emit from _mined/<kind>.json (no live visit) — for behavior-script iteration")
    p.add_argument("--out", default=str(HERE / "all_fixtures.json"))
    a = p.parse_args()
    url = a.url or json.load(open(a.run))["url"]
    cache = HERE / "_mined"; cache.mkdir(exist_ok=True)

    if a.from_cache:
        res = json.load(open(cache / f"{a.kind}.json"))
        return _emit(a, url, res)

    from browser_use import BrowserProfile, BrowserSession
    from oa_singlepage import _new_user_data_dir
    args = ["--disable-dev-shm-usage", "--disable-gpu"] + (["--no-sandbox"] if os.environ.get("OA_NO_SANDBOX", "1") == "1" else [])
    session = BrowserSession(browser_profile=BrowserProfile(
        headless=True, keep_alive=True, viewport={"width": 1280, "height": 1600},
        enable_default_extensions=False, user_data_dir=_new_user_data_dir(), args=args))
    try:
        await session.start(); await session.navigate_to(url); await asyncio.sleep(a.settle)
        page = await session.must_get_current_page()
        js = (_CAPTURE_JS.replace("__LABEL__", json.dumps(a.label)).replace("__SEL__", json.dumps(a.container_sel or None))
              .replace("__EXPAND__", str(a.expand)).replace("__EXTRAS__", json.dumps([s for s in a.extra_sel.split(",") if s])))
        res = await page.evaluate(js)
        if isinstance(res, str): res = json.loads(res)
        if not res or res.get("err"): print("MINE-FAIL:", (res or {}).get("err")); return 1
        print(f"[mined] <{res['tag']} class={res['cls']!r}> controls={res['nctrl']} textlen={res['textlen']} from {url}")
        if a.probe_js:
            out = await page.evaluate(Path(a.probe_js).read_text())
            print("[probe]", json.dumps(out) if not isinstance(out, str) else out)
    finally:
        with contextlib.suppress(Exception): await session.kill()
    json.dump(res, open(cache / f"{a.kind}.json", "w"), indent=1)
    return _emit(a, url, res)


def _emit(a, url: str, res: dict) -> int:
    card = _sanitize(res["html"]); extras = "\n".join(_sanitize(x) for x in res.get("extra", []))
    if a.dry:
        print(card); print(extras); return 0
    esc = lambda s: _h.escape(s, quote=True)  # noqa: E731
    attrs = (f'class="field" data-kind="{esc(a.kind)}" data-label="{esc(a.label)}" '
             f'data-expected="{esc(a.expected)}" data-read="{esc(a.read)}"')
    if a.actuate: attrs += f' data-actuate="{esc(a.actuate)}"'
    behavior = f"\n<script>\n{Path(a.script).read_text().strip()}\n</script>" if a.script else ""
    entry = {
        "id": a.kind.replace("_", "-"), "kind": a.kind, "label": a.fx_label or a.label,
        "profile_value": a.profile_value if a.profile_value is not None else a.expected,
        "expected": a.expected, "why_hard": a.why,
        "html": f"<div {attrs}>\n{card}\n{extras}{behavior}\n</div>",
        "faithful": True,
        "review": (f"MINED from live DOM {url}"
                   + (f" (run {Path(a.run).stem})" if a.run else "") + f" on {datetime.date.today()}; "
                   "structure verbatim (sanitized: scripts/handlers/token-values/comments stripped); inline "
                   "script replicates the live-observed interaction behavior; must be RED vs current engine at mine time."),
    }
    fx = json.load(open(a.out))
    fx = [f for f in fx if f.get("kind") != a.kind] + [entry]
    json.dump(fx, open(a.out, "w"), indent=1)
    print(f"[emit] {a.kind} -> {a.out} (total {len(fx)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
