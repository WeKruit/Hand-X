"""Shared button-attempt diagnostics and no-transition recovery helpers."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from browser_use.browser import BrowserSession
from browser_use.dom.service import EnhancedDOMTreeNode

logger = logging.getLogger(__name__)

_VISUAL_RECOVERY_CACHE: dict[str, str] = {}

_BUTTON_STATE_JS = r"""() => {
	var ff = window.__ff || null;

	function queryAll(selector) {
		if (ff && ff.queryAll) return ff.queryAll(selector);
		return Array.from(document.querySelectorAll(selector));
	}

	function rootParent(node) {
		if (ff && ff.rootParent) return ff.rootParent(node);
		if (!node) return null;
		if (node.parentElement) return node.parentElement;
		var root = node.getRootNode ? node.getRootNode() : null;
		if (root && root.host) return root.host;
		return null;
	}

	function normalize(text) {
		return (text || '').replace(/\s+/g, ' ').trim();
	}

	function isVisible(el) {
		if (!el) return false;
		if (el.getAttribute && el.getAttribute('aria-hidden') === 'true') return false;
		if (ff && ff.isVisible && !ff.isVisible(el)) return false;
		var rect = el.getBoundingClientRect();
		if (rect.width === 0 || rect.height === 0) return false;
		var style = window.getComputedStyle(el);
		if (style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none') return false;
		return true;
	}

	function dedupeTexts(values, limit) {
		var seen = new Set();
		var result = [];
		for (var i = 0; i < values.length; i++) {
			var value = normalize(values[i]);
			if (!value || seen.has(value)) continue;
			seen.add(value);
			result.push(value.substring(0, 160));
			if (result.length >= limit) break;
		}
		return result;
	}

	function getAccessibleText(el) {
		var values = [];
		try {
			if (ff && ff.getAccessibleName) {
				values.push(ff.getAccessibleName(el));
			}
		} catch (e) {}
		values.push(el.innerText || '');
		values.push(el.textContent || '');
		values.push(el.value || '');
		values.push(el.getAttribute ? (el.getAttribute('aria-label') || '') : '');
		values.push(el.getAttribute ? (el.getAttribute('title') || '') : '');
		return normalize(values.find(function(value) { return normalize(value); }) || '');
	}

	function getCheckboxLabel(el) {
		var current = el;
		for (var depth = 0; current && depth < 5; depth++) {
			if (current.tagName === 'LABEL') return normalize(current.innerText || current.textContent || '');
			current = rootParent(current);
		}
		return getAccessibleText(el);
	}

	function collectButtonInfo() {
		return queryAll('button, [role="button"], input[type="submit"], input[type="button"], a[role="button"]')
			.filter(isVisible)
			.slice(0, 20)
			.map(function(el) {
				return {
					text: getAccessibleText(el),
					tag: (el.tagName || '').toLowerCase(),
					role: el.getAttribute ? (el.getAttribute('role') || '') : '',
					type: el.getAttribute ? (el.getAttribute('type') || '') : '',
					disabled: !!el.disabled || (el.getAttribute && el.getAttribute('aria-disabled') === 'true'),
				};
			});
	}

	function collectHiddenErrors() {
		var selectors = [
			'[role="alert"]',
			'[aria-invalid="true"]',
			'[class*="error"]',
			'[class*="Error"]',
			'[class*="invalid"]',
			'[class*="Invalid"]'
		];
		var texts = [];
		selectors.forEach(function(selector) {
			queryAll(selector).forEach(function(el) {
				var text = getAccessibleText(el);
				if (text) texts.push(text);
			});
		});
		return dedupeTexts(texts, 8);
	}

	function collectStatusTexts() {
		var selectors = [
			'[role="status"]',
			'[aria-live]',
			'#status',
			'#result',
			'#error'
		];
		var texts = [];
		selectors.forEach(function(selector) {
			queryAll(selector).forEach(function(el) {
				var text = getAccessibleText(el);
				if (text) texts.push(text);
			});
		});
		return dedupeTexts(texts, 8);
	}

	function collectForms() {
		return queryAll('form').slice(0, 5).map(function(form) {
			var invalidFields = [];
			Array.from(form.querySelectorAll('input, select, textarea')).forEach(function(field) {
				try {
					if (field.checkValidity && !field.checkValidity()) {
						invalidFields.push({
							name: normalize(field.name || field.id || field.type || field.tagName || 'field').substring(0, 80),
							message: normalize(field.validationMessage || '').substring(0, 120),
						});
					}
				} catch (e) {}
			});
			return {
				valid: invalidFields.length === 0,
				invalidCount: invalidFields.length,
				invalidFields: invalidFields.slice(0, 6),
			};
		});
	}

	function collectCheckboxes() {
		return queryAll('input[type="checkbox"], [role="checkbox"]')
			.filter(isVisible)
			.slice(0, 12)
			.map(function(el) {
				return {
					label: getCheckboxLabel(el).substring(0, 120),
					checked: !!el.checked || (el.getAttribute && el.getAttribute('aria-checked') === 'true'),
				};
			});
	}

	function hasText(patterns, texts) {
		return texts.some(function(text) {
			return patterns.some(function(pattern) { return pattern.test(text); });
		});
	}

	var headings = dedupeTexts(
		queryAll('h1, h2, h3, [role="heading"], [data-automation-id*="pageHeader"], [data-automation-id*="stepTitle"]')
			.filter(isVisible)
			.map(getAccessibleText),
		8
	);
	var buttonInfo = collectButtonInfo();
	var buttonTexts = buttonInfo.map(function(button) { return button.text; });
	var visibleTexts = dedupeTexts(
		headings
			.concat(buttonTexts)
			.concat(queryAll('label, p, span, div, a').filter(isVisible).slice(0, 120).map(getAccessibleText)),
		120
	);
	var hiddenErrors = collectHiddenErrors();
	var statusTexts = collectStatusTexts();
	var forms = collectForms();
	var checkboxes = collectCheckboxes();

	var passwordInputs = queryAll('input[type="password"]').filter(isVisible).length;
	var emailInputs = queryAll('input[type="email"], input[name*="email" i], input[id*="email" i]').filter(isVisible).length;
	var confirmPasswordVisible =
		passwordInputs >= 2 ||
		queryAll('[data-automation-id="verifyPassword"], input[name*="confirm" i], input[id*="confirm" i], input[aria-label*="confirm" i]')
			.filter(isVisible).length > 0;

	var auth = {
		passwordInputs: passwordInputs,
		emailInputs: emailInputs,
		confirmPasswordVisible: confirmPasswordVisible,
		createAccountSignals: hasText([/\bcreate account\b/i, /\bregister\b/i, /\bsign up\b/i], visibleTexts),
		signInSignals: hasText([/\bsign in\b/i, /\blog in\b/i, /\blogin\b/i], visibleTexts),
		startDialogSignals: hasText([/\bstart your application\b/i, /\bautofill with resume\b/i, /\bapply manually\b/i, /\buse my last application\b/i], visibleTexts),
		accountExistsSignals: hasText([/\balready exists\b/i, /\balready have an account\b/i, /\balready registered\b/i, /\baccount exists\b/i], visibleTexts.concat(hiddenErrors)),
		verificationSignals: hasText([/\bverify your account\b/i, /\bverification email\b/i, /\bconfirm your email\b/i, /\bcheck your inbox\b/i, /\bverify your email\b/i], visibleTexts.concat(hiddenErrors).concat(statusTexts)),
		authErrorSignals: hasText([/\binvalid password\b/i, /\bincorrect password\b/i, /\baccount not found\b/i, /\bincorrect\b/i, /\berror\b/i, /\bmust have a value\b/i], hiddenErrors.concat(statusTexts)),
	};

	return JSON.stringify({
		url: window.location.href,
		title: document.title,
		headings: headings,
		buttons: buttonInfo,
		hiddenErrors: hiddenErrors,
		statusTexts: statusTexts,
		forms: forms,
		checkboxes: checkboxes,
		auth: auth,
	});
}"""


def _normalize_label(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _hash_snapshot(snapshot: dict[str, Any]) -> str:
    payload = {
        "url": snapshot.get("url"),
        "title": snapshot.get("title"),
        "headings": snapshot.get("headings", []),
        "buttons": snapshot.get("buttons", []),
        "hiddenErrors": snapshot.get("hiddenErrors", []),
        "statusTexts": snapshot.get("statusTexts", []),
        "forms": snapshot.get("forms", []),
        "checkboxes": snapshot.get("checkboxes", []),
        "auth": snapshot.get("auth", {}),
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _classify_auth_state(snapshot: dict[str, Any]) -> str:
    auth = snapshot.get("auth", {}) if isinstance(snapshot, dict) else {}
    headings = [str(item).lower() for item in snapshot.get("headings", [])]
    button_texts = [str((item or {}).get("text") or "").lower() for item in snapshot.get("buttons", [])]
    combined = headings + button_texts + [str(item).lower() for item in snapshot.get("statusTexts", [])]

    def _has_text(*needles: str) -> bool:
        return any(needle in text for text in combined for needle in needles)

    if auth.get("verificationSignals"):
        return "verification_required"
    if auth.get("authErrorSignals"):
        return "explicit_auth_error"
    if auth.get("confirmPasswordVisible"):
        return "still_create_account"
    if auth.get("emailInputs") and auth.get("passwordInputs") and auth.get("signInSignals"):
        return "native_login"
    if auth.get("createAccountSignals"):
        return "still_create_account"
    if _has_text(
        "my information",
        "my experience",
        "application questions",
        "save and continue",
        "review",
        "submit application",
        "voluntary disclosures",
        "self identify",
    ):
        return "authenticated_or_application_resumed"
    if auth.get("passwordInputs") or auth.get("emailInputs"):
        return "unknown_pending"
    return "unknown_pending"


def build_button_descriptor_from_node(node: EnhancedDOMTreeNode) -> dict[str, Any]:
    attrs = node.attributes or {}
    text = _normalize_label(
        " ".join(
            part
            for part in [
                attrs.get("aria-label", ""),
                attrs.get("title", ""),
                attrs.get("value", ""),
                node.get_all_children_text(max_depth=2),
            ]
            if part
        )
    )
    return {
        "label": text or _normalize_label(attrs.get("name", "") or attrs.get("id", "")),
        "text": text,
        "tag": (node.tag_name or node.node_name or "").lower(),
        "role": str(attrs.get("role") or "").lower(),
        "type": str(attrs.get("type") or "").lower(),
        "automation_id": str(attrs.get("data-automation-id") or ""),
        "source": "standard_click",
    }


def build_button_descriptor(
    label: str,
    *,
    text: str = "",
    tag: str = "",
    role: str = "",
    type_: str = "",
    automation_id: str = "",
    source: str = "domhand_click_button",
) -> dict[str, Any]:
    normalized = _normalize_label(label or text)
    return {
        "label": normalized,
        "text": _normalize_label(text or label),
        "tag": (tag or "").lower(),
        "role": (role or "").lower(),
        "type": (type_ or "").lower(),
        "automation_id": automation_id or "",
        "source": source,
    }


def annotate_button_descriptor(descriptor: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(descriptor)
    label_text = _normalize_label(
        " ".join(
            part
            for part in [
                annotated.get("label", ""),
                annotated.get("text", ""),
                annotated.get("automation_id", ""),
            ]
            if part
        )
    ).lower()
    auth_state = snapshot.get("auth_state") or _classify_auth_state(snapshot)
    auth_related = auth_state in {
        "still_create_account",
        "native_login",
        "verification_required",
        "explicit_auth_error",
        "unknown_pending",
    }

    if auth_related and any(
        token in label_text
        for token in ("create account", "sign in", "log in", "login", "sign up", "register")
    ):
        kind = "auth_submit"
    elif snapshot.get("auth", {}).get("startDialogSignals") and any(
        token in label_text
        for token in ("autofill with resume", "apply with resume", "continue", "apply", "use my last application")
    ):
        kind = "dialog_continue"
    elif any(
        token in label_text
        for token in (
            "continue",
            "next",
            "save and continue",
            "save & continue",
            "apply",
            "autofill with resume",
            "submit",
            "create account",
            "sign in",
        )
    ) or annotated.get("type") == "submit":
        kind = "form_advance"
    else:
        kind = "generic"

    annotated["kind"] = kind
    annotated["critical"] = kind in {"auth_submit", "dialog_continue", "form_advance"}
    annotated["allow_secondary_fallback"] = kind in {"auth_submit", "dialog_continue", "form_advance"}
    return annotated


async def capture_button_state(
    browser_session: BrowserSession,
    *,
    page: Any | None = None,
) -> dict[str, Any]:
    if page is None:
        page = await browser_session.get_current_page()
    if page is None:
        return {
            "url": "",
            "title": "",
            "headings": [],
            "buttons": [],
            "hiddenErrors": [],
            "statusTexts": [],
            "forms": [],
            "checkboxes": [],
            "auth": {},
            "auth_state": "unknown_pending",
            "form_invalid_count": 0,
            "fingerprint": "missing-page",
        }

    with contextlib.suppress(Exception):
        from ghosthands.dom.shadow_helpers import ensure_helpers

        await ensure_helpers(page)

    snapshot: dict[str, Any]
    try:
        raw = await page.evaluate(_BUTTON_STATE_JS)
        snapshot = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception as exc:
        logger.debug("button_state_snapshot_failed", extra={"error": str(exc)})
        snapshot = {
            "url": "",
            "title": "",
            "headings": [],
            "buttons": [],
            "hiddenErrors": [],
            "statusTexts": [],
            "forms": [],
            "checkboxes": [],
            "auth": {},
        }

    snapshot["auth_state"] = _classify_auth_state(snapshot)
    snapshot["form_invalid_count"] = sum(
        int((form or {}).get("invalidCount") or 0)
        for form in snapshot.get("forms", [])
        if isinstance(form, dict)
    )
    snapshot["fingerprint"] = _hash_snapshot(snapshot)
    return snapshot


async def _capture_visual_recheck(
    browser_session: BrowserSession,
    incident_key: str,
) -> tuple[str | None, bool]:
    cached_path = _VISUAL_RECOVERY_CACHE.get(incident_key)
    if cached_path and Path(cached_path).exists():
        return cached_path, False

    file_name = re.sub(r"[^a-z0-9]+", "-", incident_key.lower()).strip("-")[:80] or "button-no-transition"
    path = Path(tempfile.gettempdir()) / f"{file_name}-{uuid4().hex[:8]}.png"
    try:
        await browser_session.take_screenshot(path=str(path), full_page=False)
    except Exception as exc:
        logger.debug("button_visual_recheck_failed", extra={"incident_key": incident_key, "error": str(exc)})
        return None, False

    _VISUAL_RECOVERY_CACHE[incident_key] = str(path)
    return str(path), True


async def assess_button_outcome(
    browser_session: BrowserSession,
    descriptor: dict[str, Any],
    before_snapshot: dict[str, Any],
    *,
    click_method: str,
    page: Any | None = None,
    capture_visual: bool = True,
) -> dict[str, Any]:
    after_snapshot = await capture_button_state(browser_session, page=page)
    url_before = str(before_snapshot.get("url") or "")
    url_after = str(after_snapshot.get("url") or "")
    auth_state_before = str(before_snapshot.get("auth_state") or "unknown_pending")
    auth_state_after = str(after_snapshot.get("auth_state") or "unknown_pending")
    form_invalid_before = int(before_snapshot.get("form_invalid_count") or 0)
    form_invalid_after = int(after_snapshot.get("form_invalid_count") or 0)

    url_changed = url_before != url_after
    fingerprint_changed = str(before_snapshot.get("fingerprint") or "") != str(after_snapshot.get("fingerprint") or "")
    auth_state_changed = auth_state_before != auth_state_after
    form_validation_changed = form_invalid_before != form_invalid_after
    meaningful_change = url_changed or fingerprint_changed or auth_state_changed or form_validation_changed
    no_transition = bool(descriptor.get("critical")) and not meaningful_change

    screenshot_path: str | None = None
    screenshot_captured = False
    if no_transition and capture_visual:
        incident_key = f"{descriptor.get('kind', 'generic')}|{descriptor.get('label', '')}|{before_snapshot.get('fingerprint', '')}"
        screenshot_path, screenshot_captured = await _capture_visual_recheck(browser_session, incident_key)

    return {
        "descriptor": descriptor,
        "click_method": click_method,
        "url_before": url_before,
        "url_after": url_after,
        "url_changed": url_changed,
        "state_changed": fingerprint_changed,
        "auth_state_before": auth_state_before,
        "auth_state_after": auth_state_after,
        "form_invalid_before": form_invalid_before,
        "form_invalid_after": form_invalid_after,
        "form_validation_changed": form_validation_changed,
        "meaningful_change": meaningful_change,
        "no_transition": no_transition,
        "before": before_snapshot,
        "after": after_snapshot,
        "screenshot_path": screenshot_path,
        "visual_recheck": {
            "performed": bool(screenshot_path),
            "captured_new": screenshot_captured,
            "screenshot_path": screenshot_path,
        },
    }


def format_button_no_transition_message(
    outcome: dict[str, Any],
    *,
    secondary_summary: str | None = None,
) -> str:
    descriptor = outcome.get("descriptor", {}) if isinstance(outcome, dict) else {}
    label = descriptor.get("label") or descriptor.get("text") or "button"
    before = outcome.get("before", {}) if isinstance(outcome, dict) else {}
    after = outcome.get("after", {}) if isinstance(outcome, dict) else {}
    parts = [
        f"No transition observed after clicking '{label}' via {outcome.get('click_method', 'click')}.",
        (
            f"Kind={descriptor.get('kind', 'generic')}, "
            f"auth_state={outcome.get('auth_state_before')} -> {outcome.get('auth_state_after')}, "
            f"url_changed={outcome.get('url_changed')}, state_changed={outcome.get('state_changed')}."
        ),
    ]
    hidden_errors = after.get("hiddenErrors") or []
    if hidden_errors:
        parts.append(f"Visible or hidden validation errors: {hidden_errors[:4]}.")
    status_texts = after.get("statusTexts") or []
    if status_texts:
        parts.append(f"Status text after click: {status_texts[:4]}.")
    screenshot_path = outcome.get("screenshot_path")
    if screenshot_path:
        parts.append(
            f"Captured a screenshot for visual re-check: {screenshot_path}. "
            "Use the screenshot as ground truth before retrying."
        )
    if secondary_summary:
        parts.append(secondary_summary.strip())
    if not hidden_errors and not status_texts:
        parts.append(
            "The button was observed and clicked, but the page still appears unchanged. "
            "Treat this as a failed advance/submit attempt, not a success."
        )
    return " ".join(part for part in parts if part)


def _serialize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the subset of observed page state that is useful for Desktop debug UI."""
    if not isinstance(snapshot, dict):
        return {}

    return {
        "url": snapshot.get("url"),
        "title": snapshot.get("title"),
        "headings": snapshot.get("headings", []),
        "buttons": snapshot.get("buttons", []),
        "hiddenErrors": snapshot.get("hiddenErrors", []),
        "statusTexts": snapshot.get("statusTexts", []),
        "forms": snapshot.get("forms", []),
        "checkboxes": snapshot.get("checkboxes", []),
        "auth_state": snapshot.get("auth_state"),
        "form_invalid_count": snapshot.get("form_invalid_count"),
    }


def build_button_no_transition_payload(
    outcome: dict[str, Any],
    *,
    secondary_summary: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable payload for unresolved button no-transition incidents."""
    descriptor = outcome.get("descriptor", {}) if isinstance(outcome, dict) else {}
    before = outcome.get("before", {}) if isinstance(outcome, dict) else {}
    after = outcome.get("after", {}) if isinstance(outcome, dict) else {}

    return {
        "label": descriptor.get("label") or descriptor.get("text") or "button",
        "kind": descriptor.get("kind") or "generic",
        "click_method": outcome.get("click_method"),
        "message": format_button_no_transition_message(outcome, secondary_summary=secondary_summary),
        "screenshot_path": outcome.get("screenshot_path"),
        "auth_state_before": outcome.get("auth_state_before"),
        "auth_state_after": outcome.get("auth_state_after"),
        "url_before": outcome.get("url_before"),
        "url_after": outcome.get("url_after"),
        "form_invalid_before": outcome.get("form_invalid_before"),
        "form_invalid_after": outcome.get("form_invalid_after"),
        "secondary_summary": secondary_summary,
        "before": _serialize_snapshot(before),
        "after": _serialize_snapshot(after),
    }


def emit_button_no_transition_event(
    outcome: dict[str, Any],
    *,
    secondary_summary: str | None = None,
) -> None:
    """Emit a structured JSONL event for unresolved no-transition button failures."""
    try:
        from ghosthands.output.jsonl import emit_event

        emit_event(
            "button_no_transition",
            **build_button_no_transition_payload(outcome, secondary_summary=secondary_summary),
        )
    except Exception as exc:
        logger.debug("button_no_transition_emit_failed", extra={"error": str(exc)})
