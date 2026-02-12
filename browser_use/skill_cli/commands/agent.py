"""Agent task command handler."""

import logging
import os
from typing import Any

from browser_use.skill_cli.api_key import APIKeyRequired, require_api_key
from browser_use.skill_cli.sessions import SessionInfo

logger = logging.getLogger(__name__)

# Cloud-only flags that only work in remote mode
CLOUD_ONLY_FLAGS = [
	'session_id',
	'proxy_country',
	'wait',
	'stream',
	'flash',
	'keep_alive',
	'thinking',
	'start_url',
	'metadata',
	'secret',
	'allowed_domain',
	'skill_id',
	'structured_output',
	'judge',
	'judge_ground_truth',
]


async def handle(session: SessionInfo, params: dict[str, Any]) -> Any:
	"""Handle agent run command.

	Routes based on browser mode:
	- Remote mode (--browser remote): Uses Cloud API with US proxy by default
	- Local mode (default): Uses local browser-use agent
	"""
	task = params.get('task')
	if not task:
		return {'success': False, 'error': 'No task provided'}

	# Route based on browser mode
	if session.browser_mode == 'remote':
		# Remote mode requires Browser-Use API key
		try:
			require_api_key('Cloud agent tasks')
		except APIKeyRequired as e:
			return {'success': False, 'error': str(e)}
		return await _handle_cloud_task(params)
	else:
		# Check if user tried to use cloud-only flags in local mode
		used_cloud_flags = [f for f in CLOUD_ONLY_FLAGS if params.get(f)]
		if used_cloud_flags:
			from browser_use.skill_cli.install_config import is_mode_available

			flags_str = ', '.join(f'--{f.replace("_", "-")}' for f in used_cloud_flags)

			if is_mode_available('remote'):
				# Remote is available, user just needs to use it
				return {
					'success': False,
					'error': f'Cloud-only flags used in local mode: {flags_str}\nUse --browser remote to enable cloud features.',
				}
			else:
				# Remote not installed (--local-only install)
				return {
					'success': False,
					'error': f'Cloud-only flags require remote mode: {flags_str}\n'
					f'Remote mode is not installed. Reinstall with --full to enable:\n'
					f'  curl -fsSL https://browser-use.com/install.sh | bash -s -- --full',
				}
		return await _handle_local_task(session, params)


async def _handle_cloud_task(params: dict[str, Any]) -> Any:
	"""Handle task execution via Cloud API.

	By default uses US proxy for all cloud tasks.
	"""
	from browser_use.skill_cli.commands import cloud_task

	task = params['task']

	# Handle vision flag (--vision vs --no-vision)
	vision: bool | None = None
	if params.get('vision'):
		vision = True
	elif params.get('no_vision'):
		vision = False

	# Parse key=value list params
	metadata = _parse_key_value_list(params.get('metadata'))
	secrets = _parse_key_value_list(params.get('secret'))

	# Build session params - only include what user explicitly set
	session_id = params.get('session_id')
	profile_id = params.get('profile')
	proxy_country = params.get('proxy_country')

	try:
		logger.info(f'Creating cloud task: {task}')

		# Create session first if profile or proxy specified and no session_id
		if (profile_id or proxy_country) and not session_id:
			session = cloud_task.create_session(
				profile_id=profile_id,
				proxy_country=proxy_country,
				keep_alive=params.get('keep_alive'),
			)
			session_id = session.id
			logger.info(f'Created cloud session: {session_id}')

		# Create cloud task - only pass what user explicitly set
		task_response = cloud_task.create_task(
			task=task,
			llm=params.get('llm'),
			session_id=session_id,
			max_steps=params.get('max_steps'),
			flash_mode=params.get('flash'),
			thinking=params.get('thinking'),
			vision=vision,
			start_url=params.get('start_url'),
			metadata=metadata,
			secrets=secrets,
			allowed_domains=params.get('allowed_domain'),
			skill_ids=params.get('skill_id'),
			structured_output=params.get('structured_output'),
			judge=params.get('judge'),
			judge_ground_truth=params.get('judge_ground_truth'),
		)

		task_id = task_response.id
		response_session_id = task_response.session_id

		if not task_id:
			return {
				'success': False,
				'error': 'Cloud API did not return a task ID',
				'task': task,
			}

		logger.info(f'Cloud task created: {task_id}')

		# Return immediately unless --wait is specified
		if not params.get('wait'):
			return {
				'success': True,
				'task_id': task_id,
				'session_id': response_session_id,
				'message': 'Task started. Use "browser-use task status <task_id>" to check progress.',
			}

		# Poll until complete
		logger.info('Waiting for task completion...')
		result = cloud_task.poll_until_complete(task_id, stream=params.get('stream', False))

		return {
			'success': True,
			'task': task,
			'task_id': task_id,
			'session_id': response_session_id,
			'status': result.status,
			'output': result.output,
			'cost': result.cost,
			'done': result.status == 'finished',
		}

	except Exception as e:
		logger.exception(f'Cloud task failed: {e}')
		return {
			'success': False,
			'error': str(e),
			'task': task,
		}


def _parse_key_value_list(items: list[str] | None) -> dict[str, str | None] | None:
	"""Parse a list of 'key=value' strings into a dict."""
	if not items:
		return None
	result: dict[str, str | None] = {}
	for item in items:
		if '=' in item:
			key, value = item.split('=', 1)
			result[key] = value
	return result if result else None


