#!/usr/bin/env python3
"""P3-E regression: the visual checkpoint must NOT demote a genuinely painted pill to ESCALATE when the
VLM misreads it as blank (replit 008 false-RED). Reuses the real-mined airwallex_ai_policy_pill DOM
(runs/fixtures/all_fixtures.json); paints the 'Yes' pill; asserts oa_singlepage._choice_painted_active
(the DOM cross-check the fix consults before escalating) returns True for the painted pill and False for
the blank group. That flips the checkpoint's still-blank->ESCALATE (RED) to dom-painted->keep (GREEN).

Run:  OA_NO_SANDBOX=1 .venv/bin/python test_painted_pill_not_blank.py
"""
import asyncio
import contextlib
import http.server
import json
import re
import socket
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SHELL = "<!doctype html><meta charset=utf-8><style>body{font-family:sans-serif}</style>"


async def main() -> int:
    import oa_singlepage as osp

    fx = json.load(open(HERE / "runs/fixtures/all_fixtures.json"))
    pill = next((f for f in fx if f["kind"] == "airwallex_ai_policy_pill"), None)
    if pill is None:
        print("SETUP-FAIL: airwallex_ai_policy_pill fixture missing")
        return 2
    name = re.search(r'name="([^"]+)"', pill["html"])
    if not name:
        print("SETUP-FAIL: pill has no group name")
        return 2
    gname = name.group(1)

    d = HERE / "runs/fixtures/_selfcheck"
    d.mkdir(exist_ok=True)
    (d / "painted_pill_not_blank.html").write_text(SHELL + pill["html"])
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(d), **k)  # noqa: E731
    httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/painted_pill_not_blank.html"

    from browser_use import BrowserProfile, BrowserSession
    from oa_singlepage import _new_user_data_dir
    args = ["--disable-dev-shm-usage", "--disable-gpu", "--no-sandbox"]
    sess = BrowserSession(browser_profile=BrowserProfile(
        headless=True, keep_alive=True, viewport={"width": 900, "height": 800},
        enable_default_extensions=False, user_data_dir=_new_user_data_dir(), args=args))
    ok = True
    try:
        await sess.start()
        # 1) BLANK group (nothing painted) -> cross-check must be False (escalate proceeds, no false suppress)
        await sess.navigate_to(url); await asyncio.sleep(0.5)
        blank = await osp._choice_painted_active(sess, gname, "Yes")
        # 2) paint the 'Yes' pill (the fixture's script paints _active_ on click, next tick)
        page = await sess.must_get_current_page()
        # the real engine commits via a TRUSTED mouse click (cdp_click_xy) -> mousedown fires; this pill
        # paints on mousedown (setTimeout next-tick), NOT on a bare .click(). Mirror that here.
        clicked = await page.evaluate(
            "() => { const b=[...document.querySelectorAll('button')].find(x=>/^\\s*yes\\s*$/i.test(x.textContent||''));"
            " if(!b) return false; b.dispatchEvent(new MouseEvent('mousedown',{bubbles:true})); b.click(); return true; }"
        )
        await asyncio.sleep(0.6)
        painted = await osp._choice_painted_active(sess, gname, "Yes")
        print(f"clicked-Yes={clicked}  blank_group_active={blank}  painted_pill_active={painted}")
        # RED->GREEN: old checkpoint always escalated a blank-vision DONE; the fix keeps it iff painted.
        if blank is not False:
            print("FAIL: blank group read as painted -> would wrongly SUPPRESS a real escalate"); ok = False
        if painted is not True:
            print("FAIL: painted pill read as blank -> checkpoint would false-RED ESCALATE it"); ok = False
    finally:
        with contextlib.suppress(Exception):
            await sess.kill()
    print("PASS: painted pill kept DONE, blank group still escalates" if ok else "TEST FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
