"""oa_action — thin, TRUSTED wrappers over browser-use's OWN action primitives.

`observe_act` is a *deterministic* orchestrator: it drives browser-use's perception
(`DomService`) and its trusted-CDP actions (`tools`/watchdog) DIRECTLY, without the
expensive agent loop. This module is the action half — one thin wrapper per primitive,
each dispatching the SAME `BrowserSession.event_bus` event that `browser_use/tools/service.py`
dispatches, so every click/type/select goes through the trusted CDP path
(`default_action_watchdog`) with React-aware clearing, `elementFromPoint` occlusion reroute,
and char-by-char trusted keystrokes — none of it reinvented here.

REAL browser-use entrypoints reused (file:line, verified against the vendored tree):
  - Event definitions ......... browser_use/browser/events.py
        ClickElementEvent           :125   (node)            -> dict|None
        ClickCoordinateEvent        :136   (x,y,force)       -> dict
        TypeTextEvent               :147   (node,text,clear) -> dict|None  (char-by-char trusted)
        ScrollEvent                 :159   (direction,amount,node?) -> None
        SendKeysEvent               :240   (keys)            -> None       (Enter / ArrowDown / ...)
        UploadFileEvent             :248   (node,file_path)  -> None       (CDP setFileInputFiles)
        GetDropdownOptionsEvent     :257   (node)            -> dict[str,str]
        SelectDropdownOptionEvent   :269   (node,text)       -> dict[str,str]
  - Dispatch pattern (copied verbatim from tools/service.py:611-614):
        event = session.event_bus.dispatch(SomeEvent(...))
        await event
        result = await event.event_result(raise_if_any=..., raise_if_none=...)
  - dropdown_options tool (read path) .... browser_use/tools/service.py:1555
  - select_dropdown tool (commit path) ... browser_use/tools/service.py:1604
  - _click_by_index (trusted click) ...... browser_use/tools/service.py:584
  - _click_by_coordinate (trusted xy) .... browser_use/tools/service.py:540
  - input (char-by-char type) ............ browser_use/tools/service.py:658
  - scroll (page/element wheel) .......... browser_use/tools/service.py:1280
  - send_keys (trusted key) .............. browser_use/tools/service.py:1385
  - watchdog handlers .................... browser_use/browser/watchdogs/default_action_watchdog.py
        on_ClickElementEvent :338  on_TypeTextEvent :798  on_ScrollEvent :860
        on_SendKeysEvent :2770  on_UploadFileEvent :2975  on_GetDropdownOptionsEvent :3107
        on_SelectDropdownOptionEvent :3651
  - GetDropdownOptions return shape ...... default_action_watchdog.py:3421
        {'type', 'options': json.dumps([{index,text,value,selected}]), ...}
  - SelectDropdown success flag .......... default_action_watchdog.py / tools/service.py:1623
        selection_data.get('success') == 'true'

DESIGN NOTES (OBSERVE_ACT_DESIGN.md):
  - read_options/select_option  -> §3, S_CLOSED_LIST / S_NATIVE (the dropdown read+commit path).
  - click_node / click_xy       -> S3_OPEN trusted-open, commit-by-handle (C0+C1+VLM agree).
  - type_text + press_key       -> §5 search-loop (per-char type then Enter-on-highlight commit).
  - scroll                      -> §3.5 / S_CLOSED_LIST off-screen / virtualized reread.

HARD: fill-only. Nothing here submits a form; the only key helper sends individual keys
(Enter/ArrowDown/Backspace) the search-loop needs, never clicks a submit control.
"""

from __future__ import annotations

import json
import os

import oa_cdp_action as cdp

from browser_use.browser.events import (
    ClickCoordinateEvent,
    ClickElementEvent,
    GetDropdownOptionsEvent,
    ScrollEvent,
    SelectDropdownOptionEvent,
    SendKeysEvent,
    TypeTextEvent,
    UploadFileEvent,
)
from browser_use.browser.session import BrowserSession
from browser_use.browser.views import BrowserError
from browser_use.dom.views import EnhancedDOMTreeNode

