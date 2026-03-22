"""Agent factory — creates a fully-configured browser-use Agent for job applications.

This is the main entry point for spawning an agent.  The factory wires
together:

- An LLM via ``get_chat_model()`` (routes through VALET proxy if configured)
- A ``BrowserProfile`` with headless mode and domain lockdown
- The GhostHands system-prompt extension (platform guardrails + profile)
- DomHand custom actions registered on the ``Tools`` controller
- Step hooks for cost tracking, blocker detection, and HITL signals
- Sensitive-data passthrough for credential autofill

Usage::

    from ghosthands.agent.factory import create_job_agent

    agent = await create_job_agent(
        task="Apply to https://jobs.lever.co/example/12345",
        resume_profile={"name": "Jane Doe", "email": "jane@example.com", ...},
        credentials={"lever.co": {"email": "jane@example.com", "password": "x_secret_x"}},
        platform="lever",
    )
    result = await agent.run(max_steps=100)
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse
from collections.abc import Awaitable, Callable
from typing import Any

from ghosthands.blockers import blocker_text_from_extracted, build_unresolved_blocker_payload
from ghosthands.agent.handx_agent import HandXAgent
from ghosthands.browser import HandXBrowserProfile, HandXTools
from ghosthands.agent.hooks import StepHooks
from ghosthands.agent.prompts import build_system_prompt
from ghosthands.config.settings import settings
from ghosthands.security.domain_lockdown import PLATFORM_ALLOWLISTS

logger = logging.getLogger(__name__)


def _extract_task_url(task: str) -> str:
    for token in str(task or "").split():
        if token.startswith(("http://", "https://")):
            return token.rstrip(').,]}>')
    return ""


def _browser_allowed_domain_matches(url: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "").lower()
    if not host or not pattern:
        return False

    normalized = pattern.strip().lower()
    full_url_pattern = f"{scheme}://{host}"

    if "*" in normalized:
        if normalized.startswith("*."):
            domain_part = normalized[2:]
            return scheme in {"http", "https"} and (host == domain_part or host.endswith("." + domain_part))
        if normalized.endswith("/*"):
            return fnmatch(url, normalized)
        return fnmatch(full_url_pattern if "://" in normalized else host, normalized)

    if "://" in normalized:
        return url.lower().startswith(normalized)

    if host == normalized:
        return True
    return normalized.count(".") == 1 and host == f"www.{normalized}"


def _warn_if_allowed_domains_miss_task_host(*, task: str, platform: str, allowed_domains: list[str], job_id: str) -> None:
    job_url = _extract_task_url(task)
    if not job_url:
        return

    host = (urlparse(job_url).hostname or "").lower()
    if not host:
        return

    if any(_browser_allowed_domain_matches(job_url, domain) for domain in allowed_domains):
        return

    platform_domains = sorted(set(PLATFORM_ALLOWLISTS.get(platform.lower(), [])))
    logger.warning(
        "agent.allowed_domains_missing_target_host",
        extra={
            "job_id": job_id,
            "platform": platform,
            "job_url": job_url,
            "host": host,
            "allowed_domains": list(allowed_domains),
            "platform_allowlist": platform_domains,
            "covered_by_platform_allowlist": any(
                host == domain.lower() or host.endswith("." + domain.lower()) for domain in platform_domains
            ),
        },
    )


async def create_job_agent(
    task: str,
    resume_profile: dict[str, Any],
    credentials: dict[str, str | dict[str, str]] | None = None,
    platform: str = "generic",
    headless: bool | None = None,
    max_steps: int | None = None,
    job_id: str = "",
    max_budget: float | None = None,
    on_status_update: Callable[..., Awaitable[None]] | None = None,
    allowed_domains: list[str] | None = None,
    browser_session: Any | None = None,
) -> HandXAgent:
    """Create a browser-use Agent configured for job application automation.

    Parameters
    ----------
    task:
            The agent task description (typically includes the job URL).
    resume_profile:
            Parsed resume data dict with keys like ``name``, ``email``,
            ``phone``, ``experience``, ``education``, ``skills``.
    credentials:
            Optional login credentials.  Passed to browser-use as
            ``sensitive_data`` so the agent can reference them by key
            without the raw values leaking into prompts.
            Supports both flat ``{"email": "...", "password": "..."}`` and
            domain-scoped ``{"lever.co": {"email": "...", "password": "..."}}``
            formats (browser-use handles both).
    platform:
            ATS platform identifier for guardrail selection.
            One of: ``"workday"``, ``"greenhouse"``, ``"lever"``,
            ``"smartrecruiters"``, ``"generic"``.
    headless:
            Whether to run the browser headless.  Defaults to the
            ``GH_HEADLESS`` setting.
    max_steps:
            Maximum agent steps.  Defaults to ``GH_MAX_STEPS_PER_JOB``.
    job_id:
            Job identifier for logging and status callbacks.
    max_budget:
            Maximum LLM spend in USD.  Defaults to ``GH_MAX_BUDGET_PER_JOB``.
    on_status_update:
            Optional async callback fired after each step with a status dict.
    allowed_domains:
            Override for domain lockdown.  Defaults to ``GH_ALLOWED_DOMAINS``.

    Returns
    -------
    Agent
            A fully-configured browser-use Agent ready to ``run()``.
    """
    # ── Resolve defaults from settings ────────────────────────────
    if headless is None:
        headless = settings.headless
    if max_steps is None:
        max_steps = settings.max_steps_per_job
    if max_budget is None:
        max_budget = settings.max_budget_per_job
    if allowed_domains is None:
        allowed_domains = settings.allowed_domains
    _warn_if_allowed_domains_miss_task_host(
        task=task,
        platform=platform,
        allowed_domains=allowed_domains,
        job_id=job_id,
    )

    # ── LLM ───────────────────────────────────────────────────────
    from ghosthands.llm.client import get_chat_model

    llm = get_chat_model()

    # ── Tools with DomHand actions ────────────────────────────────
    excluded_actions = ["write_file", "replace_file", "evaluate", "read_file"]
    tools: HandXTools = HandXTools(exclude_actions=excluded_actions)
    use_domhand_tools = bool(settings.enable_domhand or platform == "workday")
    if settings.enable_domhand:
        logger.info("agent.domhand_runtime_prefill_enabled")
    else:
        logger.info("agent.domhand_runtime_prefill_disabled")
    if use_domhand_tools:
        from ghosthands.actions import register_domhand_actions

        register_domhand_actions(tools)
        if platform == "workday" and not settings.enable_domhand:
            logger.info("agent.workday_domhand_action_surface_enabled")

    # ── System prompt ─────────────────────────────────────────────
    system_prompt = build_system_prompt(
        resume_profile,
        platform,
        use_domhand=use_domhand_tools,
    )

    # ── Browser profile with domain lockdown ──────────────────────
    browser_profile = HandXBrowserProfile(
        headless=headless,
        allowed_domains=allowed_domains,
        keep_alive=True,  # Keep browser open for user review / HITL
        aboutblank_loading_logo_enabled=True,
        demo_mode=False,  # Suppress browser-use logo/panel overlay
        interaction_highlight_color="rgb(37, 99, 235)",
        wait_between_actions=settings.wait_between_actions,
    )

    # ── Sensitive data (credentials) ──────────────────────────────
    # browser-use accepts both flat {key: value} and domain-scoped
    # {domain: {key: value}} dicts.  Pass credentials through as-is.
    sensitive_data: dict[str, str | dict[str, str]] | None = None
    if credentials:
        sensitive_data = {k: v for k, v in credentials.items()}

    # ── Assemble the agent ────────────────────────────────────────
    agent = HandXAgent(
        task=task,
        llm=llm,
        tools=tools,
        browser_profile=browser_profile,
        browser_session=browser_session,
        extend_system_message=system_prompt,
        sensitive_data=sensitive_data,
        # Cost tracking — browser-use will populate history.usage
        calculate_cost=True,
        # Keep screenshot-based visual fallback available for exact stuck-field
        # retries while still allowing the agent to request vision only when
        # needed.
        use_vision="auto",
        # No judge needed — we detect completion ourselves
        use_judge=False,
        # Reasonable defaults for job-application flows
        max_actions_per_step=settings.agent_max_actions_per_step,
        max_failures=5,
        use_thinking=True,
    )

    logger.info(
        "agent.created",
        extra={
            "job_id": job_id,
            "platform": platform,
            "model": settings.agent_model,
            "max_steps": max_steps,
            "max_budget": max_budget,
            "headless": headless,
            "has_credentials": credentials is not None,
            "domain_count": len(allowed_domains),
            "use_vision": "auto",
            "llm_proxy": bool(settings.llm_proxy_url),
            "domhand_enabled": settings.enable_domhand,
        },
    )

    return agent


async def run_job_agent(
    task: str,
    resume_profile: dict[str, Any],
    credentials: dict[str, str | dict[str, str]] | None = None,
    platform: str = "generic",
    headless: bool | None = None,
    max_steps: int | None = None,
    job_id: str = "",
    max_budget: float | None = None,
    on_status_update: Callable[..., Awaitable[None]] | None = None,
    allowed_domains: list[str] | None = None,
    keep_alive: bool = False,
    browser_session: Any | None = None,
) -> dict[str, Any]:
    """Convenience wrapper: create an agent, run it, and return a result dict.

    This is the function the worker calls for each job.  It handles the
    full lifecycle: create agent -> run -> extract result -> close browser.

    Parameters
    ----------
    keep_alive:
            Controls browser cleanup after the agent finishes.
            ``False`` (default) — kill the browser process.  This is the
            safe default for the EC2 worker path which does not pass
            keep_alive explicitly.
            ``True`` — stop the event bus but leave the browser open for
            human review / HITL (desktop app path).

    Returns
    -------
    dict
            ``{"success": bool, "steps": int, "cost_usd": float,
              "extracted_text": str | None, "blocker": str | None}``
    """
    if max_steps is None:
        max_steps = settings.max_steps_per_job
    if max_budget is None:
        max_budget = settings.max_budget_per_job

    hooks = StepHooks(
        job_id=job_id,
        max_budget=max_budget,
        on_status_update=on_status_update,
    )

    agent = await create_job_agent(
        task=task,
        resume_profile=resume_profile,
        credentials=credentials,
        platform=platform,
        headless=headless,
        max_steps=max_steps,
        job_id=job_id,
        max_budget=max_budget,
        on_status_update=on_status_update,
        allowed_domains=allowed_domains,
        browser_session=browser_session,
    )

    completed = False
    try:
        history = await agent.run(
            max_steps=max_steps,
            on_step_start=hooks.on_step_start,
            on_step_end=hooks.on_step_end,
        )

        # ── Extract result from history ───────────────────────────
        is_done = history.is_done()
        success = False
        extracted_text: str | None = None
        blocker: str | None = None

        if is_done and history.history:
            last_entry = history.history[-1]
            for result in last_entry.result:
                if result.is_done:
                    success = result.success or False
                if result.extracted_content:
                    extracted_text = result.extracted_content
                    blocker = blocker_text_from_extracted(extracted_text) or blocker

        unresolved_blocker = build_unresolved_blocker_payload(agent.browser_session, blocker)
        if blocker is None and unresolved_blocker is not None:
            blocker = str(unresolved_blocker.get("message") or "").strip() or None

        completed = True
        return {
            "success": success,
            "steps": agent.state.n_steps,
            "cost_usd": round(hooks.cumulative_cost, 6),
            "extracted_text": extracted_text,
            "blocker": blocker,
            "unresolved_blocker": unresolved_blocker,
        }
    finally:
        # Respect the keep_alive parameter for browser cleanup.
        # keep_alive=False (default, EC2 worker): kill the browser process.
        # keep_alive=True (Desktop): stop event bus but leave browser open.
        if agent.browser_session is not None:
            try:
                if not keep_alive:
                    await agent.browser_session.kill()
                else:
                    # keep_alive=True: stop event bus but leave browser open
                    await agent.browser_session.event_bus.stop(clear=False, timeout=1.0)
            except Exception:
                pass
