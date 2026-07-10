"""oa_cdp_action — DIRECT-CDP WRITE backend (the SPA-hang fix, BUILD FIX A).

`observe_act` drives actions through browser-use's event-bus watchdog. On a never-idle SPA
/apply page (Lever / Ashby) the watchdog handlers WAIT for page readiness / navigation that
never settles -> 30-60s TimeoutError per action -> fields ESCALATE. The OLD per-archetype
filler used DIRECT Playwright (no readiness gate) and worked at ~99%.

This module is the FIX: a DIRECT-CDP action path that sets values + dispatches REAL trusted
events straight via CDP, bypassing the readiness watchdog entirely. It mirrors the EXACT
plumbing oa_dom_value already uses to READ (cdp_client_for_node -> DOM.resolveNode ->
Runtime.callFunctionOn), but for WRITES, and adds the trusted Input.* mouse/key dispatch the
watchdog uses for clicks/typeahead — WITHOUT the watchdog's wait.

REAL browser-use / CDP API mirrored (file:line, verified against the vendored tree — NOT guessed):

  RESOLVE (read plumbing reused verbatim — same as oa_dom_value):
    * BrowserSession.cdp_client_for_node(node) -> CDPSession
          browser/session.py:3788
    * cdp_session.cdp_client.send.DOM.resolveNode(params={'backendNodeId': <id>}, session_id=…)
          -> result['object']['objectId']
          default_action_watchdog.py:1145-1152 / :2102-2109 (the exact resolve pattern)

  WRITE value + REACT-aware events (Runtime.callFunctionOn, returnByValue=True):
    * _set_value_directly native-setter pattern .. default_action_watchdog.py:1996-2017
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set ->
          nativeSetter.call(this, text); dispatch focus/input/change/blur (React onChange).
    * _clear_text_field contenteditable+value .... default_action_watchdog.py:1657-1711
          contenteditable -> textContent="" + input/change; value -> native setter "".
    * native <select> commit ..................... default_action_watchdog.py:3697-3711
          element.focus(); element.value=…; option.selected=true; element.selectedIndex=…;
          dispatch input + change.

  CLICK (trusted, no readiness wait):
    * Input.dispatchMouseEvent move/press/release . default_action_watchdog.py:1259-1304
          mouseMoved -> mousePressed(button=left,clickCount=1) -> mouseReleased(...).
    * get_element_coordinates (center from quads) . browser/session.py:2648 (getContentQuads ->
          getBoxModel -> getBoundingClientRect); we reuse the node's absolute_position first,
          falling back to a callFunctionOn getBoundingClientRect (session.py:2712-2735 pattern).
    * JS click fallback .......................... default_action_watchdog.py:1154-1160 / :1242-1248
          callFunctionOn 'function() { this.click(); }' for occluded / box-less nodes
          (radios / checkboxes / option cells without a stable quad).

  TYPE (per-char trusted keystrokes for typeahead):
    * DOM.focus .................................. default_action_watchdog.py:1870 (focus before keys)
    * Input.dispatchKeyEvent keyDown/char/keyUp .. default_action_watchdog.py:2223-2257
          keyDown(key,code,vk) -> char(text=char) -> keyUp(...). Plain fields use _set_value
          directly (faster, React-aware); typeahead uses the keystroke path so debounced XHR fires.

EVERY public call is wrapped in a per-action asyncio timeout (CDP_ACTION_TIMEOUT) so a single
CDP round-trip can never hang the field loop the way the watchdog wait did. On timeout/error the
funcs return False (writes) — the state machine's verify/recommit handles the miss; nothing here
waits 30-60s. GENERIC: no per-ATS branch, no renameable-attribute key — pure standard DOM + CDP.

HARD: fill-only. Nothing here submits; cdp_click drives option/radio/checkbox/combobox triggers
only (the caller decides the target — never a submit control).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

# Per-action hard timeout (seconds). A single CDP round-trip is sub-second on a healthy session;
# this only exists so a wedged socket fails FAST (False) instead of hanging the field deadline.
CDP_ACTION_TIMEOUT = 4.0


# --------------------------------------------------------------------------- #
# JS bodies (bound to the located control via `this`, callFunctionOn). Pure standard-DOM,
# React-aware (native value setter), never throw across the CDP boundary.
# --------------------------------------------------------------------------- #

# Set value + dispatch REAL input/change so React's onChange fires (native setter bypasses
# React's _valueTracker). Mirrors _set_value_directly (watchdog :1996-2020). Handles
# input/textarea (.value via the right prototype's native setter) AND contenteditable
# (.textContent + input/change, watchdog :1666-1687). Returns the readable value string.
_SET_VALUE_JS = r"""
function(text) {
  try {
    const el = this;
    const tag = (el.tagName || "").toUpperCase();
    const isCE = (el.getAttribute && (el.getAttribute("contenteditable") === "" ||
                  el.getAttribute("contenteditable") === "true")) || el.isContentEditable === true;
    if (isCE && tag !== "INPUT" && tag !== "TEXTAREA") {
      while (el.firstChild) { el.removeChild(el.firstChild); }
      el.textContent = text;
      el.dispatchEvent(new FocusEvent("focus", { bubbles: true }));
      el.dispatchEvent(new Event("input", { bubbles: true, cancelable: true }));
      el.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
      el.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
      return el.textContent == null ? "" : String(el.textContent);
    }
    const proto = (tag === "TEXTAREA") ? window.HTMLTextAreaElement.prototype
                                       : window.HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    const nativeSetter = desc && desc.set;
    if (nativeSetter) { nativeSetter.call(el, text); } else { el.value = text; }
    // REACT _valueTracker RESET: the native setter updates el.value AND React's internal value
    // tracker in lockstep, so React's onChange change-detection (el.value === tracker.getValue())
    // sees NO change and never commits the value to component STATE — el.value reads back correct
    // but a later re-render repaints the input from its empty state and WIPES it (samsara greenhouse
    // embed: First/Preferred name filled + read-back CORRECT, then cleared by a downstream field's
    // re-render, a verify-passes-but-empty false-green). Forcing the tracker to a DIFFERENT value
    // before the input event makes React detect the change and setState the real value, so it
    // survives re-renders. Canonical controlled-input fix; a no-op on plain/uncontrolled inputs.
    try { const tk = el._valueTracker; if (tk) { tk.setValue(""); } } catch (e) {}
    el.dispatchEvent(new FocusEvent("focus", { bubbles: true }));
    el.dispatchEvent(new Event("input", { bubbles: true, cancelable: true }));
    el.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
    el.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
    return el.value == null ? "" : String(el.value);
  } catch (e) { return ""; }
}
"""

# Native <select> commit by visible option text OR value (case-insensitive). Mirrors the
# watchdog select handler (:3691-3711): focus -> value/selected/selectedIndex -> input/change.
# Returns the committed option text on success, "" on no match (caller -> False).
_SELECT_JS = r"""
function(want) {
  try {
    const el = this;
    if ((el.tagName || "").toUpperCase() !== "SELECT") return "";
    const target = String(want == null ? "" : want).trim().toLowerCase();
    const opts = Array.from(el.options || []);
    let hit = null;
    for (const o of opts) {
      const t = String(o.textContent || o.label || "").trim().toLowerCase();
      const v = String(o.value || "").trim().toLowerCase();
      if (t === target || v === target) { hit = o; break; }
    }
    if (!hit) {
      for (const o of opts) {  // looser contains-match as a second pass
        const t = String(o.textContent || o.label || "").trim().toLowerCase();
        if (target && t.indexOf(target) !== -1) { hit = o; break; }
      }
    }
    if (!hit) return "";
    el.focus();
    el.value = hit.value;
    hit.selected = true;
    el.selectedIndex = hit.index;
    el.dispatchEvent(new Event("input", { bubbles: true, cancelable: true }));
    el.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
    el.blur();
    return String(hit.textContent || hit.label || hit.value || "").trim();
  } catch (e) { return ""; }
}
"""

# this.click() — the JS-click fallback used for radios / checkboxes / option cells without a
# stable quad. Mirrors the watchdog occluded/box-less fallback (:1154-1160 / :1242-1248).
_JS_CLICK_JS = r"""
function() { try { this.click(); return true; } catch (e) { return false; } }
"""

# getBoundingClientRect center — used when the node has no absolute_position handle. Mirrors
# session.get_element_coordinates Method 3 (session.py:2712-2735).
_RECT_JS = r"""
function() {
  try {
    const r = this.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height };
  } catch (e) { return null; }
}
"""


# --------------------------------------------------------------------------- #
# Resolve plumbing — IDENTICAL to oa_dom_value.read_dom_value (the proven read path).
# Returns (cdp_session, session_id, object_id) or None.
# --------------------------------------------------------------------------- #
async def _resolve(session: Any, node: Any) -> tuple[Any, Any, str] | None:
    if node is None:
        return None
    backend_node_id = getattr(node, "backend_node_id", None)
    if backend_node_id is None:
        return None
    cdp_session = await session.cdp_client_for_node(node)
    session_id = cdp_session.session_id
    resolved = await cdp_session.cdp_client.send.DOM.resolveNode(
        params={"backendNodeId": int(backend_node_id)},
        session_id=session_id,
    )
    obj = (resolved or {}).get("object") or {}
    object_id = obj.get("objectId")
    if not object_id:
        return None
    return cdp_session, session_id, object_id


async def _call_on(cdp_session: Any, session_id: Any, object_id: str, fn: str, args: list[Any] | None = None) -> Any:
    """Runtime.callFunctionOn the located node (returnByValue=True). Returns result.result.value."""
    params: dict[str, Any] = {
        "functionDeclaration": fn,
        "objectId": object_id,
        "returnByValue": True,
    }
    if args is not None:
        params["arguments"] = [{"value": a} for a in args]
    result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(params=params, session_id=session_id)
    return ((result or {}).get("result") or {}).get("value")


# JS: `this` = the located node. STRUCTURAL (class-independent — duolingo & co. hash their class names
# so `.select__control` never matches): climb ≤6 ancestors to the first bounded box carrying rendered
# text, strip the leading label, and report whether the VALUE region is a placeholder (Select…/Choose…)
# or empty. Same proven body as oa_complete._STILL_EMPTY_CHOICE_JS (which flags duolingo's revert at the
# END), keyed off the live node so the ALREADY-CORRECT pre-check can tell a genuinely-painted 'No' from
# a control still showing 'SELECT…'. Diag fields returned for OA_PRECHK_DIAG instrumentation.
_RENDERED_PLACEHOLDER_JS = r"""
function(label, val){
  const nrm = s => (s||'').replace(/\s+/g,' ').trim();
  const low = s => nrm(s).toLowerCase();
  const isPh = t => { const s = nrm(t); return !s || /^(select|choose|start typing|pick|--)\b/i.test(s) || /select\.\.\./i.test(s); };
  let box = this, disp = '';
  for (let i=0;i<6 && box;i++){ const r=box.getBoundingClientRect(); const t=nrm(box.innerText); if(r.width>40 && r.height>0 && t){ disp=t; break; } box=box.parentElement; }
  const labLow = low(label).replace(/[*:]/g,'').trim();
  let valRegion = disp; const li = low(disp).indexOf(labLow);
  if (labLow.length > 4 && li >= 0) valRegion = nrm(disp.slice(li + labLow.length));
  // VALUE-INDEPENDENT (a value-substring false-matched 'No' inside the question 'Will you *no*w…').
  // ph = the value region is a placeholder/empty. Fallback token-scan of the whole box catches the
  // case where label-strip failed (rendered label != ctx.label) and left the question text in front.
  const phByRegion = isPh(valRegion);
  const phByToken = /(select|choose|start typing)\s*(\.\.\.|…)/i.test(disp);
  const ph = phByRegion || phByToken;
  return JSON.stringify({disp: disp.slice(0,60), valRegion: valRegion.slice(0,32), phByRegion, phByToken, ph});
}
"""


async def rendered_is_placeholder(session: Any, node: Any, label: str = "", value: str = "") -> bool:
    """True iff the node's rendered control box shows a placeholder / is empty (value NOT painted).
    Class-independent structural read. Conservative: False on any read failure, so it can only VETO an
    already-correct short-circuit when it DEFINITIVELY sees a placeholder — never turns a genuinely-
    prefilled field into a re-commit. OA_PRECHK_DIAG=1 prints what the box actually showed."""
    import json as _json
    import os as _os

    r = await _resolve(session, node)
    if r is None:
        return False
    cdp_session, session_id, object_id = r
    with contextlib.suppress(Exception):
        got = await _call_on(cdp_session, session_id, object_id, _RENDERED_PLACEHOLDER_JS, args=[str(label or ""), str(value or "")])
        d = _json.loads(got) if got else {}
        if _os.environ.get("OA_PRECHK_DIAG") == "1":
            print(f"   [PRECHK] ph={d.get('ph')} region={d.get('phByRegion')} token={d.get('phByToken')} disp={d.get('disp')!r} valRegion={d.get('valRegion')!r}")
        return bool(d.get("ph"))
    return False


# --------------------------------------------------------------------------- #
# PUBLIC: cdp_set_value — React-aware value set + input/change. No readiness wait.
# --------------------------------------------------------------------------- #
async def cdp_set_value(session: Any, node: Any, text: str) -> bool:
    """Set el.value (or textContent for contenteditable) AND dispatch REAL input/change events.

    Uses the native value setter so React's onChange fires (mirrors watchdog _set_value_directly).
    DIRECT CDP only — never goes through the event-bus readiness watchdog, so it cannot hang on a
    never-idle SPA. Returns True if the JS reported the value landed, False on miss/timeout/error.
    """

    async def _do() -> bool:
        r = await _resolve(session, node)
        if r is None:
            return False
        cdp_session, session_id, object_id = r
        got = await _call_on(cdp_session, session_id, object_id, _SET_VALUE_JS, args=[str(text)])
        # JS returns the post-set value. Success = the value LANDED. Exact match is the plain case;
        # but a FORMATTING / MASK input (phone "+1 415 555 0177" -> "+1 (415) 555-0177", a date
        # picker re-rendering "2026-09-01", a currency field) rewrites separators on input/change, so
        # an exact compare false-negatives a value that is genuinely in the field. Accept the set when
        # the readback's MEANINGFUL characters (alphanumerics, case-folded) match what we sent — the
        # mask only ever reshapes separators/whitespace, never the alphanumeric content. The downstream
        # verify oracle (DOM read-back + VLM) remains the real correctness gate; this only decides
        # "did a value land", generically, with no per-ATS branch.
        return _value_landed(got, str(text))

    return await _guarded(_do())


_MINT_CHIP_JS = r"""
function(token){
  try{
    const el=this;
    const set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
    set.call(el, token);
    try{ const tk=el._valueTracker; if(tk) tk.setValue(''); }catch(e){}
    el.dispatchEvent(new Event('input',{bubbles:true}));
    const opt={key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true};
    el.dispatchEvent(new KeyboardEvent('keydown',opt));
    el.dispatchEvent(new KeyboardEvent('keypress',opt));
    el.dispatchEvent(new KeyboardEvent('keyup',opt));
    return el.value==null ? '' : String(el.value);
  }catch(e){ return 'ERR'; }
}
"""


async def cdp_type_enter_chips(session: Any, node: Any, tokens: list[str]) -> int:
    """Mint one chip per token in a free-text tag/chip input (react-tag-input / Ashby skills box).

    Resolve the input ONCE, then per token set its .value (native setter) and dispatch the widget's
    own keydown/keypress/keyup Enter as an UNTRUSTED KeyboardEvent bound to the element. ONE resolve
    with NO get_state between tokens avoids the stale-backend-node error that dropped every chip after
    the first; the UNTRUSTED JS Enter fires the page's keydown handler yet can never trigger a native
    <form> submit. Returns the count of tokens whose .value emptied after Enter (chip box clears on
    mint)."""
    toks = [str(t) for t in (tokens or []) if str(t).strip()]
    if not toks:
        return 0
    landed = [0]

    async def _do() -> bool:
        r = await _resolve(session, node)
        if r is None:
            return False
        cdp_session, session_id, object_id = r
        for tok in toks:
            with contextlib.suppress(Exception):
                got = await _call_on(cdp_session, session_id, object_id, _MINT_CHIP_JS, args=[tok])
                if str(got or "") == "":
                    landed[0] += 1
        return True

    await _guarded(_do(), timeout=max(CDP_ACTION_TIMEOUT, 2.0 + 0.3 * len(toks)))
    return landed[0]


def _alnum_fold(s: str) -> str:
    """Meaningful characters only: alphanumerics, case-folded, separators/whitespace dropped.
    A formatting mask reshapes separators but preserves these — so two strings that fold equal are
    the same value rendered differently (phone/date/currency masks)."""
    return "".join(ch.lower() for ch in str(s) if ch.isalnum())


def _value_landed(got: Any, want: str) -> bool:
    """True if `got` (the post-set readback) is the value we set, tolerating a formatting mask.
    Exact strip-equality is the plain path; mask path = non-empty readback whose folded alphanumeric
    content equals the wanted value's. Empty readback (nothing landed) is always False."""
    if got is None:
        return False
    g = str(got).strip()
    if g == want.strip():
        return True
    if not g:
        return False  # nothing landed
    return _alnum_fold(g) == _alnum_fold(want) and _alnum_fold(want) != ""


