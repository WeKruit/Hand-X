#!/usr/bin/env python3
"""Focused LIVE proof for the coreweave cross-origin embedded form: navigate the REAL coreweave job
page, let the greenhouse OOPIF attach, arm cross_origin_iframes, discover the iframe controls, FILL
first/last/email/phone deterministically (no LLM), scroll the form into view, and take a VIEWPORT
screenshot (composites the OOPIF, unlike full_page stitching). Prints DOM read-back per field.

Run:  OA_NO_SANDBOX=1 PYTHONPATH=<wt> .venv/bin/python scripts/live_coreweave_shot.py <out.png>
"""
import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

URL = "https://coreweave.com/careers/job?4688540006&board=coreweave&gh_jid=4688540006"
WANT = {"first_name": "Ada", "last_name": "Lovelace", "email": "ada.lovelace@example.com", "phone": "+15551234567"}


async def main() -> int:
    import os

    from browser_use import BrowserProfile, BrowserSession

    import oa_action as act
    import oa_cdp_action as cdpa
    import oa_discover as disc
    import oa_perception as perc
    from oa_singlepage import _has_dominant_cross_origin_iframe, _new_user_data_dir

    out = sys.argv[1] if len(sys.argv) > 1 else "coreweave_shot.png"
    args = ["--disable-dev-shm-usage", "--disable-gpu"]
    if os.environ.get("OA_NO_SANDBOX", "1") == "1":
        args.append("--no-sandbox")
    prof = BrowserProfile(headless=True, keep_alive=True, viewport={"width": 1280, "height": 1000},
                          enable_default_extensions=False, user_data_dir=_new_user_data_dir(), args=args)
    session = BrowserSession(browser_profile=prof)
    try:
        await session.start()
        with contextlib.suppress(Exception):
            await session.navigate_to(URL)
        page = await session.must_get_current_page()
        print("dominant cross-origin iframe:", await _has_dominant_cross_origin_iframe(page))
        perc.arm_cross_origin_iframes(session)
        # settle until the OOPIF form controls surface
        fields = []
        for _ in range(15):
            await asyncio.sleep(1.0)
            st = await perc.get_state(session)
            fields = await disc.discover_fields_in_frames(session, st)
            if any(f.name in WANT for f in fields):
                break
        print("frame fields discovered:", [f.name for f in fields][:12], "..." if len(fields) > 12 else "")

        st = await perc.get_state(session)
        for name, val in WANT.items():
            node, how, _ = await perc.locate_field_tiered(st, name, dom_ref=name)
            if node is None:
                # try by label too
                node, how, _ = await perc.locate_field_tiered(st, name.replace("_", " "), dom_ref=name)
            if node is None:
                print(f"  {name:11} LOCATE-MISS")
                continue
            await act.type_text(session, node, val, clear=True)
            got = ""
            r = await cdpa._resolve(session, node)
            if r is not None:
                cs, sid, oid = r
                got = str(await cdpa._call_on(cs, sid, oid, "function(){ return this.value || ''; }") or "")
            print(f"  {name:11} how={how:9} readback={got!r} {'OK' if got == val else 'MISMATCH'}")
            # scroll the iframe form into view (via the located node's OOPIF session)
            if r is not None and name == "first_name":
                with contextlib.suppress(Exception):
                    await cdpa._call_on(cs, sid, oid, "function(){ this.scrollIntoView({block:'center'}); }")

        # RESUME upload proof: locate the OOPIF file input by dom-ref, setFileInputFiles, settle,
        # then read the card text — the GH embed paints the attached-file chip if the upload registered.
        rnode, rhow, _ = await perc.locate_field_tiered(st, "Resume/CV", dom_ref="resume")
        if rnode is not None:
            rp = str(Path(__file__).resolve().parent.parent / "fixtures" / "test_resume.pdf")
            ok = await cdpa.cdp_set_file(session, rnode, rp)
            await asyncio.sleep(4.0)  # GH embed processes the file, then paints the chip
            rr = await cdpa._resolve(session, rnode)
            chip = ""
            if rr is not None:
                cs2, sid2, oid2 = rr
                chip = str(await cdpa._call_on(cs2, sid2, oid2,
                    "function(){ let p=this.parentElement; for(let i=0;i<8&&p;i++){ const t=(p.innerText||'');"
                    " if(t.toLowerCase().includes('test_resume')) return t.slice(0,120); p=p.parentElement; } return ''; }") or "")
            print(f"  resume      how={rhow:9} set_file={ok} chip_text={chip!r}")
        await asyncio.sleep(1.0)
        # VIEWPORT screenshot (not full_page) composites the OOPIF form.
        png = await session.take_screenshot(full_page=False)
        import base64

        data = base64.b64decode(png) if isinstance(png, str) else png
        Path(out).write_bytes(data)
        print("wrote viewport screenshot:", out, len(data), "bytes")
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.kill(), timeout=8.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
