"""Postgres operations via asyncpg — job queue, credentials, resume profiles.

All GhostHands tables use the ``gh_`` prefix. VALET tables (resumes, user_profiles)
have no prefix. Raw SQL is used throughout — no ORM.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
import structlog

logger = structlog.get_logger()

# ── Job pickup SQL (mirrors GH's JobPoller.ts PICKUP_SQL) ─────────────────
# Atomically claim the next available job using FOR UPDATE SKIP LOCKED.
# Matches both 'pending' (legacy) and 'queued' (pg-boss) statuses so it
# works regardless of how VALET dispatched the job.

PICKUP_SQL = """
WITH next_job AS (
	SELECT id
	FROM gh_automation_jobs
	WHERE status IN ('pending', 'queued')
		AND (scheduled_at IS NULL OR scheduled_at <= NOW())
		AND (
			worker_affinity = 'any'
			OR (worker_affinity = 'preferred'
				AND (target_worker_id IS NULL OR target_worker_id = $1))
			OR (worker_affinity = 'strict' AND target_worker_id = $1)
		)
	ORDER BY
		CASE WHEN target_worker_id = $1 THEN 0 ELSE 1 END ASC,
		priority ASC,
		created_at ASC
	LIMIT 1
	FOR UPDATE SKIP LOCKED
)
UPDATE gh_automation_jobs
SET status = 'running',
	worker_id = $1,
	started_at = NOW(),
	last_heartbeat = NOW(),
	updated_at = NOW()
FROM next_job
WHERE gh_automation_jobs.id = next_job.id
RETURNING gh_automation_jobs.*;
"""

# ── Release jobs on shutdown ──────────────────────────────────────────────

RELEASE_SQL = """
UPDATE gh_automation_jobs
SET
	status = 'pending',
	worker_id = NULL,
	error_details = jsonb_build_object(
		'released_by', $1::TEXT,
		'reason', 'worker_shutdown'
	),
	updated_at = NOW()
WHERE worker_id = $1
	AND status IN ('queued', 'running')
