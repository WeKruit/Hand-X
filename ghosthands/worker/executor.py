"""Job executor — orchestrates a single job from claim to completion.

Takes a claimed job row, loads resume + credentials, detects the platform,
creates the browser-use agent, runs it, and reports results back to the
database and VALET.
"""

from __future__ import annotations

import asyncio
import json
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import structlog

from ghosthands.config.settings import settings
from ghosthands.integrations.credentials import (
	decrypt_credentials,
	decrypt_valet_credentials,
)
from ghosthands.integrations.database import Database
from ghosthands.integrations.resume_loader import load_resume
from ghosthands.integrations.valet_callback import ValetClient
from ghosthands.worker.cost_tracker import (
	BudgetExceededError,
	CostTracker,
	StepLimitExceededError,
	resolve_quality_preset,
)
from ghosthands.worker.hitl import HITLManager

logger = structlog.get_logger()

# ── Platform detection ────────────────────────────────────────────────────

PLATFORM_PATTERNS: dict[str, list[str]] = {
	"workday": ["myworkdayjobs.com", "myworkday.com", "wd5.myworkday.com"],
	"greenhouse": ["greenhouse.io", "boards.greenhouse.io"],
	"lever": ["lever.co", "jobs.lever.co"],
	"smartrecruiters": ["smartrecruiters.com"],
	"ashby": ["ashbyhq.com"],
	"icims": ["icims.com"],
}


def detect_platform(url: str) -> str:
	"""Detect ATS platform from a job URL. Returns platform name or 'generic'."""
	normalized = url.lower()
	for platform, patterns in PLATFORM_PATTERNS.items():
		if any(pattern in normalized for pattern in patterns):
			return platform
	return "generic"


def validate_domain(url: str, allowed_domains: list[str]) -> bool:
	"""Check if the URL's domain is in the allowed list."""
	try:
		hostname = urlparse(url).hostname
		if not hostname:
			return False
		return any(
			hostname == domain or hostname.endswith(f".{domain}")
			for domain in allowed_domains
		)
	except Exception:
		return False


# ── Heartbeat task ────────────────────────────────────────────────────────


async def _heartbeat_loop(db: Database, job_id: str, interval: float = 30.0) -> None:
	"""Background task that updates the heartbeat timestamp periodically.

	Runs until cancelled. Prevents the stuck-job recovery from reclaiming
	our job while it's still running.
	"""
	while True:
		try:
			await db.heartbeat(job_id)
		except Exception as exc:
			logger.warning("executor.heartbeat_failed", job_id=job_id, error=str(exc))
		await asyncio.sleep(interval)


# ── Main executor ─────────────────────────────────────────────────────────


