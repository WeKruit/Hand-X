"""Serialize browser-use agent history for the JSONL terminal ``done`` contract."""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_agent_history_payload(
    history: Any,
    sensitive_data: dict[str, Any] | dict[str, str | dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Return a JSON-serializable snapshot of ``AgentHistoryList`` (full step history).

    Used in ``result_data.agentHistory`` so the Desktop run record has the same
    narrative + actions whether the run ends gracefully, is cancelled, errors,
    or is terminated via SIGTERM (last snapshot from :func:`update_runtime_snapshot`).

    Sensitive strings in action parameters are redacted when ``sensitive_data`` matches
    browser-use's ``AgentHistory.model_dump`` contract.
    """
    empty: dict[str, Any] = {
        "schemaVersion": 1,
        "history": [],
        "itemCount": 0,
        "usage": None,
    }
    if history is None:
        return dict(empty)

    raw_items = getattr(history, "history", None)
    item_count = len(raw_items) if isinstance(raw_items, list) else 0

    history_rows: list[dict[str, Any]] = []
    model_dump = getattr(history, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(sensitive_data=sensitive_data)
        except TypeError:
            try:
                dumped = model_dump()
            except Exception as exc:
                logger.warning("agent_history.model_dump_failed", error=str(exc))
                dumped = None
        except Exception as exc:
            logger.warning("agent_history.model_dump_failed", error=str(exc))
            dumped = None

        if isinstance(dumped, dict) and isinstance(dumped.get("history"), list):
            history_rows = [row for row in dumped["history"] if isinstance(row, dict)]
            item_count = len(history_rows)
        elif isinstance(dumped, dict):
            history_rows = []
    elif isinstance(raw_items, list):
        for item in raw_items:
            item_md = getattr(item, "model_dump", None)
            if not callable(item_md):
                continue
            try:
                step = item_md(sensitive_data=sensitive_data)
            except TypeError:
                step = item_md()
            except Exception:
                continue
            if isinstance(step, dict):
                history_rows.append(step)
        item_count = len(history_rows)

    usage_dump: dict[str, Any] | None = None
    usage = getattr(history, "usage", None)
    if usage is not None and hasattr(usage, "model_dump"):
        with contextlib.suppress(Exception):
            usage_dump = usage.model_dump(mode="json", exclude_none=True)

    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "history": history_rows,
        "itemCount": item_count,
        "usage": usage_dump,
    }

    try:
        json.dumps(payload, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("agent_history.not_json_safe", error=str(exc))
        return {
            **empty,
            "itemCount": item_count,
            "historyOmitted": True,
            "historyOmitReason": "not_json_serializable",
        }

    return payload