async def _handle_local_task(session: SessionInfo, params: dict[str, Any]) -> Any:
	"""Handle task execution locally with browser-use agent."""
	task = params['task']
	max_steps = params.get('max_steps')
	model = params.get('llm')  # Optional model override

	try:
		# Import agent and LLM
		from browser_use.agent.service import Agent

		# Try to get LLM from environment (with optional model override)
		llm = await get_llm(model=model)
		if llm is None:
			if model:
				return {
					'success': False,
					'error': f'Could not initialize model "{model}". '
					f'Make sure the appropriate API key is set (OPENAI_API_KEY, ANTHROPIC_API_KEY, or GOOGLE_API_KEY).',
				}
			return {
				'success': False,
				'error': 'No LLM configured. Set BROWSER_USE_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, or GOOGLE_API_KEY',
			}

		# Create and run agent
		agent = Agent(
			task=task,
			llm=llm,
			browser_session=session.browser_session,
		)

		logger.info(f'Running local agent task: {task}')
		run_kwargs = {}
		if max_steps is not None:
			run_kwargs['max_steps'] = max_steps
		result = await agent.run(**run_kwargs)

		# Extract result info
		final_result = result.final_result() if result else None

		return {
			'success': True,
			'task': task,
			'steps': len(result) if result else 0,
			'result': str(final_result) if final_result else None,
			'done': result.is_done() if result else False,
		}

	except Exception as e:
		logger.exception(f'Local agent task failed: {e}')
		return {
			'success': False,
			'error': str(e),
			'task': task,
		}


def _get_provider_from_model(model: str) -> str | None:
	"""Determine the provider from a model name.

	Returns 'openai', 'anthropic', 'google', or None if unknown.
	"""
	model_lower = model.lower()

	# Check for explicit provider prefix (e.g., "openai/gpt-4o")
	if '/' in model:
		provider = model.split('/')[0].lower()
		if provider in ('openai', 'anthropic', 'google'):
			return provider

	# Infer from model name patterns
	if model_lower.startswith('gpt-') or model_lower.startswith('o1') or model_lower.startswith('o3'):
		return 'openai'
	elif model_lower.startswith('claude-'):
		return 'anthropic'
	elif model_lower.startswith('gemini-'):
		return 'google'

	return None


async def get_llm(model: str | None = None) -> Any:
	"""Get LLM instance from environment configuration.

	Args:
		model: Optional model name to use. If provided, will instantiate
		       the appropriate provider for that model. If not provided,
		       auto-detects from available API keys.

	Supported model patterns:
		- gpt-*, o1*, o3* → OpenAI (requires OPENAI_API_KEY)
		- claude-* → Anthropic (requires ANTHROPIC_API_KEY)
		- gemini-* → Google (requires GOOGLE_API_KEY)
		- openai/model, anthropic/model, google/model → Explicit provider
	"""
	# If model specified, use that provider
	if model:
		provider = _get_provider_from_model(model)

		# Strip provider prefix if present (e.g., "openai/gpt-4o" → "gpt-4o")
		model_name = model.split('/')[-1] if '/' in model else model

		if provider == 'openai':
			if not os.environ.get('OPENAI_API_KEY'):
				logger.warning(f'Model {model} requires OPENAI_API_KEY')
				return None
			try:
				from langchain_openai import ChatOpenAI  # type: ignore[import-not-found]

				return ChatOpenAI(model=model_name)  # type: ignore[return-value]
			except ImportError:
				logger.warning('langchain-openai not installed')
				return None

		elif provider == 'anthropic':
			if not os.environ.get('ANTHROPIC_API_KEY'):
				logger.warning(f'Model {model} requires ANTHROPIC_API_KEY')
				return None
			try:
				from langchain_anthropic import ChatAnthropic  # type: ignore[import-not-found]

				return ChatAnthropic(model=model_name)  # type: ignore[return-value]
			except ImportError:
				logger.warning('langchain-anthropic not installed')
				return None

		elif provider == 'google':
			if not os.environ.get('GOOGLE_API_KEY'):
				logger.warning(f'Model {model} requires GOOGLE_API_KEY')
				return None
			try:
				from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore[import-not-found]

				return ChatGoogleGenerativeAI(model=model_name)  # type: ignore[return-value]
			except ImportError:
				logger.warning('langchain-google-genai not installed')
				return None

		else:
			logger.warning(f'Unknown model provider for: {model}')
			return None

	# No model specified - auto-detect from available API keys

	# Try ChatBrowserUse first (optimized for browser automation)
	if os.environ.get('BROWSER_USE_API_KEY'):
		try:
			from browser_use.llm import ChatBrowserUse

			return ChatBrowserUse()  # type: ignore[return-value]
		except ImportError:
			pass

	# Try OpenAI
	if os.environ.get('OPENAI_API_KEY'):
		try:
			from langchain_openai import ChatOpenAI  # type: ignore[import-not-found]

			return ChatOpenAI(model='gpt-4o')  # type: ignore[return-value]
		except ImportError:
			pass

	# Try Anthropic
	if os.environ.get('ANTHROPIC_API_KEY'):
		try:
			from langchain_anthropic import ChatAnthropic  # type: ignore[import-not-found]

			return ChatAnthropic(model='claude-sonnet-4-20250514')  # type: ignore[return-value]
		except ImportError:
			pass

	# Try Google
	if os.environ.get('GOOGLE_API_KEY'):
		try:
			from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore[import-not-found]

			return ChatGoogleGenerativeAI(model='gemini-2.0-flash')  # type: ignore[return-value]
		except ImportError:
			pass

	# Try to use browser-use's default LLM setup
	try:
		from browser_use.llm import get_default_llm

		return get_default_llm()  # type: ignore[return-value]
	except Exception:
		pass

	return None
