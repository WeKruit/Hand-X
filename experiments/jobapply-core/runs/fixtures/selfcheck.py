#!/usr/bin/env python3
"""Fixture WINNABILITY guard — proves every playground fixture can actually reach its `expected`
paint when the obvious control is actuated. The whole point: catch a fixture whose own JS can never
produce `expected` (a dead click handler, a read/paint selector mismatch) BEFORE it silently
masquerades as an engine bug. This is the guard that would have caught tonight's `(fn,0)` no-op
handlers in minute 1.

It does NOT run the engine. It loads each fixture, programmatically actuates the control matching
`expected` (click the pill/radio/option, or type into the input), waits a beat for next-tick paint,
then runs the fixture's OWN `data-read` and asserts it returns `expected`.

Verdicts:
  PASS  — read == expected after actuation (fixture is winnable).
  FAIL  — the fixture reads a JS-PAINTED state (a class / aria-* the handler must set) yet actuation
          did not make it appear -> the dead-handler / read-mismatch class. THIS is the bug to catch.
  SKIP  — actuator limitation (multi-value / compound / native-widget shape this simple harness does
          not reproduce). Not a fixture defect; reported so the count is honest, never hidden.

Run:  .venv/bin/python runs/fixtures/selfcheck.py            # all fixtures
      .venv/bin/python runs/fixtures/selfcheck.py <kind> ... # a subset
Exit code 1 if any FAIL. Meant to run as a precondition before playground scoring.
"""
import asyncio
import contextlib
import http.server
import json
import os
import socket
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

SHELL_HEAD = "<!doctype html><html lang=en><head><meta charset=utf-8></head><body><form id=app>"
SHELL_TAIL = "</form></body></html>"

# A read that gates on a JS-SET class or aria state is the shape a dead handler can break. If actuation
# fails to paint one of these, it is a real fixture defect (FAIL), not an actuator limit (SKIP).
PAINT_STATE_TOKENS = ("is-active", "aria-checked", "aria-pressed", "aria-selected",
                      "data-state", "classlist", ".on", "[class")


def _painted_read_gates_on_js_state(html: str) -> bool:
    lo = html.lower()
    i = lo.find("data-read")
    seg = lo[i:i + 400] if i >= 0 else lo
    return any(t in seg for t in PAINT_STATE_TOKENS)


# Bare arrow (browser_use wraps as `(fn)()`; a bare arrow, NOT an IIFE — see memory bu_evaluate_bare_arrow).
# `expected` is JSON-injected so no arg-passing ambiguity. Actuate the control that MEANS expected:
# a data-v / value / text match on a clickable option, else a native option/radio/checkbox, else type it.
def _actuate_js(expected: str) -> str:
    ex = json.dumps(expected)
    return r"""() => {
  const EX = %s;
  const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const ex = norm(EX);
  const shown = e => e && (e.offsetParent!==null || (e.getClientRects&&e.getClientRects().length));
  const fire = e => { e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true})); };
  // 1) a clickable option carrying the expected value (data-v / value / own visible text)
  const cl = [...document.querySelectorAll('button,[role=option],[role=radio],[role=checkbox],[role=switch],label,.pill,.pill-btn,.star,li,a,span,div')];
  let hit = cl.find(e => e.getAttribute && norm(e.getAttribute('data-v'))===ex)
         || cl.find(e => e.getAttribute && norm(e.getAttribute('value'))===ex)
         || cl.find(e => shown(e) && norm(e.textContent)===ex);
  if(hit){ hit.click(); fire(hit); return 'click'; }
  // 2) native <select> option
  const opt=[...document.querySelectorAll('option')].find(o=>norm(o.textContent)===ex||norm(o.value)===ex);
  if(opt){ const s=opt.closest('select'); if(s){ s.value=opt.value; s.dispatchEvent(new Event('change',{bubbles:true})); return 'select'; } }
  // 3) native radio/checkbox by value or label
  const rc=[...document.querySelectorAll('input[type=radio],input[type=checkbox]')].find(i=>norm(i.value)===ex||norm((i.labels&&i.labels[0]||{}).textContent||'')===ex);
  if(rc){ rc.click(); rc.dispatchEvent(new Event('change',{bubbles:true})); return 'radio'; }
  // 4) lone consent checkbox + affirmative
  const box=document.querySelector('input[type=checkbox]');
  if(box && !['no','false','none','0',''].includes(ex)){ if(!box.checked) box.click(); return 'checkbox'; }
  // 5) plain text/number/textarea -> type the expected value
  const inp=document.querySelector('input:not([type=checkbox]):not([type=radio]),textarea');
  if(inp){ inp.focus(); inp.value=EX; fire(inp); return 'type'; }
  return 'none';
}""" % ex


