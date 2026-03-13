"""DomHand Close Popup — dismiss visible blocking modals and interstitials.

Handles generic popups that block job applications, such as newsletter prompts,
"Not ready to apply?" interstitials, promo dialogs, and other modals that dim
the page and steal focus.

Strategy:
1. Discover the most likely blocking popup/dialog in the current DOM
2. Prefer a visible close/dismiss button if present
3. Fall back to backdrop click or Escape when appropriate
4. Verify the popup is gone before returning success
"""

import asyncio
import json
import logging
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from ghosthands.actions.domhand_fill import _build_inject_helpers_js
from ghosthands.actions.views import DomHandClosePopupParams

logger = logging.getLogger(__name__)


_FIND_BLOCKING_POPUP_JS = r"""(targetText) => {
	const norm = (text) => (text || '').replace(/\s+/g, ' ').trim();
	const lower = (text) => norm(text).toLowerCase();
	const hint = lower(targetText || '');

	const ff = window.__ff || null;
	const qAll = (selector) => {
		if (ff && ff.queryAll) return ff.queryAll(selector);
		return Array.from(document.querySelectorAll(selector));
	};

	const isVisible = (el) => {
		if (!el) return false;
		const style = window.getComputedStyle(el);
		if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
		const rect = el.getBoundingClientRect();
		return rect.width > 0 && rect.height > 0;
	};

	const candidates = [];
	const seen = new Set();
	const selectors = [
		'[role="dialog"]',
		'[aria-modal="true"]',
		'dialog',
		'[class*="modal"]',
		'[class*="popup"]',
		'[class*="overlay"]',
		'[data-testid*="modal"]',
		'[data-test*="modal"]',
		'[id*="modal"]',
	];

	for (const selector of selectors) {
		for (const el of qAll(selector)) {
			if (!el || seen.has(el) || !isVisible(el)) continue;
			seen.add(el);
			if (el.matches('[role="listbox"], [role="menu"], [role="tooltip"]')) continue;

			const rect = el.getBoundingClientRect();
			if (rect.width < 120 || rect.height < 80) continue;

			const text = norm(el.innerText || el.textContent || '');
			const textLower = text.toLowerCase();
			const style = window.getComputedStyle(el);
			const centerDistance = Math.abs((rect.left + rect.width / 2) - window.innerWidth / 2)
				+ Math.abs((rect.top + rect.height / 2) - window.innerHeight / 2);

			let score = 0;
			if (el.matches('[role="dialog"], [aria-modal="true"], dialog')) score += 120;
			if (style.position === 'fixed') score += 40;
			else if (style.position === 'absolute') score += 20;
			if (rect.width >= window.innerWidth * 0.25) score += 20;
			if (rect.height >= window.innerHeight * 0.2) score += 20;
			if (centerDistance < window.innerWidth * 0.35) score += 25;
			const zIndex = parseInt(style.zIndex || '0', 10);
			if (!Number.isNaN(zIndex) && zIndex >= 10) score += Math.min(40, zIndex);
			if (hint && textLower.includes(hint)) score += 80;

			const closeCandidates = [];
			const closeNodes = Array.from(el.querySelectorAll('button, [role="button"], a, [aria-label], [title]'));
			for (const node of closeNodes) {
				if (!isVisible(node)) continue;
				const nodeRect = node.getBoundingClientRect();
				const label = norm(
					node.innerText ||
					node.textContent ||
					node.getAttribute('aria-label') ||
					node.getAttribute('title') ||
					''
				);
				const labelLower = label.toLowerCase();
				let closeScore = 0;
				if (/^(x|×|✕|close)$/.test(labelLower)) closeScore += 120;
				if (/(close|dismiss|not now|no thanks|skip|cancel|maybe later|continue later)/.test(labelLower)) closeScore += 100;
				if (hint && labelLower.includes(hint)) closeScore += 25;
				if (nodeRect.width <= 56 && nodeRect.height <= 56) closeScore += 10;
				if (nodeRect.right >= rect.right - 72 && nodeRect.top <= rect.top + 72) closeScore += 40;
				if (closeScore <= 0 && nodeRect.right >= rect.right - 56 && nodeRect.top <= rect.top + 56) {
					closeScore += 20;
				}
				if (closeScore > 0) {
					closeCandidates.push({
						label,
						score: closeScore,
						x: nodeRect.left + nodeRect.width / 2,
						y: nodeRect.top + nodeRect.height / 2,
					});
				}
			}
			closeCandidates.sort((a, b) => b.score - a.score);

			let backdrop = null;
			if (style.position === 'fixed' && rect.width >= window.innerWidth * 0.5 && rect.height >= window.innerHeight * 0.5) {
				backdrop = {
					x: Math.max(12, rect.left + 12),
					y: Math.max(12, rect.top + 12),
				};
			}

			candidates.push({
				score,
				text,
				close: closeCandidates[0] || null,
				backdrop,
			});
		}
	}

	candidates.sort((a, b) => b.score - a.score);
	const best = candidates[0];
	if (!best) {
		return JSON.stringify({ found: false });
	}

	return JSON.stringify({
		found: true,
		text: best.text.slice(0, 300),
		close: best.close,
		backdrop: best.backdrop,
	});
}"""


