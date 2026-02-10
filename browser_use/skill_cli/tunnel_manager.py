"""Cloudflared tunnel binary management.

This module manages the cloudflared binary for tunnel support.
Cloudflared must be installed via install.sh or manually by the user.

Tunnels are managed independently of browser sessions - they are purely
a network utility for exposing local ports via Cloudflare quick tunnels.
"""

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Pattern to extract tunnel URL from cloudflared output
_URL_PATTERN = re.compile(r'(https://\S+\.trycloudflare\.com)')


@dataclass
class TunnelInfo:
	"""Information about an active cloudflare tunnel."""

	port: int
	url: str
	process: asyncio.subprocess.Process


# Module-level storage for running tunnels (independent of browser sessions)
_active_tunnels: dict[int, TunnelInfo] = {}


class TunnelManager:
	"""Manages cloudflared binary location."""

	def __init__(self) -> None:
		self._binary_path: str | None = None

	def get_binary_path(self) -> str:
		"""Get cloudflared binary path.

		Returns:
			Absolute path to cloudflared binary

		Raises:
			RuntimeError: If cloudflared is not installed
		"""
		# Cached result from previous call
		if self._binary_path:
			return self._binary_path

		# Check system installation
		system_binary = shutil.which('cloudflared')
		if system_binary:
			logger.info('Using cloudflared: %s', system_binary)
			self._binary_path = system_binary
			return system_binary

		# Not found
		raise RuntimeError(
			'cloudflared not installed.\n\n'
			'Install cloudflared:\n'
			'  macOS:   brew install cloudflared\n'
			'  Linux:   curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o ~/.local/bin/cloudflared && chmod +x ~/.local/bin/cloudflared\n'
			'  Windows: winget install Cloudflare.cloudflared\n\n'
			'Or re-run install.sh which installs cloudflared automatically.\n\n'
			'Then retry: browser-use tunnel <port>'
		)

	def is_available(self) -> bool:
		"""Check if cloudflared is available."""
		if self._binary_path:
			return True
		return shutil.which('cloudflared') is not None

	def get_status(self) -> dict[str, Any]:
		"""Get tunnel capability status for doctor command."""
		system_binary = shutil.which('cloudflared')
		if system_binary:
			return {
				'available': True,
				'source': 'system',
				'path': system_binary,
				'note': 'cloudflared installed',
			}

		return {
			'available': False,
			'source': None,
			'path': None,
			'note': 'cloudflared not installed - run install.sh or install manually',
		}


# Global singleton instance
_tunnel_manager: TunnelManager | None = None


def get_tunnel_manager() -> TunnelManager:
	"""Get the global TunnelManager instance (singleton pattern)."""
	global _tunnel_manager
	if _tunnel_manager is None:
		_tunnel_manager = TunnelManager()
	return _tunnel_manager


# =============================================================================
# Standalone Tunnel Functions (no browser session required)
# =============================================================================


async def start_tunnel(port: int) -> dict[str, Any]:
	"""Start a cloudflare quick tunnel for a local port.

	Args:
		port: Local port to tunnel

	Returns:
		Dict with 'url' and 'port' on success, or 'error' on failure
	"""
	# Check if tunnel already exists for this port
	if port in _active_tunnels:
		info = _active_tunnels[port]
		return {'url': info.url, 'port': port, 'existing': True}

	# Get cloudflared binary
	try:
		tunnel_manager = get_tunnel_manager()
		cloudflared_binary = tunnel_manager.get_binary_path()
	except RuntimeError as e:
		return {'error': str(e)}

	# Spawn cloudflared process
	process = await asyncio.create_subprocess_exec(
		cloudflared_binary,
		'tunnel',
		'--url',
		f'http://localhost:{port}',
		stdout=asyncio.subprocess.DEVNULL,
		stderr=asyncio.subprocess.PIPE,
	)

	# Read stderr lines until we find the tunnel URL
	assert process.stderr is not None
	url: str | None = None
	try:
		deadline = asyncio.get_event_loop().time() + 15
		while asyncio.get_event_loop().time() < deadline:
			try:
				line_bytes = await asyncio.wait_for(process.stderr.readline(), timeout=1.0)
			except TimeoutError:
				continue
			if not line_bytes:
				break
			line = line_bytes.decode(errors='replace')
			match = _URL_PATTERN.search(line)
			if match:
				url = match.group(1)
				break
	except Exception as e:
		process.terminate()
		return {'error': f'Failed to start tunnel: {e}'}

	if url is None:
		process.terminate()
		return {'error': 'Timed out waiting for cloudflare tunnel URL (15s)'}

	# Store tunnel info
	_active_tunnels[port] = TunnelInfo(port=port, url=url, process=process)
	logger.info(f'Tunnel started: localhost:{port} -> {url}')

	return {'url': url, 'port': port}


def list_tunnels() -> dict[str, Any]:
	"""List active tunnels.

	Returns:
		Dict with 'tunnels' list and 'count'
	"""
	tunnels = [{'port': info.port, 'url': info.url} for info in _active_tunnels.values()]
	return {'tunnels': tunnels, 'count': len(tunnels)}


async def stop_tunnel(port: int) -> dict[str, Any]:
	"""Stop a tunnel for a specific port.

	Args:
		port: Port number to stop tunnel for

	Returns:
		Dict with 'stopped' port and 'url' on success, or 'error'
	"""
	if port not in _active_tunnels:
		return {'error': f'No tunnel running on port {port}'}

	info = _active_tunnels.pop(port)
	info.process.terminate()
	try:
		await asyncio.wait_for(info.process.wait(), timeout=5)
	except TimeoutError:
		info.process.kill()
	logger.info(f'Tunnel stopped: localhost:{port}')

	return {'stopped': port, 'url': info.url}


async def stop_all_tunnels() -> dict[str, Any]:
	"""Stop all active tunnels.

	Returns:
		Dict with 'stopped' list of ports
	"""
	stopped = []
	for port in list(_active_tunnels.keys()):
		result = await stop_tunnel(port)
		if 'stopped' in result:
			stopped.append(port)

	return {'stopped': stopped, 'count': len(stopped)}
