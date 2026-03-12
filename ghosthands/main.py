"""GhostHands entry point — starts the worker poller and agent loop."""

import asyncio
import sys

import structlog

from ghosthands.config.settings import settings

logger = structlog.get_logger()


async def run() -> None:
	"""Start the GhostHands worker."""
	logger.info(
		"ghosthands.starting",
		worker_id=settings.worker_id,
		headless=settings.headless,
		poll_interval=settings.poll_interval_seconds,
		max_steps=settings.max_steps_per_job,
		max_budget=settings.max_budget_per_job,
	)

	from ghosthands.worker.poller import run_worker

	await run_worker()


def main() -> None:
	"""Sync entry point for the ``ghosthands`` CLI command."""
	try:
		asyncio.run(run())
	except KeyboardInterrupt:
		logger.info("ghosthands.stopped")
		sys.exit(0)


if __name__ == "__main__":
	main()
