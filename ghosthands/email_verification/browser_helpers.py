"""Deterministic browser helpers for applying email-verification artifacts."""

from __future__ import annotations

import inspect
from typing import Any

from ghosthands.email_verification.models import (
    CodeEntryMode,
    CodeEntryResult,
    CodeEntryStatus,
    EmailVerificationPageState,
    MagicLinkOpenResult,
    MagicLinkOpenStatus,
)
from ghosthands.email_verification.page_state import _coerce_mapping

_FILL_VERIFICATION_CODE_JS = r"""(payload) => {
  const code = String((payload && payload.code) || '').replace(/\s+/g, '');
  if (!code) {
    return { status: 'missing_code', mode: 'none', reason: 'Verification code was empty.' };
  }

  const providedSelectors = Array.isArray(payload && payload.selectors) ? payload.selectors : [];
  const clickAction = !payload || payload.click_action !== false;
  const visible = (el) => {
    if (!el || el.disabled || el.readOnly) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const textOf = (node) => String((node && (node.innerText || node.textContent)) || '').trim();
  const labelText = (el) => {
    const pieces = [
      el.getAttribute('aria-label') || '',
      el.getAttribute('placeholder') || '',
      el.getAttribute('autocomplete') || '',
      el.getAttribute('name') || '',
      el.getAttribute('id') || '',
    ];
    if (el.labels) {
      for (const label of Array.from(el.labels)) pieces.push(textOf(label));
    }
    const parentLabel = el.closest && el.closest('label');
    if (parentLabel) pieces.push(textOf(parentLabel));
    const prev = el.previousElementSibling;
    if (prev) pieces.push(textOf(prev));
    return pieces.filter(Boolean).join(' ');
  };
  const dispatchValue = (el, value) => {
    el.focus();
    const proto = el.tagName.toLowerCase() === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value') && Object.getOwnPropertyDescriptor(proto, 'value').set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.blur();
  };
  const bySelector = providedSelectors
    .map((selector) => {
      try { return document.querySelector(selector); } catch (_) { return null; }
    })
    .filter(visible);
  const inferred = Array.from(document.querySelectorAll('input, textarea'))
    .filter(visible)
    .filter((el) => {
      const tag = el.tagName.toLowerCase();
      const type = String(el.type || '').toLowerCase();
      if (tag === 'input' && ['hidden', 'checkbox', 'radio', 'file', 'submit', 'button', 'email'].includes(type)) return false;
      const meta = labelText(el).toLowerCase();
      const maxLength = Number(el.maxLength || 0);
      const pageText = String(document.body && document.body.innerText || '').toLowerCase();
      return /(code|otp|one.?time|passcode|security|verification|confirm)/i.test(meta)
        || String(el.autocomplete || '').toLowerCase() === 'one-time-code'
        || /(numeric|decimal|tel)/i.test(`${el.inputMode || ''} ${type}`)
        || (maxLength > 0 && maxLength <= 12 && /\b(code|otp|passcode|one-time|verification code|security code)\b/i.test(pageText));
    });
  const inputs = (bySelector.length ? bySelector : inferred)
    .filter((el, index, arr) => arr.indexOf(el) === index);
  if (!inputs.length) {
    return {
      status: 'no_code_input',
      mode: 'none',
      filled_input_count: 0,
      clicked_action: false,
      page_url: window.location.href,
      reason: 'No visible verification-code input was found.',
    };
  }

  const segmented = inputs.length > 1 && inputs.length <= code.length && inputs.every((el) => {
    const maxLength = Number(el.maxLength || 0);
    return maxLength <= 1 || maxLength === 524288 || el.getBoundingClientRect().width < 80;
  });
  let filledCount = 0;
  if (segmented) {
    for (let i = 0; i < inputs.length && i < code.length; i += 1) {
      dispatchValue(inputs[i], code[i]);
      filledCount += 1;
    }
  } else {
    dispatchValue(inputs[0], code);
    filledCount = 1;
  }

  let clickedAction = false;
  let clickedActionLabel = '';
  if (clickAction) {
    const controls = Array.from(document.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"]'))
      .filter(visible)
      .map((el) => ({
        el,
        label: String(el.value || el.getAttribute('aria-label') || el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim(),
      }))
      .filter((item) => item.label);
    const pageText = String(document.body && document.body.innerText || '').toLowerCase();
    const safeVerificationPage = /\b(email|e-mail|inbox|verification|security|one-time|otp|code|passcode)\b/.test(pageText);
    const safeAction = controls.find((item) => {
      const label = item.label.toLowerCase();
      if (/\b(submit application|send application|apply|finish application|final submit)\b/.test(label)) return false;
      if (/\b(verify|confirm|submit code|check code|continue|next|sign in|log in)\b/.test(label)) return true;
      return safeVerificationPage && label === 'submit';
    });
    if (safeAction) {
      safeAction.el.click();
      clickedAction = true;
      clickedActionLabel = safeAction.label;
    }
  }

  return {
    status: 'entered',
    mode: segmented ? 'segmented_inputs' : 'single_input',
    filled_input_count: filledCount,
    clicked_action: clickedAction,
    clicked_action_label: clickedActionLabel,
    page_url: window.location.href,
    reason: clickedAction ? 'Verification code entered and a safe action button was clicked.' : 'Verification code entered.',
  };
}"""


