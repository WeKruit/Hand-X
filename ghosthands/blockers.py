"""Structured unresolved-blocker helpers shared across runtimes."""

from __future__ import annotations

from typing import Any

from ghosthands.step_trace import get_blocker_attempt_state


def blocker_text_from_extracted(extracted_text: str | None) -> str | None:
    """Return blocker text when the final extracted content carries one."""
    text = str(extracted_text or "").strip()
    if not text:
        return None
    return text if "blocker:" in text.lower() else None


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    return ordered


def _fallback_blocker_message(last_application_state: dict[str, Any] | None) -> str | None:
    if not isinstance(last_application_state, dict):
        return None

    single_blocker = last_application_state.get("single_active_blocker")
    if isinstance(single_blocker, dict):
        blocker_label = (
            str(single_blocker.get("field_label") or "").strip()
            or str(single_blocker.get("field_id") or "").strip()
            or "the current blocker"
        )
        blocker_reason = str(single_blocker.get("reason") or "").strip()
        suffix = f" Reason: {blocker_reason}." if blocker_reason else ""
        return f'blocker: unresolved blocker "{blocker_label}" remains active.{suffix}'

    blocker_count = len(last_application_state.get("blocking_field_keys") or [])
    if blocker_count > 0:
        return f"blocker: unresolved blockers remain on the current page ({blocker_count} total)"

    return None


def build_unresolved_blocker_payload(
    browser_session: Any | None,
    blocker_text: str | None = None,
    *,
    fallback_state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build a first-class unresolved-blocker payload from browser/session state."""
    last_application_state = (
        getattr(browser_session, "_gh_last_application_state", None)
        if browser_session is not None
        else fallback_state
    )
    if last_application_state is None:
        last_application_state = fallback_state

    explicit_payload = (
        getattr(browser_session, "_gh_unresolved_blocker", None)
        if browser_session is not None
        else None
    )
    attempt_state = get_blocker_attempt_state(browser_session) if browser_session is not None else {}

    payload: dict[str, Any] = {}
    if isinstance(last_application_state, dict) and (last_application_state.get("blocking_field_keys") or explicit_payload):
        payload.update(
            {
                "page_context_key": last_application_state.get("page_context_key"),
                "page_url": last_application_state.get("page_url"),
                "same_blocker_signature_count": int(last_application_state.get("same_blocker_signature_count") or 0),
                "blocking_field_keys": list(last_application_state.get("blocking_field_keys") or []),
                "blocking_field_labels": list(last_application_state.get("blocking_field_labels") or []),
                "blocking_field_reasons": dict(last_application_state.get("blocking_field_reasons") or {}),
                "single_active_blocker": last_application_state.get("single_active_blocker"),
                "recovery_target": last_application_state.get("recovery_target"),
                "source": "application_state",
            }
        )

    if isinstance(explicit_payload, dict):
        payload.update(explicit_payload)

    single_blocker = payload.get("single_active_blocker")
    if isinstance(single_blocker, dict):
        blocker_key = str(single_blocker.get("field_key") or "").strip()
        if blocker_key:
            attempts = attempt_state.get(blocker_key, {})
            attempted_strategies = attempts.get("attempted_strategies") or []
            if attempted_strategies:
                payload["attempted_strategies"] = _dedupe_preserve_order(list(attempted_strategies))
            last_strategy = str(attempts.get("last_attempt_strategy") or "").strip()
            if last_strategy:
                payload["last_attempt_strategy"] = last_strategy

    message = str(blocker_text or payload.get("message") or "").strip() or _fallback_blocker_message(last_application_state)
    if message:
        payload["message"] = message

    if not payload and blocker_text:
        return {
            "source": "done_text",
            "message": blocker_text.strip(),
            "requires_human": True,
        }

    if payload:
        payload["requires_human"] = True
        return payload

    return None
