"""Structured step tracing and blocker-attempt state for Hand-X runs."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from ghosthands.config.settings import settings

logger = logging.getLogger(__name__)

try:
    from redis.asyncio import from_url as redis_from_url
except ImportError:  # pragma: no cover - optional dependency at runtime
    redis_from_url = None


@dataclass
class StepTracePublisher:
    """Small Redis Streams publisher used for per-step replay/debug tracing."""

    redis_url: str
    maxlen: int
    ttl_seconds: int
    _client: Any | None = None
    _disabled: bool = False

    async def publish(self, job_id: str, event_type: str, payload: dict[str, Any]) -> None:
        if self._disabled or not job_id:
            return
        if redis_from_url is None:
            self._disabled = True
            logger.warning("step_trace.redis_not_installed")
            return
        if self._client is None:
            self._client = redis_from_url(self.redis_url, decode_responses=True)

        stream_key = f"gh:job:{job_id}:steps"
        body = {
            "event_type": event_type,
            "job_id": job_id,
            "ts": time.time(),
            **payload,
        }
        try:
            await self._client.xadd(
                stream_key,
                {
                    "event_type": event_type,
                    "job_id": job_id,
                    "payload": json.dumps(body, ensure_ascii=False, default=str),
                },
                maxlen=max(self.maxlen, 100),
                approximate=True,
            )
            if self.ttl_seconds > 0:
                await self._client.expire(stream_key, self.ttl_seconds)
        except Exception as exc:  # pragma: no cover - network/runtime safety
            logger.warning(
                "step_trace.publish_failed",
                extra={"job_id": job_id, "event_type": event_type, "error": str(exc)},
            )


_publisher_singleton: StepTracePublisher | None = None


def get_step_trace_publisher() -> StepTracePublisher | None:
    """Return the configured step trace publisher, or None when disabled."""
    global _publisher_singleton
    if not settings.step_trace_enabled or not settings.step_trace_redis_url:
        return None
    if _publisher_singleton is None:
        _publisher_singleton = StepTracePublisher(
            redis_url=settings.step_trace_redis_url,
            maxlen=settings.step_trace_maxlen,
            ttl_seconds=settings.step_trace_ttl_seconds,
        )
    return _publisher_singleton


def attach_step_trace_context(browser_session: Any, *, job_id: str) -> None:
    """Attach trace context to the browser session for action-level publishers."""
    if browser_session is None or not job_id:
        return
    publisher = get_step_trace_publisher()
    if publisher is None:
        return
    setattr(browser_session, "_gh_step_trace_publisher", publisher)
    setattr(browser_session, "_gh_step_trace_job_id", job_id)


async def publish_browser_session_trace(browser_session: Any, event_type: str, payload: dict[str, Any]) -> None:
    """Publish a trace event using the publisher attached to the browser session."""
    if browser_session is None:
        return
    publisher = getattr(browser_session, "_gh_step_trace_publisher", None)
    job_id = getattr(browser_session, "_gh_step_trace_job_id", "")
    if publisher is None or not job_id:
        return
    await publisher.publish(job_id=job_id, event_type=event_type, payload=payload)


def update_blocker_attempt_state(
    browser_session: Any,
    *,
    field_key: str,
    field_id: str = "",
    strategy: str,
    desired_value: str = "",
    observed_value: str = "",
    visible_error: str = "",
    retry_capped: bool = False,
    success: bool = False,
    state_change: str = "",
    recommended_next_action: str = "",
) -> None:
    """Record the latest recovery attempt for a blocker field on the session."""
    if browser_session is None or not field_key:
        return
    attempts = getattr(browser_session, "_gh_blocker_attempt_state", None)
    if not isinstance(attempts, dict):
        attempts = {}
    attempts[field_key] = {
        "field_id": field_id,
        "last_attempt_strategy": strategy,
        "desired_value": desired_value,
        "last_observed_value": observed_value,
        "last_visible_error": visible_error,
        "last_state_change": state_change,
        "retry_capped": bool(retry_capped),
        "success": bool(success),
        "recommended_next_action": recommended_next_action,
        "updated_at": time.time(),
    }
    setattr(browser_session, "_gh_blocker_attempt_state", attempts)


def get_blocker_attempt_state(browser_session: Any) -> dict[str, dict[str, Any]]:
    """Return the latest blocker-attempt state keyed by stable field key."""
    attempts = getattr(browser_session, "_gh_blocker_attempt_state", None)
    return attempts if isinstance(attempts, dict) else {}