__all__ = [
    "click_node",
    "click_xy",
    "press_key",
    "read_options",
    "scroll",
    "select_option",
    "type_text",
    "upload_file",
]


# ---------------------------------------------------------------------------
# ACTION BACKEND (BUILD FIX A) — direct-CDP is the DEFAULT for the write primitives
# (type/click/select), so they NEVER wait on browser-use's readiness watchdog. On a
# never-idle SPA /apply page (Lever / Ashby) the event-bus path's watchdog handlers wait for
# page readiness / navigation that never settles -> 30-60s TimeoutError per action -> fields
# ESCALATE. The direct-CDP path (oa_cdp_action) sets values + dispatches trusted events straight
# via CDP (the SAME resolveNode + callFunctionOn / Input.* plumbing the watchdog uses) WITHOUT
# that wait, mirroring the OLD direct-Playwright filler that hit ~99%.
#
# OA_ACTION_BACKEND=eventbus reverts to the original event-bus wrappers (the documented fallback).
# read_options / upload_file / scroll / press_key stay on the event-bus path: their watchdog
# handlers are read-only or one-shot (no readiness loop) and re-implementing the dropdown/file/
# scroll watchdogs would duplicate browser-use, not bypass a hang.
# ---------------------------------------------------------------------------
def _use_cdp() -> bool:
    return os.environ.get("OA_ACTION_BACKEND", "cdp").lower() != "eventbus"


# ---------------------------------------------------------------------------
# Dropdown read / commit (browser-use dropdown_options / select_dropdown path)
# ---------------------------------------------------------------------------


async def read_options(session: BrowserSession, node: EnhancedDOMTreeNode) -> list[str]:
    """Return the option *texts* of a dropdown (native <select> + ARIA combobox + custom widgets).

    Drives `GetDropdownOptionsEvent` exactly like tools/service.py:1566. The watchdog
    (default_action_watchdog.py:3107) handles native selects, ARIA comboboxes
    (`aria-controls`), and custom/role=option|menuitem widgets at child-depth 4, returning a
    dict whose `options` is a JSON string of `[{index,text,value,selected}, ...]`
    (watchdog :3421). We parse it down to the human-readable option texts.

    Returns `[]` when the widget exposes no inspectable options (e.g. a button/custom widget
    that must be opened+clicked instead) — observe_act then falls back to the delta/click path.

    NOTE: browser-use's watchdog (default_action_watchdog.py:3378) *raises* `BrowserError`
    ("…not recognizable dropdown types…") for any element it cannot classify as a native/ARIA/
    role=option dropdown — this includes the react-select `INPUT[role=combobox]` and plain
    `TEXTAREA` controls the typeahead search-loop must handle. That is the "no inspectable
    options" case, not a fatal error: we swallow it and return `[]` so the state machine falls
    through to the type+delta / click+delta path. (`raise_if_any=False` so the watchdog error is
    surfaced as data, and the BrowserError guard covers the dispatch raising directly.)
    """
    try:
        event = session.event_bus.dispatch(GetDropdownOptionsEvent(node=node))
        await event
        data = await event.event_result(raise_if_any=False, raise_if_none=False)
    except BrowserError:
        return []

    if not data or not isinstance(data, dict):
        return []
    if data.get("error"):  # watchdog reported "not a dropdown" / "no options" as data
        return []
    raw = data.get("options")
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []

    texts: list[str] = []
    for opt in parsed:
        if isinstance(opt, dict):
            text = opt.get("text")
            if text is not None and str(text).strip():
                texts.append(str(text))
    return texts


