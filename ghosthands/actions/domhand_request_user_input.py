"""Explicit HITL pause action for answerable missing form data."""

from __future__ import annotations

import json
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession

from ghosthands.actions.domhand_fill import DEFAULT_HITL_TIMEOUT_SECONDS
from ghosthands.actions.views import DomHandRequestUserInputParams
from ghosthands.bridge.protocol import get_field_answer, is_hitl_available
from ghosthands.output.jsonl import emit_event


def _safe_page_url(page: Any) -> str:
    if not page:
        return ""
    try:
        url_attr = getattr(page, "url", None)
        if callable(url_attr):
            return str(url_attr() or "")
        if isinstance(url_attr, str):
            return url_attr
    except Exception:
        return ""
    return ""


async def domhand_request_user_input(
    params: DomHandRequestUserInputParams,
    browser_session: BrowserSession,
) -> ActionResult:
    """Emit a Desktop HITL prompt for one required answer and wait for resume."""

    field_label = params.field_label.strip()
    if not field_label:
        return ActionResult(error="field_label is required")

    field_id = params.field_id or field_label
    page = await browser_session.get_current_page()
    page_url = _safe_page_url(page)

    emit_event(
        "field_needs_input",
        question_key=field_id,
        field_label=field_label,
        field_id=params.field_id,
        field_type=params.field_type or "text",
        question_text=params.question_text or field_label,
        section=params.section or "",
        options=[{"value": option, "text": option} for option in params.options],
        source="domhand_request_user_input",
        page_url=page_url,
    )

    if not is_hitl_available():
        return ActionResult(
            extracted_content=json.dumps(
                {
                    "field_label": field_label,
                    "field_id": params.field_id,
                    "answered": False,
                    "answer": "",
                    "reason": "hitl_unavailable",
                }
            )
        )

    answer = await get_field_answer(
        field_id,
        timeout=float(params.timeout_seconds or DEFAULT_HITL_TIMEOUT_SECONDS),
        field_label=field_label,
    )
    return ActionResult(
        extracted_content=json.dumps(
            {
                "field_label": field_label,
                "field_id": params.field_id,
                "answered": bool(answer),
                "answer": answer or "",
                "page_url": page_url,
            }
        )
    )