async def execute_job(
	job: dict[str, Any],
	db: Database,
	valet: ValetClient,
) -> dict[str, Any]:
	"""Execute a single automation job end-to-end.

	Steps:
	1. Parse job metadata and validate
	2. Notify VALET that job is running
	3. Load user's resume profile
	4. Load and decrypt credentials (if needed)
	5. Detect ATS platform from target URL
	6. Validate domain is allowed
	7. Create browser-use agent with DomHand actions
	8. Run agent loop with cost tracking and heartbeats
	9. Process result
	10. Update DB and fire VALET completion callback
	11. Return result

	Args:
		job: Full job row from ``gh_automation_jobs``.
		db: Connected Database instance.
		valet: VALET callback client.

	Returns:
		Result dict with ``success``, ``summary``, cost info, etc.
	"""
	job_id = str(job["id"])
	user_id = str(job.get("user_id", ""))
	target_url = job.get("target_url", "")
	job_type = job.get("job_type", "apply")
	input_data = job.get("input_data") or {}
	valet_task_id = job.get("valet_task_id")

	# Parse input_data if it's a string
	if isinstance(input_data, str):
		try:
			input_data = json.loads(input_data)
		except json.JSONDecodeError:
			input_data = {}

	log = logger.bind(job_id=job_id, job_type=job_type, user_id=user_id)
	log.info("executor.starting", target_url=target_url)

	# Initialize cost tracker
	quality = resolve_quality_preset(input_data)
	cost_tracker = CostTracker(
		job_id=job_id,
		max_budget=settings.max_budget_per_job,
		quality_preset=quality,
		job_type=job_type,
		max_steps=settings.max_steps_per_job,
	)

	# Initialize HITL manager
	hitl = HITLManager(db=db, valet=valet, worker_id=settings.worker_id)

	# Start heartbeat background task
	heartbeat_task = asyncio.create_task(_heartbeat_loop(db, job_id))

	try:
		# Step 1: Notify VALET we're running
		await valet.report_running(
			job_id=job_id,
			valet_task_id=valet_task_id,
			worker_id=settings.worker_id,
		)

		# Step 2: Load resume profile
		resume_profile: dict[str, Any] | None = None
		if user_id:
			try:
				resume_profile = await load_resume(db, user_id)
				log.info("executor.resume_loaded", name=resume_profile.get("full_name", ""))
			except ValueError as exc:
				log.warning("executor.no_resume", error=str(exc))
				# Resume is optional for some job types
				resume_profile = None

		# Step 3: Load and decrypt credentials
		credentials: dict[str, str] | None = None
		if user_id:
			try:
				cred_row = await db.load_credentials(job_id)
				if cred_row and cred_row.get("encrypted_secret"):
					if settings.credential_encryption_key:
						credentials = decrypt_credentials(
							cred_row["encrypted_secret"],
							settings.credential_encryption_key,
						)
						log.info("executor.credentials_decrypted", platform=cred_row.get("platform"))
					else:
						log.warning("executor.no_encryption_key")
			except Exception as exc:
				log.warning("executor.credential_load_failed", error=str(exc))

		# Step 4: Detect platform and validate domain
		platform = detect_platform(target_url)
		log.info("executor.platform_detected", platform=platform)

		if not validate_domain(target_url, settings.allowed_domains):
			error_msg = f"Domain not allowed: {urlparse(target_url).hostname}"
			log.error("executor.domain_blocked", url=target_url)
			await _fail_job(
				db, valet, job_id, "domain_blocked", error_msg,
				valet_task_id=valet_task_id,
				cost_tracker=cost_tracker,
			)
			return {"success": False, "error": error_msg, "error_code": "domain_blocked"}

		# Step 5: Report progress — setup complete
		await valet.report_progress(
			job_id=job_id,
			step=1,
			total_steps=5,
			description="Setup complete, launching browser agent",
			valet_task_id=valet_task_id,
		)

		# Step 6: Create and run the browser-use agent
		# TODO: This is the integration point for browser-use.
		# When the agent module is ready, this will call:
		#   agent = create_agent(platform, target_url, resume_profile, credentials)
		#   result = await agent.run(max_steps=settings.max_steps_per_job)
		#
		# For now, we execute a placeholder that returns a structured result.
		result = await _run_agent(
			job_id=job_id,
			target_url=target_url,
			platform=platform,
			resume_profile=resume_profile,
			credentials=credentials,
			input_data=input_data,
			cost_tracker=cost_tracker,
			hitl=hitl,
			valet_task_id=valet_task_id,
			log=log,
		)

		# Step 7: Report progress — agent complete
		await valet.report_progress(
			job_id=job_id,
			step=4,
			total_steps=5,
			description="Application submitted, finalizing",
			valet_task_id=valet_task_id,
		)

		# Step 8: Write result to DB
		cost_summary = cost_tracker.get_summary()
		result_data = {
			"success": result.get("success", True),
			"summary": result.get("summary", "Job completed"),
			"platform": platform,
			"steps_taken": cost_summary["step_count"],
			"cost": cost_summary,
			**{k: v for k, v in result.items() if k not in ("success", "summary")},
		}

		await db.write_job_result(job_id, result_data)
		await db.write_job_event(job_id, "completed", metadata=cost_summary)

		# Step 9: Fire VALET completion callback
		await valet.report_completion(
			job_id=job_id,
			success=True,
			result=result_data,
			valet_task_id=valet_task_id,
			worker_id=settings.worker_id,
			cost={
				"total_cost_usd": cost_summary["total_cost_usd"],
				"action_count": cost_summary["step_count"],
				"total_tokens": cost_summary["total_tokens"],
			},
		)

		log.info(
			"executor.completed",
			success=True,
			steps=cost_summary["step_count"],
			cost=cost_summary["total_cost_usd"],
		)

		return result_data

	except BudgetExceededError as exc:
		log.warning("executor.budget_exceeded", cost=exc.snapshot.total_cost)
		error_data = {
			"error_code": "budget_exceeded",
			"error": str(exc),
			"cost": exc.snapshot.to_dict(),
		}
		await _fail_job(
			db, valet, job_id, "budget_exceeded", str(exc),
			valet_task_id=valet_task_id,
			cost_tracker=cost_tracker,
		)
		return {"success": False, **error_data}

	except StepLimitExceededError as exc:
		log.warning("executor.step_limit_exceeded", steps=exc.step_count, limit=exc.limit)
		error_data = {
			"error_code": "step_limit_exceeded",
			"error": str(exc),
		}
		await _fail_job(
			db, valet, job_id, "step_limit_exceeded", str(exc),
			valet_task_id=valet_task_id,
			cost_tracker=cost_tracker,
		)
		return {"success": False, **error_data}

	except asyncio.CancelledError:
		log.info("executor.cancelled")
		await db.update_job_status(job_id, "cancelled")
		raise

	except Exception as exc:
		log.error(
			"executor.unhandled_error",
			error=str(exc),
			traceback=traceback.format_exc(),
		)
		await _fail_job(
			db, valet, job_id, "internal_error", str(exc),
			valet_task_id=valet_task_id,
			cost_tracker=cost_tracker,
		)
		return {"success": False, "error": str(exc), "error_code": "internal_error"}

	finally:
		# Always cancel heartbeat
		heartbeat_task.cancel()
		try:
			await heartbeat_task
		except asyncio.CancelledError:
			pass


