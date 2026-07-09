"""Isolate the samsara First Name bug: run the ENGINE's observe_act on First Name ALONE, then
check if the visible input rendered. Then fill a few LATER fields and re-check First Name — if it
clears only after later fills, the bug is fill-order/re-render, not the per-field fill."""
import asyncio
import contextlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
URL = "https://www.samsara.com/company/careers/roles/7618026?gh_jid=7618026"

READ_FN_JS = r"""
() => {
  const near=(el)=>{let p=el;for(let i=0;i<6&&p;i++){let s=p.previousElementSibling;for(let j=0;j<5&&s;j++){const t=(s.innerText||'').replace(/\s+/g,' ').trim();if(t&&t.length<45)return t;s=s.previousElementSibling;}p=p.parentElement;}return '';};
  const out=[];
  for(const el of document.querySelectorAll('input')){
    const n=near(el).toLowerCase();
    if(n.includes('first name')&&!n.includes('preferred')){ const r=el.getBoundingClientRect();
      out.push({id:el.id, val:el.value, vis:r.width>0&&r.height>0}); }
  }
  return JSON.stringify(out);
}
"""


async def read_fn(page, tag):
    r = json.loads(await page.evaluate(READ_FN_JS))
    print(f"[{tag}] First Name inputs: {r}", flush=True)
    return r


async def main():
    import ats_engine as eng
    import oa_observe_act as oa
    from browser_use import BrowserProfile, BrowserSession
    from oa_discover import discover_fields
    from oa_singlepage import _field_dict
    profile = json.load(open("fixtures/rich_profile.json"))
    session = BrowserSession(browser_profile=BrowserProfile(
        headless=True, keep_alive=True, viewport={"width": 1280, "height": 900},
        enable_default_extensions=False, user_data_dir=tempfile.mkdtemp(prefix="fn3_"), args=["--no-sandbox"]))
    await session.start()
    with contextlib.suppress(Exception):
        await session.navigate_to(URL)
    await asyncio.sleep(2.5)
    page = await session.must_get_current_page()
    fields = await discover_fields(page)
    if len(fields) < 2:
        with contextlib.suppress(Exception):
            if await eng._try_apply_click(session, page):
                page = await session.must_get_current_page()
                fields = await discover_fields(page)
    if not fields:
        with contextlib.suppress(Exception):
            src = await page.evaluate("() => { const fs=[...document.querySelectorAll('iframe')].map(f=>({src:f.src||'',a:f.getBoundingClientRect().width*f.getBoundingClientRect().height})).filter(f=>/^https?:/.test(f.src)&&f.a>60000).sort((x,y)=>y.a-x.a); return fs.length?fs[0].src:''; }")
            if src:
                await session.navigate_to(str(src)); await asyncio.sleep(4)
                page = await session.must_get_current_page()
                fields = await discover_fields(page)
    print(f"FIELDS={len(fields)}", flush=True)
    mapped = await eng.map_fields(None, fields, profile, "") if False else {}
    # find First Name + a few later text fields
    fn_field = next((f for f in fields if "first name" in (f.label or "").lower() and "preferred" not in (f.label or "").lower()), None)
    print(f"FN field: name={getattr(fn_field,'name',None)!r} label={getattr(fn_field,'label','')[:30]!r} type={getattr(fn_field,'type',None)!r}", flush=True)
    if not fn_field:
        await session.kill(); return
    await read_fn(page, "PRE")
    # ENGINE fill on First Name alone
    fd = _field_dict(fn_field, "Jordan", resume=None, llm=None, adapter=None, page=page)
    out = await oa.observe_act(session, fd)
    print(f"observe_act -> {out} committed={fd.get('_committed')!r} trace={fd.get('_trace')}", flush=True)
    await asyncio.sleep(1)
    await read_fn(page, "AFTER-FN-ONLY")
    # now fill a few LATER text fields (Last Name, Email) to trigger re-renders
    for lbl, val in [("last name", "Avery"), ("email", "jordan.avery.demo2026@gmail.com")]:
        f2 = next((f for f in fields if lbl in (f.label or "").lower() and "preferred" not in (f.label or "").lower()), None)
        if f2:
            fd2 = _field_dict(f2, val, resume=None, llm=None, adapter=None, page=page)
            await oa.observe_act(session, fd2)
    await asyncio.sleep(1)
    await read_fn(page, "AFTER-LATER-FILLS")
    with contextlib.suppress(Exception):
        await session.kill()

asyncio.run(main())
