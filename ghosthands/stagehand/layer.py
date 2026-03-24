"""StagehandLayer — thin async adapter over the Stagehand Python SDK.

Lifecycle:
    1. ``get_stagehand_layer()`` returns the module-level singleton.
    2. ``await layer.ensure_started(cdp_url)`` lazily initializes on first call.
    3. ``act()`` / ``observe()`` / ``extract()`` delegate to the Stagehand session.
    4. ``await layer.stop()`` detaches without closing the browser.

The layer never owns the browser process — browser-use does.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_MODEL = "anthropic/claude-haiku-4-5-20251001"
_ACT_TIMEOUT_MS = 30_000
_DOM_SETTLE_TIMEOUT_MS = 3_000


@dataclass
class ActResult:
    success: bool
    message: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ObservedElement:
    selector: str
    description: str
    method: str = ""
    arguments: list[str] = field(default_factory=list)
    backend_node_id: int | None = None


class StagehandLayer:
    """Manages one Stagehand session attached to a browser-use CDP browser."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._session_id: str | None = None
        self._started = False
        self._start_lock = asyncio.Lock()
        self._cdp_url: str | None = None

    @property
    def is_available(self) -> bool:
        return self._started and self._session_id is not None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def ensure_started(self, cdp_url: str, *, model: str | None = None) -> bool:
        """Lazily initialize Stagehand, connecting to the existing browser via CDP.

        Returns True if Stagehand is ready, False if initialization failed.
        Safe to call multiple times — only the first call initializes.
        """
        if self._started:
            return self.is_available

        async with self._start_lock:
            if self._started:
                return self.is_available
            return await self._do_start(cdp_url, model=model)

    async def _do_start(self, cdp_url: str, *, model: str | None = None) -> bool:
        self._started = True
        try:
            from stagehand import AsyncStagehand

            model_name = model or os.environ.get("GH_STAGEHAND_MODEL", _DEFAULT_MODEL)
            api_key = os.environ.get("BROWSERBASE_API_KEY") or os.environ.get("STAGEHAND_API_KEY", "")
            project_id = os.environ.get("BROWSERBASE_PROJECT_ID", "")

            self._client = AsyncStagehand(
                api_key=api_key or "local",
                project_id=project_id or "local",
            )

            response = await self._client.sessions.start(
                model_name=model_name,
                browser={
                    "type": "local",
                    "cdp_url": cdp_url,
                },
                verbose=0,
                dom_settle_timeout_ms=_DOM_SETTLE_TIMEOUT_MS,
                act_timeout_ms=_ACT_TIMEOUT_MS,
            )

            if response.success and response.data:
                self._session_id = response.data.session_id
                self._cdp_url = cdp_url
                logger.info(
                    "stagehand.started",
                    session_id=self._session_id,
                    cdp_url=cdp_url[:60],
                    model=model_name,
                )
                return True

            logger.warning("stagehand.start_failed", response_success=response.success)
            return False

        except Exception as exc:
            logger.warning("stagehand.start_error", error=str(exc), cdp_url=cdp_url[:60])
            self._client = None
            self._session_id = None
            return False

    async def stop(self) -> None:
        """Detach Stagehand session without closing the browser."""
        if self._client and self._session_id:
            try:
                await self._client.sessions.end(self._session_id)
                logger.info("stagehand.stopped", session_id=self._session_id)
            except Exception as exc:
                logger.debug("stagehand.stop_error", error=str(exc))
            finally:
                try:
                    await self._client.close()
                except Exception:
                    pass
        self._client = None
        self._session_id = None
        self._started = False

    async def act(self, instruction: str, *, timeout: float | None = None) -> ActResult:
        """Execute a semantic action on the page.

        Examples:
            act("Select 'United States' in the Country dropdown")
            act("Fill the 'First Name' field with 'John'")
        """
        if not self.is_available:
            return ActResult(success=False, message="Stagehand not available")

        try:
            response = await self._client.sessions.act(
                self._session_id,
                input=instruction,
                timeout=timeout or (_ACT_TIMEOUT_MS / 1000),
            )
            if response.success and response.data and response.data.result:
                result = response.data.result
                return ActResult(
                    success=bool(result.success),
                    message=result.message or "",
                    actions=[
                        {"description": a.action_description if hasattr(a, "action_description") else str(a)}
                        for a in (result.actions or [])
                    ],
                )
            return ActResult(success=False, message="No result from Stagehand act()")

        except Exception as exc:
            logger.warning("stagehand.act_error", instruction=instruction[:120], error=str(exc))
            return ActResult(success=False, message=f"Stagehand act() error: {exc}")

    async def observe(self, instruction: str | None = None) -> list[ObservedElement]:
        """Observe interactive elements on the page.

        Returns a list of discovered elements with selectors and descriptions.
        """
        if not self.is_available:
            return []

        try:
            response = await self._client.sessions.observe(
                self._session_id,
                instruction=instruction or "Find all interactive form fields, buttons, and inputs on this page",
            )
            if response.success and response.data and response.data.result:
                return [
                    ObservedElement(
                        selector=el.selector or "",
                        description=el.description or "",
                        method=el.method or "",
                        arguments=list(el.arguments or []),
                        backend_node_id=el.backend_node_id,
                    )
                    for el in response.data.result
                ]
            return []

        except Exception as exc:
            logger.warning("stagehand.observe_error", instruction=(instruction or "")[:120], error=str(exc))
            return []

    async def extract(self, instruction: str, schema: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Extract structured data from the page using an LLM."""
        if not self.is_available:
            return None

        try:
            kwargs: dict[str, Any] = {"instruction": instruction}
            if schema:
                kwargs["schema"] = schema
            response = await self._client.sessions.extract(self._session_id, **kwargs)
            if response.success and response.data:
                return response.data.result if hasattr(response.data, "result") else None
            return None

        except Exception as exc:
            logger.warning("stagehand.extract_error", instruction=instruction[:120], error=str(exc))
            return None

    async def navigate(self, url: str) -> bool:
        """Navigate the Stagehand session to a URL."""
        if not self.is_available:
            return False

        try:
            response = await self._client.sessions.navigate(self._session_id, url=url)
            return bool(response.success)
        except Exception as exc:
            logger.warning("stagehand.navigate_error", url=url[:120], error=str(exc))
            return False


_singleton: StagehandLayer | None = None
_singleton_lock = asyncio.Lock()


def get_stagehand_layer() -> StagehandLayer:
    """Return the module-level StagehandLayer singleton.

    Thread-safe but not async-safe for creation — call from the event loop.
    """
    global _singleton
    if _singleton is None:
        _singleton = StagehandLayer()
    return _singleton


def reset_stagehand_layer() -> None:
    """Reset the singleton (for testing)."""
    global _singleton
    _singleton = None