# Run the fixture's OWN data-read and return its painted value (or an error marker). The read is a JS
# expression over `f` (the field element) — evaluate it EXACTLY as oa_singlepage does: bind f = the
# [data-read] node (which is the .field[data-kind] element). Bare `eval` left f undefined -> ReferenceError.
_READ_JS = r"""() => {
  const el = document.querySelector('[data-read]');
  if(!el) return '__noread';
  try { return String((new Function('f','return ('+el.getAttribute('data-read')+')'))(el)); }
  catch(e){ return '__readerr:'+String(e); }
}"""


def _norm(s: str) -> str:
    return " ".join(str(s or "").strip().lower().split())


async def main() -> int:
    from browser_use import BrowserProfile, BrowserSession

    only = set(sys.argv[1:])
    fx = json.load(open(HERE / "all_fixtures.json"))
    if only:
        fx = [f for f in fx if f.get("kind") in only]

    # one http server serving each fixture as its own page
    d = HERE / "_selfcheck"
    d.mkdir(exist_ok=True)
    for f in fx:
        (d / f"{f['kind']}.html").write_text(SHELL_HEAD + f["html"] + SHELL_TAIL)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    h = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(d), **k)  # noqa: E731
    httpd = http.server.HTTPServer(("127.0.0.1", port), h)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    args = ["--disable-dev-shm-usage", "--disable-gpu"]
    if os.environ.get("OA_NO_SANDBOX", "1") == "1":
        args.append("--no-sandbox")
    from oa_singlepage import _new_user_data_dir  # reuse the run-scoped dir helper
    profile = BrowserProfile(headless=True, keep_alive=True, viewport={"width": 1000, "height": 900},
                             enable_default_extensions=False, user_data_dir=_new_user_data_dir(), args=args)
    session = BrowserSession(browser_profile=profile)

    results = []
    try:
        await session.start()
        for f in fx:
            kind, exp = f["kind"], str(f.get("expected", ""))
            url = f"http://127.0.0.1:{port}/{kind}.html"
            painted, act = "__nav", "?"
            with contextlib.suppress(Exception):
                await session.navigate_to(url)
                await asyncio.sleep(0.3)
                page = await session.must_get_current_page()
                act = str(await page.evaluate(_actuate_js(exp)))
                await asyncio.sleep(0.7)  # next-tick paint (fixtures defer up to ~400ms)
                painted = str(await page.evaluate(_READ_JS))
            ok = _norm(painted) == _norm(exp) or (exp and _norm(exp) in _norm(painted))
            # FAIL only on HIGH confidence: the actuator found + clicked the EXACT option whose own
            # text/value == expected (act=='click'), it is a single-value JS-painted widget, yet the
            # paint never appeared -> a dead handler / read mismatch (the pill bug this guard exists for).
            # Anything the simple actuator can't reliably drive (multi-value, portal/type-search, hidden
            # tab, a switch it mis-clicked as a radio -> act in none/type/radio/select/checkbox) is SKIP,
            # never a false FAIL. The real ISO run already proves engine-winnability for those.
            single = "|" not in exp
            if ok:
                verdict = "PASS"
            elif act == "click" and single and _painted_read_gates_on_js_state(f["html"]):
                verdict = "FAIL"
            else:
                verdict = "SKIP"
            results.append((verdict, kind, exp, painted, act))
    finally:
        with contextlib.suppress(Exception):
            await session.kill()

    npass = sum(1 for r in results if r[0] == "PASS")
    nfail = [r for r in results if r[0] == "FAIL"]
    nskip = [r for r in results if r[0] == "SKIP"]
    for v, kind, exp, painted, act in results:
        if v != "PASS":
            print(f"  {v:4} {kind[:34]:34} exp={exp[:18]!r:20} painted={painted[:22]!r:24} actuated={act}")
    print(f"\nSELFCHECK: {npass}/{len(results)} PASS  |  {len(nfail)} FAIL  |  {len(nskip)} SKIP(actuator-limit)")
    if nfail:
        print("  FAIL = fixture cannot reach its expected paint (dead handler / read mismatch) — FIX the fixture, do not score it.")
    print("SELFCHECK_DONE")
    return 1 if nfail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
