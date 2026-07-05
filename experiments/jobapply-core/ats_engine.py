"""Generic, ATS-agnostic application-filler engine.

The invariant pipeline lives here; everything platform-specific is a pluggable
ATSAdapter. The split (第一性原理):

  INVARIANT (this module, shared across every ATS)
    - MAP        : ONE structured LLM call mapping profile -> field values by LABEL.
    - LADDER     : per-field L1 fill -> L2 re-try -> L3 single-field browser-use Agent.
    - VERIFY     : read-back compare (delegated primitive per adapter).
    - INSTRUMENT : per-field tier + running $; measures the real escalation rate.

  VARIANT (each ATSAdapter)
    - extract(url)        : produce the normalized [Field] list (schema-API or DOM-scrape).
    - locate / fill / read_back : drive that platform's widgets & locators.
    - reveal (optional)   : pre-fill DOM toggles (e.g. "Enter manually").

Cost model:  total ≈ 1 map call + (escalation_rate x per-field agent cost).
On a clean schema-driven ATS escalation -> 0 and total -> the map call (~$0.0015).
The instrument step is the feedback loop: it shows which widget/adapter bleeds $ so
you add a deterministic routine THERE and drive escalation back to zero.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import os
import re
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

import failcap  # failure capture + triage (leaf module; heavy deps lazy-imported inside)
from pydantic import BaseModel, Field

# Field sources whose value is produced by the ONE structured LLM mapping call.
# `standard` already carries a deterministic profile value; `file` is an upload;
# `skip` is dropped. Everything else (select / input_text / open_ended) is mapped.
MAP_SOURCES = {"select", "input_text", "open_ended"}


# ---------------------------------------------------------------------------
# Normalized field descriptor — every adapter's extract() yields these.
# ---------------------------------------------------------------------------
@dataclass
class FormField:
    name: str  # stable id used to locate the element on the page
    label: str  # human label (what MAP reasons over)
    type: str  # adapter-native type tag (text/textarea/file/single_select/…)
    source: str  # standard | select | input_text | open_ended | file | skip
    required: bool = False
    options: list[str] | None = None
    option_values: dict | None = None  # {option_label: option_value_id} — to check the right checkbox
    value: str | None = None  # deterministic value already known at extract (standard fields)

    @property
    def needs_map(self) -> bool:
        return self.source in MAP_SOURCES


# ---------------------------------------------------------------------------
# Wizard value types (multi-page adapters only).
# ---------------------------------------------------------------------------
@dataclass
class Credentials:
    email: str
    password: str  # never via CLI args — env / secret bootstrap (see project memory)
    existing: bool = False  # True = a previously-created account -> SIGN IN (reuse), never re-create


@dataclass
class AuthResult:
    ok: bool
    needs_verification: bool = False  # emailed link/code follows -> HITL halt
    reason: str = ""


@dataclass
class Step:
    index: int  # 1-based active step (from the progress bar)
    total: int  # total steps M
    name: str  # e.g. "My Information"
    fields: list[FormField]
    is_review: bool  # name == 'Review' or index == total -> STOP, never submit


@dataclass
class AdvanceResult:
    ok: bool
    page: Any = None
    blocked_reason: str = ""


# ---------------------------------------------------------------------------
# Adapter contract.
# ---------------------------------------------------------------------------
class ATSAdapter(abc.ABC):
    hosts: tuple[str, ...] = ()  # url hostnames this adapter claims
    multi_page: bool = False  # single-page (Greenhouse/Lever/Ashby) leave False
    advance_label: str = "Save and Continue"  # the step-advance button (agent repair clicks it)

    @abc.abstractmethod
    async def extract(self, url: str, profile: dict) -> tuple[str, list[FormField]]:
        """Return (job_title, fields) WITHOUT a browser where possible (schema API),
        else by classifying the live DOM. For wizards, returns (title, []) — fields come
        per-step from extract_step()."""

    async def open_form(self, session: Any, page: Any) -> Any:
        """Reach the actual form after the initial navigation, returning the page the form
        lives on. Default: the page unchanged. Override to drill into an iframe-embedded
        form, dismiss a wall, click "Apply", or (for wizards) create an account."""
        return page

    @abc.abstractmethod
    async def locate(self, page: Any, field: FormField) -> Any | None:
        """Return the live element for this field (or None). Used by the engine's
        form-present pre-flight and by fill/read_back."""

    @abc.abstractmethod
    async def fill(self, session: Any, page: Any, field: FormField, value: str, resume: str | None) -> bool:
        """L1/L2 fill mechanism for this field type. Return whether the mechanism ran."""

    @abc.abstractmethod
    async def read_back(self, session: Any, page: Any, field: FormField, value: str) -> bool:
        """Read the value back off the live DOM and confirm it took."""

    # --- wizard-only hooks (multi_page=True). Safe defaults keep single-page untouched. ---
    async def authenticate(self, session: Any, page: Any, creds: Credentials | None) -> AuthResult:
        """Create/sign into the account that gates the wizard. Single-page: no-op."""
        return AuthResult(ok=True)

    async def extract_step(self, session: Any, page: Any, profile: dict) -> Step:
        """Classify the CURRENTLY-MOUNTED wizard step's live DOM into a Step."""
        raise NotImplementedError

    async def next_step(self, session: Any, page: Any) -> AdvanceResult:
        """Click this step's advance control and wait for the next step to mount."""
        raise NotImplementedError

    async def is_complete(self, session: Any, page: Any) -> bool:
        """At-Review / terminal detection. HARD STOP — never click Submit. Single-page: True."""
        return True

    async def validation_errors(self, page: Any) -> list[str]:
        """Step-level validation messages currently blocking advance (e.g. 'Enter a valid format
        for Phone Number'). Used to trigger generic agent-mode repair when next_step fails. The
        messages should name the offending field(s). Default: none."""
        return []

    async def upload_resume(self, session: Any, page: Any, resume: str | None) -> bool:
        """Optional: DETERMINISTIC-ONLY resume upload for wizards. Runs fresh at the top of every step,
        scans the live DOM for the file input, pushes bytes via CDP, and is idempotent (skips if already
        uploaded). NEVER escalates to an agent. Single-page adapters keep the file field on the per-field
        ladder, so this default is a no-op for them (Greenhouse/Lever/Ashby/jobapply untouched)."""
        return False

    async def fill_repeaters(self, session: Any, page: Any, profile: dict, allow_escalation: bool = True) -> dict:
        """Optional: fill 'Add another' repeater sections (education / work experience) that
        are NOT in the flat field schema — they exist only in the live DOM and need an
        add-row loop, not the per-field map. Default: none. Kept structurally separate from
        the flat FormField fill so it never perturbs form_present / read_back.

        allow_escalation MUST be honored by any override that spawns an agent — the kalepa
        leak (GH education agent running under escalate=False, overrunning the wall clock,
        poisoning the next session) is exactly this gate being skipped."""
        return {}


async def form_present(adapter: ATSAdapter, page: Any, fields: list[FormField]) -> bool:
    """Pre-flight: is the form actually on this page? Guards the expensive L1->L2->L3
    ladder from running on a redirect / WAF wall / login page / wrong host (where every
    field would 'escalate' and burn agent $). True if any of the first few real fields
    can be located."""
    probes = [f for f in fields if f.source not in ("skip",)][:4]
    for f in probes:
        if await adapter.locate(page, f) is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# Generic DOM utilities adapters may reuse.
# ---------------------------------------------------------------------------
async def first(page: Any, selector: str) -> Any | None:
    try:
        els = await page.get_elements_by_css_selector(selector)
        return els[0] if els else None
    except Exception:
        return None


def norm(s: str) -> str:
    return "".join((s or "").split()).lower()


async def click_by_text(page: Any, text: str) -> int:
    """Click every short button/link whose visible text matches (CSS has no :has-text)."""
    want = text.strip().lower()
    clicked = 0
    for b in await page.get_elements_by_css_selector('button, [role="button"]'):
        try:
            t = ((await b.evaluate("() => this.textContent")) or "").strip()
        except Exception:
            continue
        if t and len(t) < 30 and want in t.lower():
            try:
                await b.click()
                clicked += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass
    return clicked


async def upload_file(session: Any, page: Any, file_el: Any, path: str) -> bool:
    """File upload via CDP DOM.setFileInputFiles (no high-level wrapper exists)."""
    bnid = getattr(file_el, "_backend_node_id", None) or getattr(file_el, "backend_node_id", None)
    if not bnid:
        return False
    sid = (
        getattr(file_el, "_session_id", None)
        or getattr(file_el, "session_id", None)
        or getattr(page, "session_id", None)
    )
    if hasattr(sid, "__await__"):  # page.session_id is a COROUTINE — the prior code passed it
        sid = await sid  # un-awaited, so CDP got a coroutine as session_id and failed
    try:
        await session.cdp_client.send.DOM.setFileInputFiles(
            params={"files": [str(Path(path).resolve())], "backendNodeId": bnid},
            session_id=sid,
        )
        # Workday's React autofill/validation listens for a `change` event; a raw CDP setFileInputFiles
        # does NOT reliably fire it, so the file attaches but the résumé-PARSE never triggers (the legacy
        # DomHand used Playwright setInputFiles, which fires input+change). Dispatch them explicitly.
        with contextlib.suppress(Exception):
            await file_el.evaluate(
                "() => { this.dispatchEvent(new Event('input', {bubbles:true}));"
                " this.dispatchEvent(new Event('change', {bubbles:true})); }"
            )
        return True
    except Exception as exc:
        print(f"   [upload] CDP setFileInputFiles failed: {exc}")
        return False


async def press_key_trusted(session: Any, page: Any, *, key: str, code: str, vk: int) -> bool:
    """A TRUSTED key via CDP Input.dispatchKeyEvent on the focused element. react-select (and Workday's
    typeahead) IGNORE synthetic page.press / JS-dispatched keys — only a real CDP key navigates the
    suggestion list / commits. Caller must have focused/typed first."""
    try:
        sid = await page.session_id
        for kind in ("rawKeyDown", "keyUp"):
            await session.cdp_client.send.Input.dispatchKeyEvent(
                params={
                    "type": kind,
                    "windowsVirtualKeyCode": vk,
                    "nativeVirtualKeyCode": vk,
                    "code": code,
                    "key": key,
                },
                session_id=sid,
            )
        return True
    except Exception as exc:
        print(f"   [trusted-key:{key}] {exc}")
        return False


async def type_text_trusted(session: Any, page: Any, text: str) -> bool:
    """TRUSTED character typing via CDP keyDown(text)+keyUp per char — for widgets that ignore
    synthetic .fill()/JS keys (Workday's segmented date spinbuttons REDISTRIBUTE a programmatic
    fill across segments via their auto-advance; real keystrokes are segmented correctly by the
    widget itself). Caller must have focused the first segment."""
    try:
        sid = await page.session_id
        for ch in text:
            vk = ord(ch.upper()) if ch.isalnum() else 0
            for kind in ("keyDown", "keyUp"):
                params = {"type": kind, "key": ch, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
                if kind == "keyDown":
                    params["text"] = ch
                await session.cdp_client.send.Input.dispatchKeyEvent(params=params, session_id=sid)
            await asyncio.sleep(0.05)
        return True
    except Exception:
        return False


async def press_enter_trusted(session: Any, page: Any) -> bool:
    """TRUSTED Enter — commits the highlighted option (Caller must have focused/typed first)."""
    return await press_key_trusted(session, page, key="Enter", code="Enter", vk=13)


async def arrow_down_trusted(session: Any, page: Any) -> bool:
    """TRUSTED ArrowDown — HIGHLIGHTS the first suggestion in a typeahead menu that does NOT auto-
    highlight (Workday's School / Field-of-Study / Skills multiselect). Without it a following Enter
    commits NOTHING (the verified chip-residual bug). ArrowDown then Enter lands the pill."""
    return await press_key_trusted(session, page, key="ArrowDown", code="ArrowDown", vk=40)


async def click_trusted(session: Any, page: Any, element: Any) -> bool:
    """A TRUSTED mouse click via CDP Input.dispatchMouseEvent at the element's on-screen center. A
    React-controlled radio/checkbox IGNORES a synthetic .click()/label-click for its onChange state
    (the verified Workday failure) — only a REAL pointer event flips it. Scrolls the element into view,
    reads its viewport-center coords, dispatches mousePressed+mouseReleased there. Returns False if the
    element has no box (detached/zero-size)."""
    try:
        sid = await page.session_id
        box = await element.evaluate(
            "() => { const el=this; el.scrollIntoView({block:'center',inline:'center'});"
            " const r=el.getBoundingClientRect();"
            " return (r.width&&r.height) ? JSON.stringify({x:r.left+r.width/2, y:r.top+r.height/2}) : ''; }"
        )
        if isinstance(box, str):
            box = json.loads(box) if box else None
        if not isinstance(box, dict):
            return False
        x, y = box["x"], box["y"]
        for ev in (
            {"type": "mouseMoved", "x": x, "y": y, "buttons": 0},
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1},
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1},
        ):
            await session.cdp_client.send.Input.dispatchMouseEvent(params=ev, session_id=sid)
        return True
    except Exception as exc:
        print(f"   [trusted-click] {exc}")
        return False


