"""Blocker detection — detects CAPTCHAs, login walls, 2FA prompts, rate limits, and bot checks.

Ported from GHOST-HANDS BlockerDetector.ts with the full pattern sets:
- URL patterns: known blocker redirect URLs
- DOM selector patterns: CAPTCHA iframes, challenge elements, login forms
- Text regex patterns: "verify you are human", "security check", etc.
- Non-blocking reCAPTCHA v2/v3 differentiation (invisible badges are NOT blockers)

Works with Playwright Page objects from browser-use.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
	from playwright.async_api import Page


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class BlockerType(str, Enum):
	"""Types of blockers that can be detected on a page."""
	CAPTCHA = "captcha"
	LOGIN_REQUIRED = "login_required"
	TWO_FACTOR = "2fa"
	RATE_LIMITED = "rate_limited"
	BOT_DETECTION = "bot_detection"
	VERIFICATION = "verification"
	UNKNOWN = "unknown"


class BlockerDetection(BaseModel):
	"""Result of a blocker detection scan."""
	detected: bool = Field(description="Whether a blocker was detected.")
	blocker_type: BlockerType | None = Field(
		default=None,
		description="Type of blocker detected, if any.",
	)
	confidence: float = Field(
		default=0.0,
		ge=0.0,
		le=1.0,
		description="Confidence score from 0.0 to 1.0.",
	)
	details: str = Field(
		default="",
		description="Human-readable description of what was detected.",
	)
	selector: str | None = Field(
		default=None,
		description="CSS selector that matched, if applicable.",
	)
	source: str = Field(
		default="dom",
		description="Detection method: 'url', 'dom_selector', 'dom_text'.",
	)


# ---------------------------------------------------------------------------
# URL-based blocker patterns
# ---------------------------------------------------------------------------
# Catches redirects to known blocker pages (cheapest check — string only).

_URL_PATTERNS: list[tuple[re.Pattern[str], BlockerType, float]] = [
	(re.compile(r"google\.com/sorry", re.IGNORECASE), BlockerType.CAPTCHA, 0.95),
	(re.compile(r"cloudflare\.com/cdn-cgi/challenge", re.IGNORECASE), BlockerType.BOT_DETECTION, 0.95),
	(re.compile(r"challenges\.cloudflare\.com", re.IGNORECASE), BlockerType.BOT_DETECTION, 0.95),
	(re.compile(r"captcha", re.IGNORECASE), BlockerType.CAPTCHA, 0.6),
]


# ---------------------------------------------------------------------------
# DOM selector patterns
# ---------------------------------------------------------------------------
# Evaluated via page.evaluate() — checks for known blocker DOM elements.
# Each entry: (selector, blocker_type, base_confidence)

_SELECTOR_PATTERNS: list[tuple[str, BlockerType, float]] = [
	# -- CAPTCHA --
	('iframe[src*="recaptcha"]', BlockerType.CAPTCHA, 0.95),
	('iframe[src*="hcaptcha"]', BlockerType.CAPTCHA, 0.95),
	(".g-recaptcha", BlockerType.CAPTCHA, 0.9),
	(".h-captcha", BlockerType.CAPTCHA, 0.9),
	("#captcha", BlockerType.CAPTCHA, 0.7),
	("[data-captcha]", BlockerType.CAPTCHA, 0.7),
	('iframe[src*="challenges.cloudflare.com"]', BlockerType.CAPTCHA, 0.95),
	('iframe[src*="funcaptcha"]', BlockerType.CAPTCHA, 0.9),
	("#FunCaptcha", BlockerType.CAPTCHA, 0.85),

	# -- Audio CAPTCHA --
	(".rc-audiochallenge", BlockerType.CAPTCHA, 0.95),
	("#audio-source", BlockerType.CAPTCHA, 0.9),
	('audio[src*="captcha"]', BlockerType.CAPTCHA, 0.85),

	# -- Login --
	('form[action*="login"]', BlockerType.LOGIN_REQUIRED, 0.8),
	('form[action*="signin"]', BlockerType.LOGIN_REQUIRED, 0.8),
	('form[action*="sign-in"]', BlockerType.LOGIN_REQUIRED, 0.8),
	('input[type="password"]', BlockerType.LOGIN_REQUIRED, 0.6),
	("#login-form", BlockerType.LOGIN_REQUIRED, 0.85),
	('[data-testid="login-form"]', BlockerType.LOGIN_REQUIRED, 0.85),

	# -- Bot check --
	("#challenge-running", BlockerType.BOT_DETECTION, 0.95),
	("#cf-challenge-running", BlockerType.BOT_DETECTION, 0.95),
	(".cf-browser-verification", BlockerType.BOT_DETECTION, 0.9),
	("#px-captcha", BlockerType.BOT_DETECTION, 0.9),
	("[data-datadome]", BlockerType.BOT_DETECTION, 0.85),

	# -- Visual verification --
	(".slider-captcha", BlockerType.VERIFICATION, 0.85),
	("[data-slider-captcha]", BlockerType.VERIFICATION, 0.85),
]


# ---------------------------------------------------------------------------
# Text regex patterns
# ---------------------------------------------------------------------------
# Matched against document.body.innerText (first 5000 chars).

_TEXT_PATTERNS: list[tuple[re.Pattern[str], BlockerType, float]] = [
	# -- CAPTCHA text --
	(re.compile(r"please complete the (security |captcha )?check", re.IGNORECASE), BlockerType.CAPTCHA, 0.75),
	(re.compile(r"verify you('re| are) (a )?human", re.IGNORECASE), BlockerType.CAPTCHA, 0.8),
	(re.compile(r"prove you('re| are) not a robot", re.IGNORECASE), BlockerType.CAPTCHA, 0.8),
	(re.compile(r"i('|')m not a robot", re.IGNORECASE), BlockerType.CAPTCHA, 0.85),
	(re.compile(r"cloudflare.*turnstile", re.IGNORECASE), BlockerType.CAPTCHA, 0.8),

	# -- 2FA --
	(re.compile(r"two[- ]?factor auth", re.IGNORECASE), BlockerType.TWO_FACTOR, 0.85),
	(re.compile(r"verification code", re.IGNORECASE), BlockerType.TWO_FACTOR, 0.7),
	(re.compile(r"authenticator app", re.IGNORECASE), BlockerType.TWO_FACTOR, 0.85),
	(re.compile(r"enter the code sent to", re.IGNORECASE), BlockerType.TWO_FACTOR, 0.8),
	(re.compile(r"security code", re.IGNORECASE), BlockerType.TWO_FACTOR, 0.6),
	(re.compile(r"email verification", re.IGNORECASE), BlockerType.TWO_FACTOR, 0.7),

	# -- Login text --
	(re.compile(r"sign in to continue", re.IGNORECASE), BlockerType.LOGIN_REQUIRED, 0.85),
	(re.compile(r"session (has )?expired", re.IGNORECASE), BlockerType.LOGIN_REQUIRED, 0.8),
	(re.compile(r"please (log|sign) ?in", re.IGNORECASE), BlockerType.LOGIN_REQUIRED, 0.75),

	# -- Bot check text --
	(re.compile(r"checking your browser", re.IGNORECASE), BlockerType.BOT_DETECTION, 0.85),
	(re.compile(r"just a moment", re.IGNORECASE), BlockerType.BOT_DETECTION, 0.5),
	(re.compile(r"please wait while we verify", re.IGNORECASE), BlockerType.BOT_DETECTION, 0.8),
	(re.compile(r"access denied", re.IGNORECASE), BlockerType.BOT_DETECTION, 0.5),
	(re.compile(r"blocked by.*security", re.IGNORECASE), BlockerType.BOT_DETECTION, 0.7),
	(re.compile(r"are you a (ro)?bot", re.IGNORECASE), BlockerType.BOT_DETECTION, 0.85),
	(re.compile(r"please verify you('re| are) human", re.IGNORECASE), BlockerType.BOT_DETECTION, 0.85),

	# -- Rate limiting --
	(re.compile(r"too many requests", re.IGNORECASE), BlockerType.RATE_LIMITED, 0.9),
	(re.compile(r"please try again later", re.IGNORECASE), BlockerType.RATE_LIMITED, 0.65),
	(re.compile(r"rate limit(ed)?", re.IGNORECASE), BlockerType.RATE_LIMITED, 0.85),
	(re.compile(r"429", re.IGNORECASE), BlockerType.RATE_LIMITED, 0.5),

	# -- Audio CAPTCHA text --
	(re.compile(r"press play and type what you hear", re.IGNORECASE), BlockerType.CAPTCHA, 0.9),
	(re.compile(r"listen and type the numbers", re.IGNORECASE), BlockerType.CAPTCHA, 0.9),
	(re.compile(r"audio challenge", re.IGNORECASE), BlockerType.CAPTCHA, 0.85),
	(re.compile(r"switch to audio", re.IGNORECASE), BlockerType.CAPTCHA, 0.7),

	# -- Visual verification text --
	(re.compile(r"select all images with", re.IGNORECASE), BlockerType.VERIFICATION, 0.9),
	(re.compile(r"slide to (verify|unlock)", re.IGNORECASE), BlockerType.VERIFICATION, 0.85),
	(re.compile(r"drag the (slider|puzzle)", re.IGNORECASE), BlockerType.VERIFICATION, 0.85),
]


# ---------------------------------------------------------------------------
# JavaScript for DOM detection
# ---------------------------------------------------------------------------
# Runs inside page.evaluate() — must be self-contained, no imports.
# Returns a list of {selector, type, confidence, visible, isNonBlocking}.

_DOM_DETECT_JS = """
(patterns) => {
	const found = [];
	for (const p of patterns) {
		const el = document.querySelector(p.selector);
		if (el) {
			const rect = el.getBoundingClientRect();
			const visible = rect.width > 0 && rect.height > 0;

			// Detect non-blocking reCAPTCHA (invisible v2 / v3).
			// These are embedded on many forms but do NOT block the user —
			// they validate silently on submit.
			let isNonBlocking = false;
			if (p.selector.includes('recaptcha') || p.selector === '.g-recaptcha') {
				const invisibleWidget = document.querySelector('.g-recaptcha[data-size="invisible"]');
				const badge = document.querySelector('.grecaptcha-badge');
				const v3Script = document.querySelector('script[src*="recaptcha"][src*="render="]');
				const isSmallIframe = el.tagName === 'IFRAME' && rect.width < 100 && rect.height < 100;
				if (invisibleWidget || badge || v3Script || isSmallIframe) {
					isNonBlocking = true;
				}
			}

			found.push({
				selector: p.selector,
				type: p.type,
				confidence: p.confidence,
				visible: visible,
				isNonBlocking: isNonBlocking,
			});
		}
	}
	return found;
}
"""

_TEXT_EXTRACT_JS = """
() => {
	return document.body ? document.body.innerText.substring(0, 5000) : '';
}
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_url_for_blockers(url: str) -> BlockerDetection | None:
	"""Check a URL against known blocker URL patterns (cheapest check).

	Returns a BlockerDetection if the URL matches, or None.
	"""
	for pattern, blocker_type, confidence in _URL_PATTERNS:
		if pattern.search(url):
			return BlockerDetection(
				detected=True,
				blocker_type=blocker_type,
				confidence=confidence,
				details=f"URL matches blocker pattern: {pattern.pattern}",
				source="url",
			)
	return None


