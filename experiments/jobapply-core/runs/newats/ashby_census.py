import asyncio, contextlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from browser_use import BrowserProfile, BrowserSession
URL=sys.argv[1]
CEN=("() => ({bodyLen:(document.body&&document.body.innerText||'').length,"
 "radios:document.querySelectorAll('[role=radio]').length,"
 "optRole:document.querySelectorAll('[role=option]').length,"
 "inpRadio:document.querySelectorAll('input[type=radio]').length,"
 "buttons:document.querySelectorAll('button').length,"
 "combobox:document.querySelectorAll('[role=combobox]').length,"
 "hasAuth:/authoriz|sponsor|relatives|18 (years|or older)/i.test(document.body&&document.body.innerText||''),"
 "snippet:(document.body&&document.body.innerText||'').slice(0,300)})")
async def main():
    prof=BrowserProfile(headless=True,keep_alive=True,viewport={"width":1280,"height":1600},
        enable_default_extensions=False,args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu"])
    s=BrowserSession(browser_profile=prof); await s.start()
    try:
        with contextlib.suppress(Exception): await s.navigate_to(URL)
        await asyncio.sleep(6)
        p=await s.must_get_current_page()
        print("CENSUS:", json.dumps(await p.evaluate(CEN)))
    finally:
        with contextlib.suppress(Exception): await s.kill()
asyncio.run(main())
