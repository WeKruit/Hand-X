"""Session management command handlers."""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
	from browser_use.skill_cli.sessions import SessionRegistry

logger = logging.getLogger(__name__)

COMMANDS = {'sessions', 'close'}


async def handle(action: str, registry: 'SessionRegistry', params: dict[str, Any]) -> Any:
	"""Handle session management command."""
	if action == 'sessions':
		sessions = registry.list_sessions()
		return {
			'sessions': sessions,
			'count': len(sessions),
		}

	elif action == 'close':
		if params.get('all'):
			# Close all sessions
			sessions = registry.list_sessions()
			await registry.close_all()
			return {
				'closed': [s['name'] for s in sessions],
				'count': len(sessions),
			}
		else:
			# Close current session
			name = params.get('session', 'default')
			success = await registry.close_session(name)
			if success:
				return {'closed': name}
			else:
				return {'error': f'Session not found: {name}'}

	raise ValueError(f'Unknown session action: {action}')
