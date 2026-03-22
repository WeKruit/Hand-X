from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from typing import Any, cast

from pydantic import ValidationError

from browser_use.agent.service import Agent
from browser_use.agent.views import ActionResult, AgentOutput
from browser_use.browser.events import SendKeysEvent
from browser_use.browser.views import BrowserStateSummary
from browser_use.llm.exceptions import ModelProviderError, ModelRateLimitError
from browser_use.llm.messages import BaseMessage, UserMessage
from browser_use.observability import observe_debug
from browser_use.utils import time_execution_async
from ghosthands.config.settings import settings
from ghosthands.step_trace import get_blocker_attempt_state, publish_browser_session_trace


def _truncate_log_text(text: str | None, limit: int | None = None) -> str:
    if not text:
        return ""
    if limit is None:
        raw_limit = os.getenv("GH_AGENT_LOG_TEXT_LIMIT", "220").strip()
        try:
            limit = max(40, int(raw_limit))
        except ValueError:
            limit = 220
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _normalize_blocker_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text).replace("*", " ")).strip().lower()


def _extract_action_name_params(action: Any) -> tuple[str, dict[str, Any]]:
    action_data = action.model_dump(exclude_unset=True) if hasattr(action, "model_dump") else {}
    if not isinstance(action_data, dict) or not action_data:
        return "unknown", {}
    action_name = next(iter(action_data.keys()), "unknown")
    params = action_data.get(action_name, {})
    return action_name, params if isinstance(params, dict) else {}


def _blocker_field_family(field_type: str | None) -> str:
    normalized = _normalize_blocker_text(str(field_type or "").replace("-", " "))
    if normalized in {"select", "combobox", "listbox"}:
        return "select"
    if normalized in {"checkbox", "checkbox group", "radio", "radio group", "toggle", "button group"}:
        return "binary"
    if normalized in {"text", "email", "tel", "url", "number", "search", "textarea", "date"}:
        return "text"
    return normalized or "unknown"


def _blocker_display_text(blocker: dict[str, Any]) -> str:
    return str(
        blocker.get("field_label")
        or blocker.get("question_text")
        or blocker.get("field_id")
        or "the current blocker"
    ).strip()


def _build_compact_runtime_audit_text(last_application_state: dict[str, Any] | None) -> str | None:
    if not isinstance(last_application_state, dict):
        return None

    current_section = str(last_application_state.get("current_section") or "").strip() or "(unknown)"
    unresolved_required = int(last_application_state.get("unresolved_required_count") or 0)
    advance_allowed = bool(last_application_state.get("advance_allowed"))
    labels = [
        str(label).strip()
        for label in (
            last_application_state.get("blocking_field_labels_ordered")
            or last_application_state.get("blocking_field_labels")
            or []
        )
        if str(label).strip()
    ]
    blocker_text = ", ".join(labels[:4])
    if len(labels) > 4:
        blocker_text += f" (+{len(labels) - 4} more)"

    text = (
        "RUNTIME_PAGE_AUDIT: "
        f"section={current_section}; "
        f"advance_allowed={advance_allowed}; "
        f"unresolved_required={unresolved_required}."
    )
    if blocker_text:
        text += f" Active blockers: {blocker_text}."

    primary_blocker = last_application_state.get("primary_active_blocker")
    if not isinstance(primary_blocker, dict):
        primary_blocker = last_application_state.get("single_active_blocker")
    if isinstance(primary_blocker, dict):
        primary_label = _blocker_display_text(primary_blocker)
        if primary_label:
            text += f" Resolve this blocker next: {primary_label}."
    return text


def _candidate_blocker_strings_from_node(node: Any) -> list[str]:
    candidates: list[str] = []
    attributes = getattr(node, "attributes", None) or {}
    if isinstance(attributes, dict):
        for key in ("id", "name", "aria-label", "placeholder", "title", "value"):
            value = attributes.get(key)
            if value:
                candidates.append(str(value))
    ax_node = getattr(node, "ax_node", None)
    ax_name = getattr(ax_node, "name", None) if ax_node is not None else None
    if ax_name:
        candidates.append(str(ax_name))
    with contextlib.suppress(Exception):
        meaningful = node.get_meaningful_text_for_llm()
        if meaningful:
            candidates.append(str(meaningful))
    with contextlib.suppress(Exception):
        children_text = node.get_all_children_text()
        if children_text:
            candidates.append(str(children_text))
    return [candidate for candidate in candidates if str(candidate).strip()]


def _extract_dom_role(node: Any) -> str:
    attrs = getattr(node, "attributes", None) or {}
    role = str(attrs.get("role") or "").strip().lower()
    if role:
        return role
    ax_node = getattr(node, "ax_node", None)
    ax_role = str(getattr(ax_node, "role", None) or "").strip().lower()
    return ax_role


def _pending_recovery_display_text(pending: dict[str, Any]) -> str:
    return str(
        pending.get("field_label")
        or pending.get("field_desc")
        or pending.get("field_index")
        or "the current field"
    ).strip()


def _pending_recovery_expected_text(pending: dict[str, Any]) -> str:
    return str(
        pending.get("last_option_text")
        or pending.get("expected_value")
        or ""
    ).strip()


