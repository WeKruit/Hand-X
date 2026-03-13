"""Chromium browser engine provider.

Wraps the same Chrome binary finding, argument building, and subprocess
spawning logic used by LocalBrowserWatchdog. This is a thin abstraction
layer -- the watchdog continues to work as before.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from browser_use.browser.providers.base import BrowserProvider
from browser_use.utils import logger

if TYPE_CHECKING:
	from browser_use.browser.profile import BrowserProfile


class ChromiumProvider(BrowserProvider):
	"""Chromium browser engine provider.

	Delegates to the same code paths used by LocalBrowserWatchdog:
	- Chrome binary discovery via _find_installed_browser_path()
	- Argument building via profile.get_args()
	- Subprocess spawning via asyncio.create_subprocess_exec()

	This provider does NOT replace LocalBrowserWatchdog. It is a
	parallel abstraction that can be used when engine-swapping is
	needed (e.g., switching between Chromium and Firefox/Camoufox).
	"""

	def __init__(self) -> None:
		self._process: psutil.Process | None = None
		self._temp_dirs: list[Path] = []

	@property
	def engine_name(self) -> str:
		"""Return engine identifier."""
		return 'chromium'

	@property
	def supports_cdp(self) -> bool:
		"""Chromium fully supports CDP."""
		return True

	async def launch(self, profile: BrowserProfile) -> tuple[str, int | None]:
		"""Launch Chromium browser and return (cdp_url, pid).

		Uses the same logic as LocalBrowserWatchdog._launch_browser():
		1. Build launch args from profile.get_args()
		2. Find Chrome binary (custom path or system search)
		3. Spawn subprocess
		4. Wait for CDP to be ready

		Args:
			profile: Browser profile with launch configuration.

		Returns:
			Tuple of (cdp_url, process_pid_or_none).
		"""
		from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog

		# Build args from profile (same as watchdog)
		launch_args = profile.get_args()

		# Add debugging port
		debug_port = LocalBrowserWatchdog._find_free_port()
		launch_args.append(f'--remote-debugging-port={debug_port}')

		# Find browser executable (same priority as watchdog)
		if profile.executable_path:
			browser_path = profile.executable_path
		else:
			browser_path = LocalBrowserWatchdog._find_installed_browser_path(channel=profile.channel)
			if not browser_path:
				raise RuntimeError(
					'No local Chrome/Chromium install found. '
					'Set executable_path in BrowserProfile or install Chrome.'
				)

		logger.debug(f'[ChromiumProvider] Launching {browser_path} on CDP port {debug_port}')

		# Spawn subprocess (same as watchdog)
		subprocess = await asyncio.create_subprocess_exec(
			browser_path,
			*launch_args,
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
		)

		self._process = psutil.Process(subprocess.pid)

		# Wait for CDP readiness (same as watchdog)
		cdp_url = await LocalBrowserWatchdog._wait_for_cdp_url(debug_port)

		return cdp_url, subprocess.pid

	async def kill(self) -> None:
		"""Kill the Chromium subprocess and clean up temp dirs."""
		if self._process:
			try:
				self._process.terminate()
				# Wait up to 5 seconds for graceful shutdown
				for _ in range(50):
					if not self._process.is_running():
						break
					await asyncio.sleep(0.1)
				# Force kill if still running
				if self._process.is_running():
					self._process.kill()
					await asyncio.sleep(0.1)
			except psutil.NoSuchProcess:
				pass
			except Exception:
				pass
			finally:
				self._process = None

		# Clean up temp directories
		for temp_dir in self._temp_dirs:
			try:
				if 'browseruse-tmp-' in str(temp_dir):
					shutil.rmtree(temp_dir, ignore_errors=True)
			except Exception:
				pass
		self._temp_dirs.clear()

	def get_default_args(self, profile: BrowserProfile) -> list[str]:
		"""Get Chromium launch arguments from the profile.

		Delegates to profile.get_args() which handles all the
		argument compilation logic (defaults, headless, security, etc.).

		Args:
			profile: Browser profile to derive arguments from.

		Returns:
			List of Chrome CLI arguments.
		"""
		return profile.get_args()