def _failure(status: CodeEntryStatus, reason: str) -> CodeEntryResult:
    return CodeEntryResult(status=status, mode=CodeEntryMode.NONE, reason=reason)


async def fill_verification_code(
    page: Any,
    code: str,
    *,
    state: EmailVerificationPageState | None = None,
    click_action: bool = True,
) -> CodeEntryResult:
    """Enter a verification code into visible code inputs without involving the LLM agent."""

    clean_code = str(code or "").strip()
    if not clean_code:
        return _failure(CodeEntryStatus.MISSING_CODE, "Verification code was empty.")
    selectors = list(state.code_input_selectors) if state else []
    try:
        raw = await page.evaluate(
            _FILL_VERIFICATION_CODE_JS,
            {
                "code": clean_code,
                "selectors": selectors,
                "click_action": click_action,
            },
        )
        data = _coerce_mapping(raw)
        return CodeEntryResult(**data)
    except Exception as exc:
        return _failure(CodeEntryStatus.FAILED, f"Failed to enter verification code: {type(exc).__name__}: {exc}")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _current_url(browser_session: Any) -> str:
    getter = getattr(browser_session, "get_current_page_url", None)
    if getter is None:
        return ""
    try:
        return str(await _maybe_await(getter()) or "")
    except Exception:
        return ""


async def _switch_to_target(browser_session: Any, target_id: str) -> bool:
    event_bus = getattr(browser_session, "event_bus", None)
    if event_bus is not None and hasattr(event_bus, "dispatch"):
        from browser_use.browser.events import SwitchTabEvent

        event = event_bus.dispatch(SwitchTabEvent(target_id=target_id))
        await _maybe_await(event)
        if hasattr(event, "event_result"):
            await event.event_result(raise_if_any=True, raise_if_none=False)
        return True

    focus = getattr(browser_session, "get_or_create_cdp_session", None)
    if focus is not None:
        await _maybe_await(focus(target_id=target_id, focus=True))
        return True

    return False


async def open_magic_link_in_new_tab(
    browser_session: Any,
    magic_link: str,
    *,
    return_to_original: bool = True,
) -> MagicLinkOpenResult:
    """Open a verification magic link in a new tab and restore focus to the application tab."""

    link = str(magic_link or "").strip()
    if not link:
        return MagicLinkOpenResult(status=MagicLinkOpenStatus.MISSING_LINK, reason="Magic link was empty.")
    navigate_to = getattr(browser_session, "navigate_to", None)
    if browser_session is None or navigate_to is None:
        return MagicLinkOpenResult(
            status=MagicLinkOpenStatus.MISSING_BROWSER_SESSION,
            reason="Browser session cannot navigate to a magic link.",
        )

    original_target_id = str(getattr(browser_session, "agent_focus_target_id", "") or "")
    original_url = await _current_url(browser_session)
    try:
        await _maybe_await(navigate_to(link, new_tab=True))
        magic_link_target_id = str(getattr(browser_session, "agent_focus_target_id", "") or "")
        magic_link_url = await _current_url(browser_session)
        returned = False
        if return_to_original and original_target_id and magic_link_target_id != original_target_id:
            returned = await _switch_to_target(browser_session, original_target_id)
        return MagicLinkOpenResult(
            status=MagicLinkOpenStatus.OPENED,
            original_target_id=original_target_id,
            magic_link_target_id=magic_link_target_id,
            original_url=original_url,
            magic_link_url=magic_link_url or link,
            returned_to_original=returned,
            reason="Magic link opened in a new tab." if not returned else "Magic link opened and focus returned.",
        )
    except Exception as exc:
        return MagicLinkOpenResult(
            status=MagicLinkOpenStatus.FAILED,
            original_target_id=original_target_id,
            original_url=original_url,
            reason=f"Failed to open magic link: {type(exc).__name__}: {exc}",
        )
