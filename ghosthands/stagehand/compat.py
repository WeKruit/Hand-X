"""Compatibility helpers between browser-use BrowserSession and Stagehand.

browser-use connects to Chrome via CDP (not Playwright).  The resolved
WebSocket URL lives at ``browser_session.cdp_url`` after ``start()``.
Stagehand connects to the *same* browser process via that CDP URL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from browser_use.browser import BrowserSession

    from ghosthands.stagehand.layer import StagehandLayer

logger = structlog.get_logger(__name__)


def get_cdp_url(browser_session: Any) -> str | None:
    """Extract the resolved WebSocket CDP URL from a browser-use BrowserSession.

    Returns None if the session hasn't started or CDP URL isn't available.
    """
    url = getattr(browser_session, "cdp_url", None)
    if url and isinstance(url, str) and url.startswith("ws"):
        return url

    profile = getattr(browser_session, "browser_profile", None)
    if profile:
        url = getattr(profile, "cdp_url", None)
        if url and isinstance(url, str):
            return url

    return None


async def ensure_stagehand_for_session(
    browser_session: Any,
    *,
    model: str | None = None,
) -> StagehandLayer:
    """Get or initialize the StagehandLayer singleton, connecting to the browser-use browser.

    Safe to call repeatedly — only the first call initializes.
    Start is gated in layer.py: without a desktop proxy or Browserbase key,
    ``_do_start`` returns False immediately (no SEA binary spawn).
    """
    from ghosthands.stagehand.layer import get_stagehand_layer

    layer = get_stagehand_layer()
    if layer.is_available:
        return layer

    cdp_url = get_cdp_url(browser_session)
    if not cdp_url:
        logger.debug("stagehand.compat.no_cdp_url")
        return layer

    await layer.ensure_started(cdp_url, model=model)
    return layer
