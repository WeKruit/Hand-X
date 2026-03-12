"""VALET API callback client — reports job status, progress, and completion.

Mirrors the CallbackNotifier pattern from GH's TypeScript codebase, adapted for
Python + httpx. All callbacks are fire-and-forget with retry; failures are logged
but never fail the job.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

MAX_RETRIES = 3
RETRY_DELAYS = [1.0, 3.0, 10.0]  # seconds
TIMEOUT_SECONDS = 10.0


class ValetClient:
	"""Async HTTP client for VALET API callbacks.

	Sends job status updates, progress reports, and final completion results
	back to VALET. Uses ``X-GH-Service-Key`` header for auth (matching the
	GH TypeScript CallbackNotifier pattern).
	"""

	def __init__(self, api_url: str, secret: str) -> None:
		self.api_url = api_url.rstrip("/")
		self.secret = secret
		self.client = httpx.AsyncClient(
			timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=5.0),
			headers={
				"Content-Type": "application/json",
				"User-Agent": "GhostHands-HandX/1.0",
			},
		)

	async def close(self) -> None:
		"""Close the underlying HTTP client."""
		await self.client.aclose()

	# ── Auth headers ──────────────────────────────────────────────────

	def _auth_headers(self) -> dict[str, str]:
		headers: dict[str, str] = {}
		if self.secret:
			headers["X-GH-Service-Key"] = self.secret
		return headers

	# ── Status reporting ──────────────────────────────────────────────

	async def report_status(
		self,
		job_id: str,
		status: str,
		metadata: dict[str, Any] | None = None,
		valet_task_id: str | None = None,
		worker_id: str | None = None,
	) -> bool:
		"""Report a job status change to VALET.

		This is the general-purpose callback. For specific transitions
		(running, completion, needs_human), prefer the dedicated methods
		which include the correct payload shape.
		"""
		payload: dict[str, Any] = {
			"job_id": job_id,
			"valet_task_id": valet_task_id,
			"status": status,
		}
		if worker_id:
			payload["worker_id"] = worker_id
		if metadata:
			payload["result_data"] = metadata

		return await self._send_callback(job_id, payload)

	async def report_running(
		self,
		job_id: str,
		valet_task_id: str | None = None,
		worker_id: str | None = None,
		execution_mode: str | None = None,
	) -> bool:
		"""Notify VALET that a job has started executing."""
		payload: dict[str, Any] = {
			"job_id": job_id,
			"valet_task_id": valet_task_id,
			"status": "running",
		}
		if worker_id:
			payload["worker_id"] = worker_id
		if execution_mode:
			payload["execution_mode"] = execution_mode
		return await self._send_callback(job_id, payload)

	async def report_progress(
		self,
		job_id: str,
		step: int,
		total_steps: int,
		description: str,
		valet_task_id: str | None = None,
	) -> bool:
		"""Report incremental progress on a running job.

		Progress is sent as metadata on a 'running' status callback so
		VALET can update its UI without changing state.
		"""
		payload: dict[str, Any] = {
			"job_id": job_id,
			"valet_task_id": valet_task_id,
			"status": "running",
			"result_data": {
				"progress_step": step,
				"progress_total": total_steps,
				"progress_description": description,
				"progress_pct": round(step / max(total_steps, 1) * 100, 1),
			},
		}
		return await self._send_callback(job_id, payload)

	async def report_completion(
		self,
		job_id: str,
		success: bool,
		result: dict[str, Any],
		valet_task_id: str | None = None,
		worker_id: str | None = None,
		cost: dict[str, Any] | None = None,
	) -> bool:
		"""Report final job completion (success or failure)."""
		status = "completed" if success else "failed"

		payload: dict[str, Any] = {
			"job_id": job_id,
			"valet_task_id": valet_task_id,
			"status": status,
			"completed_at": datetime.now(timezone.utc).isoformat(),
		}

		if worker_id:
			payload["worker_id"] = worker_id

		if success:
			payload["result_data"] = result
			payload["result_summary"] = result.get("summary", "Job completed")
		else:
			payload["error_code"] = result.get("error_code", "job_failed")
			payload["error_message"] = result.get("error", str(result))

		if cost:
			payload["cost"] = cost

		return await self._send_callback(job_id, payload)

	async def report_needs_human(
		self,
		job_id: str,
		interaction_type: str,
		description: str,
		valet_task_id: str | None = None,
		worker_id: str | None = None,
		screenshot_url: str | None = None,
		page_url: str | None = None,
	) -> bool:
		"""Notify VALET that a job needs human intervention (CAPTCHA, login, etc.)."""
		payload: dict[str, Any] = {
			"job_id": job_id,
			"valet_task_id": valet_task_id,
			"status": "needs_human",
			"interaction": {
				"type": interaction_type,
				"description": description,
				"message": f"Human intervention needed: {interaction_type}",
			},
		}
		if worker_id:
			payload["worker_id"] = worker_id
		if screenshot_url:
			payload["interaction"]["screenshot_url"] = screenshot_url
		if page_url:
			payload["interaction"]["page_url"] = page_url

		return await self._send_callback(job_id, payload)

	async def report_resumed(
		self,
		job_id: str,
		valet_task_id: str | None = None,
		worker_id: str | None = None,
	) -> bool:
		"""Notify VALET that a paused job has resumed after human intervention."""
		payload: dict[str, Any] = {
			"job_id": job_id,
			"valet_task_id": valet_task_id,
			"status": "resumed",
		}
		if worker_id:
			payload["worker_id"] = worker_id
		return await self._send_callback(job_id, payload)

	# ── Internal ──────────────────────────────────────────────────────

	async def _send_callback(self, job_id: str, payload: dict[str, Any]) -> bool:
		"""Send a callback to VALET with retries. Never raises."""
		if not self.api_url:
			logger.debug("valet_callback.skipped_no_url", job_id=job_id)
			return False

		callback_url = f"{self.api_url}/api/v1/tasks/{job_id}/callback"

		for attempt in range(MAX_RETRIES + 1):
			try:
				response = await self.client.post(
					callback_url,
					json=payload,
					headers=self._auth_headers(),
				)

				if response.is_success:
					logger.info(
						"valet_callback.sent",
						job_id=job_id,
						status=payload.get("status"),
						url=callback_url,
					)
					return True

				body = response.text[:500] if response.text else ""
				logger.warning(
					"valet_callback.non_ok",
					job_id=job_id,
					status_code=response.status_code,
					attempt=attempt + 1,
					body=body,
				)

			except httpx.TimeoutException:
				logger.warning(
					"valet_callback.timeout",
					job_id=job_id,
					attempt=attempt + 1,
				)
			except Exception as exc:
				logger.warning(
					"valet_callback.error",
					job_id=job_id,
					attempt=attempt + 1,
					error=str(exc),
				)

			# Wait before retry (except on last attempt)
			if attempt < MAX_RETRIES:
				import asyncio
				await asyncio.sleep(RETRY_DELAYS[attempt])

		logger.error(
			"valet_callback.exhausted",
			job_id=job_id,
			url=callback_url,
			max_retries=MAX_RETRIES,
		)
		return False