async def _vlm_apply_text(session: Any) -> str:
    """ONE nano-VLM read: the exact visible text of the start-application button. Language-
    agnostic (SmartRecruiters "I'm interested", pt-BR "Estou interessado", ...). '' on miss."""
    try:
        # The start-application affordance is usually a header tab/button ('Application' tab,
        # SR 'I'm interested') OR below a long JD ('Apply for this Job'). A full-page shot of a
        # long page downscales the button below readability (the hibob miss). So read the TOP
        # viewport FIRST (crisp, catches header/sticky affordances), then full-page as fallback.
        import base64 as _b64d

        import oa_llm as _oa
        from vision_verify import _vlm

        from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam, ImageURL, UserMessage

        async def _shot(full: bool):
            with contextlib.suppress(Exception):
                sh = await asyncio.wait_for(session.take_screenshot(full_page=full), timeout=15.0)
                return _b64d.b64decode(sh) if isinstance(sh, str) else sh
            return None

        async def _ask(png_bytes):
            if png_bytes is None:
                return ""
            msg = UserMessage(content=[
                ContentPartTextParam(type="text", text=(
                    "This is a job posting page. Reply ONLY the EXACT visible text of the button, "
                    "tab, or link a candidate clicks to START/OPEN the application (any language, "
                    "e.g. 'Apply now', 'Application', \"I'm interested\"). If none, reply NONE.")),
                ContentPartImageParam(type="image_url", image_url=ImageURL(
                    url=f"data:image/png;base64,{_b64d.b64encode(png_bytes).decode()}",
                    detail="low", media_type="image/png"))])
            res = await _oa.resilient_vlm([msg], primary=_vlm())
            t = str(getattr(res, "completion", res) or "").strip().strip("\"'")
            return "" if (not t or t.upper() == "NONE" or len(t) > 40) else t

        with contextlib.suppress(Exception):  # scroll to top so the header affordance is in view
            page0 = await session.must_get_current_page()
            await page0.evaluate("() => window.scrollTo(0,0)")
        want = await _ask(await _shot(False)) or await _ask(await _shot(True))
        return want
    except Exception as exc:
        print(f"   [reach] vlm affordance failed: {type(exc).__name__}: {exc}")
        return ""


async def _try_apply_click(session: Any, page: Any) -> bool:
    """REACH rung: redirect tenants often land on the job DESCRIPTION page with a visible
    start-application affordance. NO regex (standing rule): ONE nano-VLM read of the rendered
    page NAMES the button (any language/wording — 'Apply now', "I'm interested", 'Estou
    interessado'); the DOM lookup below only LOCATES that exact named text and clicks it
    trusted. Meaning lives in the VLM, never in a pattern list."""
    by_text_js = (
        "(w) => { const want=w.trim().toLowerCase().replace(/\\s+/g,' ');"
        " const tx=e=>((e.innerText||e.textContent||e.getAttribute('aria-label')||e.value||'')"
        "   .trim().toLowerCase().replace(/\\s+/g,' '));"
        " const els=[...document.querySelectorAll('a,button,[role=button],[role=link],input[type=submit]')]"
        " .filter(e=>{const r=e.getBoundingClientRect(); if(r.width<10||r.height<10) return false;"
        " const t=tx(e); return t && (t===want || (t.length<60 && (t.includes(want)||want.includes(t))));})"
        " .sort((a,b)=>tx(a).length-tx(b).length);"
        " const el=els[0]; if(!el) return '';"
        " el.scrollIntoView({block:'center'}); const r=el.getBoundingClientRect();"
        " let href=''; const a=el.closest('a'); try{ href=a&&a.href?a.href:''; }catch(e){}"
        " return JSON.stringify({x:r.left+r.width/2, y:r.top+r.height/2,"
        " t:(el.innerText||'').trim().slice(0,40), href}); }"
    )
    try:
        want = await _vlm_apply_text(session)
        if not want:
            return False
        print(f"   [reach] VLM names the affordance: '{want}'")
        raw = await page.evaluate(by_text_js, want)
        if not raw:
            print(f"   [reach] named affordance '{want}' not found in DOM")
            return False
        c = json.loads(raw)
        print(f"   [reach] clicking apply affordance: '{c['t']}'")
        sid = await page.session_id
        for ev in (
            {"type": "mouseMoved", "x": c["x"], "y": c["y"], "buttons": 0},
            {"type": "mousePressed", "x": c["x"], "y": c["y"], "button": "left", "buttons": 1, "clickCount": 1},
            {"type": "mouseReleased", "x": c["x"], "y": c["y"], "button": "left", "buttons": 0, "clickCount": 1},
        ):
            await session.cdp_client.send.Input.dispatchMouseEvent(params=ev, session_id=sid)
        await asyncio.sleep(2.5)  # navigation / SPA form mount
        # FOLLOW-HREF: a synthetic click on an SPA anchor (workable's <a href=.../apply/>) may not
        # fire its client-route. If the affordance carried an href and the page didn't move there,
        # navigate to it directly — the affordance's OWN destination, still no per-ATS URL pattern.
        href = c.get("href") or ""
        if href:
            with contextlib.suppress(Exception):
                cur = await page.get_url()
                if href.rstrip("/") not in (cur or "").rstrip("/"):
                    print(f"   [reach] following affordance href: {href[:80]}")
                    await session.navigate_to(href)
                    await asyncio.sleep(2.5)
        return True
    except Exception as exc:
        print(f"   [reach] apply click failed: {exc}")
        return False


async def hover_trusted(session: Any, page: Any, element: Any) -> bool:
    """Move the real cursor over an element so a hover-highlighting widget (Workday's typeahead
    suggestion menu) HIGHLIGHTS it — WITHOUT clicking. Pairs with press_enter_trusted: hover the
    matched option to highlight it, then Enter commits it. A trusted CLICK on a multi-pill suggestion
    leaves the menu OPEN (verified); hover+Enter commits AND closes it. Returns False if no box."""
    try:
        sid = await page.session_id
        box = await element.evaluate(
            "() => { const el=this; el.scrollIntoView({block:'center',inline:'center'});"
            " const r=el.getBoundingClientRect();"
            " return (r.width&&r.height) ? JSON.stringify({x:r.left+r.width/2, y:r.top+r.height/2}) : ''; }"
        )
        if isinstance(box, str):
            box = json.loads(box) if box else None
        if not isinstance(box, dict):
            return False
        await session.cdp_client.send.Input.dispatchMouseEvent(
            params={"type": "mouseMoved", "x": box["x"], "y": box["y"], "buttons": 0}, session_id=sid
        )
        return True
    except Exception as exc:
        print(f"   [trusted-hover] {exc}")
        return False


def _locate_idx(chosen: str, options: list[str]) -> int | None:
    """Index in `options` whose VISIBLE text equals `chosen` (the LLM's pick), normalized-exact ONLY.
    This is NOT option-matching — it merely locates the element the LLM already chose. No regex, no
    substring, no startswith: a value->option DECISION is the LLM's job (directive #3); this is the
    pure equality that turns the LLM's chosen string back into a clickable index. None if absent."""
    c = norm(chosen)
    if not c or not options:
        return None
    for i, opt in enumerate(options):
        if norm(opt) == c:
            return i
    return None


async def pick_dropdown(
    session: Any,
    page: Any,
    value: str,
    *,
    read_dom_options: Any,
    commit: Any | None = None,
    llm: Any = None,
    verify_label: str | None = None,
    vis_key: str | None = None,
) -> bool:
    """SHARED visual-dropdown-pick primitive. The caller has ALREADY opened the widget and typed the
    filter. This decides WHICH option matches and commits it, using VISION as the source of truth for
    the rendered options — the documented fix for the lagging/stale DOM option portal.

    Pipeline:
      1. READ options from the DOM (cheap) via the caller's `read_dom_options(page) -> [str]`.
      2. ALSO read the screen with vision_verify.read_options_visually(session) when the DOM came back
         empty (lagged/stale portal): one low-detail screenshot, VLM transcribes the ACTUALLY-rendered
         options (no DOM lag). Vision is the source of truth when present; DOM is the cheap supplement.
      3. MATCH is LLM-ONLY (directive #3): the cheap text LLM reads the REAL (vision-or-DOM) option
         strings and picks the single best one (it canonicalizes abbreviations, e.g. 'B.S.' -> the
         "Bachelor's Degree" option). NO regex / substring / startswith / equality MATCH heuristic —
         _locate_idx only turns the LLM's chosen string back into a clickable index by exact equality.
      4. COMMIT: `commit(idx, options)` if the caller gave one (e.g. click the matched node); else the
         default trusted CDP Enter on the widget's pre-highlighted top match (press_enter_trusted).
      5. VERIFY the committed VALUE with the cheap value-aware VLM (visual_check(want=value)); return
         True only when matches==true. No verify_label -> commit success is the answer (still no spin).

    Frozen/contradiction -> returns False fast (caller decides escalation). NEVER submits."""
    import contextlib

    from vision_verify import _matches, read_options_visually, visual_check

    want = norm(value)
    if not want:
        return True

    # 1. DOM read (cheap, may be stale/empty)
    dom_opts: list[str] = []
    with contextlib.suppress(Exception):
        dom_opts = [o for o in (await read_dom_options(page) or []) if o]

    # 2. when the DOM portal lagged (empty), read the actually-rendered options off the screen
    options = dom_opts
    used_vision = False
    if not dom_opts:
        vkey = vis_key or f"{verify_label or 'menu'}:{value}"
        vis_opts = await read_options_visually(session, key=vkey)
        if vis_opts:
            options = vis_opts
            used_vision = True
    if not options:
        # DIAGNOSTIC (was a silent False that made listbox failures undebuggable): neither the DOM
        # portal nor the screenshot shows options -> the menu simply isn't open/rendered.
        print(f"  [pick_dropdown] want={value!r} NO options (dom+vision both empty — menu not open?)", flush=True)
        return False

    # 3. MATCH is LLM-ONLY — no regex/substring; the LLM picks the single best option text, then
    #    _locate_idx resolves that exact string to an index (equality is just element-location).
    #    No llm passed -> default to the cheap flash-lite (vision_verify._vlm) so the match is ALWAYS
    #    an LLM decision (directive #3) — never a silent abort for lack of a caller-supplied llm.
    if llm is None:
        with contextlib.suppress(Exception):
            from vision_verify import _vlm

            llm = _vlm()
    idx = None
    if llm is not None:
        from wd_repeaters import _llm_pick

        choice = await _llm_pick(llm, value, options)
        if choice:
            idx = _locate_idx(choice, options)
    # The DOM portal can serve the WRONG list: a select WITHOUT aria-controls (PayPal's Degree /
    # language-proficiency widgets) falls back to the SHARED global portal, which returns another
    # widget's options (e.g. language NAMES 'Afrikaans/Albanian' for a proficiency select) -> no match.
    # When DOM gave no match, READ THE SCREEN with the VLM (it sees the actually-open widget, no portal
    # scoping) and re-match. Visuals are the source of truth.
    if idx is None and not used_vision:
        with contextlib.suppress(Exception):
            vis_opts = await read_options_visually(session, key=vis_key or f"{verify_label or 'menu'}:{value}")
            if vis_opts and llm is not None:
                from wd_repeaters import _llm_pick

                options = vis_opts
                used_vision = True
                choice = await _llm_pick(llm, value, options)
                if choice:
                    idx = _locate_idx(choice, options)
    print(
        f"  [pick_dropdown] want={value!r} src={'vlm' if used_vision else 'dom'} "
        f"opts={options[:5]} hit={idx is not None}",
        flush=True,
    )
    if idx is None:
        return False  # no LLM pick (or it chose NONE/absent) -> abort fast (no wrong-value commit)

    # 4. commit
    committed = False
    if commit is not None:
        with contextlib.suppress(Exception):
            committed = bool(await commit(idx, options))
    else:
        committed = await press_enter_trusted(session, page)
    if not committed:
        return False

    # 5. value-aware visual verify (the wrong-option catch). SKIP it when the match came from a fresh DOM
    # read (used_vision=False): the LLM matched an EXACT visible option + trusted-Enter committed it — a
    # reliable closed-list commit (Degree, State, language proficiency). The costly per-field VLM verify
    # is reserved for the src=vlm case (DOM lagged -> wrong-commit risk). ~3x faster on stable selects.
    if not verify_label or not used_vision:
        return True
    await asyncio.sleep(0.4)
    with contextlib.suppress(Exception):
        verdict = await visual_check(session, verify_label, want=value, use_cache=False)
        return _matches(verdict)
    return False