def _pending_recovery_eval_is_negative(evaluation_text: str) -> bool:
    normalized = _normalize_blocker_text(evaluation_text)
    return any(
        marker in normalized
        for marker in (
            "uncertain",
            "failure",
            "failed",
            "not yet",
            "does not yet",
            "did not",
            "not reflected",
            "not visible",
            "not appear",
            "not persisted",
            "not recorded",
            "not successfully",
        )
    )


def _pending_recovery_eval_is_positive(evaluation_text: str) -> bool:
    normalized = _normalize_blocker_text(evaluation_text)
    if not normalized or _pending_recovery_eval_is_negative(evaluation_text):
        return False
    return any(
        marker in normalized
        for marker in (
            "success",
            "successfully",
            "was successful",
            "selection was successful",
            "selected",
            "committed",
            "entered",
            "filled",
        )
    )


def _dom_node_matches_blocker(node: Any, blocker: dict[str, Any]) -> bool:
    blocker_texts = [
        str(blocker.get("field_id") or "").strip(),
        str(blocker.get("field_label") or "").strip(),
        str(blocker.get("question_text") or "").strip(),
    ]
    normalized_blockers = [_normalize_blocker_text(text) for text in blocker_texts if text]
    if not normalized_blockers:
        return False

    for candidate in _candidate_blocker_strings_from_node(node):
        normalized_candidate = _normalize_blocker_text(candidate)
        if not normalized_candidate:
            continue
        for normalized_blocker in normalized_blockers:
            if (
                normalized_candidate == normalized_blocker
                or normalized_blocker in normalized_candidate
                or normalized_candidate in normalized_blocker
            ):
                return True
    return False


def _action_targets_single_blocker(action_name: str, params: dict[str, Any], blocker: dict[str, Any]) -> bool:
    blocker_id = str(blocker.get("field_id") or "").strip()
    blocker_label = _normalize_blocker_text(blocker.get("field_label") or blocker.get("question_text") or "")
    blocker_family = _blocker_field_family(blocker.get("field_type"))
    param_field_id = str(params.get("field_id") or "").strip()
    param_label = _normalize_blocker_text(
        params.get("field_label") or params.get("question_text") or params.get("name") or ""
    )
    if action_name == "domhand_assess_state":
        return True
    if action_name == "domhand_interact_control":
        return bool(
            (param_field_id and blocker_id and param_field_id == blocker_id)
            or (param_label and blocker_label and param_label == blocker_label)
        )
    if action_name == "domhand_record_expected_value":
        return bool(
            (param_field_id and blocker_id and param_field_id == blocker_id)
            or (param_label and blocker_label and param_label == blocker_label)
        )
    if action_name == "domhand_fill":
        focus_fields = params.get("focus_fields") or []
        normalized_focus = {_normalize_blocker_text(label) for label in focus_fields if str(label).strip()}
        return bool(blocker_label and blocker_label in normalized_focus)
    if action_name == "domhand_select":
        if blocker_family != "select":
            return False
        if param_field_id:
            return bool(blocker_id and param_field_id == blocker_id)
        if param_label:
            return bool(blocker_label and param_label == blocker_label)
        return True
    return False


def _classify_blocker_strategy(action_name: str, params: dict[str, Any]) -> str:
    if action_name in {"domhand_fill", "domhand_select", "domhand_interact_control", "domhand_record_expected_value"}:
        return action_name
    if action_name in {"click", "input", "send_keys"}:
        return f"manual_{action_name}"
    return action_name


def _is_manual_recovery_action(action_name: str) -> bool:
    return action_name in {"click", "input", "send_keys"}


def _is_mutating_blocker_action(action_name: str) -> bool:
    return action_name not in {"wait", "domhand_assess_state"}


def _result_requests_immediate_assessment(result: ActionResult) -> bool:
    if result.error or result.is_done:
        return False
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    recommended = str(metadata.get("recommended_next_action") or "").strip().lower()
    return recommended == "call domhand_assess_state"


def _manual_input_should_wait_for_selection(recovery_target: dict[str, Any], result: ActionResult) -> bool:
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    if metadata.get("is_autocomplete_field") is True:
        return True
    return str(recovery_target.get("verification_mode") or "").strip().lower() == "selected_option"


def _blocker_prefers_selection_commit(blocker: dict[str, Any]) -> bool:
    family = _blocker_field_family(blocker.get("field_type"))
    widget_kind = _normalize_blocker_text(blocker.get("widget_kind") or "")
    return family == "select" or widget_kind in {
        "custom combobox",
        "custom select",
        "native select",
        "group selection",
    }


