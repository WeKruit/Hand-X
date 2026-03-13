"""Camoufox (anti-detect Firefox) browser engine provider.

Launches Camoufox via its Python library and exposes a WebSocket endpoint
for Playwright-based connection. Camoufox is a Firefox fork with built-in
anti-fingerprinting; it uses the Juggler protocol (not CDP) under the hood,
but provides a Playwright-compatible WebSocket server.

Install: pip install camoufox && python -m camoufox fetch
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from browser_use.browser.providers.base import BrowserProvider
from browser_use.utils import logger

if TYPE_CHECKING:
	from browser_use.browser.profile import BrowserProfile


class CamoufoxProvider(BrowserProvider):
	"""Launches Camoufox (anti-detect Firefox) browser.

	Camoufox is a Firefox fork with engine-level anti-fingerprinting.
	It wraps Playwright's Firefox launcher, so most stealth patches are
	built in and fewer JS-level patches are needed compared to Chromium.

	Two launch modes:
	1. Library mode (preferred): Uses the camoufox Python package to
	   launch via ``AsyncCamoufox`` context manager, returning a
	   Playwright ``Browser`` object directly.
	2. Subprocess mode (fallback): Launches the camoufox binary with
	   ``--remote-debugging-port`` and waits for the endpoint.

	Since browser-use's ``BrowserSession`` connects via CDP WebSocket,
	this provider uses Camoufox's ``launch_server()`` to expose a
	WebSocket endpoint that Playwright can connect to.
	"""

	def __init__(self) -> None:
		self._process: asyncio.subprocess.Process | None = None
		self._server: Any | None = None  # Camoufox server instance
		self._ws_endpoint: str | None = None

	@property
	def engine_name(self) -> str:
		"""Return engine identifier."""
		return 'firefox'

	@property
	def supports_cdp(self) -> bool:
		"""Camoufox uses Juggler (Playwright Firefox protocol), not CDP.

		It exposes a Playwright-compatible WebSocket, but the underlying
		protocol is NOT Chrome DevTools Protocol. Some CDP-specific
		features (Fetch domain, DOM domain commands) may not work.
		"""
		return False

	async def launch(self, profile: BrowserProfile) -> tuple[str, int | None]:
		"""Launch Camoufox and return (ws_endpoint, pid).

		Attempts to use the camoufox Python library first. Falls back
		to subprocess launch if the library is not available.

		Args:
			profile: Browser profile with launch configuration.

		Returns:
			Tuple of (websocket_endpoint_url, process_pid_or_none).

		Raises:
			FileNotFoundError: If camoufox is not installed.
		"""
		# Try library-based launch first
		ws_url, pid = await self._launch_via_library(profile)
		if ws_url:
			return ws_url, pid

		# Fall back to subprocess launch
		return await self._launch_via_subprocess(profile)

	async def _launch_via_library(self, profile: BrowserProfile) -> tuple[str | None, int | None]:
		"""Launch Camoufox using the Python library.

		Returns (None, None) if the library is not installed.
		"""
		try:
			from camoufox.server import launch_server
		except ImportError:
			return None, None

		kwargs: dict[str, Any] = {}

		if profile.headless:
			kwargs['headless'] = True

		# Camoufox launch_server returns a server with ws_endpoint
		try:
			self._server = await launch_server(**kwargs)
			self._ws_endpoint = self._server.ws_endpoint
			logger.debug(f'[CamoufoxProvider] Launched via library, ws={self._ws_endpoint}')
			return self._ws_endpoint, None
		except Exception as e:
			logger.warning(f'[CamoufoxProvider] Library launch failed: {e}, trying subprocess')
			self._server = None
			return None, None

	async def _launch_via_subprocess(self, profile: BrowserProfile) -> tuple[str, int | None]:
		"""Launch Camoufox as a subprocess with remote debugging port.

		Falls back to binary execution if the Python library is unavailable.

		Raises:
			FileNotFoundError: If the camoufox binary cannot be found.
		"""
		binary = self._find_camoufox_binary()
		if binary is None:
			raise FileNotFoundError(
				'Camoufox binary not found. Install via: pip install camoufox && python -m camoufox fetch'
			)

		args = self.get_default_args(profile)

		self._process = await asyncio.create_subprocess_exec(
			binary,
			*args,
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
		)

		port = 9222
		ws_url = f'ws://127.0.0.1:{port}'

		# Wait for the endpoint to become available
		await self._wait_for_ws_endpoint(port, timeout=15.0)

		logger.debug(f'[CamoufoxProvider] Launched subprocess pid={self._process.pid}, ws={ws_url}')
		return ws_url, self._process.pid

	async def kill(self) -> None:
		"""Kill the Camoufox browser process and clean up."""
		# Clean up library-launched server
		if self._server is not None:
			try:
				await self._server.close()
			except Exception:
				pass
			self._server = None

		# Clean up subprocess
		if self._process is not None and self._process.returncode is None:
			try:
				self._process.terminate()
				await asyncio.wait_for(self._process.wait(), timeout=5.0)
			except asyncio.TimeoutError:
				try:
					self._process.kill()
				except ProcessLookupError:
					pass
			except ProcessLookupError:
				pass
			self._process = None

		self._ws_endpoint = None

	def get_default_args(self, profile: BrowserProfile) -> list[str]:
		"""Get Firefox-style launch arguments for Camoufox.

		Firefox uses different flag conventions than Chromium:
		- ``-headless`` instead of ``--headless``
		- ``-profile <dir>`` instead of ``--user-data-dir=<dir>``
		- ``--remote-debugging-port=<port>`` (same as Chrome)

		Args:
			profile: Browser profile to derive arguments from.

		Returns:
			List of CLI arguments for the Camoufox binary.
		"""
		args: list[str] = []

		args.append('--remote-debugging-port=9222')

		if profile.headless:
			args.append('-headless')

		if profile.user_data_dir:
			args.extend(['-profile', str(profile.user_data_dir)])

		if profile.window_size:
			args.append(f'--width={profile.window_size.width}')
			args.append(f'--height={profile.window_size.height}')

		return args

	def _find_camoufox_binary(self) -> str | None:
		"""Find the camoufox binary in common locations.

		Searches:
		1. PATH (via shutil.which)
		2. ~/.camoufox/camoufox
		3. ~/.local/bin/camoufox

		Returns:
			Path to the binary, or None if not found.
		"""
		# Check PATH
		binary = shutil.which('camoufox')
		if binary:
			return binary

		# Check common install locations
		home = Path.home()
		candidates = [
			home / '.camoufox' / 'camoufox',
			home / '.local' / 'bin' / 'camoufox',
			# pip install camoufox puts it in the venv
			Path(sys.prefix) / 'bin' / 'camoufox',
		]
		for path in candidates:
			if path.exists() and path.is_file():
				return str(path)

		return None

	async def _wait_for_ws_endpoint(self, port: int, timeout: float = 15.0) -> None:
		"""Wait for the WebSocket endpoint to become available.

		Polls the HTTP endpoint at the given port until it responds
		or the timeout is reached.

		Args:
			port: The port to check.
			timeout: Maximum seconds to wait.

		Raises:
			TimeoutError: If the endpoint is not available within the timeout.
		"""
		import urllib.request
		import urllib.error

		deadline = asyncio.get_event_loop().time() + timeout
		url = f'http://127.0.0.1:{port}/json/version'

		while asyncio.get_event_loop().time() < deadline:
			try:
				req = urllib.request.Request(url, method='GET')
				with urllib.request.urlopen(req, timeout=1) as resp:
					if resp.status == 200:
						return
			except (urllib.error.URLError, OSError, ConnectionError):
				pass
			await asyncio.sleep(0.3)

		raise TimeoutError(f'Camoufox endpoint not available on port {port} after {timeout}s')

	@classmethod
	def is_available(cls) -> bool:
		"""Check if Camoufox is installed and available.

		Returns:
			True if either the camoufox Python package or binary is found.
		"""
		# Check for Python library
		try:
			import camoufox  # noqa: F401

			return True
		except ImportError:
			pass

		# Check for binary
		provider = cls()
		return provider._find_camoufox_binary() is not None
