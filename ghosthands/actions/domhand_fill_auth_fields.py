"""DomHand auth field fill helper.

Provides a narrow auth-page helper for no-DomHand runs. It fills visible
email/password/confirm-password fields directly via a shadow-DOM-safe DOM
pass, without routing through the larger form extraction pipeline.
"""

import json
import os
from pydantic import BaseModel

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession

_FILL_AUTH_FIELDS_JS = r"""(payload) => {
	var ff = window.__ff || null;

	function queryAll(selector) {
		if (ff && ff.queryAll) return ff.queryAll(selector);
		return Array.from(document.querySelectorAll(selector));
	}

	function isVisible(el) {
		if (!el) return false;
		if (ff && ff.isVisible && !ff.isVisible(el)) return false;
		var rect = el.getBoundingClientRect();
		if (rect.width === 0 || rect.height === 0) return false;
		var style = window.getComputedStyle(el);
		if (style.display === 'none' || style.visibility === 'hidden') return false;
		return true;
	}

	function normalize(text) {
		return (text || '').replace(/\s+/g, ' ').trim().toLowerCase();
	}

	function labelText(el) {
		var parts = [];
		try {
			if (ff && ff.getAccessibleName) parts.push(ff.getAccessibleName(el));
		} catch (e) {}
		parts.push(el.getAttribute('aria-label') || '');
		parts.push(el.getAttribute('placeholder') || '');
		parts.push(el.getAttribute('name') || '');
		parts.push(el.getAttribute('id') || '');
		return normalize(parts.join(' '));
	}

	function classify(el, passwordIndex) {
		var type = normalize(el.getAttribute('type') || el.type || '');
		var label = labelText(el);
		if (type === 'password') {
			if (label.includes('confirm') || label.includes('verify') || label.includes('repeat') || passwordIndex > 0) {
				return 'confirm_password';
			}
			return 'password';
		}
		if (
			type === 'email' ||
			label.includes('email') ||
			label.includes('e-mail') ||
			label.includes('username') ||
			label.includes('user name') ||
			label.includes('login')
		) {
			return 'email';
		}
		return '';
	}

	function setValue(el, value) {
		if (value == null) return false;
		el.focus();
		var proto =
			el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
		var descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
		if (descriptor && descriptor.set) descriptor.set.call(el, value);
		else el.value = value;
		el.dispatchEvent(new Event('input', { bubbles: true }));
		el.dispatchEvent(new Event('change', { bubbles: true }));
		el.dispatchEvent(new Event('blur', { bubbles: true }));
		return normalize(el.value) === normalize(value) || String(el.value || '').length === String(value || '').length;
	}

	var email = String(payload.email || '');
	var password = String(payload.password || '');
	var inputs = queryAll('input, textarea');
	var seen = new Set();
	var filled = [];
	var detected = [];
	var passwordIndex = 0;

	for (var i = 0; i < inputs.length; i++) {
		var el = inputs[i];
		if (!isVisible(el) || seen.has(el)) continue;
		seen.add(el);
		var kind = classify(el, passwordIndex);
		if (!kind) continue;
		if (kind === 'password' || kind === 'confirm_password') passwordIndex += 1;
		detected.push({
			kind: kind,
			type: normalize(el.getAttribute('type') || el.type || ''),
			label: labelText(el).slice(0, 120),
		});
		var value = kind === 'email' ? email : password;
		if (!value) continue;
		var ok = setValue(el, value);
		filled.push({ kind: kind, ok: ok, valueLength: String(value).length });
	}

	return JSON.stringify({
		detected: detected,
		filled: filled,
		emailDetected: detected.some((item) => item.kind === 'email'),
		passwordDetected: detected.some((item) => item.kind === 'password'),
		confirmPasswordDetected: detected.some((item) => item.kind === 'confirm_password'),
		emailFilled: filled.some((item) => item.kind === 'email' && item.ok),
		passwordFilled: filled.some((item) => item.kind === 'password' && item.ok),
		confirmPasswordFilled: filled.some((item) => item.kind === 'confirm_password' && item.ok),
	});
}"""


class DomHandFillAuthFieldsParams(BaseModel):
    """Parameters for domhand_fill_auth_fields."""

    # No parameters needed — uses GH_EMAIL / GH_PASSWORD on the current auth page.


async def domhand_fill_auth_fields(
    params: DomHandFillAuthFieldsParams,
    browser_session: BrowserSession,
) -> ActionResult:
    """Fill visible auth-like fields using explicit credential overrides."""

    del params  # Param model is intentionally empty.
    from ghosthands.dom.shadow_helpers import ensure_helpers

    page = await browser_session.get_current_page()
    if not page:
        return ActionResult(error="No active page found")

    email = (os.environ.get("GH_EMAIL") or "").strip()
    password = (os.environ.get("GH_PASSWORD") or "").strip()
    if not email and not password:
        return ActionResult(error="DomHand auth fill: GH_EMAIL / GH_PASSWORD are both empty.")

    await ensure_helpers(page)
    payload = json.loads(await page.evaluate(_FILL_AUTH_FIELDS_JS, {"email": email, "password": password}))

    if not payload.get("detected"):
        return ActionResult(
            error="DomHand auth fill: no visible auth fields were detected on the current page.",
            include_in_memory=True,
        )

    if not payload.get("emailFilled") and payload.get("emailDetected") and email:
        return ActionResult(
            error="DomHand auth fill: detected email field but failed to commit the email value.",
            include_in_memory=True,
            metadata=payload,
        )

    if not payload.get("passwordFilled") and payload.get("passwordDetected") and password:
        return ActionResult(
            error="DomHand auth fill: detected password field but failed to commit the password value.",
            include_in_memory=True,
            metadata=payload,
        )

    if payload.get("confirmPasswordDetected") and password and not payload.get("confirmPasswordFilled"):
        return ActionResult(
            error="DomHand auth fill: detected confirm-password field but failed to commit the password value.",
            include_in_memory=True,
            metadata=payload,
        )

    summary_bits = []
    if payload.get("emailFilled"):
        summary_bits.append("email")
    if payload.get("passwordFilled"):
        summary_bits.append("password")
    if payload.get("confirmPasswordFilled"):
        summary_bits.append("confirm password")
    summary = ", ".join(summary_bits) if summary_bits else "visible auth fields"

    return ActionResult(
        extracted_content=f"DomHand auth fill: committed {summary}.",
        include_in_memory=True,
        metadata=payload,
    )