def _blocker_guard_decision(
    last_state: dict[str, Any] | None,
    actions: list[dict[str, Any]],
    attempt_state: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(last_state, dict):
        return None
    blocker_keys = [str(value).strip() for value in (last_state.get("blocking_field_keys") or []) if str(value).strip()]
    if not blocker_keys:
        return None
    manual_actions = []
    non_observation_actions = []
    for action in actions:
        action_name = str(action.get("action") or "unknown")
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        if action_name in {"click", "input", "send_keys"}:
            manual_actions.append((action_name, params))
        if action_name not in {"wait", "domhand_assess_state"}:
            non_observation_actions.append((action_name, params))
    if manual_actions and (len(manual_actions) > 1 or len(non_observation_actions) > 1):
        return {
            "reason": "manual_multi_action_with_active_blockers",
            "recovery_target": last_state.get("recovery_target"),
            "strategy": "manual_recovery_batch",
            "message": (
                "Active blockers are present on this page. "
                "Do exactly one manual field action next, then wait or reassess before touching any other control."
            ),
            "recommended_next_action": (
                "use one click/input/send_keys action for the current blocker, optionally followed only by wait, "
                "then call domhand_assess_state or inspect the updated page state"
            ),
        }
    attempt_state = attempt_state or {}
    single_blocker = last_state.get("single_active_blocker")
    recovery_target = last_state.get("recovery_target") if isinstance(last_state.get("recovery_target"), dict) else None
    if isinstance(single_blocker, dict):
        blocker_key = str(single_blocker.get("field_key") or "").strip()
        blocker_change = str((last_state.get("blocking_field_state_changes") or {}).get(blocker_key) or "").strip()
        last_attempt = attempt_state.get(blocker_key, {})
        attempted = last_attempt.get("attempted_strategies") or []
        mutating_actions = [
            action for action in actions if _is_mutating_blocker_action(str(action.get("action") or "unknown"))
        ]
        manual_mutating_actions = [
            action for action in mutating_actions if _is_manual_recovery_action(str(action.get("action") or "unknown"))
        ]
        if manual_mutating_actions and len(mutating_actions) > 1:
            return {
                "reason": "multi_action_single_blocker",
                "blocker_key": blocker_key,
                "blocker": single_blocker,
                "recovery_target": recovery_target,
                "strategy": "manual_serial_only",
                "message": (
                    f'Latest domhand_assess_state anchors recovery to a single blocker: '
                    f'"{_blocker_display_text(single_blocker)}". '
                    "Manual recovery must use exactly one field-mutating action per step, then reassess immediately."
                ),
                "recommended_next_action": (
                    "type or click one control only, wait/observe, then call domhand_assess_state before touching another field"
                ),
            }
        exhausted = blocker_change == "no_state_change" and isinstance(attempted, list) and len(attempted) >= 2
        if not exhausted:
            for action in actions:
                action_name = str(action.get("action") or "unknown")
                params = action.get("params") if isinstance(action.get("params"), dict) else {}
                if action_name == "domhand_assess_state":
                    continue
                if action_name in ("click", "input", "send_keys"):
                    domhand_tried = isinstance(attempted, list) and len(attempted) >= 1
                    if not domhand_tried:
                        return {
                            "reason": "manual_recovery_before_domhand",
                            "blocker_key": blocker_key,
                            "blocker": single_blocker,
                            "recovery_target": recovery_target,
                            "strategy": _classify_blocker_strategy(action_name, params),
                            "message": (
                                f'A single active blocker "{_blocker_display_text(single_blocker)}" '
                                "has not been attempted with DomHand yet. "
                                "Try domhand_interact_control or domhand_select first before falling back to manual clicks."
                            ),
                            "recommended_next_action": (
                                "use domhand_interact_control with the exact field_id/field_type for binary controls, "
                                "use domhand_select for select blockers, or call domhand_assess_state again"
                            ),
                        }
                    return None
                if not _action_targets_single_blocker(action_name, params, single_blocker):
                    return {
                        "reason": "unrelated_action_with_single_blocker",
                        "blocker_key": blocker_key,
                        "blocker": single_blocker,
                        "recovery_target": recovery_target,
                        "strategy": _classify_blocker_strategy(action_name, params),
                        "message": (
                            f'Latest domhand_assess_state reports a single active blocker: '
                            f'"{_blocker_display_text(single_blocker)}". '
                            "Do not target unrelated fields before this blocker is cleared or reassessed."
                        ),
                        "recommended_next_action": (
                            "use domhand_interact_control with the exact field_id/field_type for binary controls, "
                            "use domhand_select for select blockers, or call domhand_assess_state again"
                        ),
                    }
                current_strategy = _classify_blocker_strategy(action_name, params)
                if blocker_change == "no_state_change" and last_attempt.get("last_attempt_strategy") == current_strategy:
                    return {
                        "reason": "same_strategy_no_state_change",
                        "blocker_key": blocker_key,
                        "blocker": single_blocker,
                        "recovery_target": recovery_target,
                        "strategy": current_strategy,
                        "message": (
                            f'The blocker "{_blocker_display_text(single_blocker)}" '
                            "has not changed since the last assessment. "
                            f'Do not retry the same strategy "{current_strategy}" again.'
                        ),
                        "recommended_next_action": last_attempt.get("recommended_next_action")
                        or "change recovery strategy for the same blocker, then reassess immediately",
                    }
                return None
            return None
        for action in actions:
            action_name = str(action.get("action") or "unknown")
            params = action.get("params") if isinstance(action.get("params"), dict) else {}
            if action_name == "domhand_assess_state":
                continue
            if action_name in ("click", "input", "send_keys"):
                return None
            if not _action_targets_single_blocker(action_name, params, single_blocker):
                return {
                    "reason": "unrelated_action_with_single_blocker",
                    "blocker_key": blocker_key,
                    "blocker": single_blocker,
                    "recovery_target": recovery_target,
                    "strategy": _classify_blocker_strategy(action_name, params),
                    "message": (
                        f'Latest domhand_assess_state still anchors recovery to "{_blocker_display_text(single_blocker)}". '
                        "Do not target unrelated fields while this recovery target is active."
                    ),
                    "recommended_next_action": (
                        "use domhand_assess_state, or continue manual/browser-use recovery on the same field only"
                    ),
                }
        return None

    blocker_set = set(blocker_keys)
    blocker_labels = {
        _normalize_blocker_text(label) for label in (last_state.get("blocking_field_labels") or []) if str(label).strip()
    }
    for action in actions:
        action_name = str(action.get("action") or "unknown")
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        if action_name not in {"domhand_fill", "domhand_interact_control", "domhand_record_expected_value", "domhand_select"}:
            continue
        param_field_id = str(params.get("field_id") or "").strip()
        param_label = _normalize_blocker_text(params.get("field_label") or params.get("name") or "")
        focus_fields = params.get("focus_fields") or []
        normalized_focus = {_normalize_blocker_text(label) for label in focus_fields if str(label).strip()}
        if param_field_id and param_field_id not in blocker_set:
            return {
                "reason": "explicit_non_blocker_target",
                "strategy": _classify_blocker_strategy(action_name, params),
                "message": "The planned action explicitly targets a field that is not in the latest blocker set.",
            }
        if param_label and param_label not in blocker_labels:
            return {
                "reason": "explicit_non_blocker_target",
                "strategy": _classify_blocker_strategy(action_name, params),
                "message": "The planned action explicitly targets a label that is not in the latest blocker set.",
            }
        if normalized_focus and normalized_focus.isdisjoint(blocker_labels):
            return {
                "reason": "explicit_non_blocker_target",
                "strategy": _classify_blocker_strategy(action_name, params),
                "message": "The planned focus_fields do not match the latest blocker set.",
            }
    return None


def _build_blocker_guard_action_result(guard_decision: dict[str, Any]) -> ActionResult:
    message = guard_decision.get("message") or "Active blocker guard rejected the planned action."
    recommended = guard_decision.get("recommended_next_action") or ""
    content = f"{message} {recommended}".strip() if recommended else message
    return ActionResult(
        extracted_content=content,
        include_extracted_content_only_once=True,
        metadata={
            "blocker_guard": True,
            "reason": guard_decision.get("reason"),
            "strategy": guard_decision.get("strategy"),
            "blocker": guard_decision.get("blocker"),
            "recovery_target": guard_decision.get("recovery_target"),
            "recommended_next_action": recommended,
        },
    )


def _log_response(response: AgentOutput, logger: logging.Logger) -> None:
    compact_logs = os.getenv("BROWSER_USE_COMPACT_LOGS", "false").lower()[:1] in "ty1"

    if response.current_state.thinking:
        logger.debug(f"💡 Thinking:\n{response.current_state.thinking}")

    eval_goal = response.current_state.evaluation_previous_goal
    if eval_goal:
        eval_goal = _truncate_log_text(eval_goal, 180)
        if "success" in eval_goal.lower():
            logger.info(f"  \033[32m👍 Eval: {eval_goal}\033[0m")
        elif "failure" in eval_goal.lower():
            logger.info(f"  \033[31m⚠️ Eval: {eval_goal}\033[0m")
        else:
            logger.info(f"  ❔ Eval: {eval_goal}")

    if response.current_state.memory and not compact_logs:
        logger.info(f"  🧠 Memory: {_truncate_log_text(response.current_state.memory)}")

    next_goal = response.current_state.next_goal
    if next_goal and not compact_logs:
        logger.info(f"  \033[34m🎯 Next goal: {_truncate_log_text(next_goal)}\033[0m")


class HandXAgent(Agent):
    async def _pending_manual_recovery_is_settled(self, pending: dict[str, Any]) -> bool:
        if self.browser_session is None:
            return False
        if str(pending.get("phase") or "").strip().lower() != "awaiting_settlement":
            return False

        evaluation_text = ""
        last_model_output = getattr(self.state, "last_model_output", None)
        current_state = getattr(last_model_output, "current_state", None) if last_model_output is not None else None
        if current_state is not None:
            evaluation_text = str(getattr(current_state, "evaluation_previous_goal", "") or "")
        if evaluation_text:
            if _pending_recovery_eval_is_negative(evaluation_text):
                return False
            if _pending_recovery_eval_is_positive(evaluation_text):
                pending_label = _normalize_blocker_text(_pending_recovery_display_text(pending))
                expected_text = _normalize_blocker_text(_pending_recovery_expected_text(pending))
                normalized_eval = _normalize_blocker_text(evaluation_text)
                if (
                    (pending_label and pending_label in normalized_eval)
                    or (expected_text and expected_text in normalized_eval)
                    or ("selection was successful" in normalized_eval)
                    or ("selected" in normalized_eval)
                ):
                    return True

        field_index = pending.get("field_index")
        if not isinstance(field_index, int):
            return False

        try:
            node = await self.browser_session.get_dom_element_by_index(field_index)
        except Exception:
            node = None
        if node is None:
            return False

        expected_text = _normalize_blocker_text(_pending_recovery_expected_text(pending))
        if not expected_text:
            return False

        for candidate in _candidate_blocker_strings_from_node(node):
            normalized_candidate = _normalize_blocker_text(candidate)
            if not normalized_candidate:
                continue
            if (
                normalized_candidate == expected_text
                or expected_text in normalized_candidate
                or normalized_candidate in expected_text
            ):
                return True
        return False

    async def _get_pending_manual_recovery(self) -> dict[str, Any] | None:
        if settings.enable_domhand or self.browser_session is None:
            return None
        pending = getattr(self.browser_session, "_gh_pending_manual_recovery", None)
        if not isinstance(pending, dict):
            return None
        if await self._pending_manual_recovery_is_settled(pending):
            setattr(self.browser_session, "_gh_pending_manual_recovery", None)
            return None
        return pending

    async def _guard_pending_manual_recovery(self, actions_data: list[dict[str, Any]]) -> dict[str, Any] | None:
        pending = await self._get_pending_manual_recovery()
        if pending is None or self.browser_session is None:
            return None

        field_label = _pending_recovery_display_text(pending)
        field_index = pending.get("field_index")
        for action in actions_data:
            action_name = str(action.get("action") or "unknown")
            params = action.get("params") if isinstance(action.get("params"), dict) else {}
            if action_name == "wait":
                continue

            index = params.get("index")
            if action_name in {"input", "dropdown_options", "select_dropdown"} and isinstance(index, int):
                if index == field_index:
                    continue
                return {
                    "reason": "pending_manual_recovery",
                    "strategy": action_name,
                    "message": (
                        f'The previous combobox/select recovery for "{field_label}" has not visibly settled yet. '
                        "Do not move to another question yet."
                    ),
                    "recommended_next_action": (
                        f'continue working on "{field_label}" only: wait, reopen the same field, or select its option again'
                    ),
                }

            if action_name == "click" and isinstance(index, int):
                try:
                    node = await self.browser_session.get_dom_element_by_index(index)
                except Exception:
                    node = None
                if index == field_index:
                    continue
                if node is not None and _extract_dom_role(node) == "option":
                    continue
                return {
                    "reason": "pending_manual_recovery",
                    "strategy": action_name,
                    "message": (
                        f'The previous combobox/select recovery for "{field_label}" has not visibly settled yet. '
                        "Do not move to another question yet."
                    ),
                    "recommended_next_action": (
                        f'continue working on "{field_label}" only: wait, reopen the same field, or click its matching option again'
                    ),
                }

            return {
                "reason": "pending_manual_recovery",
                "strategy": action_name,
                "message": (
                    f'The previous combobox/select recovery for "{field_label}" has not visibly settled yet. '
                    "Finish that same field before taking a different action."
                ),
                "recommended_next_action": (
                    f'continue working on "{field_label}" only until the field visibly shows the selected value'
                ),
            }
        return None

    def _update_pending_manual_recovery_after_action(
        self,
        *,
        action_name: str,
        params: dict[str, Any],
        result: ActionResult,
    ) -> None:
        if settings.enable_domhand or self.browser_session is None or result.error or result.is_done:
            return
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        pending = getattr(self.browser_session, "_gh_pending_manual_recovery", None)
        current_pending = pending if isinstance(pending, dict) else None

        if action_name == "input":
            if metadata.get("is_autocomplete_field") is not True:
                return
            field_index = params.get("index")
            if not isinstance(field_index, int):
                return
            setattr(
                self.browser_session,
                "_gh_pending_manual_recovery",
                {
                    "field_index": field_index,
                    "field_label": str(metadata.get("field_label") or metadata.get("field_desc") or "").strip(),
                    "field_desc": str(metadata.get("field_desc") or "").strip(),
                    "expected_value": str(params.get("text") or "").strip(),
                    "phase": "awaiting_selection",
                    "created_step": getattr(self.state, "n_steps", 0),
                },
            )
            return

        if action_name == "select_dropdown" and current_pending is not None:
            field_index = params.get("index")
            if isinstance(field_index, int) and field_index == current_pending.get("field_index"):
                updated = dict(current_pending)
                updated["phase"] = "awaiting_settlement"
                updated["expected_value"] = str(params.get("text") or updated.get("expected_value") or "").strip()
                setattr(self.browser_session, "_gh_pending_manual_recovery", updated)
            return

        if action_name == "click" and current_pending is not None:
            field_index = params.get("index")
            if isinstance(field_index, int) and field_index == current_pending.get("field_index"):
                return
            if metadata.get("is_option_click") is True:
                updated = dict(current_pending)
                updated["phase"] = "awaiting_settlement"
                option_text = str(metadata.get("option_text") or "").strip()
                if option_text:
                    updated["last_option_text"] = option_text
                    updated["expected_value"] = option_text
                setattr(self.browser_session, "_gh_pending_manual_recovery", updated)

    async def _guard_primary_blocker_target(
        self,
        actions_data: list[dict[str, Any]],
        last_state: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(last_state, dict) or self.browser_session is None:
            return None
        blocker_keys = [str(value).strip() for value in (last_state.get("blocking_field_keys") or []) if str(value).strip()]
        if len(blocker_keys) <= 1:
            return None
        primary_blocker = last_state.get("primary_active_blocker")
        if not isinstance(primary_blocker, dict):
            return None

        for action in actions_data:
            action_name = str(action.get("action") or "unknown")
            params = action.get("params") if isinstance(action.get("params"), dict) else {}
            if action_name not in {"click", "input"}:
                continue
            index = params.get("index")
            if not isinstance(index, int):
                continue
            node = await self.browser_session.get_dom_element_by_index(index)
            if node is None:
                continue
            if _dom_node_matches_blocker(node, primary_blocker):
                continue
            primary_label = _blocker_display_text(primary_blocker)
            return {
                "reason": "manual_non_primary_blocker_target",
                "blocker": primary_blocker,
                "strategy": _classify_blocker_strategy(action_name, params),
                "message": (
                    f'The latest runtime page audit still prioritizes "{primary_label}". '
                    "Do not interact with a later field until that required blocker is resolved or explicitly reported as missing."
                ),
                "recommended_next_action": (
                    f'target the primary blocker "{primary_label}" next, or report done(success=False) if the applicant profile truly lacks the answer'
                ),
            }
        return None

    def _inject_loop_detection_nudge(self) -> None:
        if not self.settings.loop_detection_enabled:
            return
        nudge = self.state.loop_detector.get_nudge_message()
        if not nudge:
            return
        last_state = getattr(self.browser_session, "_gh_last_application_state", None) if self.browser_session else None
        blocker_context = ""
        if isinstance(last_state, dict):
            blocker_keys = [str(key).strip() for key in (last_state.get("blocking_field_keys") or []) if str(key).strip()]
            same_blocker_signature_count = int(last_state.get("same_blocker_signature_count") or 0)
            if blocker_keys:
                blocker_context = (
                    "\n\nLatest domhand_assess_state still has active blockers on this page. "
                    "Only target one current blocker next. Do not retry fields that are not in the latest blocker set."
                )
                if same_blocker_signature_count >= 1:
                    blocker_context += (
                        " The blocker set has not changed since the last assessment. "
                        "Change strategy on one current blocker instead of refilling the same answer again."
                    )
        if blocker_context:
            nudge += blocker_context
        self.logger.info(
            f"🔁 Loop detection nudge injected (repetition={self.state.loop_detector.max_repetition_count}, "
            f"stagnation={self.state.loop_detector.consecutive_stagnant_pages})"
        )
        self._message_manager._add_context_message(UserMessage(content=nudge))

    @time_execution_async("--get_next_action")
    @observe_debug(ignore_input=True, ignore_output=True, name="get_model_output")
    async def get_model_output(self, input_messages: list[BaseMessage]) -> AgentOutput:
        urls_replaced = self._process_messsages_and_replace_long_urls_shorter_ones(input_messages)
        kwargs: dict[str, Any] = {"output_format": self.AgentOutput, "session_id": self.session_id}

        try:
            response = await self.llm.ainvoke(input_messages, **kwargs)
            parsed: AgentOutput = response.completion  # type: ignore[assignment]

            if urls_replaced:
                self._recursive_process_all_strings_inside_pydantic_model(parsed, urls_replaced)

            if len(parsed.action) > self.settings.max_actions_per_step:
                parsed.action = parsed.action[: self.settings.max_actions_per_step]

            if not (hasattr(self.state, "paused") and (self.state.paused or self.state.stopped)):
                _log_response(parsed, self.logger)
                await self._broadcast_model_state(parsed)

            self._log_next_action_summary(parsed)
            return parsed
        except ValidationError:
            raise
        except (ModelRateLimitError, ModelProviderError) as e:
            if not self._try_switch_to_fallback_llm(e):
                raise
            response = await self.llm.ainvoke(input_messages, **kwargs)
            parsed = response.completion  # type: ignore[assignment]
            if urls_replaced:
                self._recursive_process_all_strings_inside_pydantic_model(parsed, urls_replaced)
            if len(parsed.action) > self.settings.max_actions_per_step:
                parsed.action = parsed.action[: self.settings.max_actions_per_step]
            if not (hasattr(self.state, "paused") and (self.state.paused or self.state.stopped)):
                _log_response(parsed, self.logger)
                await self._broadcast_model_state(parsed)
            self._log_next_action_summary(parsed)
            return parsed

    async def _execute_actions(self) -> None:
        if self.state.last_model_output is None:
            raise ValueError("No model output to execute actions from")

        actions_data = []
        for action in self.state.last_model_output.action:
            action_name, params = _extract_action_name_params(action)
            actions_data.append({"action": action_name, "params": params})
        last_state = getattr(self.browser_session, "_gh_last_application_state", None) if self.browser_session else None
        guard_decision = await self._guard_pending_manual_recovery(actions_data)
        if guard_decision is None:
            guard_decision = _blocker_guard_decision(
                last_state,
                actions_data,
                get_blocker_attempt_state(self.browser_session),
            )
        if guard_decision is None:
            guard_decision = await self._guard_primary_blocker_target(actions_data, last_state)
        if guard_decision is not None:
            self.logger.info(f"⛔ Blocker guard: {_truncate_log_text(str(guard_decision.get('message') or ''))}")
            if self.browser_session:
                await publish_browser_session_trace(
                    self.browser_session,
                    "blocker_guard_reject",
                    {
                        "step": self.state.n_steps,
                        "decision": guard_decision,
                        "actions": actions_data,
                    },
                )
            self.state.last_result = [_build_blocker_guard_action_result(guard_decision)]
            return

        result = await self.multi_act(self.state.last_model_output.action)
        self.state.last_result = result

    async def _run_runtime_assess_state_followup(self, *, reason: str) -> ActionResult | None:
        if not settings.enable_domhand:
            return None
        if self.browser_session is None:
            return None
        try:
            from ghosthands.actions.domhand_assess_state import domhand_assess_state
            from ghosthands.actions.views import DomHandAssessStateParams
            assess_logger = logging.getLogger("ghosthands.actions.domhand_assess_state")
            fill_logger = logging.getLogger("ghosthands.actions.domhand_fill")

            self.logger.info(f"🔁 Runtime follow-up: domhand_assess_state ({reason})")
            await publish_browser_session_trace(
                self.browser_session,
                "runtime_followup",
                {
                    "reason": reason,
                    "tool": "domhand_assess_state",
                    "step": self.state.n_steps,
                },
            )
            previous_assess_level = assess_logger.level
            previous_fill_level = fill_logger.level
            assess_logger.setLevel(logging.ERROR)
            fill_logger.setLevel(logging.ERROR)
            try:
                raw_result = await domhand_assess_state(DomHandAssessStateParams(), self.browser_session)
            finally:
                assess_logger.setLevel(previous_assess_level)
                fill_logger.setLevel(previous_fill_level)

            compact_text = _build_compact_runtime_audit_text(
                getattr(self.browser_session, "_gh_last_application_state", None)
            )
            if compact_text:
                return ActionResult(
                    extracted_content=compact_text,
                    include_extracted_content_only_once=True,
                    metadata={"runtime_page_audit": True, "reason": reason},
                )
            return raw_result
        except Exception as exc:
            self.logger.warning(f"Runtime follow-up domhand_assess_state failed: {type(exc).__name__}: {exc}")
            return ActionResult(error=f"Runtime follow-up domhand_assess_state failed: {type(exc).__name__}: {exc}")

    async def _maybe_record_active_recovery_target(self, recovery_target: dict[str, Any]) -> ActionResult | None:
        if not settings.enable_domhand:
            return None
        if self.browser_session is None:
            return None

        field_id = str(recovery_target.get("field_id") or "").strip()
        field_type = str(recovery_target.get("field_type") or "").strip()
        desired_value = str(recovery_target.get("desired_value") or "").strip()
        field_label = str(recovery_target.get("field_label") or recovery_target.get("question_text") or "").strip()
        target_section = str(recovery_target.get("section") or "").strip() or None

        if not (field_id and field_type and desired_value and field_label):
            return None

        try:
            from ghosthands.actions.domhand_record_expected_value import domhand_record_expected_value
            from ghosthands.actions.views import DomHandRecordExpectedValueParams

            self.logger.info(f"🔁 Runtime follow-up: domhand_record_expected_value ({field_id}={desired_value})")
            await publish_browser_session_trace(
                self.browser_session,
                "runtime_followup",
                {
                    "reason": "manual_recovery",
                    "tool": "domhand_record_expected_value",
                    "field_id": field_id,
                    "desired_value": desired_value,
                    "step": self.state.n_steps,
                },
            )
            result = await domhand_record_expected_value(
                DomHandRecordExpectedValueParams(
                    field_label=field_label,
                    expected_value=desired_value,
                    field_id=field_id,
                    field_type=field_type,
                    target_section=target_section,
                ),
                self.browser_session,
            )
            if result.error:
                self.logger.info(f"Runtime follow-up domhand_record_expected_value skipped: {result.error}")
                return None
            return result
        except Exception as exc:
            self.logger.warning(f"Runtime follow-up domhand_record_expected_value failed: {type(exc).__name__}: {exc}")
            return None

    async def _maybe_run_runtime_followups(
        self,
        *,
        action_name: str,
        result: ActionResult,
        pre_action_url: str,
        pre_action_state: dict[str, Any] | None,
    ) -> list[ActionResult]:
        if not settings.enable_domhand:
            return []
        if self.browser_session is None or result.error or result.is_done:
            return []

        try:
            current_url = await self.browser_session.get_current_page_url()
        except Exception:
            current_url = ""

        followups: list[ActionResult] = []
        single_active_blocker = pre_action_state.get("single_active_blocker") if isinstance(pre_action_state, dict) else None
        recovery_target = pre_action_state.get("recovery_target") if isinstance(pre_action_state, dict) else None
        primary_active_blocker = pre_action_state.get("primary_active_blocker") if isinstance(pre_action_state, dict) else None
        if (
            _is_manual_recovery_action(action_name)
            and isinstance(pre_action_state, dict)
            and (
                (isinstance(single_active_blocker, dict) and isinstance(recovery_target, dict))
                or isinstance(primary_active_blocker, dict)
            )
        ):
            state_url = str(pre_action_state.get("page_url") or "").strip()
            if not state_url or not current_url or current_url == state_url:
                tracked_blocker = cast(dict[str, Any], recovery_target if isinstance(recovery_target, dict) else primary_active_blocker)
                if action_name == "input" and isinstance(recovery_target, dict) and _manual_input_should_wait_for_selection(recovery_target, result):
                    return followups
                if action_name == "input" and _blocker_prefers_selection_commit(tracked_blocker):
                    return followups
                if action_name == "input":
                    try:
                        tab_event = self.browser_session.event_bus.dispatch(SendKeysEvent(keys="Tab"))
                        await tab_event
                        await tab_event.event_result(raise_if_any=True, raise_if_none=False)
                    except Exception as exc:
                        self.logger.debug(f"Runtime follow-up blur skipped after input: {type(exc).__name__}: {exc}")
                if isinstance(recovery_target, dict):
                    record_result = await self._maybe_record_active_recovery_target(recovery_target)
                    if record_result is not None:
                        followups.append(record_result)
                await asyncio.sleep(0.2)
                assess_result = await self._run_runtime_assess_state_followup(reason=f"manual_{action_name}")
                if assess_result is not None:
                    followups.append(assess_result)
                return followups

        if _result_requests_immediate_assessment(result) and (not current_url or current_url == pre_action_url):
            assess_result = await self._run_runtime_assess_state_followup(reason=action_name)
            if assess_result is not None:
                followups.append(assess_result)
        return followups

    async def multi_act(self, actions: list[Any], check_for_new_elements: bool = True) -> list[ActionResult]:
        results: list[ActionResult] = []

        if self.browser_session is None:
            raise ValueError("No browser session")

        if self.browser_session.browser_profile.wait_between_actions:
            await asyncio.sleep(self.browser_session.browser_profile.wait_between_actions)

        total_actions = len(actions)
        for i, action in enumerate(actions):
            action_name = list(action.model_dump(exclude_unset=True).keys())[0]
            action_data = action.model_dump(exclude_unset=True)

            if i > 0 and action_data.get("done") is not None:
                self.logger.debug(f"Done action is allowed only as a single action - stopped after action {i} / {total_actions}.")
                break

            if i > 0:
                self.logger.debug(f"Waiting {self.browser_profile.wait_between_actions} seconds between actions")
                await asyncio.sleep(self.browser_profile.wait_between_actions)

            try:
                await self._check_stop_or_pause()
                await self._log_action(action, action_name, i + 1, total_actions)

                pre_action_url = await self.browser_session.get_current_page_url()
                pre_action_focus = self.browser_session.agent_focus_target_id
                pre_action_state = getattr(self.browser_session, "_gh_last_application_state", None)

                result = await self.tools.act(
                    action=action,
                    browser_session=self.browser_session,
                    file_system=self.file_system,
                    page_extraction_llm=self.settings.page_extraction_llm,
                    sensitive_data=self.sensitive_data,
                    available_file_paths=self.available_file_paths,
                    extraction_schema=self.extraction_schema,
                )

                if result.error:
                    await self._demo_mode_log(
                        f'Action "{action_name}" failed: {result.error}',
                        "error",
                        {"action": action_name, "step": self.state.n_steps},
                    )
                elif result.is_done:
                    completion_text = result.long_term_memory or result.extracted_content or "Task marked as done."
                    level = "success" if result.success is not False else "warning"
                    await self._demo_mode_log(
                        completion_text,
                        level,
                        {"action": action_name, "step": self.state.n_steps},
                )

                results.append(result)
                self._update_pending_manual_recovery_after_action(
                    action_name=action_name,
                    params=action_data.get(action_name, {}) if isinstance(action_data.get(action_name, {}), dict) else {},
                    result=result,
                )

                followup_results = await self._maybe_run_runtime_followups(
                    action_name=action_name,
                    result=result,
                    pre_action_url=pre_action_url,
                    pre_action_state=pre_action_state if isinstance(pre_action_state, dict) else None,
                )
                if followup_results:
                    results.extend(followup_results)
                    break

                if results[-1].is_done or results[-1].error or i == total_actions - 1:
                    break

                registered_action = self.tools.registry.registry.actions.get(action_name)
                if registered_action and registered_action.terminates_sequence:
                    self.logger.info(
                        f'Action "{action_name}" terminates sequence — skipping {total_actions - i - 1} remaining action(s)'
                    )
                    break

                post_action_url = await self.browser_session.get_current_page_url()
                post_action_focus = self.browser_session.agent_focus_target_id

                if post_action_url != pre_action_url or post_action_focus != pre_action_focus:
                    self.logger.info(f'Page changed after "{action_name}" — skipping {total_actions - i - 1} remaining action(s)')
                    break

            except Exception as e:
                self.logger.error(f"❌ Executing action {i + 1} failed -> {type(e).__name__}: {e}")
                await self._demo_mode_log(
                    f'Action "{action_name}" raised {type(e).__name__}: {e}',
                    "error",
                    {"action": action_name, "step": self.state.n_steps},
                )
                raise e

        return results

    async def _judge_and_log(self) -> None:
        judgement = await self._judge_trace()

        if self.history.history[-1].result[-1].is_done:
            last_result = self.history.history[-1].result[-1]
            last_result.judgement = judgement
            self_reported_success = last_result.success

            if judgement:
                if self_reported_success is True and judgement.verdict is True:
                    return

                judge_log = "\n"
                if self_reported_success is True and judgement.verdict is False:
                    judge_log += "⚠️  \033[33mAgent reported success but judge thinks task failed\033[0m\n"

                verdict_color = "\033[32m" if judgement.verdict else "\033[31m"
                verdict_text = "✅ PASS" if judgement.verdict else "❌ FAIL"
                judge_log += f"⚖️  {verdict_color}Judge Verdict: {verdict_text}\033[0m\n"
                if judgement.failure_reason:
                    judge_log += f"   Failure: {_truncate_log_text(judgement.failure_reason)}\n"
                if judgement.reached_captcha:
                    judge_log += "   🤖 Captcha Detected: Agent encountered captcha challenges\n"
                    judge_log += "   👉 🥷 Use Browser Use Cloud for the most stealth browser infra: https://docs.browser-use.com/customize/browser/remote\n"
                if judgement.reasoning:
                    judge_log += f"   Evidence: {_truncate_log_text(judgement.reasoning)}\n"
                self.logger.info(judge_log)
