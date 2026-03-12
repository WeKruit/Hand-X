"""Shared visual highlight for DomHand actions.

Injects the same animated corner-bracket effect that browser-use uses
(`highlight_interaction_element`) so users can see exactly which element
a DomHand action is interacting with.
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

	var maxCornerSize = 20;
	var minCornerSize = 8;
	var cornerSize = Math.max(
		minCornerSize,
		Math.min(maxCornerSize, Math.min(rect.width, rect.height) * 0.35)
	);
	var borderWidth = 3;
	var startOffset = 10;
	var finalOffset = -3;

	var scrollX = window.pageXOffset || document.documentElement.scrollLeft || 0;
	var scrollY = window.pageYOffset || document.documentElement.scrollTop || 0;

	var container = document.createElement('div');
	container.setAttribute('data-domhand-highlight', 'true');
	container.style.cssText =
		'position:absolute;pointer-events:none;z-index:2147483647;' +
		'left:' + (rect.x + scrollX) + 'px;' +
		'top:' + (rect.y + scrollY) + 'px;' +
		'width:' + rect.width + 'px;' +
		'height:' + rect.height + 'px;';

	var corners = [
		{ top: '0', left: '0', right: '', bottom: '', bT: true, bL: true,
		  sx: -startOffset, sy: -startOffset, fx: finalOffset, fy: finalOffset },
		{ top: '0', left: '', right: '0', bottom: '', bT: true, bR: true,
		  sx: startOffset, sy: -startOffset, fx: -finalOffset, fy: finalOffset },
		{ top: '', left: '0', right: '', bottom: '0', bB: true, bL: true,
		  sx: -startOffset, sy: startOffset, fx: finalOffset, fy: -finalOffset },
		{ top: '', left: '', right: '0', bottom: '0', bB: true, bR: true,
		  sx: startOffset, sy: startOffset, fx: -finalOffset, fy: -finalOffset }
	];

	corners.forEach(function(c) {
		var b = document.createElement('div');
		b.style.cssText =
			'position:absolute;pointer-events:none;' +
			'width:' + cornerSize + 'px;height:' + cornerSize + 'px;' +
			'transition:all 0.15s ease-out;' +
			'transform:translate(' + c.sx + 'px,' + c.sy + 'px);';
		if (c.top !== '')   b.style.top = c.top;
		if (c.left !== '')  b.style.left = c.left;
		if (c.right !== '') b.style.right = c.right;
		if (c.bottom !== '') b.style.bottom = c.bottom;
		if (c.bT) b.style.borderTop    = borderWidth + 'px solid ' + color;
		if (c.bR) b.style.borderRight  = borderWidth + 'px solid ' + color;
		if (c.bB) b.style.borderBottom = borderWidth + 'px solid ' + color;
		if (c.bL) b.style.borderLeft   = borderWidth + 'px solid ' + color;
		container.appendChild(b);
		setTimeout(function() {
			b.style.transform = 'translate(' + c.fx + 'px,' + c.fy + 'px)';
		}, 10);
	});

	document.body.appendChild(container);
	setTimeout(function() {
		container.style.opacity = '0';
		container.style.transition = 'opacity 0.3s ease-out';
		setTimeout(function() { container.remove(); }, 300);
	}, durationMs);
	return true;
}"""

DEFAULT_COLOR = "rgb(255, 127, 39)"
DEFAULT_DURATION_MS = 1000


async def highlight_element(page, selector: str, color: str = DEFAULT_COLOR, duration_ms: int = DEFAULT_DURATION_MS) -> None:
	"""Show animated corner brackets around an element, matching browser-use's style.

	Safe to call fire-and-forget — failures are silently ignored.
	"""
	try:
		await page.evaluate(HIGHLIGHT_JS, selector, color, duration_ms)
	except Exception:
		pass  # Never fail the action because of a visual effect
