"""Step hooks for the GhostHands agent.

browser-use's ``Agent.run()`` accepts two optional hook callbacks:

- ``on_step_start(agent: Agent) -> None``  — called before each step
- ``on_step_end(agent: Agent) -> None``    — called after each step

Both receive the ``Agent`` instance (the ``AgentHookFunc`` type alias is
``Callable[[Agent], Awaitable[None]]``).  From the agent you can inspect
``agent.state`` (AgentState), ``agent.history`` (AgentHistoryList), and
``agent.token_cost_service`` for usage data.

These hooks are used to:
- Check for external stop / pause signals (HITL)
- Track cumulative LLM cost against the per-job budget
- Detect blockers reported by the agent's output
- Log step progress for observability
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from browser_use.agent.service import Agent

logger = logging.getLogger(__name__)
_SAME_TAB_GUARD_INSTALLED: set[int] = set()

_SAME_TAB_GUARD_JS = r"""(() => {
	if (window.__ghSameTabGuardInstalled) {
		try {
			document.querySelectorAll('a[target], area[target], form[target]').forEach((el) => el.removeAttribute('target'));
		} catch (e) {}
		return;
	}
	window.__ghSameTabGuardInstalled = true;

	function stripTargets(root) {
		var scope = root && root.querySelectorAll ? root : document;
		try {
			scope.querySelectorAll('a[target], area[target], form[target]').forEach(function(el) {
				el.removeAttribute('target');
			});
		} catch (e) {}
	}

	var originalOpen = typeof window.open === 'function' ? window.open.bind(window) : null;
	window.open = function(url) {
		try {
			if (typeof url === 'string' && url && url !== 'about:blank') {
				window.location.assign(url);
			}
		} catch (e) {}
		return window;
	};
	window.__ghOriginalWindowOpen = originalOpen;

	document.addEventListener('click', function(event) {
		var el = event.target && event.target.closest ? event.target.closest('a[target], area[target]') : null;
		if (el) {
			try { el.removeAttribute('target'); } catch (e) {}
		}
	}, true);

	document.addEventListener('submit', function(event) {
		var form = event.target;
		if (form && form.removeAttribute) {
			try { form.removeAttribute('target'); } catch (e) {}
		}
	}, true);

	var observer = new MutationObserver(function(records) {
		for (var i = 0; i < records.length; i++) {
			var record = records[i];
			if (record.type === 'attributes' && record.attributeName === 'target' && record.target && record.target.removeAttribute) {
				try { record.target.removeAttribute('target'); } catch (e) {}
			}
			if (!record.addedNodes) continue;
			for (var j = 0; j < record.addedNodes.length; j++) {
				var node = record.addedNodes[j];
				if (node && node.querySelectorAll) stripTargets(node);
			}
		}
	});

	if (document.documentElement) {
		observer.observe(document.documentElement, {
			subtree: true,
			childList: true,
			attributes: true,
			attributeFilter: ['target'],
		});
	}

	stripTargets(document);
})();"""


async def install_same_tab_guard(agent: "Agent") -> None:
	"""Prevent sites from opening new tabs during job-application flows.

	This is best-effort:
	- installs a CDP init script once per browser session so future documents inherit it
	- reapplies the script on the current page every step to catch already-loaded DOM
	"""
	try:
		browser_session = getattr(agent, "browser_session", None)
		if browser_session is None:
			return

		session_key = id(browser_session)
		if session_key not in _SAME_TAB_GUARD_INSTALLED:
			try:
				await browser_session._cdp_add_init_script(_SAME_TAB_GUARD_JS)
				_SAME_TAB_GUARD_INSTALLED.add(session_key)
			except Exception as exc:
				logger.debug("step.same_tab_guard_init_failed", extra={"error": str(exc)})

		page = await browser_session.get_current_page()
		if page:
			await page.evaluate(_SAME_TAB_GUARD_JS)
	except Exception as exc:
		logger.debug("step.same_tab_guard_apply_failed", extra={"error": str(exc)})

PHASE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"upload.*resume|resume.*upload|attach.*resume", re.IGNORECASE), "Uploading resume"),
    (re.compile(r"fill.*experience|experience", re.IGNORECASE), "Filling work experience"),
    (
        re.compile(r"personal.*info|personal.*information|contact.*info|contact.*information", re.IGNORECASE),
        "Filling personal information",
    ),
    (
        re.compile(r"additional.*question|supplemental.*question|extra.*question", re.IGNORECASE),
        "Answering additional questions",
    ),
    (re.compile(r"review|submit", re.IGNORECASE), "Preparing to submit"),
    (re.compile(r"navigate|open.*application|go to .*application", re.IGNORECASE), "Navigating to application"),
)


def infer_phase_from_goal(goal: str | None) -> str | None:
    """Map a raw agent goal to a user-friendly progress phase."""
    if not goal:
        return None

    normalized_goal = goal.strip()
    if not normalized_goal:
        return None

    for pattern, phase in PHASE_PATTERNS:
        if pattern.search(normalized_goal):
            return phase
    return None


class StepHooks:
    """Factory that produces bound ``on_step_start`` / ``on_step_end`` hooks.

    Parameters
    ----------
    job_id:
        Identifier for the current job (for logging / metrics).
    max_budget:
        Maximum LLM spend in USD for this job.  When exceeded the agent's
        ``state.stopped`` flag is set so it terminates at the next step.
    on_status_update:
        Optional async callback invoked after every step with a status dict
        that the worker can relay to VALET via callback.  Signature::

            async def on_status_update(status: dict) -> None
    """

    def __init__(
        self,
        job_id: str = "",
        max_budget: float = 0.50,
        on_status_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.job_id = job_id
        self.max_budget = max_budget
        self.on_status_update = on_status_update
        self._cumulative_cost: float = 0.0
        self._metadata_cost_total: float = 0.0
        self._last_phase: str | None = None

    # ------------------------------------------------------------------
    # on_step_start
    # ------------------------------------------------------------------

    async def on_step_start(self, agent: Agent) -> None:
        """Called before each agent step.

        Responsibilities:
        - Check if the agent has been externally paused/stopped (HITL).
        - Log step start for observability.
        """
        step = agent.state.n_steps
        logger.debug(
            "step.start",
            extra={"job_id": self.job_id, "step": step},
        )

        await install_same_tab_guard(agent)

        # If an external signal set the stopped flag, the agent loop will
        # honour it on the next iteration.  We don't need to do anything
        # extra here; just ensure it's visible in logs.
        if agent.state.stopped:
            logger.info(
                "step.start.agent_stopped",
                extra={"job_id": self.job_id, "step": step},
            )

    # ------------------------------------------------------------------
    # on_step_end
    # ------------------------------------------------------------------

    async def on_step_end(self, agent: Agent) -> None:
        """Called after each agent step.

        Responsibilities:
        - Track cumulative LLM cost and stop if budget exceeded.
        - Detect blocker keywords in the agent's last output.
        - Fire an optional status-update callback for the worker.
        """
        step = agent.state.n_steps
        last_output = agent.state.last_model_output

        # ── Cost tracking ──────────────────────────────────────────
        usage = agent.history.usage
        browser_use_cost = 0.0
        usage_input_tokens = 0
        usage_output_tokens = 0
        if usage is not None:
            browser_use_cost = usage.total_cost or 0.0
            usage_input_tokens = usage.total_prompt_tokens or 0
            usage_output_tokens = usage.total_completion_tokens or 0

        last_entry = agent.history.history[-1] if agent.history.history else None
        step_cost: float | None = None
        step_input_tokens: int | None = None
        step_output_tokens: int | None = None
        step_model: str | None = None
        step_llm_calls: int | None = None
        if last_entry and last_entry.result:
            step_cost_total = 0.0
            step_input_total = 0
            step_output_total = 0
            step_llm_call_total = 0
            has_step_metadata = False
            has_input_tokens = False
            has_output_tokens = False
            has_llm_calls = False

            for result in last_entry.result:
                metadata = result.metadata or {}
                if 'step_cost' not in metadata:
                    continue
                has_step_metadata = True
                try:
                    step_cost_total += float(metadata.get('step_cost') or 0.0)
                except (TypeError, ValueError):
                    logger.debug('step.invalid_step_cost', extra={'job_id': self.job_id, 'step': step})
                if metadata.get('input_tokens') is not None:
                    has_input_tokens = True
                    step_input_total += int(metadata['input_tokens'])
                if metadata.get('output_tokens') is not None:
                    has_output_tokens = True
                    step_output_total += int(metadata['output_tokens'])
                if metadata.get('domhand_llm_calls') is not None:
                    has_llm_calls = True
                    step_llm_call_total += int(metadata['domhand_llm_calls'])
                if metadata.get('model'):
                    step_model = str(metadata['model'])

            if has_step_metadata:
                step_cost = step_cost_total
                self._metadata_cost_total += step_cost_total
                if has_input_tokens:
                    step_input_tokens = step_input_total
                if has_output_tokens:
                    step_output_tokens = step_output_total
                if has_llm_calls:
                    step_llm_calls = step_llm_call_total

        self._cumulative_cost = browser_use_cost + self._metadata_cost_total

        if self._cumulative_cost >= self.max_budget:
            logger.warning(
                "step.budget_exceeded",
                extra={
                    "job_id": self.job_id,
                    "step": step,
                    "cost": self._cumulative_cost,
                    "budget": self.max_budget,
                },
            )
            agent.state.stopped = True

        # ── Blocker detection from done text ───────────────────────
        blocker_detected: str | None = None
        if agent.history.is_done() and last_entry:
            for result in last_entry.result:
                if result.extracted_content and "blocker:" in result.extracted_content.lower():
                    blocker_detected = result.extracted_content

        # ── Status update callback ─────────────────────────────────
        if self.on_status_update is not None:
            status: dict[str, Any] = {
                "job_id": self.job_id,
                "step": step,
                "step_cost": round(step_cost, 6) if step_cost is not None else None,
                "cost_usd": round(self._cumulative_cost, 6),
                "is_done": agent.history.is_done(),
            }
            if usage is not None:
                status["usage_input_tokens"] = usage_input_tokens
                status["usage_output_tokens"] = usage_output_tokens
            if step_input_tokens is not None:
                status["input_tokens"] = step_input_tokens
            if step_output_tokens is not None:
                status["output_tokens"] = step_output_tokens
            if step_model:
                status["model"] = step_model
            if step_llm_calls is not None:
                status["domhand_llm_calls"] = step_llm_calls
            if blocker_detected:
                status["blocker"] = blocker_detected
            if last_output is not None:
                next_goal = last_output.next_goal
                if next_goal:
                    status["next_goal"] = next_goal
                phase = infer_phase_from_goal(next_goal)
                if phase is not None and phase != self._last_phase:
                    status["phase"] = phase
                    self._last_phase = phase
            try:
                await self.on_status_update(status)
            except Exception:
                logger.exception(
                    "step.status_update_failed",
                    extra={"job_id": self.job_id, "step": step},
                )

        logger.debug(
            "step.end",
            extra={
                "job_id": self.job_id,
                "step": step,
                "cost_usd": round(self._cumulative_cost, 6),
                "done": agent.history.is_done(),
            },
        )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def cumulative_cost(self) -> float:
        """Return the cumulative LLM cost tracked so far."""
        return self._cumulative_cost
