"""Live-DOM probe: duolingo dropdown structure. My choice render-verify gate binds via
.select__control (react-select) but duolingo returned bound:False for sponsorship/authorized — so
duolingo uses a DIFFERENT widget. Dump the control + value-render for the sponsorship, experience,
and gender fields so the gate binding (and the gender multi-select fix) is grounded in real DOM.
No submit. Run: .venv/bin/python scripts/probe_duolingo_dom.py
"""
import asyncio, contextlib, json, sys, tempfile

URL = sys.argv[1] if len(sys.argv) > 1 else "https://careers.duolingo.com/jobs/8442932002?gh_jid=8442932002"
NEEDLES = ["require sponsorship", "level of experience", "gender identity", "authorized to work"]

DUMP_JS = r"""
(needles) => {
  const nrm = s => (s||'').replace(/\s+/g,' ').trim();
  const out = [];
  // find each field's control: an interactive widget whose nearest preceding label matches a needle
  const labelEls = [...document.querySelectorAll('label,div,span,p,legend')].filter(e => {
    const t = nrm(e.innerText).toLowerCase();
    return needles.some(n => t.includes(n)) && nrm(e.innerText).length < 160;
  });
  for (const needle of needles) {
    // nearest control after a label containing the needle
    let lab = labelEls.find(e => nrm(e.innerText).toLowerCase().includes(needle));
    if (!lab) { out.push({needle, found:false}); continue; }
    // walk forward siblings / descendants of the label's container to the widget
    let scope = lab.parentElement || lab;
    for (let i=0;i<4 && scope;i++){ if (scope.querySelector('input,select,button,[role=combobox],[role=listbox],[class*=control],[class*=Select],[aria-haspopup]')) break; scope = scope.parentElement; }
    const w = scope && scope.querySelector('input,select,button,[role=combobox],[class*=control],[class*=Select],[aria-haspopup]');
    if (!w) { out.push({needle, found:false, labText:nrm(lab.innerText).slice(0,40)}); continue; }
    // climb to the visual control box
    let ctrl = w; for (let i=0;i<4&&ctrl;i++){ const c=String(ctrl.className||''); if (/control|select|dropdown|combobox/i.test(c)) break; ctrl=ctrl.parentElement; }
    ctrl = ctrl || w;
    const sv = ctrl.querySelector('[class*=singleValue],[class*=single-value],[class*=value],[class*=Value]');
    const ph = ctrl.querySelector('[class*=placeholder],[class*=Placeholder]');
    out.push({
      needle, found:true,
      wTag:w.tagName, wRole:w.getAttribute('role'), wClass:String(w.className).slice(0,50),
      wType:w.tagName==='INPUT'?w.type:null, wValue:w.tagName==='INPUT'||w.tagName==='SELECT'?String(w.value).slice(0,30):null,
      ctrlTag:ctrl.tagName, ctrlClass:String(ctrl.className).slice(0,50),
      ctrlText:nrm(ctrl.innerText).slice(0,40),
      valueSpanClass: sv?String(sv.className).slice(0,40):null, valueSpanText: sv?nrm(sv.innerText).slice(0,30):null,
      placeholderClass: ph?String(ph.className).slice(0,40):null, placeholderText: ph?nrm(ph.innerText).slice(0,20):null,
    });
  }
  return JSON.stringify(out);
}
"""


async def main():
    from browser_use import BrowserProfile, BrowserSession
    profile = BrowserProfile(headless=True, keep_alive=True, viewport={"width":1280,"height":1000},
        enable_default_extensions=False, user_data_dir=tempfile.mkdtemp(prefix="duo_"), args=["--no-sandbox"])
    session = BrowserSession(browser_profile=profile)
    await session.start()
    with contextlib.suppress(Exception):
        await session.navigate_to(URL)
    await asyncio.sleep(8)
    page = await session.must_get_current_page()
    res = json.loads(await page.evaluate(DUMP_JS, NEEDLES))
    for r in res:
        print(json.dumps(r, indent=1))
    with contextlib.suppress(Exception):
        await session.kill()


if __name__ == "__main__":
    asyncio.run(main())