async def select_option(session: BrowserSession, node: EnhancedDOMTreeNode, text: str) -> bool:
    """Commit a dropdown option by text. DEFAULT = direct-CDP (oa_cdp_action.cdp_select) so a
    native <select> commit never waits on the readiness watchdog; falls back to the event-bus
    `SelectDropdownOptionEvent` path when OA_ACTION_BACKEND=eventbus.

    cdp_select handles native <select> (the intrinsic S_NATIVE / S_RECOMMIT path). For an
    ARIA/custom dropdown the cdp path returns False (no native <select>) and the state machine's
    delta/click commit (click_node on the option cell) is the right tool — so a False here simply
    routes the caller to its click path, exactly as before."""
    if _use_cdp():
        return await cdp.cdp_select(session, node, text)
    return await _select_option_eventbus(session, node, text)


async def _select_option_eventbus(session: BrowserSession, node: EnhancedDOMTreeNode, text: str) -> bool:
    """Event-bus fallback: commit via `SelectDropdownOptionEvent`.

    Mirrors tools/service.py:1604 / watchdog :3651: case-insensitive match with a
    multi-strategy commit (native value+events / aria-selected / custom click). The handler
    returns a dict with `success == 'true'` on a registered selection. Returns the boolean.
    """
    event = session.event_bus.dispatch(SelectDropdownOptionEvent(node=node, text=text))
    await event
    data = await event.event_result(raise_if_any=False, raise_if_none=False)
    if not data or not isinstance(data, dict):
        return False
    return str(data.get("success", "")).lower() == "true"


# ---------------------------------------------------------------------------
# Trusted clicks (browser-use _click_by_index / _click_by_coordinate path)
# ---------------------------------------------------------------------------


async def click_node(session: BrowserSession, node: EnhancedDOMTreeNode) -> bool:
    """Click a DOM node. DEFAULT = direct-CDP (oa_cdp_action.cdp_click): a trusted
    Input.dispatchMouseEvent at the node center (move/press/release), JS this.click() fallback for
    box-less/occluded nodes — NO readiness wait, works on radios/checkboxes/option cells. Falls
    back to the event-bus `ClickElementEvent` path when OA_ACTION_BACKEND=eventbus."""
    if _use_cdp():
        return await cdp.cdp_click(session, node)
    return await _click_node_eventbus(session, node)


async def _click_node_eventbus(session: BrowserSession, node: EnhancedDOMTreeNode) -> bool:
    """Event-bus fallback: TRUSTED CDP click via `ClickElementEvent` (tools/service.py:611).

    The watchdog (on_ClickElementEvent :338) scrolls into view and dispatches a real
    `Input.dispatchMouseEvent`, with `elementFromPoint` occlusion detection + reroute to the
    topmost hit element. Returns False if the watchdog reports a `validation_error`
    (e.g. the node is a <select> or a file input — those must use select_option / upload_file),
    True otherwise.
    """
    event = session.event_bus.dispatch(ClickElementEvent(node=node))
    await event
    meta = await event.event_result(raise_if_any=True, raise_if_none=False)
    return not (isinstance(meta, dict) and "validation_error" in meta)


async def click_xy(session: BrowserSession, x: int, y: int) -> bool:
    """Click at absolute viewport coordinates. DEFAULT = direct-CDP
    (oa_cdp_action.cdp_click_xy): a trusted Input.dispatchMouseEvent at (x,y) on the root CDP
    session — NO readiness wait. Falls back to the event-bus `ClickCoordinateEvent` path when
    OA_ACTION_BACKEND=eventbus."""
    if _use_cdp():
        return await cdp.cdp_click_xy(session, None, int(x), int(y))
    return await _click_xy_eventbus(session, x, y)


async def _click_xy_eventbus(session: BrowserSession, x: int, y: int) -> bool:
    """Event-bus fallback: TRUSTED CDP click at absolute coords via `ClickCoordinateEvent`.

    Mirrors tools/service.py:557 (`force=True` skips the file-input/select safety gate — the
    caller has already decided this is a plain clickable point, e.g. an option cell's center).
    Returns False on a reported `validation_error`, True otherwise.
    """
    event = session.event_bus.dispatch(ClickCoordinateEvent(coordinate_x=int(x), coordinate_y=int(y), force=True))
    await event
    meta = await event.event_result(raise_if_any=True, raise_if_none=False)
    return not (isinstance(meta, dict) and "validation_error" in meta)


