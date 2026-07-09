import asyncio, contextlib, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from browser_use import BrowserProfile, BrowserSession
import oa_cdp_action as cdpa
URL="https://jobs.ashbyhq.com/clipboard/11673cb5-ab23-467a-95f5-085c3af98411/application"
LABELS=["Are you authorized to work in your country of residence?"]
async def main():
    prof=BrowserProfile(headless=True,keep_alive=True,viewport={"width":1280,"height":1600},
        enable_default_extensions=False,args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu"])
    s=BrowserSession(browser_profile=prof); await s.start()
    try:
        with contextlib.suppress(Exception): await s.navigate_to(URL)
        await asyncio.sleep(6)
        p=await s.must_get_current_page()
        for lab in LABELS:
            with contextlib.suppress(Exception):
                r=await cdpa.cdp_choose_button_by_label(p,lab,"Yes")
                print(f"LABEL={lab[:45]!r} -> cdp_choose_button_by_label={r}")
    finally:
        with contextlib.suppress(Exception): await s.kill()
asyncio.run(main())
