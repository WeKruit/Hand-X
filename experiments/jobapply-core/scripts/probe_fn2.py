"""Samsara first-name probe v2: mirror the ENGINE's navigation (navigate -> discover_fields ->
apply-click / iframe-hop -> re-discover) so page.evaluate runs in the SAME frame the engine fills.
Then dump every first-name-ish input (by proximity text) + whether a hidden duplicate holds the
value while the visible one stays empty. page.evaluate returns a JSON STRING -> json.loads it."""
import asyncio
import contextlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # jobapply-core dir

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.samsara.com/company/careers/roles/7618026?gh_jid=7618026"

DUMP_JS = r"""
() => {
  const near = (el) => { let p=el; for(let i=0;i<6&&p;i++){ let s=p.previousElementSibling;
    for(let j=0;j<5&&s;j++){ const t=(s.innerText||'').replace(/\s+/g,' ').trim(); if(t&&t.length<45) return t; s=s.previousElementSibling; } p=p.parentElement; } return ''; };
  const out=[];
  for(const el of document.querySelectorAll('input, textarea')){
    const ty=(el.type||'').toLowerCase(); if(['hidden','file','checkbox','radio','submit','button'].includes(ty)) continue;
    const r=el.getBoundingClientRect(); const cs=getComputedStyle(el);
    const n=near(el).toLowerCase();
    if(!(n.includes('first name')||(el.getAttribute('name')||'').toLowerCase().includes('first'))) continue;
    out.push({name:el.getAttribute('name')||'', id:el.id||'', type:ty, near:near(el).slice(0,35),
      value:el.value, visible:r.width>0&&r.height>0&&cs.display!=='none'&&cs.visibility!=='hidden'&&cs.opacity!=='0',
      rect:[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]});
  }
  return JSON.stringify(out);
}
"""

SET_JS = r"""
() => {
  const near=(el)=>{let p=el;for(let i=0;i<6&&p;i++){let s=p.previousElementSibling;for(let j=0;j<5&&s;j++){const t=(s.innerText||'').replace(/\s+/g,' ').trim();if(t&&t.length<45)return t;s=s.previousElementSibling;}p=p.parentElement;}return '';};
  const setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
  const done=[];
  for(const el of document.querySelectorAll('input')){
    const n=near(el).toLowerCase();
    if(n.includes('first name')&&!n.includes('preferred')){
      // EXACT engine sequence: native setter -> focus -> input -> change -> BLUR
      setter.call(el,'PROBE_JORDAN');
      el.dispatchEvent(new FocusEvent('focus',{bubbles:true}));
      el.dispatchEvent(new Event('input',{bubbles:true,cancelable:true}));
      el.dispatchEvent(new Event('change',{bubbles:true,cancelable:true}));
      el.dispatchEvent(new FocusEvent('blur',{bubbles:true}));
      done.push({name:el.getAttribute('name')||'', id:el.id||'', immediate_value: el.value});
    }
  }
  return JSON.stringify(done);
}
"""

IFRAME_JS = "() => { const fs=[...document.querySelectorAll('iframe')].map(f=>({src:f.src||'',a:f.getBoundingClientRect().width*f.getBoundingClientRect().height})).filter(f=>/^https?:/.test(f.src)&&f.a>60000).sort((x,y)=>y.a-x.a); return fs.length?fs[0].src:''; }"


async def dump(page, tag):
    d = json.loads(await page.evaluate(DUMP_JS))
    print(f"[{tag}] first-name inputs = {len(d)}", flush=True)
    for i in d:
        print(f"    vis={i['visible']!s:5} type={i['type']:8} name={i['name']!r:16} near={i['near']!r:30} val={i['value']!r} rect={i['rect']}", flush=True)
    return d


async def main():
    import ats_engine as eng
    from browser_use import BrowserProfile, BrowserSession
    from oa_discover import discover_fields
    session = BrowserSession(browser_profile=BrowserProfile(
        headless=True, keep_alive=True, viewport={"width": 1280, "height": 900},
        enable_default_extensions=False, user_data_dir=tempfile.mkdtemp(prefix="fn2_"), args=["--no-sandbox"]))
    await session.start()
    with contextlib.suppress(Exception):
        await session.navigate_to(URL)
    await asyncio.sleep(2.5)
    page = await session.must_get_current_page()
    fields = await discover_fields(page)
    print(f"discover#1 = {len(fields)} fields", flush=True)
    if len(fields) < 2:
        with contextlib.suppress(Exception):
            if await eng._try_apply_click(session, page):
                print("apply-click hit", flush=True)
                page = await session.must_get_current_page()
                fields = await discover_fields(page)
    if not fields:
        await asyncio.sleep(8.0)
        with contextlib.suppress(Exception):
            page = await session.must_get_current_page()
            fields = await discover_fields(page)
    if not fields:
        with contextlib.suppress(Exception):
            src = await page.evaluate(IFRAME_JS)
            if src:
                print(f"iframe-hop -> {src[:70]}", flush=True)
                await session.navigate_to(str(src)); await asyncio.sleep(4)
                page = await session.must_get_current_page()
                fields = await discover_fields(page)
    print(f"discover FINAL = {len(fields)} fields", flush=True)
    await asyncio.sleep(2)
    await dump(page, "BEFORE")
    print("--- SET PROBE_JORDAN on first-name (native setter+events), settle 2.5s ---", flush=True)
    print("SET_ON=" + await page.evaluate(SET_JS), flush=True)
    await asyncio.sleep(2.5)
    await dump(page, "AFTER")
    with contextlib.suppress(Exception):
        await session.kill()

asyncio.run(main())