# --------------------------------------------------------------------------- #
# PUBLIC: cdp_select — native <select> value + change. No readiness wait.
# --------------------------------------------------------------------------- #
async def cdp_select(session: Any, node: Any, text: str) -> bool:
    """Set a native <select> to the option matching `text` (by visible text or value) + dispatch
    input/change. Mirrors the watchdog select handler. Returns True on a committed match."""

    async def _do() -> bool:
        r = await _resolve(session, node)
        if r is None:
            return False
        cdp_session, session_id, object_id = r
        got = await _call_on(cdp_session, session_id, object_id, _SELECT_JS, args=[str(text)])
        return bool(got) and str(got).strip() != ""

    return await _guarded(_do())


# JS: select MULTIPLE options in a native <select multiple> — each `wants` entry (already resolved to an
# exact option text by the caller) sets option.selected=true; fires input/change once. Returns -1 when the
# element is NOT a multiple-select (so the caller falls back to single-pick, immune to a comma inside one
# option like 'San Francisco, CA'), else the count selected.
_SELECT_MULTI_JS = r"""
function(wantsJson){
  if(!this.multiple) return -1;
  const wants = JSON.parse(wantsJson);
  const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const opts = [...this.options];
  let n = 0;
  for(const w of wants){
    const wl = norm(w);
    let o = opts.find(o => norm(o.textContent)===wl || norm(o.value)===wl);
    if(!o) o = opts.find(o => { const t=norm(o.textContent); return t && (t.startsWith(wl)||wl.startsWith(t)); });
    if(!o) o = opts.find(o => { const t=norm(o.textContent); return t && (t.includes(wl)||wl.includes(t)); });
    if(o && !o.selected){ o.selected = true; n++; }
  }
  if(n){ this.dispatchEvent(new Event('input',{bubbles:true})); this.dispatchEvent(new Event('change',{bubbles:true})); }
  return n;
}
"""


_LISTBOX_CLICK_JS = r"""
function(valuesJson){
  const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const wants = JSON.parse(valuesJson).map(norm).filter(Boolean);
  // resolve the listbox: the node itself, its ancestor, its aria-controls/owns target, else page.
  let box = (this.matches && this.matches('[role=listbox]')) ? this
          : (this.closest ? this.closest('[role=listbox]') : null);
  if(!box){ const lid = this.getAttribute && (this.getAttribute('aria-controls')||this.getAttribute('aria-owns')); if(lid) box = document.getElementById(lid); }
  if(!box) box = document.querySelector('[role=listbox]');
  if(!box) return 0;
  const opts = [...box.querySelectorAll('[role=option]')];
  let n = 0;
  for(const w of wants){
    let o = opts.find(x => norm(x.innerText||x.textContent) === w);
    if(!o) o = opts.find(x => { const t = norm(x.innerText||x.textContent); return t && (t.includes(w)||w.includes(t)); });
    if(o){ o.scrollIntoView({block:'nearest'});
      for(const ev of ['pointerdown','mousedown','mouseup','click']) o.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
      n++; }
  }
  return n;
}
"""


_DUAL_LB_JS = "() => { const label = __LABEL__, wants = __VALUES__;" + r"""
  const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const labLow = norm(label).replace(/[*:?]/g,'').trim();
  // the dual-listbox container = >=2 item lists + an add/move-right button. Prefer the one whose text
  // carries the field label; take the smallest such container.
  const conts = [...document.querySelectorAll('div,fieldset,section,[role=group]')].filter(c => {
    const lists = c.querySelectorAll('ul,[role=listbox]');
    const hasAdd = [...c.querySelectorAll('button,[role=button]')].some(b =>
      /add|move|select|transfer|→|›|»|>/i.test((b.getAttribute('aria-label')||'')+' '+(b.className||'')+' '+(b.innerText||'')));
    return lists.length>=2 && hasAdd && c.children.length<40;
  }).sort((a,b)=>(a.innerText||'').length-(b.innerText||'').length);
  const cont = (labLow.length>=4 && conts.find(c=>norm(c.innerText).includes(labLow.slice(0,20)))) || conts[0];
  if(!cont) return 0;
  const addBtn = [...cont.querySelectorAll('button,[role=button]')].find(b =>
    /(^|[^a-z])(add|move|select|transfer)([^a-z]|$)|→|›|»/i.test((b.getAttribute('aria-label')||'')+' '+(b.className||'')+' '+(b.innerText||'')));
  if(!addBtn) return 0;
  let n = 0;
  for(const w of wants){
    if(!w) continue;
    const li = [...cont.querySelectorAll('li,[role=option]')].find(x => {
      const t = norm((x.dataset && x.dataset.val) ? x.dataset.val : (x.innerText||x.textContent));
      return t && (t===w || t.includes(w) || w.includes(t)); });
    if(!li) continue;
    li.scrollIntoView({block:'nearest'});
    for(const ev of ['pointerdown','mousedown','mouseup','click']) li.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
    for(const ev of ['pointerdown','mousedown','mouseup','click']) addBtn.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
    n++;
  }
  return n; }"""


async def cdp_dual_listbox_transfer(page: Any, label: str, values: list) -> int:
    """Two-panel transfer widget (bootstrap-duallistbox / PrimeFaces PickList): for each value, click its
    item in the source list + click the add/move-right button to move it to the selected panel. Anchored
    by the field LABEL. Returns the count moved; _s_verify re-checks the Selected panel (no false-green)."""
    import json as _json

    with contextlib.suppress(Exception):
        js = _DUAL_LB_JS.replace("__LABEL__", _json.dumps(str(label or ""))).replace(
            "__VALUES__", _json.dumps([str(v).lower() for v in values]))
        got = await page.evaluate(js)
        try:
            return int(got or 0)
        except Exception:
            return 0
    return 0


async def cdp_click_listbox_options(session: Any, node: Any, values: list) -> int:
    """Click matching [role=option] cells inside an ARIA role=listbox (one per value token). Returns the
    count clicked; _s_verify then re-checks the real aria-selected state so this cannot false-green."""
    async def _do() -> int:
        r = await _resolve(session, node)
        if r is None:
            return 0
        cdp_session, session_id, object_id = r
        import json as _json
        got = await _call_on(cdp_session, session_id, object_id, _LISTBOX_CLICK_JS, args=[_json.dumps([str(v) for v in values])])
        try:
            return int(got)
        except Exception:
            return 0

    try:
        return int(await asyncio.wait_for(_do(), timeout=CDP_ACTION_TIMEOUT))
    except Exception:
        return 0


async def cdp_select_multiple(session: Any, node: Any, texts: list) -> int:
    """Select several options in a native <select multiple> (each an exact option text) + fire change.
    Returns -1 if the element is not a multiple-select, else the count selected."""
    async def _do() -> int:
        r = await _resolve(session, node)
        if r is None:
            return 0
        cdp_session, session_id, object_id = r
        import json as _json
        got = await _call_on(cdp_session, session_id, object_id, _SELECT_MULTI_JS, args=[_json.dumps([str(t) for t in texts])])
        try:
            return int(got)
        except Exception:
            return 0

    # NOT via _guarded — it bool()-coerces the return, which would turn the -1 "not-a-multiple-select"
    # sentinel into True (bool(-1)) and false-green a non-native listbox. Preserve the raw int.
    try:
        return int(await asyncio.wait_for(_do(), timeout=CDP_ACTION_TIMEOUT))
    except Exception:
        return 0


# JS: within `this` (the card/group container) find the radio/checkbox input whose VALUE attr (or its
# wrapping <label> text) matches `want`, exact then substring, click it + fire input/change (React).
# Mirrors the proven ats_lever._click_option: Lever radios are REAL inputs (often visually HIDDEN behind
# a styled label) carrying value="<option label>" — invisible to a visible-only scan but reachable in the
# full DOM. Returns the matched value string, or "" when nothing matched. Generic standard-DOM.
_CHOOSE_OPTION_JS = r"""
function(want, groupName){
  const norm = s => (s||'').replace(/\s+/g,'').toLowerCase();
  const vis = e => norm((e.innerText!=null && e.innerText.trim()) ? e.innerText : (e.textContent||''));
  const w = norm(want);
  if(!w) return "";
  // IDENTITY-SCOPED: discovery captured the radio group's `name` attr — that names the exact
  // input set document-wide, immune to a mis-located container (live: a spatially-bound wrong
  // card made the container scan miss and the visual path answered a NEIGHBOUR question).
  let inputs = [];
  if(groupName){
    const esc = (window.CSS && CSS.escape) ? CSS.escape(groupName) : groupName;
    inputs = [...document.querySelectorAll('input[type=radio][name="'+esc+'"],input[type=checkbox][name="'+esc+'"]')];
  }
  if(!inputs.length){
    // bound to a LONE radio/checkbox input (dom-ref locate)? widen to its GROUP: the enclosing
    // fieldset/radiogroup, else all same-name inputs in the form/document.
    let root = this;
    if(root.matches && root.matches('input[type=radio],input[type=checkbox]')){
      root = root.closest('fieldset,[role=radiogroup],[role=group]') || root.form || document;
    }
    inputs = [...root.querySelectorAll('input[type=radio],input[type=checkbox]')];
    if(this !== root && this.name) inputs = inputs.filter(el => el.name === this.name);
  }
  // BUTTON-PILL GROUP (ashby mega/38-39/54 'Yes'/'No' pills): the options are literal <button>s /
  // role=button. Tried when NO radio/checkbox input exists — and ALSO when the inputs exist but
  // none matched (replo mega/54: the pills hide value='on' checkboxes with NO resolvable label,
  // so the input matcher came up empty and the button branch was unreachable).
  const tryButtons = () => {
    let root = this;
    if(root.matches && root.matches('input,button')) root = root.closest('fieldset,[role=group],[role=radiogroup]') || root.parentElement || root;
    const btns = [...root.querySelectorAll('button,[role=button]')].filter(b => {
      const ty=(b.getAttribute('type')||'').toLowerCase();
      if(ty==='submit') return false;
      const t=vis(b); return t && t.length<=30 && !/submit|apply|upload|replace|next|continue/.test(t);
    });
    const hit = btns.find(b => vis(b)===w);
    if(hit){ hit.click();
      hit.dispatchEvent(new Event('input',{bubbles:true}));
      hit.dispatchEvent(new Event('change',{bubbles:true}));
      return (hit.innerText||'').trim() || want; }
    return "";
  };
  if(!inputs.length) return tryButtons();
  // LONE checkbox + an affirmative want -> check it (a consent box has no per-option labels to
  // match; the mapper already decided this field gets a value). Explicit negatives leave it be.
  if(inputs.length===1 && inputs[0].type==='checkbox' && !['no','false','none','0'].includes(w)){
    const t=inputs[0];
    // HIDDEN RENDER-MIRROR CHECKBOX (ashby yes-no pill, airwallex AI-Policy): a zero-box /
    // tabindex=-1 / display:none checkbox is a ONE-WAY mirror — the visible option BUTTONS drive
    // the paint, and clicking the mirror never repaints them (both pills stay grey while .checked
    // reads true => the false-green). When such a mirror sits beside matching option buttons,
    // commit the BUTTON (it paints); on no button match return "" (never bless the dead mirror).
    // A truly hidden LONE consent box (sr-only, no sibling buttons) still gets clicked below.
    const bx = t.getBoundingClientRect();
    const mirror = bx.width < 2 || bx.height < 2 || t.tabIndex === -1
      || getComputedStyle(t).visibility === 'hidden' || getComputedStyle(t).display === 'none';
    if(mirror){
      // STRUCTURE, not text: a pill group has sibling option BUTTONS; an sr-only lone consent box
      // does not. Any non-submit button in the tight group container marks this as a pill group.
      let broot = t.closest('fieldset,[role=group],[role=radiogroup]') || t.parentElement || t;
      const hasBtns = broot.querySelectorAll && [...broot.querySelectorAll('button,[role=button]')]
        .some(b => (b.getAttribute('type')||'').toLowerCase() !== 'submit');
      if(hasBtns) return tryButtons();
    }
    if(!t.checked){ t.click(); t.dispatchEvent(new Event('input',{bubbles:true})); t.dispatchEvent(new Event('change',{bubbles:true})); }
    return t.checked ? (t.getAttribute('value')||t.value||'checked') : "";
  }
  const valOf = el => norm(el.getAttribute('value')||el.value||'');
  // el.labels covers BOTH a wrapping <label> and a sibling <label for=id> (teamtailor pills use
  // the sibling shape — closest('label') missed them and the commit fell through to visual).
  const labOf = el => { const L = (el.labels && el.labels[0]) || el.closest('label');
    let t = L?vis(L):''; if(!t) t = norm(el.getAttribute('aria-label')||''); return t; };
  // the option text often lives OUTSIDE the <label> and is wired via aria-labelledby on the
  // styled [role=radio|checkbox|option] wrapper (workable) — resolve each referenced id's text
  // as an INDIVIDUAL candidate (joined they'd include the question text and match nothing).
  const ariaTexts = el => {
    const host = el.closest('[role=radio],[role=checkbox],[role=option],[data-ui=option]');
    const ids = ((host&&host.getAttribute('aria-labelledby'))||'').split(/\s+/).filter(Boolean);
    return ids.map(i => { const e=document.getElementById(i); return e?vis(e):''; }).filter(Boolean);
  };
  // GENERIC value attrs ('on' — the browser default when markup sets none — 'true', '1')
  // discriminate NOTHING: every input in the group carries the same one (ashby mega/37
  // committed literal 'on'). They never participate in matching, and el_val prefers the
  // option's LABEL so the ledger records what a human reads, not the submit payload.
  const generic = v => ['on','true','1'].includes(v);
  let t = inputs.find(el => { const v=valOf(el); return v && !generic(v) && v===w; })
       || inputs.find(el => labOf(el)===w)
       || inputs.find(el => ariaTexts(el).some(x => x===w));
  if(!t) t = inputs.find(el => { const v=valOf(el); return v && !generic(v) && (v.includes(w)||w.includes(v)); });
  if(!t) t = inputs.find(el => { const l=labOf(el); return l && (l.includes(w)||w.includes(l)); });
  if(!t) return tryButtons();
  // a VISUALLY-HIDDEN input's widget updates its rendered state from the LABEL's native click
  // forwarding (teamtailor dropdown-as-radios: clicking the hidden input checked it but left the
  // trigger text on its placeholder). Prefer the label when the input has no box.
  const box = t.getBoundingClientRect();
  const L = (t.labels && t.labels[0]) || null;
  if (L && (box.width < 2 || box.height < 2)) { L.click(); } else { t.click(); }
  t.dispatchEvent(new Event('input',{bubbles:true}));
  t.dispatchEvent(new Event('change',{bubbles:true}));
  // the click is only a COMMIT if the control actually took it — a controlled widget can swallow
  // .click() and leave checked=false (then the caller must fall through, not report success).
  if(!t.checked) return "";
  return el_val(t);
  function el_val(el){ const v=el.getAttribute('value')||el.value||'';
    return labOf(el) || (generic(v) ? '' : v) || want; }
}
"""


    # a controlled widget can accept the click NOW and revert on the next render — sierra
