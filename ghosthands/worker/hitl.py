"""Human-in-the-loop (HITL) pause/resume management via Postgres LISTEN/NOTIFY.

When the agent encounters a blocker it cannot handle (CAPTCHA, login wall, 2FA),
it pauses the job and waits for a human signal via Postgres NOTIFY. The VALET
frontend allows users to solve the blocker in a live VNC view, then clicks
"Resume" which triggers a NOTIFY on the job's channel.

Channel naming: ``gh_job_signal_{job_id_with_underscores}``
Payload: JSON ``{"action": "resume"|"cancel", "data": {...}}``
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import asyncpg
import structlog

from ghosthands.integrations.database import Database
from ghosthands.integrations.valet_callback import ValetClient

logger = structlog.get_logger()

# Default timeout waiting for human to resolve a blocker
DEFAULT_WAIT_TIMEOUT = 300.0  # 5 minutes


class HITLManager:
	"""Manage human-in-the-loop pause/resume for a running job.

	Lifecycle:
	1. Agent detects blocker (CAPTCHA, login, etc.)
	2. ``pause_job()`` — updates DB status to ``needs_human``, fires VALET callback
	3. ``wait_for_resume()`` — blocks until NOTIFY signal or timeout
	4. Signal received → returns signal data; timeout → returns None
	"""

	def __init__(
		self,
		db: Database,
		valet: ValetClient,
		worker_id: str,
	) -> None:
		self.db = db
		self.valet = valet
		self.worker_id = worker_id
		self._listen_conn: asyncpg.Connection | None = None
		self._signal_event: asyncio.Event = asyncio.Event()
		self._signal_data: dict[str, Any] | None = None

	async def check_for_signals(self, job_id: str) -> str | None:
		"""Non-blocking check if there's a pending resume/cancel signal.

		Queries the job status in the database. If the status has been
		changed to ``cancelled`` by VALET, returns ``'cancel'``.
		If ``running``, returns None (no signal).

		This is a lightweight poll — use ``wait_for_resume()`` for blocking waits.
		"""
		pool = self.db._require_pool()
		row = await pool.fetchrow(
			"SELECT status FROM gh_automation_jobs WHERE id = $1::uuid",
			job_id if not isinstance(job_id, str) else __import__("uuid").UUID(job_id),
		)
		if row is None:
			return "cancel"  # Job doesn't exist, treat as cancel

		status = row["status"]
		if status == "cancelled":
			return "cancel"
		if status in ("pending", "queued"):
			# Someone re-queued it externally (retry) — treat as resume
			return "resume"
		return None

	async def pause_job(
		self,
		job_id: str,
		reason: str,
		interaction_type: str = "blocker",
		screenshot_url: str | None = None,
		page_url: str | None = None,
		valet_task_id: str | None = None,
	) -> None:
		"""Pause the job and notify VALET that human intervention is needed.

		Updates the job status to ``needs_human`` in the database, writes
		a job event, and fires a VALET callback so the frontend can show
		the blocker to the user.
		"""
		# Update DB
		await self.db.update_job_status(
			job_id,
			"needs_human",
			metadata={
				"interaction_type": interaction_type,
				"reason": reason,
				"page_url": page_url,
				"screenshot_url": screenshot_url,
			},
		)

		# Write audit event
		await self.db.write_job_event(
			job_id,
			"needs_human",
			metadata={
				"type": interaction_type,
				"reason": reason,
				"worker_id": self.worker_id,
			},
		)

		# Notify VALET
		await self.valet.report_needs_human(
			job_id=job_id,
			interaction_type=interaction_type,
			description=reason,
			valet_task_id=valet_task_id,
			worker_id=self.worker_id,
			screenshot_url=screenshot_url,
			page_url=page_url,
		)

		logger.info(
			"hitl.job_paused",
			job_id=job_id,
			interaction_type=interaction_type,
			reason=reason,
		)

	async def wait_for_resume(
		self,
		job_id: str,
		timeout: float = DEFAULT_WAIT_TIMEOUT,
	) -> dict[str, Any] | None:
		"""Block until a resume/cancel signal arrives or timeout expires.

		Subscribes to the job's Postgres NOTIFY channel and waits. When a
		signal is received, updates job status back to ``running`` (if resumed)
		and returns the signal data.

		Args:
			job_id: The job UUID.
			timeout: Maximum seconds to wait.

		Returns:
			Signal data dict on resume (e.g. ``{"action": "resume", ...}``),
			or None on timeout. If ``action`` is ``"cancel"``, returns
			``{"action": "cancel"}``.
		"""
		self._signal_event.clear()
		self._signal_data = None

		async def _on_signal(data: dict[str, Any]) -> None:
			self._signal_data = data
			self._signal_event.set()

		# Start listening
		try:
			self._listen_conn = await self.db.listen_for_signals(job_id, _on_signal)
		except Exception as exc:
			logger.warning(
				"hitl.listen_failed",
				job_id=job_id,
				error=str(exc),
			)
			# Fall back to polling
			return await self._poll_for_resume(job_id, timeout)

		try:
			# Wait for signal or timeout
			try:
				await asyncio.wait_for(self._signal_event.wait(), timeout=timeout)
			except asyncio.TimeoutError:
				logger.info("hitl.wait_timeout", job_id=job_id, timeout=timeout)
				return None

			signal = self._signal_data
			if signal is None:
				return None

			action = signal.get("action", "resume")
			logger.info(
				"hitl.signal_received",
				job_id=job_id,
				action=action,
				data=signal,
			)

			if action == "cancel":
				await self.db.update_job_status(job_id, "cancelled")
				return {"action": "cancel"}

			# Resume: update status back to running
			await self.db.update_job_status(job_id, "running")
			await self.db.write_job_event(
				job_id,
				"resumed",
				metadata={"signal": signal, "worker_id": self.worker_id},
			)

			return signal

		finally:
			# Clean up listener
			if self._listen_conn is not None:
				await self.db.unlisten(self._listen_conn, job_id)
				self._listen_conn = None

	async def _poll_for_resume(
		self,
		job_id: str,
		timeout: float,
	) -> dict[str, Any] | None:
		"""Fallback polling loop when LISTEN is unavailable (e.g. pgbouncer).

		Checks job status every 5 seconds until it changes from ``needs_human``
		or the timeout expires.
		"""
		poll_interval = 5.0
		elapsed = 0.0

		while elapsed < timeout:
			await asyncio.sleep(min(poll_interval, timeout - elapsed))
			elapsed += poll_interval

			signal = await self.check_for_signals(job_id)
			if signal == "cancel":
				return {"action": "cancel"}
			if signal == "resume":
				await self.db.update_job_status(job_id, "running")
				return {"action": "resume"}

			# Also check if status changed from needs_human
			pool = self.db._require_pool()
			row = await pool.fetchrow(
				"SELECT status FROM gh_automation_jobs WHERE id = $1::uuid",
				__import__("uuid").UUID(job_id) if isinstance(job_id, str) else job_id,
			)
			if row and row["status"] not in ("needs_human",):
				return {"action": "resume", "source": "status_change"}

		logger.info("hitl.poll_timeout", job_id=job_id, timeout=timeout)
		return None