# ---------------------------------------------------------------------------
# Step 2 — the ONE structured LLM call (generic).
# ---------------------------------------------------------------------------
class FieldFill(BaseModel):
    name: str = Field(description="echo the field name verbatim")
    value: str = Field(description="value to type/select, or '' if the profile gives no basis")
    why: str = Field(default="", description="one short clause: which profile fact this came from")


class FillMap(BaseModel):
    fields: list[FieldFill]


_MAP_SYSTEM = """You map an applicant PROFILE onto a job-application FORM. You are given the \
job title and a list of fields; each field has a human LABEL, a TYPE, whether it is required, \
and (for dropdowns) the exact allowed OPTIONS.

For EVERY field, return an object {name, value, why}. Rules:
- Echo `name` exactly as given.
- Decide a field's meaning from its LABEL, never from its machine name.
- Use ONLY facts present in the profile. Never invent or assume facts not in the profile.
- If the profile gives no basis for the field, return value "" (empty string).
- PHONE fields: keep the FULL international form with its leading + (e.g. "+1 415 555 0142") — \
phone widgets parse the country from it; a stripped national number under the widget's default \
country flag is invalid and gets wiped.
- OPEN-ENDED prose questions ("Why do you want to join us?", "Tell us about…"): write 2-4 \
tailored sentences from the profile's experience. NEVER a bare "Yes"/"No" — a yes/no in a \
prose box is always wrong.
- SANCTIONED DEFAULTS when the profile does not disclose (prefix `why` with "DEFAULT: "): \
veteran status -> the "not a protected veteran" option; disability -> the "no, I don't have a \
disability" option; government-official / worked-for-THIS-company / conflict-of-interest \
history -> "No". These are answered, not skipped — but the DEFAULT marker must be set so the \
run record shows which answers were assumed rather than known.
- A REQUIRED work-authorization or visa-sponsorship question is NEVER left blank: resolve the \
role's location from the job context (when the context names none, assume the candidate's own \
country) and answer from work_authorization / requires_sponsorship ("authorized to work in the \
location where this role is based?" + US role + US-authorized candidate -> "Yes").
- ADJACENT-SKILL INFERENCE (user-sanctioned): a years-of-experience question about a skill \
implied by the candidate's stack is answered from the implying skill's tenure, never 0 \
(e.g. React on the profile implies TypeScript — answer with the React years). A skill with \
no such neighbour keeps the honest low answer.
- OFFICE-ATTENDANCE / COMMUTE commitment questions ("able to work from our X office N days a \
week?", "willing to work in-person from Y?"): reason from the candidate's location vs the \
office location — same metro area -> "Yes"; different metro -> use willing_to_relocate. A \
conditional follow-up premised on NOT being local ("If not currently in the area, …") is \
answered only when the premise holds; otherwise leave it blank. Never leave the PRIMARY \
commitment question blank.
- If the field has OPTIONS, `value` MUST be EXACTLY one of those option strings, copied \
verbatim. Pick the option the profile best supports. For a yes/no question, reason from the \
profile (e.g. "authorized to work in Japan?" -> the profile is US-authorized only -> "No"). \
For demographic / EEO questions (gender, race/ethnicity, Hispanic or Latino, veteran, disability, \
sexual orientation, gender identity / transgender status, pronouns): if the profile DISCLOSES that \
attribute (profile keys gender, race_ethnicity, hispanic_or_latino, veteran_status, \
disability_status, sexual_orientation, gender_identity, transgender, pronoun), pick the option \
matching it; ONLY if the profile does not disclose it, choose a "Prefer not to say" / "I don't wish \
to answer" / "Decline" option.
- SKILL SELF-RATING questions ("rate your experience 1 (No experience) to 5 (Expert)" for a \
named technology): rate from the profile's skills/experience — a skill listed in the profile or \
central to their roles -> "4" or "5"; an adjacent/related skill -> "3"; something the profile \
never mentions -> "2". Answer with the OPTION string exactly (e.g. "4"). Never leave a required \
rating blank.
- SCREENING / ELIGIBILITY yes-no questions: answer the safe, TRUTHFUL default for an ordinary \
applicant rather than leaving a required question blank. Work-authorization / visa-sponsorship / \
citizenship / export-control questions -> answer from the profile (work_authorization, \
authorized_to_work_us, requires_sponsorship, visa_status, citizenship). An age question \
("18 or older?") -> "Yes". Questions about a prior tie the profile does NOT mention — prior/current \
employment at a NAMED company, a family/relationship or conflict-of-interest tie, owning or \
controlling intellectual property, being a current/former government or military/DOD employee, an \
existing non-compete / non-disclosure / non-solicitation agreement, criminal or disciplinary \
history — default to "No" (the applicant has no such tie unless the profile says so). For a \
"select all that apply" / checkbox question the profile does not cover, choose the neutral \
none-of-the-above option if one is present ("Neither" / "None" / "None of the above" / "Not \
applicable"); otherwise leave it unchecked.
- SAFE DEFAULTS when there is no exact profile basis (do NOT leave a reasonable field blank, but \
NEVER invent specific data — zip, salary, employee id, address, references): \
"Preferred name"/"preferred first name" -> the profile's first name; \
"How did you hear about us/this job?" -> pick the most neutral truthful OPTION present, preferring \
"LinkedIn" when the profile has a LinkedIn, else "Company Website"/"Other" (free text -> "LinkedIn"); \
a required acknowledgement/consent option (label starts with "I ", "By ", "Acknowledge", or says \
agree/confirm/consent) -> return that option's label verbatim to select it; \
a "phone device type"/"phone type" field, when the profile gives a phone but no device type -> \
"Mobile" (a personal contact number is a mobile/cell — pick the option matching that if OPTIONS \
are given); \
a "country/region phone code" / "phone code" / "dialing code" field -> the profile's COUNTRY NAME \
(these widgets are searched by country name, e.g. country "United States" -> "United States", NOT \
the numeric "+1"); \
a specific data field the profile lacks -> "" (blank, never fabricated).
- If TYPE is `textarea` (an open-ended question like "Why are you interested?"), WRITE a \
concise, specific answer of 3-5 sentences, first person, plain text, grounded ONLY in the \
profile. Do not use markdown.
- For short text fields, copy the matching profile value verbatim (e.g. a LinkedIn / Website \
/ GitHub URL); blank if the profile has none.
- PHONE NUMBER field: if the field list ALSO contains a separate "phone code" / "country/region \
phone code" / "dialing code" field, the phone-number value MUST EXCLUDE the country/dial code \
(e.g. profile "+1 415 555 0142" -> "415 555 0142") — the code lives in its own field, and a \
number with a duplicate code fails validation. If there is NO separate code field, keep the full \
number as the profile has it.
Return one entry per field, no extras."""


async def map_fields(
    llm: Any, fields: list[FormField], profile: dict, title: str, job_context: str = ""
) -> dict[str, FieldFill]:
    """The single paid step. Returns {field_name: FieldFill}."""
    from browser_use.llm.messages import SystemMessage, UserMessage

    descriptors = [
        {
            "name": f.name,
            "label": f.label,
            "type": f.type,
            "required": f.required,
            **({"options": f.options} if f.options else {}),
        }
        for f in fields
    ]
    ctx = {"job_title": title, "applicant_profile": profile, "fields": descriptors}
    # JOB CONTEXT (audit pattern 3): prose answers ('Why do you want to join us?') written from
    # profile + job TITLE alone come out generic — give the mapper the page's own JD text so the
    # answer can reference the actual role/company.
    if job_context:
        ctx["job_description_excerpt"] = str(job_context)[:3500]
    res = await llm.ainvoke(
        [SystemMessage(content=_MAP_SYSTEM), UserMessage(content=json.dumps(ctx, ensure_ascii=False))],
        output_format=FillMap,
    )
    out = {f.name: f for f in res.completion.fields}
    # IDENTITY GUARD (not semantics): the mapper keeps returning the phone with its +CC stripped
    # even when instructed otherwise; a national number under a widget's default country flag is
    # invalid and gets wiped (rippling '+44 GB' + red 'required'). If a mapped value is a strict
    # SUFFIX of the profile phone, restore the full international form verbatim.
    ph = str((profile or {}).get("phone") or "").strip()
    if ph.startswith("+"):
        for f in out.values():
            v = (f.value or "").strip()
            if v and v != ph and ph.replace(" ", "").endswith(v.replace(" ", "")):
                f.value = ph
    # PROSE GUARD (deterministic — the prompt rule alone did NOT stop the model): a bare Yes/No in
    # a PROSE question is always wrong ('Why do you want to join Figma?*' -> 'Yes'). Detect prose by
    # CONTROL (textarea/open_ended) OR by the QUESTION asking for an explanation — figma renders it
    # as <input type=text>, escaping a control-only check (the audit's exact gap). Blank it so the
    # field routes to retry/agent (which writes real prose) instead of committing garbage.
    def _is_prose(f: Any) -> bool:
        if str(getattr(f, "source", "")) == "open_ended" or str(getattr(f, "type", "")) == "textarea":
            return True
        q = (getattr(f, "label", "") or "").lower()
        # a question inviting free-form text — principle+example, not an exhaustive verb list
        return any(k in q for k in ("why do you", "why are you", "tell us", "describe", "explain", "in your own words", "1-2 sentence", "2-3 sentence", "cover letter")) and len(q) > 15
    _prose = {f.name for f in fields if _is_prose(f)}
    for name, f in out.items():
        if name in _prose and (f.value or "").strip().lower() in ("yes", "no", "n/a", "-", "true", "false"):
            f.why = f"PROSE-GUARD dropped bare '{f.value}'"
            f.value = ""
    # LABEL-ECHO GUARD (deterministic): a mapped value equal to the field's own question is a
    # mapper echo, never an answer — downstream it gets TYPED as a search query / committed as
    # the 'value' (ambral mega/39: search 'Are you currently authorized to work in the US?').
    _lab_by_name = {f.name: " ".join((getattr(f, "label", "") or "").split()).lower().rstrip("*: ") for f in fields}
    for name, f in out.items():
        v = " ".join((f.value or "").split()).lower().rstrip("*: ")
        lab = _lab_by_name.get(name, "")
        # exact OR prefix: a vision-union label carries the options tail ('… located? Yes No')
        # while the mapper echoes just the question — prefix relation, not equality (openai
        # mega/48: value == the question, exact-match guard slipped).
        if v and len(v) >= 15 and (v == lab or lab.startswith(v)):
            f.why = "LABEL-ECHO dropped (value == question)"
            f.value = ""
    return out


