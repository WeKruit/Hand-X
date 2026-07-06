"""oa_cdp_core — the pure-CDP action core (user mandate: 'CDP 做 action,不要 browser-use agent').

An ATS filler needs six verbs. Every verb here:
  * resolves its target FRESH by CSS selector at call time (no browser-use selector_map, no
    cached nodes — the stale-node class died with this),
  * executes via direct CDP (trusted Input events / native setters / DOM.setFileInputFiles),
  * verifies by DOM read-back and returns the committed evidence ('' = did not commit).

The LLM's ONLY place is choosing the VALUE upstream (map_fields). Targeting, committing and
verifying are deterministic. browser-use's machine remains as the L3 fallback path only.

Every interaction primitive in here is live-CDP-proven (greenhouse embed sessions, 2026-07-05):
keyboard open (focus+ArrowDown), aria-controls scoped option read, trusted rect click with the
rect read AT CLICK TIME, checked re-read after settle, fresh-node setFileInputFiles.
"""

import asyncio
import contextlib
import json
from typing import Any

import oa_cdp_action as cdpa

_TIMEOUT = 12.0


async def _resolve_sel(session: Any, css: str) -> tuple[Any, Any, str] | None:
    """(cdp_session, session_id, object_id) for the FIRST match of css — fresh every call."""
    with contextlib.suppress(Exception):
        cdp_session = await session.get_or_create_cdp_session()
        sid = cdp_session.session_id
        r = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": f"document.querySelector({json.dumps(css)})", "returnByValue": False},
            session_id=sid,
        )
        oid = ((r or {}).get("result") or {}).get("objectId")
        if oid:
            return cdp_session, sid, oid
    return None


def selectors_for(name: str) -> list[str]:
    """Candidate selectors for a discovery identity (input id first, then name attr)."""
    esc = name.replace("\\", "\\\\").replace('"', '\\"')
    out = []
    if name and not name.startswith(("#", "[", ".")):
        out.append(f'[id="{esc}"]')
        out.append(f'[name="{esc}"]')
    else:
        out.append(name)
    return out


async def _first(session: Any, name: str) -> tuple[Any, Any, str] | None:
    for css in selectors_for(name):
        r = await _resolve_sel(session, css)
        if r is not None:
            return r
    return None


_DESCEND_JS = r"""
function(){
  if (this.matches && this.matches('input:not([type=hidden]),textarea,select,button')) return this;
  return this.querySelector(
    'input[role=combobox],input[aria-autocomplete],button[aria-haspopup="listbox"],select,input:not([type=hidden]),textarea,button[aria-haspopup]'
  ) || this;
}
"""


async def _trigger(session: Any, name: str) -> tuple[Any, Any, str] | None:
    """Resolve the identity, then DESCEND to the actionable control inside it (a discovery
    name is often the role=group wrapper — duolingo's aria-select — while the trigger is the
    button/input within)."""
    r = await _first(session, name)
    if r is None:
        return None
    cdp_session, sid, oid = r
    with contextlib.suppress(Exception):
        res = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
            params={"functionDeclaration": _DESCEND_JS, "objectId": oid, "returnByValue": False},
            session_id=sid,
        )
        oid2 = ((res or {}).get("result") or {}).get("objectId")
        if oid2:
            return cdp_session, sid, oid2
    return r


# ---------------------------------------------------------------- set_text --
_SET_JS = r"""
function(want){
  let el = this;
  if(!(el.matches && el.matches('input,textarea'))) el = el.querySelector('input,textarea');
  if(!el) return "";
  const proto = el.tagName==='TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const d = Object.getOwnPropertyDescriptor(proto,'value');
  d.set.call(el, want);
  el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
  el.dispatchEvent(new Event('blur',{bubbles:true}));
  return el.value || "";
}
"""


async def set_text(session: Any, name: str, value: str) -> str:
    """Native-setter text write; returns the read-back value ('' = failed)."""
    r = await _first(session, name)
    if r is None:
        return ""
    cdp_session, sid, oid = r
    with contextlib.suppress(Exception):
        got = await asyncio.wait_for(
            cdpa._call_on(cdp_session, sid, oid, _SET_JS, args=[str(value)]), timeout=_TIMEOUT)
        return str(got or "")
    return ""


