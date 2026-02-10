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


def _handle_status(args: argparse.Namespace) -> int:
	"""Handle 'task status <task_id>' command."""
	try:
		# Use get_task() for full details including steps
		task_data = _run_async(cloud_task.get_task(args.task_id))
	except Exception as e:
		print(f'Error: {e}', file=sys.stderr)
		return 1

	if getattr(args, 'json', False):
		print(json.dumps(task_data))
	else:
		task_id = task_data.get('id', args.task_id)
		task_status = task_data.get('status', 'unknown')
		output = task_data.get('output')
		cost = task_data.get('cost')
		steps = task_data.get('steps', [])
		started_at = task_data.get('startedAt')
		finished_at = task_data.get('finishedAt')

		compact = getattr(args, 'compact', False)
		verbose = getattr(args, 'verbose', False)
		last_n = getattr(args, 'last', None)
		reverse = getattr(args, 'reverse', False)
		specific_step = getattr(args, 'step', None)

		# Determine display mode:
		# - Default: show only latest step
		# - --compact: show all steps with reasoning
		# - --verbose: show all steps with full details
		show_all_steps = compact or verbose

		# Status emoji
		status_emoji = {
			'started': '🔄',
			'running': '🔄',
			'finished': '✅',
			'stopped': '⏹️',
			'failed': '❌',
		}.get(task_status, '❓')

		# Build header line: status, cost, duration
		parts = [f'{status_emoji} {task_id[:8]}... [{task_status}]']
		if cost is not None:
			parts.append(f'${cost}')
		duration = _format_duration(started_at, finished_at)
		if duration:
			parts.append(duration)
		print(' '.join(parts))

		# Show steps
		if steps:
			total_steps = len(steps)

			# Filter to specific step if requested
			if specific_step is not None:
				steps = [s for s in steps if s.get('number') == specific_step]
				if not steps:
					print(f'  Step {specific_step} not found (task has {total_steps} steps)')
				else:
					print(f'  (showing step {specific_step} of {total_steps})')
				# Display the specific step
				for step in steps:
					_print_step(step, verbose)
			elif not show_all_steps:
				# Default mode: show only the latest step
				latest_step = steps[-1]
				earlier_count = total_steps - 1
				if earlier_count > 0:
					print(f'  ... {earlier_count} earlier steps')
				_print_step(latest_step, verbose=False)
			else:
				# --compact or --verbose: show all steps (with optional filters)
				skipped_earlier = 0
				if last_n is not None and last_n < total_steps:
					skipped_earlier = total_steps - last_n
					steps = steps[-last_n:]

				# Apply --reverse
				if reverse:
					steps = list(reversed(steps))

				# Show count info
				if skipped_earlier > 0:
					print(f'  ... {skipped_earlier} earlier steps')

				# Display steps
				for step in steps:
					_print_step(step, verbose)

		if output:
			print(f'\nOutput: {output}')

	return 0


def _print_step(step: dict, verbose: bool) -> None:
	"""Print a single step in compact or verbose format."""
	step_num = step.get('number', '?')
	memory = step.get('memory', '')

	if verbose:
		url = step.get('url', '')
		actions = step.get('actions', [])

		# Truncate URL for display
		short_url = url[:60] + '...' if len(url) > 60 else url

		print(f'  [{step_num}] {short_url}')
		if memory:
			# Truncate memory/reasoning for display
			short_memory = memory[:100] + '...' if len(memory) > 100 else memory
			print(f'      Reasoning: {short_memory}')
		if actions:
			for action in actions[:2]:  # Show max 2 actions per step
				# Truncate action for display
				short_action = action[:70] + '...' if len(action) > 70 else action
				print(f'      Action: {short_action}')
			if len(actions) > 2:
				print(f'      ... and {len(actions) - 2} more actions')
	else:
		# Compact mode: just step number and reasoning
		if memory:
			# Truncate reasoning for compact display
			short_memory = memory[:80] + '...' if len(memory) > 80 else memory
			print(f'  {step_num}. {short_memory}')
		else:
			print(f'  {step_num}. (no reasoning)')


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
