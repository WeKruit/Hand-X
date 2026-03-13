"""Shared visual highlight for DomHand actions.

Injects a Hand-X styled frame highlight so users can see exactly which
element a DomHand action is interacting with.
"""

# JS function that draws animated corner brackets around an element found
# by CSS selector (using __ff.queryAll for shadow DOM piercing).
# Matches the style from browser_use/browser/session.py:2715-2812.
HIGHLIGHT_JS = r"""(selector, color, durationMs) => {
	function qAll(sel) {
		if (window.__ff && window.__ff.queryAll) return window.__ff.queryAll(sel);
		return Array.from(document.querySelectorAll(sel));
	}
	var els = qAll(selector);
	if (els.length === 0) return false;
	var el = els[0];
	var rect = el.getBoundingClientRect();
	if (rect.width === 0 && rect.height === 0) return false;

	var scrollX = window.pageXOffset || document.documentElement.scrollLeft || 0;
	var scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;

	var container = document.createElement('div');
	container.setAttribute('data-domhand-highlight', 'true');
	container.style.cssText =
		'position:absolute;pointer-events:none;z-index:2147483647;opacity:0;' +
		'transform:scale(0.985);transition:opacity 0.18s ease-out,transform 0.18s ease-out;' +
		'left:' + (rect.x + scrollX) + 'px;' +
		'top:' + (rect.y + scrollY) + 'px;' +
		'width:' + rect.width + 'px;' +
		'height:' + rect.height + 'px;';

	var frame = document.createElement('div');
	frame.style.cssText =
		'position:absolute;inset:-4px;border-radius:12px;' +
		'border:2px solid ' + color + ';' +
		'background:linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0));' +
		'box-shadow:0 0 0 4px rgba(96,165,250,0.16), 0 10px 24px rgba(2,6,23,0.22);';
	container.appendChild(frame);

	var accent = document.createElement('div');
	accent.style.cssText =
		'position:absolute;top:-7px;right:10px;width:10px;height:10px;border-radius:999px;' +
		'background:' + color + ';box-shadow:0 0 0 4px rgba(96,165,250,0.22);';
	container.appendChild(accent);

	document.body.appendChild(container);
	requestAnimationFrame(function() {
		container.style.opacity = '1';
		container.style.transform = 'scale(1)';
	});
	setTimeout(function() {
		container.style.opacity = '0';
		container.style.transition = 'opacity 0.3s ease-out';
		setTimeout(function() { container.remove(); }, 300);
	}, durationMs);
	return true;
}"""

DEFAULT_COLOR = "rgb(37, 99, 235)"
DEFAULT_DURATION_MS = 1000


async def highlight_element(
    page, selector: str, color: str = DEFAULT_COLOR, duration_ms: int = DEFAULT_DURATION_MS
) -> None:
    """Show animated corner brackets around an element, matching browser-use's style.

    Safe to call fire-and-forget — failures are silently ignored.
    """
    try:
        await page.evaluate(HIGHLIGHT_JS, selector, color, duration_ms)
    except Exception:
        pass  # Never fail the action because of a visual effect
