"""Unified cost summary helpers for browser-use + DomHand/Stagehand."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

_STAGEHAND_USAGE_ATTR = "_gh_stagehand_usage"


def mark_stagehand_usage(browser_session: Any, *, source: str) -> None:
    """Mark that Stagehand was used on this browser session and count calls."""
    if browser_session is None:
        return
    raw = getattr(browser_session, _STAGEHAND_USAGE_ATTR, None)
    if isinstance(raw, dict):
        state = {
            "used": bool(raw.get("used")),
            "sources": list(raw.get("sources") or []),
            "calls": int(raw.get("calls") or 0),
        }
    else:
        state = {"used": False, "sources": [], "calls": 0}
    state["used"] = True
    state["calls"] += 1
    cleaned_source = str(source or "").strip()
    if cleaned_source and cleaned_source not in state["sources"]:
        state["sources"].append(cleaned_source)
    setattr(browser_session, _STAGEHAND_USAGE_ATTR, state)


def get_stagehand_usage(browser_session: Any) -> dict[str, Any]:
    """Return Stagehand usage metadata for a browser session."""
    raw = getattr(browser_session, _STAGEHAND_USAGE_ATTR, None)
    if not isinstance(raw, dict):
        return {"used": False, "sources": [], "calls": 0}
    return {
        "used": bool(raw.get("used")),
        "sources": [str(source) for source in (raw.get("sources") or []) if str(source).strip()],
        "calls": int(raw.get("calls") or 0),
    }


@dataclass(frozen=True)
class UnifiedCostSummary:
    """Single source of truth for tracked and untracked LLM spend.

    Three subsystems:
    - browser_use: planner LLM (browser-use TokenCostService)
    - domhand: DOM-first form-fill LLM calls (DomHand action metadata)
    - stagehand: semantic action layer (SDK doesn't expose token/cost data)
    """

    browser_use_cost_usd: float = 0.0
    browser_use_prompt_tokens: int = 0
    browser_use_completion_tokens: int = 0
    domhand_cost_usd: float = 0.0
    domhand_prompt_tokens: int = 0
    domhand_completion_tokens: int = 0
    domhand_llm_calls: int = 0
    stagehand_used: bool = False
    stagehand_calls: int = 0
    stagehand_sources: tuple[str, ...] = ()
    total_tracked_cost_usd: float = 0.0
    total_tracked_prompt_tokens: int = 0
    total_tracked_completion_tokens: int = 0
    total_tracked_tokens: int = 0
    untracked_cost_possible: bool = False
    untracked_reasons: tuple[str, ...] = ()
    domhand_models: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "browser_use_cost_usd": round(self.browser_use_cost_usd, 6),
            "browser_use_prompt_tokens": self.browser_use_prompt_tokens,
            "browser_use_completion_tokens": self.browser_use_completion_tokens,
            "domhand_cost_usd": round(self.domhand_cost_usd, 6),
            "domhand_prompt_tokens": self.domhand_prompt_tokens,
            "domhand_completion_tokens": self.domhand_completion_tokens,
            "domhand_llm_calls": self.domhand_llm_calls,
            "stagehand_used": self.stagehand_used,
            "stagehand_calls": self.stagehand_calls,
            "stagehand_sources": list(self.stagehand_sources),
            "total_tracked_cost_usd": round(self.total_tracked_cost_usd, 6),
            "total_tracked_prompt_tokens": self.total_tracked_prompt_tokens,
            "total_tracked_completion_tokens": self.total_tracked_completion_tokens,
            "total_tracked_tokens": self.total_tracked_tokens,
            "untracked_cost_possible": self.untracked_cost_possible,
            "untracked_reasons": list(self.untracked_reasons),
            "domhand_models": list(self.domhand_models),
        }


def build_cost_summary(
    *,
    browser_use_cost_usd: float = 0.0,
    browser_use_prompt_tokens: int = 0,
    browser_use_completion_tokens: int = 0,
    domhand_cost_usd: float = 0.0,
    domhand_prompt_tokens: int = 0,
    domhand_completion_tokens: int = 0,
    domhand_llm_calls: int = 0,
    domhand_models: list[str] | tuple[str, ...] | None = None,
    stagehand_used: bool = False,
    stagehand_calls: int = 0,
    stagehand_sources: list[str] | tuple[str, ...] | None = None,
) -> UnifiedCostSummary:
    """Build a normalized cost summary from tracked components."""
    cleaned_domhand_models = tuple(
        sorted(
            {
                str(model).strip()
                for model in (domhand_models or [])
                if str(model).strip()
            }
        )
    )
    cleaned_stagehand_sources = tuple(
        sorted(
            {
                str(source).strip()
                for source in (stagehand_sources or [])
                if str(source).strip()
            }
        )
    )
    untracked_reasons: list[str] = []
    if stagehand_used:
        untracked_reasons.append("stagehand_cost_not_tracked")

    total_tracked_cost = float(browser_use_cost_usd or 0.0) + float(domhand_cost_usd or 0.0)
    total_prompt_tokens = int(browser_use_prompt_tokens or 0) + int(domhand_prompt_tokens or 0)
    total_completion_tokens = int(browser_use_completion_tokens or 0) + int(domhand_completion_tokens or 0)

    return UnifiedCostSummary(
        browser_use_cost_usd=float(browser_use_cost_usd or 0.0),
        browser_use_prompt_tokens=int(browser_use_prompt_tokens or 0),
        browser_use_completion_tokens=int(browser_use_completion_tokens or 0),
        domhand_cost_usd=float(domhand_cost_usd or 0.0),
        domhand_prompt_tokens=int(domhand_prompt_tokens or 0),
        domhand_completion_tokens=int(domhand_completion_tokens or 0),
        domhand_llm_calls=int(domhand_llm_calls or 0),
        stagehand_used=bool(stagehand_used),
        stagehand_calls=int(stagehand_calls or 0),
        stagehand_sources=cleaned_stagehand_sources,
        total_tracked_cost_usd=total_tracked_cost,
        total_tracked_prompt_tokens=total_prompt_tokens,
        total_tracked_completion_tokens=total_completion_tokens,
        total_tracked_tokens=total_prompt_tokens + total_completion_tokens,
        untracked_cost_possible=bool(untracked_reasons),
        untracked_reasons=tuple(untracked_reasons),
        domhand_models=cleaned_domhand_models,
    )


def summarize_history_cost(history: Any, browser_session: Any = None) -> dict[str, Any]:
    """Build a unified cost summary from an AgentHistoryList-like object."""
    usage = getattr(history, "usage", None)
    browser_use_cost = float(getattr(usage, "total_cost", 0.0) or 0.0)
    browser_use_prompt_tokens = int(getattr(usage, "total_prompt_tokens", 0) or 0)
    browser_use_completion_tokens = int(getattr(usage, "total_completion_tokens", 0) or 0)

    domhand_cost = 0.0
    domhand_prompt_tokens = 0
    domhand_completion_tokens = 0
    domhand_llm_calls = 0
    domhand_models: set[str] = set()

    for entry in getattr(history, "history", []) or []:
        for result in getattr(entry, "result", []) or []:
            metadata = getattr(result, "metadata", None) or {}
            if "step_cost" not in metadata:
                continue
            with contextlib.suppress(TypeError, ValueError):
                domhand_cost += float(metadata.get("step_cost") or 0.0)
            if metadata.get("input_tokens") is not None:
                domhand_prompt_tokens += int(metadata["input_tokens"])
            if metadata.get("output_tokens") is not None:
                domhand_completion_tokens += int(metadata["output_tokens"])
            if metadata.get("domhand_llm_calls") is not None:
                domhand_llm_calls += int(metadata["domhand_llm_calls"])
            if metadata.get("model"):
                domhand_models.add(str(metadata["model"]).strip())

    stagehand_usage = get_stagehand_usage(browser_session)
    return build_cost_summary(
        browser_use_cost_usd=browser_use_cost,
        browser_use_prompt_tokens=browser_use_prompt_tokens,
        browser_use_completion_tokens=browser_use_completion_tokens,
        domhand_cost_usd=domhand_cost,
        domhand_prompt_tokens=domhand_prompt_tokens,
        domhand_completion_tokens=domhand_completion_tokens,
        domhand_llm_calls=domhand_llm_calls,
        domhand_models=sorted(domhand_models),
        stagehand_used=bool(stagehand_usage["used"]),
        stagehand_calls=int(stagehand_usage["calls"]),
        stagehand_sources=stagehand_usage["sources"],
    ).to_dict()