# ------------------------------------------------------------ choose_option --
async def _open(cdp_session: Any, sid: Any, oid: str) -> None:
    """Open a closed select trigger. INPUT-style (react-select): focus + ArrowDown — clicks do
    NOT open that build (live-proven). BUTTON-style (aria disclosure): trusted click at the
    trigger's fresh rect — ArrowDown does NOT open those (duolingo, live-proven)."""
    state = {}
    with contextlib.suppress(Exception):
        state = await cdpa._call_on(cdp_session, sid, oid, r"""
function(){ const ac = this.getAttribute('aria-controls') || this.getAttribute('aria-owns');
  return {tag: this.tagName, exp: this.getAttribute('aria-expanded'),
          open: !!(ac && document.getElementById(ac))}; }""") or {}
    tag = str(state.get("tag") or "")
    # already open — for a BUTTON a second click TOGGLES it closed, so skip. For an INPUT-style
    # react-select, focus+ArrowDown on an open menu only moves the highlight (never closes), and a
    # STALE aria-expanded=true left by a prior rung (that opened then failed to scope) would make an
    # early-return read a menu that is actually CLOSED -> read_options=0 (twilio in-engine: fresh
    # probe reads 4, but after a sibling rung thrashed the control the reader saw stale-true and
    # skipped the real open). So only trust the early-return for BUTTON triggers.
    if (state.get("open") is True or str(state.get("exp")) == "true") and tag.upper() == "BUTTON":
        return
    if tag.upper() == "BUTTON":
        rect = await cdpa._call_on(cdp_session, sid, oid, r"""
function(){ this.scrollIntoView({block:'center'}); const r=this.getBoundingClientRect();
  return {x:r.x+r.width/2, y:r.y+r.height/2}; }""")
        if isinstance(rect, dict) and rect.get("x") is not None:
            await cdpa._dispatch_mouse_click(cdp_session, sid, rect["x"], rect["y"])
    else:
        # SCROLL INTO VIEW before focus (as the BUTTON path already does): after many prior fields
        # the react-select is off-screen; focusing an off-screen react-select input does NOT mount
        # its menu, so the aria-controls listbox never appears and the scoped read misses (twilio
        # in-engine read_options=0 while a fresh on-screen probe reads 4). Center it, then open.
        await cdpa._call_on(cdp_session, sid, oid, "function(){ this.scrollIntoView({block:'center'}); this.focus(); }")
        key = cdp_session.cdp_client.send.Input.dispatchKeyEvent
        for typ in ("keyDown", "keyUp"):
            await key(params={"type": typ, "key": "ArrowDown", "code": "ArrowDown",
                              "windowsVirtualKeyCode": 40, "nativeVirtualKeyCode": 40}, session_id=sid)
    await asyncio.sleep(0.6)


async def choose_option(session: Any, name: str, option_text: str) -> str:
    """Open + aria-scoped read + trusted rect click. option_text must be the EXACT
    option string (the caller already decided the value — no semantic re-pick here).
    Returns the control's rendered text on commit, '' otherwise."""
    r = await _trigger(session, name)
    if r is None:
        return ""
    cdp_session, sid, oid = r
    with contextlib.suppress(Exception):
        await _open(cdp_session, sid, oid)
        rect = await cdpa._call_on(cdp_session, sid, oid, cdpa._RS_OPTION_RECT_JS, args=[str(option_text)])
        if not (isinstance(rect, dict) and rect.get("x") is not None):
            # menu may need a filter for long lists: type the option text, re-read
            await cdpa._call_on(cdp_session, sid, oid, _SET_JS, args=[str(option_text)[:24]])
            await asyncio.sleep(0.6)
            rect = await cdpa._call_on(cdp_session, sid, oid, cdpa._RS_OPTION_RECT_JS, args=[str(option_text)])
        if isinstance(rect, dict) and rect.get("x") is not None:
            await cdpa._dispatch_mouse_click(cdp_session, sid, rect["x"], rect["y"])
            await asyncio.sleep(0.3)
            got = await cdpa._call_on(cdp_session, sid, oid, _READBACK_JS)
            return str(got or "").strip()
    return ""


# read-back that survives hash-classed widgets: react-select control climb first, then the
# ARIA group's own rendered text (duolingo: the value span is a SIBLING of the button).
_READBACK_JS = r"""
function(){
  let ctrl = this;
  for(let i=0;i<6 && ctrl;i++){ if(/(^|[^a-z])control([^a-z]|$)|select__control|Control/i.test(String(ctrl.className))) break; ctrl = ctrl.parentElement; }
  let t = "";
  if (ctrl && /control/i.test(String(ctrl.className))) t = (ctrl.innerText||'').replace(/\s+/g,' ').trim();
  if (!t) {
    const g = this.closest('[role=group],[aria-required]') || this.parentElement;
    t = ((g && g.innerText)||'').replace(/\s+/g,' ').trim();
  }
  return /^(select|choose|start typing|pick)/i.test(t) ? "" : t;
}
"""