# ---------------------------------------------------------------------------
# L3 — escalate a single field to a browser-use Agent (generic fallback).
# ---------------------------------------------------------------------------
# Freeze every already-FILLED field so an agent — which can misread a React-controlled input as
# empty (the bu-2-0 false-empty problem) — physically CANNOT re-fill or disturb completed work.
# Empty fields (the failed target, or a not-yet-filled box) stay editable. Restored after the agent.
_FREEZE_FILLED_JS = (
    "() => { let n=0; document.querySelectorAll('input,textarea,select').forEach(e => {"
    # A CHIP/typeahead input is EMPTY even when committed — its value lives in a `selectedItem` PILL in
    # the surrounding multiSelect/select container. Without this the agent re-types committed chips
    # (School / Field of Study / Skills), reopening the menu -> the residual-agent timeout. Treat a chip
    # whose container holds a pill as filled, so it's frozen too.
    ' const box = e.closest(\'[data-automation-id="multiSelectContainer"],[data-uxi-widget-type="selectinput"]\');'
    " const pill = !!(box && box.querySelector('[data-automation-id=\"selectedItem\"]'));"
    " const filled = (e.type==='checkbox'||e.type==='radio') ? e.checked : (((e.value||'').trim().length>0) || pill);"
    " if (filled && !e.readOnly && !e.disabled) {"
    "   const lock = (e.tagName==='SELECT'||e.type==='checkbox'||e.type==='radio'||e.type==='file'||pill);"
    "   e.setAttribute('data-gh-froze', lock ? 'd' : 'r'); if (lock) e.disabled = true; else e.readOnly = true; n++; }"
    " }); return n; }"
)
_UNFREEZE_JS = (
    "() => document.querySelectorAll('[data-gh-froze]').forEach(e => {"
    " if (e.getAttribute('data-gh-froze') === 'd') e.disabled = false; else e.readOnly = false;"
    " e.removeAttribute('data-gh-froze'); })"
)


async def _unfreeze(session: Any) -> None:
    with contextlib.suppress(Exception):
        p = await session.must_get_current_page()
        await p.evaluate(_UNFREEZE_JS)


# ---------------------------------------------------------------------------
# Anti-thrash kit for the wizard agents — the SAME single-page kit proven in
# jobapply.py (the only thing that breaks the Workday vision-thrash loop + the
# resume-upload structural failure that timed out 5 live runs at 480s / ~$7).
#   * register_visual_verify(tools) -> the agent can CALL verify_field_visually
#     to confirm a false-empty BEFORE retyping (kills the non-committing-dropdown
#     loop where it clicks a stale option node 6x to timeout).
#   * make_loop_verify_hook() on_step_end -> automatic nudge that names the field
#     + value when vision confirms it is already set, so bu cannot re-hallucinate
#     it empty.
#   * available_file_paths=[resume] -> resume upload stops failing structurally.
# ---------------------------------------------------------------------------
# Anti-loop rules appended to every wizard-agent task prompt (the proven text).
_ANTI_LOOP_RULES = (
    "\nANTI-LOOP RULES (these break the vision-thrash that times the run out):\n"
    "- If a field reads EMPTY in the browser state but you ALREADY acted on it, call "
    "verify_field_visually BEFORE re-typing — never type or click the same field twice "
    "without verifying it first (the state read-back is often a false-empty).\n"
    "- For dropdowns use the dropdown SELECT tools (get_dropdown_options / select_dropdown_option), "
    "do NOT hand-click option nodes — they re-render and your click lands on a stale node, "
    "looping forever."
)


def capture_l3_history(hist: Any, tag: str) -> None:
    """Persist a browser-use Agent run so an L3 escalation is NEVER re-debugged (user directive: 'download
    the script from browser use for our escalate & auto improve'). Writes two files under GH_L3_LEARN_DIR
    (default runs/l3_learn/): (1) <tag>.json — the FULL AgentHistoryList (replayable, human-readable);
    (2) appends one line to corpus.jsonl — a distilled {tag, success, secs, actions:[{name, params,
    selector}]} record that the auto-improve pass mines to promote a repeated agent action into an L1
    rule. Best-effort; a capture failure never affects the run."""
    import contextlib
    import json
    import os
    import pathlib

    out_dir = pathlib.Path(os.environ.get("GH_L3_LEARN_DIR", "runs/l3_learn"))
    with contextlib.suppress(Exception):
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", tag)[:120]
        with contextlib.suppress(Exception):
            hist.save_to_file(str(out_dir / f"{safe}.json"))  # full replayable history
        # distilled corpus line: the successful ACTIONS + their interacted selectors = the learn signal
        actions = []
        with contextlib.suppress(Exception):
            for a in hist.model_actions():  # list[dict]: {action_name: params, interacted_element: {...}}
                name = next((k for k in a if k != "interacted_element"), "?")
                el = a.get("interacted_element") or {}
                sel = ""
                if isinstance(el, dict):
                    sel = el.get("css_selector") or el.get("xpath") or el.get("tag_name") or ""
                elif el:
                    sel = str(getattr(el, "css_selector", "") or getattr(el, "xpath", "") or "")[:200]
                actions.append({"name": name, "params": a.get(name), "selector": str(sel)[:200]})
        rec = {"tag": safe, "success": None, "secs": None, "final": None, "actions": actions}
        with contextlib.suppress(Exception):
            rec["success"] = hist.is_successful()
        with contextlib.suppress(Exception):
            rec["secs"] = round(hist.total_duration_seconds() or 0, 1)
        with contextlib.suppress(Exception):
            rec["final"] = str(hist.final_result() or "")[:300]
        with (out_dir / "corpus.jsonl").open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"   [L3-capture] {safe} -> {out_dir}/ (actions={len(actions)} success={rec['success']})")


def _wizard_agent_kit() -> tuple[Any, Any]:
    """Build the (tools, on_step_end_hook) the single-page path uses, for a wizard agent.
    tools carries verify_field_visually; the hook is the cached/capped/nudge-only loop guard
    (it NEVER stops the agent). Fresh per-page VLM budget is reset by run_wizard each step."""
    from vision_verify import make_loop_verify_hook, register_visual_verify

    from browser_use import Tools

    tools = Tools()
    register_visual_verify(tools)
    return tools, make_loop_verify_hook()


def _strip_urls(text: str) -> str:
    """Adobe bad-seed fix: a Workday validation message can contain a token that LOOKS like a
    URL (e.g. 'https://value.Error' / a hallucinated link), which browser-use will obediently
    NAVIGATE to — losing the form. Strip anything URL-shaped out of the repair seed text so the
    agent only ever reads field-level instructions, never a destination to open.

    HARDENED (verified live on autodesk): browser-use's task preprocessor also matches BARE
    domain-shaped tokens and prepends https:// itself — the concatenated error text '...must have
    a value.Error-Country...' became a navigation to 'https://value.Error', which wiped a fully
    filled page. So: strip explicit links, strip bare word.tld tokens, and break every
    dot-followed-by-letter junction so nothing in the seed can ever parse as a domain."""
    t = re.sub(r"\b(?:https?://|www\.)\S+", "[link removed]", text or "")
    t = re.sub(r"\b\S+\.(?:com|org|net|io|co|ai|dev|app)\b\S*", "[link removed]", t)
    return re.sub(r"\.(?=[A-Za-z])", ". ", t)


async def _ensure_cdp_live(session: Any) -> None:
    """Repair the shared CDP client after an L3 browser_use.Agent — REACTIVELY. Agent teardown on a
    keep_alive session is inconsistent in 0.13.1: sometimes it nulls the client (next deterministic op
    throws 'Client is not started'), sometimes it leaves a LIVE websocket. Neither prior guard was
    correct: an UNCONDITIONAL session.connect() tears down a live socket ('connect() called but CDP
    client already exists! Cleaning up old connection' -> WebSocket closed -> FATAL), while guarding on
    session.is_cdp_connected skips needed reconnects because that flag LIES (reads True when dead).
    So PROBE with a real CDP round-trip and reconnect ONLY when the probe actually fails."""
    try:
        page = await session.must_get_current_page()
        await page.evaluate("() => 1")  # real Runtime.evaluate round-trip; throws iff the client is dead
        return  # live — must NOT reconnect (would destroy the working socket)
    except Exception:
        pass
    with contextlib.suppress(Exception):
        await session.connect()  # genuinely dead -> rebuild the root client + watchdogs


# ---------------------------------------------------------------------------
# Job wall-clock budget: agents are the only open-ended time sink (each step is an LLM
# call + screenshot, ~20s). The kalepa/ringcentral class = an agent grinding into the
# runner's kill window. Runners call set_job_deadline(); every agent entry point checks
# there is enough runway left before spawning, so a job DEGRADES (field FAILs, captured)
# instead of dying mid-CDP.
# ---------------------------------------------------------------------------
_JOB_DEADLINE: float | None = None


def set_job_deadline(budget_s: float | None) -> None:
    global _JOB_DEADLINE
    _JOB_DEADLINE = (time.monotonic() + budget_s) if budget_s else None


def _agent_runway(min_s: float = 90.0) -> bool:
    """True if no deadline set or enough wall clock remains to be worth spawning an agent."""
    return _JOB_DEADLINE is None or (_JOB_DEADLINE - time.monotonic()) > min_s


async def escalate(
    session: Any, agent_llm: Any, page: Any, field: FormField, value: str, resume: str | None = None
) -> bool:
    if not _agent_runway():
        print("   [budget] wall clock nearly spent — L3 escalation skipped")
        return False
    from browser_use import Agent

    label = field.label or field.name
    with contextlib.suppress(Exception):
        await page.evaluate(_FREEZE_FILLED_JS)  # lock filled fields — the agent can only touch the target
    task = (
        f"You are already on the application page. Every other field is LOCKED — fill ONLY the single "
        f"form input labeled '{label}' and put this exact text into it: {value!r}. "
        "It is a form field on THIS page — never navigate, never open a URL, even if the value looks like a link. "
        "Do not submit the form and do not touch any other field. Call done once that field shows the value."
        + _ANTI_LOOP_RULES
    )
    tools, hook = _wizard_agent_kit()
    try:
        # use_vision='auto' lets the agent pull a screenshot to SEE field state (avoids re-typing a
        # field the serialized DOM falsely reads empty) — it observes, the deterministic layer fills.
        # tools -> verify_field_visually; available_file_paths -> resume upload works; hook -> loop guard.
        agent = Agent(
            task=task,
            llm=agent_llm,
            browser_session=session,
            tools=tools,
            use_vision="auto",
            available_file_paths=[resume] if resume else None,
        )
        hist = await agent.run(max_steps=4, on_step_end=hook)
        capture_l3_history(hist, f"field_{field.name}")
        return True
    except Exception as exc:
        print(f"   [L3] agent failed for {field.name}: {exc}")
        return False
    finally:
        # browser_use.Agent teardown may or may not drop the shared CDP client. Repair it REACTIVELY
        # (probe-then-reconnect) — an unconditional reconnect here was DESTROYING live sockets and
        # killing the whole wizard after the first escalation.
        await _ensure_cdp_live(session)
        await _unfreeze(session)  # ALWAYS unlock — else subsequent deterministic fills hit frozen fields


