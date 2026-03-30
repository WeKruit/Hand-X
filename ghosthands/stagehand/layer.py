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
import contextlib
import os
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Provider IDs for Stagehand ``sessions.start(model_name=...)`` (override with GH_STAGEHAND_MODEL).
_DEFAULT_MODEL = "anthropic/claude-haiku-4-5-20251001"  # Haiku 4.5
_GOOGLE_STAGEHAND_MODEL = "google/gemini-3-flash-preview"
_OPENAI_STAGEHAND_MODEL = "openai/gpt-5.4-nano"
_ACT_TIMEOUT_MS = 30_000
_DOM_SETTLE_TIMEOUT_MS = 3_000


def _infer_stagehand_model(model_api_key: str) -> str:
    """Align ``model_name`` with the provider for ``model_api_key`` (local server calls that API).

    If ``MODEL_API_KEY`` is a Google key but ``model_name`` defaults to Anthropic, ``act()`` returns 401.
    """
    k = (model_api_key or "").strip()
    if not k:
        return _DEFAULT_MODEL

    g = (os.environ.get("GOOGLE_API_KEY") or "").strip()
    if g and k == g:
        return _GOOGLE_STAGEHAND_MODEL

    for env_name in ("OPENAI_API_KEY", "GH_OPENAI_API_KEY"):
        v = (os.environ.get(env_name) or "").strip()
        if v and k == v:
            return _OPENAI_STAGEHAND_MODEL

    for env_name in ("ANTHROPIC_API_KEY", "GH_ANTHROPIC_API_KEY"):
        v = (os.environ.get(env_name) or "").strip()
        if v and k == v:
            return _DEFAULT_MODEL

    if k.startswith("AIza"):
        return _GOOGLE_STAGEHAND_MODEL
    if k.startswith("sk-ant"):
        return _DEFAULT_MODEL
    if k.startswith("sk-proj") or (k.startswith("sk-") and not k.startswith("sk-ant")):
        return _OPENAI_STAGEHAND_MODEL

    return _DEFAULT_MODEL


