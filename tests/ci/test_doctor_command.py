"""Tests for doctor command."""

from unittest.mock import patch

import pytest

from browser_use.skill_cli.commands import doctor


@pytest.mark.asyncio
async def test_doctor_healthy():
	"""Test doctor when all checks pass."""
	with patch.object(doctor, '_check_package') as mock_pkg:
		mock_pkg.return_value = {'status': 'ok', 'message': 'browser-use installed'}

		with patch.object(doctor, '_check_browser') as mock_browser:
			mock_browser.return_value = {'status': 'ok', 'message': 'Browser available'}

			with patch.object(doctor, '_check_api_key_config') as mock_api:
				mock_api.return_value = {'status': 'ok', 'message': 'API key configured'}

				with patch.object(doctor, '_check_cloudflared') as mock_cloudflared:
					mock_cloudflared.return_value = {'status': 'ok', 'message': 'Cloudflared available'}

					with patch.object(doctor, '_check_network') as mock_network:
						mock_network.return_value = {'status': 'ok', 'message': 'Network OK'}

						result = await doctor.handle()

						assert result['status'] == 'healthy'
						assert all(c['status'] == 'ok' for c in result['checks'].values())
						assert 'summary' in result


@pytest.mark.asyncio
async def test_doctor_issues():
	"""Test doctor when issues are found."""
	with patch.object(doctor, '_check_package') as mock_pkg:
		mock_pkg.return_value = {'status': 'error', 'message': 'browser-use not installed'}

		with patch.object(doctor, '_check_browser'):
			with patch.object(doctor, '_check_api_key_config'):
				with patch.object(doctor, '_check_cloudflared'):
					with patch.object(doctor, '_check_network'):
						result = await doctor.handle()

						assert result['status'] == 'issues_found'
						assert result['checks']['package']['status'] == 'error'


def test_check_package_installed():
	"""Test _check_package when installed."""
	import browser_use

	result = doctor._check_package()
	assert result['status'] == 'ok'
	assert 'browser-use' in result['message']


def test_check_package_not_installed():
	"""Test _check_package when not installed."""
	with patch('builtins.__import__', side_effect=ImportError):
		result = doctor._check_package()
		assert result['status'] == 'error'
		assert 'not installed' in result['message']


def test_check_api_key_available():
	"""Test _check_api_key_config when API key is available."""
	with patch('browser_use.skill_cli.api_key.check_api_key') as mock_check:
		mock_check.return_value = {'available': True, 'source': 'env'}
		result = doctor._check_api_key_config()
		assert result['status'] == 'ok'


def test_check_api_key_missing():
	"""Test _check_api_key_config when API key is missing."""
	with patch('browser_use.skill_cli.api_key.check_api_key') as mock_check:
		mock_check.return_value = {'available': False}
		result = doctor._check_api_key_config()
		assert result['status'] == 'missing'


def test_check_cloudflared_available():
	"""Test _check_cloudflared when available."""
	with patch('browser_use.skill_cli.tunnel_manager.get_tunnel_manager') as mock_mgr:
		mock_instance = mock_mgr.return_value
		mock_instance.get_status.return_value = {
			'available': True,
			'source': 'system',
			'note': 'System installed',
		}
		result = doctor._check_cloudflared()
		assert result['status'] == 'ok'


def test_check_cloudflared_missing():
	"""Test _check_cloudflared when not available."""
	with patch('browser_use.skill_cli.tunnel_manager.get_tunnel_manager') as mock_mgr:
		mock_instance = mock_mgr.return_value
		mock_instance.get_status.return_value = {
			'available': False,
			'note': 'Not installed',
		}
		result = doctor._check_cloudflared()
		assert result['status'] == 'missing'


def test_summarize_checks_all_ok():
	"""Test _summarize_checks when all checks pass."""
	checks = {
		'check1': {'status': 'ok'},
		'check2': {'status': 'ok'},
		'check3': {'status': 'ok'},
	}
	summary = doctor._summarize_checks(checks)
	assert '3/3' in summary


def test_summarize_checks_mixed():
	"""Test _summarize_checks with mixed results."""
	checks = {
		'check1': {'status': 'ok'},
		'check2': {'status': 'warning'},
		'check3': {'status': 'missing'},
	}
	summary = doctor._summarize_checks(checks)
	assert '1/3' in summary
	assert '1 warning' in summary
	assert '1 missing' in summary