# mega4/48 hear-about: t.checked was true at click time, the ledger said DONE, and the
# final screenshot showed the whole group unselected. Settle, then re-read the group.
_STILL_CHECKED_JS = r"""
function(groupName){
  let inputs = [];
  if(groupName){
const esc = (window.CSS && CSS.escape) ? CSS.escape(groupName) : groupName;
inputs = [...document.querySelectorAll('input[type=radio][name="'+esc+'"],input[type=checkbox][name="'+esc+'"]')];
  }
  if(!inputs.length){
let root = this;
if(root.matches && root.matches('input[type=radio],input[type=checkbox]')){
  root = root.closest('fieldset,[role=radiogroup],[role=group]') || root.form || document;
}
inputs = [...root.querySelectorAll('input[type=radio],input[type=checkbox]')];
  }
  if(inputs.some(el => el.checked)) return true;
  // ASHBY PILL (live-CDP-confirmed on 1password): the option is a <button> whose hidden
  // checkbox lags on React re-render — but the button itself carries an ACTIVE/SELECTED class
  // the instant it commits ('_active_', 'selected', 'checked', aria-pressed/checked=true). Read
  // that visual state too, so a lagging checkbox no longer makes the commit look reverted (which
  // sent the field to the visual path -> recommit-toggle-thrash -> commit-cap, 1password 'people
  // managers'). Scope to the SAME group container as the checkbox.
  let root = this;
  if(root.matches && root.matches('input')) root = root.closest('fieldset,[role=group],[role=radiogroup]') || (inputs[0] && inputs[0].closest('div')) || root.form || document;
  else if(inputs.length) root = inputs[0].closest('fieldset,[role=group],[role=radiogroup],div') || document;
  const activeBtn = [...root.querySelectorAll('button,[role=radio],[role=checkbox],[role=option]')].some(b => {
    const c = String(b.className||'');
    return /(^|[^a-z])(active|selected|checked|_active_)([^a-z]|$)/i.test(c)
      || b.getAttribute('aria-pressed') === 'true' || b.getAttribute('aria-checked') === 'true'
      || b.getAttribute('data-state') === 'checked' || b.getAttribute('data-selected') === 'true';
  });
  if(activeBtn) return true;
  // RETIRED accept-on-unknown: "no readable input AND no active button -> trust the commit" blessed
  // a dead render-mirror checkbox (both pills grey) as committed. Fail CLOSED — an unconfirmed
  // commit returns "" from cdp_choose_option and falls through to the visual set-of-marks path,
  // which SEES + clicks the painted option and self-verifies on real selected-state.
  return false;
}
"""


# JS (bare arrow, baked args): commit an ashby-style pill BUTTON group the cdp_choose_option container
# scan missed (live-verified on apolink: the 'No' option is a <button>, clicking it adds `_active_1svni_57`).
# ANCHOR to the field's own entry container by LABEL text (not page-wide -> no cross-field bleed), click
# the button whose text == value, and return the value ONLY if the button ends in an active/selected
# state -> self-verifying, so it cannot false-green an unselected pill.
# shared prologue: resolve the field-entry container BY LABEL, then the target OPTION control — either
# an ashby-style pill <button>/role widget OR a native <input type=radio|checkbox> (matched by its
# associated <label> / value). Assignment-only (no early return) so CLICK returns text and VERIFY can
# report found-ness. `hit` is the control to test for selected-state; `clickTarget` the thing to click
# (a native input's own <label>, which drives React's onChange); `isInput` switches active-detection to
# the real .checked property (a native radio has no _active_ class — trusting label-text there false-greens).
_BTN_FIND = r"""
  const low = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const norm2 = s => (s||'').replace(/\s+/g,'').toLowerCase();
  const w = norm2(want); const labLow = low(label).replace(/[*:]/g,'').trim();
  let hit=null, clickTarget=null, isInput=false, cont=null;
  if(w && labLow.length >= 4){
    // MATCH BY QUESTION STEM, not the whole label: discovery can append option text onto the label
    // ('Do you require sponsorship? * Yes No'), and a startsWith(full-label) then never finds the
    // field's own container. Take the stem (text before the first '?', else the first 40 chars) and
    // accept any small container whose text CONTAINS it (tolerant of leading/trailing junk on either
    // side); the shortest such container is the field entry.
    const _pre = labLow.split('?')[0].trim();
    const stem = (_pre.length >= 6 ? _pre : labLow).slice(0,40);
    const cands = [...document.querySelectorAll('div,fieldset,section')].filter(e => {
      const t = low(e.innerText); return e.children.length < 40 && t && (t.includes(stem) || t.startsWith(labLow.slice(0,40)));
    }).sort((a,b) => (a.innerText||'').length - (b.innerText||'').length);
    cont = cands[0] || null;
  }
  if(cont){
    const btns = [...cont.querySelectorAll('button,[role=button],[role=radio],[role=option]')].filter(b => {
      const ty=(b.getAttribute('type')||'').toLowerCase(); if(ty==='submit') return false;
      const t=norm2(b.innerText); return t && t.length<=30 && !/submit|apply|upload|replace|next|continue/.test(t); });
    hit = btns.find(b => norm2(b.innerText)===w) || null;
    clickTarget = hit;
    if(!hit){
      // native radio/checkbox — the option a grouped-locate desync mis-committed (ledger DONE, radio
      // blank on screen). Re-find it here BY LABEL, immune to the bad container.
      const esc = id => (window.CSS && CSS.escape) ? CSS.escape(id) : id;
      const _raw = inp => { let t='';
        if(inp.id){ const l=cont.querySelector('label[for="'+esc(inp.id)+'"]'); if(l) t=l.innerText; }
        if(!t){ const p=inp.closest('label'); if(p) t=p.innerText; }
        if(!t) t=inp.getAttribute('aria-label')||inp.value||''; return t; };
      const lblText = inp => norm2(_raw(inp));
      const lblSp = inp => low(_raw(inp));   // spaced -> real word boundaries
      const inps = [...cont.querySelectorAll('input[type=radio],input[type=checkbox]')];
      let inp = inps.find(i => lblText(i)===w);
      if(!inp){
        // WORD-BOUNDARY PREFIX before any substring: 'No' -> the option STARTING with the word 'No'
        // ('No, I do not have a disability...'), never a substring hit inside 'I do NOt want to answer'
        // (the CC-305 false-pick). No length cap here — the correct option is often a long clause.
        const wl = low(want);
        const re = new RegExp('^' + wl.replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + '\\b');
        inp = inps.find(i => re.test(lblSp(i)));
      }
      if(!inp) inp = inps.find(i => { const t=lblText(i); return t && t.length<=30 && (t.includes(w)||w.includes(t)); });
      if(inp){ hit=inp; isInput=true;
        clickTarget = (inp.id && cont.querySelector('label[for="'+esc(inp.id)+'"]')) || inp.closest('label') || inp; }
    }
  }
  const _active = el => (el.getAttribute('aria-checked')==='true' || el.getAttribute('aria-pressed')==='true'
    || el.getAttribute('data-state')==='checked'
    || /(^|[^a-z])(active|selected|checked|_active_)([^a-z]|$)/i.test(String(el.className||'')));
  // COMMITTED TEXT = the option's VISIBLE LABEL, never input.value. A native radio's value attr is
  // usually the generic 'on' (Ashby EEO groups), so returning it makes committed='on' -> the want-vs-got
  // completeness veto false-REDs a correctly-selected radio (want 'Woman' got 'on'). The label[for] /
  // wrapping <label> / aria-label is the human-meaningful text that the veto compares against ctx.value.
  const _labelOf = el => {
    if(!el) return '';
    if(isInput){
      let t='';
      if(el.id){ const _e=(window.CSS&&CSS.escape)?CSS.escape(el.id):el.id;
        const l=(cont||document).querySelector('label[for="'+_e+'"]'); if(l) t=l.innerText; }
      if(!t){ const p=el.closest('label'); if(p) t=p.innerText; }
      if(!t) t=el.getAttribute('aria-label')||'';
      return (t||'').replace(/\s+/g,' ').trim();
    }
    return (el.innerText||'').replace(/\s+/g,' ').trim();
  };
"""
# STEP 1 — click the option (pill: pointer sequence; native input: click its label + fire change).
# Returns the option text (the click landed), "" when nothing matched.
_BTN_CLICK_JS = "() => { const label = __LABEL__, want = __WANT__;" + _BTN_FIND + r"""
  if(!hit) return "";
  if(isInput){
    if(hit.checked) return (_labelOf(hit)||want);             // already selected -> don't re-toggle
    try{ hit.scrollIntoView({block:'center'}); }catch(_){}
    try{ clickTarget.click(); }catch(_){}
    if(!hit.checked){ try{ hit.checked = true; }catch(_){} }   // belt: force + fire so React onChange commits
    hit.dispatchEvent(new Event('input',{bubbles:true}));
    hit.dispatchEvent(new Event('change',{bubbles:true}));
    return (_labelOf(hit)||want);
  }
  if(_active(hit)) return (hit.innerText||'').trim() || want;  // already selected -> don't re-click (would TOGGLE off)
  try{ hit.scrollIntoView({block:'center'}); }catch(_){}
  for(const ev of ['pointerdown','mousedown','mouseup','click']) hit.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
  return (hit.innerText||'').trim() || want; }"""
# STEP 2 — AFTER a tick, is that same option now really selected? React applies state on the next tick,
# so this MUST run in a separate evaluate after an await (a same-evaluate check read stale every time).
# Returns JSON {v: committed-text-or-"", found: a matching control existed} so the caller can tell a
# real Lever styled-DIV (found=false -> trust the prior click) from a false-green native radio the click
# didn't take (found=true, v="" -> do NOT trust the ledger; re-commit via the visual/group path).
_BTN_VERIFY_JS = "() => { const label = __LABEL__, want = __WANT__;" + _BTN_FIND + r"""
  if(!hit) return JSON.stringify({v:"", found:false});
  const ok = isInput ? !!hit.checked : _active(hit);
  const v = ok ? (_labelOf(hit)||want) : "";
  return JSON.stringify({v:v, found:true}); }"""


# JS (bare arrow, __LABEL__/__WANT__ baked via .replace — same pattern as _BTN_CLICK_JS): commit the two
# GROUPED widgets whose option is resolvable only by STRUCTURAL ASSOCIATION or ORDINAL position, never by
# free option-text. (1) MATRIX CELL (Likert): a radio whose aria-labelledby references >=2 headers (row +
# column) — the shared column label ('Strongly agree') repeats every row, so a text match checks the WRONG
# row; bind by rowHeader-text==label AND colHeader-text==value. (2) ORDINAL SCALE (star rating): N sibling
# <button>/[role=radio] in a [role=group] labelled by this field's label; the integer value is the ordinal —
# click the Nth (options carry the star glyph, not the digit). Self-verifying: returns v only when the
# control ends really selected (.checked / aria-pressed / aria-checked / on|active|selected|checked class),
# so it can never false-green; m marks that the exact structure was found (caller must NOT fall to the text
# path when m && !v — that is the shared-column false-green).
_GRID_SCALE_JS = "() => { const label = __LABEL__, want = __WANT__;" + r"""
  const low = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const norm = s => (s||'').replace(/\s+/g,'').toLowerCase();
  const L = low(label).replace(/[*:]/g,'').trim();
  const W = norm(want);
  const idText = id => { const e = id && document.getElementById(id); return e ? low(e.innerText||e.textContent) : ''; };
  const rowMatch = t => !!t && (t===L || (L.length>8 && (t.includes(L)||L.includes(t))));
  // MATRIX CELL: a radio whose aria-labelledby references BOTH a row header and a column header.
  const cells = [...document.querySelectorAll('input[type=radio][aria-labelledby],[role=radio][aria-labelledby]')];
  for(const r of cells){
    const ids = (r.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
    if(ids.length < 2) continue;
    const texts = ids.map(idText);
    if(!(texts.some(rowMatch) && texts.some(t => norm(t)===W))) continue;
    if(!r.checked){ try{ r.scrollIntoView({block:'center'}); }catch(_){}
      try{ r.click(); }catch(_){} if(!r.checked){ try{ r.checked = true; }catch(_){} }
      r.dispatchEvent(new Event('input',{bubbles:true})); r.dispatchEvent(new Event('change',{bubbles:true})); }
    return JSON.stringify({v: r.checked ? want : '', m: true});
  }
  // ORDINAL RATING SCALE: value is a bare integer N -> click the Nth sibling rating control.
  const s = String(want).trim(); const n = parseInt(s,10);
  if(String(n)===s && n>=1){
    for(const g of [...document.querySelectorAll('[role=group],[role=radiogroup]')]){
      const gl = idText(g.getAttribute('aria-labelledby')) || low(g.getAttribute('aria-label'));
      if(!rowMatch(gl)) continue;
      const opts = [...g.querySelectorAll('button,[role=radio]')].filter(b => (b.getAttribute('type')||'').toLowerCase()!=='submit');
      if(opts.length < 3 || n > opts.length) continue;
      const ordinal = opts.every((b,i) => { const dv=b.getAttribute('data-v');
        return (dv && parseInt(dv,10)===i+1) || /^\s*\d+\b/.test(low(b.getAttribute('aria-label'))); });
      if(!ordinal) continue;
      const t = opts[n-1];
      try{ t.scrollIntoView({block:'center'}); }catch(_){}
      try{ t.click(); }catch(_){}
      const on = t.getAttribute('aria-pressed')==='true' || t.getAttribute('aria-checked')==='true'
        || /(^|[^a-z])(on|active|selected|checked)([^a-z]|$)/i.test(String(t.className||''));
      return JSON.stringify({v: on ? s : '', m: true});
    }
  }
  return JSON.stringify({v:'', m:false}); }"""


async def cdp_commit_grid_or_scale(page: Any, label: str, value: str) -> tuple[str, bool]:
    """Commit a Likert MATRIX CELL or an ordinal RATING SCALE — the two grouped widgets whose option is
    resolvable only by STRUCTURAL association (aria-labelledby row x column) or ORDINAL position, never by
    free option-text (the shared column label repeats every row; the star glyph never equals the digit).
    Self-verifying: returns ``(committed_text, matched)`` where committed non-empty == the control ended
    really selected (.checked / aria-pressed / aria-checked). ``('', True)`` == the exact cell/scale was
    found but the click did not take (caller must NOT fall to the text path -> shared-column false-green).
    ``('', False)`` == not one of these structures (caller continues normal classify). Generic; no per-ATS
    string, no heading-text heuristic beyond the accessible row/col/group name association."""
    import json as _json

    with contextlib.suppress(Exception):
        js = _GRID_SCALE_JS.replace("__LABEL__", _json.dumps(str(label or ""))).replace(
            "__WANT__", _json.dumps(str(value or "")))
        raw = await page.evaluate(js)
        try:
            res = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            res = {}
        return (str(res.get("v") or "").strip(), bool(res.get("m")))
    return ("", False)


