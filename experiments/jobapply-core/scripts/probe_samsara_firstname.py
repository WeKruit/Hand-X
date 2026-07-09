"""Live-DOM probe (browser_use, NOT playwright): WHY does Samsara First Name read-back 'Jordan'
(verdict CORRECT) while the visible field stays empty? Two hypotheses:
  A) the locator binds a HIDDEN/DUPLICATE first-name input (read-back reads the wrong node);
  B) a React controlled input REVERTS after commit, and the read-back fires before the revert.

Enumerates EVERY text input (identity + visibility + rect + value), sets 'PROBE_JORDAN' on every
first-name candidate, waits, and RE-READS — showing which input holds it and whether the VISIBLE
one does. Distinguishes A (two inputs, value on the hidden one) from B (value on the visible one
then cleared after a longer settle). No submit. Run AFTER any sweep finishes (bundled chromium).

Run: .venv/bin/python scripts/probe_samsara_firstname.py 'https://www.samsara.com/company/careers/roles/7618026?gh_jid=7618026'
"""
import asyncio
import sys
import tempfile

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.samsara.com/company/careers/roles/7618026?gh_jid=7618026"

ENUM_JS = r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('input, textarea')) {
    const ty = (el.type || '').toLowerCase();
    if (['hidden','file','checkbox','radio','submit','button'].includes(ty)) continue;
    const r = el.getBoundingClientRect();
    const lbl = (el.labels && el.labels[0] && el.labels[0].innerText) || '';
    const aria = el.getAttribute('aria-label') || '';
    const ph = el.getAttribute('placeholder') || '';
    const cs = getComputedStyle(el);
    out.push({
      name: el.getAttribute('name') || '', id: el.id || '', type: ty,
      label: (lbl||aria||ph).replace(/\s+/g,' ').slice(0,50),
      value: el.value,
      visible: r.width>0 && r.height>0 && cs.visibility!=='hidden' && cs.display!=='none' && cs.opacity!=='0',
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      display: cs.display,
    });
  }
  return out;
}
"""

SET_JS = r"""
() => {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const hits = [];
  for (const el of document.querySelectorAll('input')) {
    const nm = (el.getAttribute('name')||'').toLowerCase();
    const lbl = ((el.labels&&el.labels[0]&&el.labels[0].innerText)||el.getAttribute('aria-label')||el.getAttribute('placeholder')||'').toLowerCase();
    if ((nm.includes('first') && !nm.includes('preferred')) || lbl.includes('first name')) {
      setter.call(el, 'PROBE_JORDAN');
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      const r = el.getBoundingClientRect();
      hits.push({name: el.getAttribute('name'), id: el.id, visible: r.width>0&&r.height>0});
    }
  }
  return hits;
}
"""


def _fn(i):
    n = (i.get("name") or "").lower(); l = (i.get("label") or "").lower()
    return "first" in n or "first name" in l


async def main():
    from browser_use import BrowserProfile, BrowserSession
    profile = BrowserProfile(
        headless=True, keep_alive=True, viewport={"width": 1280, "height": 900},
        enable_default_extensions=False, user_data_dir=tempfile.mkdtemp(prefix="probe_"),
        args=["--no-sandbox"],
    )
    session = BrowserSession(browser_profile=profile)
    await session.start()
    import contextlib
    with contextlib.suppress(Exception):
        await session.navigate_to(URL)
    await asyncio.sleep(7)
    page = await session.must_get_current_page()

    inputs = await page.evaluate(ENUM_JS)
    print(f"\n=== {len(inputs)} text inputs ===")
    for i in inputs:
        print(f"  vis={i['visible']!s:5} name={(i['name'] or '-')[:20]:20} label={(i['label'] or '-')[:24]:24} val={i['value']!r:10} rect={i['rect']}{'  <== FIRST-NAME' if _fn(i) else ''}")

    print("\n=== ENGINE discover_fields — first-name candidates ===")
    with contextlib.suppress(Exception):
        from oa_discover import discover_fields
        for f in await discover_fields(page):
            if "first" in (getattr(f, "name", "") or "").lower() or "first name" in (getattr(f, "label", "") or "").lower():
                print(f"  discovered: name={getattr(f,'name','')!r} label={getattr(f,'label','')[:40]!r} type={getattr(f,'type','')!r}")

    print("\n=== SET 'PROBE_JORDAN' on first-name inputs, re-read after settle ===")
    hits = await page.evaluate(SET_JS)
    print("set on:", hits)
    await asyncio.sleep(2.5)  # let React re-render / possibly revert
    inputs2 = await page.evaluate(ENUM_JS)
    for i in inputs2:
        if _fn(i):
            print(f"  AFTER: vis={i['visible']!s:5} name={(i['name'] or '-')[:20]:20} val={i['value']!r}")
    with contextlib.suppress(Exception):
        await session.kill()


if __name__ == "__main__":
    asyncio.run(main())