async def read_options(session: Any, name: str) -> list[str]:
    """Open (keyboard) and read the scoped option texts — for the caller's value decision."""
    r = await _trigger(session, name)
    if r is None:
        return []
    cdp_session, sid, oid = r
    with contextlib.suppress(Exception):
        await _open(cdp_session, sid, oid)
        raw = await cdpa._call_on(cdp_session, sid, oid, cdpa._RS_OPEN_READ_JS, args=[True])
        opts = json.loads(raw) if raw else []
        if isinstance(opts, list):
            return [str(o) for o in opts]
    return []


# ---------------------------------------------------------------- choose --
async def choose(session: Any, group_name: str, want: str) -> str:
    """Radio/checkbox commit by value/label identity, checked re-read after settle
    (delegates to the proven cdp_choose_option, which already re-verifies)."""
    r = await _first(session, group_name)
    node_container = None
    if r is None:
        return ""
    # cdp_choose_option works on a browser-use node; here we call its JS directly instead.
    cdp_session, sid, oid = r
    with contextlib.suppress(Exception):
        got = await asyncio.wait_for(
            cdpa._call_on(cdp_session, sid, oid, cdpa._CHOOSE_OPTION_JS, args=[str(want), str(group_name)]),
            timeout=_TIMEOUT)
        if not got:
            return ""
        await asyncio.sleep(0.3)
        still = await cdpa._call_on(cdp_session, sid, oid, cdpa._STILL_CHECKED_JS if hasattr(cdpa, "_STILL_CHECKED_JS") else "function(){return true}", args=[str(group_name)])
        return str(got).strip() if still is not False else ""
    return ""


# ---------------------------------------------------------------- upload --
async def upload(session: Any, name: str, path: str) -> bool:
    """Fresh DOM query -> DOM.setFileInputFiles -> card-scoped chip read-back."""
    r = await _first(session, name)
    if r is None:
        return False
    cdp_session, sid, oid = r
    with contextlib.suppress(Exception):
        nid = (await cdp_session.cdp_client.send.DOM.requestNode(
            params={"objectId": oid}, session_id=sid)).get("nodeId")
        if not nid:
            return False
        await cdp_session.cdp_client.send.DOM.setFileInputFiles(
            params={"files": [str(path)], "nodeId": nid}, session_id=sid)
        await asyncio.sleep(1.5)
        import os as _os

        needle = _os.path.basename(str(path))[:8].lower()
        got = await cdpa._call_on(cdp_session, sid, oid, r"""
function(n){
  let p = this.parentElement;
  for(let i=0;i<8&&p;i++){
    if(p.querySelectorAll('input[type=file]').length>1) break;
    if(((p.innerText||'').toLowerCase()).includes(n)) return true;
    p = p.parentElement;
  }
  return false;
}""", args=[needle])
        if got is True:
            return True
        # the widget may CONSUME the input on attach (greenhouse) — chip renders, node gone:
        # a page-level re-query that no longer finds the input + finds the chip = success
        chip = await _resolve_sel(session, "body")
        if chip:
            _, sid2, oid2 = chip
            got2 = await cdpa._call_on(cdp_session, sid2, oid2,
                "function(n){ return (this.innerText||'').toLowerCase().includes(n); }", args=[needle])
            return got2 is True
    return False


# ----------------------------------------------------------------- probe --
_PROBE_JS = r"""
function(){
  const el = this;
  const a = (x,k)=>x&&x.getAttribute&&x.getAttribute(k);
  return JSON.stringify({
    tag: el.tagName, type: el.type||null, role: a(el,'role'),
    haspopup: a(el,'aria-haspopup'), controls: a(el,'aria-controls')||a(el,'aria-owns'),
    expanded: a(el,'aria-expanded'), required: el.required||a(el,'aria-required'),
    value: el.value!==undefined?String(el.value).slice(0,40):null,
    group: (()=>{const g=el.closest('[role=group],[aria-required]'); return g?{id:g.id,req:a(g,'aria-required')}:null})(),
  });
}
"""


async def probe(session: Any, name: str) -> dict:
    """The interrogation battery, as a runtime action: what IS this control."""
    r = await _trigger(session, name)
    if r is None:
        return {"found": False}
    cdp_session, sid, oid = r
    with contextlib.suppress(Exception):
        raw = await cdpa._call_on(cdp_session, sid, oid, _PROBE_JS)
        d = json.loads(raw) if raw else {}
        d["found"] = True
        return d
    return {"found": False}
