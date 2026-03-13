"""Abstract base class for browser engine providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from browser_use.browser.profile import BrowserProfile


class BrowserProvider(ABC):
	"""Abstract browser engine provider.

	Decouples the agent from the specific browser engine, enabling
	engine swapping (e.g., Chromium -> Camoufox/Firefox) without
	changing agent code.
	"""

	@abstractmethod
	async def launch(self, profile: BrowserProfile) -> tuple[str, int | None]:
		"""Launch browser and return (cdp_url, process_pid_or_none).

		Args:
			profile: Browser profile with launch configuration.

		Returns:
			Tuple of (cdp_url, process_pid_or_none).
		"""
		...

	@abstractmethod
	async def kill(self) -> None:
		"""Kill the browser process."""
		...

	@abstractmethod
	def get_default_args(self, profile: BrowserProfile) -> list[str]:
		"""Get engine-specific launch arguments.

		Args:
			profile: Browser profile to derive arguments from.

		Returns:
			List of CLI arguments for the browser engine.
		"""
		...

	@property
	@abstractmethod
	def engine_name(self) -> str:
		"""Return engine identifier: 'chromium' or 'firefox'."""
		...

	@property
	@abstractmethod
	def supports_cdp(self) -> bool:
		"""Whether this engine supports CDP protocol."""
		...
