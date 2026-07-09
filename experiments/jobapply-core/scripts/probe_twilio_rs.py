"""Live-DOM probe: WHY does the twilio 'source of your right to work' react-select read
'no options: pageOpts 244' (scoped reader can't isolate its menu) while a later rung reads 4
good options? Dumps the control's aria-controls/owns, the option-portal location, and scoped-vs-
global [class*=option] counts, BEFORE and AFTER a keyboard open — so the scoping fix is grounded
in the real DOM, not guessed. No submit.

Run (after any sweep finishes — bundled chromium): .venv/bin/python scripts/probe_twilio_rs.py
"""
import asyncio
import contextlib
import sys
import tempfile

URL = sys.argv[1] if len(sys.argv) > 1 else "https://job-boards.greenhouse.io/twilio/jobs/7936698"
NEEDLE = "source of your right to work"

# find the react-select control whose nearby text mentions the needle; report its structure
FIND_JS = r"""
(needle) => {
  const nrm = s => (s||'').replace(/\s+/g,' ').trim();
  const near = (el) => { let p=el; for(let i=0;i<8&&p;i++){ const t=nrm(p.innerText); if(t&&t.toLowerCase().includes(needle)) return t.slice(0,80); p=p.parentElement;} return ''; };
  // candidate react-select controls
  const ctrls = [...document.querySelectorAll('[class*=control],[role=combobox],input[aria-autocomplete]')];
  for (const c of ctrls) {
    const nt = near(c);
    if (!nt) continue;
    const inp = c.matches('input') ? c : c.querySelector('input');
    return JSON.stringify({
      found: true, nearText: nt,
      ctrlTag: c.tagName, ctrlClass: String(c.className).slice(0,60),
      inpName: inp && inp.getAttribute('name'), inpId: inp && inp.id,
      role: (inp||c).getAttribute('role'),
      ariaControls: (inp||c).getAttribute('aria-controls') || (inp||c).getAttribute('aria-owns'),
      ariaExpanded: (inp||c).getAttribute('aria-expanded'),
      ariaAutocomplete: (inp||c).getAttribute('aria-autocomplete'),
      globalOptionEls: document.querySelectorAll('[class*=option]').length,
    });
  }
  return JSON.stringify({found:false, ctrlCount: ctrls.length});
}
"""

# after opening: where do the options actually live? dump the menu/listbox structure
DUMP_JS = r"""
(inpId) => {
  const out = {};
  const inp = inpId ? document.getElementById(inpId) : null;
  const ac = inp && (inp.getAttribute('aria-controls') || inp.getAttribute('aria-owns'));
  out.ariaControls = ac;
  out.ariaExpanded = inp && inp.getAttribute('aria-expanded');
  // 1) aria-controls target
  if (ac) { const lb = document.getElementById(ac); out.listboxById = lb ? {tag:lb.tagName, role:lb.getAttribute('role'), optChildren: lb.querySelectorAll('[class*=option],[role=option]').length} : null; }
  // 2) react-select menu portals anywhere on the page
  out.menus = [...document.querySelectorAll('[class*=menu],[id*=listbox],[role=listbox]')].slice(0,8).map(m => ({
    tag:m.tagName, id:m.id||'', cls:String(m.className).slice(0,40), role:m.getAttribute('role'),
    opts: [...m.querySelectorAll('[class*=option],[role=option]')].slice(0,6).map(o => nrmTxt(o)),
    optCount: m.querySelectorAll('[class*=option],[role=option]').length,
  }));
  // 3) global option elements + a sample
  const allOpts = [...document.querySelectorAll('[class*=option],[role=option]')];
  out.globalOptCount = allOpts.length;
  out.globalOptSample = allOpts.slice(0,6).map(o => nrmTxt(o));
  function nrmTxt(o){ return (o.innerText||'').replace(/\s+/g,' ').trim().slice(0,50); }
  return JSON.stringify(out);
}
"""


async def main():
    from browser_use import BrowserProfile, BrowserSession
    profile = BrowserProfile(
        headless=True, keep_alive=True, viewport={"width": 1280, "height": 1000},
        enable_default_extensions=False, user_data_dir=tempfile.mkdtemp(prefix="twrs_"),
        args=["--no-sandbox"],
    )
    session = BrowserSession(browser_profile=profile)
    await session.start()
    with contextlib.suppress(Exception):
        await session.navigate_to(URL)
    await asyncio.sleep(8)
    page = await session.must_get_current_page()

    import json
    found = json.loads(await page.evaluate(FIND_JS, NEEDLE))
    print("=== FIND control ===")
    print(json.dumps(found, indent=2))
    if not found.get("found"):
        with contextlib.suppress(Exception):
            await session.kill()
        return
    inp_id = found.get("inpId")

    print("\n=== DUMP before open ===")
    print(await page.evaluate(DUMP_JS, inp_id))

    # keyboard-open: focus the input + ArrowDown (the engine's open path)
    with contextlib.suppress(Exception):
        await page.evaluate("(id)=>{const e=document.getElementById(id); if(e){e.focus();}}", inp_id)
        cdp = session
        # dispatch ArrowDown via evaluate keydown (best-effort) + rely on focus
        await page.evaluate("""(id)=>{const e=document.getElementById(id); if(e){ e.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',code:'ArrowDown',keyCode:40,bubbles:true})); }}""", inp_id)
    await asyncio.sleep(1.0)
    print("\n=== DUMP after keyboard open ===")
    print(await page.evaluate(DUMP_JS, inp_id))

    with contextlib.suppress(Exception):
        await session.kill()


if __name__ == "__main__":
    asyncio.run(main())