# ---------------------------------------------------------------------------
# Trusted typing / keys (browser-use input / send_keys path)
# ---------------------------------------------------------------------------


def _is_typeahead(node: EnhancedDOMTreeNode) -> bool:
    """A combobox / autocomplete control needs REAL per-char keystrokes (debounced search/XHR);
    a plain text input/textarea can take a single React-aware value set (faster, one round-trip).
    Read off STANDARD DOM (role / aria-autocomplete) — no renameable key."""
    attrs = getattr(node, "attributes", None) or {}
    role = (attrs.get("role") or "").lower()
    if not role:
        ax = getattr(node, "ax_node", None)
        role = ((getattr(ax, "role", None) or "") if ax else "").lower()
    if role == "combobox" or role == "searchbox":
        return True
    aa = (attrs.get("aria-autocomplete") or "").lower()
    return aa not in ("", "none")


async def type_text(session: BrowserSession, node: EnhancedDOMTreeNode, text: str, clear: bool = True) -> bool:
    """Enter text into a node. DEFAULT = direct-CDP (oa_cdp_action.cdp_type), NO readiness wait:
      * combobox / aria-autocomplete  -> per-char trusted Input.dispatchKeyEvent (typeahead: the
        page's debounced search/XHR fires — what the §5 search-loop needs), clear-first via the
        React-aware native setter.
      * plain text / textarea / date  -> one React-aware value set (native setter + input/change),
        fast and reliable.
    Falls back to the event-bus `TypeTextEvent` (char-by-char) when OA_ACTION_BACKEND=eventbus."""
    if _use_cdp():
        return await cdp.cdp_type(session, node, text, keystrokes=_is_typeahead(node), clear=clear)
    return await _type_text_eventbus(session, node, text, clear)


async def _type_text_eventbus(
    session: BrowserSession, node: EnhancedDOMTreeNode, text: str, clear: bool = True
) -> bool:
    """Event-bus fallback: char-by-char TRUSTED type via `TypeTextEvent` (tools/service.py:683).

    The watchdog (on_TypeTextEvent :798) focuses the element, optionally React-aware clears it
    (`clear=True`), then emits per-keystroke trusted key events so debounced search/XHR fires —
    exactly what the §5 typeahead search-loop needs. Returns False on a reported
    `validation_error`, True otherwise.
    """
    event = session.event_bus.dispatch(TypeTextEvent(node=node, text=text, clear=clear))
    await event
    meta = await event.event_result(raise_if_any=True, raise_if_none=False)
    return not (isinstance(meta, dict) and "validation_error" in meta)


async def press_key(session: BrowserSession, key: str) -> None:
    """Send a single trusted key (Enter / ArrowDown / Backspace / ...) via `SendKeysEvent`.

    Mirrors tools/service.py:1388 / watchdog on_SendKeysEvent :2770. Keys go to the currently
    focused element (the one a preceding type_text focused), which is the design's
    `enter_on_highlight` commit and the ArrowDown highlight-walk. `key` uses browser-use's
    syntax, e.g. "Enter", "ArrowDown", "Backspace", "Escape". Fill-only: never a submit action.
    """
    event = session.event_bus.dispatch(SendKeysEvent(keys=key))
    await event
    await event.event_result(raise_if_any=True, raise_if_none=False)


# ---------------------------------------------------------------------------
# File upload (browser-use UploadFileEvent path) — CDP setFileInputFiles, NO click
# ---------------------------------------------------------------------------


