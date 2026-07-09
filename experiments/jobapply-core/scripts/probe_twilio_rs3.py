"""Live-CDP probe #3 — confirm the THRASH-POISONS-READ mechanism + the reset fix.
1) fresh core.read_options(question_66968126)  -> expect 4
2) cdp_choose_react_select on the node (the thrashing rung)
3) core.read_options again                      -> poisoned? (expect 0 if hypothesis holds)
4) Escape via CDP + settle, core.read_options    -> recovered? (expect 4 if a reset fixes it)
No submit. Run after any sweep finishes.
"""
import asyncio, contextlib, sys, tempfile

URL = sys.argv[1] if len(sys.argv) > 1 else "https://job-boards.greenhouse.io/twilio/jobs/7936698"


async def main():
    from browser_use import BrowserProfile, BrowserSession
    import oa_cdp_action as cdpa
    import oa_cdp_core as core

    profile = BrowserProfile(headless=True, keep_alive=True, viewport={"width":1280,"height":1000},
        enable_default_extensions=False, user_data_dir=tempfile.mkdtemp(prefix="tw3_"), args=["--no-sandbox"])
    session = BrowserSession(browser_profile=profile)
    await session.start()
    with contextlib.suppress(Exception):
        await session.navigate_to(URL)
    await asyncio.sleep(8)
    page = await session.must_get_current_page()

    # find the combobox node + name
    node = None; name = "question_66968126"
    with contextlib.suppress(Exception):
        from oa_discover import discover_fields
        for f in await discover_fields(page):
            lab = (getattr(f, "label", "") or "").lower()
            if "source of your right" in lab and getattr(f, "type", "") == "combobox":
                node = getattr(f, "node", None) or getattr(f, "_node", None)
                name = getattr(f, "name", "") or name
                break

    print("1) fresh read_options:", (await core.read_options(session, name))[:4])

    if node is not None:
        async def _pick(v, opts):
            from brain import pick_option
            return await pick_option(v, opts, llm=None, label="src")
        with contextlib.suppress(Exception):
            got = await cdpa.cdp_choose_react_select(session, node, "Other", pick=_pick)
            print("2) cdp_choose_react_select ->", repr(got))
    else:
        print("2) node not found, skipping thrash")

    print("3) read_options after thrash:", (await core.read_options(session, name))[:4])

    # 4) reset: Escape on the control, then blur, then re-read
    with contextlib.suppress(Exception):
        r = await core._trigger(session, name)
        if r:
            cdp_session, sid, oid = r
            await cdpa._call_on(cdp_session, sid, oid, "function(){ this.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})); this.blur && this.blur(); }")
            await asyncio.sleep(0.6)
    print("4) read_options after Escape+blur:", (await core.read_options(session, name))[:4])

    with contextlib.suppress(Exception):
        await session.kill()


if __name__ == "__main__":
    asyncio.run(main())
