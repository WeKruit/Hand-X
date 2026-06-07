"""Browser page-state extraction and classification for email verification walls."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from ghosthands.email_verification.models import EmailVerificationPageKind, EmailVerificationPageState

_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_VERIFY_TEXT_RE = re.compile(r"\b(verify|verification|confirm|confirmation|security|one[-\s]?time|otp|code|passcode)\b")
_EMAIL_TEXT_RE = re.compile(
    r"\b(email|e-mail|inbox|mailbox|check your mail)\b|\b(sent|we sent).{0,40}\b(email|e-mail|inbox|mailbox)\b"
)
_MAGIC_LINK_TEXT_RE = re.compile(r"\b(click|open|follow).{0,40}\b(link|button)\b|\bverification link\b|\bmagic link\b")
_SMS_TEXT_RE = re.compile(r"\b(sms|text message|mobile|phone number|sent to your phone|cell phone)\b")
_AUTHENTICATOR_TEXT_RE = re.compile(r"\b(authenticator|authentication app|2fa app|mfa app|totp)\b")
_CAPTCHA_TEXT_RE = re.compile(r"\b(captcha|recaptcha|robot|human check|verify you are human)\b")

_EXTRACT_VERIFICATION_PAGE_STATE_JS = r"""() => {
  const textOf = (node) => String((node && (node.innerText || node.textContent)) || '').trim();
  const visible = (el) => {
    if (!el || el.disabled) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const attr = (el, name) => String(el.getAttribute(name) || '');
  const escapeAttr = (value) => String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const cssEscape = (value) => {
    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(String(value));
    return String(value).replace(/([^a-zA-Z0-9_-])/g, '\\$1');
  };
  const selectorFor = (el, index) => {
    if (el.id) return `#${cssEscape(el.id)}`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${escapeAttr(el.name)}"]`;
    const key = String(index);
    el.setAttribute('data-gh-email-verification-input', key);
    return `[data-gh-email-verification-input="${key}"]`;
  };
  const labelText = (el) => {
    const pieces = [
      attr(el, 'aria-label'),
      attr(el, 'placeholder'),
      attr(el, 'autocomplete'),
      attr(el, 'name'),
      attr(el, 'id'),
    ];
    if (el.labels) {
      for (const label of Array.from(el.labels)) pieces.push(textOf(label));
    }
    const parentLabel = el.closest && el.closest('label');
    if (parentLabel) pieces.push(textOf(parentLabel));
    const describedBy = attr(el, 'aria-describedby');
    if (describedBy) {
      for (const id of describedBy.split(/\s+/).filter(Boolean)) {
        const desc = document.getElementById(id);
        if (desc) pieces.push(textOf(desc));
      }
    }
    const prev = el.previousElementSibling;
    if (prev) pieces.push(textOf(prev));
    return pieces.filter(Boolean).join(' ');
  };
  const bodyText = textOf(document.body).replace(/\s+/g, ' ').trim();
  const headingText = Array.from(document.querySelectorAll('h1,h2,h3,[role="heading"]'))
    .filter(visible)
    .map(textOf)
    .filter(Boolean)
    .slice(0, 8)
    .join(' | ');
  const emails = Array.from(new Set((bodyText.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/ig) || []).map((item) => item.toLowerCase())));
  const lowerBody = bodyText.toLowerCase();
  const inputCandidates = Array.from(document.querySelectorAll('input, textarea'))
    .filter(visible)
    .filter((el) => {
      const tag = el.tagName.toLowerCase();
      const type = String(el.type || '').toLowerCase();
      if (tag === 'input' && ['hidden', 'checkbox', 'radio', 'file', 'submit', 'button', 'email'].includes(type)) return false;
      const meta = labelText(el).toLowerCase();
      const maxLength = Number(el.maxLength || 0);
      const hasCodeLabel = /(code|otp|one.?time|passcode|security|verification|confirm)/i.test(meta);
      const hasOneTimeAutocomplete = String(el.autocomplete || '').toLowerCase() === 'one-time-code';
      const hasNumericHint = /(numeric|decimal|tel)/i.test(`${el.inputMode || ''} ${type}`);
      const pageSuggestsCode = /\b(code|otp|passcode|one-time|verification code|security code)\b/i.test(lowerBody);
      const shortCodeInput = maxLength > 0 && maxLength <= 12 && pageSuggestsCode;
      return hasCodeLabel || hasOneTimeAutocomplete || hasNumericHint || shortCodeInput;
    });
  const codeInputs = inputCandidates.map((el, index) => ({
    selector: selectorFor(el, index),
    label: labelText(el),
    maxLength: Number(el.maxLength || 0),
    valueLength: String(el.value || '').length,
  }));
  const controls = Array.from(document.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"]'))
    .filter(visible)
    .map((el) => String(el.value || el.getAttribute('aria-label') || el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .slice(0, 30);
  const labelsText = controls.join(' | ').toLowerCase();
  return {
    current_url: window.location.href,
    site_hostname: window.location.hostname,
    application_email: emails[0] || '',
    visible_text: bodyText.slice(0, 6000),
    heading_text: headingText.slice(0, 1000),
    detected_email_addresses: emails.slice(0, 10),
    code_input_count: codeInputs.length,
    code_input_selectors: codeInputs.map((item) => item.selector),
    action_button_labels: controls,
    supports_code_entry: codeInputs.length > 0,
    supports_magic_link: /(click|open|follow).{0,40}(link|button)|verification link|magic link/i.test(bodyText),
    resend_available: /\b(resend|send again|new code|another code)\b/.test(labelsText + ' ' + lowerBody),
    verify_button_available: /\b(verify|confirm|submit code|check code)\b/.test(labelsText),
    continue_button_available: /\b(continue|next)\b/.test(labelsText),
    email_signals: /\b(email|e-mail|inbox|mailbox|check your mail)\b|(sent|we sent).{0,40}\b(email|e-mail|inbox|mailbox)\b/.test(lowerBody) || emails.length > 0,
    magic_link_signals: /(click|open|follow).{0,40}(link|button)|verification link|magic link/i.test(bodyText),
    sms_signals: /\b(sms|text message|mobile|phone number|sent to your phone|cell phone)\b/.test(lowerBody),
    authenticator_signals: /\b(authenticator|authentication app|2fa app|mfa app|totp)\b/.test(lowerBody),
    captcha_signals: /\b(captcha|recaptcha|robot|human check|verify you are human)\b/.test(lowerBody),
  };
}"""


def _coerce_mapping(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        parsed = json.loads(text)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise TypeError(f"Expected page.evaluate() to return a mapping or JSON object string, got {type(raw).__name__}")


def _normalized_page_text(state: EmailVerificationPageState) -> str:
    return f"{state.heading_text} {state.visible_text} {' '.join(state.action_button_labels)}".lower()


def classify_email_verification_page_state(state: EmailVerificationPageState) -> EmailVerificationPageKind:
    """Classify a page using only visible, deterministic browser facts."""

    text = _normalized_page_text(state)
    has_verification_language = bool(_VERIFY_TEXT_RE.search(text))
    has_email_signal = state.email_signals or bool(state.application_email) or bool(state.detected_email_addresses)
    has_email_signal = has_email_signal or bool(_EMAIL_TEXT_RE.search(text))
    has_code_input = state.supports_code_entry or state.code_input_count > 0
    has_magic_link_signal = state.supports_magic_link or state.magic_link_signals or bool(_MAGIC_LINK_TEXT_RE.search(text))
    has_sms_signal = state.sms_signals or bool(_SMS_TEXT_RE.search(text))
    has_authenticator_signal = state.authenticator_signals or bool(_AUTHENTICATOR_TEXT_RE.search(text))
    has_captcha_signal = state.captcha_signals or bool(_CAPTCHA_TEXT_RE.search(text))

    if has_captcha_signal:
        return EmailVerificationPageKind.CAPTCHA

    if has_email_signal and has_code_input and not has_sms_signal and not has_authenticator_signal:
        return EmailVerificationPageKind.EMAIL_CODE

    if has_email_signal and has_magic_link_signal and not has_code_input:
        return EmailVerificationPageKind.EMAIL_MAGIC_LINK

    if has_sms_signal and has_code_input and not has_email_signal:
        return EmailVerificationPageKind.SMS_CODE

    if has_authenticator_signal and has_code_input and not has_email_signal:
        return EmailVerificationPageKind.AUTHENTICATOR_CODE

    if has_email_signal and (has_code_input or has_magic_link_signal):
        return EmailVerificationPageKind.AMBIGUOUS

    if has_sms_signal and (has_code_input or has_verification_language):
        return EmailVerificationPageKind.SMS_CODE

    if has_authenticator_signal and (has_code_input or has_verification_language):
        return EmailVerificationPageKind.AUTHENTICATOR_CODE

    if has_verification_language or has_code_input or has_magic_link_signal:
        return EmailVerificationPageKind.AMBIGUOUS

    return EmailVerificationPageKind.NOT_VERIFICATION


def is_auto_resolvable_email_page(state: EmailVerificationPageState) -> bool:
    """Return whether the page can use the Gmail-backed recovery path."""

    return state.page_kind in {
        EmailVerificationPageKind.EMAIL_CODE,
        EmailVerificationPageKind.EMAIL_MAGIC_LINK,
    }


async def extract_email_verification_page_state(
    page: Any,
    *,
    platform: str = "generic",
    company_hint: str | None = None,
) -> EmailVerificationPageState:
    """Extract current-page verification facts through one deterministic browser script."""

    raw = await page.evaluate(_EXTRACT_VERIFICATION_PAGE_STATE_JS)
    data = _coerce_mapping(raw)
    data["platform"] = platform
    data["company_hint"] = company_hint
    state = EmailVerificationPageState(**data)
    return state.model_copy(update={"page_kind": classify_email_verification_page_state(state)})
