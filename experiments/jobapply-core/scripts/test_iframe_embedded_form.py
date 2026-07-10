#!/usr/bin/env python3
"""Fixture `iframe_embedded_form` — cross-origin embedded form fill (coreweave-class), RED -> GREEN.

Mines the coreweave Greenhouse job_app embed shape into a LOCAL two-origin page:
  * PARENT  page (origin A = http://127.0.0.1:PORT)  — a career page with page chrome (cookie-consent
    checkboxes) whose ONLY form is a CROSS-ORIGIN <iframe id=grnhse_iframe> pointing at origin B.
  * FORM    page (origin B = http://localhost:PORT)  — the GH-style application form (first/last name,
    email, phone [text], resume [file], a screening textarea).
Origin A (127.0.0.1) vs origin B (localhost) are CROSS-SITE, so with --site-per-process Chrome puts the
iframe in its own process (OOPIF) — the exact topology of coreweave.com embedding job-boards.greenhouse.io.

RED   (current engine): `discover_fields(parent)` runs _ENUM_JS on the TOP document -> sees only the
        cookie checkboxes, 0 form fields (the form is behind a cross-origin document boundary).
GREEN (the fix): arm cross_origin_iframes -> get_state surfaces the iframe controls (OOPIF target) ->
        `discover_fields_in_frames` finds first/last/email/phone/textarea -> each is LOCATED by id/name
        against the armed selector_map and FILLED via the normal committer, read back INSIDE the iframe.

Run:  OA_NO_SANDBOX=1 PYTHONPATH=<worktree-root> .venv/bin/python scripts/test_iframe_embedded_form.py
Exit 0 = GREEN (RED baseline confirmed + all form fields filled across the OOPIF boundary).
"""
import asyncio
import contextlib
import http.server
import socket
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # experiments/jobapply-core
sys.path.insert(0, str(ROOT))

# GH job_app embed shape (id + name mirror greenhouse's job_application[...] fields; label wired via
# <label for>). The four text fields + textarea are the RED->GREEN gate; a file input proves the file
# lane resolves in the OOPIF too.
FORM_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8><title>Apply</title></head>
<body><form id=application_form>
  <div class=field><label for=first_name>First Name *</label>
    <input id=first_name name="job_application[first_name]" type=text required></div>
  <div class=field><label for=last_name>Last Name *</label>
    <input id=last_name name="job_application[last_name]" type=text required></div>
  <div class=field><label for=email>Email *</label>
    <input id=email name="job_application[email]" type=email required></div>
  <div class=field><label for=phone>Phone</label>
    <input id=phone name="job_application[phone]" type=tel></div>
  <div class=field><label for=resume>Resume/CV *</label>
    <input id=resume name="job_application[resume]" type=file></div>
  <div class=field><label for=q_why>Why do you want to work here? *</label>
    <textarea id=q_why name="job_application[answers_attributes][0][text_value]" required></textarea></div>
</form></body></html>"""

# Parent career page: page chrome (two cookie-consent checkboxes = the "3 cookie boxes" coreweave case)
# + the cross-origin iframe. IFRAME_SRC is templated with origin B at serve time.
PARENT_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8><title>Careers</title></head>
<body>
  <header><nav>CoreCorp Careers</nav></header>
  <div id=cookie-banner>
    <label><input type=checkbox id=cookie_analytics name=cookie_analytics> Allow analytics cookies</label>
    <label><input type=checkbox id=cookie_marketing name=cookie_marketing> Allow marketing cookies</label>
  </div>
  <h1>Senior Engineer</h1>
  <iframe id=grnhse_iframe title="Job Application form" src="__IFRAME_SRC__"
          style="width:760px;height:900px;border:0"></iframe>
</body></html>"""