async def cdp_choose_button_by_label(page: Any, label: str, value: str) -> tuple[str, bool]:
    """Commit an ashby pill button OR native radio group anchored by the field's LABEL (field-scoped,
    no bleed). Clicks the option, then AFTER a tick re-checks it ended really selected (active class for
    a pill, .checked for a native input) — never label text, so it cannot false-green. Returns
    ``(committed_text, found)``: ``committed_text`` non-empty == real commit; ``found`` == a matching
    control existed in the field container. ``("", True)`` is the caught false-green (control present but
    unselected); ``("", False)`` == no anchorable control (e.g. Lever styled-DIV — caller trusts its own
    verified click). Split click/verify because React applies state on the next tick."""
    import asyncio as _a
    import json as _json

    with contextlib.suppress(Exception):
        def _b(t):
            return t.replace("__LABEL__", _json.dumps(str(label or ""))).replace("__WANT__", _json.dumps(str(value or "")))

        clicked = await page.evaluate(_b(_BTN_CLICK_JS))
        if not str(clicked or "").strip():
            return ("", False)  # nothing matched -> no control here
        await _a.sleep(0.3)
        raw = await page.evaluate(_b(_BTN_VERIFY_JS))
        try:
            res = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            res = {}
        return (str(res.get("v") or "").strip(), bool(res.get("found")))
    return ("", False)


async def cdp_choose_option(session: Any, container_node: Any, value: str, group_name: str = "") -> str:
    """Commit a radio/checkbox GROUP the proven Lever way: scan the container's REAL inputs (incl.
    visually-hidden ones a visible-only selector_map misses), match the one whose VALUE attr / wrapping
    <label> / aria-labelledby text means ``value``, ``.click()`` it + fire input/change. When
    ``group_name`` (discovery's name attr for the group) is given, the input set is resolved by that
    IDENTITY document-wide first — immune to a mis-located container. Returns the matched option
    string, or "" when no input matched (caller falls back to the visual path). Generic."""


    async def _do() -> str:
        r = await _resolve(session, container_node)
        if r is None:
            return ""
        cdp_session, session_id, object_id = r
        for attempt in (1, 2):
            got = await _call_on(cdp_session, session_id, object_id, _CHOOSE_OPTION_JS, args=[str(value), str(group_name or "")])
            if not got:
                return ""
            await asyncio.sleep(0.3)
            still = await _call_on(cdp_session, session_id, object_id, _STILL_CHECKED_JS, args=[str(group_name or "")])
            if still is not False:
                return str(got).strip()
            if attempt == 1:
                print("   [choice] commit reverted by re-render — one re-click")
        return ""

    try:
        return await asyncio.wait_for(_do(), timeout=CDP_ACTION_TIMEOUT + 1.0)
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        return ""
    except Exception:
        return ""


# Read every option CONTROL in the card (pill button / [role=radio|checkbox|option] / native input)
# with its visible text, LIVE viewport-rect center, and painted SELECTED state. Bound to `this` = the
# card container. A visually-hidden styled input uses its <label>'s rect (that is what a human clicks).
# Painted state is the real signal a DOM-echo commit lies about: aria-checked/pressed, data-state,
# .checked, or an active/selected class token. Used by the choice paint-confirm + trusted-click repair.
_CHOICE_OPTIONS_JS = r"""
function(groupName){
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const active = el => (el.getAttribute && (el.getAttribute('aria-checked')==='true'
      || el.getAttribute('aria-pressed')==='true' || el.getAttribute('data-state')==='checked'
      || el.getAttribute('data-selected')==='true'))
    || (el.matches && el.matches('input') && el.checked)
    || /(^|[^a-z])(active|selected|checked|_active_)([^a-z]|$)/i.test(String(el.className||''));
  const OPT = 'button,[role=button],[role=radio],[role=checkbox],[role=option],input[type=radio],input[type=checkbox]';
  // IDENTITY-SCOPE first (immune to a mislocated ctx.node — live airwallex bound an unrelated
  // input[role=combobox], not the pill group): if the field's group `name` is known, find that
  // control document-wide and use ITS enclosing group as the root. Else climb from `this` to the
  // nearest option-bearing container (a grouped locate can bind the bare hidden input).
  let root = this;
  if(groupName){
    const esc = (window.CSS && CSS.escape) ? CSS.escape(groupName) : groupName;
    const g = document.querySelector('[name="'+esc+'"]');
    if(g) root = g.closest('fieldset,[role=group],[role=radiogroup]') || g.parentElement || g;
  }
  for(let i=0;i<4 && root && !(root.querySelector && root.querySelector(OPT)); i++) root = root.parentElement;
  if(!root || !root.querySelectorAll) return '[]';
  const ctrls = [...root.querySelectorAll(OPT)];
  const out = [], seen = new Set();
  for(const el of ctrls){
    const ty=((el.getAttribute&&el.getAttribute('type'))||'').toLowerCase(); if(ty==='submit') continue;
    let t = norm(el.innerText||el.textContent);
    let box = el.getBoundingClientRect();
    if(el.matches && el.matches('input')){
      const L=(el.labels&&el.labels[0])||el.closest('label');
      if(L){ t = norm(L.innerText)||t; const lr=L.getBoundingClientRect(); if(lr.width>2&&lr.height>2) box=lr; }
      // a GENERIC value ('on'/'true'/'1' — the browser default a render-mirror checkbox carries) is
      // NOT an option label; leaving t empty drops the mirror so it is not counted as an active option.
      if(!t){ const v=norm(el.getAttribute('aria-label')||el.value); if(!['on','true','1','checked'].includes(v.toLowerCase())) t=v; }
    }
    if(!t || t.length>80) continue;
    if(box.width<2 || box.height<2) continue;
    const key = t.toLowerCase(); if(seen.has(key)) continue; seen.add(key);
    out.push({text:t, x:Math.round(box.left+box.width/2), y:Math.round(box.top+box.height/2), active:!!active(el)});
  }
  return JSON.stringify(out);
}
"""


async def cdp_read_choice_options(session: Any, card_node: Any, group_name: str = "") -> list[dict]:
    """The field's option controls as ``[{text, x, y, active}]`` — visible text, LIVE viewport-rect
    center, and painted selected-state. When ``group_name`` (the field's group ``name``) is given, the
    group is resolved by that IDENTITY document-wide — immune to a mislocated ``card_node`` (live
    airwallex bound an unrelated input[role=combobox]); else it climbs from ``card_node`` to the
    nearest option-bearing container. For the choice paint-confirm + trusted-coordinate-click repair."""
    with contextlib.suppress(Exception):
        r = await _resolve(session, card_node)
        if r is None:
            return []
        cdp_session, session_id, object_id = r
        raw = await _call_on(cdp_session, session_id, object_id, _CHOICE_OPTIONS_JS, args=[str(group_name or "")])
        opts = json.loads(raw) if raw else []
        return opts if isinstance(opts, list) else []
    return []


# JS: within `this` (the card/group container) find a native <select> and set its option matching `want`
# (text or value, exact then substring) by selectedIndex + fire input/change. Mirrors the proven
# ats_lever._select_native: browser-use's select_option matches by VALUE via CDP and SILENTLY no-ops on
# Lever's React selects; selectedIndex + a dispatched change is the deterministic path. Returns the
# committed option text, or "".
_SELECT_IN_CONTAINER_JS = r"""
function(want){
  const norm = s => (s||'').replace(/\s+/g,'').toLowerCase();
  const w = norm(want);
  if(!w) return "";
  const sels = [...this.querySelectorAll('select')];
  for(const e of sels){
    const opts = [...e.options];
    let idx = opts.findIndex(o => norm(o.textContent)===w || norm(o.value)===w);
    if(idx<0) idx = opts.findIndex(o => { const t=norm(o.textContent); return t && (t.includes(w)||w.includes(t)); });
    if(idx<0) continue;
    e.selectedIndex = idx;
    e.dispatchEvent(new Event('input',{bubbles:true}));
    e.dispatchEvent(new Event('change',{bubbles:true}));
    return opts[idx].textContent || opts[idx].value || want;
  }
  return "";
}
"""