# ── Internal helpers ──────────────────────────────────────────────────────


async def _run_agent(
	job_id: str,
	target_url: str,
	platform: str,
	resume_profile: dict[str, Any] | None,
	credentials: dict[str, str] | None,
	input_data: dict[str, Any],
	cost_tracker: CostTracker,
	hitl: HITLManager,
	valet_task_id: str | None,
	log: Any,
) -> dict[str, Any]:
	"""Run the browser-use agent for the job.

	This is the integration point with browser-use. When the agent module
	is fully implemented, this function will:

	1. Launch a Playwright browser (headless or headed)
	2. Create a browser-use Agent with DomHand custom actions registered
	3. Navigate to the target URL
	4. Fill the application form using DomHand (DOM-first) + LLM fallback
	5. Handle blockers (CAPTCHA, login) via HITL
	6. Submit the application
	7. Return structured result

	For now, this raises NotImplementedError to make it clear that the
	agent module needs to be wired in.
	"""
	# TODO: Replace with actual browser-use agent execution.
	#
	# Example integration (when agent module is ready):
	#
	#   from ghosthands.agent import create_agent
	#
	#   agent = await create_agent(
	#       platform=platform,
	#       target_url=target_url,
	#       resume_profile=resume_profile,
	#       credentials=credentials,
	#       input_data=input_data,
	#       cost_tracker=cost_tracker,
	#       hitl=hitl,
	#       headless=settings.headless,
	#   )
	#
	#   result = await agent.run(max_steps=settings.max_steps_per_job)
	#   return {
	#       "success": result.success,
	#       "summary": result.summary,
	#       "pages_visited": result.pages_visited,
	#       "fields_filled": result.fields_filled,
	#       "screenshots": result.screenshots,
	#   }

	raise NotImplementedError(
		"Agent execution not yet wired. "
		"Implement ghosthands.agent.create_agent() and connect it here. "
		f"Job {job_id} targeting {target_url} on {platform}."
	)


async def _fail_job(
	db: Database,
	valet: ValetClient,
	job_id: str,
	error_code: str,
	error_message: str,
	valet_task_id: str | None = None,
	cost_tracker: CostTracker | None = None,
) -> None:
	"""Mark a job as failed in DB and notify VALET."""
	await db.update_job_status(
		job_id,
		"failed",
		metadata={"error_code": error_code, "message": error_message},
	)
	await db.write_job_event(
		job_id,
		"failed",
		metadata={"error_code": error_code, "error": error_message},
	)

	cost = None
	if cost_tracker:
		summary = cost_tracker.get_summary()
		cost = {
			"total_cost_usd": summary["total_cost_usd"],
			"action_count": summary["step_count"],
			"total_tokens": summary["total_tokens"],
		}

	await valet.report_completion(
		job_id=job_id,
		success=False,
		result={"error": error_message, "error_code": error_code},
		valet_task_id=valet_task_id,
		worker_id=settings.worker_id,
		cost=cost,
	)