async def detect_blockers(page: Page) -> BlockerDetection:
	"""Detect blockers on the current page via URL patterns, DOM selectors, and text patterns.

	Strategy (ordered by cost):
	  1. Check current URL against known blocker patterns (free).
	  2. Run DOM selector detection via page.evaluate() (cheap).
	  3. Run text pattern detection against body text (cheap).
	  4. Return the highest-confidence match.

	Non-blocking reCAPTCHA (invisible v2 / v3) is detected but assigned
	a very low confidence (0.15) so it does not trigger human-in-the-loop.

	Args:
		page: Playwright Page object from browser-use.

	Returns:
		BlockerDetection with detected=True if a blocker was found,
		or detected=False if the page is clean.
	"""
	matches: list[BlockerDetection] = []

	# ── Pass 1: URL check ──────────────────────────────────────────────
	try:
		url_result = check_url_for_blockers(page.url)
		if url_result is not None:
			matches.append(url_result)
			# High-confidence URL match → return immediately
			if url_result.confidence >= 0.8:
				return url_result
	except Exception:
		# page.url can fail if page is closed
		pass

	# ── Pass 2: DOM selector check ─────────────────────────────────────
	try:
		selector_data = [
			{"selector": sel, "type": bt.value, "confidence": conf}
			for sel, bt, conf in _SELECTOR_PATTERNS
		]

		selector_results: list[dict] = await page.evaluate(
			_DOM_DETECT_JS,
			selector_data,
		)

		for result in selector_results:
			# Non-blocking reCAPTCHA: drastically reduce confidence
			if result.get("isNonBlocking", False):
				matches.append(BlockerDetection(
					detected=True,
					blocker_type=BlockerType(result["type"]),
					confidence=0.15,
					details=f"Matched selector: {result['selector']} (non-blocking reCAPTCHA, invisible/v3)",
					selector=result["selector"],
					source="dom_selector",
				))
				continue

			# Visible elements get full confidence; hidden ones get reduced
			visible = result.get("visible", False)
			confidence = result["confidence"] if visible else result["confidence"] * 0.5

			matches.append(BlockerDetection(
				detected=True,
				blocker_type=BlockerType(result["type"]),
				confidence=confidence,
				details=f"Matched selector: {result['selector']} (visible={visible})",
				selector=result["selector"],
				source="dom_selector",
			))
	except Exception:
		# page.evaluate timed out or failed — skip selector check
		pass

	# ── Pass 3: Text pattern check ─────────────────────────────────────
	try:
		body_text: str = await page.evaluate(_TEXT_EXTRACT_JS)

		for pattern, blocker_type, confidence in _TEXT_PATTERNS:
			if pattern.search(body_text):
				matches.append(BlockerDetection(
					detected=True,
					blocker_type=blocker_type,
					confidence=confidence,
					details=f"Matched text pattern: {pattern.pattern}",
					source="dom_text",
				))
	except Exception:
		# page.evaluate timed out or failed — skip text check
		pass

	# ── Return highest-confidence match ────────────────────────────────
	if not matches:
		return BlockerDetection(detected=False)

	matches.sort(key=lambda m: m.confidence, reverse=True)
	return matches[0]
