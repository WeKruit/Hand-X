"""Live-CDP probe #2 — reuse the ENGINE's own open plumbing (oa_cdp_core._trigger/_open) on the
twilio 'source of your right to work' react-select. Tries the focus+ArrowDown open AND a trusted
rect click, and after each dumps whether the .select__menu actually mounted + its options. Pins
WHICH open renders the menu (the escalate root: menu never opened -> no options). No submit.

Run after any sweep finishes: .venv/bin/python scripts/probe_twilio_rs2.py
"""
import asyncio
import contextlib
import json
import sys
import tempfile

URL = sys.argv[1] if len(sys.argv) > 1 else "https://job-boards.greenhouse.io/twilio/jobs/7936698"

DUMP_JS = r"""
function(){
  let ctrl=this; for(let i=0;i<6&&ctrl;i++){ if(/select__control|(^|[^a-z])control([^a-z]|$)/i.test(String(ctrl.className||''))) break; ctrl=ctrl.parentElement; }
  const cont = ctrl ? ctrl.parentElement : null;
  const menu = (cont && cont.querySelector('[class*=menu]')) || null;
  const inp = (this.matches && this.matches('input')) ? this : (this.querySelector && this.querySelector('input'));
  const exp = inp && (inp.getAttribute('aria-expanded'));
  const ac = inp && (inp.getAttribute('aria-controls')||inp.getAttribute('aria-owns'));
  const lb = ac && document.getElementById(ac);
  const scoped = menu ? [...menu.querySelectorAll('[class*=option],[role=option]')] : (lb ? [...lb.querySelectorAll('[class*=option],[role=option]')] : []);
  return JSON.stringify({
    inpId: inp && inp.id, inpName: inp && inp.getAttribute('name'),
    ariaExpanded: exp, ariaControls: ac,
    ctrlCls: ctrl ? String(ctrl.className).slice(0,50) : null,
    hasMenuSibling: !!menu, menuCls: menu ? String(menu.className).slice(0,45) : null,
    listboxById: !!lb,
    scopedOpts: [...new Set(scoped.map(o=>(o.innerText||'').replace(/\s+/g,' ').trim()).filter(Boolean))].slice(0,8),
  });
}
"""


async def main():
    from browser_use import BrowserProfile, BrowserSession
    import oa_cdp_action as cdpa
    import oa_cdp_core as core

    profile = BrowserProfile(
        headless=True, keep_alive=True, viewport={"width": 1280, "height": 1000},
        enable_default_extensions=False, user_data_dir=tempfile.mkdtemp(prefix="twrs2_"), args=["--no-sandbox"],
    )
    session = BrowserSession(browser_profile=profile)
    await session.start()
    with contextlib.suppress(Exception):
        await session.navigate_to(URL)
    await asyncio.sleep(8)
    page = await session.must_get_current_page()

    # find the source-of-right field name via the engine's discovery
    name = None
    with contextlib.suppress(Exception):
        from oa_discover import discover_fields
        for f in await discover_fields(page):
            lab = (getattr(f, "label", "") or "").lower()
            if "source of your right" in lab or "right to work" in lab:
                print(f"discovered: name={getattr(f,'name','')!r} label={getattr(f,'label','')[:50]!r} type={getattr(f,'type','')!r}")
                if getattr(f, "type", "") == "combobox" or name is None:
                    name = getattr(f, "name", "")
    if not name:
        print("source-of-right field NOT discovered")
        with contextlib.suppress(Exception):
            await session.kill()
        return

    r = await core._trigger(session, name)
    if r is None:
        print(f"_trigger failed for name={name!r}")
        await session.kill(); return
    cdp_session, sid, oid = r

    print("\n=== BEFORE open ==="); print(await cdpa._call_on(cdp_session, sid, oid, DUMP_JS))

    # OPEN method A: the engine's _open (focus+ArrowDown for input)
    with contextlib.suppress(Exception):
        await core._open(cdp_session, sid, oid)
    await asyncio.sleep(0.8)
    print("\n=== AFTER _open (focus+ArrowDown) ==="); print(await cdpa._call_on(cdp_session, sid, oid, DUMP_JS))

    # OPEN method B: trusted CDP mouse click at the control's rect
    with contextlib.suppress(Exception):
        rect = await cdpa._call_on(cdp_session, sid, oid, r"""function(){ let c=this; for(let i=0;i<6&&c;i++){ if(/control/i.test(String(c.className||''))) break; c=c.parentElement;} c=c||this; c.scrollIntoView({block:'center'}); const r=c.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; }""")
        if isinstance(rect, dict) and rect.get("x") is not None:
            await cdpa._dispatch_mouse_click(cdp_session, sid, rect["x"], rect["y"])
    await asyncio.sleep(0.8)
    print("\n=== AFTER trusted rect click ==="); print(await cdpa._call_on(cdp_session, sid, oid, DUMP_JS))

    # what does the engine's own read_options return now?
    with contextlib.suppress(Exception):
        opts = await core.read_options(session, name)
        print(f"\n=== core.read_options -> {opts}")

    with contextlib.suppress(Exception):
        await session.kill()


if __name__ == "__main__":
    asyncio.run(main())
