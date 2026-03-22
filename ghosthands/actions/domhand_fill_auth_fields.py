"""DomHand auth field fill helper.

Provides a narrow auth-page helper for no-DomHand runs. It reuses the
existing DomHand credential override logic to fill only visible auth-like
fields (email, password, confirm password) on the current page.
"""

from __future__ import annotations

from pydantic import BaseModel

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession


class DomHandFillAuthFieldsParams(BaseModel):
    """Parameters for domhand_fill_auth_fields."""

    # No parameters needed — uses GH_EMAIL / GH_PASSWORD on the current auth page.


async def domhand_fill_auth_fields(
    params: DomHandFillAuthFieldsParams,
    browser_session: BrowserSession,
) -> ActionResult:
    """Fill visible auth-like fields using explicit credential overrides."""

    del params  # Param model is intentionally empty.

    from ghosthands.actions.domhand_fill import (
        _is_auth_like_field,
        _preferred_field_label,
        DomHandFillParams,
        domhand_fill,
        extract_visible_form_fields,
    )

    page = await browser_session.get_current_page()
    if not page:
        return ActionResult(error="No active page found")

    fields = await extract_visible_form_fields(page)
    focus_fields: list[str] = []
    seen_labels: set[str] = set()
    for field in fields:
        if not _is_auth_like_field(field):
            continue
        label = str(_preferred_field_label(field) or field.name or "").strip()
        if not label or label in seen_labels:
            continue
        focus_fields.append(label)
        seen_labels.add(label)

    if not focus_fields:
        return ActionResult(
            extracted_content="DomHand auth fill: no visible auth-like fields found on the current page.",
            include_in_memory=True,
        )

    return await domhand_fill(
        DomHandFillParams(
            focus_fields=focus_fields,
            use_auth_credentials=True,
        ),
        browser_session,
    )
