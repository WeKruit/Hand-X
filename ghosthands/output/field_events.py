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

_installed = False


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
            emit_progress,
        )

        # Track cumulative fill counts across rounds
        _counts = {"filled": 0, "total": 0, "last_round": 0}

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
                    value=result.value_set or "",
                    method="domhand",
                )
            else:
                emit_field_failed(
                    field=result.name,
                    error=result.error or "unknown error",
                )

            # Emit cumulative progress after each field
            emit_progress(
                filled=_counts["filled"],
                total=_counts["total"],
                round=round_num,
            )

        fill_module._on_field_result = _on_field_result
        logger.debug("field_events: JSONL callback installed on domhand_fill")

    except ImportError:
        logger.debug("field_events: domhand_fill not available, skipping")
