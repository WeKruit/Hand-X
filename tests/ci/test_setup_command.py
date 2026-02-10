"""Tests for setup command."""

import json
from unittest.mock import MagicMock, patch

import pytest

from browser_use.skill_cli.commands import setup


@pytest.mark.asyncio
async def test_setup_local_profile():
	"""Test setup with local profile."""
	with patch('browser_use.skill_cli.commands.setup.run_checks') as mock_checks:
		mock_checks.return_value = {
			'browser_use_package': {'status': 'ok', 'message': 'browser-use installed'},
			'browser': {'status': 'ok', 'message': 'Browser available'},
		}

		with patch('browser_use.skill_cli.commands.setup.plan_actions', return_value=[]):
			with patch('browser_use.skill_cli.commands.setup.execute_actions'):
				with patch('browser_use.skill_cli.commands.setup.validate_setup') as mock_validate:
					mock_validate.return_value = {
						'browser_use_import': 'ok',
						'browser_profile': 'ok',
					}

					result = await setup.handle(
						'setup',
						{
							'profile': 'local',
							'api_key': None,
							'yes': False,
							'json': False,
						},
					)

					assert result['status'] == 'success'
					assert result['profile'] == 'local'
					assert 'checks' in result
					assert 'validation' in result


@pytest.mark.asyncio
async def test_setup_remote_profile():
	"""Test setup with remote profile."""
	with patch('browser_use.skill_cli.commands.setup.run_checks') as mock_checks:
		mock_checks.return_value = {
			'browser_use_package': {'status': 'ok', 'message': 'browser-use installed'},
			'api_key': {'status': 'missing', 'message': 'Not configured'},
			'cloudflared': {'status': 'ok', 'message': 'Will auto-install'},
		}

		with patch('browser_use.skill_cli.commands.setup.plan_actions') as mock_plan:
			mock_plan.return_value = [
				{
					'type': 'configure_api_key',
					'description': 'Configure API key',
					'api_key': 'test_key',
				}
			]

			with patch('browser_use.skill_cli.commands.setup.execute_actions'):
				with patch('browser_use.skill_cli.commands.setup.validate_setup') as mock_validate:
					mock_validate.return_value = {
						'browser_use_import': 'ok',
						'api_key_available': True,
						'cloudflared_available': True,
					}

					result = await setup.handle(
						'setup',
						{
							'profile': 'remote',
							'api_key': 'test_key',
							'yes': True,
							'json': False,
						},
					)

					assert result['status'] == 'success'
					assert result['profile'] == 'remote'


@pytest.mark.asyncio
async def test_setup_invalid_profile():
	"""Test setup with invalid profile."""
	result = await setup.handle(
		'setup',
		{
			'profile': 'invalid',
			'api_key': None,
			'yes': False,
			'json': False,
		},
	)

	assert 'error' in result


def test_plan_actions_no_actions_needed():
	"""Test plan_actions when nothing is needed."""
	checks = {
		'browser_use_package': {'status': 'ok'},
		'browser': {'status': 'ok'},
		'api_key': {'status': 'ok'},
		'cloudflared': {'status': 'ok'},
	}

	actions = setup.plan_actions(checks, 'local', yes=False, api_key=None)
	assert actions == []


def test_plan_actions_install_browser():
	"""Test plan_actions when browser needs installation."""
	checks = {
		'browser_use_package': {'status': 'ok'},
		'browser': {'status': 'error'},
	}

	actions = setup.plan_actions(checks, 'local', yes=False, api_key=None)
	assert any(a['type'] == 'install_browser' for a in actions)


def test_plan_actions_configure_api_key():
	"""Test plan_actions when API key is provided."""
	checks = {
		'api_key': {'status': 'missing'},
	}

	actions = setup.plan_actions(checks, 'remote', yes=True, api_key='test_key')
	assert any(a['type'] == 'configure_api_key' for a in actions)


@pytest.mark.asyncio
async def test_check_browser_available():
	"""Test _check_browser when browser is available."""
	with patch('browser_use.browser.profile.BrowserProfile'):
		result = await setup._check_browser()
		assert result['status'] == 'ok'


@pytest.mark.asyncio
async def test_check_browser_unavailable():
	"""Test _check_browser when browser is not available."""
	with patch('browser_use.browser.profile.BrowserProfile', side_effect=Exception('Not available')):
		result = await setup._check_browser()
		assert result['status'] == 'error'
