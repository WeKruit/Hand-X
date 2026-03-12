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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
	from browser_use.agent.service import Agent

logger = logging.getLogger(__name__)


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

	# ------------------------------------------------------------------
	# on_step_start
	# ------------------------------------------------------------------

	async def on_step_start(self, agent: "Agent") -> None:
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

		# If an external signal set the stopped flag, the agent loop will
		# honour it on the next iteration.  We don't need to do anything
		# extra here — just ensure it's visible in logs.
		if agent.state.stopped:
			logger.info(
				"step.start.agent_stopped",
				extra={"job_id": self.job_id, "step": step},
			)

	# ------------------------------------------------------------------
	# on_step_end
	# ------------------------------------------------------------------

	async def on_step_end(self, agent: "Agent") -> None:
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
		if usage is not None:
			self._cumulative_cost = usage.total_cost or 0.0

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
		if agent.history.is_done() and agent.history.history:
			last_entry = agent.history.history[-1]
			for result in last_entry.result:
				if result.extracted_content and "blocker:" in result.extracted_content.lower():
					blocker_detected = result.extracted_content

		# ── Status update callback ─────────────────────────────────
		if self.on_status_update is not None:
			status: dict = {
				"job_id": self.job_id,
				"step": step,
				"cost_usd": round(self._cumulative_cost, 6),
				"is_done": agent.history.is_done(),
			}
			if blocker_detected:
				status["blocker"] = blocker_detected
			if last_output is not None:
				status["next_goal"] = last_output.next_goal
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