async def agent_fill_section(
    session: Any, page: Any, *, section: str, instructions: str, resume: str | None = None, max_steps: int = 10
) -> dict:
    """Hand a hard, NON-schema section (an education / experience REPEATER whose rows + searchable
    closed-taxonomy comboboxes exist only in the live DOM, below the fold and NOT in the selector
    map) to a FOCUSED browser-use Agent. The agent scrolls to the section and drives the comboboxes
    with browser-use's native dropdown actions + reasoning — robust where deterministic string-match
    fails ('B.S.' -> 'Bachelor of Science', huge searchable school lists).

    FILL-ONLY is enforced STRUCTURALLY, not by prompt alone: every submit control is DISABLED before
    the agent runs (it physically cannot submit) and restored after. Runs LAST, so the agent's CDP
    teardown can't perturb earlier deterministic fields."""
    if not _agent_runway(min_s=150.0):  # sections legitimately run long — need real runway
        print(f"   [budget] wall clock nearly spent — section agent '{section}' skipped")
        return {}
    from browser_use import Agent, ChatGoogle

    disable_js = (
        "() => { let n=0; document.querySelectorAll('button[type=submit],input[type=submit]')"
        ".forEach(b => { b.setAttribute('data-gh-was', b.disabled ? '1':'0'); b.disabled = true; n++; }); return n; }"
    )
    restore_js = (
        "() => document.querySelectorAll('[data-gh-was]').forEach(b => {"
        " b.disabled = b.getAttribute('data-gh-was') === '1'; b.removeAttribute('data-gh-was'); })"
    )
    with contextlib.suppress(Exception):
        await page.evaluate(disable_js)  # neutralise submit so the agent CANNOT submit the form
    with contextlib.suppress(Exception):
        await page.evaluate(_FREEZE_FILLED_JS)  # lock already-filled fields — agent can't disturb them

    task = (
        f"You are already on a job-application page. Fill ONLY the {section} section: {instructions}. "
        f"Each {section} field (School, Degree, Discipline, etc.) is a SEARCHABLE dropdown — scroll to "
        "it, click it, type, and pick an option. CRITICAL: these are CLOSED lists, so you must pick the "
        "CLOSEST AVAILABLE option, not insist on an exact string. Map abbreviations (Degree 'B.S.' -> "
        "'Bachelor's Degree' / 'Bachelor of Science'; 'M.S.' -> 'Master's Degree' / 'Master of Science'). "
        "If a search shows 'No options', the list doesn't have that exact term — RETRY with a SHORTER or "
        "broader term (e.g. 'Electrical and Computer Engineering' -> 'Electrical' -> 'Engineering' -> "
        "'Computer'), then pick the nearest option offered. Do not leave a dropdown with text typed but no "
        "option selected. Use 'Add another' before each additional entry. Touch NOTHING outside this "
        "section. The Submit button is DISABLED on purpose — do NOT submit and do NOT navigate. Call done "
        "once every dropdown in the section shows a SELECTED value." + _ANTI_LOOP_RULES
    )
    ok = True
    tools, hook = _wizard_agent_kit()
    try:
        import oa_llm as _oal

        llm = _oal.openai_primary_llm("agent") or ChatGoogle(
            model="gemini-3-flash-preview", api_key=os.environ.get("GOOGLE_API_KEY")
        )
        agent = Agent(
            task=task,
            llm=llm,
            browser_session=session,
            tools=tools,
            use_vision="auto",
            available_file_paths=[resume] if resume else None,
        )
        hist = await agent.run(max_steps=max_steps, on_step_end=hook)
        capture_l3_history(hist, f"section_{section}")
        # HONEST bookkeeping (verified live: 'agent_ok': True was recorded for section agents whose
        # judge verdict was FAIL and who changed NOTHING) — agent_ok must reflect the agent's own
        # done-success, not merely 'the agent ran without raising'.
        with contextlib.suppress(Exception):
            ok = hist.is_successful() is True
    except Exception as exc:
        print(f"   [agent:{section}] {exc}")
        ok = False
    finally:
        await _ensure_cdp_live(session)  # probe-then-reconnect (see escalate); the is_cdp_connected guard lied
        with contextlib.suppress(Exception):
            await page.evaluate(restore_js)
        await _unfreeze(session)  # unlock the frozen filled fields
    return {"section": section, "agent_ok": ok}


async def repair_and_advance(
    session: Any,
    page: Any,
    errors: list[str],
    advance_label: str,
    agent_llm: Any = None,
    resume: str | None = None,
    max_steps: int = 22,
) -> bool:
    """Agent-driven recovery for a step that FAILED validation on advance. KEY finding: once a
    platform (Workday) rejects a Save, NO deterministic re-fill re-arms it — not browser-use
    el.fill, not the native value-setter + input/change/blur, not clicking Save repeatedly. Only a
    real, coherent interaction context (fix the field AND click the advance button as one human-like
    sequence) re-arms the form. So we hand the WHOLE recovery to a browser-use Agent: it reads the
    validation messages, corrects the flagged fields, and clicks the step-advance button itself.
    Only the FINAL submit is disabled (the agent must still be able to Save-and-Continue past the
    step); the agent is told never to navigate and never to finalize. The agent's CDP teardown is
    re-attached in finally. Returns True if the agent ran (advancement is verified by the caller)."""
    if not _agent_runway(min_s=120.0):
        print("   [budget] wall clock nearly spent — repair agent skipped")
        return False
    from browser_use import Agent, ChatGoogle

    # STRUCTURAL submit-guard (belt-and-suspenders to install_submit_guard already running on the
    # session): re-disable any final-submit button now, in case the guard interval isn't installed.
    await install_submit_guard(page)

    # ADOBE BAD-SEED FIX: strip URL-shaped tokens from each validation message before seeding the
    # task — a message like 'invalid: https://value.Error' would otherwise make browse-use NAVIGATE
    # to a hallucinated link and lose the form.
    bullet = "\n- ".join(_strip_urls(e) for e in errors[:12])
    task = (
        "You are on one step of a multi-step job-application form. It FAILED to advance because of "
        f"these validation errors:\n- {bullet}\n"
        "Fix ONLY the field(s) named by these errors so they become valid, then click the "
        f"'{advance_label}' button EXACTLY ONCE to advance ONE step, and then IMMEDIATELY call done. "
        "Work efficiently — fix all the flagged fields, then advance; do not re-verify endlessly.\n"
        "ABSOLUTE STOP RULES (a human reviewer must submit, not you):\n"
        "- Advance only ONE step. After the page advances once, call done. Do NOT fill or advance a "
        "second step.\n"
        "- NEVER click a button labelled 'Submit', 'Submit Application', 'Submit Apply', 'Finish', or "
        "anything that finalizes the application — these are FORBIDDEN.\n"
        "- If clicking advance brings you to a REVIEW / summary page, or the only remaining action is "
        "a Submit/Finish button, call done IMMEDIATELY WITHOUT clicking anything.\n"
        "Reason from the error + the other fields already filled. Two common cases:\n"
        "1. FORMAT: a phone number rejected as invalid, when a separate country/dial-code field is "
        "already set, should DROP the leading dial code: '+1 415 555 0142' -> '415 555 0142'.\n"
        "2. SEARCHABLE DROPDOWN (School, Degree, Field of Study, etc.): click it, type, and pick an "
        "option. These are CLOSED lists — pick the CLOSEST available option, do not insist on an "
        "exact string. Map abbreviations (Degree 'B.S.' -> \"Bachelor's Degree\"/'Bachelor of "
        "Science'; 'M.S.' -> \"Master's Degree\"). If it shows 'No options', RETRY with a SHORTER or "
        "broader term (e.g. 'Electrical and Computer Engineering' -> 'Electrical' -> 'Engineering'), "
        "then pick the nearest option. Never leave a dropdown with text typed but no option selected.\n"
        "3. SCREENING / ELIGIBILITY yes-no questions (18 or older?, prior employee of a named "
        "company?, own intellectual property?, government/DOD employee?, non-compete agreement?, "
        "work authorization / visa sponsorship?): answer the safe, TRUTHFUL default for an ordinary "
        "applicant — '18 or older' -> Yes; questions about a prior tie the resume does not mention "
        "(prior employment at a named company, family/conflict ties, owning IP, gov/military "
        "employment, non-compete/NDA) -> No; authorized to work in the US -> Yes, require visa "
        "sponsorship -> No (unless the form data says otherwise). For a 'select all that apply' / "
        "checkbox question the resume doesn't cover, tick the none-of-the-above option ('Neither' / "
        "'None' / 'None of the above' / 'Not applicable') if present. Do NOT leave required questions "
        "blank.\n"
        "Prefer values already on the form / resume; for the screening defaults above use the stated "
        "ordinary-applicant answer. CRITICAL: every field is "
        "on THIS page — NEVER open a URL, navigate, search the web, or go back/forward (it loses the "
        "form). Do NOT submit a FINAL application (any 'Submit Application' is disabled). Call done "
        "as soon as the page has advanced to the next step." + _ANTI_LOOP_RULES
    )
    tools, hook = _wizard_agent_kit()
    try:
        agent = Agent(
            task=task,
            llm=agent_llm
            or __import__("oa_llm").openai_primary_llm("agent")
            or ChatGoogle(model="gemini-3-flash-preview", api_key=os.environ.get("GOOGLE_API_KEY")),
            browser_session=session,
            tools=tools,
            use_vision=True,
            available_file_paths=[resume] if resume else None,
        )
        hist = await agent.run(max_steps=max_steps, on_step_end=hook)
        capture_l3_history(hist, "repair_" + re.sub(r"[^A-Za-z0-9]+", "_", " ".join(errors)[:40]))
    except Exception as exc:
        print(f"   [agent:repair] {exc}")
    finally:
        await _ensure_cdp_live(session)  # probe-then-reconnect (see escalate); the is_cdp_connected guard lied
        # NOTE: the submit-guard is intentionally LEFT installed — the final Submit must stay
        # disabled for the rest of the wizard so nothing finalizes the application.
    return True


async def install_submit_guard(page: Any) -> None:
    """Continuously DISABLE any button that could FINALIZE (submit/finish) or DESTROY (discard/
    cancel/sign-out/back-to-posting) the application, via a persistent 300ms interval. Workday's
    apply flow is an SPA: an agent can advance forward (e.g. into Review) WITHIN the same document,
    so a one-time disable wouldn't cover controls that mount on a later step — and a confused agent
    has been seen click Back -> "Discard Application?" -> Discard, which would wipe all work. The
    interval re-disables these every tick so the automation physically cannot submit OR discard; a
    human must do either. Forward controls (Save/Continue/Next/Add) stay enabled. Idempotent."""
    with contextlib.suppress(Exception):
        await page.evaluate(
            "() => { const kill=()=>{"
            "   document.querySelectorAll('button,input[type=submit],a[role=button]').forEach(b=>{"
            "     const t=((b.textContent||'')+' '+(b.value||'')+' '+(b.getAttribute('aria-label')||''));"
            "     const danger=/submit|finish|finali[sz]e|discard|cancel|sign ?out|log ?out|delete application|withdraw|\\bback\\b|\\bprevious\\b|go back/i;"
            "     const safe=/save|continue|next|add|search|upload|edit/i;"
            "     if (danger.test(t) && !safe.test(t)) b.disabled=true; });"
            '   document.querySelectorAll(\'[data-automation-id="progressBar"],[data-automation-id*="progressBar"],'
            "[role=navigation] ol,[role=navigation] ul').forEach(e=>{ e.style.pointerEvents='none'; }); };"
            "  kill(); if (!window.__ghSubGuard) window.__ghSubGuard=setInterval(kill, 300); }"
        )


