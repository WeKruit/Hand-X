import asyncio, os, sys, json
BASE = "/Users/adam/Desktop/WeKruit/VALET & GH/Hand-X/.claude/worktrees/observe-act-generic/experiments/jobapply-core"
sys.path.insert(0, BASE)
from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, '.env'))
os.environ.setdefault('OA_NO_SANDBOX', '1')

import ats_engine as eng  # noqa: E402
from browser_use import BrowserProfile, BrowserSession  # noqa: E402

URL = "https://jobs.ashbyhq.com/apolink/65f1af51-43ef-419"  # will be completed from argv
if len(sys.argv) > 1:
    URL = sys.argv[1]

DUMP_JS = r"""() => {
  const qs = [...document.querySelectorAll('*')].filter(e =>
    (e.innerText||'').trim().toLowerCase().startsWith('have you worked on satellite') && e.children.length < 25);
  if(!qs.length) return JSON.stringify({found:false, hasSat:/satellite/i.test(document.body.innerText||'')});
  const q = qs[qs.length-1];
  let cont = q.parentElement;
  for(let i=0;i<5 && cont;i++){ if(/\byes\b/i.test(cont.innerText||'') && /\bno\b/i.test(cont.innerText||'')) break; cont = cont.parentElement; }
  const scope = cont || q.parentElement;
  const noBtn = [...scope.querySelectorAll('button,[role=button],div,label,span')].find(e => (e.innerText||'').trim()==='No' && e.children.length<=2);
  if(!noBtn) return JSON.stringify({found:true, noBtn:false});
  const snap = e => ({tag:e.tagName, cls:String(e.className||''), aria:e.getAttribute('aria-checked'),
    ariaPressed:e.getAttribute('aria-pressed'), dstate:e.getAttribute('data-state'), sel:e.getAttribute('data-selected')});
  const before = snap(noBtn);
  const beforeCont = snap(noBtn.parentElement);
  // click it (real user path: pointer + click)
  for(const t of ['pointerdown','mousedown','mouseup','click']) noBtn.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
  return JSON.stringify({found:true, noBtn:true, before, beforeCont, __clicked:true}, null, 1);
}"""
AFTER_JS = r"""() => {
  const qs = [...document.querySelectorAll('*')].filter(e =>
    (e.innerText||'').trim().toLowerCase().startsWith('have you worked on satellite') && e.children.length < 25);
  if(!qs.length) return '{}';
  const q = qs[qs.length-1]; let cont=q.parentElement;
  for(let i=0;i<5 && cont;i++){ if(/\byes\b/i.test(cont.innerText||'') && /\bno\b/i.test(cont.innerText||'')) break; cont=cont.parentElement; }
  const scope = cont || q.parentElement;
  const noBtn = [...scope.querySelectorAll('button,[role=button],div,label,span')].find(e => (e.innerText||'').trim()==='No' && e.children.length<=2);
  const snap = e => e?({tag:e.tagName, cls:String(e.className||''), aria:e.getAttribute('aria-checked'), ariaPressed:e.getAttribute('aria-pressed'), dstate:e.getAttribute('data-state'), sel:e.getAttribute('data-selected')}):null;
  return JSON.stringify({AFTER_click_No: snap(noBtn), AFTER_cont: snap(noBtn&&noBtn.parentElement)}, null, 1);
}"""

async def main():
    bp = BrowserProfile(headless=True, keep_alive=True, viewport={'width':1280,'height':900},
                        enable_default_extensions=False, args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu'])
    session = BrowserSession(browser_profile=bp)
    await session.start()
    try:
        with __import__('contextlib').suppress(Exception):
            await session.navigate_to(URL)
        await asyncio.sleep(3.0)
        page = await session.must_get_current_page()
        # reveal the form
        with __import__('contextlib').suppress(Exception):
            await eng._try_apply_click(session, page)
            await asyncio.sleep(3.0)
            page = await session.must_get_current_page()
        # DIRECT test of cdp_choose_button_by_label on the live page (isolate the fn from engine routing)
        import oa_cdp_action as cdpa
        for lab in ["Have you worked on satellite missions before?", "Have you worked on satellite missions before?*"]:
            got = await cdpa.cdp_choose_button_by_label(page, lab, "No")
            print(f"cdp_choose_button_by_label(label={lab!r}, 'No') -> {got!r}")
        # what containers does the label-anchor match?
        probe = await page.evaluate(r"""() => {
          const low = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
          const labLow = 'have you worked on satellite missions before?';
          const cands = [...document.querySelectorAll('div,fieldset,section')].filter(e => {
            const t = low(e.innerText); return e.children.length<40 && t && t.startsWith(labLow.slice(0,40)); });
          return JSON.stringify({matched: cands.length, first: cands[0]?({tag:cands[0].tagName, cls:String(cands[0].className).slice(0,50), text:(cands[0].innerText||'').slice(0,50)}):null});
        }""")
        print("label-anchor container probe:", probe)
    finally:
        with __import__('contextlib').suppress(Exception):
            await session.stop()

asyncio.run(main())
