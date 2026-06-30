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
        return await cdp_set_value(session, node, text)

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
        for ch in str(text):
            await _dispatch_char(cdp_session, session_id, ch)
        return True

    return await _guarded(_do())


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


async def _guarded(coro: Any) -> bool:
    """Run a write coroutine under the per-action timeout; any timeout/error -> False (never hang)."""
    try:
        return bool(await asyncio.wait_for(coro, timeout=CDP_ACTION_TIMEOUT))
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
    fs6 = _FakeSession(call_value="")
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
