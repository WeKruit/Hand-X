"""HITL end-to-end demo (user: '遇到blocker如何continue'): a self-contained proof that the engine
PAUSES on a human-only blocker, a human ACTS, and the engine RESUMES — no real CAPTCHA needed.

Sets up a tiny local page whose form is hidden behind a 'verify' gate. Runs the generic lane
with GH_HITL=1. The engine hits the gate (no fields) -> classifies -> pauses via oa_hitl,
writing runs/hitl/blocker.json. A background 'human' waits, reveals the form (touches the
continue file), and the engine re-discovers + fills. Proves the pause/resume loop live."""
import asyncio
import contextlib
import os
from pathlib import Path

HERE = Path.cwd()

PAGE = """<!doctype html><html><head><title>Demo</title></head><body>
<div id=gate>Verification required — a human must confirm.</div>
<form id=form style='display:none'>
  <label for=fn>First name *</label><input id=fn required>
  <label for=em>Email *</label><input id=em type=email required>
  <button type=button>Apply</button>
</form>
<script>
  // the 'human' clears the gate by setting window.__cleared (our continue-signal analog);
  // we poll a flag the outer test flips via CDP to reveal the form.
  setInterval(()=>{ if(window.__cleared){ document.getElementById('gate').style.display='none';
    document.getElementById('form').style.display='block'; } }, 300);
</script></body></html>"""


async def main():
    import oa_singlepage as sp

    from browser_use import BrowserProfile, BrowserSession

    import http.server, socketserver, threading, functools
    docroot = HERE / "runs/newats"; docroot.mkdir(parents=True, exist_ok=True)
    (docroot / "hitl_page.html").write_text(PAGE)
    Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(docroot))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler); port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/hitl_page.html"

    hitl_dir = HERE / "runs/hitl_demo"
    with contextlib.suppress(Exception):
        import shutil; shutil.rmtree(hitl_dir)
    os.environ.update(GH_HITL="1", GH_HITL_DIR=str(hitl_dir), GH_HITL_TIMEOUT_S="40", OA_NO_SANDBOX="1")

    prof = BrowserProfile(headless=True, keep_alive=True, viewport={"width":1000,"height":700},
        enable_default_extensions=False, user_data_dir=sp._new_user_data_dir(), args=["--no-sandbox"])
    s = BrowserSession(browser_profile=prof); await s.start()
    with contextlib.suppress(Exception): await s.navigate_to(url)
    await asyncio.sleep(1)
    page = await s.must_get_current_page()

    # THE HUMAN: after 6s, "solve the blocker" by revealing the form + signalling continue
    async def human():
        await asyncio.sleep(6)
        print("   [human] solving the blocker in the browser (revealing the form)...")
        with contextlib.suppress(Exception):
            await page.evaluate("() => { window.__cleared = true; }")
        # (no continue file: the engine's still_blocked poll should DETECT the revealed form)
    asyncio.create_task(human())

    # the engine: discover -> (0 fields, gate) -> HITL pause -> human reveals -> resume + discover
    import oa_hitl
    from oa_discover import discover_fields
    fields = await discover_fields(page)
    print(f"   [engine] first look: {len(fields)} fields (gate blocks the form)")
    if not fields:
        async def still_blocked(pg): return not await discover_fields(pg)
        ok = await oa_hitl.wait_for_unblock(s, page, kind="LOGIN_OR_VERIFY",
            reason="form behind a human-only verification gate", still_blocked=still_blocked)
        print(f"   [engine] HITL returned: {ok}")
        if ok:
            page = await s.must_get_current_page()
            await asyncio.sleep(1.0)
            fields = await discover_fields(page)
            print(f"   [engine] AFTER human cleared it: {len(fields)} fields -> {[f.label for f in fields]}")
    await s.kill()
    print("HITL_DEMO_DONE ->", "PASS" if fields else "FAIL")

asyncio.run(main())
