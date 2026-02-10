"""Task management command handlers for browser-use CLI.

Handles: task list, task status, task stop, task logs
"""

import argparse
import asyncio
import json
import sys
from typing import Any

from browser_use.skill_cli.api_key import APIKeyRequired, require_api_key
from browser_use.skill_cli.commands import cloud_task


def handle_task_command(args: argparse.Namespace) -> int:
	"""Handle task subcommands.

	Task commands manage cloud tasks and always require the cloud API.

	Args:
		args: Parsed command-line arguments

	Returns:
		Exit code (0 for success, 1 for error)
	"""
	from browser_use.skill_cli.install_config import is_mode_available

	# Check if remote mode is available
	if not is_mode_available('remote'):
		print(
			'Error: Task management requires remote mode.\n'
			'Remote mode is not installed. Reinstall with --full to enable:\n'
			'  curl -fsSL https://browser-use.com/install.sh | bash -s -- --full',
			file=sys.stderr,
		)
		return 1

	# Check API key
	try:
		require_api_key('Cloud tasks')
	except APIKeyRequired as e:
		print(f'Error: {e}', file=sys.stderr)
		return 1

	if args.task_command == 'list':
		return _handle_list(args)
	elif args.task_command == 'status':
		return _handle_status(args)
	elif args.task_command == 'stop':
		return _handle_stop(args)
	elif args.task_command == 'logs':
		return _handle_logs(args)
	else:
		print('Usage: browser-use task <command>')
		print('Commands: list, status <task_id>, stop <task_id>, logs <task_id>')
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


def _handle_list(args: argparse.Namespace) -> int:
	"""Handle 'task list' command."""
	try:
		status_filter = getattr(args, 'status', None)
		tasks = _run_async(cloud_task.list_tasks(limit=args.limit, status=status_filter))
	except Exception as e:
		print(f'Error: {e}', file=sys.stderr)
		return 1

	if getattr(args, 'json', False):
		print(json.dumps(tasks))
	else:
		if not tasks:
			status_msg = f' with status "{status_filter}"' if status_filter else ''
			print(f'No tasks found{status_msg}')
		else:
			header = f'Tasks ({len(tasks)})'
			if status_filter:
				header = f'{status_filter.capitalize()} tasks ({len(tasks)})'
			print(f'{header}:')
			for t in tasks:
				task_id = t.get('id', 'unknown')
				status = t.get('status', 'unknown')
				task_desc = t.get('task', '')
				# Truncate long task descriptions
				if len(task_desc) > 50:
					task_desc = task_desc[:47] + '...'

				# Status emoji
				status_emoji = {
					'started': '🔄',
					'running': '🔄',
					'finished': '✅',
					'stopped': '⏹️',
					'failed': '❌',
				}.get(status, '❓')

				print(f'  {status_emoji} {task_id[:8]}... [{status}] {task_desc}')

	return 0


def _handle_status(args: argparse.Namespace) -> int:
	"""Handle 'task status <task_id>' command."""
	try:
		status = _run_async(cloud_task.get_task_status(args.task_id))
	except Exception as e:
		print(f'Error: {e}', file=sys.stderr)
		return 1

	if getattr(args, 'json', False):
		print(json.dumps(status))
	else:
		task_id = status.get('id', args.task_id)
		task_status = status.get('status', 'unknown')
		output = status.get('output')
		cost = status.get('cost')

		# Status emoji
		status_emoji = {
			'started': '🔄',
			'running': '🔄',
			'finished': '✅',
			'stopped': '⏹️',
			'failed': '❌',
		}.get(task_status, '❓')

		print(f'Task: {task_id}')
		print(f'Status: {status_emoji} {task_status}')

		if cost is not None:
			print(f'Cost: ${cost}')

		if output:
			print(f'Output: {output}')

	return 0


def _handle_stop(args: argparse.Namespace) -> int:
	"""Handle 'task stop <task_id>' command."""
	try:
		result = _run_async(cloud_task.stop_task(args.task_id))
	except Exception as e:
		print(f'Error: {e}', file=sys.stderr)
		return 1

	if getattr(args, 'json', False):
		print(json.dumps(result))
	else:
		print(f'Stopped task: {args.task_id}')

	return 0


def _handle_logs(args: argparse.Namespace) -> int:
	"""Handle 'task logs <task_id>' command."""
	try:
		result = _run_async(cloud_task.get_task_logs(args.task_id))
	except Exception as e:
		print(f'Error: {e}', file=sys.stderr)
		return 1

	if getattr(args, 'json', False):
		print(json.dumps(result))
	else:
		download_url = result.get('downloadUrl')
		if download_url:
			print(f'Download logs: {download_url}')
		else:
			print('No logs available for this task')

	return 0
