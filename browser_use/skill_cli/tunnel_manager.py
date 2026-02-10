"""Cloudflared tunnel binary management.

This module manages the cloudflared binary for tunnel support.
Cloudflared must be installed via install.sh or manually by the user.
"""

import logging
import shutil
from typing import Any

logger = logging.getLogger(__name__)


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