async def upload_file(session: BrowserSession, node: EnhancedDOMTreeNode, path: str) -> bool:
    """Set a file on an `input[type=file]` via `UploadFileEvent` — CDP only, NEVER a click.

    Mirrors tools/service.py:900 / watchdog on_UploadFileEvent :2975 (CDP
    `DOM.setFileInputFiles`). A click would open the OS picker CDP cannot drive; this path sets
    files directly, tolerating a hidden/zero-box file input (S_FILE in the state machine).
    Returns True if the event completes without raising.
    """
    event = session.event_bus.dispatch(UploadFileEvent(node=node, file_path=path))
    await event
    await event.event_result(raise_if_any=True, raise_if_none=False)
    return True


# ---------------------------------------------------------------------------
# Scroll (browser-use ScrollEvent path) — page or element, for off-screen reread
# ---------------------------------------------------------------------------


async def scroll(session: BrowserSession, node: EnhancedDOMTreeNode | None, dy: int) -> None:
    """Scroll the page (node=None) or a specific element/overlay container by `dy` pixels.

    Mirrors tools/service.py:1366 / watchdog on_ScrollEvent :860, which maps a signed pixel
    delta onto `direction` + positive `amount`: `dy > 0` scrolls down, `dy < 0` up. Passing the
    overlay's container node scrolls THAT container (the §3.5 / S_CLOSED_LIST virtualized /
    off-screen option reread), not the page.
    """
    direction = "down" if dy >= 0 else "up"
    event = session.event_bus.dispatch(ScrollEvent(direction=direction, amount=abs(int(dy)), node=node))
    await event
    await event.event_result(raise_if_any=True, raise_if_none=False)


# ---------------------------------------------------------------------------
# Self-test: import + dry signature check (NO live browser, NO network)
# ---------------------------------------------------------------------------


def _self_test() -> None:
    import inspect

    # 1. Every public wrapper is an async coroutine function.
    wrappers = {
        "read_options": read_options,
        "select_option": select_option,
        "click_node": click_node,
        "click_xy": click_xy,
        "type_text": type_text,
        "press_key": press_key,
        "upload_file": upload_file,
        "scroll": scroll,
    }
    for name, fn in wrappers.items():
        assert inspect.iscoroutinefunction(fn), f"{name} must be async"

    # 2. Signatures match the documented thin-wrapper contract.
    expected = {
        "read_options": ["session", "node"],
        "select_option": ["session", "node", "text"],
        "click_node": ["session", "node"],
        "click_xy": ["session", "x", "y"],
        "type_text": ["session", "node", "text", "clear"],
        "press_key": ["session", "key"],
        "upload_file": ["session", "node", "path"],
        "scroll": ["session", "node", "dy"],
    }
    for name, params in expected.items():
        got = list(inspect.signature(wrappers[name]).parameters)
        assert got == params, f"{name} signature {got} != {params}"

    # 3. The browser-use events we dispatch accept the kwargs we pass (field presence check).
    assert {"node"} <= set(GetDropdownOptionsEvent.model_fields)
    assert {"node", "text"} <= set(SelectDropdownOptionEvent.model_fields)
    assert {"node"} <= set(ClickElementEvent.model_fields)
    assert {"coordinate_x", "coordinate_y", "force"} <= set(ClickCoordinateEvent.model_fields)
    assert {"node", "text", "clear"} <= set(TypeTextEvent.model_fields)
    assert {"keys"} <= set(SendKeysEvent.model_fields)
    assert {"node", "file_path"} <= set(UploadFileEvent.model_fields)
    assert {"direction", "amount", "node"} <= set(ScrollEvent.model_fields)

    # 4. EnhancedDOMTreeNode (the handle our wrappers take) has backend_node_id.
    assert "backend_node_id" in EnhancedDOMTreeNode.__dataclass_fields__

    # 5. BrowserSession exposes the event_bus we dispatch on.
    assert "event_bus" in BrowserSession.model_fields

    print("oa_action self-test OK: 8 wrappers, signatures + browser-use event fields verified")


if __name__ == "__main__":
    _self_test()
