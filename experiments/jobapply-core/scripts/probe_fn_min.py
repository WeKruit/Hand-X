"""Samsara first-name probe (FIXED: page.evaluate returns a JSON STRING -> json.loads it).
Dump every VISIBLE-or-valued text input with its nearest preceding text (samsara labels are sibling
divs, not <label for>), so we can find the First Name field by proximity and see if a hidden
duplicate holds the value while the visible one stays empty."""
import asyncio
import contextlib
import json
import sys
import tempfile

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.samsara.com/company/careers/roles/7618026?gh_jid=7618026"

DUMP_JS = r"""
() => {
  const near = (el) => {
    // nearest text ABOVE/left: walk previous siblings + parent chain for a short text line
    let p = el;
    for (let i=0;i<5&&p;i++){
      let s = p.previousElementSibling;
      for (let j=0;j<4&&s;j++){ const t=(s.innerText||'').replace(/\s+/g,' ').trim(); if(t && t.length<40) return t; s=s.previousElementSibling; }
      p = p.parentElement;
    }
    return '';
  };
  const out = [];
  for (const el of document.querySelectorAll('input')) {
    const ty=(el.type||'').toLowerCase();
    if(['hidden','file','checkbox','radio','submit','button'].includes(ty)) continue;
    const r=el.getBoundingClientRect(); const cs=getComputedStyle(el);
    const vis = r.width>0&&r.height>0&&cs.display!=='none'&&cs.visibility!=='hidden'&&cs.opacity!=='0';
    if(!vis && !el.value) continue;  // only visible OR value-carrying inputs
    out.push({name:el.getAttribute('name')||'', id:el.id||'', vis, val:el.value,
      rect:[Math.round(r.x),Math.round(r.y)], near:near(el).slice(0,30)});
  }
  return JSON.stringify(out);
}
"""

SET_FN_JS = r"""
() => {
  const setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
  const near=(el)=>{let p=el;for(let i=0;i<5&&p;i++){let s=p.previousElementSibling;for(let j=0;j<4&&s;j++){const t=(s.innerText||'').replace(/\s+/g,' ').trim();if(t&&t.length<40)return t;s=s.previousElementSibling;}p=p.parentElement;}return '';};
  const done=[];
  for(const el of document.querySelectorAll('input')){
    const n=near(el).toLowerCase();
    if(n.includes('first name') && !n.includes('preferred')){
      setter.call(el,'PROBE_JORDAN'); el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true}));
      const r=el.getBoundingClientRect(); const cs=getComputedStyle(el);
      done.push({name:el.getAttribute('name')||'', vis:r.width>0&&r.height>0&&cs.display!=='none', near:near(el).slice(0,30)});
    }
  }
  return JSON.stringify(done);
}
"""


async def main():
    from browser_use import BrowserProfile, BrowserSession
    session = BrowserSession(browser_profile=BrowserProfile(
        headless=True, keep_alive=True, viewport={"width": 1280, "height": 900},
        enable_default_extensions=False, user_data_dir=tempfile.mkdtemp(prefix="fnmin_"), args=["--no-sandbox"]))
    await session.start()
    with contextlib.suppress(Exception):
        await session.navigate_to(URL)
    await asyncio.sleep(7)
    page = await session.must_get_current_page()
    dump = json.loads(await page.evaluate(DUMP_JS))
    print("VISIBLE_OR_VALUED_INPUTS=" + str(len(dump)), flush=True)
    for i in dump:
        fn = "first name" in (i["near"] or "").lower()
        print(f"  vis={i['vis']!s:5} near={i['near']!r:32} name={i['name']!r:18} val={i['val']!r} rect={i['rect']}{'  <== FIRST-NAME' if fn else ''}", flush=True)
    print("--- SET on first-name (by near-text), re-read ---", flush=True)
    setres = json.loads(await page.evaluate(SET_FN_JS))
    print("SET_ON=" + repr(setres), flush=True)
    await asyncio.sleep(2.5)
    dump2 = json.loads(await page.evaluate(DUMP_JS))
    for i in dump2:
        if "first name" in (i["near"] or "").lower():
            print(f"  AFTER vis={i['vis']!s:5} near={i['near']!r} name={i['name']!r} val={i['val']!r}", flush=True)
    with contextlib.suppress(Exception):
        await session.kill()

asyncio.run(main())
