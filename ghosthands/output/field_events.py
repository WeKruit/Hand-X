"""DomHand field-level event bridge for JSONL output.

When the CLI runs in JSONL mode, this module installs a callback on
``domhand_fill`` that emits ``field_filled`` / ``field_failed`` events
as each form field is processed -- in real time, not after the fill
completes.

Usage::

    from ghosthands.output.field_events import install_jsonl_callback
    install_jsonl_callback()
    # All subsequent domhand_fill calls will emit per-field JSONL.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Field labels containing any of these substrings (case-insensitive) will have
# their values replaced with "[REDACTED]" before being emitted over JSONL.
_SENSITIVE_FIELD_PATTERNS: frozenset[str] = frozenset(
    {
        "password",
        "ssn",
        "social_security",
        "social security",
        "date_of_birth",
        "date of birth",
        "salary",
        "compensation",
        "bank",
        "routing",
        "account_number",
    }
)


def _redact_if_sensitive(label: str, value: str) -> str:
    """Return ``value`` unchanged, or ``"[REDACTED]"`` when *label* matches a sensitive pattern."""
    label_lower = label.lower()
    for pattern in _SENSITIVE_FIELD_PATTERNS:
        if pattern in label_lower:
            return "[REDACTED]"
    return value


_installed = False

# Track cumulative fill counts across rounds (module-level for external access)
_counts: dict[str, int] = {"filled": 0, "total": 0, "last_round": 0}


def get_field_counts() -> tuple[int, int]:
    """Return (filled, failed) counts from the installed callback.

    Returns ``(0, 0)`` if the callback was never installed or no fields
    have been processed yet.
    """
    if not _installed:
        return (0, 0)
    filled = _counts.get("filled", 0)
    total = _counts.get("total", 0)
    return (filled, total - filled)


def install_jsonl_callback() -> None:
    """Wire the JSONL emitter into ``domhand_fill``'s field result callback.

    Sets ``domhand_fill._on_field_result`` to a function that emits
    ``field_filled`` or ``field_failed`` for each FillFieldResult, plus
    cumulative ``progress`` events.

    Safe to call multiple times -- only installs once.
    """
    global _installed
    if _installed:
        return
    _installed = True

    try:
        from ghosthands.actions import domhand_fill as fill_module
        from ghosthands.output.jsonl import (
            emit_field_failed,
            emit_field_filled,
            emit_phase,
            emit_progress,
        )

        def _on_field_result(result, round_num: int) -> None:
            """Called by domhand_fill for each FillFieldResult."""
            # Reset counts on new round
            if round_num != _counts["last_round"]:
                _counts["last_round"] = round_num

            _counts["total"] += 1

            if result.success:
                _counts["filled"] += 1
                emit_field_filled(
                    field=result.name,
                    value=_redact_if_sensitive(result.name, result.value_set or ""),
                    method="domhand",
                )
                if _counts["filled"] > 0 and _counts["filled"] % 5 == 0:
                    emit_phase(f"Filling form fields ({_counts['filled']} completed)")
            else:
                emit_field_failed(
                    field=result.name,
                    reason=result.error or "unknown error",
                )

            # Emit cumulative progress after each field
            emit_progress(
                step=_counts["filled"],
                max_steps=_counts["total"],
                description=f"Round {round_num}",
            )

        fill_module._on_field_result = _on_field_result
        logger.debug("field_events: JSONL callback installed on domhand_fill")

    except ImportError:
        logger.debug("field_events: domhand_fill not available, skipping")
