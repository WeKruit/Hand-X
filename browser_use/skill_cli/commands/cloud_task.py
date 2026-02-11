"""Cloud Task API client for browser-use CLI.

This module provides async functions to interact with the Browser-Use Cloud API
for creating and managing cloud-based agent tasks.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from browser_use.skill_cli.api_key import require_api_key

logger = logging.getLogger(__name__)

API_BASE = 'https://api.browser-use.com/api/v2'


def _get_headers() -> dict[str, str]:
	"""Get headers with API key for authenticated requests."""
	api_key = require_api_key('Cloud tasks')
	return {
		'X-Browser-Use-API-Key': api_key,
		'Content-Type': 'application/json',
	}


def _api_request(method: str, endpoint: str, body: dict | None = None) -> dict:
	"""Make a synchronous request to the Cloud API.

	Returns dict with 'success', 'data' or 'error'.
	"""
	url = f'{API_BASE}{endpoint}'
	headers = _get_headers()

	data = json.dumps(body).encode() if body else None
	req = urllib.request.Request(url, data=data, headers=headers, method=method)

	try:
		with urllib.request.urlopen(req) as resp:
			if resp.status == 204:  # No content
				return {'success': True, 'data': {}}
			return {'success': True, 'data': json.loads(resp.read().decode())}
	except urllib.error.HTTPError as e:
		try:
			error_body = json.loads(e.read().decode())
			error_msg = error_body.get('detail', str(e))
		except Exception:
			error_msg = str(e)
		return {'success': False, 'error': f'{e.code}: {error_msg}'}
	except urllib.error.URLError as e:
		return {'success': False, 'error': f'Connection error: {e.reason}'}


async def create_session(
	profile_id: str | None = None,
	proxy_country: str | None = None,
	keep_alive: bool = False,
) -> dict[str, Any]:
	"""Create a cloud browser session via API.

	Args:
		profile_id: Cloud profile ID for persistent auth/cookies
		proxy_country: Proxy country code (us, gb, de, etc.)
		keep_alive: Keep session alive after task completes

	Returns:
		Session response with 'id', 'status', 'liveUrl', etc.
	"""
	payload: dict[str, Any] = {}

	if profile_id:
		payload['profileId'] = profile_id
	if proxy_country:
		payload['proxyCountryCode'] = proxy_country
	if keep_alive:
		payload['keepAlive'] = True

	result = _api_request('POST', '/sessions', payload)
	if not result['success']:
		raise RuntimeError(f'Failed to create session: {result["error"]}')

	return result['data']


async def list_sessions(
	limit: int = 10,
	status: str | None = None,
) -> list[dict[str, Any]]:
	"""List cloud browser sessions.

	Args:
		limit: Maximum number of sessions to return (1-100)
		status: Filter by status ('active' or 'stopped')

	Returns:
		List of session objects
	"""
	params = [f'pageSize={min(limit, 100)}']
	if status:
		params.append(f'filterBy={status}')

	query = '&'.join(params)
	result = _api_request('GET', f'/sessions?{query}')
	if not result['success']:
		raise RuntimeError(f'Failed to list sessions: {result["error"]}')

	data = result['data']
	if isinstance(data, list):
		return data
	return data.get('items', [])


async def get_session(session_id: str) -> dict[str, Any]:
	"""Get details of a specific session.

	Args:
		session_id: Session ID to retrieve

	Returns:
		Session object with full details
	"""
	result = _api_request('GET', f'/sessions/{session_id}')
	if not result['success']:
		raise RuntimeError(f'Failed to get session: {result["error"]}')

	return result['data']


async def stop_session(session_id: str) -> dict[str, Any]:
	"""Stop a cloud session (keeps session history).

	Args:
		session_id: Session ID to stop

	Returns:
		Updated session object
	"""
	result = _api_request('PATCH', f'/sessions/{session_id}', {'action': 'stop'})
	if not result['success']:
		raise RuntimeError(f'Failed to stop session: {result["error"]}')

	return result['data']


async def stop_sessions_parallel(session_ids: list[str]) -> tuple[list[str], list[dict]]:
	"""Stop multiple cloud sessions in parallel using thread pool.

	Args:
		session_ids: List of session IDs to stop

	Returns:
		Tuple of (stopped_ids, errors) where errors is list of {id, error} dicts
	"""

	def _stop_one_sync(session_id: str) -> tuple[str, str | None]:
		"""Stop one session synchronously, return (id, error_or_none)."""
		try:
			result = _api_request('PATCH', f'/sessions/{session_id}', {'action': 'stop'})
			if not result['success']:
				return (session_id, result['error'])
			return (session_id, None)
		except Exception as e:
			return (session_id, str(e))

	# Run all stops in parallel using thread pool
	results = await asyncio.gather(*[asyncio.to_thread(_stop_one_sync, sid) for sid in session_ids])

	stopped = []
	errors = []
	for session_id, error in results:
		if error is None:
			stopped.append(session_id)
		else:
			errors.append({'id': session_id, 'error': error})

	return stopped, errors


async def delete_session(session_id: str) -> dict[str, Any]:
	"""Delete a cloud session and all its tasks.

	Args:
		session_id: Session ID to delete

	Returns:
		Empty dict on success
	"""
	result = _api_request('DELETE', f'/sessions/{session_id}')
	if not result['success']:
		raise RuntimeError(f'Failed to delete session: {result["error"]}')

	return result['data']


async def create_task(
	task: str,
	llm: str | None = None,
	session_id: str | None = None,
	profile_id: str | None = None,
	proxy_country: str | None = None,
	max_steps: int = 100,
	flash_mode: bool = False,
	thinking: bool = False,
	vision: bool | None = None,
	keep_alive: bool = False,
) -> dict[str, Any]:
	"""Create a cloud task via API.

	Args:
		task: Task description for the agent
		llm: LLM model identifier (gpt-4o, claude-opus-4-5, gemini-2.0-flash)
		session_id: Existing session ID to use
		profile_id: Cloud profile ID (will create session if needed)
		proxy_country: Proxy country code (will create session if needed)
		max_steps: Maximum agent steps
		flash_mode: Enable flash mode for faster execution
		thinking: Enable extended reasoning mode
		vision: Enable/disable vision (None = default)
		keep_alive: Keep session alive after task

	Returns:
		Task response with 'id', 'sessionId', etc.
	"""
	# Create session if profile or proxy specified and no session_id
	if (profile_id or proxy_country) and not session_id:
		session = await create_session(
			profile_id=profile_id,
			proxy_country=proxy_country,
			keep_alive=keep_alive,
		)
		session_id = session['id']
		logger.info(f'Created cloud session: {session_id}')

	payload: dict[str, Any] = {
		'task': task,
		'maxSteps': max_steps,
	}

	if llm:
		payload['llm'] = llm
	if session_id:
		payload['sessionId'] = session_id
	if flash_mode:
		payload['flashMode'] = True
	if thinking:
		payload['thinking'] = True
	if vision is not None:
		payload['vision'] = vision

	result = _api_request('POST', '/tasks', payload)
	if not result['success']:
		raise RuntimeError(f'Failed to create task: {result["error"]}')

	return result['data']


async def get_task_status(task_id: str) -> dict[str, Any]:
	"""Get lightweight task status.

	Args:
		task_id: Task ID to check

	Returns:
		Status response with 'id', 'status', 'output', 'cost', etc.
	"""
	result = _api_request('GET', f'/tasks/{task_id}/status')
	if not result['success']:
		raise RuntimeError(f'Failed to get task status: {result["error"]}')

	return result['data']


async def get_task(task_id: str) -> dict[str, Any]:
	"""Get full task details.

	Args:
		task_id: Task ID to get

	Returns:
		Full task response with all details
	"""
	result = _api_request('GET', f'/tasks/{task_id}')
	if not result['success']:
		raise RuntimeError(f'Failed to get task: {result["error"]}')

	return result['data']


async def stop_task(task_id: str) -> dict[str, Any]:
	"""Stop a running task.

	Args:
		task_id: Task ID to stop

	Returns:
		Response confirming stop action
	"""
	result = _api_request('PATCH', f'/tasks/{task_id}', {'action': 'stop'})
	if not result['success']:
		raise RuntimeError(f'Failed to stop task: {result["error"]}')

	return result['data']


async def get_task_logs(task_id: str) -> dict[str, Any]:
	"""Get task execution logs.

	Args:
		task_id: Task ID to get logs for

	Returns:
		Response with 'downloadUrl' for logs
	"""
	result = _api_request('GET', f'/tasks/{task_id}/logs')
	if not result['success']:
		raise RuntimeError(f'Failed to get task logs: {result["error"]}')

	return result['data']


async def list_tasks(limit: int = 10, status: str | None = None) -> list[dict[str, Any]]:
	"""List recent tasks.

	Args:
		limit: Maximum number of tasks to return
		status: Filter by status (running, finished, stopped, failed)

	Returns:
		List of task summaries
	"""
	# Fetch more if filtering client-side to ensure we get enough results
	fetch_limit = limit * 3 if status else limit
	result = _api_request('GET', f'/tasks?pageSize={fetch_limit}')
	if not result['success']:
		raise RuntimeError(f'Failed to list tasks: {result["error"]}')

	# API might return items in a wrapper or directly
	data = result['data']
	if isinstance(data, list):
		tasks = data
	else:
		tasks = data.get('items', [])

	# Filter by status if specified (client-side filtering)
	if status:
		# Normalize status names (API uses 'started' for running)
		status_map = {'running': ['started', 'running']}
		match_statuses = status_map.get(status, [status])
		tasks = [t for t in tasks if t.get('status') in match_statuses]
		tasks = tasks[:limit]

	return tasks


async def poll_until_complete(
	task_id: str,
	stream: bool = False,
	poll_interval: float = 1.0,
) -> dict[str, Any]:
	"""Poll task status until finished.

	Args:
		task_id: Task ID to poll
		stream: If True, print status updates as they happen
		poll_interval: Seconds between polls

	Returns:
		Final task status
	"""
	last_status = None

	while True:
		status = await get_task_status(task_id)
		current_status = status.get('status')

		if stream and current_status != last_status:
			print(f'Status: {current_status}')
			last_status = current_status

		if current_status in ('finished', 'stopped', 'failed'):
			return status

		await asyncio.sleep(poll_interval)
