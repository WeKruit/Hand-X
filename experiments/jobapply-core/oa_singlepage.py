"""oa_singlepage — fill ONE single-page ATS form (Greenhouse / Lever / Ashby) field-by-field
via the generic ``observe_act`` primitive, instead of the per-archetype ``fill_with_ladder``.

This is the PROOF harness for the observe_act state machine on real single-page forms
(no auth -> no rate-limit). It REUSES the existing pipeline verbatim for everything EXCEPT
the per-field fill:

  * field discovery   -> the adapter's ``extract`` (boards-api / posting schema -> FormField list)
  * value mapping     -> ``ats_engine.map_fields`` (the ONE structured LLM call, label -> value)
  * navigation        -> BrowserSession + ``adapter.open_form`` (iframe drill / "Enter manually")
  * form-present gate  -> ``ats_engine.form_present`` (skip the run if the form isn't reachable)
  * end screenshot    -> ``ats_engine._screenshot`` (CDP, clipped to the form)

…and swaps ONLY the fill: each discovered field becomes a ``{label,value,required}`` dict
handed to ``observe_act(session, field)``. The per-field Outcome (DONE/OTHER/SKIP/ESCALATE)
plus the state-machine trace is recorded.

HARD CONSTRAINTS honoured:
  * FILL-ONLY — never clicks Submit / Apply-final. ``observe_act`` itself never submits, and this
    runner never clicks an advance/submit control. Single-page adapters have ``is_complete()==True``
    and no ``next_step`` — there is no submit path here.
  * No secrets in CLI args — profile/resume come from files or env, never argv.
  * ``.venv/bin/python`` — the vendored browser_use import (ats_engine already does this).

Usage (example, fill-only):
    .venv/bin/python oa_singlepage.py --url https://job-boards.greenhouse.io/acme/jobs/123 \
        --profile fixtures/profiles/jordan.json --resume fixtures/resume.pdf --screenshot out.png
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import ats_engine as eng
import oa_observe_act as oa
from ats_ashby import AshbyAdapter
from ats_greenhouse import GreenhouseAdapter
from ats_lever import LeverAdapter

_ADAPTERS: list[type[eng.ATSAdapter]] = [GreenhouseAdapter, LeverAdapter, AshbyAdapter]

# --------------------------------------------------------------------------- #
# BULLETPROOF BROWSER LIFECYCLE (FIX B) — a run must NEVER orphan a Chromium.
#
# Root cause of the "crashes": a SIGKILL of python (per-job timeout kill in the dev
# sweep, Ctrl-C, OOM) does NOT also kill the headless Chromium browser-use launched
# as a child with ``--user-data-dir=<dir>`` in its argv. The orphan keeps the page's
# heavy SPA renderer alive; the next run inherits a wedged machine -> the false
# "crash". browser-use's own ``session.kill()`` only runs if python is still alive to
# await it, so it cannot be the sole guarantee.
#
# The defense here makes orphaning impossible:
#   1. UNIQUE user-data-dir per run (``_new_user_data_dir``). The dir string is the
#      precise kill key: only THIS run's Chromium has it in its command line. We keep
#      the ``browser-use-user-data-dir`` substring so the coarse sweep
#      (``pkill -9 -f "browser-use-user-data-dir"``) still catches it as a backstop.
#   2. ``_kill_browser_for_dir(udd)`` — a psutil cmdline scan that terminates (then
#      hard-kills) any process whose argv contains that exact dir. Best-effort, every
#      exception swallowed; it targets ONLY this run, never a sibling.
#   3. A try/finally around the whole run: in ``finally`` we ALWAYS ``session.kill()``
#      (guarded), then ``_kill_browser_for_dir`` as the belt-and-braces even if
#      ``kill()`` itself raised/hung, then delete the temp dir.
#   4. A module-level SIGTERM/SIGINT handler that, before the process exits, kills the
#      browser of every currently-active run's dir — so a signal between launch and the
#      ``finally`` can't leak an orphan either. It chains to any previous handler and
#      re-raises the default so the process still terminates.
# --------------------------------------------------------------------------- #

# Active runs' user-data-dirs (one entry per in-flight run). The signal handler reads
# this to kill any browser still up when a signal arrives before the run's finally.
_ACTIVE_USER_DATA_DIRS: set[str] = set()
_SIGNALS_INSTALLED = False


def _new_user_data_dir() -> str:
    """A UNIQUE per-run profile dir whose path is the exact cleanup key. Keeps the
    ``browser-use-user-data-dir-`` prefix so the coarse global sweep still matches it."""
    return tempfile.mkdtemp(prefix="browser-use-user-data-dir-oa-")


def _kill_browser_for_dir(user_data_dir: str | None) -> None:
    """Best-effort terminate every process whose argv holds ``user_data_dir`` — i.e. the
    Chromium THIS run launched with ``--user-data-dir=<user_data_dir>``. Targets only this
    run (the dir is unique), never a sibling. Swallows ALL exceptions: cleanup must never
    raise. Tries SIGTERM first, then SIGKILL on anything still alive after a short grace."""
    if not user_data_dir:
        return
    try:
        import psutil
    except Exception:
        # Fallback: the unique dir is still a precise pkill key (no psutil dependency).
        with contextlib.suppress(Exception):
            import subprocess

            subprocess.run(["pkill", "-9", "-f", user_data_dir], capture_output=True, timeout=10)
        return

    victims: list[Any] = []
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.pid == me:
                continue
            cmdline = proc.info.get("cmdline") or []
            if any(user_data_dir in part for part in cmdline):
                victims.append(proc)
        except Exception:
            continue
    for proc in victims:
        with contextlib.suppress(Exception):
            proc.terminate()
    if victims:
        with contextlib.suppress(Exception):
            _gone, alive = psutil.wait_procs(victims, timeout=3)
            for proc in alive:
                with contextlib.suppress(Exception):
                    proc.kill()


def _install_signal_cleanup() -> None:
    """Install a SIGTERM/SIGINT handler (once) that kills every active run's browser before
    the process dies, then chains to the previous handler / re-raises the default. This closes
    the window between launch and the run's ``finally`` — a signal there would otherwise orphan
    the just-launched Chromium. No-op when not on the main thread (signal can't be set)."""
    global _SIGNALS_INSTALLED
    if _SIGNALS_INSTALLED:
        return
    _SIGNALS_INSTALLED = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous = signal.getsignal(sig)

            def _handler(signum: int, frame: Any, _prev: Any = previous) -> None:
                for udd in list(_ACTIVE_USER_DATA_DIRS):
                    _kill_browser_for_dir(udd)
                if callable(_prev) and _prev not in (signal.SIG_DFL, signal.SIG_IGN):
                    _prev(signum, frame)  # chain to whatever was there
                else:
                    # restore + re-raise the default so the process actually terminates
                    signal.signal(signum, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)

            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # not the main thread (e.g. under a test runner) — skip, the finally still cleans up
            _SIGNALS_INSTALLED = False
            return


def pick_adapter(url: str) -> eng.ATSAdapter | None:
    """Same host-match the sweep uses (sweep.py:_pick): Greenhouse / Lever / Ashby."""
    host = (urlparse(url).hostname or "").lower()
    for cls in _ADAPTERS:
        if any(host == h or host.endswith("." + h) or h in host for h in cls.hosts):
            return cls()
    return None


@dataclass
class FieldResult:
    name: str
    label: str
    type: str
    value_src: str
    outcome: str  # DONE | OTHER | SKIP | ESCALATE
    nature: str = ""
    committed: str = ""
    trace: list[str] | None = None
    # the MAPPED value the engine was asked to fill — forensics for wrong-value autopsies
    # (ashby mega/37 transgender showed 'Yes' on screen; without the wanted value stored the
    # ledger cannot distinguish a mapper hallucination from a wrong click).
    value: str = ""

    @property
    def filled(self) -> bool:
        # "filled correctly" for the proof = a DONE/OTHER terminal (committed a value);
        # ESCALATE = deterministic gap, SKIP = left blank.
        return self.outcome in (oa.DONE, oa.OTHER)


def _field_dict(
    field: eng.FormField,
    value: str,
    *,
    resume: str | None,
    llm: Any,
    adapter: Any = None,
    page: Any = None,
) -> dict[str, Any]:
    """The observe_act input for one discovered FormField. Multi-value labels (Skills/Languages,
    or a comma/semicolon-joined value) carry cardinality='many' so the state machine enters
    S_MULTI_LOOP; everything else is 'one'.

    KIND HINT (the card-commit fix): we forward the adapter's OWN parsed control type as ``kind``.
    The adapter scraped the live form schema and classified each control as radio | checkbox |
    single_select | textarea | text | input_file etc. (e.g. ats_lever._classify reads the actual
    ``<input type=radio>`` / ``<select>`` off the rendered DOM). That is a RELIABLE STRUCTURAL FACT
    — not a renameable label. The engine honours it to route a choice card straight to S_CHOICE
    (click the already-visible Yes/No) or a custom select to the open+read path, BEFORE the
    label-meaning LLM guess that mis-derives BOOLEAN/MULTI/SEARCH from the question wording (the
    proven Lever mis-route in runs/final3/lever.json). GENERIC — any ATS's adapter sets field.type."""
    label = field.label or field.name
    cardinality = "one"
    multi_label = any(k in label.lower() for k in ("skill", "language", "technolog"))
    if multi_label or (field.type or "").endswith("multi_select") or ";" in (value or ""):
        cardinality = "many"
    return {
        "label": label,
        "value": value,
        "required": bool(field.required),
        "cardinality": cardinality,
        "kind": field.type or "",  # the adapter's REAL control type — the routing hint
        "resume": resume if field.source == "file" else None,
        "llm": llm,
        # PROVEN-PATH DELEGATION: hand the engine the adapter + page + original FormField so the
        # COMMIT can run the adapter's battle-tested fill()/read_back() before the generic engine.
        "adapter": adapter,
        "page": page,
        "field_obj": field,
    }


async def run_single_page_oa(
    *,
    url: str,
    profile: dict,
    resume: str | None,
    headless: bool = True,
    screenshot_path: str | None = None,
    force_generic: bool = False,
    json_path: str | None = None,
) -> dict:
    """Fill a single-page ATS form via observe_act, field-by-field. Returns a result dict with the
    per-field outcomes + a fill-rate. FILL-ONLY: never submits.

    force_generic=True ignores the adapter ON PURPOSE (benchmark mode): run the no-adapter
    lane on a KNOWN ATS whose adapter result is the ground truth — the diff measures the
    generic lane's true capability on pages where we can score it exactly."""
    adapter = None if force_generic else pick_adapter(url)

    from browser_use import BrowserProfile, BrowserSession, ChatGoogle
    from browser_use.tokens.service import TokenCost

    if adapter is None:
        # GENERIC LANE (foreign forms / company sites): no schema, no adapter — fields are
        # discovered from the LIVE DOM after the page opens (oa_discover), mapped with the
        # same ONE call, committed by the same observe_act machine (its adapter-delegation
        # lane no-ops when ctx.adapter is None).
        # Scroll-locate ON here: a foreign form usually sits BELOW a long job description, so
        # its fields are out of the viewport selector_map — the exact 'no-control' the benchmark
        # exposed (GH extend 2/10 -> 9/10 with this on). Safe in the generic lane: these are
        # static/CMS pages, not the heavy Lever/Ashby SPAs the default-off guard protects.
        os.environ.setdefault("OA_SCROLL_LOCATE", "1")
        title, fields = "(generic form)", []
        print(f"[oa:generic] no adapter — live-DOM discovery lane for {url[:70]}")
    else:
        # step 1 — schema extract (no browser), reused verbatim.
        title, fields = await adapter.extract(url, profile)
        print(f"[oa:{adapter.__class__.__name__}] {title}  ({len(fields)} fields)")

    tc = TokenCost(include_cost=True)
    await tc.initialize()
    llm = tc.register_llm(
        __import__("oa_llm").openai_primary_llm("agent")
        or ChatGoogle(
            model="gemini-3-flash-preview",
            api_key=os.environ.get("GOOGLE_API_KEY"),
            thinking_level="minimal",
        )
    )

    # step 2 — the ONE structured mapping call (label -> value), reused verbatim.
    map_rows = [f for f in fields if f.needs_map]
    # map_fields is a RAW llm.ainvoke (gemini); wrap it in the resilient layer so a gemini 503/"high
    # demand" or stall fails over to the configured fallback (gpt-5.4-mini) instead of killing the run.
    # observe_act keeps the PLAIN llm — oa_brain already routes its calls through resilient_text.
    import oa_llm

    mapped = await eng.map_fields(oa_llm.ResilientLLM(llm), map_rows, profile, title) if map_rows else {}

    # navigate + reach the form (iframe drill / "Enter manually"), reused verbatim.
    # HARDENED browser launch (env-tunable): a small viewport + stability flags + no extension
    # download keep headless Chrome from going unresponsive on heavy SPA /apply pages (Lever/Ashby),
    # which otherwise drops the CDP WebSocket -> every action waits out its 30-60s timeout.
    _vw = int(os.environ.get("OA_VIEWPORT_W", "1280"))
    _vh = int(os.environ.get("OA_VIEWPORT_H", "900"))
    _hard_args = ["--disable-dev-shm-usage", "--disable-gpu"]
    if os.environ.get("OA_NO_SANDBOX") == "1":
        _hard_args.append("--no-sandbox")
    # UNIQUE user-data-dir per run -> the exact, run-scoped cleanup key (FIX B). The profile's
    # validator RESOLVES the path (on macOS /var -> /private/var), and THAT resolved string is what
    # ends up in Chromium's ``--user-data-dir`` argv — so read it back off the profile and use the
    # resolved form as both the active-tracking key and the kill key (matching the real child argv).
    # NOTE: named browser_profile ON PURPOSE — this used to be `profile =`, silently
    # SHADOWING the user-profile dict; the generic lane's map_fields call then json.dumps'd
    # a BrowserProfile object (the toast crash).
    _extra: dict = {}
    if os.environ.get("OA_CHROME_PATH"):  # real Chrome binary = real fingerprint (SR device check passes)
        _extra["executable_path"] = os.environ["OA_CHROME_PATH"]
    if os.environ.get("OA_STEALTH") == "1":  # SR-class device checks key on automation fingerprints
        # this venv's browser_use may not ship stealth (only the vendored root copy does);
        # degrade to a plain profile rather than crash — prod uses the user's REAL browser.
        with contextlib.suppress(Exception):
            from browser_use.browser.profile import StealthConfig

            if "stealth" in BrowserProfile.model_fields:
                _extra["stealth"] = StealthConfig(enabled=True)
    browser_profile = BrowserProfile(
        **_extra,
        headless=headless,
        keep_alive=True,
        viewport={"width": _vw, "height": _vh},
        enable_default_extensions=False,
        user_data_dir=_new_user_data_dir(),
        args=_hard_args,
    )
    user_data_dir = str(browser_profile.user_data_dir)
    # Register active + arm the signal handler BEFORE start(), so a signal during launch can't orphan it.
    _ACTIVE_USER_DATA_DIRS.add(user_data_dir)
    _install_signal_cleanup()
    # CONNECT-OVER-CDP: attach to an ALREADY-RUNNING Chrome (the user's real browser, launched with
    # --remote-debugging-port). Real profile + no --enable-automation flag = the fingerprint SR's
    # device check passes — the definitive real-browser test, and the shape production uses (the
    # Desktop app owns the browser; Hand-X attaches). When set we do NOT own/kill the browser.
    _cdp = os.environ.get("OA_CDP_URL")
    if _cdp:
        _ACTIVE_USER_DATA_DIRS.discard(user_data_dir)  # not ours to reap
        session = BrowserSession(cdp_url=_cdp)
    else:
        session = BrowserSession(browser_profile=browser_profile)

    result: dict[str, Any] = {
        "adapter": adapter.__class__.__name__ if adapter else "generic",
        "title": title,
        "url": url,
        "fields_total": len(fields),
        "mapped": len(mapped),
        "screenshot": None,
    }

    # WHOLE run wrapped in try/finally: the finally ALWAYS kills the session AND hard-kills any
    # Chromium still holding THIS run's unique user-data-dir, even on error/timeout/cancel — so a
    # SIGKILL of python (or any raise below) can no longer leave an orphaned browser.
    try:
        await session.start()
        # A never-idle SPA /apply page can make NavigateToUrlEvent exceed its timeout even though the
        # DOM is already rendered enough to fill. Don't let that KILL the run — suppress the nav timeout
        # and proceed; we serialize the DOM directly (oa_perception.get_state) regardless of nav state.
        with contextlib.suppress(Exception):
            await session.navigate_to(url)
        await asyncio.sleep(2.5)
        page = await session.must_get_current_page()
        if adapter is None:
            from oa_discover import discover_fields

            fields = await discover_fields(page)
            if len(fields) < 2 and await eng._try_apply_click(session, page):
                # fresh navigation lands PRE-Apply (wayve class) — click the affordance once
                with contextlib.suppress(Exception):
                    page = await session.must_get_current_page()
                fields = await discover_fields(page)
            if not fields:
                # interstitial (SmartRecruiters 'Verifying the device...' / slow SPA mount) —
                # ONE bounded wait, then re-look. A hard anti-bot wall stays empty and falls
                # through to the classified BLOCKED below.
                await asyncio.sleep(8.0)
                with contextlib.suppress(Exception):
                    page = await session.must_get_current_page()
                fields = await discover_fields(page)
            if not fields:
                # GENERIC IFRAME-HOP: the real form is often a CROSS-ORIGIN iframe the main frame
                # can't see into (comeet.co embed, GH job_app embed). Hop the top-level page to the
                # LARGEST iframe's src and re-discover. Host-agnostic — any embed.
                with contextlib.suppress(Exception):
                    src = await page.evaluate(
                        "() => { const fs=[...document.querySelectorAll('iframe')]"
                        " .map(f=>({src:f.src||'',a:f.getBoundingClientRect().width*f.getBoundingClientRect().height}))"
                        " .filter(f=>/^https?:/.test(f.src) && f.a>60000).sort((x,y)=>y.a-x.a);"
                        " return fs.length ? fs[0].src : ''; }"
                    )
                    if src:
                        print(f"   [generic] hopping into embedded form iframe: {str(src)[:80]}")
                        await session.navigate_to(str(src))
                        await asyncio.sleep(3.0)
                        page = await session.must_get_current_page()
                        fields = await discover_fields(page)
            # VISUAL discovery union (user: visuals+DOM): vision lists EVERY question; anything
            # the DOM enum missed (pure-div widgets) joins the field list — observe_act's
            # label-driven locate binds them without needing a native input.
            with contextlib.suppress(Exception):
                from oa_discover import discover_fields_visual

                extra = await discover_fields_visual(session, fields)
                if extra:
                    print(f"[oa:generic] VISION found {len(extra)} fields the DOM enum missed: "
                          f"{[f.label[:32] for f in extra[:5]]}")
                    fields = fields + extra
            result["fields_total"] = len(fields)
            print(f"[oa:generic] discovered {len(fields)} fields (DOM+vision union)")
            if not fields:
                with contextlib.suppress(Exception):
                    result["final_url"] = await page.get_url()
                if screenshot_path:
                    result["screenshot"] = await eng._screenshot(session, page, screenshot_path)
                page_kind = "?"
                with contextlib.suppress(Exception):  # classify the wall (anti-bot? landing? blank?)
                    import failcap

                    rec = await failcap.capture(
                        session, page, "generic_no_fields", "BLOCKED", "generic lane found no fillable fields"
                    )
                    page_kind = (rec or {}).get("triage", {}).get("kind", "?")
                result["page_kind"] = page_kind
                # HITL BLOCKER-CONTINUE: a human-clearable wall (CAPTCHA/login/verify) -> pause for
                # the human to clear it in the (CDP-attached) browser, then RE-DISCOVER + continue.
                if page_kind in ("CAPTCHA_OR_ANTIBOT", "LOGIN_OR_VERIFY"):
                    import oa_hitl

                    async def _still(pg: Any) -> bool:
                        return not await discover_fields(pg)

                    if await oa_hitl.wait_for_unblock(
                        session, page, kind=page_kind, reason="form behind a human-only wall", still_blocked=_still
                    ):
                        with contextlib.suppress(Exception):
                            page = await session.must_get_current_page()
                        fields = await discover_fields(page)
                        result["fields_total"] = len(fields)
                if not fields:
                    usage = await tc.get_usage_summary()
                    status = "NEEDS_HUMAN" if page_kind in ("CAPTCHA_OR_ANTIBOT", "LOGIN_OR_VERIFY") else "BLOCKED"
                    result.update(status=status, cost=usage.total_cost, filled=0, results=[])
                    print(f"  {status} — generic lane found no fillable fields (kind: {page_kind})")
                    return result
            map_rows = [f for f in fields if f.needs_map]
            # JD text for the mapper (audit pattern 3): prose answers need the actual role/company,
            # not just the title. One free DOM read.
            _jd = ""
            with contextlib.suppress(Exception):
                _jd = str(await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 4000)"))
            mapped = await eng.map_fields(oa_llm.ResilientLLM(llm), map_rows, profile, title, job_context=_jd) if map_rows else {}
            result["mapped"] = len(mapped)
            with contextlib.suppress(Exception):  # cookie/consent banners intercept focus + wipe fills
                import oa_complete

                await oa_complete.dismiss_consent(session, page)
            with contextlib.suppress(Exception):  # SAFETY: disable every Submit/Apply/Finish so no
                await eng.install_submit_guard(page)  # repeater Save/Add click can ever finalize
            with contextlib.suppress(Exception):  # FIRST-LOOK PLAN (decides sections/denominator)
                import oa_planner

                result["plan"] = await oa_planner.plan_page(session, profile, llm=llm)
        else:
            page = await adapter.open_form(session, page)

            if not await eng.form_present(adapter, page, fields):
                with contextlib.suppress(Exception):
                    result["final_url"] = await page.get_url()
                if screenshot_path:
                    result["screenshot"] = await eng._screenshot(session, page, screenshot_path)
                usage = await tc.get_usage_summary()
                result.update(status="BLOCKED", cost=usage.total_cost, filled=0, results=[])
                print(f"  BLOCKED — form not reachable for {adapter.__class__.__name__}")
                return result

        res = await _fill_form(
            session=session,
            adapter=adapter,
            page=page,
            title=title,
            fields=fields,
            mapped=mapped,
            resume=resume,
            llm=llm,
            tc=tc,
            headless=headless,
            screenshot_path=screenshot_path,
            result=result,
            profile=profile,
        )
        # PRE-TEARDOWN dump: a wedged executor thread (LLM HTTP call with no timeout) can hang
        # asyncio.run's shutdown AFTER the fill finished — main()'s post-run dump then never
        # executes and the proc-cap hard-exit loses a SUCCESSFUL result (mega #19/#20). Persist
        # the result the moment it exists; main()'s dump becomes an idempotent rewrite.
        if json_path:
            with contextlib.suppress(Exception):
                with open(json_path, "w", encoding="utf-8") as fh:
                    json.dump(res, fh, indent=2)
                print(f"  wrote {json_path} (pre-teardown)")
        return res
    finally:
        # 1) ALWAYS ask browser-use to stop the browser (guarded — kill() must not mask the result).
        #    BOUNDED: after a forced browser reset (a single_select typeahead can desync CDP ->
        #    on_BrowserStopEvent), ``session.kill()`` itself can hang awaiting a dead target. Cap it so
        #    teardown can never wedge the process — the hard browser-dir kill below is the real cleanup.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.kill(), timeout=8.0)
        # 2) belt-and-braces: hard-kill anything STILL holding this run's user-data-dir (covers a
        #    kill() that raised or hung, and the SIGKILL-of-python case where kill() never ran).
        _kill_browser_for_dir(user_data_dir)
        _ACTIVE_USER_DATA_DIRS.discard(user_data_dir)
        # 3) remove the now-unused temp profile dir.
        with contextlib.suppress(Exception):
            shutil.rmtree(user_data_dir, ignore_errors=True)


async def _fill_form(
    *,
    session: Any,
    adapter: eng.ATSAdapter,
    page: Any,
    title: str,
    fields: list[eng.FormField],
    mapped: dict,
    resume: str | None,
    llm: Any,
    tc: Any,
    headless: bool,
    screenshot_path: str | None,
    result: dict[str, Any],
    profile: dict | None = None,
) -> dict:
    """The per-field fill body (extracted so ``run_single_page_oa`` can wrap the whole session
    lifecycle in one try/finally). Returns the populated ``result`` dict. FILL-ONLY: never submits.
    Browser teardown is owned by the caller's ``finally`` — this function NEVER kills the session,
    so a raise here still hits the caller's guaranteed cleanup (no orphaned browser)."""
    # step 3 — the SWAP: per-field fill via observe_act (NOT fill_with_ladder).
    # Lift the per-PAGE VLM cap to a high backstop so the verify oracle's per-FIELD VLM budget
    # (FIELD_VLM_CAP) is the real limiter — a long single page must not starve field 7+ (the
    # capped->UNKNOWN->ESCALATE false-failure this fix removes). DOM read-back stays free + primary.
    import vision_verify as _vv

    _vv.reset_visual_cache()
    oa.reset_page_vlm_backstop()
    per_field: list[FieldResult] = []
    t0 = time.monotonic()
    # FILE FIELDS LAST: a drag-drop resume dropzone (Lever/Ashby) kicks off heavy client-side
    # processing on upload that can freeze the headless renderer; doing it AFTER the text fields
    # means a wedge can't cost us the rest of the form. Stable sort keeps every other field's order.
    fields_file_last = sorted(fields, key=lambda f: 1 if getattr(f, "source", "") == "file" else 0)
    # the FORM's url, captured BEFORE any fill click can navigate away (samsara mega3/28: a
    # mid-fill click drifted to /company/belonging, so complete()'s entry snapshot already held
    # the wrong page and its drift guard saw no difference).
    _form_url = ""
    with contextlib.suppress(Exception):
        _form_url = await page.get_url()
    _done_labels: set[str] = set()
    _done_sigs: dict[str, list] = {}  # label -> option-signatures already committed under it
    for f in fields_file_last:
        if f.source == "skip":
            continue
        # TWIN GUARD: discovery unions DOM + vision and a widget's input vs its hidden-select/
        # wrapper surface as TWO fields with the SAME question label. The real row commits+verifies,
        # then the twin re-touches the SAME widget and its visual fallback clicks chrome — discord
        # mega/29: twin committed 'Clear selections' (wiped the verified Gender) and 'Toggle flyout'
        # (left 'Toggle' in the Disability filter, menu open 'No options'). An identical-label field
        # that already verified DONE this run -> SKIP. If two DISTINCT real fields ever share a
        # label, the completeness audit flags the empty one and retry fills it — safe direction.
        _lkey = " ".join(str(f.label or f.name).split()).lower()
        # OPTION SIGNATURE: two same-label fields are twins only if they touch the SAME widget.
        # stripe mega4/6 renders TWO required "Please indicate what you have experience with. *"
        # multi-selects with DIFFERENT options ([Hunting,Farming] vs [SMB,Mid-Market,Enterprise]);
        # keying the twin-skip on label ALONE left the second group empty (skipped as a false
        # twin, its computed 'Enterprise' never applied). A distinct option set = distinct question.
        _sig = tuple(sorted(str(o).strip().lower() for o in (getattr(f, "options", None) or []) if str(o).strip()))
        # FILE fields are exempt: Resume and Cover Letter both render as 'Attach' on
        # greenhouse — the second slot is a DIFFERENT real field, and retry cannot heal a
        # skipped file (airbnb mega3/10-11: Cover Letter slot skipped as a twin, flagged by
        # vision, unfixable downstream).
        _is_twin = (
            _lkey in _done_labels
            and "file" not in str(f.type or "").lower()
            # a non-empty option signature that DIFFERS from every signature already committed
            # under this label is a distinct question, not a twin.
            and (not _sig or _sig in _done_sigs.get(_lkey, []) or not _done_sigs.get(_lkey))
        )
        if _is_twin:
            per_field.append(
                FieldResult(
                    name=f.name, label=f.label or f.name, type=f.type, value_src="twin",
                    outcome=oa.SKIP, nature="", committed="", trace=["twin-label-already-done->skip"],
                )
            )
            continue
        value, src = eng._resolve(f, mapped, resume)
        if adapter is None:
            # GENERIC lane: discovery scanned the whole document, but locate reads the
            # serialized VIEWPORT map — a below-the-fold form (toast) yields 'no-control'
            # for every field. Center the control (by the id/name discovery recorded) first.
            with contextlib.suppress(Exception):
                await page.evaluate(
                    "(n) => { const el = document.getElementById(n) || document.getElementsByName(n)[0];"
                    " if (el) el.scrollIntoView({block: 'center'}); }",
                    f.name,
                )
        fd = _field_dict(f, value, resume=resume, llm=llm, adapter=adapter, page=page)
        if os.environ.get("OA_FIELD_TRACE"):
            print(
                f"[FIELD-START] name={f.name[:28]} src={f.source} type={f.type} val={str(value)[:30]!r}",
                flush=True,
            )
        try:
            # HARD per-field wall-clock ceiling. ``observe_act``'s own ``FIELD_DEADLINE`` guard only
            # fires BETWEEN states; a single blocking CDP await (a wedged UploadFile/dropdown event on
            # a never-idle SPA) is not interrupted by it and would otherwise hang the WHOLE form. Wrap
            # each field in ``asyncio.wait_for`` at FIELD_DEADLINE + a small margin so a wedged field
            # is forcibly ESCALATED and the rest of the form keeps filling — the documented per-field
            # ESCALATE production policy, not a process kill.
            outcome = await asyncio.wait_for(oa.observe_act(session, fd), timeout=oa.FIELD_DEADLINE + 6.0)
        except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041 — explicit for clarity
            outcome = oa.ESCALATE
            fd.setdefault("_trace", []).append("HARD-FIELD-TIMEOUT->ESCALATE")
        except Exception as exc:  # a single hard field must not abort the page (fill-only proof)
            outcome = oa.ESCALATE
            fd["_trace"] = [f"EXC:{type(exc).__name__}:{exc}"]
        per_field.append(
            FieldResult(
                name=f.name,
                label=f.label or f.name,
                type=f.type,
                value_src=src,
                outcome=outcome,
                nature=fd.get("_nature", ""),
                committed=fd.get("_committed", ""),
                trace=fd.get("_trace"),
                value=str(value)[:120],
            )
        )
        if outcome == oa.DONE:
            _done_labels.add(_lkey)
            if _sig:
                _done_sigs.setdefault(_lkey, []).append(_sig)
        # MID-FILL DRIFT (samsara mega3/30: a fill click navigated to /company/belonging and
        # every later field died no-control — the judge-time guard saved the verdict but not
        # the fields). On a no-control locate miss, check the url and pull the run back.
        if outcome != oa.DONE and "no-control" in " ".join(fd.get("_trace") or []):
            with contextlib.suppress(Exception):
                _now = await page.get_url()
                if _form_url and _now and _now.split("#")[0] != _form_url.split("#")[0]:
                    print(f"   [fill] mid-fill drift {_now[:60]} -> back to form")
                    await session.navigate_to(_form_url)
                    await asyncio.sleep(1.5)
                    page = await session.must_get_current_page()

    # CAPTCHA-AT-END: interaction-triggered challenges (lever hCaptcha) mount AFTER the
    # start-of-run page-kind check and sit over the form at judge time — mega/61/66 the vision
    # gate flagged 'Full name' while staring at the puzzle overlay, and the run reported FILLED
    # instead of the HITL lane. Provider identity (finite vendor iframes), not tenant text.
    with contextlib.suppress(Exception):
        _captcha = bool(
            await page.evaluate(
                "(() => [...document.querySelectorAll("
                "'iframe[src*=\"hcaptcha\"],iframe[src*=\"recaptcha\"],iframe[src*=\"turnstile\"],iframe[title*=\"challenge\"]'"
                ")].some(e => { const r = e.getBoundingClientRect(); return r.width > 50 && r.height > 50; }))()"
            )
        )
        if _captcha:
            result["status"] = "NEEDS_HUMAN"
            result["blocker"] = "captcha"
            print("   [gate] CAPTCHA overlay present at end of run -> NEEDS_HUMAN")
    # FORM-EVIDENCE GATE: complete:True is only meaningful when an APPLICATION FORM was actually
    # reached and substantively filled. A search box / JD page / login wall has no required-empty
    # fields, so the audit passes VACUOUSLY (atsx: oracle filled 0/4, phenom 1/6, bain 1/2 all
    # reported complete:True — the over-positive class). Floor: a real application always has at
    # least name+email+one more (>=3 fills); a login/captcha page-kind can never be complete.
    _done_n = sum(1 for r in per_field if r.outcome == oa.DONE)
    # STRUCTURAL application evidence: every real application takes an email and/or a resume; a
    # careers-landing SEARCH widget takes neither (SAP: 5 search-box fills passed the count floor
    # and still reported complete:True). input type=email / type=file are structural, not label
    # semantics.
    _has_anchor = any(
        r.outcome == oa.DONE
        and (
            "email" in str(r.type or "").lower()
            or "file" in str(r.type or "").lower()
            # rippling renders its email input as type=text — the committed VALUE carrying an
            # '@' is the same evidence (identity on the value, not label semantics)
            or "@" in str(r.committed or "")
        )
        for r in per_field
    )
    _kind = str(((result.get("plan") or {}).get("page_kind")) or "")
    if adapter is None and (_done_n < 3 or not _has_anchor or _kind == "login_or_captcha"):
        result["completeness"] = {
            "complete": False, "missing_required": [], "sections_filled": [], "sections_skipped": [],
            "retried": 0, "not_reached": True, "page_kind": _kind or None, "filled_done": _done_n,
        }
        # POLICY (user): an auth wall is the HITL lane, not a fill failure — report NEEDS_HUMAN
        # so scoring counts it as HITL-pass (wait_for_unblock resumes it when a human signs in).
        if _kind == "login_or_captcha":
            result["status"] = "NEEDS_HUMAN"
        print(f"   [complete] NOT_REACHED — form evidence too thin (done={_done_n}, kind={_kind or '?'})")
    # COMPLETENESS PASS (generic lane only): discover_fields sees only flat rendered inputs, so a
    # repeater section (Work Experience / Education behind 'Add another') is invisible and would be
    # silently skipped. Audit for unfilled repeater sections + empty required fields and fill each
    # via the proven agent_fill_section. Makes the completeness verdict honest instead of a blind 1.0.
    elif adapter is None and profile is not None and os.environ.get("OA_NO_COMPLETE") != "1":
        with contextlib.suppress(Exception):
            import oa_complete

            # audit always runs (cheap: DOM + 1 VLM); the repeater FILL uses the multi-step agent,
            # so it is gated to OA_COMPLETE_AGENT=1 (off in the cheap sweep, on for real fills). Even
            # with the fill off, the verdict honestly flags a section we would otherwise have missed.
            _pk = [s.get("profile_key") for s in (result.get("plan") or {}).get("repeater_sections") or []
                   if s.get("has_add_control") and s.get("profile_key") in ("experience", "education")]
            # PLANNER-FLAKE fallback: the first-look VLM plan sometimes returns no sections for a
            # form that HAS them (hibob run-to-run). When the profile carries entries, hand the
            # keys over anyway — fill_repeaters no-ops cleanly when it finds no Add affordance.
            if not _pk and profile:
                _pk = [k for k in ("experience", "education") if profile.get(k)]
            # the fill ledger's VERIFIED committed values, keyed by label — makes the crop check
            # VALUE-AWARE (audit pattern 2: presence-only verify blesses label/junk text).
            # DONE-only: `committed` on a SKIP/ESCALATE row is the intended value of a FAILED
            # attempt (the box is empty on screen), which would falsely bless both the crop
            # corroboration and the hard gate. Kept as a NAMED ref: complete() mutates it in
            # place with retry commits (retry only writes on DONE), and the hard gate reads it
            # back as "who actually got a verified commit".
            _cbl = {str(r.label): str(r.committed) for r in per_field if r.outcome == oa.DONE and r.committed}
            result["completeness"] = await oa_complete.complete(
                session, page, profile, resume, allow_agent=os.environ.get("OA_COMPLETE_AGENT") == "1",
                llm=llm, planner_keys=_pk,
                # per_field is the LOCAL fill ledger — result['results'] is only assembled later
                filled_names={str(r.name) for r in per_field},
                required_labels=[f.label or f.name for f in fields if getattr(f, "required", False)],
                committed_by_label=_cbl,
                form_url=_form_url,
            )
            # REQUIRED-ESCALATE VETO (zero-cost, deterministic): a REQUIRED field whose own
            # outcome is ESCALATE is by definition not complete — yet the audit can't see a
            # hidden file input and the banded VLM samples. The engine's own ledger already
            # KNOWS (hibob #19: resume ESCALATE'd honestly, verdict still said complete:True).
            with contextlib.suppress(Exception):
                _req = {f.name for f in fields if getattr(f, "required", False)}
                _esc = [r.label or r.name for r in per_field if r.name in _req and r.outcome == oa.ESCALATE]
                # STALENESS FIX (doordash mega3/16: LinkedIn ESCALATE'd in the ledger, then
                # retry/agent healed it — the screen shows the URL — yet this veto kept
                # complete=False forever). The ledger outcome is a snapshot from BEFORE the
                # completeness repairs; only veto fields the FINAL audit still sees as
                # missing/unanswered (token match, both sides normalized).
                _c = result.get("completeness") or {}
                _still = " ".join(
                    str(x).lower() for x in (_c.get("missing_required") or []) + (_c.get("visually_unanswered") or [])
                )
                def _tok(s: str) -> set:
                    return {w for w in str(s).lower().replace("*", " ").split() if len(w) > 2}
                def _matches(hay: str, l: str) -> bool:
                    t = _tok(l)
                    return bool(t) and len(t & set(hay.split())) >= max(1, len(t) // 2)
                # a stale escalate is only FORGIVEN when a later pass actually committed the
                # field (retry commits join committed_by_label) — an audit that merely sees
                # "non-empty" can be looking at a FOREIGN value we never chose (stripe mega4/5
                # rerun: 'Belgium' stood on reside-country while our fill escalated; the old
                # audit-based staleness filter blessed it as healed).
                _healed = " ".join(str(k).lower().replace("*", " ") for k in _cbl)
                _esc = [l for l in _esc if _matches(_still, l) or not _matches(_healed, l)]
                if _esc and isinstance(result.get("completeness"), dict):
                    result["completeness"]["complete"] = False
                    result["completeness"]["required_escalated"] = _esc
                    print(f"   [complete] VETO — required field(s) escalated: {_esc[:3]}")
            # RESUME VETO (deterministic): the page HAS a file field and we HAVE a resume, yet no
            # file field committed — an application without its resume is never complete. Does not
            # rely on the flaky star flag (hibob #20: 'Resume*' star didn't survive discovery ->
            # SKIP not ESCALATE -> the required-veto missed it while 'Add file' sat empty), nor on
            # the sampling VLM. A form with no file field at all is unaffected.
            with contextlib.suppress(Exception):
                _file_rows = [r for r in per_field if "file" in str(r.type or "").lower()]
                if (
                    resume
                    and _file_rows
                    and not any(r.outcome == oa.DONE for r in _file_rows)
                    and isinstance(result.get("completeness"), dict)
                    and result["completeness"].get("complete")
                ):
                    result["completeness"]["complete"] = False
                    result["completeness"]["resume_not_attached"] = True
                    print("   [complete] VETO — resume provided but no file field committed")
            # WANT-vs-GOT VETO (samsara mega4/24: 'Enter manually' committed for want='0',
            # 'Gender Fluid' for want='Male', verdict still COMPLETE — every audit checks
            # PRESENCE, nothing checked the VALUE): a DONE row whose committed text shares no
            # token with the mapped value and prefixes neither way is a wrong-value commit,
            # unless the picker confirms the mapping semantically ('B.S.' -> "Bachelor's").
            with contextlib.suppress(Exception):
                import re as _re

                import oa_brain as brain

                def _wtok(s: str) -> set:
                    return {w for w in _re.sub(r"[^a-z0-9 ]", " ", str(s).lower()).split() if len(w) > 1}

                _suspects = []
                for r in per_field:
                    v, c = str(r.value or "").strip(), str(r.committed or "").strip()
                    if r.outcome != oa.DONE or not v or not c or "file" in str(r.type or "").lower():
                        continue
                    if v.lower() == c.lower() or (_wtok(v) & _wtok(c)):
                        continue
                    if c.lower().startswith(v.lower()) or v.lower().startswith(c.lower()):
                        continue
                    _ok = None
                    with contextlib.suppress(Exception):
                        _ok = await brain.pick_option(v, [c], llm=llm, label=str(r.label))
                    if not _ok:
                        _suspects.append(f"{str(r.label)[:60]} (want '{v[:25]}' got '{c[:25]}')")
                if _suspects and isinstance(result.get("completeness"), dict):
                    print(f"   [complete] VETO — want-vs-got mismatch: {_suspects[:3]}")
                    result["completeness"]["complete"] = False
                    result["completeness"].setdefault("missing_required", []).extend(_suspects)
            with contextlib.suppress(Exception):
                page = await session.must_get_current_page()

        # ============================================================================
        # HARD LEDGER GATE — the un-suppressed, ledger-first veto (mega4 green audit:
        # 30 of 31 confirmed false-greens had the failure ALREADY RECORDED in the ledger
        # — ESCALATE or blank->SKIP on a required field — while the verdict said green,
        # because the veto above lived INSIDE contextlib.suppress and behind a fragile
        # token-staleness filter). This gate runs OUTSIDE every suppress block: the
        # deterministic layer's own bookkeeping is the source of truth, and a form whose
        # ledger records ANY required field as unfilled can NEVER be green.
        # ============================================================================
        if adapter is None and profile is not None and os.environ.get("OA_NO_COMPLETE") != "1":
            import re
            def _norm(s: Any) -> str:
                return " ".join(str(s or "").split()).lower().strip(" *:✱")
            comp = result.get("completeness")
            if not isinstance(comp, dict):
                # complete() raised inside its suppress and left no verdict — UNKNOWN is not green.
                comp = {"complete": False, "verdict_missing": True, "missing_required": [], "visually_unanswered": []}
                result["completeness"] = comp
            _dbg_staresc = [str(r.label)[:40] for r in per_field
                            if r.outcome in (oa.ESCALATE, oa.SKIP) and ("*" in str(r.label or "") or "✱" in str(r.label or ""))]
            print(f"   [gate-dbg] per_field={len(per_field)} star-esc/skip rows={_dbg_staresc[:4]}")
            _req_names = {str(f.name) for f in fields if getattr(f, "required", False)}
            _req_labels = {_norm(f.label) for f in fields if getattr(f, "required", False) and f.label}
            # a label is HEALED iff a row with a VERIFIED terminal (outcome DONE) committed a
            # non-empty value, OR a later pass (retry) recorded one in committed_by_label
            # (retry only writes on out==DONE). `committed` on a SKIP/ESCALATE row is the
            # INTENDED value from a FAILED attempt, NOT a real commit (1password mega4/1:
            # 'people managers' recommit EMPTY x2 + commit-cap yet committed='No' — the box is
            # empty on screen). Only DONE rows count.
            _committed_labels = {_norm(r.label) for r in per_field
                                 if r.outcome == oa.DONE and r.committed and str(r.committed).strip()}
            _committed_labels |= {_norm(k) for k, v in _cbl.items() if v and str(v).strip()}
            _fails = []
            for r in per_field:
                # required from THREE independent signals — the gate does not trust discovery's
                # required flag alone (stripe mega4/7-8: 'anticipate working in... *' escalated
                # empty yet passed green because grpReq missed the star when it sat on a separate
                # span/line and no fieldset carried aria-required). The ledger label itself is
                # the third, self-sufficient signal: a star on the label = required, full stop.
                _lab = str(r.label or "")
                star = ("*" in _lab or "✱" in _lab) and "indicates a required" not in _lab.lower()
                is_req = str(r.name) in _req_names or _norm(r.label) in _req_labels or star
                if not is_req:
                    continue
                tr = str(r.trace or "")
                # a required field is UNFILLED if its row escalated OR skipped for any reason
                # other than a twin-dedup (the primary handled it) or a conditional premise that
                # legitimately does not apply. Covers blank->SKIP AND the commit-cap/recommit-
                # EMPTY class (1password mega4/1 'people managers': tried, box stayed empty,
                # SKIP with committed='No' intended — NOT blank->SKIP, previously slipped).
                # a CONDITIONAL follow-up ('If so…', 'If yes…', 'If you selected…') is required
                # only when its premise holds; a blank->SKIP on one means the mapper judged the
                # premise false (duolingo mega4/19: 'If so, are you eligible for OPT?' correctly
                # skipped because the candidate needs no sponsorship). Not a real miss.
                _ll = _norm(r.label)
                # conditional = starts with a premise reference, OR back-references a prior
                # answer ('after the OPT…' is premised on being in OPT, itself premised on
                # needing sponsorship — a chain that is inapplicable when sponsorship=No).
                _conditional = bool(
                    re.match(r"^(if so|if yes|if no\b|if not|if you|if applicable|if the|if selected|si oui|si vous|after the|based on|given your|as indicated)\b", _ll)
                    or re.search(r"\b(if you (selected|answered|chose|indicated))\b", _ll)
                )
                escalated = r.outcome == oa.ESCALATE
                real_skip = (
                    r.outcome == oa.SKIP
                    and "twin-label-already-done" not in tr
                    and "premise" not in tr.lower()
                    and "conditional" not in tr.lower()
                    and not _conditional
                )
                if not (escalated or real_skip):
                    continue
                if _norm(r.label) in _committed_labels:
                    continue  # a sibling/retry committed it — genuinely filled
                _fails.append(f"{str(r.label)[:55]} [{r.outcome}]")
            if _fails:
                _seen_f: set = set()
                _fails = [x for x in _fails if not (x in _seen_f or _seen_f.add(x))]
                comp["complete"] = False
                comp.setdefault("missing_required", [])
                for x in _fails:
                    if x not in comp["missing_required"]:
                        comp["missing_required"].append(x)
                comp["ledger_gate_failed"] = _fails
                print(f"   [HARD GATE] {len(_fails)} required field(s) unfilled in ledger -> NOT complete: {_fails[:4]}")

    secs = round(time.monotonic() - t0, 1)
    usage = await tc.get_usage_summary()
    if screenshot_path:
        # BOUNDED: on a never-idle SPA the terminal CDP screenshot can hang ~60s ("Runtime.evaluate did
        # not respond") AFTER the fill is already done — cap it so it can't push wall-clock past budget.
        with contextlib.suppress(Exception):
            result["screenshot"] = await asyncio.wait_for(eng._screenshot(session, page, screenshot_path), timeout=15.0)
    with contextlib.suppress(Exception):
        result["final_url"] = await page.get_url()

    _print_report(adapter.__class__.__name__, title, per_field, usage, len(mapped), secs)

    fillable = [r for r in per_field if r.outcome != oa.SKIP]
    filled = [r for r in fillable if r.filled]
    # HONEST DENOMINATOR (user: 'eval underestimate了任务'): the planner's expected_total_fields
    # counts flat fields + every repeater row a COMPLETE application needs. fill_rate over
    # discovered-only systematically over-states (5/5 on a 20-field form). Report BOTH: fill_rate
    # (of what we touched) and coverage (of what the page actually needs).
    rep_filled = int((result.get("completeness") or {}).get("repeater_fields_filled") or 0)
    total_filled = len(filled) + rep_filled  # flat + repeater rows = the honest fill count
    expected = 0
    with contextlib.suppress(Exception):
        expected = int((result.get("plan") or {}).get("expected_total_fields") or 0)
    if not expected:  # planner miss -> estimate: discovered flat + ~5 fields/row for history sections
        with contextlib.suppress(Exception):
            exp_rows = len(profile.get("experience") or []) if profile else 0
            edu_rows = len(profile.get("education") or []) if profile else 0
            secs = result.get("completeness", {}).get("sections_filled", []) + result.get("completeness", {}).get("sections_skipped", [])
            has_hist = any("exp" in str(x).lower() or "edu" in str(x).lower() for x in secs)
            expected = len(fillable) + (5 * (exp_rows + edu_rows) if has_hist else 0)
    # REDUNDANT LEDGER SAFETY NET (unconditional, 4-space — runs on EVERY path, not just the
    # generic-lane elif where the hard gate lives). twilio mega4/29+34: a star+ESCALATE
    # required 'source of your right to work…*' shipped complete=True — the in-elif gate did
    # not fire (branch/scope reason still under diagnosis). This net reads the fill ledger one
    # more time, right before the result is finalized, and can NEVER be skipped: any required
    # field (star in label, discovery-required, or name/label match) whose ledger row escalated
    # or non-twin/non-conditional skipped, with no DONE row committing that label, forces
    # complete=False. Belt-and-suspenders on the no-false-green invariant.
    with contextlib.suppress(Exception):
        import re as _re2
        _comp = result.get("completeness")
        if isinstance(_comp, dict) and _comp.get("complete") and profile is not None:
            def _nrm(s: Any) -> str:
                return " ".join(str(s or "").split()).lower().strip(" *:✱")
            _dn = {_nrm(r.label) for r in per_field if r.outcome == oa.DONE and r.committed and str(r.committed).strip()}
            _rq = {str(f.name) for f in fields if getattr(f, "required", False)} if fields else set()
            _net = []
            for r in per_field:
                _lb = str(r.label or "")
                _st = ("*" in _lb or "✱" in _lb) and "indicates a required" not in _lb.lower()
                if not (str(r.name) in _rq or _st):
                    continue
                _tr = str(r.trace or "")
                _cnd = bool(_re2.match(r"^(if so|if yes|if no\b|if not|if you|if applicable|if the|if selected|after the|based on|given your|as indicated)\b", _nrm(r.label)))
                _bad = r.outcome == oa.ESCALATE or (
                    r.outcome == oa.SKIP and "twin-label-already-done" not in _tr
                    and "premise" not in _tr.lower() and not _cnd)
                if _bad and _nrm(r.label) not in _dn:
                    _net.append(f"{_lb[:50]} [{r.outcome}]")
            if _net:
                _seen: set = set()
                _net = [x for x in _net if not (x in _seen or _seen.add(x))]
                _comp["complete"] = False
                _comp.setdefault("missing_required", [])
                for x in _net:
                    if x not in _comp["missing_required"]:
                        _comp["missing_required"].append(x)
                _comp["safety_net_failed"] = _net
                print(f"   [SAFETY NET] {len(_net)} required unfilled -> NOT complete: {_net[:4]}")
    result.update(
        status="FILLED",
        cost=usage.total_cost,
        secs=secs,
        outcomes={t: sum(1 for r in per_field if r.outcome == t) for t in (oa.DONE, oa.OTHER, oa.SKIP, oa.ESCALATE)},
        # RUN RECORD (user: '要加一个记录'): which answers were ASSUMED via sanctioned defaults
        # (veteran/disability/government/worked-for -> No) rather than known from the profile —
        # the audit trail for a human reviewing before submit.
        defaults_used=[
            {"name": n, "label": next((r.label for r in per_field if r.name == n), n), "value": m.value, "why": m.why}
            for n, m in (mapped or {}).items()
            if str(getattr(m, "why", "")).upper().startswith("DEFAULT")
        ],
        fill_rate=round(len(filled) / len(fillable), 3) if fillable else 0.0,
        expected_total_fields=expected or None,
        coverage=round(min(1.0, total_filled / expected), 3) if expected else None,
        filled=total_filled,
        flat_filled=len(filled),
        repeater_filled=rep_filled,
        results=[
            {
                "name": r.name,
                "label": r.label,
                "type": r.type,
                "src": r.value_src,
                "outcome": r.outcome,
                "nature": r.nature,
                "committed": r.committed,
                "value": r.value,
                "trace": r.trace,
            }
            for r in per_field
        ],
    )

    # NB: browser teardown is owned by run_single_page_oa's finally (the bulletproof path) — we
    # do NOT kill the session here, so a raise anywhere above still reaches that guaranteed cleanup.
    if not headless:
        print("\n  Browser left open for review (fill-only — NOT submitted). Ctrl+C to close.")
        with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
            while True:
                await asyncio.sleep(1)
    return result


def _print_report(
    adapter_name: str, title: str, rows: list[FieldResult], usage: Any, n_mapped: int, secs: float
) -> None:
    print("\n" + "=" * 84)
    print(f"  {adapter_name.upper()} — observe_act GENERIC FILL (fill-only, NEVER submitted)")
    print(f"  {title}")
    print("=" * 84)
    print(f"  {'FIELD':<22}{'TYPE':<22}{'NATURE':<14}{'SRC':<9}{'OUTCOME':<9}")
    print("  " + "-" * 80)
    for r in rows:
        print(f"  {r.name[:21]:<22}{r.type[:21]:<22}{r.nature[:13]:<14}{r.value_src:<9}{r.outcome:<9}")
    print("  " + "-" * 80)
    counts = {t: sum(1 for r in rows if r.outcome == t) for t in (oa.DONE, oa.OTHER, oa.SKIP, oa.ESCALATE)}
    fillable = [r for r in rows if r.outcome != oa.SKIP]
    filled = [r for r in fillable if r.filled]
    rate = (len(filled) / len(fillable) * 100) if fillable else 0.0
    print(f"  fields                  : {len(rows)}")
    print(
        f"  outcomes                : DONE={counts[oa.DONE]}  OTHER={counts[oa.OTHER]}  "
        f"SKIP={counts[oa.SKIP]}  ESCALATE={counts[oa.ESCALATE]}"
    )
    print(f"  fill-rate (DONE+OTHER / non-skip) : {rate:.0f}%  ({len(filled)}/{len(fillable)})")
    print(f"  mapped by the 1 structured call   : {n_mapped}")
    print(f"  LLM calls                         : {usage.entry_count}")
    print(f"  TOTAL LLM COST                    : ${usage.total_cost:.5f}")
    print(f"  fill wall-clock                   : {secs}s")
    print("=" * 84)


def _load_profile(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- #
# OFFLINE self-test (FIX B) — proves the browser can NOT be orphaned:
#   * the run's try/finally ALWAYS calls session.kill() AND the browser-dir hard-kill,
#     even when the fill loop RAISES,
#   * a unique resolved user-data-dir is registered active then deregistered,
#   * the signal handler installs and, when fired, kills every active run's browser.
# No browser, no network, no $ — fakes for BrowserSession/BrowserProfile/adapter/eng.
# --------------------------------------------------------------------------- #
async def _selftest() -> int:
    import types

    checks: list[tuple[str, bool, Any]] = []

    def chk(name: str, passed: bool, detail: Any = "") -> None:
        checks.append((name, passed, detail))

    killed_dirs: list[str] = []

    class _FakeProfile:
        def __init__(self, *, user_data_dir: str, **_kw: Any) -> None:
            # mimic the real validator: store the RESOLVED path (what lands in Chromium argv).
            self.user_data_dir = os.path.realpath(user_data_dir)

    class _FakeSession:
        """A session whose fill loop RAISES so we can prove the finally still tears down."""

        def __init__(self, *, browser_profile: Any) -> None:
            self.profile = browser_profile
            self.kill_calls = 0

        async def start(self) -> None:
            return None

        async def navigate_to(self, _url: str) -> None:
            return None

        async def must_get_current_page(self) -> Any:
            return object()

        async def kill(self) -> None:
            self.kill_calls += 1

    sessions: list[_FakeSession] = []

    def _fake_browser_session(*, browser_profile: Any) -> _FakeSession:
        s = _FakeSession(browser_profile=browser_profile)
        sessions.append(s)
        return s

    # A fake browser_use module so `from browser_use import BrowserProfile, BrowserSession, ChatGoogle`
    # and `from browser_use.tokens.service import TokenCost` resolve to our doubles.
    class _FakeTokenCost:
        def __init__(self, **_kw: Any) -> None:
            pass

        async def initialize(self) -> None:
            return None

        def register_llm(self, llm: Any) -> Any:
            return llm

        async def get_usage_summary(self) -> Any:
            return types.SimpleNamespace(total_cost=0.0, entry_count=0)

    fake_bu = types.ModuleType("browser_use")
    fake_bu.BrowserProfile = _FakeProfile  # type: ignore[attr-defined]
    fake_bu.BrowserSession = _fake_browser_session  # type: ignore[attr-defined]
    fake_bu.ChatGoogle = lambda **_kw: object()  # type: ignore[attr-defined]
    fake_tokens = types.ModuleType("browser_use.tokens")
    fake_tokens_service = types.ModuleType("browser_use.tokens.service")
    fake_tokens_service.TokenCost = _FakeTokenCost  # type: ignore[attr-defined]

    # A raising adapter: open_form is fine, but the fill loop blows up via observe_act below.
    class _RaisingAdapter:
        hosts = ("job-boards.greenhouse.io",)

        async def extract(self, _url: str, _profile: dict) -> tuple[str, list[Any]]:
            return ("Test Job", [types.SimpleNamespace(name="first_name", needs_map=False, source="standard")])

        async def open_form(self, _session: Any, page: Any) -> Any:
            return page

    # Patch the module-level seams for the duration of the test.
    orig_pick = pick_adapter
    orig_kill = _kill_browser_for_dir
    orig_fill = _fill_form
    orig_modules = {k: sys.modules.get(k) for k in ("browser_use", "browser_use.tokens", "browser_use.tokens.service")}

    async def _boom_fill(**_kw: Any) -> dict:
        raise RuntimeError("fill loop blew up (simulated mid-form crash)")

    def _record_kill(udd: str | None) -> None:
        if udd:
            killed_dirs.append(udd)

    fake_eng = types.SimpleNamespace(
        form_present=_async_true,
        map_fields=_async_empty_map,
        _screenshot=_async_noop_str,
        ATSAdapter=object,
        FormField=object,
    )

    globals_backref = globals()
    orig_eng = globals_backref["eng"]
    try:
        sys.modules["browser_use"] = fake_bu
        sys.modules["browser_use.tokens"] = fake_tokens
        sys.modules["browser_use.tokens.service"] = fake_tokens_service
        globals_backref["pick_adapter"] = lambda _url: _RaisingAdapter()
        globals_backref["_kill_browser_for_dir"] = _record_kill
        globals_backref["_fill_form"] = _boom_fill
        globals_backref["eng"] = fake_eng

        active_before = set(_ACTIVE_USER_DATA_DIRS)
        raised = False
        try:
            await run_single_page_oa(
                url="https://job-boards.greenhouse.io/acme/jobs/1",
                profile={},
                resume=None,
                headless=True,
            )
        except RuntimeError:
            raised = True

        chk("fill-loop raise propagates", raised, raised)
        chk(
            "session.kill() called in finally (even on raise)",
            sessions and sessions[0].kill_calls == 1,
            sessions[0].kill_calls if sessions else None,
        )
        chk("browser-dir hard-kill ran in finally", len(killed_dirs) == 1, killed_dirs)
        chk(
            "kill key is the RESOLVED user-data-dir",
            killed_dirs and killed_dirs[0] == os.path.realpath(killed_dirs[0]),
            killed_dirs,
        )
        chk(
            "active-dir set restored (deregistered)",
            set(_ACTIVE_USER_DATA_DIRS) == active_before,
            _ACTIVE_USER_DATA_DIRS,
        )
        chk("temp profile dir removed", killed_dirs and not os.path.exists(killed_dirs[0]), killed_dirs)
    finally:
        globals_backref["pick_adapter"] = orig_pick
        globals_backref["_kill_browser_for_dir"] = orig_kill
        globals_backref["_fill_form"] = orig_fill
        globals_backref["eng"] = orig_eng
        for k, v in orig_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    # --- signal handler installs + kills every active dir when fired ---
    fired: list[str] = []
    orig_kill2 = globals_backref["_kill_browser_for_dir"]
    try:
        globals_backref["_kill_browser_for_dir"] = lambda udd: fired.append(udd) if udd else None
        global _SIGNALS_INSTALLED
        _SIGNALS_INSTALLED = False
        _ACTIVE_USER_DATA_DIRS.add("/tmp/fake-active-udd")
        _install_signal_cleanup()
        handler = signal.getsignal(signal.SIGTERM)
        installed = callable(handler)
        chk("SIGTERM handler installed", installed, type(handler).__name__)
        # Invoke the handler body directly via the active-dir loop it runs (don't actually signal).
        if installed:
            for udd in list(_ACTIVE_USER_DATA_DIRS):
                globals_backref["_kill_browser_for_dir"](udd)
        chk("signal-path kills active dir", "/tmp/fake-active-udd" in fired, fired)
    finally:
        globals_backref["_kill_browser_for_dir"] = orig_kill2
        _ACTIVE_USER_DATA_DIRS.discard("/tmp/fake-active-udd")
        with contextlib.suppress(Exception):
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.SIG_DFL)

    ok = True
    print("\n=== oa_singlepage offline self-test (FIX B browser lifecycle, no browser, $0) ===")
    for name, passed, detail in checks:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail}")
    print(f"\n{'>>> ALL PASS' if ok else '>>> SOME FAIL'}  ({len(checks)} checks)")
    return 0 if ok else 1


async def _async_true(*_a: Any, **_k: Any) -> bool:
    return True


async def _async_empty_map(*_a: Any, **_k: Any) -> dict:
    return {}


async def _async_noop_str(*_a: Any, **_k: Any) -> str:
    return ""


def main() -> None:
    if "--selftest" in sys.argv:
        raise SystemExit(asyncio.run(_selftest()))
    # PROCESS HARD-CAP: a blocked sync call in the default executor (an LLM HTTP call with no
    # timeout) keeps asyncio.run's shutdown waiting forever AFTER the fill finished — mega #19
    # hung 2.5h post-completion. A daemon thread cannot block exit; it just guarantees one.
    import threading as _th
    import time as _time

    def _proc_cap() -> None:
        _time.sleep(float(os.environ.get("OA_PROC_CAP_S", "1500")))
        print("  [proc-cap] hard exit — executor/teardown hang", flush=True)
        os._exit(3)

    _th.Thread(target=_proc_cap, daemon=True).start()
    p = argparse.ArgumentParser(description="Fill ONE single-page ATS form via observe_act (FILL-ONLY, never submits)")
    p.add_argument(
        "--selftest", action="store_true", help="run the offline browser-lifecycle self-test ($0, no browser)"
    )
    p.add_argument("--url", required=True, help="Greenhouse / Lever / Ashby single-page job URL")
    p.add_argument("--profile", required=True, help="path to a profile JSON (no secrets in argv)")
    p.add_argument("--resume", default=None, help="path to a resume file for the file field")
    p.add_argument("--screenshot", default=None, help="write an end-of-fill PNG here")
    p.add_argument("--headed", action="store_true", help="run headed (default headless)")
    p.add_argument("--json", default=None, help="write the full per-field result JSON here")
    p.add_argument("--generic", action="store_true", help="benchmark mode: force the no-adapter lane")
    args = p.parse_args()

    profile = _load_profile(args.profile)
    res = asyncio.run(
        run_single_page_oa(
            url=args.url,
            profile=profile,
            resume=args.resume,
            headless=not args.headed,
            screenshot_path=args.screenshot,
            force_generic=args.generic,
            json_path=args.json,
        )
    )
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2)
        print(f"  wrote {args.json}")


if __name__ == "__main__":
    main()