# field types whose deterministic read-back is prone to false-negatives (custom widgets the
# serialized DOM mis-reads) — worth a cheap VLM glance before re-filling / escalating.
# Widget types whose serialized DOM false-negatives on a busy SPA while the field is VISIBLY filled.
# A cheap cached VLM glance confirms them instead of paying for (and fragmenting the session with) an
# L3 agent. Text/textarea/email/tel included: on Workday a freshly-typed input frequently serializes
# blank mid-render, and re-filling a text field is safe/idempotent-or-cheap to visually confirm.
_VLM_RESCUE_TYPES = {
    "single_select",
    "multi_select",
    "radio",
    "checkbox",
    "date",
    "select_native",
    "text",
    "textarea",
    "email",
    "tel",
    "number",
}

# Anti-cascade: max L3 agent escalations allowed PER STEP. Once this many fields on one step have
# needed the agent, the rest of that step fills deterministically only (allow_escalation=False). One
# false-negating widget must never spawn N agents — each agent opens/switches tabs and fragments the
# shared CDP session, and unbounded escalation is exactly the chromium runaway we saw (36 procs).
_STEP_ESC_BUDGET = 3


async def _vlm_filled(session: Any, field: FormField, value: str) -> bool:
    """Cheap, cached VLM read-back rescue (handoff R1): is the field VISIBLY filled? Only for the
    widget types that false-negative; silent on any error / over-budget (caller falls through)."""
    if field.type not in _VLM_RESCUE_TYPES:
        return False
    with contextlib.suppress(Exception):
        from vision_verify import _matches, visual_check

        # VALUE-AWARE (was presence-only): 'is it filled?' rubber-stamps a WRONG committed value as
        # done — verified live: a garbage 02/02/2006 date read 'visually filled' and the ladder
        # reported tier=vlm, leaving a validation error that blocked the advance. Ask 'does it show
        # MY value?' instead; the VLM judges semantically (canonical wording still passes).
        verdict = await visual_check(session, target=field.label or field.name, key=field.name, want=value, use_cache=False)
        return _matches(verdict)
    return False


async def fields_from_errors(errs: list[str], fields: list[FormField], llm: Any = None) -> list[FormField]:
    """GENERIC reader of the advance-blocking message -> which fields to refill at L1.
    Two readers, merged: (1) deterministic label-containment (fast, exact, zero cost);
    (2) the CHEAP LLM as semantic authority — required for localized text, label-less messages
    ('33132 is not a valid postal code for Virginia' -> Postal Code AND State), and indirect
    references. Over-selection is safe (refill is idempotent, values come from the profile);
    under-selection costs a 10-minute agent run — so both readers always contribute.
    Bounded inputs (errors 800 chars, <=60 labels); errored LLM contributes nothing (never blocks)."""
    ek = norm(" ".join(errs))
    picked: dict[str, FormField] = {}
    for f in fields:
        if f.label and len(norm(f.label)) >= 3 and norm(f.label) in ek:
            picked[f.name] = f
    if llm is not None:
        with contextlib.suppress(Exception):
            import oa_llm as _oal
            from pydantic import BaseModel

            from browser_use.llm.messages import SystemMessage, UserMessage

            class _Sel(BaseModel):
                labels: list[str]

            labels = [f.label for f in fields if f.label][:60]
            res = await _oal.resilient_text(
                [
                    SystemMessage(
                        content="A form failed to advance with the validation message(s) below. "
                        "From the provided field labels, return the labels of EVERY field the "
                        "message says is missing, invalid, or inconsistent — including fields the "
                        "message references indirectly (a postal-code-vs-state mismatch implicates "
                        "BOTH 'Postal Code' and 'State'). Return only labels from the list."
                    ),
                    UserMessage(content=f"messages: {' | '.join(errs)[:800]}\nfield labels: {labels}"),
                ],
                output_format=_Sel,
                primary=llm,
            )
            if res is not None:
                want = {norm(x) for x in (res.completion.labels or [])[:8]}
                for f in fields:
                    if f.label and norm(f.label) in want:
                        picked.setdefault(f.name, f)
    return list(picked.values())[:8]


_URL_FIELD_RE = re.compile(r"url|linkedin|website|github|portfolio", re.I)
_BARE_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(/\S*)?$", re.I)


def _norm_url_value(field: FormField, value: str) -> str:
    """Normalize a URL-ish value for a URL-ish field: résumés carry bare 'linkedin.com/in/x' and
    Workday validators demand a full scheme'd URL (live evidence: 'Invalid LinkedIn URL' recurred on
    5 tenants; the nvidia repair only passed as 'https://www...in/x/'). Scheme + trailing slash for
    path URLs; non-URL fields and already-schemed values pass through untouched."""
    v = (value or "").strip()
    if not v or "://" in v:
        return value
    label = f"{field.label or ''} {field.name or ''}"
    if _URL_FIELD_RE.search(label) and _BARE_DOMAIN_RE.match(v):
        v = "https://" + ("www." if v.lower().startswith("linkedin.com") else "") + v
        if "/" in v.split("://", 1)[1] and not v.endswith("/"):
            v += "/"
        return v
    return value


async def fill_with_ladder(
    adapter: ATSAdapter,
    session: Any,
    page: Any,
    field: FormField,
    value: str,
    agent_llm: Any,
    resume: str | None,
    allow_escalation: bool = True,
) -> str:
    """Fill one field through L1 -> L2 -> L3. Return the tier that succeeded.

    NOTE: L3 runs a browser_use.Agent whose teardown stops the shared CDP client even on a
    keep_alive session — so after an L3 escalation, subsequent fields/screenshots on the same
    session fail ('Client is not started'). Set allow_escalation=False to cap the ladder at L2
    (used by the screenshot proof sweep so the session stays intact). Fixing the re-attach is
    tracked separately.
    """
    if not (value or "").strip():  # nothing to fill (incl. a file field with no path)
        return "blank"
    value = _norm_url_value(field, value)

    # ladder_trace: record WHICH rung died so a FAIL self-reports its class (commit vs
    # read-back vs escalation) into failures.jsonl — the loop's attribution signal; nobody
    # greps logs to answer "observe, match or commit?" anymore.
    trace: list[str] = []
    filled = await adapter.fill(session, page, field, value, resume)
    trace.append(f"L1.fill={'ok' if filled else 'MISS'}")
    if filled and await adapter.read_back(session, page, field, value):
        return "L1"
    if filled:
        trace.append("L1.read_back=MISS")
    # READ-BACK RESCUE (handoff R1): a custom widget (Workday listbox/checkbox) is often visibly
    # filled while the serialized DOM reads it blank -> a FALSE read-back failure. A cheap, cached
    # VLM glance confirms it without RE-FILLING (re-picking a listbox can mis-select) or paying for
    # the agent. Only for the widget types that actually false-negative.
    if filled and await _vlm_filled(session, field, value):
        return "vlm"

    await asyncio.sleep(0.4)
    if await adapter.fill(session, page, field, value, resume) and await adapter.read_back(session, page, field, value):
        return "L2"
    trace.append("L2=MISS")
    if await _vlm_filled(session, field, value):
        return "vlm"
    trace.append("vlm=MISS")

    if allow_escalation and await escalate(session, agent_llm, page, field, value, resume=resume):
        with contextlib.suppress(Exception):
            page = await session.must_get_current_page()  # re-acquire after agent + CDP re-attach
        await _unfreeze(session)  # belt-and-suspenders: ensure nothing stays locked for later fields
        if await adapter.read_back(session, page, field, value):
            return "L3"
        trace.append("L3.read_back=MISS")
    else:
        trace.append("L3=" + ("MISS" if allow_escalation else "off"))
    await failcap.capture(
        session, page, f"field_{field.name}", "FIELD_FAIL", " > ".join(trace),
        extra={"field": field.name, "ftype": str(getattr(field, "type", "")), "value_len": len(value)},
    )
    return "FAIL"


# ---------------------------------------------------------------------------
# Value resolution + instrumentation.
# ---------------------------------------------------------------------------
def _resolve(field: FormField, mapped: dict[str, FieldFill], resume: str | None) -> tuple[str, str]:
    if field.source == "file":
        return (resume or ""), "file"
    if field.source == "standard":
        return ("" if field.value is None else str(field.value)), "profile"
    if field.needs_map:
        ff = mapped.get(field.name)
        return (ff.value if ff else ""), "llm-map"
    return "", "profile"


@dataclass
class _Row:
    name: str
    type: str
    src: str
    tier: str
    fields: dict = dc_field(default_factory=dict)


def _print_report(adapter_name: str, title: str, report: list[_Row], usage: Any, n_mapped: int) -> None:
    tiers = {t: sum(1 for r in report if r.tier == t) for t in ("L1", "L2", "L3", "vlm", "blank", "FAIL")}
    fillable = [r for r in report if r.tier != "blank"]
    escalated = tiers["L2"] + tiers["L3"] + tiers["FAIL"]
    esc_rate = (escalated / len(fillable) * 100) if fillable else 0.0

    print("\n" + "=" * 78)
    print(f"  {adapter_name.upper()} SCHEMA-DRIVEN FILL — PER-FIELD INSTRUMENTATION (fill-only)")
    print(f"  {title}")
    print("=" * 78)
    print(f"  {'FIELD':<24}{'TYPE':<28}{'VALUE-SRC':<10}{'TIER':<6}")
    print("  " + "-" * 74)
    for r in report:
        print(f"  {r.name[:23]:<24}{r.type[:27]:<28}{r.src:<10}{r.tier:<6}")
    print("  " + "-" * 74)
    print(f"  fields total            : {len(report)}")
    print(
        f"  fill tiers              : L1={tiers['L1']}  L2={tiers['L2']}  L3={tiers['L3']}  "
        f"blank={tiers['blank']}  FAIL={tiers['FAIL']}"
    )
    print(f"  escalation rate (L2+L3+FAIL / fillable) : {esc_rate:.0f}%  ({escalated}/{len(fillable)})")
    print(f"  fields mapped by the 1 structured call  : {n_mapped}")
    print(f"  LLM calls (map + any L3 escalations)    : {usage.entry_count}")
    print(f"  TOTAL LLM COST                          : ${usage.total_cost:.5f}")
    print(f"  prompt tok {usage.total_prompt_tokens:,} | completion tok {usage.total_completion_tokens:,}")
    print("=" * 78)
    print("  (schema + deterministic fill are $0; cost is the 1 mapping call, plus L3 only when a field escalates)")


# ---------------------------------------------------------------------------
# The run loop — wires an adapter through the invariant pipeline.
# ---------------------------------------------------------------------------
async def _screenshot(session: Any, page: Any, path: str) -> str | None:
    """Save a PNG of the form via CDP, CLIPPED to the form region (drops the long job
    description so the filled fields are readable). Falls back to full-page if no form."""
    import base64

    try:
        sid = await page.session_id
        params: dict = {"format": "png", "captureBeyondViewport": True}
        clip_json = await page.evaluate(
            "() => { const a=document.querySelector('#first_name,[name=first_name],#email,[name=email]');"
            " if(!a) return ''; const form=a.closest('form')||a.parentElement;"
            " const r=form.getBoundingClientRect();"
            " return JSON.stringify({x: Math.max(0, window.scrollX + r.left - 12),"
            " y: window.scrollY + r.top - 12, w: Math.min(1100, r.width + 24),"
            " h: Math.min(6500, form.scrollHeight + 24)}); }"
        )
        if clip_json:
            c = json.loads(clip_json)
            params["clip"] = {"x": c["x"], "y": c["y"], "width": c["w"], "height": c["h"], "scale": 1}
        res = await session.cdp_client.send.Page.captureScreenshot(params=params, session_id=sid)
        Path(path).write_bytes(base64.b64decode(res["data"]))
        return path
    except Exception as exc:
        print(f"   [screenshot] failed: {exc}")
        return None


