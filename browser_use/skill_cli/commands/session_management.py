"""Session management command handlers for browser-use CLI.

Handles: session list, session get, session stop
"""

import argparse
import asyncio
import json
import sys
from typing import Any

from browser_use.skill_cli.api_key import APIKeyRequired, require_api_key
from browser_use.skill_cli.commands import cloud_task


def handle_session_command(args: argparse.Namespace) -> int:
	"""Handle session subcommands.

	Session commands manage cloud sessions and always require the cloud API.

	Args:
		args: Parsed command-line arguments

	Returns:
		Exit code (0 for success, 1 for error)
	"""
	from browser_use.skill_cli.install_config import is_mode_available

	# Check if remote mode is available
	if not is_mode_available('remote'):
		print(
			'Error: Session management requires remote mode.\n'
			'Remote mode is not installed. Reinstall with --full to enable:\n'
			'  curl -fsSL https://browser-use.com/install.sh | bash -s -- --full',
			file=sys.stderr,
		)
		return 1

	# Check API key
	try:
		require_api_key('Cloud sessions')
	except APIKeyRequired as e:
		print(f'Error: {e}', file=sys.stderr)
		return 1

	if args.session_command == 'list':
		return _handle_list(args)
	elif args.session_command == 'get':
		return _handle_get(args)
	elif args.session_command == 'stop':
		return _handle_stop(args)
	else:
		print('Usage: browser-use session <command>')
		print('Commands: list, get <session_id>, stop <session_id>')
		return 1


def _run_async(coro: Any) -> Any:
	"""Run an async coroutine, handling event loop creation."""
	try:
		loop = asyncio.get_event_loop()
		if loop.is_running():
			# If already running, create a new loop
			import concurrent.futures

			with concurrent.futures.ThreadPoolExecutor() as executor:
				future = executor.submit(asyncio.run, coro)
				return future.result()
		return loop.run_until_complete(coro)
	except RuntimeError:
		return asyncio.run(coro)


def _format_duration(started_at: str | None, finished_at: str | None) -> str:
	"""Format duration between two timestamps, or elapsed time if still running."""
	if not started_at:
		return ''

	from datetime import datetime, timezone

	try:
		# Parse ISO format timestamp
		start = datetime.fromisoformat(started_at.replace('Z', '+00:00'))

		if finished_at:
			end = datetime.fromisoformat(finished_at.replace('Z', '+00:00'))
		else:
			end = datetime.now(timezone.utc)

		delta = end - start
		total_seconds = int(delta.total_seconds())

		if total_seconds < 60:
			return f'{total_seconds}s'
		elif total_seconds < 3600:
			minutes = total_seconds // 60
			seconds = total_seconds % 60
			return f'{minutes}m {seconds}s'
		else:
			hours = total_seconds // 3600
			minutes = (total_seconds % 3600) // 60
			return f'{hours}h {minutes}m'
	except Exception:
		return ''


def _handle_list(args: argparse.Namespace) -> int:
	"""Handle 'session list' command."""
	try:
		status_filter = getattr(args, 'status', None)
		sessions = _run_async(cloud_task.list_sessions(limit=args.limit, status=status_filter))
	except Exception as e:
		print(f'Error: {e}', file=sys.stderr)
		return 1

	if getattr(args, 'json', False):
		print(json.dumps(sessions))
	else:
		if not sessions:
			status_msg = f' with status "{status_filter}"' if status_filter else ''
			print(f'No sessions found{status_msg}')
		else:
			header = f'Sessions ({len(sessions)})'
			if status_filter:
				header = f'{status_filter.capitalize()} sessions ({len(sessions)})'
			print(f'{header}:')
			for s in sessions:
				session_id = s.get('id', 'unknown')
				status = s.get('status', 'unknown')
				live_url = s.get('liveUrl')
				started_at = s.get('startedAt')
				finished_at = s.get('finishedAt')
				keep_alive = '🔄' if s.get('keepAlive') else ''

				# Status emoji
				status_emoji = {
					'active': '🟢',
					'stopped': '⏹️',
				}.get(status, '❓')

				# Truncate ID for display
				short_id = session_id[:8] + '...' if len(session_id) > 8 else session_id

				# Build line with duration
				duration = _format_duration(started_at, finished_at)
				line = f'  {status_emoji} {short_id} [{status}]'
				if duration:
					line += f' {duration}'
				if keep_alive:
					line += f' {keep_alive}'
				if live_url and status == 'active':
					line += f'\n      live: {live_url}'
				print(line)

	return 0


def _handle_get(args: argparse.Namespace) -> int:
	"""Handle 'session get <session_id>' command."""
	try:
		session = _run_async(cloud_task.get_session(args.session_id))
	except Exception as e:
		print(f'Error: {e}', file=sys.stderr)
		return 1

	if getattr(args, 'json', False):
		print(json.dumps(session))
	else:
		session_id = session.get('id', args.session_id)
		status = session.get('status', 'unknown')
		live_url = session.get('liveUrl')
		started_at = session.get('startedAt')
		finished_at = session.get('finishedAt')
		keep_alive = session.get('keepAlive', False)
		proxy_cost = session.get('proxyCost')

		# Status emoji
		status_emoji = {
			'active': '🟢',
			'stopped': '⏹️',
		}.get(status, '❓')

		# Build header with duration
		duration = _format_duration(started_at, finished_at)
		header_parts = [f'{status_emoji} {session_id[:8]}... [{status}]']
		if duration:
			header_parts.append(duration)
		if proxy_cost:
			# Format proxy cost to 2 decimal places
			try:
				cost_val = float(proxy_cost)
				header_parts.append(f'${cost_val:.2f}')
			except (ValueError, TypeError):
				header_parts.append(f'${proxy_cost}')
		print(' '.join(header_parts))

		if keep_alive:
			print('  Keep Alive: Yes')
		if live_url:
			print(f'  Live URL: {live_url}')

	return 0


def _handle_stop(args: argparse.Namespace) -> int:
	"""Handle 'session stop <session_id>' command."""
	# Handle --all flag
	if getattr(args, 'all', False):
		return _handle_stop_all(args)

	try:
		_run_async(cloud_task.stop_session(args.session_id))
	except Exception as e:
		print(f'Error: {e}', file=sys.stderr)
		return 1

	if getattr(args, 'json', False):
		print(json.dumps({'stopped': args.session_id}))
	else:
		print(f'Stopped session: {args.session_id}')

	return 0


def _handle_stop_all(args: argparse.Namespace) -> int:
	"""Handle 'session stop --all' command."""
	try:
		# Get all active sessions
		sessions = _run_async(cloud_task.list_sessions(limit=100, status='active'))
	except Exception as e:
		print(f'Error listing sessions: {e}', file=sys.stderr)
		return 1

	if not sessions:
		print('No active sessions to stop')
		return 0

	# Extract session IDs
	session_ids = [s.get('id') for s in sessions if s.get('id')]

	if not session_ids:
		print('No active sessions to stop')
		return 0

	# Stop all sessions in parallel
	stopped, errors = _run_async(cloud_task.stop_sessions_parallel(session_ids))

	if getattr(args, 'json', False):
		print(json.dumps({'stopped': stopped, 'errors': errors}))
	else:
		if stopped:
			print(f'Stopped {len(stopped)} session(s):')
			for sid in stopped:
				print(f'  ✓ {sid[:8]}...')
		if errors:
			print(f'Failed to stop {len(errors)} session(s):')
			for err in errors:
				print(f'  ✗ {err["id"][:8]}...: {err["error"]}')

	return 0 if not errors else 1
