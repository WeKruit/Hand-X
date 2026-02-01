"""Interactions watchdog for hover, double-click, right-click."""

import asyncio
from typing import Any, ClassVar

from bubus import BaseEvent

from browser_use.browser.events import (
	ElementDblClickEvent,
	HoverElementEvent,
	ElementRightClickEvent,
)
from browser_use.browser.watchdog_base import BaseWatchdog
from browser_use.dom.service import EnhancedDOMTreeNode

# Rebuild event models that have forward references
HoverElementEvent.model_rebuild()
ElementDblClickEvent.model_rebuild()
ElementRightClickEvent.model_rebuild()


class InteractionsWatchdog(BaseWatchdog):
	"""Handles hover, double-click, and right-click interactions."""

	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [
		HoverElementEvent,
		ElementDblClickEvent,
		ElementRightClickEvent,
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
