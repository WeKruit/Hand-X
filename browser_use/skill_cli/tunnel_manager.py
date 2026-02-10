"""Cloudflared tunnel binary management via pycloudflared.

This module manages the cloudflared binary for tunnel support, following
the same pattern as Playwright's Chromium auto-install.

Preference order:
1. System cloudflared (if user installed via brew/apt/winget)
2. pycloudflared (auto-downloaded from official Cloudflare releases)
"""

import logging
import shutil
from typing import Any, Literal

logger = logging.getLogger(__name__)


class TunnelManager:
	"""Manages cloudflared binary installation and execution.

	Respects user's explicit choice (system install) while providing
	automatic fallback for seamless first-time experience.
	"""

	def __init__(self) -> None:
		self._binary_path: str | None = None
		self._installation_status: Literal['system', 'pycloudflared', 'none'] = 'none'

	def get_binary_path(self) -> str:
		"""Get cloudflared binary path, installing if needed.

		Returns:
			Absolute path to cloudflared binary

		Raises:
			RuntimeError: If installation fails (network, disk space, etc.)
		"""
		# Cached result from previous call
		if self._binary_path:
			return self._binary_path

		# Check system installation first (user preference)
		system_binary = shutil.which('cloudflared')
		if system_binary:
			logger.info('Using system cloudflared: %s', system_binary)
			self._binary_path = system_binary
			self._installation_status = 'system'
			return system_binary

		# Fallback to pycloudflared (auto-install)
		try:
			from pycloudflared import cloudflared_path  # type: ignore

			# First import triggers auto-download if needed
			logger.info('📦 Downloading cloudflared (~20MB, one-time setup)...')
			binary = cloudflared_path()

			logger.info('✓ Cloudflared ready: %s', binary)
			self._binary_path = str(binary)
			self._installation_status = 'pycloudflared'
			return self._binary_path

		except ImportError:
			# Should never happen (pycloudflared is in dependencies)
			raise RuntimeError(
				'pycloudflared not installed. This is a bug - '
				'please report to browser-use maintainers.'
			)
		except Exception as e:
			# Network failure, disk full, platform unsupported, etc.
			raise RuntimeError(
				f'Failed to initialize cloudflared: {e}\n\n'
				'Possible causes:\n'
				'  - Network error during download\n'
				'  - Insufficient disk space (~20MB needed)\n'
				'  - Platform not supported (ARM Mac needs Rosetta 2)\n\n'
				'Workaround - install manually:\n'
				'  macOS:   brew install cloudflared\n'
				'  Linux:   https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/\n'
				'  Windows: winget install Cloudflare.cloudflared\n\n'
				'Then retry: browser-use tunnel <port>'
			) from e

	def is_available(self) -> bool:
		"""Check if cloudflared is available (without triggering download)."""
		if self._binary_path:
			return True
		return shutil.which('cloudflared') is not None

	def get_status(self) -> dict[str, Any]:
		"""Get tunnel capability status for doctor command."""
		if self._installation_status == 'system':
			return {
				'available': True,
				'source': 'system',
				'path': self._binary_path,
				'note': 'Using system cloudflared (user installed)',
			}
		elif self._installation_status == 'pycloudflared':
			return {
				'available': True,
				'source': 'pycloudflared',
				'path': self._binary_path,
				'note': 'Auto-downloaded from Cloudflare releases',
			}

		# Not yet initialized - check what would happen
		system_binary = shutil.which('cloudflared')
		if system_binary:
			return {
				'available': True,
				'source': 'system (not yet used)',
				'path': system_binary,
				'note': 'Will use system cloudflared',
			}

		# Would auto-download on first tunnel
		return {
			'available': True,
			'source': 'pycloudflared (will auto-download)',
			'path': None,
			'note': 'Will download on first tunnel (~20MB, one-time)',
		}


# Global singleton instance
_tunnel_manager: TunnelManager | None = None


def get_tunnel_manager() -> TunnelManager:
	"""Get the global TunnelManager instance (singleton pattern)."""
	global _tunnel_manager
	if _tunnel_manager is None:
		_tunnel_manager = TunnelManager()
	return _tunnel_manager