async def _find_blocking_popup(page: Any, target_text: str | None) -> dict[str, Any]:
    """Return the best visible blocking popup candidate on the current page."""
    try:
        raw = await page.evaluate(_FIND_BLOCKING_POPUP_JS, target_text or "")
    except Exception:
        return {"found": False}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {"found": False}
    return parsed if isinstance(parsed, dict) else {"found": False}


async def domhand_close_popup(params: DomHandClosePopupParams, browser_session: BrowserSession) -> ActionResult:
    """Dismiss a visible blocking popup or modal on the current page."""
    page = await browser_session.get_current_page()
    if not page:
        return ActionResult(error="No active page found in browser session")

    try:
        from ghosthands.dom.shadow_helpers import ensure_helpers

        await ensure_helpers(page)
    except Exception:
        pass

    await page.evaluate(_build_inject_helpers_js())

    candidate = await _find_blocking_popup(page, params.target_text)
    if not candidate.get("found"):
        return ActionResult(
            extracted_content="No blocking popup detected.",
            include_extracted_content_only_once=True,
        )

    attempts: list[str] = []
    mouse = await page.mouse

    close_target = candidate.get("close") or {}
    if close_target:
        try:
            attempts.append(f"close:{close_target.get('label') or 'button'}")
            await mouse.click(int(close_target["x"]), int(close_target["y"]))
            await asyncio.sleep(0.35)
        except Exception as exc:
            logger.debug(f"domhand_close_popup close click failed: {exc}")

    remaining = await _find_blocking_popup(page, params.target_text)
    if not remaining.get("found"):
        summary = f"Closed blocking popup via {attempts[-1]}." if attempts else "Closed blocking popup."
        return ActionResult(
            extracted_content=summary,
            include_extracted_content_only_once=True,
            metadata={"attempts": attempts},
        )

    backdrop_target = remaining.get("backdrop") or {}
    if backdrop_target:
        try:
            attempts.append("backdrop")
            await mouse.click(int(backdrop_target["x"]), int(backdrop_target["y"]))
            await asyncio.sleep(0.35)
        except Exception as exc:
            logger.debug(f"domhand_close_popup backdrop click failed: {exc}")

    remaining = await _find_blocking_popup(page, params.target_text)
    if not remaining.get("found"):
        return ActionResult(
            extracted_content="Closed blocking popup via backdrop click.",
            include_extracted_content_only_once=True,
            metadata={"attempts": attempts},
        )

    try:
        attempts.append("escape")
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.25)
    except Exception as exc:
        logger.debug(f"domhand_close_popup escape failed: {exc}")

    remaining = await _find_blocking_popup(page, params.target_text)
    if not remaining.get("found"):
        return ActionResult(
            extracted_content="Closed blocking popup via Escape.",
            include_extracted_content_only_once=True,
            metadata={"attempts": attempts},
        )

    popup_text = str(remaining.get("text") or candidate.get("text") or "").strip()
    return ActionResult(
        error="Blocking popup is still visible after close attempts.",
        extracted_content=f"Popup still visible: {popup_text[:200]}",
        metadata={"attempts": attempts, "popup_text": popup_text[:300]},
    )
