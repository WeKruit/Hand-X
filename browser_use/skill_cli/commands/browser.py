"""Browser control commands."""

import base64
import logging
from pathlib import Path
from typing import Any

from browser_use.skill_cli.sessions import SessionInfo

logger = logging.getLogger(__name__)

COMMANDS = {
	'open',
	'click',
	'type',
	'input',
	'scroll',
	'back',
	'screenshot',
	'state',
	'switch',
	'close-tab',
	'keys',
	'select',
	'eval',
	'extract',
	'cookies',
	'wait',
	'hover',
	'dblclick',
	'rightclick',
	'get',
}


async def _execute_js(session: SessionInfo, js: str) -> Any:
	"""Execute JavaScript in the browser via CDP."""
	bs = session.browser_session
	# Get or create a CDP session for the focused target
	cdp_session = await bs.get_or_create_cdp_session(target_id=None, focus=False)
	if not cdp_session:
		raise RuntimeError('No active browser session')

	result = await cdp_session.cdp_client.send.Runtime.evaluate(
		params={'expression': js, 'returnByValue': True},
		session_id=cdp_session.session_id,
	)
	return result.get('result', {}).get('value')


async def handle(action: str, session: SessionInfo, params: dict[str, Any]) -> Any:
	"""Handle browser control command."""
	bs = session.browser_session

	if action == 'open':
		url = params['url']
		# Ensure URL has scheme
		if not url.startswith(('http://', 'https://', 'file://')):
			url = 'https://' + url

		from browser_use.browser.events import NavigateToUrlEvent

		await bs.event_bus.dispatch(NavigateToUrlEvent(url=url))
		result: dict[str, Any] = {'url': url}
		# Add live preview URL for cloud browsers
		if bs.browser_profile.use_cloud and bs.cdp_url:
			from urllib.parse import quote

			result['live_url'] = f'https://live.browser-use.com/?wss={quote(bs.cdp_url, safe="")}'
		return result

	elif action == 'click':
		from browser_use.browser.events import ClickElementEvent

		index = params['index']
		# Look up node from selector map
		node = await bs.get_element_by_index(index)
		if node is None:
			return {'error': f'Element index {index} not found - page may have changed'}
		await bs.event_bus.dispatch(ClickElementEvent(node=node))
		return {'clicked': index}

	elif action == 'type':
		# Type into currently focused element using CDP directly
		text = params['text']
		cdp_session = await bs.get_or_create_cdp_session(target_id=None, focus=False)
		if not cdp_session:
			return {'error': 'No active browser session'}
		await cdp_session.cdp_client.send.Input.insertText(
			params={'text': text},
			session_id=cdp_session.session_id,
		)
		return {'typed': text}

	elif action == 'input':
		from browser_use.browser.events import ClickElementEvent, TypeTextEvent

		index = params['index']
		text = params['text']
		# Look up node from selector map
		node = await bs.get_element_by_index(index)
		if node is None:
			return {'error': f'Element index {index} not found - page may have changed'}
		await bs.event_bus.dispatch(ClickElementEvent(node=node))
		await bs.event_bus.dispatch(TypeTextEvent(node=node, text=text))
		return {'input': text, 'element': index}

	elif action == 'scroll':
		from browser_use.browser.events import ScrollEvent

		direction = params.get('direction', 'down')
		amount = params.get('amount', 500)
		await bs.event_bus.dispatch(ScrollEvent(direction=direction, amount=amount))
		return {'scrolled': direction, 'amount': amount}

	elif action == 'back':
		from browser_use.browser.events import GoBackEvent

		await bs.event_bus.dispatch(GoBackEvent())
		return {'back': True}

	elif action == 'screenshot':
		data = await bs.take_screenshot(full_page=params.get('full', False))

		if params.get('path'):
			path = Path(params['path'])
			path.write_bytes(data)
			return {'saved': str(path), 'size': len(data)}

		# Return base64 encoded
		return {'screenshot': base64.b64encode(data).decode(), 'size': len(data)}

	elif action == 'state':
		# Return the same LLM representation that browser-use agents see
		state_text = await bs.get_state_as_text()
		return {'_raw_text': state_text}

	elif action == 'switch':
		from browser_use.browser.events import SwitchTabEvent

		tab_index = params['tab']
		# Get target_id from tab index
		page_targets = bs.session_manager.get_all_page_targets() if bs.session_manager else []
		if tab_index < 0 or tab_index >= len(page_targets):
			return {'error': f'Invalid tab index {tab_index}. Available: 0-{len(page_targets) - 1}'}
		target_id = page_targets[tab_index].target_id
		await bs.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
		return {'switched': tab_index}

	elif action == 'close-tab':
		from browser_use.browser.events import CloseTabEvent

		tab_index = params.get('tab')
		# Get target_id from tab index
		page_targets = bs.session_manager.get_all_page_targets() if bs.session_manager else []
		if tab_index is not None:
			if tab_index < 0 or tab_index >= len(page_targets):
				return {'error': f'Invalid tab index {tab_index}. Available: 0-{len(page_targets) - 1}'}
			target_id = page_targets[tab_index].target_id
		else:
			# Close current/focused tab
			target_id = bs.session_manager.get_focused_target().target_id if bs.session_manager else None
			if not target_id:
				return {'error': 'No focused tab to close'}
		await bs.event_bus.dispatch(CloseTabEvent(target_id=target_id))
		return {'closed': tab_index}

	elif action == 'keys':
		from browser_use.browser.events import SendKeysEvent

		keys = params['keys']
		await bs.event_bus.dispatch(SendKeysEvent(keys=keys))
		return {'sent': keys}

	elif action == 'select':
		from browser_use.browser.events import SelectDropdownOptionEvent

		index = params['index']
		value = params['value']
		# Look up node from selector map
		node = await bs.get_element_by_index(index)
		if node is None:
			return {'error': f'Element index {index} not found - page may have changed'}
		await bs.event_bus.dispatch(SelectDropdownOptionEvent(node=node, text=value))
		return {'selected': value, 'element': index}

	elif action == 'eval':
		js = params['js']
		# Execute JavaScript via CDP
		result = await _execute_js(session, js)
		return {'result': result}

	elif action == 'extract':
		query = params['query']
		# This requires LLM integration
		# For now, return a placeholder
		return {'query': query, 'error': 'extract requires agent mode - use: browser-use run "extract ..."'}

	elif action == 'hover':
		from browser_use.browser.events import HoverElementEvent

		index = params['index']
		node = await bs.get_element_by_index(index)
		if node is None:
			return {'error': f'Element index {index} not found - page may have changed'}
		await bs.event_bus.dispatch(HoverElementEvent(node=node))
		return {'hovered': index}

	elif action == 'dblclick':
		from browser_use.browser.events import ElementDblClickEvent

		index = params['index']
		node = await bs.get_element_by_index(index)
		if node is None:
			return {'error': f'Element index {index} not found - page may have changed'}
		await bs.event_bus.dispatch(ElementDblClickEvent(node=node))
		return {'double_clicked': index}

	elif action == 'rightclick':
		from browser_use.browser.events import ElementRightClickEvent

		index = params['index']
		node = await bs.get_element_by_index(index)
		if node is None:
			return {'error': f'Element index {index} not found - page may have changed'}
		await bs.event_bus.dispatch(ElementRightClickEvent(node=node))
		return {'right_clicked': index}

	elif action == 'cookies':
		cookies_command = params.get('cookies_command')

		if cookies_command == 'get':
			from browser_use.browser.events import GetCookiesEvent

			url = params.get('url')
			event = GetCookiesEvent(url=url)
			await bs.event_bus.dispatch(event)
			cookies = await event.event_result()
			return {'cookies': cookies}

		elif cookies_command == 'set':
			from browser_use.browser.events import SetCookieEvent

			event = SetCookieEvent(
				name=params['name'],
				value=params['value'],
				domain=params.get('domain'),
				path=params.get('path', '/'),
				secure=params.get('secure', False),
				http_only=params.get('http_only', False),
				same_site=params.get('same_site'),
				expires=params.get('expires'),
			)
			await bs.event_bus.dispatch(event)
			success = await event.event_result()
			return {'set': params['name'], 'success': success}

		elif cookies_command == 'clear':
			from browser_use.browser.events import ClearCookiesEvent

			url = params.get('url')
			event = ClearCookiesEvent(url=url)
			await bs.event_bus.dispatch(event)
			# No event_result() call - this is a void operation
			return {'cleared': True, 'url': url}

		elif cookies_command == 'export':
			import json

			from browser_use.browser.events import GetCookiesEvent

			url = params.get('url')
			event = GetCookiesEvent(url=url)
			await bs.event_bus.dispatch(event)
			cookies = await event.event_result()

			file_path = Path(params['file'])
			file_path.write_text(json.dumps(cookies, indent=2))
			return {'exported': len(cookies), 'file': str(file_path)}

		elif cookies_command == 'import':
			import json

			file_path = Path(params['file'])
			if not file_path.exists():
				return {'error': f'File not found: {file_path}'}

			cookies = json.loads(file_path.read_text())

			# Get CDP session for bulk cookie setting
			cdp_session = await bs.get_or_create_cdp_session(target_id=None, focus=False)
			if not cdp_session:
				return {'error': 'No active browser session'}

			# Build cookie list for bulk set
			cookie_list = []
			for c in cookies:
				cookie_params = {
					'name': c['name'],
					'value': c['value'],
					'domain': c.get('domain'),
					'path': c.get('path', '/'),
					'secure': c.get('secure', False),
					'httpOnly': c.get('httpOnly', False),
				}
				if c.get('sameSite'):
					cookie_params['sameSite'] = c['sameSite']
				if c.get('expires'):
					cookie_params['expires'] = c['expires']
				cookie_list.append(cookie_params)

			# Set all cookies in one call
			try:
				await cdp_session.cdp_client.send.Network.setCookies(
					params={'cookies': cookie_list},
					session_id=cdp_session.session_id,
				)
				return {'imported': len(cookie_list), 'file': str(file_path)}
			except Exception as e:
				return {'error': f'Failed to import cookies: {e}'}

		return {'error': 'Invalid cookies command. Use: get, set, clear, export, import'}

	elif action == 'wait':
		wait_command = params.get('wait_command')

		if wait_command == 'selector':
			from browser_use.browser.events import WaitForSelectorEvent

			event = WaitForSelectorEvent(
				selector=params['selector'],
				timeout_ms=params.get('timeout', 30000),
				state=params.get('state', 'visible'),
			)
			await bs.event_bus.dispatch(event)
			success = await event.event_result()
			return {'selector': params['selector'], 'found': success}

		elif wait_command == 'text':
			from browser_use.browser.events import WaitForTextEvent

			event = WaitForTextEvent(
				text=params['text'],
				timeout_ms=params.get('timeout', 30000),
			)
			await bs.event_bus.dispatch(event)
			success = await event.event_result()
			return {'text': params['text'], 'found': success}

		return {'error': 'Invalid wait command. Use: selector, text'}

	elif action == 'get':
		get_command = params.get('get_command')

		if get_command == 'title':
			from browser_use.browser.events import GetPageTitleEvent

			event = GetPageTitleEvent()
			await bs.event_bus.dispatch(event)
			title = await event.event_result()
			return {'title': title}

		elif get_command == 'html':
			from browser_use.browser.events import GetPageHtmlEvent

			selector = params.get('selector')
			event = GetPageHtmlEvent(selector=selector)
			await bs.event_bus.dispatch(event)
			html = await event.event_result()
			return {'html': html}

		elif get_command == 'text':
			from browser_use.browser.events import GetElementTextEvent

			index = params['index']
			node = await bs.get_element_by_index(index)
			if node is None:
				return {'error': f'Element index {index} not found - page may have changed'}
			event = GetElementTextEvent(node=node)
			await bs.event_bus.dispatch(event)
			text = await event.event_result()
			return {'index': index, 'text': text}

		elif get_command == 'value':
			from browser_use.browser.events import GetElementValueEvent

			index = params['index']
			node = await bs.get_element_by_index(index)
			if node is None:
				return {'error': f'Element index {index} not found - page may have changed'}
			event = GetElementValueEvent(node=node)
			await bs.event_bus.dispatch(event)
			value = await event.event_result()
			return {'index': index, 'value': value}

		elif get_command == 'attributes':
			from browser_use.browser.events import GetElementAttributesEvent

			index = params['index']
			node = await bs.get_element_by_index(index)
			if node is None:
				return {'error': f'Element index {index} not found - page may have changed'}
			event = GetElementAttributesEvent(node=node)
			await bs.event_bus.dispatch(event)
			attrs = await event.event_result()
			return {'index': index, 'attributes': attrs}

		elif get_command == 'bbox':
			from browser_use.browser.events import GetElementBoundingBoxEvent

			index = params['index']
			node = await bs.get_element_by_index(index)
			if node is None:
				return {'error': f'Element index {index} not found - page may have changed'}
			event = GetElementBoundingBoxEvent(node=node)
			await bs.event_bus.dispatch(event)
			bbox = await event.event_result()
			return {'index': index, 'bbox': bbox}

		return {'error': 'Invalid get command. Use: title, html, text, value, attributes, bbox'}

	raise ValueError(f'Unknown browser action: {action}')