async def run(
    adapter: ATSAdapter,
    *,
    url: str,
    profile: dict,
    resume: str | None,
    headless: bool,
    screenshot_path: str | None = None,
    allow_escalation: bool = True,
    creds: Credentials | None = None,
) -> dict:
    """Dispatch by adapter shape: single-page (one extract+fill pass) vs wizard (stepped)."""
    with contextlib.suppress(Exception):  # runners may also call set_job_deadline() directly
        b = float(os.environ.get("GH_JOB_BUDGET_S", "0"))
        if b > 0:
            set_job_deadline(b)
    if adapter.multi_page:
        return await run_wizard(
            adapter,
            url=url,
            profile=profile,
            resume=resume,
            headless=headless,
            screenshot_path=screenshot_path,
            allow_escalation=allow_escalation,
            creds=creds,
        )
    return await run_single_page(
        adapter,
        url=url,
        profile=profile,
        resume=resume,
        headless=headless,
        screenshot_path=screenshot_path,
        allow_escalation=allow_escalation,
    )


async def run_single_page(
    adapter: ATSAdapter,
    *,
    url: str,
    profile: dict,
    resume: str | None,
    headless: bool,
    screenshot_path: str | None = None,
    allow_escalation: bool = True,
) -> dict:
    title, fields = await adapter.extract(url, profile)  # step 1 (adapter)
    print(f"[fill:{adapter.__class__.__name__}] {title}  ({len(fields)} fields)")

    from browser_use import BrowserProfile, BrowserSession, ChatGoogle
    from browser_use.tokens.service import TokenCost

    tc = TokenCost(include_cost=True)
    await tc.initialize()
    # thinking_level='minimal': label->value mapping is deterministic reasoning, not a
    # puzzle — minimal thinking cuts thought tokens ~10x, holding the call near ~$0.0015.
    llm = tc.register_llm(
        __import__("oa_llm").openai_primary_llm("agent")
        or ChatGoogle(model="gemini-3-flash-preview", api_key=os.environ.get("GOOGLE_API_KEY"), thinking_level="minimal")
    )

    map_rows = [f for f in fields if f.needs_map]  # step 2 (generic)
    mapped = await map_fields(llm, map_rows, profile, title) if map_rows else {}

    session = BrowserSession(browser_profile=BrowserProfile(headless=headless, keep_alive=True))
    await session.start()
    await session.navigate_to(url)
    await asyncio.sleep(2.5)
    page = await session.must_get_current_page()
    page = await adapter.open_form(session, page)  # reach the form (iframe-embed / wall / apply)
    # SUBMIT-GUARD (hard rule: NEVER submit). The single-page path relied only on "deterministic never
    # clicks Submit + escalation off" — fragile once L3/escalation is enabled. Install the structural
    # guard here too (parity with run_wizard) so a real Submit button is disabled regardless of tier.
    await install_submit_guard(page)

    result: dict = {
        "adapter": adapter.__class__.__name__,
        "title": title,
        "url": url,
        "fields_total": len(fields),
        "mapped": len(mapped),
        "screenshot": None,
    }

    # REACH rung: a redirect tenant often lands on the job DESCRIPTION with a visible
    # Apply affordance — click it ONCE and re-check before giving up (toasttab / n26 class).
    if not await form_present(adapter, page, fields) and await _try_apply_click(session, page):
        with contextlib.suppress(Exception):
            page = await session.must_get_current_page()

    # REACH rung 2 — iframe src-hop (toast class): the company careers page renders the SAME
    # hosted ATS form inside a cross-origin iframe our CDP session can't see into. The iframe
    # src IS the fillable form — hop the top-level page to it and fill normally. Cheap
    # alternative to a full OOPIF second-session drill (build that only if data still demands).
    if not await form_present(adapter, page, fields):
        hosts = [re.escape(h) for h in (getattr(adapter, "hosts", None) or [])]
        if hosts:
            with contextlib.suppress(Exception):
                src = await page.evaluate(
                    "() => { const rx = new RegExp(" + json.dumps("|".join(hosts)) + ");"
                    " const f=[...document.querySelectorAll('iframe')].find(i=>rx.test(i.src||''));"
                    " return f ? f.src : ''; }"
                )
                if src:
                    print(f"   [reach] hopping into ATS iframe src: {str(src)[:90]}")
                    await session.navigate_to(str(src))
                    await asyncio.sleep(2.0)
                    page = await session.must_get_current_page()

    if not await form_present(adapter, page, fields):
        # The form is not on this page — boards-api gave us the schema but the live form
        # is behind a redirect to the company site, an anti-bot wall (Cloudflare), a login,
        # or a different host. Abort BEFORE the ladder so we don't escalate every absent
        # field to the L3 agent (that path silently burns ~$0.01+/field, e.g. coinbase $0.22).
        try:
            final_url = await page.get_url()
        except Exception:
            final_url = url
        usage = await tc.get_usage_summary()
        if screenshot_path:
            result["screenshot"] = await _screenshot(session, page, screenshot_path)
        # capture + classify NOW (dead posting? redirect? wall?) so the record self-explains;
        # anything a human could act on (login / challenge / landing) is NEEDS_HUMAN, not BLOCKED.
        kind = "?"
        with contextlib.suppress(Exception):
            rec = await failcap.capture(
                session, page, f"blocked_{adapter.__class__.__name__}", "BLOCKED",
                "form not reachable", extra={"fields": len(fields), "mapped": len(mapped)},
            )
            kind = (rec or {}).get("triage", {}).get("kind", "?")
        status = "NEEDS_HUMAN" if kind in ("CAREERS_LANDING", "JOB_DESCRIPTION", "LOGIN_OR_VERIFY", "CAPTCHA_OR_ANTIBOT") else "BLOCKED"
        print("\n" + "=" * 78)
        print(f"  {status} — form not reachable for {adapter.__class__.__name__} (page kind: {kind})")
        print(f"  landed on: {final_url}")
        print(
            f"  fields in schema: {len(fields)}   mapped (paid): {len(mapped)}   cost so far: ${usage.total_cost:.5f}"
        )
        print("  (ladder skipped — no $ wasted escalating absent fields)")
        print("=" * 78)
        await session.kill()
        result.update(status=status, page_kind=kind, final_url=final_url, cost=usage.total_cost, tiers={}, filled=0)
        return result

    report: list[_Row] = []
    esc_used = 0  # anti-cascade: L3 agents spent on THIS step
    for f in fields:
        if f.source == "skip":
            continue
        value, src = _resolve(f, mapped, resume)
        allow = allow_escalation and esc_used < _STEP_ESC_BUDGET
        try:
            tier = await fill_with_ladder(adapter, session, page, f, value, llm, resume, allow)  # steps 3-4
        except asyncio.CancelledError:
            # runner wall clock hit MID-FILL (99% of wall time is inside this await) — grab the
            # artifact bundle before the cancellation propagates, else TIMEOUT rows have nothing
            # to autopsy (the kalepa gap: 240s, no screenshot, no reason).
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.shield(failcap.capture(
                        session, page, "timeout_mid_fill", "TIMEOUT",
                        f"wall clock hit while filling '{f.name}'",
                        extra={"filled_so_far": len(report), "fields_total": len(fields)},
                    )),
                    timeout=8,
                )
            raise
        # ONLY refresh the page handle when an L3 escalation actually ran (it re-attaches the
        # CDP client). Doing it on every FAIL is harmful: must_get_current_page() can latch a
        # stray about:blank target, after which all remaining fields fill on a blank page.
        if allow and tier in ("L3", "FAIL"):
            esc_used += 1
            with contextlib.suppress(Exception):
                page = await session.must_get_current_page()
        report.append(_Row(name=f.name, type=f.type, src=src, tier=tier))

    # repeater sections (education / experience) — separate add-row pass, not the flat loop
    with contextlib.suppress(Exception):
        rep = await adapter.fill_repeaters(session, page, profile, allow_escalation=allow_escalation)
        if rep:
            result["repeaters"] = rep
            print(f"  repeaters: {rep}")

    usage = await tc.get_usage_summary()
    _print_report(adapter.__class__.__name__.replace("Adapter", ""), title, report, usage, len(mapped))  # step 5
    if screenshot_path:
        result["screenshot"] = await _screenshot(session, page, screenshot_path)

    tiers = {t: sum(1 for r in report if r.tier == t) for t in ("L1", "L2", "L3", "vlm", "blank", "FAIL")}
    try:
        final_url = await page.get_url()
    except Exception:
        final_url = url
    result.update(
        status="FILLED",
        final_url=final_url,
        cost=usage.total_cost,
        tiers=tiers,
        filled=tiers["L1"] + tiers["L2"] + tiers["L3"] + tiers["vlm"],
    )

    if headless:
        await session.kill()
    else:
        print("\n  Browser left open for review. Ctrl+C to close.")
        with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
            while True:
                await asyncio.sleep(1)
        await session.kill()
    return result


