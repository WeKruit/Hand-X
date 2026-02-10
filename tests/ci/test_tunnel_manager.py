"""Tests for TunnelManager - cloudflared binary management."""

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from browser_use.skill_cli.tunnel_manager import TunnelManager, get_tunnel_manager


@pytest.fixture
def tunnel_manager():
	"""Create a fresh TunnelManager instance for testing."""
	return TunnelManager()


def test_tunnel_manager_system_cloudflared(tunnel_manager):
	"""Test that system cloudflared is preferred."""
	with patch('shutil.which', return_value='/usr/local/bin/cloudflared'):
		binary_path = tunnel_manager.get_binary_path()
		assert binary_path == '/usr/local/bin/cloudflared'
		assert tunnel_manager._installation_status == 'system'


def test_tunnel_manager_caches_result(tunnel_manager):
	"""Test that binary path is cached after first call."""
	with patch('shutil.which', return_value='/usr/local/bin/cloudflared'):
		path1 = tunnel_manager.get_binary_path()
		# Reset shutil.which to ensure it's not called again
		with patch('shutil.which', side_effect=Exception('Should be cached')):
			path2 = tunnel_manager.get_binary_path()
		assert path1 == path2


def test_tunnel_manager_pycloudflared_fallback(tunnel_manager):
	"""Test that pycloudflared is used when system cloudflared not found."""
	with patch('shutil.which', return_value=None):
		# Mock the pycloudflared module
		mock_cloudflared_path = MagicMock(return_value=Path('/home/user/.cache/pycloudflared/cloudflared'))
		mock_module = MagicMock()
		mock_module.cloudflared_path = mock_cloudflared_path

		with patch.dict('sys.modules', {'pycloudflared': mock_module}):
			binary_path = tunnel_manager.get_binary_path()
			assert str(binary_path) == '/home/user/.cache/pycloudflared/cloudflared'
			assert tunnel_manager._installation_status == 'pycloudflared'


def test_tunnel_manager_is_available_cached(tunnel_manager):
	"""Test is_available check with cached binary path."""
	tunnel_manager._binary_path = '/usr/local/bin/cloudflared'
	assert tunnel_manager.is_available() is True


def test_tunnel_manager_is_available_system(tunnel_manager):
	"""Test is_available check finds system cloudflared."""
	with patch('shutil.which', return_value='/usr/local/bin/cloudflared'):
		assert tunnel_manager.is_available() is True


def test_tunnel_manager_is_available_not_found(tunnel_manager):
	"""Test is_available when cloudflared not found."""
	with patch('shutil.which', return_value=None):
		assert tunnel_manager.is_available() is False


def test_tunnel_manager_status_system(tunnel_manager):
	"""Test get_status returns correct info for system cloudflared."""
	with patch('shutil.which', return_value='/usr/local/bin/cloudflared'):
		tunnel_manager.get_binary_path()  # Initialize
		status = tunnel_manager.get_status()
		assert status['available'] is True
		assert status['source'] == 'system'
		assert status['path'] == '/usr/local/bin/cloudflared'


def test_tunnel_manager_status_not_initialized(tunnel_manager):
	"""Test get_status before initialization."""
	with patch('shutil.which', return_value=None):
		status = tunnel_manager.get_status()
		assert status['available'] is True
		assert 'pycloudflared' in status['source']
		assert 'download' in status['note']


def test_get_tunnel_manager_singleton():
	"""Test that get_tunnel_manager returns a singleton."""
	# Reset the global singleton
	import browser_use.skill_cli.tunnel_manager as tm_module

	tm_module._tunnel_manager = None

	mgr1 = get_tunnel_manager()
	mgr2 = get_tunnel_manager()
	assert mgr1 is mgr2