def _stagehand_disabled() -> bool:
    v = (os.environ.get("GH_STAGEHAND_DISABLE") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _resolve_model_api_key() -> str | None:
    """Key sent as ``x-model-api-key`` to the Stagehand API (see ``AsyncStagehand``).

    Order matches apply.sh: explicit ``MODEL_API_KEY``, then Anthropic (DomHand default),
    then OpenAI, then Google. Falls back to ``GH_LLM_RUNTIME_GRANT`` (VALET proxy token)
    so the desktop DMG can use stagehand via the VALET OpenAI-compatible proxy.
    """
    for key in (
        os.environ.get("MODEL_API_KEY"),
        os.environ.get("ANTHROPIC_API_KEY"),
        os.environ.get("GH_ANTHROPIC_API_KEY"),
        os.environ.get("OPENAI_API_KEY"),
        os.environ.get("GH_OPENAI_API_KEY"),
        os.environ.get("GOOGLE_API_KEY"),
        os.environ.get("GH_LLM_RUNTIME_GRANT"),
    ):
        if key and key.strip():
            return key.strip()
    return None


def _resolve_local_proxy_config() -> tuple[int | None, str | None]:
    """Return (local_proxy_port, runtime_grant) for the desktop stagehand proxy shim.

    Desktop spawns a local HTTP proxy that forwards OpenAI-format requests to VALET.
    ``GH_STAGEHAND_LOCAL_PROXY_PORT`` is the localhost port.
    ``GH_LLM_RUNTIME_GRANT`` is the auth token (passed as Bearer to the local proxy).
    """
    port_str = (os.environ.get("GH_STAGEHAND_LOCAL_PROXY_PORT") or "").strip()
    grant = (os.environ.get("GH_LLM_RUNTIME_GRANT") or "").strip()
    if port_str and grant:
        try:
            return int(port_str), grant
        except ValueError:
            return None, None
    return None, None


def _stagehand_server_mode() -> str:
    """``remote`` = api.stagehand.browserbase.com (needs ``BROWSERBASE_API_KEY``).

    ``local`` = embedded Stagehand server on localhost (no Browserbase headers).
    Override with ``GH_STAGEHAND_SERVER=local`` or ``remote``.
    """
    override = (os.environ.get("GH_STAGEHAND_SERVER") or "").strip().lower()
    if override in ("local", "remote"):
        return override
    if (os.environ.get("BROWSERBASE_API_KEY") or "").strip():
        return "remote"
    return "local"


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
        self._session: Any | None = None
        self._session_id: str | None = None
        self._started = False
        self._start_lock = asyncio.Lock()
        self._cdp_url: str | None = None

    @property
    def is_available(self) -> bool:
        return self._started and self._session is not None and self._session_id is not None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def ensure_started(self, cdp_url: str, *, model: str | None = None) -> bool:
        """Lazily initialize Stagehand, connecting to the existing browser via CDP.

        Returns True if Stagehand is ready, False if initialization failed.
        Safe to call multiple times — only the first successful call keeps the session.
        """
        if self._started and self.is_available:
            return True

        async with self._start_lock:
            if self._started and self.is_available:
                return True
            return await self._do_start(cdp_url, model=model)

    async def _do_start(self, cdp_url: str, *, model: str | None = None) -> bool:
        if _stagehand_disabled():
            logger.info("stagehand.disabled", reason="GH_STAGEHAND_DISABLE")
            return False

        # Local SEA binary logs every Fastify request as JSON to stdout — suppress unless
        # GH_STAGEHAND_SEA_LOGS=1 (see ghosthands.stagehand.sea_process_quiet).
        from ghosthands.stagehand.sea_process_quiet import install_sea_process_quiet

        install_sea_process_quiet()

        # Check for local proxy shim (desktop mode — SEA talks to localhost, which
        # forwards to VALET). This is the primary path for desktop DMG builds.
        local_proxy_port, proxy_grant = _resolve_local_proxy_config()

        model_api_key = _resolve_model_api_key()
        if not model_api_key and not local_proxy_port:
            logger.warning(
                "stagehand.start_skipped",
                reason="missing_model_api_key",
                hint="Set MODEL_API_KEY or OPENAI_API_KEY / GH_OPENAI_API_KEY / ANTHROPIC / GOOGLE_API_KEY, or GH_STAGEHAND_DISABLE=1",
            )
            return False

        try:
            from stagehand import AsyncStagehand

            explicit = (os.environ.get("GH_STAGEHAND_MODEL") or "").strip()
            mode = _stagehand_server_mode()
            bb_key = (os.environ.get("BROWSERBASE_API_KEY") or "").strip() or None
            bb_proj = (os.environ.get("BROWSERBASE_PROJECT_ID") or "").strip() or None

            # Remote cloud API requires x-bb-api-key; without BB keys, use local server.
            if mode == "remote" and not bb_key:
                logger.warning(
                    "stagehand.fallback_local",
                    reason="GH_STAGEHAND_SERVER=remote but BROWSERBASE_API_KEY missing",
                )
                mode = "local"

            if local_proxy_port and proxy_grant:
                # Desktop mode: SEA server inherits OPENAI_BASE_URL and OPENAI_API_KEY
                # from os.environ, routing all LLM calls through the local proxy shim.
                os.environ["OPENAI_BASE_URL"] = f"http://127.0.0.1:{local_proxy_port}/v1"
                os.environ["OPENAI_API_KEY"] = proxy_grant
                model_name = model or (explicit if explicit else _OPENAI_STAGEHAND_MODEL)
                self._client = AsyncStagehand(
                    model_api_key=proxy_grant,
                    server="local",
                    local_openai_api_key=proxy_grant,
                )
                logger.info("stagehand.local_proxy", port=local_proxy_port, model=model_name)
            elif mode == "remote":
                model_name = model or (explicit if explicit else _infer_stagehand_model(model_api_key or ""))
                self._client = AsyncStagehand(
                    model_api_key=model_api_key,
                    browserbase_api_key=bb_key,
                    browserbase_project_id=bb_proj,
                )
            else:
                model_name = model or (explicit if explicit else _infer_stagehand_model(model_api_key or ""))
                self._client = AsyncStagehand(
                    model_api_key=model_api_key,
                    server="local",
                    local_openai_api_key=model_api_key,
                )

            logger.debug("stagehand.client_mode", mode=mode, model=model_name[:60])

            session = await self._client.sessions.start(
                model_name=model_name,
                browser={
                    "type": "local",
                    "cdp_url": cdp_url,
                },
                verbose=0,
                dom_settle_timeout_ms=_DOM_SETTLE_TIMEOUT_MS,
                act_timeout_ms=_ACT_TIMEOUT_MS,
            )

            if session.success and getattr(session, "id", None):
                self._session = session
                self._session_id = session.id
                self._cdp_url = cdp_url
                self._started = True
                logger.info(
                    "stagehand.started",
                    session_id=self._session_id,
                    cdp_url=cdp_url[:60],
                    model=model_name,
                )
                return True

            logger.warning("stagehand.start_failed", session_success=getattr(session, "success", None))
            with contextlib.suppress(Exception):
                await session.end()
            await self._close_client_only()
            return False

        except Exception as exc:
            logger.warning("stagehand.start_error", error=str(exc), cdp_url=cdp_url[:60])
            self._session = None
            self._session_id = None
            await self._close_client_only()
            return False

    async def _close_client_only(self) -> None:
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            await self._client.close()
        self._client = None

    async def stop(self) -> None:
        """Detach Stagehand session without closing the browser."""
        if self._session is not None:
            try:
                await self._session.end()
                logger.info("stagehand.stopped", session_id=self._session_id)
            except Exception as exc:
                logger.debug("stagehand.stop_session_error", error=str(exc))
        self._session = None
        self._session_id = None
        self._started = False
        await self._close_client_only()

    async def act(self, instruction: str, *, timeout: float | None = None) -> ActResult:
        """Execute a semantic action on the page.

        Examples:
            act("Select 'United States' in the Country dropdown")
            act("Fill the 'First Name' field with 'John'")
        """
        if not self.is_available or self._session is None:
            return ActResult(success=False, message="Stagehand not available")

        try:
            kwargs: dict[str, Any] = {"input": instruction}
            if timeout is not None:
                kwargs["timeout"] = timeout
            response = await self._session.act(**kwargs)
            if response.success and response.data and response.data.result:
                result = response.data.result
                actions_out: list[dict[str, Any]] = []
                for a in result.actions or []:
                    desc = getattr(a, "description", None) or str(a)
                    actions_out.append({"description": desc})
                msg = (getattr(result, "message", None) or "").strip()
                if not msg:
                    msg = (getattr(result, "action_description", None) or "").strip()
                return ActResult(
                    success=bool(result.success),
                    message=msg,
                    actions=actions_out,
                )
            return ActResult(success=False, message="No result from Stagehand act()")

        except Exception as exc:
            logger.debug(
                "stagehand.act_error",
                instruction=instruction[:120],
                error=str(exc)[:200],
            )
            return ActResult(success=False, message=f"Stagehand act() error: {exc}")

    async def observe(self, instruction: str | None = None) -> list[ObservedElement]:
        """Observe interactive elements on the page.

        Returns a list of discovered elements with selectors and descriptions.
        """
        if not self.is_available or self._session is None:
            return []

        try:
            response = await self._session.observe(
                instruction=instruction or "Find all interactive form fields, buttons, and inputs on this page",
            )
            if response.success and response.data and response.data.result:
                return [
                    ObservedElement(
                        selector=el.selector or "",
                        description=el.description or "",
                        method=el.method or "",
                        arguments=list(el.arguments or []),
                        backend_node_id=int(el.backend_node_id) if el.backend_node_id is not None else None,
                    )
                    for el in response.data.result
                ]
            return []

        except Exception as exc:
            logger.warning("stagehand.observe_error", instruction=(instruction or "")[:120], error=str(exc))
            return []

    async def extract(self, instruction: str, schema: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Extract structured data from the page using an LLM."""
        if not self.is_available or self._session is None:
            return None

        try:
            kwargs: dict[str, Any] = {"instruction": instruction}
            if schema:
                kwargs["schema"] = schema
            response = await self._session.extract(**kwargs)
            if response.success and response.data:
                return response.data.result if hasattr(response.data, "result") else None
            return None

        except Exception as exc:
            logger.warning("stagehand.extract_error", instruction=instruction[:120], error=str(exc))
            return None

    async def navigate(self, url: str) -> bool:
        """Navigate the Stagehand session to a URL."""
        if not self.is_available or self._session is None:
            return False

        try:
            response = await self._session.navigate(url=url)
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
