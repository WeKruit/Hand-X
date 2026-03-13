"""Global visual cursor — patches Mouse and Element to show cursor animations.

Call ``enable_visual_cursor()`` once (e.g. in register_domhand_actions) to add
cursor movement + click ripple visuals to ALL browser interactions globally.

Works by monkey-patching ``Mouse.click``, ``Mouse.move``, and ``Element.click``
so every click in browser-use (DomHand, generic actions, coordinate clicks) gets
a visual cursor pointer + click ripple automatically.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_enabled = False
_original_mouse_click = None
_original_mouse_move = None
_original_element_click = None


# ── JS snippets (raw IIFE expressions for Runtime.evaluate) ──────────

_INJECT_CURSOR_EXPR = r"""(function() {
    if (document.getElementById('gh-cursor-visual')) return 'exists';

    var style = document.createElement('style');
    style.id = 'gh-cursor-style';
    style.textContent = '\
        #gh-cursor-visual {\
            position: fixed; top: 0; left: 0;\
            z-index: 2147483646; pointer-events: none;\
            transition: transform 0.25s cubic-bezier(0.22, 1, 0.36, 1);\
            will-change: transform;\
        }\
        #gh-cursor-visual.gh-hidden { display: none !important; }\
        #gh-click-ripple {\
            position: fixed; width: 0; height: 0; border-radius: 50%;\
            background: rgba(96, 165, 250, 0.18);\
            border: 2px solid rgba(96, 165, 250, 0.72);\
            box-shadow: 0 0 0 8px rgba(96, 165, 250, 0.14);\
            pointer-events: none; z-index: 2147483645;\
            transform: translate(-50%, -50%); opacity: 0;\
        }\
        #gh-click-ripple.gh-ripple-active {\
            animation: gh-ripple-expand 0.5s cubic-bezier(0.22, 1, 0.36, 1) forwards;\
        }\
        @keyframes gh-ripple-expand {\
            0% { width: 0; height: 0; opacity: 0.8; }\
            100% { width: 50px; height: 50px; opacity: 0; }\
        }\
        #gh-action-label {\
            position: fixed; bottom: 16px; left: 50%;\
            transform: translateX(-50%); z-index: 2147483646;\
            pointer-events: none; background: rgba(2,6,23,0.84);\
            border: 1px solid rgba(96,165,250,0.24);\
            box-shadow: 0 12px 30px rgba(2,6,23,0.28);\
            color: #eff6ff; font-family: -apple-system, BlinkMacSystemFont, sans-serif;\
            font-size: 13px; padding: 6px 14px; border-radius: 999px;\
            opacity: 0; transition: opacity 0.2s ease;\
            white-space: nowrap; max-width: 400px; overflow: hidden; text-overflow: ellipsis;\
        }\
        #gh-action-label.gh-label-visible { opacity: 1; }\
    ';
    document.head.appendChild(style);

    var cursor = document.createElement('div');
    cursor.id = 'gh-cursor-visual';
    cursor.innerHTML = '<svg width="28" height="28" viewBox="0 0 28 28" fill="none">' +
        '<path d="M6 4L21 12.5L13.6 14.4L10.9 23L6 4Z" fill="#2563eb" stroke="#dbeafe" ' +
        'stroke-width="1.6" stroke-linejoin="round"/>' +
        '<circle cx="20.2" cy="20.4" r="3.1" fill="#93c5fd" opacity="0.98"/></svg>';
    document.body.appendChild(cursor);

    var ripple = document.createElement('div');
    ripple.id = 'gh-click-ripple';
    document.body.appendChild(ripple);

    var label = document.createElement('div');
    label.id = 'gh-action-label';
    document.body.appendChild(label);

    return 'injected';
})()"""


def _move_cursor_expr(x: int, y: int) -> str:
    return (
        f"(function(){{ var c=document.getElementById('gh-cursor-visual');"
        f"if(c){{ c.classList.remove('gh-hidden'); c.style.transform='translate({x}px,{y}px)'; }}}})() "
    )


def _click_ripple_expr(x: int, y: int) -> str:
    return (
        f"(function(){{ var r=document.getElementById('gh-click-ripple');"
        f"if(r){{ r.classList.remove('gh-ripple-active'); r.style.left='{x}px'; r.style.top='{y}px';"
        f"void r.offsetHeight; r.classList.add('gh-ripple-active'); }}"
        f"var c=document.getElementById('gh-cursor-visual');"
        f"if(c) c.classList.remove('gh-hidden'); }})() "
    )


_HIDE_CURSOR_EXPR = (
    "(function(){ var c=document.getElementById('gh-cursor-visual');if(c) c.classList.add('gh-hidden'); })()"
)


# ── CDP helpers ──────────────────────────────────────────────────────


async def _eval_safe(client, session_id: str | None, expression: str) -> None:
    """Run a JS expression via CDP Runtime.evaluate, swallowing errors."""
    if not session_id:
        return
    try:
        await client.send.Runtime.evaluate(
            params={"expression": expression, "returnByValue": True},
            session_id=session_id,
        )
    except Exception:
        pass


# ── Patched methods ──────────────────────────────────────────────────


async def _visual_mouse_click(self, x, y, button="left", click_count=1):
    """Mouse.click with visual cursor feedback."""
    client = self._client
    sid = self._session_id
    ix, iy = int(x), int(y)

    # Inject cursor overlay + move visual to target
    await _eval_safe(client, sid, _INJECT_CURSOR_EXPR)
    await _eval_safe(client, sid, _move_cursor_expr(ix, iy))
    await asyncio.sleep(0.15)

    # Hide visual cursor before real CDP click (avoid intercepting)
    await _eval_safe(client, sid, _HIDE_CURSOR_EXPR)
    await asyncio.sleep(0.03)

    # Real CDP click (isTrusted: true)
    await _original_mouse_click(self, x, y, button, click_count)

    # Show ripple after click
    await _eval_safe(client, sid, _click_ripple_expr(ix, iy))


async def _visual_mouse_move(self, x, y, steps=1):
    """Mouse.move with visual cursor."""
    client = self._client
    sid = self._session_id

    # Inject + move visual
    await _eval_safe(client, sid, _INJECT_CURSOR_EXPR)
    await _eval_safe(client, sid, _move_cursor_expr(int(x), int(y)))

    # Real CDP move
    await _original_mouse_move(self, x, y, steps)


async def _visual_element_click(self, button="left", click_count=1, modifiers=None):
    """Element.click with visual cursor at element center."""
    client = self._client
    sid = self._session_id
    cx, cy = 0, 0
    has_coords = False

    # Try to get element center for visual cursor
    try:
        box = await self.get_bounding_box()
        if box and box["width"] > 0 and box["height"] > 0:
            cx = int(box["x"] + box["width"] / 2)
            cy = int(box["y"] + box["height"] / 2)
            has_coords = True
            await _eval_safe(client, sid, _INJECT_CURSOR_EXPR)
            await _eval_safe(client, sid, _move_cursor_expr(cx, cy))
            await asyncio.sleep(0.12)
            await _eval_safe(client, sid, _HIDE_CURSOR_EXPR)
    except Exception:
        pass

    # Real click (uses CDP dispatchMouseEvent internally)
    await _original_element_click(self, button, click_count, modifiers)

    # Show ripple if we got coordinates
    if has_coords:
        await _eval_safe(client, sid, _click_ripple_expr(cx, cy))


# ── Public API ───────────────────────────────────────────────────────


def enable_visual_cursor() -> None:
    """Patch Mouse and Element globally to show visual cursor on all interactions."""
    global _enabled, _original_mouse_click, _original_mouse_move, _original_element_click

    if _enabled:
        return

    from browser_use.actor.element import Element
    from browser_use.actor.mouse import Mouse

    _original_mouse_click = Mouse.click
    _original_mouse_move = Mouse.move
    _original_element_click = Element.click

    Mouse.click = _visual_mouse_click
    Mouse.move = _visual_mouse_move
    Element.click = _visual_element_click

    _enabled = True
    logger.info("visual_cursor.enabled_globally")


def disable_visual_cursor() -> None:
    """Restore original Mouse and Element methods."""
    global _enabled

    if not _enabled:
        return

    from browser_use.actor.element import Element
    from browser_use.actor.mouse import Mouse

    Mouse.click = _original_mouse_click
    Mouse.move = _original_mouse_move
    Element.click = _original_element_click

    _enabled = False
    logger.info("visual_cursor.disabled")
