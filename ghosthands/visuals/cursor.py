"""Visual cursor overlay — shows a pointer + click ripple in the browser.

Injected via page.evaluate() as a persistent DOM overlay. The cursor
element uses pointer-events: none so it never intercepts real clicks.

Usage:
    cursor = CursorVisual(page)
    await cursor.ensure_injected()
    await cursor.move(x, y)       # smooth cursor slide
    await cursor.click(x, y)      # move + ripple animation
    await cursor.hide()           # hide before real click
    await cursor.show()           # show after real click
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from browser_use.actor.page import Page

logger = logging.getLogger(__name__)

# CSS + HTML injected into the page for the cursor overlay.
# Uses a simple SVG arrow pointer and an expanding circle for click ripple.
_INJECT_CURSOR_JS = r"""() => {
    if (document.getElementById('gh-cursor-visual')) return 'already_injected';

    // ── Styles ──────────────────────────────────────────
    var style = document.createElement('style');
    style.id = 'gh-cursor-style';
    style.textContent = `
        #gh-cursor-visual {
            position: fixed;
            top: 0;
            left: 0;
            z-index: 2147483646;
            pointer-events: none;
            transition: transform 0.25s cubic-bezier(0.22, 1, 0.36, 1);
            will-change: transform;
        }
        #gh-cursor-visual.gh-hidden {
            display: none !important;
        }
        #gh-click-ripple {
            position: fixed;
            width: 0;
            height: 0;
            border-radius: 50%;
            background: rgba(96, 165, 250, 0.18);
            border: 2px solid rgba(96, 165, 250, 0.72);
            box-shadow: 0 0 0 8px rgba(96, 165, 250, 0.14);
            pointer-events: none;
            z-index: 2147483645;
            transform: translate(-50%, -50%);
            opacity: 0;
        }
        #gh-click-ripple.gh-ripple-active {
            animation: gh-ripple-expand 0.5s cubic-bezier(0.22, 1, 0.36, 1) forwards;
        }
        @keyframes gh-ripple-expand {
            0% {
                width: 0;
                height: 0;
                opacity: 0.8;
            }
            100% {
                width: 50px;
                height: 50px;
                opacity: 0;
            }
        }
        #gh-action-label {
            position: fixed;
            bottom: 16px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 2147483646;
            pointer-events: none;
            background: rgba(2, 6, 23, 0.84);
            border: 1px solid rgba(96, 165, 250, 0.24);
            box-shadow: 0 12px 30px rgba(2, 6, 23, 0.28);
            color: #eff6ff;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            font-size: 13px;
            padding: 6px 14px;
            border-radius: 999px;
            opacity: 0;
            transition: opacity 0.2s ease;
            white-space: nowrap;
            max-width: 400px;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        #gh-action-label.gh-label-visible {
            opacity: 1;
        }
    `;
    document.head.appendChild(style);

    // ── Cursor pointer (SVG arrow) ──────────────────────
    var cursor = document.createElement('div');
    cursor.id = 'gh-cursor-visual';
    cursor.innerHTML = `<svg width="28" height="28" viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M6 4L21 12.5L13.6 14.4L10.9 23L6 4Z" fill="#2563eb" stroke="#dbeafe" stroke-width="1.6" stroke-linejoin="round"/>
        <circle cx="20.2" cy="20.4" r="3.1" fill="#93c5fd" opacity="0.98"/>
    </svg>`;
    document.body.appendChild(cursor);

    // ── Click ripple ────────────────────────────────────
    var ripple = document.createElement('div');
    ripple.id = 'gh-click-ripple';
    document.body.appendChild(ripple);

    // ── Action label (bottom center) ────────────────────
    var label = document.createElement('div');
    label.id = 'gh-action-label';
    document.body.appendChild(label);

    return 'injected';
}"""

_MOVE_CURSOR_JS = r"""(x, y) => {
    var cursor = document.getElementById('gh-cursor-visual');
    if (cursor) {
        cursor.style.transform = 'translate(' + x + 'px, ' + y + 'px)';
    }
}"""

_CLICK_RIPPLE_JS = r"""(x, y) => {
    var ripple = document.getElementById('gh-click-ripple');
    if (ripple) {
        ripple.classList.remove('gh-ripple-active');
        ripple.style.left = x + 'px';
        ripple.style.top = y + 'px';
        // Force reflow to restart animation
        void ripple.offsetHeight;
        ripple.classList.add('gh-ripple-active');
    }
}"""

_HIDE_CURSOR_JS = r"""() => {
    var cursor = document.getElementById('gh-cursor-visual');
    if (cursor) cursor.classList.add('gh-hidden');
}"""

_SHOW_CURSOR_JS = r"""() => {
    var cursor = document.getElementById('gh-cursor-visual');
    if (cursor) cursor.classList.remove('gh-hidden');
}"""

_SHOW_LABEL_JS = r"""(text) => {
    var label = document.getElementById('gh-action-label');
    if (label) {
        label.textContent = text;
        label.classList.add('gh-label-visible');
    }
}"""

_HIDE_LABEL_JS = r"""() => {
    var label = document.getElementById('gh-action-label');
    if (label) {
        label.classList.remove('gh-label-visible');
    }
}"""

_REMOVE_CURSOR_JS = r"""() => {
    ['gh-cursor-visual', 'gh-click-ripple', 'gh-action-label', 'gh-cursor-style'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.remove();
    });
}"""


class CursorVisual:
    """Manages a visual cursor overlay in the browser page."""

    def __init__(self, page: Page):
        self._page = page
        self._injected = False

    async def ensure_injected(self) -> None:
        """Inject the cursor overlay if not already present."""
        if self._injected:
            return
        try:
            result = await self._page.evaluate(_INJECT_CURSOR_JS)
            self._injected = True
            logger.debug("cursor_visual.injected", extra={"result": result})
        except Exception as e:
            logger.debug(f"cursor_visual.inject_failed: {e}")

    async def move(self, x: int, y: int) -> None:
        """Move the visual cursor to (x, y)."""
        await self.ensure_injected()
        try:
            await self._page.evaluate(_MOVE_CURSOR_JS, x, y)
        except Exception:
            pass

    async def click_ripple(self, x: int, y: int) -> None:
        """Show a click ripple animation at (x, y)."""
        await self.ensure_injected()
        try:
            await self._page.evaluate(_CLICK_RIPPLE_JS, x, y)
        except Exception:
            pass

    async def hide(self) -> None:
        """Hide cursor (call before real CDP click to avoid intercepting)."""
        try:
            await self._page.evaluate(_HIDE_CURSOR_JS)
        except Exception:
            pass

    async def show(self) -> None:
        """Show cursor again after click."""
        try:
            await self._page.evaluate(_SHOW_CURSOR_JS)
        except Exception:
            pass

    async def show_label(self, text: str) -> None:
        """Show an action label at the bottom of the screen."""
        await self.ensure_injected()
        try:
            await self._page.evaluate(_SHOW_LABEL_JS, text)
        except Exception:
            pass

    async def hide_label(self) -> None:
        """Hide the action label."""
        try:
            await self._page.evaluate(_HIDE_LABEL_JS)
        except Exception:
            pass

    async def remove(self) -> None:
        """Remove all visual elements from the page."""
        try:
            await self._page.evaluate(_REMOVE_CURSOR_JS)
            self._injected = False
        except Exception:
            pass
