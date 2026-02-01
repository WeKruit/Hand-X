"""Interactions watchdog for hover, double-click, right-click, and info retrieval."""

import asyncio
import json
from typing import Any, ClassVar

from bubus import BaseEvent

from browser_use.browser.events import (
	ElementDblClickEvent,
	GetElementAttributesEvent,
	GetElementBoundingBoxEvent,
	GetElementTextEvent,
	GetElementValueEvent,
	GetPageHtmlEvent,
	GetPageTitleEvent,
	HoverElementEvent,
	ElementRightClickEvent,
)
from browser_use.browser.watchdog_base import BaseWatchdog
from browser_use.dom.service import EnhancedDOMTreeNode

# Rebuild event models that have forward references
HoverElementEvent.model_rebuild()
ElementDblClickEvent.model_rebuild()
ElementRightClickEvent.model_rebuild()
GetElementTextEvent.model_rebuild()
GetElementValueEvent.model_rebuild()
GetElementAttributesEvent.model_rebuild()
GetElementBoundingBoxEvent.model_rebuild()


class InteractionsWatchdog(BaseWatchdog):
	"""Handles hover, double-click, right-click, and element info retrieval."""

	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [
		HoverElementEvent,
		ElementDblClickEvent,
		ElementRightClickEvent,
		GetPageTitleEvent,
		GetPageHtmlEvent,
		GetElementTextEvent,
		GetElementValueEvent,
		GetElementAttributesEvent,
		GetElementBoundingBoxEvent,
	]
	EMITS: ClassVar[list[type[BaseEvent]]] = []

	async def _get_element_center(self, element_node: EnhancedDOMTreeNode) -> tuple[float, float] | None:
		"""Get the center coordinates of an element."""
		try:
			cdp_session = await self.browser_session.cdp_client_for_node(element_node)
			session_id = cdp_session.session_id
			backend_node_id = element_node.backend_node_id

			# Scroll element into view first
			try:
				await cdp_session.cdp_client.send.DOM.scrollIntoViewIfNeeded(
					params={'backendNodeId': backend_node_id}, session_id=session_id
				)
				await asyncio.sleep(0.05)
			except Exception:
				pass

			# Get element coordinates
			element_rect = await self.browser_session.get_element_coordinates(backend_node_id, cdp_session)
			if element_rect:
				center_x = element_rect.x + element_rect.width / 2
				center_y = element_rect.y + element_rect.height / 2
				return center_x, center_y
			return None
		except Exception as e:
			self.logger.error(f'[InteractionsWatchdog] Failed to get element center: {e}')
			return None

	async def on_HoverElementEvent(self, event: HoverElementEvent) -> None:
		"""Hover over an element."""
		try:
			coords = await self._get_element_center(event.node)
			if not coords:
				self.logger.warning('[InteractionsWatchdog] Could not get element coordinates for hover')
				return

			center_x, center_y = coords
			cdp_session = await self.browser_session.cdp_client_for_node(event.node)

			# Move mouse to element (hover)
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mouseMoved',
					'x': center_x,
					'y': center_y,
				},
				session_id=cdp_session.session_id,
			)

			self.logger.debug(f'[InteractionsWatchdog] Hovered at ({center_x}, {center_y})')
		except Exception as e:
			self.logger.error(f'[InteractionsWatchdog] Failed to hover: {e}')

	async def on_ElementDblClickEvent(self, event: ElementDblClickEvent) -> None:
		"""Double-click an element."""
		try:
			coords = await self._get_element_center(event.node)
			if not coords:
				self.logger.warning('[InteractionsWatchdog] Could not get element coordinates for double-click')
				return

			center_x, center_y = coords
			cdp_session = await self.browser_session.cdp_client_for_node(event.node)
			session_id = cdp_session.session_id

			# Move mouse to element
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={'type': 'mouseMoved', 'x': center_x, 'y': center_y},
				session_id=session_id,
			)
			await asyncio.sleep(0.05)

			# Double click (clickCount: 2)
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mousePressed',
					'x': center_x,
					'y': center_y,
					'button': 'left',
					'clickCount': 2,
				},
				session_id=session_id,
			)
			await asyncio.sleep(0.05)

			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mouseReleased',
					'x': center_x,
					'y': center_y,
					'button': 'left',
					'clickCount': 2,
				},
				session_id=session_id,
			)

			self.logger.debug(f'[InteractionsWatchdog] Double-clicked at ({center_x}, {center_y})')
		except Exception as e:
			self.logger.error(f'[InteractionsWatchdog] Failed to double-click: {e}')

	async def on_ElementRightClickEvent(self, event: ElementRightClickEvent) -> None:
		"""Right-click (context menu) an element."""
		try:
			coords = await self._get_element_center(event.node)
			if not coords:
				self.logger.warning('[InteractionsWatchdog] Could not get element coordinates for right-click')
				return

			center_x, center_y = coords
			cdp_session = await self.browser_session.cdp_client_for_node(event.node)
			session_id = cdp_session.session_id

			# Move mouse to element
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={'type': 'mouseMoved', 'x': center_x, 'y': center_y},
				session_id=session_id,
			)
			await asyncio.sleep(0.05)

			# Right click (button: 'right')
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mousePressed',
					'x': center_x,
					'y': center_y,
					'button': 'right',
					'clickCount': 1,
				},
				session_id=session_id,
			)
			await asyncio.sleep(0.05)

			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mouseReleased',
					'x': center_x,
					'y': center_y,
					'button': 'right',
					'clickCount': 1,
				},
				session_id=session_id,
			)

			self.logger.debug(f'[InteractionsWatchdog] Right-clicked at ({center_x}, {center_y})')
		except Exception as e:
			self.logger.error(f'[InteractionsWatchdog] Failed to right-click: {e}')

	async def _execute_js(self, js: str) -> Any:
		"""Execute JavaScript in the browser context."""
		cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=None)
		if not cdp_session:
			raise RuntimeError('No active browser session')

		result = await cdp_session.cdp_client.send.Runtime.evaluate(
			params={'expression': js, 'returnByValue': True},
			session_id=cdp_session.session_id,
		)
		return result.get('result', {}).get('value')

	async def on_GetPageTitleEvent(self, event: GetPageTitleEvent) -> str:
		"""Get the page title."""
		try:
			title = await self._execute_js('document.title')
			return title or ''
		except Exception as e:
			self.logger.error(f'[InteractionsWatchdog] Failed to get title: {e}')
			return ''

	async def on_GetPageHtmlEvent(self, event: GetPageHtmlEvent) -> str:
		"""Get page HTML, optionally scoped to a selector."""
		try:
			if event.selector:
				js = f'''
					(function() {{
						const el = document.querySelector({json.dumps(event.selector)});
						return el ? el.outerHTML : null;
					}})()
				'''
			else:
				js = 'document.documentElement.outerHTML'

			html = await self._execute_js(js)
			return html or ''
		except Exception as e:
			self.logger.error(f'[InteractionsWatchdog] Failed to get HTML: {e}')
			return ''

	async def on_GetElementTextEvent(self, event: GetElementTextEvent) -> str:
		"""Get text content of an element."""
		try:
			# Use the node's text from our model
			text = event.node.get_all_children_text(max_depth=10) if event.node else ''
			return text
		except Exception as e:
			self.logger.error(f'[InteractionsWatchdog] Failed to get element text: {e}')
			return ''

	async def on_GetElementValueEvent(self, event: GetElementValueEvent) -> str:
		"""Get value of an input/textarea element."""
		try:
			cdp_session = await self.browser_session.cdp_client_for_node(event.node)
			backend_node_id = event.node.backend_node_id

			# Use callFunctionOn to get the value
			result = await cdp_session.cdp_client.send.DOM.resolveNode(
				params={'backendNodeId': backend_node_id},
				session_id=cdp_session.session_id,
			)
			object_id = result.get('object', {}).get('objectId')

			if object_id:
				value_result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
					params={
						'objectId': object_id,
						'functionDeclaration': 'function() { return this.value; }',
						'returnByValue': True,
					},
					session_id=cdp_session.session_id,
				)
				value = value_result.get('result', {}).get('value')
				return value or ''
			else:
				return ''
		except Exception as e:
			self.logger.error(f'[InteractionsWatchdog] Failed to get element value: {e}')
			return ''

	async def on_GetElementAttributesEvent(self, event: GetElementAttributesEvent) -> dict[str, Any]:
		"""Get all attributes of an element."""
		try:
			# Use the attributes from the node model
			attrs = event.node.attributes or {}
			return dict(attrs)
		except Exception as e:
			self.logger.error(f'[InteractionsWatchdog] Failed to get element attributes: {e}')
			return {}

	async def on_GetElementBoundingBoxEvent(self, event: GetElementBoundingBoxEvent) -> dict[str, float]:
		"""Get bounding box of an element."""
		try:
			cdp_session = await self.browser_session.cdp_client_for_node(event.node)
			backend_node_id = event.node.backend_node_id

			# Get box model
			result = await cdp_session.cdp_client.send.DOM.getBoxModel(
				params={'backendNodeId': backend_node_id},
				session_id=cdp_session.session_id,
			)

			model = result.get('model', {})
			content = model.get('content', [])

			if len(content) >= 8:
				# content is [x1, y1, x2, y2, x3, y3, x4, y4] - corners of the quad
				x = min(content[0], content[2], content[4], content[6])
				y = min(content[1], content[3], content[5], content[7])
				width = max(content[0], content[2], content[4], content[6]) - x
				height = max(content[1], content[3], content[5], content[7]) - y
				return {'x': x, 'y': y, 'width': width, 'height': height}
			else:
				return {}
		except Exception as e:
			self.logger.error(f'[InteractionsWatchdog] Failed to get element bbox: {e}')
			return {}
