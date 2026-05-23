"""Track whether the current page still needs a fresh assess-state checkpoint."""

from __future__ import annotations

from typing import Any

from browser_use.browser import BrowserSession


def mark_assessment_pending(
    browser_session: BrowserSession,
    *,
    page_url: str,
    page_context_key: str,
    source_action: str,
) -> None:
    """Mark the current page as needing a fresh ``domhand_assess_state`` pass."""

    object.__setattr__(
        browser_session,
        "_gh_pending_assessment",
        {
            "page_url": str(page_url or ""),
            "page_context_key": str(page_context_key or ""),
            "source_action": str(source_action or ""),
        },
    )


def get_pending_assessment(browser_session: BrowserSession) -> dict[str, Any] | None:
    """Return the current pending-assessment marker if one exists."""

    pending = getattr(browser_session, "_gh_pending_assessment", None)
    return pending if isinstance(pending, dict) else None


def clear_assessment_pending(
    browser_session: BrowserSession,
    *,
    page_url: str,
    page_context_key: str,
) -> None:
    """Clear the pending-assessment marker when this page has been reassessed."""

    pending = get_pending_assessment(browser_session)
    if not pending:
        return
    pending_context = str(pending.get("page_context_key") or "")
    pending_url = str(pending.get("page_url") or "")
    if pending_context and pending_context != str(page_context_key or ""):
        return
    if pending_url and pending_url != str(page_url or ""):
        return
    try:
        delattr(browser_session, "_gh_pending_assessment")
    except AttributeError:
        pass