# The form field names the fix must discover + fill (label -> value). Names mirror discovery's
# name = el.id || el.name -> here the id (first_name, ...). Textarea id is q_why.
WANT = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "+15551234567",
    "q_why": "I admire the mission and the team.",
}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def main() -> int:
    import os

    from browser_use import BrowserProfile, BrowserSession

    import oa_action as act
    import oa_cdp_action as cdpa
    import oa_discover as disc
    import oa_perception as perc
    from oa_singlepage import _has_dominant_cross_origin_iframe, _new_user_data_dir

    port = _free_port()
    # Origin A = http://parent.test:port, Origin B = http://embed.test:port — DIFFERENT registrable
    # domains (cross-SITE), both mapped to the one loopback server via --host-resolver-rules. With
    # --site-per-process Chrome puts the cross-site iframe in its OWN process (a real OOPIF) — the
    # exact coreweave.com-embeds-greenhouse.io topology. (localhost vs 127.0.0.1 is NOT isolated.)
    origin_b = f"http://embed.test:{port}"
    d = HERE / "_iframe_fixture"
    d.mkdir(exist_ok=True)
    (d / "form.html").write_text(FORM_HTML)
    (d / "parent.html").write_text(PARENT_HTML.replace("__IFRAME_SRC__", f"{origin_b}/form.html"))
    h = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(d), **k)  # noqa: E731
    # THREADING server: the parent's keep-alive connection must not block the iframe's request (a
    # single-threaded server serves the OOPIF form ~12s late and the frame never commits in time).
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), h)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    parent_url = f"http://parent.test:{port}/parent.html"

    args = ["--disable-dev-shm-usage", "--disable-gpu", "--site-per-process",
            "--host-resolver-rules=MAP parent.test 127.0.0.1, MAP embed.test 127.0.0.1"]
    if os.environ.get("OA_NO_SANDBOX", "1") == "1":
        args.append("--no-sandbox")
    profile = BrowserProfile(headless=True, keep_alive=True, viewport={"width": 1200, "height": 1000},
                             enable_default_extensions=False, user_data_dir=_new_user_data_dir(), args=args)
    session = BrowserSession(browser_profile=profile)

    failures: list[str] = []
    try:
        await session.start()
        with contextlib.suppress(Exception):
            await session.navigate_to(parent_url)
        page = await session.must_get_current_page()
        # Settle: the cross-site iframe becomes an OOPIF (separate target) only after its renderer
        # spins up + browser-use auto-attaches (~5s). Poll until the iframe target is known.
        main_tid0 = getattr(session, "agent_focus_target_id", None)
        for _ in range(15):
            await asyncio.sleep(1.0)
            _st = await perc.get_state(session)
            if any(getattr(n, "target_id", None) not in (None, main_tid0) for n in _st.selector_map.values()):
                break

        # ---- RED baseline: main-frame discovery is BLIND to the cross-origin iframe form. ----
        red = await disc.discover_fields(page)
        red_names = {f.name for f in red}
        print(f"[RED] main-frame discover_fields -> {len(red)} fields: {sorted(red_names)}")
        if any(n in red_names for n in WANT):
            failures.append(f"RED baseline invalid: main-frame discovery already saw form fields {red_names}")
        # detection must fire on this page (a dominant cross-origin iframe is present)
        if not await _has_dominant_cross_origin_iframe(page):
            failures.append("detection MISS: dominant cross-origin iframe not detected on parent page")

        main_tid = getattr(session, "agent_focus_target_id", None)

        # ---- Part 1 (env-independent): FORCE the flag OFF (simulate upstream default=False), then
        #      prove the OOPIF form is INVISIBLE to get_state, then arm and prove it appears. The
        #      vendored default happens to be True, so we cannot rely on "starts off" — we set it. ----
        session.browser_profile.cross_origin_iframes = False
        _wd = getattr(session, "_dom_watchdog", None)
        if _wd is not None:
            _wd._dom_service = None  # drop any flag-on service so the next serialize rebuilds with OFF
        perc._LAST_GOOD.pop(id(session), None)
        state_off = await perc.get_state(session)
        off_names = {f.name for f in await disc.discover_fields_in_frames(session, state_off)}
        print(f"[OFF] flag=False -> frame discovery {len(off_names)}: {sorted(off_names)}")
        if any(n in off_names for n in WANT):
            failures.append(f"flag OFF still surfaced OOPIF fields {off_names} — the flag/arm has no effect here")

        # arm: flip False -> True + rebuild the service.
        armed = perc.arm_cross_origin_iframes(session)
        print(f"[ARM] arm_cross_origin_iframes -> {armed} (flag now {session.browser_profile.cross_origin_iframes})")
        if not armed or not session.browser_profile.cross_origin_iframes:
            failures.append("arm_cross_origin_iframes did not turn the flag on")

        # ---- Part 2: OOPIF-inclusive get_state + frame discovery. ----
        state = await perc.get_state(session)
        tids = {getattr(n, "target_id", None) for n in state.selector_map.values()}
        oopif = {t for t in tids if t and t != main_tid}
        print(f"[OOPIF] targets in selector_map={len(tids)} main={str(main_tid)[:12]} non-main={len(oopif)}")
        if not oopif:
            failures.append("no OOPIF target in selector_map after arming (iframe not descended as a separate target)")

        green = await disc.discover_fields_in_frames(session, state)
        green_names = {f.name for f in green}
        print(f"[GREEN] discover_fields_in_frames -> {len(green)} fields: {sorted(green_names)}")
        for n in WANT:
            if n not in green_names:
                failures.append(f"frame discovery missed '{n}' (found {sorted(green_names)})")

        # ---- Part 2+3: LOCATE each field against the armed selector_map (dom-ref by id/name) and
        #      FILL it via the normal committer; read the value back INSIDE the iframe (OOPIF). ----
        fresh = await perc.get_state(session)
        for f in green:
            if f.name not in WANT:
                continue
            node, how, _card = await perc.locate_field_tiered(fresh, f.label, dom_ref=f.name)
            if node is None:
                failures.append(f"locate MISS '{f.name}'")
                continue
            tid = getattr(node, "target_id", None)
            in_oopif = bool(tid and tid != main_tid)
            ok = await act.type_text(session, node, WANT[f.name], clear=True)
            # read back through the SAME OOPIF-aware resolve the committer uses
            got = ""
            r = await cdpa._resolve(session, node)
            if r is not None:
                cs, sid, oid = r
                got = str(await cdpa._call_on(cs, sid, oid, "function(){ return this.value || ''; }") or "")
            mark = "OK" if got == WANT[f.name] else "MISMATCH"
            print(f"   fill {f.name:12} how={how:9} oopif={in_oopif} type_ok={ok} readback={got!r} [{mark}]")
            if got != WANT[f.name]:
                failures.append(f"fill/read-back '{f.name}': want {WANT[f.name]!r} got {got!r}")
            if not in_oopif:
                failures.append(f"'{f.name}' bound node is NOT in the OOPIF target (part-3 path not exercised)")
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.kill(), timeout=8.0)
        httpd.shutdown()

    if failures:
        print("\nFIXTURE RESULT: RED (unfixed / broken):")
        for x in failures:
            print(f"  - {x}")
        return 1
    print("\nFIXTURE RESULT: GREEN — cross-origin iframe form discovered + filled across the OOPIF boundary.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
