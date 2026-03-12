"""Worker main loop — poll for jobs, claim, execute, repeat.

Handles graceful shutdown via SIGTERM/SIGINT, releasing claimed jobs back to
the queue so other workers can pick them up. Mirrors the JobPoller pattern
from GH's TypeScript codebase.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

import structlog

from ghosthands.config.settings import settings
from ghosthands.integrations.database import Database
from ghosthands.integrations.valet_callback import ValetClient
from ghosthands.worker.executor import execute_job

logger = structlog.get_logger()


async def run_worker() -> None:
	"""Main worker loop: connect → poll → claim → execute → repeat.

	Runs until SIGTERM or SIGINT is received, at which point it:
	1. Stops accepting new jobs
	2. Waits for the current job to finish (up to 30s)
	3. Releases any still-claimed jobs back to the queue
	4. Closes connections and exits
	"""
	log = logger.bind(worker_id=settings.worker_id)
	log.info("worker.starting")

	# ── Initialize connections ────────────────────────────────────────

	db = Database(settings.database_url)
	await db.connect()

	valet = ValetClient(
		api_url=settings.valet_api_url,
		secret=settings.valet_callback_secret,
	)

	# ── Shutdown handling ─────────────────────────────────────────────

	running = True
	current_job_task: asyncio.Task[Any] | None = None

	async def shutdown(sig: signal.Signals) -> None:
		nonlocal running
		log.info("worker.shutdown_signal", signal=sig.name)
		running = False

		# Wait for current job to finish (with timeout)
		if current_job_task and not current_job_task.done():
			log.info("worker.waiting_for_current_job")
			try:
				await asyncio.wait_for(asyncio.shield(current_job_task), timeout=30.0)
			except asyncio.TimeoutError:
				log.warning("worker.shutdown_timeout_current_job")
				current_job_task.cancel()
				try:
					await current_job_task
				except (asyncio.CancelledError, Exception):
					pass

		# Release any claimed jobs back to queue
		try:
			released = await db.release_worker_jobs(settings.worker_id)
			if released:
				log.info("worker.released_jobs", count=len(released))
		except Exception as exc:
			log.error("worker.release_failed", error=str(exc))

		# Close connections
		await valet.close()
		await db.close()

		log.info("worker.stopped")

	# Register signal handlers
	loop = asyncio.get_running_loop()

	def _handle_signal(sig: signal.Signals) -> None:
		asyncio.ensure_future(shutdown(sig))

	for sig in (signal.SIGTERM, signal.SIGINT):
		loop.add_signal_handler(sig, _handle_signal, sig)

	log.info(
		"worker.ready",
		poll_interval=settings.poll_interval_seconds,
		max_steps=settings.max_steps_per_job,
		max_budget=settings.max_budget_per_job,
	)

	# ── Main poll loop ────────────────────────────────────────────────

	consecutive_errors = 0
	MAX_CONSECUTIVE_ERRORS = 10

	while running:
		try:
			# Poll for available jobs
			jobs = await db.poll_for_jobs(settings.worker_id)

			if not jobs:
				# No jobs available — sleep and retry
				await asyncio.sleep(settings.poll_interval_seconds)
				consecutive_errors = 0
				continue

			job = jobs[0]
			job_id = str(job["id"])
			log.info(
				"worker.job_claimed",
				job_id=job_id,
				job_type=job.get("job_type"),
				target_url=job.get("target_url", "")[:100],
			)

			# Execute the job
			current_job_task = asyncio.create_task(
				_execute_with_error_handling(job, db, valet),
				name=f"job-{job_id}",
			)

			# Wait for completion (single-task-per-worker model)
			await current_job_task
			current_job_task = None

			# Reset error counter on success
			consecutive_errors = 0

			# Immediately try to pick up the next job (no sleep)

		except asyncio.CancelledError:
			log.info("worker.loop_cancelled")
			break

		except Exception as exc:
			consecutive_errors += 1
			log.error(
				"worker.poll_error",
				error=str(exc),
				consecutive_errors=consecutive_errors,
			)

			if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
				log.critical(
					"worker.too_many_errors",
					count=consecutive_errors,
					limit=MAX_CONSECUTIVE_ERRORS,
				)
				break

			# Back off on errors (exponential with cap)
			backoff = min(settings.poll_interval_seconds * (2 ** consecutive_errors), 60.0)
			await asyncio.sleep(backoff)

	# Final cleanup (in case we exited the loop without signal)
	if running:
		await shutdown(signal.SIGTERM)


async def _execute_with_error_handling(
	job: dict[str, Any],
	db: Database,
	valet: ValetClient,
) -> None:
	"""Wrapper that catches all errors from execute_job so the poll loop never dies."""
	job_id = str(job["id"])
	try:
		result = await execute_job(job, db, valet)
		success = result.get("success", False)
		logger.info(
			"worker.job_finished",
			job_id=job_id,
			success=success,
			summary=result.get("summary", "")[:200],
		)
	except Exception as exc:
		logger.error(
			"worker.job_unhandled_error",
			job_id=job_id,
			error=str(exc),
		)
		# Ensure the job is marked as failed even if executor didn't catch it
		try:
			await db.update_job_status(
				job_id,
				"failed",
				metadata={"error_code": "worker_unhandled", "message": str(exc)},
			)
			await valet.report_completion(
				job_id=job_id,
				success=False,
				result={"error": str(exc), "error_code": "worker_unhandled"},
				worker_id=settings.worker_id,
			)
		except Exception as inner_exc:
			logger.error(
				"worker.failsafe_failed",
				job_id=job_id,
				error=str(inner_exc),
			)
