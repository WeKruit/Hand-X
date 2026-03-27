"""LLM escalation layer for DomHand — lightweight verification and fill guidance.

Graduated cost model inspired by GHOST-HANDS v3 tiers:
  - Layer 1: DOM-first fill ($0) — handled by ``fill_executor``
  - Layer 2: LLM verification on ambiguous readback (~$0.001/call)
  - Layer 2b: LLM-guided fill on executor failure (~$0.002/call)
  - Layer 3: browser-use vision fallback (~$0.005+/call) — handled by the agent

Only triggered on failure paths — the happy path (DOM fill + verify) costs $0.

The escalation model is configurable via ``GH_DOMHAND_MODEL`` (defaults to
``gemini-3-flash-preview``).  It uses ``get_chat_model()`` from the LLM client
so any supported provider (Google, Anthropic, OpenAI) works transparently.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import structlog

from ghosthands.actions.views import FormField

logger = structlog.get_logger(__name__)


def _get_escalation_model() -> Any:
    """Build a LangChain chat model for escalation calls."""
    from ghosthands.config.settings import settings
    from ghosthands.llm.client import get_chat_model

    return get_chat_model(settings.domhand_model, disable_google_thinking=True)


async def _capture_field_screenshot(page: Any, field: FormField) -> str | None:
    """Take a base64-encoded PNG screenshot focused on the field area.

    Falls back to a full-page screenshot if the element can't be located.
    Returns ``None`` if screenshot capture fails entirely.
    """
    try:
        el = await page.query_selector(f'[data-field-id="{field.field_id}"]')
        if el is None:
            el = await page.query_selector(f"#{field.field_id}")
        if el:
            raw = await el.screenshot(type="png")
        else:
            raw = await page.screenshot(type="png", full_page=False)
        return base64.b64encode(raw).decode("ascii")
    except Exception as exc:
        logger.debug("llm_escalation.screenshot_failed", error=str(exc))
        return None


def _build_image_message(screenshot_b64: str, prompt: str) -> Any:
    """Build a multimodal HumanMessage with an image + text prompt."""
    from langchain_core.messages import HumanMessage

    return HumanMessage(
        content=[
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
            },
            {"type": "text", "text": prompt},
        ]
    )


async def llm_verify_field_value(
    page: Any,
    field: FormField,
    desired_value: str,
) -> bool | None:
    """Ask the escalation model whether the field visibly shows the desired value.

    Returns ``True`` if the LLM confirms the value is present,
    ``False`` if the LLM says it does not match,
    or ``None`` if the check could not be performed (no screenshot, API error, etc.).
    """
    screenshot_b64 = await _capture_field_screenshot(page, field)
    if not screenshot_b64:
        return None

    label = field.name or field.field_type
    prompt = (
        f'Look at this form field labeled "{label}". '
        f'Does it currently show the value "{desired_value}"? '
        "Answer ONLY with a JSON object: {\"matches\": true} or {\"matches\": false}."
    )

    try:
        model = _get_escalation_model()
        message = _build_image_message(screenshot_b64, prompt)
        response = await model.ainvoke([message])
        text = (response.content if isinstance(response.content, str) else str(response.content)).strip()
        logger.debug(
            "llm_escalation.verify_response",
            field_label=label,
            desired=desired_value[:80],
            raw_response=text[:120],
        )
        parsed = json.loads(text)
        return bool(parsed.get("matches"))
    except Exception as exc:
        logger.warning("llm_escalation.verify_failed", error=str(exc), field_label=label)
        return None


async def llm_suggest_fill_action(
    page: Any,
    field: FormField,
    desired_value: str,
) -> dict[str, Any] | None:
    """Ask the escalation model how to fill a field that DOM-first methods couldn't handle.

    Returns a dict with suggested actions, e.g.::

        {"strategy": "click_then_type", "selector": "...", "steps": [...]}

    or ``None`` if guidance could not be obtained.
    """
    screenshot_b64 = await _capture_field_screenshot(page, field)
    if not screenshot_b64:
        return None

    label = field.name or field.field_type
    options_hint = ""
    if field.options:
        opts_preview = ", ".join(o[:40] for o in field.options[:8])
        options_hint = f"\nAvailable options: [{opts_preview}]"

    prompt = (
        f'This form field labeled "{label}" (type: {field.field_type}) '
        f'needs to be filled with the value "{desired_value}".{options_hint}\n\n'
        "DOM-first fill methods have failed. Based on the screenshot, suggest "
        "how to interact with this field. Respond ONLY with a JSON object:\n"
        '{"strategy": "<one of: click_then_type, open_dropdown_click, '
        'clear_and_retype, tab_select, use_keyboard>", '
        '"selector": "<CSS selector to target>", '
        '"steps": ["<step1>", "<step2>", ...]}'
    )

    try:
        model = _get_escalation_model()
        message = _build_image_message(screenshot_b64, prompt)
        response = await model.ainvoke([message])
        text = (response.content if isinstance(response.content, str) else str(response.content)).strip()
        logger.debug(
            "llm_escalation.fill_guide_response",
            field_label=label,
            desired=desired_value[:80],
            raw_response=text[:200],
        )
        return json.loads(text)
    except Exception as exc:
        logger.warning("llm_escalation.fill_guide_failed", error=str(exc), field_label=label)
        return None


async def llm_execute_fill_suggestion(
    page: Any,
    field: FormField,
    desired_value: str,
    suggestion: dict[str, Any],
) -> bool:
    """Execute a fill strategy suggested by :func:`llm_suggest_fill_action`.

    Supports a small set of deterministic strategies that the LLM can recommend.
    Returns ``True`` if the action was executed without error (caller should still verify).
    """
    strategy = suggestion.get("strategy", "")
    selector = suggestion.get("selector", f"#{field.field_id}")

    try:
        match strategy:
            case "click_then_type":
                el = await page.query_selector(selector)
                if el:
                    await el.click()
                    await page.keyboard.type(desired_value, delay=30)
                    return True
            case "clear_and_retype":
                el = await page.query_selector(selector)
                if el:
                    await el.click(click_count=3)
                    await page.keyboard.type(desired_value, delay=30)
                    return True
            case "open_dropdown_click":
                el = await page.query_selector(selector)
                if el:
                    await el.click()
                    import asyncio
                    await asyncio.sleep(0.3)
                    opt = await page.query_selector(
                        f'[role="option"]:has-text("{desired_value}"), '
                        f'li:has-text("{desired_value}"), '
                        f'div[role="listbox"] div:has-text("{desired_value}")'
                    )
                    if opt:
                        await opt.click()
                        return True
            case "tab_select":
                el = await page.query_selector(selector)
                if el:
                    await el.focus()
                    for _ in range(20):
                        await page.keyboard.press("ArrowDown")
                        import asyncio
                        await asyncio.sleep(0.05)
                        current = await el.evaluate("el => el.value || el.textContent")
                        if desired_value.lower() in (current or "").lower():
                            await page.keyboard.press("Enter")
                            return True
            case "use_keyboard":
                steps = suggestion.get("steps", [])
                for step in steps:
                    if step.startswith("press:"):
                        await page.keyboard.press(step[6:].strip())
                    elif step.startswith("type:"):
                        await page.keyboard.type(step[5:].strip(), delay=30)
                    elif step.startswith("click:"):
                        el = await page.query_selector(step[6:].strip())
                        if el:
                            await el.click()
                return bool(steps)
            case _:
                logger.debug("llm_escalation.unknown_strategy", strategy=strategy)
                return False
    except Exception as exc:
        logger.warning(
            "llm_escalation.execute_failed",
            strategy=strategy,
            error=str(exc),
            field_label=field.name,
        )
        return False
    return False
