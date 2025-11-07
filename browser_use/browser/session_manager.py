"""Event-driven CDP session management.

Manages CDP sessions by listening to Target.attachedToTarget and Target.detachedFromTarget
events, ensuring the session pool always reflects the current browser state.
"""

import asyncio
from typing import TYPE_CHECKING

from cdp_use.cdp.target import AttachedToTargetEvent, DetachedFromTargetEvent, SessionID, TargetID

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession, CDPSession


class SessionManager:
	"""Event-driven CDP session manager.

	Automatically synchronizes the CDP session pool with browser state via CDP events.

	Key features:
	- Sessions added/removed automatically via Target attach/detach events
	- Multiple sessions can attach to the same target
	- Targets only removed when ALL sessions detach
	- No stale sessions - pool always reflects browser reality
	"""

	def __init__(self, browser_session: 'BrowserSession'):
		self.browser_session = browser_session
		self.logger = browser_session.logger

		# Target -> set of sessions attached to it
		self._target_sessions: dict[TargetID, set[SessionID]] = {}

		# Session -> target mapping for reverse lookup
		self._session_to_target: dict[SessionID, TargetID] = {}

		# Target -> type cache (page, iframe, worker, etc.) - types are immutable
		self._target_types: dict[TargetID, str] = {}

		# Lock for thread-safe access
		self._lock = asyncio.Lock()

		# Lock for recovery to prevent concurrent recovery attempts
		self._recovery_lock = asyncio.Lock()

	async def start_monitoring(self) -> None:
		"""Start monitoring Target attach/detach events.

		Registers CDP event handlers to keep the session pool synchronized with browser state.
		Also discovers and initializes all existing targets on startup.
		"""
		if not self.browser_session._cdp_client_root:
			raise RuntimeError('CDP client not initialized')

		# Capture cdp_client_root in closure to avoid type errors
		cdp_client = self.browser_session._cdp_client_root

		# Register synchronous event handlers (CDP requirement)
		def on_attached(event: AttachedToTargetEvent, session_id: SessionID | None = None):
			# _handle_target_attached() handles:
			# - setAutoAttach for children
			# - Create CDPSession
			# - Enable monitoring (for pages/tabs)
			# - Add to pool
			asyncio.create_task(self._handle_target_attached(event))

		def on_detached(event: DetachedFromTargetEvent, session_id: SessionID | None = None):
			asyncio.create_task(self._handle_target_detached(event))

		self.browser_session._cdp_client_root.register.Target.attachedToTarget(on_attached)
		self.browser_session._cdp_client_root.register.Target.detachedFromTarget(on_detached)

		self.logger.debug('[SessionManager] Event monitoring started')

		# Discover and initialize ALL existing targets
		await self._initialize_existing_targets()

	async def get_session_for_target(self, target_id: TargetID) -> 'CDPSession | None':
		"""Get the current valid session for a target.

		Args:
			target_id: Target ID to get session for

		Returns:
			CDPSession if exists, None if target has detached
		"""
		async with self._lock:
			return self.browser_session._cdp_session_pool.get(target_id)

	async def validate_session(self, target_id: TargetID) -> bool:
		"""Check if a target still has active sessions.

		Args:
			target_id: Target ID to validate

		Returns:
			True if target has active sessions, False if it should be removed
		"""
		async with self._lock:
			if target_id not in self._target_sessions:
				return False

			return len(self._target_sessions[target_id]) > 0

	async def clear(self) -> None:
		"""Clear all session tracking for cleanup."""
		async with self._lock:
			self._target_sessions.clear()
			self._session_to_target.clear()
			self._target_types.clear()

		self.logger.info('[SessionManager] Cleared all session tracking')

	async def is_target_valid(self, target_id: TargetID) -> bool:
		"""Check if a target is still valid and has active sessions.

		Args:
			target_id: Target ID to validate

		Returns:
			True if target is valid and has active sessions, False otherwise
		"""
		async with self._lock:
			if target_id not in self._target_sessions:
				return False
			return len(self._target_sessions[target_id]) > 0

	async def _handle_target_attached(self, event: AttachedToTargetEvent) -> None:
		"""Handle Target.attachedToTarget event.

		Called automatically by Chrome when a new target/session is created.
		This is the ONLY place where sessions are added to the pool.
		"""
		target_id = event['targetInfo']['targetId']
		session_id = event['sessionId']
		target_type = event['targetInfo']['type']
		waiting_for_debugger = event.get('waitingForDebugger', False)

		self.logger.debug(
			f'[SessionManager] Target attached: {target_id[:8]}... (session={session_id[:8]}..., '
			f'type={target_type}, waitingForDebugger={waiting_for_debugger})'
		)

		# Enable auto-attach for this session's children (do this FIRST, outside lock)
		assert self.browser_session._cdp_client_root is not None, 'Root CDP client required'
		try:
			await self.browser_session._cdp_client_root.send.Target.setAutoAttach(
				params={'autoAttach': True, 'waitForDebuggerOnStart': False, 'flatten': True}, session_id=session_id
			)
			self.logger.debug(f'[SessionManager] Auto-attach enabled for {target_type} session {session_id[:8]}...')
		except Exception as e:
			error_str = str(e)
			# Expected for short-lived targets (workers, temp iframes) that detach before this executes
			if '-32001' in error_str or 'Session with given id not found' in error_str:
				self.logger.debug(
					f'[SessionManager] Auto-attach skipped for {target_type} session {session_id[:8]}... '
					f'(already detached - normal for short-lived targets)'
				)
			else:
				self.logger.debug(f'[SessionManager] Auto-attach failed for {target_type}: {e}')

		async with self._lock:
			# Track this session for the target
			if target_id not in self._target_sessions:
				self._target_sessions[target_id] = set()

			self._target_sessions[target_id].add(session_id)
			self._session_to_target[session_id] = target_id

			# Cache target type (immutable, set once)
			if target_id not in self._target_types:
				self._target_types[target_id] = target_type

			# Create CDPSession wrapper and add to pool
			if target_id not in self.browser_session._cdp_session_pool:
				from browser_use.browser.session import CDPSession

				assert self.browser_session._cdp_client_root is not None, 'Root CDP client required'

				cdp_session = CDPSession(
					cdp_client=self.browser_session._cdp_client_root,
					target_id=target_id,
					session_id=session_id,
					title=event['targetInfo'].get('title', 'Unknown title'),
					url=event['targetInfo'].get('url', 'about:blank'),
				)

				self.browser_session._cdp_session_pool[target_id] = cdp_session

				self.logger.debug(
					f'[SessionManager] Created session for target {target_id[:8]}... '
					f'(pool size: {len(self.browser_session._cdp_session_pool)})'
				)

				# Enable lifecycle events and network monitoring for page targets
				if target_type in ('page', 'tab'):
					await self._enable_page_monitoring(cdp_session)
			else:
				# Update existing session with new session_id
				existing = self.browser_session._cdp_session_pool[target_id]
				existing.session_id = session_id
				existing.title = event['targetInfo'].get('title', existing.title)
				existing.url = event['targetInfo'].get('url', existing.url)

		# Resume execution if waiting for debugger
		if waiting_for_debugger:
			try:
				assert self.browser_session._cdp_client_root is not None
				await self.browser_session._cdp_client_root.send.Runtime.runIfWaitingForDebugger(session_id=session_id)
				self.logger.debug(f'[SessionManager] Resumed execution for session {session_id[:8]}...')
			except Exception as e:
				self.logger.warning(f'[SessionManager] Failed to resume execution: {e}')

	async def _handle_target_detached(self, event: DetachedFromTargetEvent) -> None:
		"""Handle Target.detachedFromTarget event.

		Called automatically by Chrome when a target/session is destroyed.
		This is the ONLY place where sessions are removed from the pool.
		"""
		session_id = event['sessionId']
		target_id = event.get('targetId')  # May be empty

		# If targetId not in event, look it up via session mapping
		if not target_id:
			async with self._lock:
				target_id = self._session_to_target.get(session_id)

		if not target_id:
			self.logger.warning(f'[SessionManager] Session detached but target unknown (session={session_id[:8]}...)')
			return

		agent_focus_lost = False
		target_fully_removed = False
		target_type = None

		async with self._lock:
			# Remove this session from target's session set
			if target_id in self._target_sessions:
				self._target_sessions[target_id].discard(session_id)

				remaining_sessions = len(self._target_sessions[target_id])

				self.logger.debug(
					f'[SessionManager] Session detached: target={target_id[:8]}... '
					f'session={session_id[:8]}... (remaining={remaining_sessions})'
				)

				# Only remove target when NO sessions remain
				if remaining_sessions == 0:
					self.logger.debug(f'[SessionManager] No sessions remain for target {target_id[:8]}..., removing from pool')

					target_fully_removed = True

					# Check if agent_focus points to this target
					agent_focus_lost = (
						self.browser_session.agent_focus and self.browser_session.agent_focus.target_id == target_id
					)

					# Remove from pool
					if target_id in self.browser_session._cdp_session_pool:
						self.browser_session._cdp_session_pool.pop(target_id)
						self.logger.debug(
							f'[SessionManager] Removed target {target_id[:8]}... from pool '
							f'(pool size: {len(self.browser_session._cdp_session_pool)})'
						)

					# Clean up tracking
					del self._target_sessions[target_id]
			else:
				# Target not tracked - already removed or never attached
				self.logger.debug(
					f'[SessionManager] Session detached from untracked target: target={target_id[:8]}... '
					f'session={session_id[:8]}... (target was already removed or attach event was missed)'
				)

			# Get target type before cleaning up cache (needed for TabClosedEvent dispatch)
			target_type = self._target_types.get(target_id)

			# Clean up target type cache if target fully removed
			if target_id not in self._target_sessions and target_id in self._target_types:
				del self._target_types[target_id]

			# Remove from reverse mapping
			if session_id in self._session_to_target:
				del self._session_to_target[session_id]

		# Dispatch TabClosedEvent only for page/tab targets that are fully removed (not iframes/workers or partial detaches)
		if target_fully_removed:
			if target_type in ('page', 'tab'):
				from browser_use.browser.events import TabClosedEvent

				self.browser_session.event_bus.dispatch(TabClosedEvent(target_id=target_id))
				self.logger.debug(f'[SessionManager] Dispatched TabClosedEvent for page target {target_id[:8]}...')
			elif target_type:
				self.logger.debug(
					f'[SessionManager] Target {target_id[:8]}... fully removed (type={target_type}) - not dispatching TabClosedEvent'
				)

		# Auto-recover agent_focus outside the lock to avoid blocking other operations
		if agent_focus_lost:
			await self._recover_agent_focus(target_id)

	async def _recover_agent_focus(self, crashed_target_id: TargetID) -> None:
		"""Auto-recover agent_focus when the focused target crashes/detaches.

		Uses recovery lock to prevent concurrent recovery attempts from creating multiple emergency tabs.

		Args:
			crashed_target_id: The target ID that was lost
		"""
		# Prevent concurrent recovery attempts
		async with self._recovery_lock:
			# Check if another recovery already fixed agent_focus
			if self.browser_session.agent_focus and self.browser_session.agent_focus.target_id != crashed_target_id:
				self.logger.debug(
					f'[SessionManager] Agent focus already recovered by concurrent operation '
					f'(now: {self.browser_session.agent_focus.target_id[:8]}...), skipping recovery'
				)
				return

			self.logger.warning(
				f'[SessionManager] Agent focus target {crashed_target_id[:8]}... detached! '
				f'Auto-recovering by switching to another target...'
			)

		try:
			# Try to find another valid page target
			all_pages = await self.browser_session._cdp_get_all_pages()

			new_target_id = None
			is_existing_tab = False

			if all_pages:
				# Switch to most recent page that's not the crashed one
				new_target_id = all_pages[-1]['targetId']
				is_existing_tab = True
				self.logger.info(f'[SessionManager] Switching agent_focus to existing tab {new_target_id[:8]}...')
			else:
				# No pages exist - create a new one
				self.logger.warning('[SessionManager] No tabs remain! Creating new tab for agent...')
				new_target_id = await self.browser_session._cdp_create_new_page('about:blank')
				self.logger.info(f'[SessionManager] Created new tab {new_target_id[:8]}... for agent')

				# Dispatch TabCreatedEvent so watchdogs can initialize
				from browser_use.browser.events import TabCreatedEvent

				self.browser_session.event_bus.dispatch(TabCreatedEvent(url='about:blank', target_id=new_target_id))

			# Wait for attach event to create session, then update agent_focus
			new_session = None
			for attempt in range(20):  # Wait up to 2 seconds
				await asyncio.sleep(0.1)
				new_session = await self.get_session_for_target(new_target_id)
				if new_session:
					break

			if new_session:
				self.browser_session.agent_focus = new_session
				self.logger.info(f'[SessionManager] ✅ Agent focus recovered: {new_target_id[:8]}...')

				# Visually activate the tab in browser (only for existing tabs)
				if is_existing_tab:
					try:
						assert self.browser_session._cdp_client_root is not None
						await self.browser_session._cdp_client_root.send.Target.activateTarget(params={'targetId': new_target_id})
						self.logger.debug(f'[SessionManager] Activated tab {new_target_id[:8]}... in browser UI')
					except Exception as e:
						self.logger.debug(f'[SessionManager] Failed to activate tab visually: {e}')

				# Dispatch focus changed event
				from browser_use.browser.events import AgentFocusChangedEvent

				self.browser_session.event_bus.dispatch(AgentFocusChangedEvent(target_id=new_target_id, url=new_session.url))
				return

			# Recovery failed - create emergency fallback tab
			self.logger.error(
				f'[SessionManager] ❌ Failed to get session for {new_target_id[:8]}... after 2s, creating emergency fallback tab'
			)

			fallback_target_id = await self.browser_session._cdp_create_new_page('about:blank')
			self.logger.warning(f'[SessionManager] Created emergency fallback tab {fallback_target_id[:8]}...')

			# Try one more time with fallback
			for _ in range(20):
				await asyncio.sleep(0.1)
				fallback_session = await self.get_session_for_target(fallback_target_id)
				if fallback_session:
					self.browser_session.agent_focus = fallback_session
					self.logger.warning(f'[SessionManager] ⚠️ Agent focus set to emergency fallback: {fallback_target_id[:8]}...')

					from browser_use.browser.events import AgentFocusChangedEvent, TabCreatedEvent

					self.browser_session.event_bus.dispatch(TabCreatedEvent(url='about:blank', target_id=fallback_target_id))
					self.browser_session.event_bus.dispatch(
						AgentFocusChangedEvent(target_id=fallback_target_id, url='about:blank')
					)
					return

			# Complete failure - this should never happen
			self.logger.critical(
				'[SessionManager] 🚨 CRITICAL: Failed to recover agent_focus even with fallback! Agent may be in broken state.'
			)

		except Exception as e:
			self.logger.error(f'[SessionManager] ❌ Error during agent_focus recovery: {type(e).__name__}: {e}')

	async def _initialize_existing_targets(self) -> None:
		"""Discover and initialize all existing targets at startup.

		Attaches to all existing targets. Chrome fires attachedToTarget events which the
		on_attached handler processes via create_task(). We wait for all sessions to appear
		in the pool to ensure initialization is complete before returning.
		"""
		cdp_client = self.browser_session._cdp_client_root
		assert cdp_client is not None

		# Get all existing targets
		targets_result = await cdp_client.send.Target.getTargets()
		existing_targets = targets_result.get('targetInfos', [])

		self.logger.debug(f'[SessionManager] Discovered {len(existing_targets)} existing targets')

		# Track target IDs we're attaching to
		target_ids_to_initialize = []

		# Attach to ALL existing targets - this triggers attachedToTarget events
		for target in existing_targets:
			target_id = target['targetId']
			target_type = target.get('type', 'unknown')

			try:
				# Just attach - the on_attached handler does the rest via create_task()
				await cdp_client.send.Target.attachToTarget(params={'targetId': target_id, 'flatten': True})
				target_ids_to_initialize.append(target_id)
				self.logger.debug(f'[SessionManager] Attached to existing target: {target_id[:8]}... (type={target_type})')
			except Exception as e:
				self.logger.debug(
					f'[SessionManager] Failed to attach to existing target {target_id[:8]}... (type={target_type}): {e}'
				)

		# Wait for all attach handlers to complete by polling for sessions in pool
		# This is more reliable than sleep - we wait exactly as long as needed
		max_wait = 2.0  # 2 seconds max - most targets initialize in < 500ms
		start_time = asyncio.get_event_loop().time()

		while (asyncio.get_event_loop().time() - start_time) < max_wait:
			# Check if all target sessions are in pool
			session_checks = []
			for tid in target_ids_to_initialize:
				session = await self.get_session_for_target(tid)
				session_checks.append(session is not None)

			if all(session_checks):
				self.logger.debug(
					f'[SessionManager] All {len(self.browser_session._cdp_session_pool)} sessions initialized and ready'
				)
				return

			await asyncio.sleep(0.05)  # Poll every 50ms

		# Timeout - some sessions didn't initialize (likely short-lived targets that already detached)
		initialized_count = sum(1 for tid in target_ids_to_initialize if tid in self.browser_session._cdp_session_pool)
		self.logger.warning(
			f'[SessionManager] Initialization timeout: {initialized_count}/{len(target_ids_to_initialize)} sessions ready '
			f'(some targets may have detached during initialization)'
		)

	async def _enable_page_monitoring(self, cdp_session: 'CDPSession') -> None:
		"""Enable lifecycle events and network monitoring for a page target.

		This is called once per page when it's created, avoiding handler accumulation.
		Registers a SINGLE lifecycle handler per session that stores events for navigations to consume.

		Args:
			cdp_session: The CDP session to enable monitoring on
		"""
		try:
			# Enable lifecycle events (load, DOMContentLoaded, networkIdle, etc.)
			await cdp_session.cdp_client.send.Page.setLifecycleEventsEnabled(
				params={'enabled': True}, session_id=cdp_session.session_id
			)
			self.logger.debug(f'[SessionManager] Enabled lifecycle events for target {cdp_session.target_id[:8]}...')

			# Enable network monitoring for networkIdle detection
			await cdp_session.cdp_client.send.Network.enable(session_id=cdp_session.session_id)
			self.logger.debug(f'[SessionManager] Enabled network monitoring for target {cdp_session.target_id[:8]}...')

			# Initialize lifecycle event storage for this session (thread-safe)
			from collections import deque

			cdp_session._lifecycle_events = deque(maxlen=50)  # Keep last 50 events
			cdp_session._lifecycle_lock = asyncio.Lock()

			# Register ONE handler per session that stores events
			def on_lifecycle_event(event, session_id=None):
				if session_id == cdp_session.session_id:
					# Store event for navigations to consume
					event_data = {
						'name': event.get('name'),
						'loaderId': event.get('loaderId'),
						'timestamp': asyncio.get_event_loop().time(),
					}
					# Append is atomic in CPython, but use lock for safety
					try:
						cdp_session._lifecycle_events.append(event_data)
					except Exception:
						pass  # Session might be detaching

			# Register the handler ONCE (this is the only place we register)
			cdp_session.cdp_client.register.Page.lifecycleEvent(on_lifecycle_event)
			self.logger.debug(f'[SessionManager] Registered lifecycle handler for target {cdp_session.target_id[:8]}...')

			# Keep session URL updated on every navigation
			def on_frame_navigated(event, session_id=None):
				if session_id == cdp_session.session_id:
					frame = event.get('frame', {})
					# Only update URL for main frame (not iframes)
					if not frame.get('parentId'):
						new_url = frame.get('url')
						if new_url:
							old_url = cdp_session.url
							cdp_session.url = new_url
							if old_url != new_url:
								self.logger.debug(
									f'[SessionManager] Updated URL for target {cdp_session.target_id[:8]}...: {old_url[:50]} → {new_url[:50]}'
								)

			# Register frame navigation handler to keep URL fresh
			cdp_session.cdp_client.register.Page.frameNavigated(on_frame_navigated)
			self.logger.debug(f'[SessionManager] Registered frame navigation handler for target {cdp_session.target_id[:8]}...')

		except Exception as e:
			# Don't fail - target might be short-lived or already detached
			error_str = str(e)
			if '-32001' in error_str or 'Session with given id not found' in error_str:
				self.logger.debug(
					f'[SessionManager] Target {cdp_session.target_id[:8]}... detached before monitoring could be enabled (normal for short-lived targets)'
				)
			else:
				self.logger.warning(
					f'[SessionManager] Failed to enable monitoring for target {cdp_session.target_id[:8]}...: {e}'
				)