RETURNING id;
"""


class Database:
	"""Async Postgres client backed by an asyncpg connection pool.

	Handles job queue operations (poll, claim, update), credential lookups,
	resume profile loading, and LISTEN/NOTIFY for HITL signals.
	"""

	def __init__(self, database_url: str) -> None:
		self.database_url = database_url
		self.pool: asyncpg.Pool | None = None

	# ── Lifecycle ──────────────────────────────────────────────────────

	async def connect(self) -> None:
		"""Create the connection pool."""
		self.pool = await asyncpg.create_pool(
			self.database_url,
			min_size=2,
			max_size=10,
			command_timeout=30,
		)
		logger.info("database.connected", dsn=_redact_dsn(self.database_url))

	async def close(self) -> None:
		"""Gracefully close the pool."""
		if self.pool:
			await self.pool.close()
			self.pool = None
			logger.info("database.closed")

	def _require_pool(self) -> asyncpg.Pool:
		if self.pool is None:
			raise RuntimeError("Database.connect() has not been called")
		return self.pool

	# ── Job queue ──────────────────────────────────────────────────────

	async def poll_for_jobs(self, worker_id: str) -> list[dict[str, Any]]:
		"""Atomically claim the next queued job using FOR UPDATE SKIP LOCKED.

		Returns a list with at most one job dict, or an empty list if no
		jobs are available. The job's status is set to ``'running'`` and
		``worker_id`` is stamped before returning.
		"""
		pool = self._require_pool()
		row = await pool.fetchrow(PICKUP_SQL, worker_id)
		if row is None:
			return []
		job = dict(row)
		logger.info(
			"database.job_claimed",
			job_id=job["id"],
			job_type=job.get("job_type"),
			worker_id=worker_id,
		)
		return [job]

	async def update_job_status(
		self,
		job_id: str,
		status: str,
		metadata: dict[str, Any] | None = None,
	) -> None:
		"""Update job status with optional error/result metadata.

		``metadata`` is merged into the existing ``error_details`` JSONB column
		when the status indicates failure, or ``result_data`` on completion.
		"""
		pool = self._require_pool()

		sets: list[str] = ["status = $2", "updated_at = NOW()"]
		args: list[Any] = [job_id, status]
		idx = 3

		if status in ("completed", "failed", "cancelled", "needs_human"):
			sets.append("completed_at = NOW()")

		if metadata:
			if status in ("failed", "needs_human"):
				sets.append(f"error_details = COALESCE(error_details, '{{}}'::jsonb) || ${idx}::jsonb")
			else:
				sets.append(f"result_data = COALESCE(result_data, '{{}}'::jsonb) || ${idx}::jsonb")
			args.append(json.dumps(metadata))
			idx += 1

		sql = f"UPDATE gh_automation_jobs SET {', '.join(sets)} WHERE id = $1::uuid"
		await pool.execute(sql, *args)
		logger.info("database.job_status_updated", job_id=job_id, status=status)

	async def heartbeat(self, job_id: str) -> None:
		"""Update the heartbeat timestamp so stuck-job recovery doesn't reclaim us."""
		pool = self._require_pool()
		await pool.execute(
			"UPDATE gh_automation_jobs SET last_heartbeat = NOW() WHERE id = $1::uuid",
			uuid.UUID(job_id) if isinstance(job_id, str) else job_id,
		)

	async def write_job_result(self, job_id: str, result: dict[str, Any]) -> None:
		"""Write the final job result and mark as completed."""
		pool = self._require_pool()
		await pool.execute(
			"""
			UPDATE gh_automation_jobs
			SET status = 'completed',
				result_data = $2::jsonb,
				result_summary = $3,
				completed_at = NOW(),
				updated_at = NOW()
			WHERE id = $1::uuid
			""",
			uuid.UUID(job_id) if isinstance(job_id, str) else job_id,
			json.dumps(result),
			result.get("summary", "Job completed"),
		)
		logger.info("database.job_result_written", job_id=job_id)

	async def write_job_event(
		self,
		job_id: str,
		event_type: str,
		metadata: dict[str, Any] | None = None,
		actor: str = "worker",
	) -> None:
		"""Insert a row into gh_job_events for audit/timeline tracking."""
		pool = self._require_pool()
		await pool.execute(
			"""
			INSERT INTO gh_job_events (job_id, event_type, metadata, actor)
			VALUES ($1::uuid, $2, $3::jsonb, $4)
			""",
			uuid.UUID(job_id) if isinstance(job_id, str) else job_id,
			event_type,
			json.dumps(metadata or {}),
			actor,
		)

	async def release_worker_jobs(self, worker_id: str) -> list[str]:
		"""Release all jobs claimed by this worker back to the queue.

		Called during graceful shutdown so other workers can pick them up.
		Returns the IDs of released jobs.
		"""
		pool = self._require_pool()
		rows = await pool.fetch(RELEASE_SQL, worker_id)
		released = [str(row["id"]) for row in rows]
		if released:
			logger.info("database.jobs_released", count=len(released), job_ids=released)
		return released

	# ── Credentials ────────────────────────────────────────────────────

	async def load_credentials(self, job_id: str) -> dict[str, str] | None:
		"""Load the encrypted credential blob for a job's user.

		Returns a dict with ``encrypted_credentials`` (base64 string) and
		``credential_type`` (e.g. ``'platform'``), or None if no credentials
		exist for this user/platform combination.

		Decryption is handled by the caller via ``credentials.decrypt_*``.
		"""
		pool = self._require_pool()

		# First get the job to find user_id and target_url (for platform matching)
		job = await pool.fetchrow(
			"SELECT user_id, target_url, input_data FROM gh_automation_jobs WHERE id = $1::uuid",
			uuid.UUID(job_id) if isinstance(job_id, str) else job_id,
		)
		if not job or not job["user_id"]:
			return None

		user_id = job["user_id"]
		target_url = job.get("target_url", "")

		# Look up credentials in gh_user_credentials
		row = await pool.fetchrow(
			"""
			SELECT id, platform, login_identifier, encrypted_secret, credential_type
			FROM gh_user_credentials
			WHERE user_id = $1::uuid
				AND (
					$2::text ILIKE '%' || domain || '%'
					OR domain IS NULL
				)
			ORDER BY
				CASE WHEN domain IS NOT NULL THEN 0 ELSE 1 END ASC,
				updated_at DESC
			LIMIT 1
			""",
			uuid.UUID(user_id) if isinstance(user_id, str) else user_id,
			target_url or "",
		)

		if row is None:
			logger.debug("database.no_credentials_found", job_id=job_id, user_id=user_id)
			return None

		return {
			"id": str(row["id"]),
			"platform": row["platform"] or "",
			"login_identifier": row["login_identifier"] or "",
			"encrypted_secret": row["encrypted_secret"],
			"credential_type": row["credential_type"] or "platform",
		}

	# ── VALET credential format (iv + tag + ciphertext, scrypt-derived key)

	async def load_valet_credentials(self, user_id: str, domain: str | None = None) -> list[dict[str, Any]]:
		"""Load VALET-format platform credentials for a user.

		These are stored in ``platform_credentials`` (VALET table, no gh_ prefix)
		with scrypt-derived AES-256-GCM encryption.
		"""
		pool = self._require_pool()

		if domain:
			rows = await pool.fetch(
				"""
				SELECT id, platform, domain, login_identifier, encrypted_secret
				FROM platform_credentials
				WHERE user_id = $1::uuid AND domain = $2
				ORDER BY updated_at DESC
				""",
				uuid.UUID(user_id) if isinstance(user_id, str) else user_id,
				domain.lower(),
			)
		else:
			rows = await pool.fetch(
				"""
				SELECT id, platform, domain, login_identifier, encrypted_secret
				FROM platform_credentials
				WHERE user_id = $1::uuid
				ORDER BY updated_at DESC
				""",
				uuid.UUID(user_id) if isinstance(user_id, str) else user_id,
			)

		return [dict(row) for row in rows]

	# ── Resume profiles ────────────────────────────────────────────────

	async def load_resume_profile(self, user_id: str) -> dict[str, Any]:
		"""Load the user's default parsed resume from VALET's ``resumes`` table.

		Returns the ``parsed_data`` JSONB column (contains structured resume
		fields: fullName, email, education, workHistory, etc.) plus metadata.

		Raises ValueError if no parsed resume is found.
		"""
		pool = self._require_pool()

		row = await pool.fetchrow(
			"""
			SELECT id, user_id, file_key, parsed_data, parsing_confidence, raw_text
			FROM resumes
			WHERE user_id = $1::uuid
				AND status = 'parsed'
			ORDER BY is_default DESC NULLS LAST, created_at DESC
			LIMIT 1
			""",
			uuid.UUID(user_id) if isinstance(user_id, str) else user_id,
		)

		if row is None:
			raise ValueError(
				f"No parsed resume found for user_id={user_id}. "
				"Ensure a resume has been uploaded and parsed in VALET."
			)

		parsed_data = row["parsed_data"]
		if parsed_data is None:
			raise ValueError(
				f"Resume {row['id']} exists but has no parsed_data. It may still be parsing."
			)

		# asyncpg returns jsonb as a string; parse it if needed
		if isinstance(parsed_data, str):
			parsed_data = json.loads(parsed_data)

		return {
			"resume_id": str(row["id"]),
			"user_id": str(row["user_id"]),
			"file_key": row["file_key"],
			"parsed_data": parsed_data,
			"parsing_confidence": row["parsing_confidence"],
			"raw_text": row["raw_text"],
		}

	# ── LISTEN/NOTIFY for HITL signals ────────────────────────────────

	async def listen_for_signals(
		self,
		job_id: str,
		callback: Any,
	) -> asyncpg.Connection:
		"""Subscribe to HITL signals for a specific job via Postgres LISTEN.

		The channel name is ``gh_job_signal_{job_id}`` (hyphens replaced with
		underscores). VALET sends NOTIFY on this channel when the user clicks
		"resume" or "cancel" in the UI.

		Returns the connection (caller must close it or call UNLISTEN).
		"""
		pool = self._require_pool()
		conn = await pool.acquire()

		channel = f"gh_job_signal_{job_id.replace('-', '_')}"

		async def _listener(
			conn: asyncpg.Connection,
			pid: int,
			channel: str,
			payload: str,
		) -> None:
			try:
				data = json.loads(payload) if payload else {}
			except json.JSONDecodeError:
				data = {"raw": payload}
			await callback(data)

		await conn.add_listener(channel, _listener)  # type: ignore[arg-type]
		logger.info("database.listening", channel=channel, job_id=job_id)
		return conn

	async def unlisten(self, conn: asyncpg.Connection, job_id: str) -> None:
		"""Stop listening on a job signal channel and release the connection."""
		channel = f"gh_job_signal_{job_id.replace('-', '_')}"
		try:
			await conn.execute(f"UNLISTEN {channel}")
		except Exception:
			pass
		try:
			await self._require_pool().release(conn)
		except Exception:
			pass

	async def send_signal(self, job_id: str, signal: dict[str, Any]) -> None:
		"""Send a NOTIFY signal for a job (used by VALET or test harness)."""
		pool = self._require_pool()
		channel = f"gh_job_signal_{job_id.replace('-', '_')}"
		await pool.execute(
			f"SELECT pg_notify('{channel}', $1)",
			json.dumps(signal),
		)


# ── Helpers ───────────────────────────────────────────────────────────────


def _redact_dsn(dsn: str) -> str:
	"""Redact password from a DSN for safe logging."""
	try:
		# Simple redaction: replace :password@ with :***@
		import re
		return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)
	except Exception:
		return "<redacted>"
