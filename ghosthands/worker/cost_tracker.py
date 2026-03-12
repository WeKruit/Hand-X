"""Per-job cost tracking and budget enforcement.

Mirrors the CostTracker/CostControlService pattern from GH's costControl.ts,
adapted for Python. Tracks token usage, estimates cost from the model catalog,
and enforces per-job budget limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from ghosthands.config.models import MODEL_CATALOG, estimate_cost

logger = structlog.get_logger()

# ── Budget configuration ──────────────────────────────────────────────────

# Per-task LLM budget (in USD) by quality preset
TASK_BUDGET: dict[str, float] = {
	"speed": 0.05,
	"balanced": 0.50,
	"quality": 1.00,
}

# Per-job-type budget overrides (bypass quality preset when present)
JOB_TYPE_BUDGET_OVERRIDES: dict[str, float] = {
	"workday_apply": 2.00,
	"smart_apply": 2.00,
}

# Per-job-type max step limits
JOB_TYPE_STEP_LIMITS: dict[str, int] = {
	"apply": 100,
	"scrape": 50,
	"fill_form": 80,
	"custom": 100,
	"workday_apply": 200,
	"smart_apply": 200,
}

DEFAULT_MAX_STEPS = 100


# ── Exceptions ────────────────────────────────────────────────────────────


class BudgetExceededError(Exception):
	"""Raised when a job exceeds its LLM cost budget."""

	def __init__(self, message: str, job_id: str, snapshot: CostSnapshot) -> None:
		super().__init__(message)
		self.job_id = job_id
		self.snapshot = snapshot


class StepLimitExceededError(Exception):
	"""Raised when a job exceeds its step limit."""

	def __init__(self, message: str, job_id: str, step_count: int, limit: int) -> None:
		super().__init__(message)
		self.job_id = job_id
		self.step_count = step_count
		self.limit = limit


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StepCost:
	"""Cost record for a single agent step."""

	step: int
	model: str
	tokens_in: int
	tokens_out: int
	cost: float


@dataclass
class CostSnapshot:
	"""Frozen snapshot of current cost state."""

	total_cost: float
	input_tokens: int
	output_tokens: int
	input_cost: float
	output_cost: float
	step_count: int
	steps: list[StepCost]

	def to_dict(self) -> dict[str, Any]:
		"""Serialize for JSON storage / VALET callback."""
		return {
			"total_cost_usd": round(self.total_cost, 6),
			"input_tokens": self.input_tokens,
			"output_tokens": self.output_tokens,
			"input_cost_usd": round(self.input_cost, 6),
			"output_cost_usd": round(self.output_cost, 6),
			"step_count": self.step_count,
			"total_tokens": self.input_tokens + self.output_tokens,
		}


# ── CostTracker ───────────────────────────────────────────────────────────


class CostTracker:
	"""Per-job cost tracker with budget enforcement.

	Tracks LLM token usage across steps, computes running cost using the
	model catalog, and raises BudgetExceededError when the per-job budget
	is exceeded.

	Usage::

		tracker = CostTracker(
			job_id="abc-123",
			max_budget=0.50,
			job_type="workday_apply",
		)
		tracker.track_step(step=1, tokens_in=500, tokens_out=200, model="claude-haiku-4-5-20251001")
		if tracker.is_over_budget():
			...
	"""

	def __init__(
		self,
		job_id: str,
		max_budget: float | None = None,
		quality_preset: str = "balanced",
		job_type: str | None = None,
		max_steps: int | None = None,
	) -> None:
		self.job_id = job_id

		# Resolve budget: job_type override > explicit max_budget > quality preset
		if max_budget is not None:
			self.max_budget = max_budget
		elif job_type and job_type in JOB_TYPE_BUDGET_OVERRIDES:
			self.max_budget = JOB_TYPE_BUDGET_OVERRIDES[job_type]
		else:
			self.max_budget = TASK_BUDGET.get(quality_preset, TASK_BUDGET["balanced"])

		# Resolve step limit
		if max_steps is not None:
			self.max_steps = max_steps
		elif job_type and job_type in JOB_TYPE_STEP_LIMITS:
			self.max_steps = JOB_TYPE_STEP_LIMITS[job_type]
		else:
			self.max_steps = DEFAULT_MAX_STEPS

		self.total_cost: float = 0.0
		self._input_tokens: int = 0
		self._output_tokens: int = 0
		self._input_cost: float = 0.0
		self._output_cost: float = 0.0
		self._step_count: int = 0
		self._steps: list[StepCost] = []

	def track_step(
		self,
		step: int,
		tokens_in: int,
		tokens_out: int,
		model: str,
	) -> None:
		"""Record token usage for a single agent step.

		Computes cost from the model catalog and adds to the running total.
		Raises BudgetExceededError if the budget is exceeded after this step.
		Raises StepLimitExceededError if the step limit is exceeded.
		"""
		cost = estimate_cost(model, tokens_in, tokens_out)

		# Compute per-direction costs
		model_config = MODEL_CATALOG.get(model)
		if model_config:
			in_cost = tokens_in / 1000 * model_config.input_cost_per_1k
			out_cost = tokens_out / 1000 * model_config.output_cost_per_1k
		else:
			# Fallback: split proportionally
			in_cost = cost * (tokens_in / max(tokens_in + tokens_out, 1))
			out_cost = cost - in_cost

		step_record = StepCost(
			step=step,
			model=model,
			tokens_in=tokens_in,
			tokens_out=tokens_out,
			cost=cost,
		)

		self._steps.append(step_record)
		self._input_tokens += tokens_in
		self._output_tokens += tokens_out
		self._input_cost += in_cost
		self._output_cost += out_cost
		self.total_cost += cost
		self._step_count += 1

		logger.debug(
			"cost_tracker.step",
			job_id=self.job_id,
			step=step,
			model=model,
			tokens_in=tokens_in,
			tokens_out=tokens_out,
			step_cost=round(cost, 6),
			total_cost=round(self.total_cost, 6),
			budget=self.max_budget,
		)

		# Budget enforcement
		if self.total_cost > self.max_budget:
			raise BudgetExceededError(
				f"Job budget exceeded: ${self.total_cost:.4f} > ${self.max_budget:.2f}",
				self.job_id,
				self.get_snapshot(),
			)

		# Step limit enforcement
		if self._step_count > self.max_steps:
			raise StepLimitExceededError(
				f"Step limit exceeded: {self._step_count} > {self.max_steps}",
				self.job_id,
				self._step_count,
				self.max_steps,
			)

	def is_over_budget(self) -> bool:
		"""Check if the job has exceeded its budget."""
		return self.total_cost >= self.max_budget

	def remaining_budget(self) -> float:
		"""How much budget remains for this job."""
		return max(0.0, self.max_budget - self.total_cost)

	def get_snapshot(self) -> CostSnapshot:
		"""Return a frozen snapshot of the current cost state."""
		return CostSnapshot(
			total_cost=self.total_cost,
			input_tokens=self._input_tokens,
			output_tokens=self._output_tokens,
			input_cost=self._input_cost,
			output_cost=self._output_cost,
			step_count=self._step_count,
			steps=list(self._steps),
		)

	def get_summary(self) -> dict[str, Any]:
		"""Return a summary dict for logging and callback payloads."""
		return {
			"total_cost_usd": round(self.total_cost, 6),
			"budget_usd": self.max_budget,
			"remaining_usd": round(self.remaining_budget(), 6),
			"total_tokens": self._input_tokens + self._output_tokens,
			"input_tokens": self._input_tokens,
			"output_tokens": self._output_tokens,
			"step_count": self._step_count,
			"is_over_budget": self.is_over_budget(),
		}


# ── Quality preset resolution ────────────────────────────────────────────


def resolve_quality_preset(input_data: dict[str, Any]) -> str:
	"""Resolve quality preset from job input_data.

	Maps VALET tier names and quality values to our presets.
	Falls back to 'balanced' if not recognized.
	"""
	raw = input_data.get("quality_preset") or input_data.get("tier")
	if not isinstance(raw, str):
		return "balanced"

	mapping: dict[str, str] = {
		"speed": "speed",
		"fast": "speed",
		"free": "speed",
		"starter": "balanced",
		"balanced": "balanced",
		"pro": "quality",
		"quality": "quality",
		"thorough": "quality",
		"premium": "quality",
	}

	return mapping.get(raw.lower(), "balanced")