# ---------------------------------------------------------------------------
# Wizard run loop — N single-pages behind auth + step navigation. Reuses the
# invariant primitives (map_fields, fill_with_ladder, read_back) per step.
# ---------------------------------------------------------------------------
async def run_wizard(
    adapter: ATSAdapter,
    *,
    url: str,
    profile: dict,
    resume: str | None,
    headless: bool,
    screenshot_path: str | None = None,
    allow_escalation: bool = True,
    creds: Credentials | None = None,
) -> dict:
    from browser_use import BrowserProfile, BrowserSession, ChatGoogle
    from browser_use.tokens.service import TokenCost

    with contextlib.suppress(Exception):  # wd_one calls run_wizard directly, bypassing run()
        b = float(os.environ.get("GH_JOB_BUDGET_S", "0"))
        if b > 0:
            set_job_deadline(b)
    title, _ = await adapter.extract(url, profile)  # title only; fields come per-step
    print(f"[wizard:{adapter.__class__.__name__}] {title}")
    tc = TokenCost(include_cost=True)
    await tc.initialize()
    llm = tc.register_llm(
        __import__("oa_llm").openai_primary_llm("agent")
        or ChatGoogle(model="gemini-3-flash-preview", api_key=os.environ.get("GOOGLE_API_KEY"), thinking_level="minimal")
    )
    # separate tc-registered LLM for the repair agent so its tokens count toward per-step cost.
    agent_llm = tc.register_llm(
        __import__("oa_llm").openai_primary_llm("agent")
        or ChatGoogle(model="gemini-3-flash-preview", api_key=os.environ.get("GOOGLE_API_KEY"))
    )
    result: dict = {"adapter": adapter.__class__.__name__, "title": title, "url": url, "steps": []}

    session = BrowserSession(browser_profile=BrowserProfile(headless=headless, keep_alive=True))
    try:
        await session.start()
        await session.navigate_to(url)
        await asyncio.sleep(2.5)
        page = await session.must_get_current_page()
        page = await adapter.open_form(session, page)  # job page -> Apply -> Apply Manually

        auth = await adapter.authenticate(session, page, creds)  # the account gate (Workday step 1)
        if not auth.ok:
            return await _wizard_halt(result, "AUTH_FAILED", auth.reason, tc, session)
        if auth.needs_verification:
            return await _wizard_halt(result, "EMAIL_VERIFICATION_REQUIRED", auth.reason, tc, session)

        # HARD SAFETY: keep the final Submit/Finish button disabled for the ENTIRE wizard (SPA-
        # persistent interval) so neither the deterministic path nor an agent can finalize the
        # application — we always STOP at Review for a human to submit.
        await install_submit_guard(page)

        seen: set[int] = set()
        resumed = False  # one-shot autosave-resume after a stall (stryker class)
        for _ in range(12):  # MAX_STEPS guardrail
            await install_submit_guard(page)  # re-assert each iteration (cheap; idempotent)
            with contextlib.suppress(Exception):
                from vision_verify import reset_visual_cache

                reset_visual_cache()  # fresh per-step VLM budget for the read-back rescue
            t0 = time.monotonic()
            c0 = (await tc.get_usage_summary()).total_cost
            step = await adapter.extract_step(session, page, profile)
            # RESUME UPLOAD — DETERMINISTIC-ONLY (directive #1). Scan the live page FRESH every step for the
            # file input and push bytes via CDP; idempotent (skips if already uploaded). Runs BEFORE per-field
            # fill / fill_repeaters and CANNOT escalate to an agent. No-op on single-page adapters.
            with contextlib.suppress(Exception):
                if await adapter.upload_resume(session, page, resume):
                    print("  resume: uploaded deterministically (CDP)")
            if os.environ.get("GH_DUMP"):  # capture each step's live DOM for OFFLINE detector dev (no live reruns)
                with contextlib.suppress(Exception):
                    html = await page.evaluate("() => document.documentElement.outerHTML")
                    dp = Path(os.environ["GH_DUMP"])
                    dp.mkdir(parents=True, exist_ok=True)
                    (dp / f"step{step.index:02d}_{(step.name or 'step').replace(' ', '_')[:24]}.html").write_text(html)
                    print(f"   [dump] step {step.index} '{step.name}' ({len(html) // 1024}KB)")
            if step.is_review or await adapter.is_complete(session, page):
                result["status"] = "FILLED_TO_REVIEW"  # STOP — never submit
                shot = None
                if screenshot_path:  # capture the final Review page as proof of completion
                    shot = await _screenshot(session, page, screenshot_path.replace(".png", "_review.png"))
                    result["review_screenshot"] = shot
                result["steps"].append(
                    {
                        "name": step.name or "Review",
                        "index": step.index,
                        "total": step.total,
                        "tiers": {},
                        "seconds": round(time.monotonic() - t0, 1),
                        "cost": round((await tc.get_usage_summary()).total_cost - c0, 5),
                        "agent_used": False,
                        "screenshot": shot,
                    }
                )
                break
            if step.index in seen:  # progress-monotonicity guard
                if not resumed:
                    # stryker class: a transient 'Something went wrong' reload can bounce the wizard
                    # back to an already-done step. Workday AUTOSAVES the application — re-entering
                    # from the job URL resumes at the last saved step. Try ONCE before halting.
                    resumed = True
                    print("  [wd] stalled — re-entering wizard from the job URL once (autosave resume)")
                    with contextlib.suppress(Exception):
                        await session.navigate_to(url)
                        page = await session.must_get_current_page()
                        page = await adapter.open_form(session, page)
                        await install_submit_guard(page)
                        seen.clear()
                        continue
                return await _wizard_halt(result, "STEP_STALLED", f"re-entered step {step.index}", tc, session)
            seen.add(step.index)

            map_rows = [f for f in step.fields if f.needs_map]
            mapped = await map_fields(llm, map_rows, profile, title) if map_rows else {}
            rows: list[_Row] = []
            esc_used = 0  # anti-cascade: cap L3 agents PER STEP so a false-negating widget can't runaway
            for f in step.fields:
                if f.source == "skip":
                    continue
                value, src = _resolve(f, mapped, resume)
                allow = allow_escalation and esc_used < _STEP_ESC_BUDGET
                tier = await fill_with_ladder(adapter, session, page, f, value, llm, resume, allow)
                if allow and tier in ("L3", "FAIL"):
                    esc_used += 1
                    with contextlib.suppress(Exception):
                        page = await session.must_get_current_page()  # L3 re-attached CDP; re-acquire handle
                rows.append(_Row(name=f.name, type=f.type, src=src, tier=tier))

            # off-schema repeater sections on this step (My Experience: work experience / education /
            # skills / languages). No-op (returns {}) on steps without a repeater — the adapter gates
            # on section headings. The agent freezes filled fields + submit stays disabled.
            repeaters_used = False
            with contextlib.suppress(Exception):
                rep = await adapter.fill_repeaters(session, page, profile)
                if rep:
                    repeaters_used = True
                    print(f"  repeaters: {rep}")
                page = await session.must_get_current_page()  # agent_fill_section re-attaches CDP

            # Deterministically answer any REQUIRED screening/eligibility radio the LLM map left empty
            # (Intel gates My-Information on 'previously employed by Intel?') — the cheap LLM decides the
            # ordinary external-applicant answer, the robust _click_radio commits via CDP — BEFORE the agent
            # ever sees it (that React radio always looped a vision agent). Workday-only (single-page adapters
            # don't define the method, so it's a no-op there).
            if hasattr(adapter, "answer_required_choices"):
                with contextlib.suppress(Exception):
                    n = await adapter.answer_required_choices(session, page, profile=profile)
                    if n:
                        print(f"  [wd] answered {n} required screening choice(s) deterministically")
                        page = await session.must_get_current_page()

            # screenshot the deterministically-filled step BEFORE advancing (the agent, if invoked,
            # advances to the NEXT page, so capture this page now).
            shot = None
            if screenshot_path:
                shot = await _screenshot(session, page, screenshot_path.replace(".png", f"_step{step.index}.png"))

            # Deterministic fill got the values in but the platform may reject a field's FORMAT/choice
            # on advance — and once it rejects a Save, NO deterministic re-fill re-arms it (verified).
            # So on a validation block, hand the recovery to an agent that fixes the flagged field(s)
            # AND clicks the advance button itself (the only thing that re-arms the form). Advancement
            # is verified by re-reading the step; if the agent didn't advance, the monotonicity guard
            # at the top of the loop turns the repeated step into an honest STEP_STALLED halt.
            agent_used = False
            adv = await adapter.next_step(session, page)
            if not adv.ok:
                errs = await adapter.validation_errors(page)
                if errs:
                    print(f"  [agent-repair] advance blocked by validation: {errs}")
                    # RESUME stays DETERMINISTIC (directive #1): if an error names the file/resume, push the
                    # bytes via CDP HERE and then ADVANCE deterministically — NEVER hand the file to the agent,
                    # which network-errors + LOOPS on the dropzone (the "Loop detection nudge" hang). Only fall
                    # to the agent for NON-file errors, or if the deterministic file-fix still doesn't advance.
                    if any(re.search(r"resume|cv|upload|file|attach", e, re.I) for e in errs):
                        with contextlib.suppress(Exception):
                            if await adapter.upload_resume(session, page, resume):
                                print("  [agent-repair] resume re-uploaded deterministically (CDP)")
                        with contextlib.suppress(Exception):
                            await adapter.next_step(session, page)
                            moved0 = await adapter.extract_step(session, page, profile)
                            if (
                                moved0.index != step.index
                                or moved0.is_review
                                or await adapter.is_complete(session, page)
                            ):
                                adv = AdvanceResult(ok=True, page=page)  # advanced deterministically; skip the agent
                                errs = []
                    if errs and not adv.ok:
                        # DETERMINISTIC FIX LOOP (L1 first, agent last): Workday's validation text
                        # CONTAINS the runtime labels of the offending fields — refill exactly those
                        # through the same ladder (escalation off), then re-advance. The old finding
                        # "no deterministic re-fill re-arms a rejected Save" predates trusted CDP
                        # input; the repair agent proved trusted refill+Save re-arms the form, so L1
                        # gets first shot. Label-in-error is field SELECTION (idempotent), not value
                        # matching — the substring directive doesn't apply.
                        with contextlib.suppress(Exception):
                            redo = await fields_from_errors(errs, step.fields, llm=agent_llm)
                            if not redo:
                                # VLM reader (user directive: 'VLM can help here too'): the text
                                # readers found no owner — ask ONE screenshot which fields are
                                # VISIBLY flagged, then map those runtime labels back to fields.
                                with contextlib.suppress(Exception):
                                    from vision_verify import flagged_fields_visually

                                    seen = {norm(x) for x in await flagged_fields_visually(session)}
                                    redo = [f for f in step.fields if f.label and norm(f.label) in seen]
                                    if redo:
                                        print(f"  [validation-fix] VLM flagged: {[f.label for f in redo][:6]}")
                            for f in redo[:8]:
                                value, _src = _resolve(f, mapped, resume)
                                if (value or "").strip():
                                    tier = await fill_with_ladder(
                                        adapter, session, page, f, value, agent_llm, resume,
                                        allow_escalation=False,
                                    )
                                    print(f"  [validation-fix] refill {(f.label or f.name)[:40]!r} -> {tier}")
                            if not redo and hasattr(adapter, "fill_repeaters"):
                                # REPEATER rung: the named fields (language grid, row dates) live
                                # inside repeater sections that step.fields does not carry — ONE
                                # idempotent fixpoint re-run reads everything back and fills only
                                # what is genuinely MISSING (respect-autofill + SKIP rules apply).
                                print("  [validation-fix] no step-level owner — re-running repeaters fixpoint")
                                with contextlib.suppress(Exception):  # flagged cells must not be SKIPped
                                    import wd_repeaters as _wdr

                                    _wdr.set_flagged(errs)
                                await adapter.fill_repeaters(session, page, profile)
                                with contextlib.suppress(Exception):
                                    _wdr.set_flagged([])
                                redo = step.fields[:1]  # mark work done so we re-advance below
                            if redo:
                                await adapter.next_step(session, page)
                                moved0 = await adapter.extract_step(session, page, profile)
                                if (
                                    moved0.index != step.index
                                    or moved0.is_review
                                    or await adapter.is_complete(session, page)
                                ):
                                    adv = AdvanceResult(ok=True, page=page)
                                    errs = []
                                    print("  [validation-fix] advanced deterministically — agent skipped")
                                else:
                                    errs = await adapter.validation_errors(page) or errs
                    if errs and not adv.ok:  # the deterministic fix loop didn't clear it -> agent
                        agent_used = True
                        await repair_and_advance(
                            session, page, errs, adapter.advance_label, agent_llm=agent_llm, resume=resume
                        )
                        page = await session.must_get_current_page()
                        moved = await adapter.extract_step(session, page, profile)
                        if moved.index != step.index or moved.is_review or await adapter.is_complete(session, page):
                            adv = AdvanceResult(ok=True, page=page)  # the agent advanced the step

            result["steps"].append(
                {
                    "name": step.name,
                    "index": step.index,
                    "total": step.total,
                    "tiers": {
                        t: sum(1 for r in rows if r.tier == t) for t in ("L1", "L2", "L3", "vlm", "blank", "FAIL")
                    },
                    "seconds": round(time.monotonic() - t0, 1),
                    "cost": round((await tc.get_usage_summary()).total_cost - c0, 5),
                    "agent_used": agent_used or repeaters_used,
                    "repeaters": repeaters_used,
                    "screenshot": shot,
                }
            )
            if not adv.ok:
                return await _wizard_halt(result, "ADVANCE_FAILED", adv.blocked_reason, tc, session)
            page = adv.page or await session.must_get_current_page()

        usage = await tc.get_usage_summary()
        result.setdefault("status", "FILLED_TO_REVIEW")
        result["cost"] = usage.total_cost
        print(f"  wizard steps filled: {len(result['steps'])}   cost ${usage.total_cost:.5f}   (stopped before Submit)")
        await session.kill()
        return result
    finally:
        # LEAK-PROOF: any crash in the wizard (e.g. a CDP 'Client is not started' teardown) must
        # NOT leave a live browser behind — the caller's retry-next-req loop would stack a second
        # browser on top of it (chromium runaway). kill is idempotent; normal returns already killed.
        with contextlib.suppress(Exception):
            await session.kill()


async def _wizard_halt(result: dict, status: str, reason: str, tc: Any, session: Any) -> dict:
    usage = await tc.get_usage_summary()
    result.update(status=status, reason=reason, cost=usage.total_cost)
    print(f"  WIZARD HALT: {status} — {reason}   (cost ${usage.total_cost:.5f})")
    # single choke point for EVERY wizard failure -> one capture wires the whole wizard
    # into the failures.jsonl loop (stryker/chewy would each have a PNG+HTML bundle here).
    with contextlib.suppress(Exception):
        page = await session.must_get_current_page()
        await failcap.capture(session, page, f"wizard_{status}", status, reason)
    with contextlib.suppress(Exception):
        await session.kill()
    return result
