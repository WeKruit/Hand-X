"""Direct DOM dump of an Ashby Yes/No question's controls — role=radio vs plain button?
Decides whether a narrow radio-semantic audit fix can close the pill cluster."""
import asyncio, contextlib, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from browser_use import BrowserProfile, BrowserSession  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "https://jobs.ashbyhq.com/airwallex/40399368-5370-415a-8c43-3cf772bc7719/application"
NEEDLE = sys.argv[2] if len(sys.argv) > 2 else "relatives"

DUMP = ("() => {"
  "const rx=new RegExp(" + json.dumps(NEEDLE) + ",'i');"
  "const q=[...document.querySelectorAll('label,legend,div,span,p,h3,fieldset')].find(e=>rx.test(e.textContent||'')&&e.querySelectorAll('*').length<8);"
  "if(!q) return JSON.stringify({found:false});"
  "let card=q; for(let i=0;i<8&&card.parentElement;i++){card=card.parentElement; if(card.querySelector('button,[role=radio],[role=option],input')) break;}"
  "const sig=e=>({tag:e.tagName,role:e.getAttribute('role'),type:e.getAttribute('type'),"
  "text:(e.textContent||'').replace(/\\s+/g,' ').trim().slice(0,22),"
  "aChecked:e.getAttribute('aria-checked'),aPressed:e.getAttribute('aria-pressed'),aSel:e.getAttribute('aria-selected'),"
  "cls:(e.className||'').toString().slice(0,55)});"
  "const ctrls=[...card.querySelectorAll('button,[role],input,label')].filter(e=>{const t=(e.textContent||'').trim();return t.length<40;}).slice(0,16).map(sig);"
  "return JSON.stringify({found:true,cardRole:card.getAttribute('role'),cardCls:(card.className||'').toString().slice(0,50),ctrls});}")

async def main():
    prof = BrowserProfile(headless=True, keep_alive=True, viewport={"width":1280,"height":1400},
                          enable_default_extensions=False, args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu"])
    session = BrowserSession(browser_profile=prof)
    await session.start()
    try:
        with contextlib.suppress(Exception):
            await session.navigate_to(URL)
        await asyncio.sleep(5)
        page = await session.must_get_current_page()
        with contextlib.suppress(Exception):
            await page.evaluate("() => {const rx=new RegExp(" + json.dumps(NEEDLE) + ",'i');const q=[...document.querySelectorAll('*')].find(e=>rx.test(e.textContent||'')&&e.children.length<4);if(q)q.scrollIntoView({block:'center'});}")
        await asyncio.sleep(1)
        res = await page.evaluate(DUMP)
        print("RESULT:", res)
    finally:
        with contextlib.suppress(Exception):
            await session.kill()

asyncio.run(main())