async def cdp_select_in_container(session: Any, container_node: Any, value: str) -> str:
    """Commit a native <select> the proven Lever way: find the <select> in the container, set its option
    matching ``value`` (text/value, exact then substring) by selectedIndex + fire input/change (React).
    Returns the committed option text, or "" when no <select>/option matched. Generic; no per-ATS hook."""

    async def _do() -> str:
        r = await _resolve(session, container_node)
        if r is None:
            return ""
        cdp_session, session_id, object_id = r
        got = await _call_on(cdp_session, session_id, object_id, _SELECT_IN_CONTAINER_JS, args=[str(value)])
        return str(got).strip() if got else ""

    try:
        return await asyncio.wait_for(_do(), timeout=CDP_ACTION_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        return ""
    except Exception:
        return ""


# JS pair for ARIA comboboxes (react-select/downshift/MUI family). The listbox they point at via
# aria-owns/aria-controls UNMOUNTS when closed (verified live on workable), and any state read
# between the open-click and the option-click can blur-close it — so the helper is SELF-SUFFICIENT:
# one call finds the combobox (this, descendants, then up to 4 ancestors' subtrees) and opens it if
# collapsed; after a short mount wait a second call clicks the [role=option] matching `want`.
_ARIA_COMBO_FIND = r"""
  const findCombo = (root) => {
    const sel = '[role=combobox][aria-owns],[role=combobox][aria-controls],[aria-haspopup=listbox][aria-owns],[aria-haspopup=listbox][aria-controls],[role=combobox][aria-haspopup=listbox],[aria-haspopup=listbox]';
    if(root.matches && root.matches(sel)) return root;
    let c = root.querySelector(sel);
    if(c) return c;
    // ancestor walk: accept ONLY an unambiguous match — a broad ancestor holds OTHER fields'
    // comboboxes (live failure: a rating field matched the phone country-code list and
    // committed 'United Kingdom+44').
    let up = root;
    for(let i=0;i<4 && up;i++){
      up = up.parentElement;
      if(!up) break;
      const cs = up.querySelectorAll(sel);
      if(cs.length === 1) return cs[0];
      if(cs.length > 1) return null;
    }
    return null;
  };
"""

_ARIA_OPEN_JS = (
    r"""
function(){
"""
    + _ARIA_COMBO_FIND
    + r"""
  const c = findCombo(this);
  if(!c) return "";
  if(c.getAttribute('aria-expanded') !== 'true'){
    c.scrollIntoView({block:'center'});
    // the open handler may live on an inner <button> trigger (Workday selectWidget); a click on the
    // combobox WRAPPER never fires it. Open the inner button when present, else the combobox itself.
    const opener = c.querySelector('button') || c;
    for(const ev of ['mousedown','mouseup','click'])
      opener.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true}));
  }
  return "found";
}
"""
)

_ARIA_PICK_JS = (
    r"""
function(want){
  const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const w = norm(want);
  if(!w) return "";
"""
    + _ARIA_COMBO_FIND
    + r"""
  const c = findCombo(this);
  if(!c) return "";
  const id = c.getAttribute('aria-owns') || c.getAttribute('aria-controls');
  let lb = id && document.getElementById(id);
  if(!lb){
    // portaled/sibling listbox with NO aria-owns/controls (MUI Select body-portal; Workday sibling
    // toggled by `hidden`). Scope by ARIA OWNERSHIP: shared aria-labelledby token, OR the listbox's
    // aria-label == the trigger's accessible name; else the structural [role=listbox] inside the
    // combobox / its field. A foreign field's listbox carries a different label -> no cross-field bleed.
    const mine = (c.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
    const nm = mine.map(i=>{const e=document.getElementById(i);return e?(e.innerText||e.textContent||''):'';}).join(' ').toLowerCase().replace(/\s+/g,'');
    lb = [...document.querySelectorAll('[role=listbox]')].filter(e=>e.offsetParent).find(b=>{
      const bl=(b.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
      if(bl.some(t=>mine.includes(t))) return true;
      const al=(b.getAttribute('aria-label')||'').toLowerCase().replace(/\s+/g,'');
      return !!al && !!nm && (al.includes(nm)||nm.includes(al)); })
      || c.querySelector('[role=listbox]')
      || ((c.closest('.field,[class*=field]')||c.parentElement||c).querySelector('[role=listbox]'));
  }
  if(!lb) return "";
  const opts = [...lb.querySelectorAll('[role=option]')];
  if(!opts.length) opts.push(...lb.querySelectorAll('li'));
  // custom div dropdown (dependent-cascade city: `.citysel__opt` cells, no role=option/li). The
  // listbox was resolved by aria-owns/controls or ownership, so its class-based option cells ARE the
  // options — scoped to this list, so no page-wide bleed. Leaf cells only (skip wrapper containers).
  if(!opts.length) opts.push(...[...lb.querySelectorAll('[class*=opt],[class*=item],[class*=cell]')].filter(e=>!e.querySelector('[class*=opt],[class*=item]')));
  // innerText = RENDERED text (excludes svg <desc> junk textContent carries — live: option '3'
  // read back as 'SVGs not supported by this browser.3').
  const vis = o => norm((o.innerText!=null && o.innerText.trim()) ? o.innerText : (o.textContent||''));
  let t = opts.find(o => vis(o) === w);
  // PICK-STABILITY: word-boundary PREFIX before any substring. Value 'No' must prefer the option that
  // STARTS with the word 'No' over a 'Decline to self-identify' / 'do not consent' substring match.
  if(!t){ const re = new RegExp('^'+w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'\\b'); t = opts.find(o => re.test(vis(o))); }
  // substring fallback only for real words — a 1-char want ('4') matches half a country list
  if(!t && w.length >= 3) t = opts.find(o => { const x = vis(o); return x && (x.includes(w) || w.includes(x)); });
  if(!t) return "";
  t.scrollIntoView({block:'nearest'});
  for(const ev of ['mousedown','mouseup','click'])
    t.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true}));
  const got = (t.innerText!=null && t.innerText.trim()) ? t.innerText.trim() : (t.textContent||'').trim();
  return got || want;
}
"""
)


async def cdp_choose_aria_option(session: Any, node: Any, value: str) -> str:
    """Commit an ARIA combobox DETERMINISTICALLY: open it if collapsed, follow aria-owns/
    aria-controls to its listbox, click the [role=option] matching ``value`` by text. No delta,
    no VLM — the a11y wiring IS the structure. Both phases run on the SAME resolved node with
    no state read in between (a state read blur-closes the menu and the listbox unmounts).
    Returns committed text or "" (caller falls back to the visual path). Generic."""

    async def _do() -> str:
        r = await _resolve(session, node)
        if r is None:
            return ""
        cdp_session, session_id, object_id = r
        opened = await _call_on(cdp_session, session_id, object_id, _ARIA_OPEN_JS)
        if not opened:
            return ""
        await asyncio.sleep(0.4)  # listbox mounts on the next React commit, not synchronously
        got = await _call_on(cdp_session, session_id, object_id, _ARIA_PICK_JS, args=[str(value)])
        return str(got).strip() if got else ""

    try:
        return await asyncio.wait_for(_do(), timeout=CDP_ACTION_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        return ""
    except Exception:
        return ""


# ID-based (STALE-PROOF) portaled-listbox JS — bind the trigger by its DOM id in PAGE scope, so a
# get_state re-serialize between open and pick (which stales the backend node id) can't break it.
# _lbById mirrors _ARIA_LB_FIND's ownership scope with `t` = the id-resolved trigger. Bare arrows
# (page.evaluate wraps as (fn)()). __ID__ / __W__ baked via .replace(json.dumps(...)).
_PL_LB_HELPER = r"""
  const _lbFind = (c) => {
    const id = c.getAttribute('aria-owns') || c.getAttribute('aria-controls');
    let lb = id && document.getElementById(id);
    if(!lb){
      const mine = (c.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
      const nm = mine.map(i=>{const e=document.getElementById(i);return e?(e.innerText||e.textContent||''):'';}).join(' ').toLowerCase().replace(/\s+/g,'');
      lb = [...document.querySelectorAll('[role=listbox]')].filter(e=>e.getClientRects().length).find(b=>{
        const bl=(b.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
        if(bl.some(x=>mine.includes(x))) return true;
        const al=(b.getAttribute('aria-label')||'').toLowerCase().replace(/\s+/g,'');
        return !!al && !!nm && (al.includes(nm)||nm.includes(al)); })
        || c.querySelector('[role=listbox]')
        || ((c.closest('.field,[class*=field]')||c.parentElement||c).querySelector('[role=listbox]'));
    }
    return lb || null;
  };
  const _opts = (lb) => { let o=[...lb.querySelectorAll('[role=option]')]; if(!o.length) o.push(...lb.querySelectorAll('li')); if(!o.length) o.push(...[...lb.querySelectorAll('[class*=opt],[class*=item],[class*=cell]')].filter(e=>!e.querySelector('[class*=opt],[class*=item]'))); return o; };
  const _vis = o => ((o.innerText!=null && o.innerText.trim()) ? o.innerText : (o.textContent||'')).replace(/\s+/g,' ').trim();
"""
_PL_OPEN_BY_ID = "() => { const t = document.getElementById(__ID__); if(!t) return 'noel';" + r"""
  // IDEMPOTENT open: click ONLY when no menu is currently visible. A custom combobox (dependent-cascade
  // city) doesn't track aria-expanded, and prior rungs toggle it — clicking on aria-expanded alone
  // TOGGLES an already-open menu shut. Detect open by the owned list OR any visible menu/listbox in the
  // widget; open only if none. So whatever state prior rungs left, this ENSURES open, never closes.
  const lid = t.getAttribute('aria-controls') || t.getAttribute('aria-owns');
  const owned = lid && document.getElementById(lid);
  const scope = t.closest('.field,[class*=field]') || t.parentElement || document;
  const isOpen = (t.getAttribute('aria-expanded') === 'true')
    || (owned && owned.getClientRects().length)
    || [...scope.querySelectorAll('[role=listbox],[class*=menu],[class*=Menu]')].some(e => e.getClientRects().length);
  if(!isOpen){ t.scrollIntoView({block:'center'});
    for(const e of ['mousedown','mouseup','click']) t.dispatchEvent(new MouseEvent(e,{bubbles:true,cancelable:true,view:window})); }
  return 'ok'; }"""
_PL_READ_BY_ID = ("() => { const t = document.getElementById(__ID__); if(!t) return '[]';" + _PL_LB_HELPER + r"""
  const lb = _lbFind(t); if(!lb) return '[]';
  return JSON.stringify(_opts(lb).map(_vis).filter(Boolean)); }""")
_PL_CLICK_BY_ID = ("() => { const t = document.getElementById(__ID__); const w = (__W__||'').replace(/\\s+/g,' ').trim().toLowerCase();"
                   + _PL_LB_HELPER + r"""
  if(!t || !w) return '';
  const lb = _lbFind(t); if(!lb) return '';
  const opts = _opts(lb); const low = o => _vis(o).toLowerCase();
  let x = opts.find(o => low(o) === w) || opts.find(o => { const y=low(o); return y && (y.includes(w)||w.includes(y)); });
  if(!x) return '';
  x.scrollIntoView({block:'nearest'});
  for(const e of ['mousedown','mouseup','click']) x.dispatchEvent(new MouseEvent(e,{bubbles:true,cancelable:true,view:window}));
  return _vis(x); }""")
# SCROLL-MATERIALIZE a VIRTUALIZED listbox (react-window: only ~10 of N rows mounted). Page the
# listbox's scroll container by ~one viewport (overlapping) and return the freshly-mounted texts +
# whether it advanced, so the caller loops until the wanted row mounts or scrolling ends.
_PL_SCROLL_BY_ID = ("() => { const t = document.getElementById(__ID__);" + _PL_LB_HELPER + r"""
  const lb = _lbFind(t); if(!lb) return null;
  let sc = lb;
  if(sc.scrollHeight <= sc.clientHeight + 4){
    sc = [...lb.querySelectorAll('*')].find(e => e.scrollHeight > e.clientHeight + 4 && /auto|scroll/.test(getComputedStyle(e).overflowY)) || lb;
  }
  const before = sc.scrollTop;
  sc.scrollTop = before + Math.max(40, Math.floor(sc.clientHeight * 0.85));
  const now = sc.scrollTop;
  return JSON.stringify({advanced: now > before + 1, texts: _opts(lb).map(_vis).filter(Boolean)}); }""")


async def cdp_pick_aria_option(session: Any, node: Any, value: str, pick=None, node_id: str = "") -> str:
    """Commit a non-editable combobox (MUI Select div) whose body-portaled listbox a synthetic dispatch
    can open but the field-scoped delta never captures. STALE-PROOF: binds the trigger by its DOM id in
    PAGE scope (a get_state re-serialize stales the backend node — the reason the node-resolved rungs
    returned ''), OPENS it, READS the ownership-scoped listbox, resolves via ``pick(value, opts)`` (LLM:
    a short intent -> a long clause), then clicks the chosen option. Requires a stable id (the node's
    own id else the caller's discovery ref via ``node_id``); returns '' without one. Returns text or ''."""
    nid = str((getattr(node, "attributes", None) or {}).get("id") or node_id or "")
    if not nid:
        return ""

    async def _do() -> str:
        page = await session.must_get_current_page()
        idj = json.dumps(str(nid))
        await page.evaluate(_PL_OPEN_BY_ID.replace("__ID__", idj))
        await asyncio.sleep(0.4)  # listbox mounts on the next commit
        raw = await page.evaluate(_PL_READ_BY_ID.replace("__ID__", idj))
        try:
            opts = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            opts = []
        if not opts:
            return ""
        nv = " ".join(str(value).split()).lower()
        chosen = (await pick(value, opts)) if pick is not None else ""
        chosen = chosen or ""
        if not chosen:
            chosen = next((o for o in opts if " ".join(str(o).split()).lower() == nv), "") or \
                next((o for o in opts if nv and (nv in str(o).lower() or str(o).lower() in nv)), "")
        if not chosen:
            # VIRTUALIZED (react-window): the wanted row was never in the mounted window. Scroll-
            # materialize by exact value (the LLM can't see 500 unmounted rows), re-reading until it
            # mounts or scrolling ends. Exact/substring match — a windowed country list is not fuzzy.
            for _ in range(150):
                raw2 = await page.evaluate(_PL_SCROLL_BY_ID.replace("__ID__", idj))
                try:
                    info = json.loads(raw2) if raw2 else None
                except Exception:
                    info = None
                if not isinstance(info, dict):
                    break
                hit = next((t for t in info.get("texts", [])
                            if " ".join(str(t).split()).lower() == nv
                            or (nv and nv in " ".join(str(t).split()).lower())), None)
                if hit:
                    chosen = hit
                    break
                if not info.get("advanced"):
                    break
        if not chosen:
            return ""
        got = await page.evaluate(
            _PL_CLICK_BY_ID.replace("__ID__", idj).replace("__W__", json.dumps(str(chosen))))
        return str(got).strip() if got else str(chosen)

    try:
        return await asyncio.wait_for(_do(), timeout=max(CDP_ACTION_TIMEOUT, 4.0))
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        return ""
    except Exception:
        return ""


# react-select (greenhouse/robinhood) exposes NO aria-owns — its options render in a
# `[class*=option]` menu that only mounts on MOUSEDOWN (a plain .click() never opens it, the live
# root cause of robinhood's demographic-select escalations). This mirrors ats_greenhouse._combobox:
# find the control wrapper, mousedown-open, read the class-based options, click the match by text.
_RS_OPEN_READ_JS = r"""
function(skipOpen){
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  // the control wrapper is an ancestor of the input carrying a react-select 'control' class
  let ctrl = this, matched = false;
  for(let i=0;i<5 && ctrl;i++){ if(ctrl.className && /(^|[^a-z])control([^a-z]|$)|select__control|Control/i.test(ctrl.className.toString())){ matched = true; break; } ctrl = ctrl.parentElement; }
  // BARE combobox (MUI Select / any role=combobox trigger with no react-select 'control' wrapper):
  // when the walk matched nothing, ctrl has drifted to a random ancestor whose click never reaches
  // the trigger's own open handler (MUI painted BLANK: pageOpts=0). Fall back to `this`, the real
  // trigger. react-select keeps its matched control wrapper unchanged -> no regression.
  const target = matched ? ctrl : this;
  target.scrollIntoView({block:'center'});
  // affirm mega4/38: the synthetic dispatch NEVER opened the menu (aria/wrapper/idm all
  // absent, 244 stale option-classed nodes page-wide) — the caller now opens with a TRUSTED
  // CDP click first and passes skipOpen=true; dispatching again here would TOGGLE it closed.
  if(!skipOpen){
    for(const ev of ['mousedown','mouseup','click'])
      target.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
  }
  // SCOPE to THIS control's menu. react-select ids every option 'react-select-N-option-M' sharing
  // the input's 'react-select-N-input' instance — scope by that N so a page-wide read can't grab a
  // DIFFERENT open menu's options (live: gender read the phone country list). Fall back to the menu
  // element owning the control, then page-wide, only if the instance scope finds nothing.
  const idm = (this.id||'').match(/react-select-([^-]+)-/);
  let opts = [];
  if(idm){
    opts = [...document.querySelectorAll('[id^="react-select-'+idm[1]+'-option"]')];
  }
  if(!opts.length){
    // the OPEN menu's listbox id lands on the input's aria-controls/aria-owns — the only
    // ownership link that survives menuPortalTarget (stripe mega4/8: custom classNamePrefix
    // + body portal defeated both other scopes and the page-wide fallback read a DIFFERENT
    // field's lingering menu — 'Belgium' ended up committed into reside-country).
    const lid = this.getAttribute('aria-controls') || this.getAttribute('aria-owns');
    const lb = lid && document.getElementById(lid);
    if(lb) opts = [...lb.querySelectorAll('[class*=option],[class*=Option],[role=option]')];
  }
  if(!opts.length){
    // ARIA ownership by shared LABEL: MUI Select / Workday / Radix menus portal to <body> with NO
    // aria-owns/controls; the trigger links its listbox via a shared aria-labelledby token OR the
    // listbox's aria-label repeats the question. Deterministic ownership, survives body-portaling.
    const _mine = (this.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
    const _nm = _mine.map(id=>{const e=document.getElementById(id);return e?(e.innerText||e.textContent||''):'';}).join(' ').toLowerCase().replace(/\s+/g,'');
    const _own = [...document.querySelectorAll('[role=listbox],[class*=Menu-list],[class*=MenuList]')].filter(e=>e.offsetParent).find(b=>{
      const bl=(b.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
      if(bl.some(t=>_mine.includes(t))) return true;
      const al=(b.getAttribute('aria-label')||'').toLowerCase().replace(/\s+/g,'');
      return !!al && !!_nm && (al.includes(_nm)||_nm.includes(al)); });
    if(_own) opts = [..._own.querySelectorAll('[role=option],[class*=option],[class*=MenuItem],li')];
  }
  if(!opts.length){
    // the menu is a descendant of the control wrapper or its next sibling
    const menu = target.querySelector('[class*=menu],[class*=Menu]') || (target.nextElementSibling && target.nextElementSibling.matches('[class*=menu],[class*=Menu]') ? target.nextElementSibling : null);
    if(menu) opts = [...menu.querySelectorAll('[class*=option],[class*=Option],[role=option]')];
  }
  if(!opts.length){
    // page-wide LAST resort: trustworthy ONLY when exactly ONE menu is open AND it BELONGS to this
    // control. A lone-but-STALE neighbor menu (this field's own menu never opened) is the #1 bleed —
    // audit: 2,517 llm_pick->None, e.g. a COUNTRY field read the PRONOUNS menu (cov/35), stripe
    // 'Belgium' into reside-country. Ownership = react-select renders its menu hugging the control
    // edge (horizontal overlap + vertical adjacency). A menu elsewhere on the page is foreign -> miss.
    const all = [...document.querySelectorAll('[class*=option],[class*=Option],[role=option]')].filter(e=>e.offsetParent);
    const menus = [...new Set(all.map(o => o.closest('[class*=menu],[class*=Menu],[role=listbox]')).filter(Boolean))];
    if(menus.length === 1){
      const tr = target.getBoundingClientRect(), mr = menus[0].getBoundingClientRect();
      const hOverlap = mr.left < tr.right && mr.right > tr.left && mr.width > 0;
      const vAdjacent = Math.abs(mr.top - tr.bottom) <= 14 || Math.abs(mr.bottom - tr.top) <= 14;
      if(hOverlap && vAdjacent) opts = all;
    }
  }
  opts = opts.filter(e => e.offsetParent && !/menu-notice|no-options|placeholder/i.test((e.className||'').toString()));
  if(!opts.length){
    // close whatever we opened so it cannot bleed into the NEXT field's page-wide read
    this.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
    // WHY-EMPTY diagnostic (twilio mega4/34: rs-direct returned '' silently and the tenant
    // default stood): which scope existed but held nothing vs never resolved at all.
    const allOpt = document.querySelectorAll('[class*=option],[class*=Option],[role=option]').length;
    return JSON.stringify({__miss: {idm: !!idm, aria: !!(this.getAttribute('aria-controls')||this.getAttribute('aria-owns')),
      wrapperMenu: !!target.querySelector('[class*=menu],[class*=Menu]'), pageOpts: allOpt}});
  }
  return JSON.stringify([...new Set(opts.map(o => norm(o.innerText||o.textContent)).filter(Boolean))].slice(0,60));
}
"""

# the chosen option's viewport rect, scoped exactly like the read (aria-controls first) —
# the caller issues a TRUSTED Input.dispatchMouseEvent click there (live-proven on the
# greenhouse embed: control text flipped to the option and the menu closed).
_RS_OPTION_RECT_JS = r"""
function(want){
  const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const w = norm(want); if(!w) return null;
  const lid = this.getAttribute('aria-controls') || this.getAttribute('aria-owns');
  let root = lid && document.getElementById(lid);
  if(!root){
    const idm = (this.id||'').match(/react-select-([^-]+)-/);
    if(idm){ const o0 = document.querySelector('[id^="react-select-'+idm[1]+'-option"]'); root = o0 && o0.parentElement; }
  }
  if(!root){
    const _mine = (this.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
    const _nm = _mine.map(id=>{const e=document.getElementById(id);return e?(e.innerText||e.textContent||''):'';}).join(' ').toLowerCase().replace(/\s+/g,'');
    root = [...document.querySelectorAll('[role=listbox],[class*=Menu-list],[class*=MenuList]')].filter(e=>e.offsetParent).find(b=>{
      const bl=(b.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
      if(bl.some(t=>_mine.includes(t))) return true;
      const al=(b.getAttribute('aria-label')||'').toLowerCase().replace(/\s+/g,'');
      return !!al && !!_nm && (al.includes(_nm)||_nm.includes(al)); }) || null;
  }
  if(!root) return null;
  {
    const o = [...root.querySelectorAll('[role=option],[class*=option],[class*=MenuItem],li')]
      .find(x => norm(x.innerText||x.textContent) === w);
    if(o){ o.scrollIntoView({block:'nearest'}); const r=o.getBoundingClientRect(); if(r.width&&r.height) return {x:r.x+r.width/2, y:r.y+r.height/2}; }
  }
  const o = [...root.querySelectorAll('[class*=option],[class*=Option],[role=option]')]
    .find(x => norm(x.innerText||x.textContent) === w);
  if(!o) return null;
  o.scrollIntoView({block:'nearest'});
  const r = o.getBoundingClientRect();
  if(!r.width || !r.height) return null;
  return {x: r.x + r.width/2, y: r.y + r.height/2};
}
"""

# the control wrapper's rendered text after a commit attempt — the read-back that decides
# whether the trusted option click actually landed ('Select...' placeholders read as empty).
_RS_CTRL_TEXT_JS = r"""
function(){
  let ctrl = this;
  for(let i=0;i<6 && ctrl;i++){ if(/(^|[^a-z])control([^a-z]|$)|select__control|Control/i.test(String(ctrl.className))) break; ctrl = ctrl.parentElement; }
  const t = ((ctrl||this).innerText||'').replace(/\s+/g,' ').trim();
  return /^(select|choose|start typing)/i.test(t) ? "" : t;
}
"""

_RS_CLICK_JS = r"""
function(want){
  const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const w = norm(want); if(!w) return "";
  const idm = (this.id||'').match(/react-select-([^-]+)-/);
  let opts = idm ? [...document.querySelectorAll('[id^="react-select-'+idm[1]+'-option"]')].filter(e=>e.offsetParent) : [];
  if(!opts.length){
    const lid = this.getAttribute('aria-controls') || this.getAttribute('aria-owns');
    const lb = lid && document.getElementById(lid);
    if(lb) opts = [...lb.querySelectorAll('[class*=option],[class*=Option],[role=option]')].filter(e=>e.offsetParent);
  }
  if(!opts.length){
    // ARIA ownership by shared LABEL (MUI Select / Workday / Radix body-portal, no aria-owns/controls).
    const _mine = (this.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
    const _nm = _mine.map(id=>{const e=document.getElementById(id);return e?(e.innerText||e.textContent||''):'';}).join(' ').toLowerCase().replace(/\s+/g,'');
    const _own = [...document.querySelectorAll('[role=listbox],[class*=Menu-list],[class*=MenuList]')].filter(e=>e.offsetParent).find(b=>{
      const bl=(b.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
      if(bl.some(t=>_mine.includes(t))) return true;
      const al=(b.getAttribute('aria-label')||'').toLowerCase().replace(/\s+/g,'');
      return !!al && !!_nm && (al.includes(_nm)||_nm.includes(al)); });
    if(_own) opts = [..._own.querySelectorAll('[role=option],[class*=option],[class*=MenuItem],li')].filter(e=>e.offsetParent);
  }
  if(!opts.length){
    // page-wide click ONLY when the single open menu BELONGS to this control (renders adjacent) —
    // a lone STALE neighbor menu would commit into the WRONG question (stripe mega4/8 'Belgium').
    let ctrl = this;
    for(let i=0;i<5 && ctrl;i++){ if(/(^|[^a-z])control([^a-z]|$)|select__control|Control/i.test(String(ctrl.className||''))) break; ctrl = ctrl.parentElement; }
    const anchor = ctrl || this;
    const all = [...document.querySelectorAll('[class*=option],[class*=Option],[role=option]')].filter(e=>e.offsetParent);
    const menus = [...new Set(all.map(o => o.closest('[class*=menu],[class*=Menu],[role=listbox]')).filter(Boolean))];
    if(menus.length === 1){
      const tr = anchor.getBoundingClientRect(), mr = menus[0].getBoundingClientRect();
      const hOverlap = mr.left < tr.right && mr.right > tr.left && mr.width > 0;
      const vAdjacent = Math.abs(mr.top - tr.bottom) <= 14 || Math.abs(mr.bottom - tr.top) <= 14;
      if(hOverlap && vAdjacent) opts = all;
    }
  }
  let t = opts.find(o => norm(o.innerText||o.textContent) === w);
  if(!t && w.length>=3) t = opts.find(o => { const x=norm(o.innerText||o.textContent); return x && (x===w); });
  if(!t) return "";
  t.scrollIntoView({block:'nearest'});
  for(const ev of ['mousedown','mouseup','click']) t.dispatchEvent(new MouseEvent(ev,{bubbles:true,cancelable:true,view:window}));
  return norm(t.innerText||t.textContent);
}
"""


# SCROLL-MATERIALIZE a virtualized/windowed listbox (react-window: only ~10 of N rows mounted). Resolve
# THIS control's listbox exactly like the read/click scopes, page its scroll container by ~one viewport
# (overlapping so no row is skipped), and return the freshly-mounted option texts + whether the scroll
# advanced, so the caller can loop until the wanted row materializes or scrolling hits the end.
_RS_SCROLL_READ_JS = r"""
function(){
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  const lid = this.getAttribute('aria-controls') || this.getAttribute('aria-owns');
  let root = lid && document.getElementById(lid);
  if(!root){
    const idm = (this.id||'').match(/react-select-([^-]+)-/);
    if(idm){ const o0 = document.querySelector('[id^="react-select-'+idm[1]+'-option"]'); root = o0 && o0.parentElement; }
  }
  if(!root){
    const _mine = (this.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
    const _nm = _mine.map(id=>{const e=document.getElementById(id);return e?(e.innerText||e.textContent||''):'';}).join(' ').toLowerCase().replace(/\s+/g,'');
    root = [...document.querySelectorAll('[role=listbox],[class*=Menu-list],[class*=MenuList]')].filter(e=>e.offsetParent).find(b=>{
      const bl=(b.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean);
      if(bl.some(t=>_mine.includes(t))) return true;
      const al=(b.getAttribute('aria-label')||'').toLowerCase().replace(/\s+/g,'');
      return !!al && !!_nm && (al.includes(_nm)||_nm.includes(al)); }) || null;
  }
  if(!root) return null;
  let sc = root;
  if(sc.scrollHeight <= sc.clientHeight + 4){
    sc = [...root.querySelectorAll('*')].find(e => e.scrollHeight > e.clientHeight + 4 && /auto|scroll/.test(getComputedStyle(e).overflowY)) || root;
  }
  const before = sc.scrollTop;
  const step = Math.max(40, Math.floor(sc.clientHeight * 0.85));
  sc.scrollTop = before + step;
  const now = sc.scrollTop;
  const opts = [...root.querySelectorAll('[role=option],[class*=option],[class*=Option],[class*=MenuItem]')].filter(e=>e.offsetParent);
  const texts = [...new Set(opts.map(o => norm(o.innerText||o.textContent)).filter(Boolean))];
  return JSON.stringify({top: now, advanced: now > before + 1, texts: texts});
}
"""


# Read the option nodes CURRENTLY VISIBLE ON SCREEN (+ their center coords), restricted to a menu
# HUGGING `this` control (viewport proximity — survives body-portaling AND any classNamePrefix rename).
# role=option/menuitem/treeitem are ARIA STANDARDS; [class*=option] catches the library shells. The
# in-viewport + not-hidden filter = exactly what the user SEES. NO aria-controls / id / label / DOM
# ownership — a tenant renaming those cannot break the commit. `this` is the control node.
_VISIBLE_OPTIONS_JS = r"""
function(){
  const norm = s => (s||'').replace(/\s+/g,' ').trim();
  // bring the control (and its adjacent option cluster) into view first — an option rendered
  // below the fold is invisible to the read and the whole commit no-ops (the live flaky-empty).
  try { this.scrollIntoView({block:'center'}); } catch(e){}
  const cr = this.getBoundingClientRect();
  const inView = el => { const r=el.getBoundingClientRect();
    return r.width>1 && r.height>1 && r.top<innerHeight && r.left<innerWidth && r.bottom>0 && r.right>0; };
  const shown = el => { const s=getComputedStyle(el);
    return s.visibility!=='hidden' && s.display!=='none' && s.opacity!=='0' && el.offsetParent!==null; };
  // ARIA option roles + react-select option classes ONLY. A menu option is what this reader targets;
  // broadening to button/label grabs the form's field-title labels and mis-clicks them (proven
  // net-negative) — inline Yes/No pills are handled label-anchored elsewhere, not here.
  const sel='[role="option"],[role="menuitem"],[role="treeitem"],[role="menuitemradio"],'
           +'[class*=option],[class*=Option],[class*=MenuItem]';
  const seen=new Set(), out=[];
  document.querySelectorAll(sel).forEach(el=>{
    if(!inView(el)||!shown(el)) return;
    const t=norm(el.textContent);
    if(!t || t.length>90 || seen.has(t)) return;
    if(/menu-notice|no-options|placeholder/i.test((el.className||'').toString())) return;
    const r=el.getBoundingClientRect();
    // ANTI-BLEED: the option must belong to a menu hugging THIS control — a dropdown renders directly
    // below (or above) the control, horizontally overlapping it. A far option is a DIFFERENT field's
    // open menu (the 'Belgium into reside-country' bleed). Band by VIEWPORT rect (portal-agnostic).
    const hOverlap = r.left < cr.right+60 && r.right > cr.left-60;
    const vNear = (r.top >= cr.top-8 && r.top <= cr.bottom + innerHeight*0.75)
               || (r.bottom <= cr.bottom+8 && r.bottom >= cr.top - innerHeight*0.5);
    if(!hOverlap || !vNear) return;
    seen.add(t);
    out.push({text:t, x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2)});
  });
  return JSON.stringify(out);
}
"""


async def cdp_pick_option_visually(session: Any, node: Any, value: str, pick=None) -> str:
    """VISUAL option commit — what a HUMAN does: SEE the rendered options, click the right one. Reads
    the option nodes VISIBLE on screen (ARIA roles + [class*=option], in-viewport, hugging THIS
    control) WITH their center coords, picks the best by meaning (exact -> caller's semantic ``pick``),
    then a TRUSTED CDP click AT the option's coords. Ownership-BLIND (no aria-controls/id/label) so a
    tenant renaming those cannot break it. Fires as the fallback when the OWNED read (``_RS_OPEN_READ_JS``)
    found nothing but options ARE painted (the ``pageOpts>0`` __miss). Returns committed text or ""."""

    async def _do() -> str:
        r = await _resolve(session, node)
        if r is None:
            return ""
        cdp_session, session_id, object_id = r
        # RETRY the read: options can paint late (async menu, below-fold scroll settling). An empty
        # first read was the live flaky-empty that silently no-op'd the whole visual commit.
        opts: Any = []
        for _try in range(3):
            raw = await _call_on(cdp_session, session_id, object_id, _VISIBLE_OPTIONS_JS)
            try:
                opts = json.loads(raw) if raw else []
            except Exception:
                opts = []
            if isinstance(opts, list) and opts:
                break
            await asyncio.sleep(0.35)
        if not isinstance(opts, list) or not opts:
            return ""
        texts = [o["text"] for o in opts]
        nv = " ".join(str(value).split()).lower()
        chosen = next((t for t in texts if " ".join(t.split()).lower() == nv), "")
        if not chosen and nv and len(nv) <= 5:
            # WORD-BOUNDARY PREFIX (deterministic, LLM-free): a SHORT BOOLEAN value whose option label is a
            # long CLAUSE ('Yes' -> 'Yes, I am legally authorized to work…'; 'No' -> 'No, I require
            # sponsorship'). ^value\b so 'No' never matches 'None of the above'; require the option be
            # much longer than the value (a clause, not a peer token) so a date value can't grab a
            # same-length day cell ('15' vs day '15' — react_datepicker false-green). Live greenhouse
            # authorized/sponsorship commit that escalated when the semantic pick timed out under load.
            import re as _re
            _wb = _re.compile(r"^" + _re.escape(nv) + r"\b", _re.I)
            chosen = next((t for t in texts if _wb.match(" ".join(t.split()).lower())
                           and len(" ".join(t.split())) >= len(nv) + 6), "")
        if not chosen and pick is not None:
            with contextlib.suppress(Exception):
                chosen = await pick(value, texts) or ""
        if not chosen:
            return ""
        nch = " ".join(chosen.split()).lower()
        tgt = next((o for o in opts if " ".join(o["text"].split()).lower() == nch), None)
        if tgt is None:
            return ""
        ok = await cdp_click_xy(session, node, int(tgt["x"]), int(tgt["y"]))
        print(f"   [pick_visual] want={value!r} visible={texts[:6]} -> {chosen!r} click={ok}", flush=True)
        return chosen if ok else ""

    # return the committed TEXT (docstring contract) — NOT _guarded, which coerces to bool and made
    # callers that do `committed_text = got` / `got[:24]` set/slice a bool (TypeError).
    try:
        return await asyncio.wait_for(_do(), timeout=CDP_ACTION_TIMEOUT + 1.0)
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        return ""
    except Exception:
        return ""


async def cdp_choose_react_select(session: Any, node: Any, value: str, pick=None) -> str:
    """Commit a react-select (no aria-owns) DETERMINISTICALLY: mousedown-open the control, read
    the class-based `[class*=option]` menu, pick (exact, or via ``pick(value, options)`` for
    semantic match), click the chosen option by text. When the OWNED read finds nothing (ownership
    couldn't bind painted options, or the menu is async), falls to ``cdp_pick_option_visually`` —
    see the options on screen + click by coords. Returns committed text or "". Generic —
    the react-select DOM shape (control wrapper + option class) is a library convention, not a
    per-ATS string."""

    async def _do() -> str:
        r = await _resolve(session, node)
        if r is None:
            return ""
        cdp_session, session_id, object_id = r
        # KEYBOARD open: focus + ArrowDown. PROVEN in a live CDP session on the greenhouse
        # embed (stripe question_67542071): a trusted mousePressed/Released at the control
        # center left aria-expanded=false, while focus+ArrowDown flipped it true with
        # aria-controls resolving to react-select-<id>-listbox and 29 scoped options.
        opened = False
        with contextlib.suppress(Exception):
            await _call_on(cdp_session, session_id, object_id, "function(){ this.focus(); }")
            key = cdp_session.cdp_client.send.Input.dispatchKeyEvent
            for typ in ("keyDown", "keyUp"):
                await key(params={"type": typ, "key": "ArrowDown", "code": "ArrowDown",
                                  "windowsVirtualKeyCode": 40, "nativeVirtualKeyCode": 40},
                          session_id=session_id)
            await asyncio.sleep(0.5)
            opened = True
        raw = await _call_on(cdp_session, session_id, object_id, _RS_OPEN_READ_JS, args=[bool(opened)])
        try:
            options = json.loads(raw) if raw else []
        except Exception:
            options = []
        if isinstance(options, dict) and opened:
            # keyboard open read nothing — one retry with the synthetic-dispatch open
            raw = await _call_on(cdp_session, session_id, object_id, _RS_OPEN_READ_JS, args=[False])
            with contextlib.suppress(Exception):
                options = json.loads(raw) if raw else []
        if isinstance(options, dict) and not (options.get("__miss") or {}).get("pageOpts"):
            # ASYNC menu: options fetch on open (typed-search / XHR) — the read beat the paint
            # (pageOpts==0). Wait one beat and re-read the OWNED scope before giving up.
            await asyncio.sleep(0.6)
            with contextlib.suppress(Exception):
                raw = await _call_on(cdp_session, session_id, object_id, _RS_OPEN_READ_JS, args=[True])
                options = json.loads(raw) if raw else options
        if isinstance(options, dict) or not options:
            # OWNED read found nothing — ownership couldn't bind the painted options (pageOpts>0 but no
            # id/aria/wrapper/geometry match), or the menu never opened. VISION: read the options
            # visibly on screen by coords + click the match. Ownership-blind -> survives class renames.
            miss = options.get("__miss") if isinstance(options, dict) else None
            print(f"   [rs-direct] no owned options: {miss} -> visual", flush=True)
            return await cdp_pick_option_visually(session, node, value, pick=pick)
        chosen = ""
        # exact/normalized match first (free), else the caller's semantic picker
        nv = str(value).strip().lower()
        for o in options:
            if o.strip().lower() == nv:
                chosen = o
                break
        if not chosen and nv and len(nv) <= 5:
            # WORD-BOUNDARY PREFIX (deterministic): a SHORT BOOLEAN value whose option label is a long
            # CLAUSE ('Yes' -> 'Yes, I am legally authorized…'). ^value\b so 'No' can't match 'None of the
            # above'; require the option much longer than the value so a date value can't grab a same-length
            # day cell (react_datepicker false-green).
            import re as _re
            _wb = _re.compile(r"^" + _re.escape(nv) + r"\b", _re.I)
            chosen = next((o for o in options if _wb.match(o.strip().lower())
                           and len(o.strip()) >= len(nv) + 6), "")
        if not chosen and pick is not None:
            with contextlib.suppress(Exception):
                chosen = await pick(value, options) or ""
        if not chosen:
            # SCROLL-MATERIALIZE: a virtualized/windowed listbox (react-window) mounts only ~10 of N
            # rows, so the wanted option was never in the first read. Only when THIS trigger is click-
            # only (a text-input react-select is type-filtered instead) and its listbox actually scrolls:
            # page the listbox and re-read until an option IDENTITY-matches `value` or scrolling ends.
            editable = await _call_on(cdp_session, session_id, object_id,
                "function(){ return this.tagName==='INPUT' || this.tagName==='TEXTAREA' || this.isContentEditable === true; }")
            if not editable:
                nv = " ".join(str(value).split()).lower()
                for _ in range(150):  # ~150 pages caps a 500-row virtualized list; each read is cheap CDP
                    raw2 = await _call_on(cdp_session, session_id, object_id, _RS_SCROLL_READ_JS)
                    try:
                        info = json.loads(raw2) if raw2 else None
                    except Exception:
                        info = None
                    if not isinstance(info, dict):
                        break
                    hit = next((t for t in info.get("texts", []) if " ".join(str(t).split()).lower() == nv), None)
                    if hit:
                        chosen = hit
                        break
                    if not info.get("advanced"):  # scroll no longer moves -> end of list
                        break
        if not chosen:
            return ""
        # TRUSTED click at the option's rect first (live-proven commit path); the synthetic
        # option-event dispatch stays as the fallback.
        with contextlib.suppress(Exception):
            rect = await _call_on(cdp_session, session_id, object_id, _RS_OPTION_RECT_JS, args=[str(chosen)])
            if isinstance(rect, dict) and rect.get("x") is not None:
                await _dispatch_mouse_click(cdp_session, session_id, rect["x"], rect["y"])
                await asyncio.sleep(0.3)
                got = await _call_on(cdp_session, session_id, object_id, _RS_CTRL_TEXT_JS)
                if got and str(got).strip():
                    return str(got).strip()
        got = await _call_on(cdp_session, session_id, object_id, _RS_CLICK_JS, args=[str(chosen)])
        return str(got).strip() if got else ""

    try:
        return await asyncio.wait_for(_do(), timeout=CDP_ACTION_TIMEOUT + 2.0)
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        return ""
    except Exception:
        return ""


# JS: find the text input within `this` (the location card / control) and SET its value via the native
# setter + input/change so a React-controlled autocomplete keeps it (el.fill reverts). Mirrors the proven
# ats_lever._location. Returns the set value, or "".
_SET_TEXT_IN_CONTAINER_JS = r"""
function(want){
  if(!want) return "";
  let el = null;
  if(this.matches && this.matches('input,textarea')) el = this;
  if(!el) el = this.querySelector('input[type=text],input[type=search],input[type=email],input[type=url],input[type=tel],input:not([type]),textarea,input[role=combobox]');
  if(!el) return "";
  const proto = el.tagName==='TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto,'value').set;
  setter.call(el, want);
  el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
  return el.value || want;
}
"""


async def cdp_set_file(session: Any, node: Any, path: str) -> bool:
    """Attach a file to an input[type=file] via DIRECT CDP DOM.setFileInputFiles (no OS picker, no
    event-bus UploadFileEvent whose readiness wait HANGS on a busy SPA — the Ashby 2nd-dropzone case).
    Bounded. Returns True on success."""
    bnid = getattr(node, "backend_node_id", None)
    if node is None or bnid is None or not path:
        return False
    import os

    abspath = os.path.abspath(str(path))  # DOM.setFileInputFiles requires an ABSOLUTE path

    async def _do() -> bool:
        cdp_session = await session.cdp_client_for_node(node)
        await cdp_session.cdp_client.send.DOM.setFileInputFiles(
            params={"files": [abspath], "backendNodeId": int(bnid)},
            session_id=cdp_session.session_id,
        )
        return True

    try:
        return await asyncio.wait_for(_do(), timeout=CDP_ACTION_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        return False
    except Exception:
        return False


async def cdp_set_text_in_container(session: Any, container_node: Any, value: str) -> str:
    """Set the text input inside ``container_node`` to ``value`` via the native setter + input/change (the
    proven ats_lever._location trick for a React autocomplete that reverts el.fill). Returns the set value
    or "". Used as the geocomplete fill-only fallback when a typeahead surfaced no options."""

    async def _do() -> str:
        r = await _resolve(session, container_node)
        if r is None:
            return ""
        cdp_session, session_id, object_id = r
        got = await _call_on(cdp_session, session_id, object_id, _SET_TEXT_IN_CONTAINER_JS, args=[str(value)])
        return str(got).strip() if got else ""

    try:
        return await asyncio.wait_for(_do(), timeout=CDP_ACTION_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        return ""
    except Exception:
        return ""


_SET_RANGE_JS = r"""
function(want){
  const el = this;
  if(!(el.tagName==='INPUT' && (el.type||'').toLowerCase()==='range')) return "";
  const n = parseFloat(String(want).replace(/[^0-9.\-]/g,''));
  if(!isFinite(n)) return "";
  const min = parseFloat(el.min||'0'), max = parseFloat(el.max||'100');
  const v = Math.min(isFinite(max)?max:n, Math.max(isFinite(min)?min:n, n));
  const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
  d.set.call(el, String(v));
  el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
  return String(el.value);
}
"""


async def cdp_set_range(session: Any, node: Any, value: str) -> str:
    """Commit an <input type=range> slider: parse the numeric part of ``value``, clamp to
    min/max, set via the native setter + input/change (React keeps it). Returns the committed
    number as a string, or "" (not a range input / no numeric). teamtailor/lydia years-of-
    experience sliders sat at their default (1 vs the profile's 7) — audit-blind wrong-value."""

    async def _do() -> str:
        r = await _resolve(session, node)
        if r is None:
            return ""
        cdp_session, session_id, object_id = r
        got = await _call_on(cdp_session, session_id, object_id, _SET_RANGE_JS, args=[str(value)])
        return str(got).strip() if got else ""

    try:
        return await asyncio.wait_for(_do(), timeout=CDP_ACTION_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
        return ""
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# PUBLIC: cdp_blur / cdp_raw_value — the commit-or-discard blur test for typeaheads.
# --------------------------------------------------------------------------- #
async def cdp_blur(session: Any, node: Any) -> bool:
    """Blur the located control the way a tab-away does — native ``.blur()`` + a dispatched
    blur/focusout. A geocomplete/typeahead whose typed query was never committed via an option-row
    click WIPES on blur; a real selection survives. Returns True when the blur dispatched."""
    with contextlib.suppress(Exception):
        r = await _resolve(session, node)
        if r is None:
            return False
        cdp_session, session_id, object_id = r
        await _call_on(
            cdp_session, session_id, object_id,
            "function(){ try{ this.blur(); }catch(_){}"
            " try{ this.dispatchEvent(new Event('blur',{bubbles:false}));"
            " this.dispatchEvent(new FocusEvent('focusout',{bubbles:true})); }catch(_){}"
            " return true; }",
        )
        return True
    return False


async def cdp_raw_value(session: Any, node: Any) -> str:
    """The control's OWN ``.value`` (the typed text) — NOT the container-scan committed display. A
    non-empty raw value means the value lives IN the input (a typeahead echo candidate that blur may
    wipe); empty means it is committed as a selection elsewhere (react-select single-value / native
    <select>), so there is no blur-wipe risk. Returns '' on any failure / non-value element."""
    with contextlib.suppress(Exception):
        r = await _resolve(session, node)
        if r is None:
            return ""
        cdp_session, session_id, object_id = r
        got = await _call_on(
            cdp_session, session_id, object_id,
            "function(){ return (this && this.value != null) ? String(this.value) : ''; }",
        )
        return str(got or "").strip()
    return ""


# --------------------------------------------------------------------------- #
# PUBLIC: cdp_click — trusted Input.dispatchMouseEvent at the node center, JS-click fallback.
# --------------------------------------------------------------------------- #
async def cdp_click(session: Any, node: Any) -> bool:
    """Click the node via a trusted CDP mouse sequence at its center (move -> press -> release),
    falling back to a JS this.click() for box-less / occluded nodes. NO readiness wait — works on
    radios / checkboxes / option cells the watchdog would otherwise gate on. Returns True on click.
    """

    async def _do() -> bool:
        r = await _resolve(session, node)
        if r is None:
            return False
        cdp_session, session_id, object_id = r
        # 1) try a trusted mouse click at the node center (the watchdog's primary path).
        center = await _node_center(session, cdp_session, session_id, object_id, node)
        if center is not None:
            try:
                await _dispatch_mouse_click(cdp_session, session_id, center[0], center[1])
                return True
            except Exception:
                pass  # fall through to JS click
        # 2) JS click fallback (box-less / occluded) — the watchdog's documented fallback.
        got = await _call_on(cdp_session, session_id, object_id, _JS_CLICK_JS)
        return bool(got)

    return await _guarded(_do())


# --------------------------------------------------------------------------- #
# PUBLIC: cdp_click_xy — trusted mouse click at absolute viewport coordinates. No resolve needed.
# --------------------------------------------------------------------------- #
async def cdp_click_xy(session: Any, node_for_session: Any, x: int, y: int) -> bool:
    """Trusted CDP mouse click at absolute viewport (x, y). `node_for_session` is any node in the
    target frame (to resolve the right CDP session); when None, uses the root cdp session."""

    async def _do() -> bool:
        cdp_session = await _session_for(session, node_for_session)
        if cdp_session is None:
            return False
        await _dispatch_mouse_click(cdp_session, cdp_session.session_id, float(x), float(y))
        return True

    return await _guarded(_do())


# JS run ON the node: scroll it into view, then return its LIVE viewport-space center. getBoundingClientRect
# is ALWAYS viewport-relative (already nets out page scroll + zoom), unlike the serializer's document-space
# absolute_position — so clicking this center lands on the element no matter how far the page is scrolled.
_RECT_CENTER_JS = r"""
function() {
  try { this.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
  var r = this.getBoundingClientRect();
  if (!r || (r.width <= 0 && r.height <= 0)) return null;
  return {x: r.left + r.width / 2, y: r.top + r.height / 2};
}
"""


async def cdp_click_node_center(session: Any, node: Any) -> bool:
    """Trusted CDP mouse click at the node's LIVE viewport-space center (the visual-commit click).

    Why not ``cdp_click_xy(node_center(node))``: ``node_center`` reads the serializer's DOCUMENT-space
    rect, but ``Input.dispatchMouseEvent`` takes VIEWPORT coords — on a scrolled page (Lever's long
    form) the two diverge by the scroll offset and the click misses (observed: a 'Yes' radio clicked at
    document-y while the viewport-y was hundreds of px lower -> verify EMPTY -> ESCALATE). This resolves
    the node and asks the LIVE DOM for getBoundingClientRect (already viewport-relative + scrolled into
    view), then clicks that center. Returns False on resolve/box failure (caller falls back)."""

    async def _do() -> bool:
        r = await _resolve(session, node)
        if r is None:
            return False
        cdp_session, session_id, object_id = r
        center = await _call_on(cdp_session, session_id, object_id, _RECT_CENTER_JS)
        if not center or "x" not in center or "y" not in center:
            return False
        await _dispatch_mouse_click(cdp_session, session_id, float(center["x"]), float(center["y"]))
        return True

    return await _guarded(_do())


# --------------------------------------------------------------------------- #
# PUBLIC: cdp_type — focus + per-char trusted keystrokes (typeahead), else cdp_set_value.
# --------------------------------------------------------------------------- #
async def cdp_type(session: Any, node: Any, text: str, *, keystrokes: bool = False, clear: bool = True) -> bool:
    """Enter `text` into the node.

    keystrokes=False (default, plain fields): cdp_set_value — fast, React-aware, one round-trip.
    keystrokes=True (typeahead / combobox search): DOM.focus then per-char Input.dispatchKeyEvent
      (keyDown -> char -> keyUp) so the page's debounced search/XHR fires, mirroring the watchdog
      char-by-char path (:2223-2257). `clear` empties the field first (native setter) when typing.
    Returns True on success, False on timeout/error.
    """
    if not keystrokes:
        if await cdp_set_value(session, node, text):
            return True
        # FALLBACK: some React controlled inputs (Workable) immediately RESET a native-setter
        # value back to their state, so the fast path reads back empty. Retry with TRUSTED
        # per-char key events — React's onChange captures each keystroke and keeps it. Generic,
        # no per-ATS branch; only pays the slower path when the fast one demonstrably failed.
        keystrokes = True

    async def _do() -> bool:
        r = await _resolve(session, node)
        if r is None:
            return False
        cdp_session, session_id, object_id = r
        backend_node_id = int(node.backend_node_id)
        if clear:
            # React-aware clear via the native setter (mirrors _clear_text_field value branch).
            await _call_on(cdp_session, session_id, object_id, _SET_VALUE_JS, args=[""])
        # focus the element so keystrokes land on it (watchdog focuses before typing).
        with contextlib.suppress(Exception):
            await cdp_session.cdp_client.send.DOM.focus(
                params={"backendNodeId": backend_node_id}, session_id=session_id
            )
        # FOCUS-IDENTITY GUARD (cross-field bleed): trusted key events land on document.activeElement,
        # NOT on object_id — and the DOM.focus above is best-effort (a detached/re-rendered node fails
        # silently inside the suppress), so without this check the whole batch types this field's
        # value into WHOEVER kept focus, i.e. a neighbouring field. Structural identity only: compare
        # the resolved target object against its root's activeElement. One JS refocus retry, then
        # abort FAIL-CLOSED (the caller's read-back/verify reports the miss); mid-batch re-checks
        # catch a widget stealing focus while its dropdown opens.
        if not await _focused_is_target(cdp_session, session_id, object_id):
            with contextlib.suppress(Exception):
                await _call_on(cdp_session, session_id, object_id, "function(){ this.focus(); }")
            if not await _focused_is_target(cdp_session, session_id, object_id):
                return False
        for i, ch in enumerate(str(text)):
            if i and i % 12 == 0 and not await _focused_is_target(cdp_session, session_id, object_id):
                return False  # focus stolen mid-batch — stop feeding a stranger, fail-closed
            await _dispatch_char(cdp_session, session_id, ch)
        # HEAD-LOSS fixup: typeahead widgets steal focus/wipe while their dropdown opens on the
        # first keystrokes — the head chars deterministically vanish ('University…' -> 'ersity of
        # California', breezy/hibob/bamboohr) and RETYPING loses them again (the dropdown re-
        # opens). But after a keystroke session the widget is INITIALIZED, so a native-setter
        # re-set of the full value (+input/change) now sticks — even on React fields that refused
        # it cold (that refusal is the same init wipe). Read back; fix up on mismatch.
        with contextlib.suppress(Exception):
            got = await _call_on(cdp_session, session_id, object_id, "function(){ return this.value || ''; }")
            if os.environ.get("OA_TYPE_DEBUG"):
                print(f"   [cdp_type] after-keys value={str(got)[:60]!r} want={str(text)[:60]!r}")
            if str(got or "") != str(text):
                await asyncio.sleep(0.4)  # let the dropdown/init settle
                await _call_on(cdp_session, session_id, object_id, _SET_VALUE_JS, args=[str(text)])
                if os.environ.get("OA_TYPE_DEBUG"):
                    got2 = await _call_on(cdp_session, session_id, object_id, "function(){ return this.value || ''; }")
                    print(f"   [cdp_type] after-fixup value={str(got2)[:60]!r}")
        return True

    # LENGTH-SCALED guard: the keystroke fallback types per-char (~15-30ms each) — a 700-char
    # essay needs 10-30s, and the flat 4s guard killed it mid-flight ('text-type-refused' on
    # every long answer). Budget follows the work, still hard-bounded.
    return await _guarded(_do(), timeout=max(CDP_ACTION_TIMEOUT, 2.0 + 0.05 * len(str(text))))


# `this` = the resolved target. True iff the target (or one of its descendants — a composite
# widget's inner editable) is the activeElement of its OWN root (getRootNode covers shadow DOM;
# per-frame session covers iframes). Structural identity — no labels, no text.
_FOCUS_IS_TARGET_JS = (
    "function(){ const r = this.getRootNode ? this.getRootNode() : document;"
    " const a = (r && r.activeElement) || document.activeElement;"
    " return this === a || !!(a && this.contains && this.contains(a)); }"
)


async def _focused_is_target(cdp_session: Any, session_id: Any, object_id: str) -> bool:
    """Does the page's focus sit on the resolved target node (or inside it)? Errors -> False
    (fail-closed: if we cannot PROVE the keystrokes will land on the target, we do not type)."""
    with contextlib.suppress(Exception):
        return (await _call_on(cdp_session, session_id, object_id, _FOCUS_IS_TARGET_JS)) is True
    return False


# --------------------------------------------------------------------------- #
# Mouse / key dispatch helpers — mirror the watchdog Input.* sequences exactly.
# --------------------------------------------------------------------------- #
async def _dispatch_mouse_click(cdp_session: Any, session_id: Any, x: float, y: float) -> None:
    """mouseMoved -> mousePressed(left, clickCount=1) -> mouseReleased(left, clickCount=1).
    Mirrors default_action_watchdog.py:1259-1304 (without the watchdog's surrounding readiness wait).
    """
    send = cdp_session.cdp_client.send.Input.dispatchMouseEvent
    await send(params={"type": "mouseMoved", "x": x, "y": y}, session_id=session_id)
    await send(
        params={"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        session_id=session_id,
    )
    await send(
        params={"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        session_id=session_id,
    )


async def _dispatch_char(cdp_session: Any, session_id: Any, ch: str) -> None:
    """keyDown -> char(text=ch) -> keyUp for one character. Mirrors the watchdog char path
    (:2223-2257). The `char` event with `text` is the one that actually inserts the character."""
    send = cdp_session.cdp_client.send.Input.dispatchKeyEvent
    if ch == "\n":
        await send(
            params={"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
            session_id=session_id,
        )
        await send(params={"type": "char", "text": "\r", "key": "Enter"}, session_id=session_id)
        await send(
            params={"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
            session_id=session_id,
        )
        return
    await send(params={"type": "keyDown", "key": ch}, session_id=session_id)
    await send(params={"type": "char", "text": ch, "key": ch}, session_id=session_id)
    await send(params={"type": "keyUp", "key": ch}, session_id=session_id)


async def _node_center(
    session: Any, cdp_session: Any, session_id: Any, object_id: str, node: Any
) -> tuple[float, float] | None:
    """Center point of the node. Prefer the node's own absolute_position (free, already serialized);
    fall back to get_element_coordinates, then a getBoundingClientRect callFunctionOn (session.py
    Method-3 pattern). Returns (cx, cy) or None when no box is available (caller -> JS click)."""
    rect = getattr(node, "absolute_position", None)
    if rect is not None and getattr(rect, "width", 0) and getattr(rect, "height", 0):
        return (rect.x + rect.width / 2.0, rect.y + rect.height / 2.0)
    # browser-use's own multi-strategy coordinate read (quads -> box model -> JS rect)
    try:
        coords = await session.get_element_coordinates(int(node.backend_node_id), cdp_session)
        if coords is not None and coords.width and coords.height:
            return (coords.x + coords.width / 2.0, coords.y + coords.height / 2.0)
    except Exception:
        pass
    val = await _call_on(cdp_session, session_id, object_id, _RECT_JS)
    if isinstance(val, dict) and val.get("width") and val.get("height"):
        return (val["x"] + val["width"] / 2.0, val["y"] + val["height"] / 2.0)
    return None


async def _session_for(session: Any, node_for_session: Any) -> Any | None:
    """Resolve the CDP session for an xy click: the node's frame session, else the root cdp session."""
    if node_for_session is not None and getattr(node_for_session, "backend_node_id", None) is not None:
        try:
            return await session.cdp_client_for_node(node_for_session)
        except Exception:
            pass
    # root session fallback
    get = getattr(session, "get_or_create_cdp_session", None)
    if get is not None:
        try:
            return await get()
        except Exception:
            return None
    return None


async def _guarded(coro: Any, timeout: float | None = None) -> bool:
    """Run a write coroutine under the per-action timeout; any timeout/error -> False (never hang)."""
    try:
        return bool(await asyncio.wait_for(coro, timeout=timeout or CDP_ACTION_TIMEOUT))
    except TimeoutError:
        return False
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# OFFLINE self-test — a FAKE CDP layer (like oa_dom_value's) proving each public write calls the
# RIGHT CDP methods and dispatches the RIGHT events, with NO browser, NO network, $0.
# --------------------------------------------------------------------------- #
class _RecSend:
    """Records every CDP method call (domain.method + params) and returns scripted values."""

    def __init__(self, *, object_id: str | None, call_value: Any) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._object_id = object_id
        self._call_value = call_value
        outer = self

        async def _resolve(params=None, session_id=None):
            outer.calls.append(("DOM.resolveNode", params or {}))
            return {"object": {}} if outer._object_id is None else {"object": {"objectId": outer._object_id}}

        async def _focus(params=None, session_id=None):
            outer.calls.append(("DOM.focus", params or {}))
            return {}

        async def _call(params=None, session_id=None):
            outer.calls.append(("Runtime.callFunctionOn", params or {}))
            v = outer._call_value(params) if callable(outer._call_value) else outer._call_value
            return {"result": {"value": v}}

        async def _mouse(params=None, session_id=None):
            outer.calls.append(("Input.dispatchMouseEvent", params or {}))
            return {}

        async def _key(params=None, session_id=None):
            outer.calls.append(("Input.dispatchKeyEvent", params or {}))
            return {}

        self.DOM = type("DOM", (), {"resolveNode": staticmethod(_resolve), "focus": staticmethod(_focus)})()
        self.Runtime = type("Runtime", (), {"callFunctionOn": staticmethod(_call)})()
        self.Input = type(
            "Input",
            (),
            {"dispatchMouseEvent": staticmethod(_mouse), "dispatchKeyEvent": staticmethod(_key)},
        )()


class _RecClient:
    def __init__(self, send: _RecSend) -> None:
        self.send = send


class _RecSession:
    def __init__(self, send: _RecSend) -> None:
        self.session_id = "sess-1"
        self.cdp_client = _RecClient(send)


class _Rect:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.width, self.height = x, y, w, h


class _Node:
    def __init__(self, backend_node_id: int | None, rect: _Rect | None = None) -> None:
        self.backend_node_id = backend_node_id
        self.absolute_position = rect


class _FakeSession:
    def __init__(self, *, object_id: str | None = "obj-1", call_value: Any = "") -> None:
        self.send = _RecSend(object_id=object_id, call_value=call_value)

    async def cdp_client_for_node(self, node):
        return _RecSession(self.send)

    async def get_or_create_cdp_session(self, target_id=None, focus=True):
        return _RecSession(self.send)


def _methods(send: _RecSend) -> list[str]:
    return [m for m, _ in send.calls]


async def _selftest() -> int:
    checks: list[tuple[str, bool, Any]] = []

    def chk(name: str, passed: bool, detail: Any = "") -> None:
        checks.append((name, passed, detail))

    node = _Node(42, _Rect(10, 10, 100, 20))

    # --- cdp_set_value: resolveNode -> callFunctionOn(_SET_VALUE_JS, arg=text); success on echo ---
    fs = _FakeSession(call_value=lambda p: p["arguments"][0]["value"])  # echo the arg back
    ok = await cdp_set_value(fs, node, "Pyry")
    ms = _methods(fs.send)
    chk("set_value -> True on echo", ok is True, ok)
    chk("set_value calls resolveNode then callFunctionOn", ms == ["DOM.resolveNode", "Runtime.callFunctionOn"], ms)
    last = fs.send.calls[-1][1]
    chk("set_value passes _SET_VALUE_JS body", "nativeSetter" in last["functionDeclaration"], "")
    chk("set_value passes text as arg", last.get("arguments") == [{"value": "Pyry"}], last.get("arguments"))
    chk("set_value returnByValue=True", last.get("returnByValue") is True, last.get("returnByValue"))

    fs_bad = _FakeSession(call_value="something-else")  # JS echoes a different value -> miss
    chk("set_value False when value didn't land", (await cdp_set_value(fs_bad, node, "Pyry")) is False)

    chk("set_value False on no objectId", (await cdp_set_value(_FakeSession(object_id=None), node, "x")) is False)
    chk("set_value False on None node", (await cdp_set_value(_FakeSession(), None, "x")) is False)

    # --- cdp_select: resolveNode -> callFunctionOn(_SELECT_JS); success on non-empty echo ---
    fs2 = _FakeSession(call_value="California")
    ok2 = await cdp_select(fs2, node, "California")
    chk("select -> True on committed text", ok2 is True, ok2)
    chk("select passes _SELECT_JS body", "selectedIndex" in fs2.send.calls[-1][1]["functionDeclaration"], "")
    chk("select False on empty (no match)", (await cdp_select(_FakeSession(call_value=""), node, "Nope")) is False)

    # --- cdp_click: with a box -> trusted mouse (move/press/release), no JS click ---
    fs3 = _FakeSession(call_value=True)
    ok3 = await cdp_click(fs3, node)
    ms3 = _methods(fs3.send)
    chk("click -> True", ok3 is True, ok3)
    chk("click resolves then dispatches mouse", ms3[0] == "DOM.resolveNode", ms3)
    chk("click uses 3 trusted mouse events", ms3.count("Input.dispatchMouseEvent") == 3, ms3)
    mouse_types = [c[1].get("type") for c in fs3.send.calls if c[0] == "Input.dispatchMouseEvent"]
    chk(
        "click mouse seq move/press/release",
        mouse_types == ["mouseMoved", "mousePressed", "mouseReleased"],
        mouse_types,
    )
    chk("click did NOT need JS fallback", fs3.send.calls.count(("Runtime.callFunctionOn", fs3.send.calls)) == 0, "")

    # --- cdp_click: box-less node -> getBoundingClientRect returns null -> JS this.click() fallback ---
    boxless = _Node(7, None)
    fs4 = _FakeSession(call_value=None)  # _RECT_JS returns null -> no center -> JS click; JS click returns null too
    # make JS click report success: value depends on which fn; emulate by returning True for click body
    fs4.send._call_value = lambda p: True if "this.click()" in p["functionDeclaration"] else None
    ok4 = await cdp_click(fs4, boxless)
    ms4 = _methods(fs4.send)
    chk("click box-less -> True via JS fallback", ok4 is True, ok4)
    chk("click box-less dispatched NO mouse events", "Input.dispatchMouseEvent" not in ms4, ms4)
    chk("click box-less used callFunctionOn (rect + js click)", ms4.count("Runtime.callFunctionOn") >= 2, ms4)

    # --- cdp_click_xy: trusted mouse at absolute coords, no resolveNode needed ---
    fs5 = _FakeSession(call_value=True)
    ok5 = await cdp_click_xy(fs5, node, 120, 240)
    ms5 = _methods(fs5.send)
    chk("click_xy -> True", ok5 is True, ok5)
    chk("click_xy dispatched 3 mouse events", ms5.count("Input.dispatchMouseEvent") == 3, ms5)
    xy = [(c[1]["x"], c[1]["y"]) for c in fs5.send.calls if c[0] == "Input.dispatchMouseEvent"]
    chk("click_xy used the given coords", all(p == (120, 240) for p in xy), xy)

    # --- cdp_type keystrokes: clear (set_value '') -> focus -> per-char keyDown/char/keyUp ---
    # the focus-identity probe must answer True (focus IS on the target) for the happy path.
    fs6 = _FakeSession(
        call_value=lambda p: True if "activeElement" in p["functionDeclaration"] else ""
    )
    ok6 = await cdp_type(fs6, node, "ab", keystrokes=True, clear=True)
    ms6 = _methods(fs6.send)
    chk("type(keystrokes) -> True", ok6 is True, ok6)
    chk("type clears via callFunctionOn", "Runtime.callFunctionOn" in ms6, ms6)
    chk("type focuses via DOM.focus", "DOM.focus" in ms6, ms6)
    # 2 chars * 3 key events = 6 dispatchKeyEvent
    chk("type dispatched 6 key events (2 chars x 3)", ms6.count("Input.dispatchKeyEvent") == 6, ms6)
    key_types = [c[1].get("type") for c in fs6.send.calls if c[0] == "Input.dispatchKeyEvent"]
    chk("type key seq keyDown/char/keyUp per char", key_types == ["keyDown", "char", "keyUp"] * 2, key_types)
    char_texts = [
        c[1].get("text") for c in fs6.send.calls if c[0] == "Input.dispatchKeyEvent" and c[1].get("type") == "char"
    ]
    chk("type char events carry the chars", char_texts == ["a", "b"], char_texts)

    # --- cdp_type keystrokes REFUSED when focus sits elsewhere (cross-field bleed guard) ---
    # the probe answers False both before and after the JS refocus retry -> ZERO chars may be
    # dispatched (they would land in ANOTHER field) and the call must fail-closed.
    fs6b = _FakeSession(
        call_value=lambda p: False if "activeElement" in p["functionDeclaration"] else ""
    )
    ok6b = await cdp_type(fs6b, node, "ab", keystrokes=True, clear=True)
    chk("type(keystrokes) -> False when focus is NOT the target", ok6b is False, ok6b)
    chk(
        "focus-mismatch dispatched ZERO key events",
        "Input.dispatchKeyEvent" not in _methods(fs6b.send),
        _methods(fs6b.send),
    )
    chk(
        "focus-mismatch attempted a JS refocus first",
        any(
            "this.focus()" in (c[1].get("functionDeclaration") or "")
            for c in fs6b.send.calls
            if c[0] == "Runtime.callFunctionOn"
        ),
        "",
    )

    # --- cdp_type plain (keystrokes=False) routes to cdp_set_value (no key events) ---
    fs7 = _FakeSession(call_value=lambda p: p["arguments"][0]["value"])
    ok7 = await cdp_type(fs7, node, "plain", keystrokes=False)
    chk("type(plain) -> True via set_value", ok7 is True, ok7)
    chk("type(plain) dispatched NO key events", "Input.dispatchKeyEvent" not in _methods(fs7.send), _methods(fs7.send))

    # --- timeout guard: a hanging CDP call -> False, never hangs ---
    class _HangSession:
        async def cdp_client_for_node(self, node):
            await asyncio.sleep(999)

    import time as _t

    global CDP_ACTION_TIMEOUT
    saved = CDP_ACTION_TIMEOUT
    CDP_ACTION_TIMEOUT = 0.1
    t0 = _t.monotonic()
    timed = await cdp_set_value(_HangSession(), node, "x")
    elapsed = _t.monotonic() - t0
    CDP_ACTION_TIMEOUT = saved
    chk("timeout guard -> False", timed is False, timed)
    chk("timeout guard returned fast (<1s)", elapsed < 1.0, round(elapsed, 3))

    ok_all = True
    print("\n=== oa_cdp_action offline self-test (fake CDP, no browser, $0) ===")
    for name, passed, detail in checks:
        ok_all = ok_all and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail}")
    print(f"\n{'>>> ALL PASS' if ok_all else '>>> SOME FAIL'}  ({len(checks)} checks)")
    return 0 if ok_all else 1


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(_selftest()))


# json is imported for parity with the watchdog's json.dumps(text) value-embedding; our JS takes
# `text` as a callFunctionOn argument instead (safer than string-embedding), so json stays available
# for any future inline-embed path without re-import.
_ = json
