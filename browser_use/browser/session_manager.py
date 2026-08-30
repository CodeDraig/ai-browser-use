"""Event-driven CDP session management.

Manages CDP sessions by listening to Target.attachedToTarget and Target.detachedFromTarget
events, ensuring the session pool always reflects the current browser state.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from cdp_use import CDPClient
from cdp_use.cdp.fetch import RequestPausedEvent
from cdp_use.cdp.target import AttachedToTargetEvent, DetachedFromTargetEvent, SessionID, TargetID
from cdp_use.cdp.target.types import TargetInfo
from pydantic import BaseModel, ConfigDict, PrivateAttr

from browser_use.browser.focus_recovery import FocusRecovery
from browser_use.browser.frame_resolver import FrameResolver
from browser_use.browser.lifecycle_monitor import LifecycleMonitor
from browser_use.browser.navigation_policy import NavigationPolicy
from browser_use.runtime import create_task_with_error_handling
from browser_use.security import is_new_tab_page

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession


class Target(BaseModel):
	"""Browser target tracked by SessionManager."""

	model_config = ConfigDict(arbitrary_types_allowed=True, revalidate_instances='never')
	target_id: TargetID
	target_type: str
	url: str = 'about:blank'
	title: str = 'Unknown title'


class CDPSession(BaseModel):
	"""CDP communication channel tracked by SessionManager."""

	model_config = ConfigDict(arbitrary_types_allowed=True, revalidate_instances='never')
	cdp_client: CDPClient
	target_id: TargetID
	session_id: SessionID
	_lifecycle_events: Any = PrivateAttr(default=None)


class SessionManager:
	"""Event-driven CDP session manager.

	Automatically synchronizes the CDP session pool with browser state via CDP events.

	Key features:
	- Sessions added/removed automatically via Target attach/detach events
	- Multiple sessions can attach to the same target
	- Targets only removed when ALL sessions detach
	- No stale sessions - pool always reflects browser reality

	SessionManager is the SINGLE SOURCE OF TRUTH for all targets and sessions.
	"""

	def __init__(self, browser_session: 'BrowserSession'):
		self.browser_session = browser_session
		self.logger = browser_session.logger

		# All targets (entities: pages, iframes, workers)
		self._targets: dict[TargetID, 'Target'] = {}

		# All sessions (communication channels)
		self._sessions: dict[SessionID, 'CDPSession'] = {}

		# Mapping: target -> sessions attached to it
		self._target_sessions: dict[TargetID, set[SessionID]] = {}

		# Reverse mapping: session -> target it belongs to
		self._session_to_target: dict[SessionID, TargetID] = {}

		# Page lifecycle events per target, fed by ONE global Page.lifecycleEvent handler
		# registered in start_monitoring(). cdp-use's event registry is single-slot per
		# CDP method, so per-session handler registrations would replace each other and
		# leave every tab but the most recently attached one without lifecycle events.
		self._lock = asyncio.Lock()
		self.navigation_policy = NavigationPolicy(self)
		self.frames = FrameResolver(self)
		self.focus = FocusRecovery(self)
		self.lifecycle = LifecycleMonitor(self)

	async def start_monitoring(self) -> None:
		"""Start monitoring Target attach/detach events.

		Registers CDP event handlers to keep the session pool synchronized with browser state.
		Also discovers and initializes all existing targets on startup.
		"""
		if not self.browser_session._cdp_client_root:
			raise RuntimeError('CDP client not initialized')

		# Capture cdp_client_root in closure to avoid type errors
		cdp_client = self.browser_session._cdp_client_root

		# Enable target discovery to receive targetInfoChanged events automatically
		# This eliminates the need for getTargetInfo() polling calls
		await cdp_client.send.Target.setDiscoverTargets(
			params={'discover': True, 'filter': [{'type': 'page'}, {'type': 'iframe'}]}
		)

		# Register synchronous event handlers (CDP requirement)
		def on_attached(event: AttachedToTargetEvent, session_id: SessionID | None = None):
			# _handle_target_attached() handles:
			# - setAutoAttach for children
			# - Create CDPSession
			# - Enable monitoring (for pages/tabs)
			# - Add to pool
			create_task_with_error_handling(
				self._handle_target_attached(event),
				name='handle_target_attached',
				logger_instance=self.logger,
				suppress_exceptions=True,
			)

		def on_detached(event: DetachedFromTargetEvent, session_id: SessionID | None = None):
			create_task_with_error_handling(
				self._handle_target_detached(event),
				name='handle_target_detached',
				logger_instance=self.logger,
				suppress_exceptions=True,
			)

		def on_target_info_changed(event, session_id: SessionID | None = None):
			# Update session info from targetInfoChanged events (no polling needed!)
			create_task_with_error_handling(
				self._handle_target_info_changed(event),
				name='handle_target_info_changed',
				logger_instance=self.logger,
				suppress_exceptions=True,
			)

		def on_lifecycle_event(event, session_id: SessionID | None = None):
			# cdp-use registers one callback per CDP method and expects a plain
			# synchronous callback. Keep that adapter local while LifecycleMonitor
			# owns the actual buffering and target-state transition.
			self.lifecycle.handle_event(event, session_id)

		def on_request_paused(event: RequestPausedEvent, session_id: SessionID | None = None):
			# cdp-use stores one callback per CDP method. Keep Fetch routing here so
			# URL policy and authenticated-proxy handling cannot replace each other.
			create_task_with_error_handling(
				self.navigation_policy.handle_request_paused(event, session_id),
				name='handle_request_paused',
				logger_instance=self.logger,
				suppress_exceptions=True,
			)

		cdp_client.register.Target.attachedToTarget(on_attached)
		cdp_client.register.Target.detachedFromTarget(on_detached)
		cdp_client.register.Target.targetInfoChanged(on_target_info_changed)
		cdp_client.register.Page.lifecycleEvent(on_lifecycle_event)
		cdp_client.register.Fetch.requestPaused(on_request_paused)

		self.logger.debug('[SessionManager] Event monitoring started')

		# Discover and initialize ALL existing targets
		await self._initialize_existing_targets()

	async def get_target_id_from_tab_id(self, tab_id: str) -> TargetID:
		"""Get the full-length TargetID from the truncated 4-char tab_id using SessionManager."""
		for full_target_id in self.get_all_target_ids():
			if full_target_id.endswith(tab_id):
				if await self.is_target_valid(full_target_id):
					return full_target_id
				# Stale target - Chrome should have sent detach event
				# If we're here, event listener will clean it up
				self.logger.debug(f'Found stale target {full_target_id}, skipping')

		raise ValueError(f'No TargetID found ending in tab_id=...{tab_id}')

	async def get_all_pages(
		self,
		include_http: bool = True,
		include_about: bool = True,
		include_pages: bool = True,
		include_iframes: bool = False,
		include_workers: bool = False,
		include_chrome: bool = False,
		include_chrome_extensions: bool = False,
		include_chrome_error: bool = False,
	) -> list[TargetInfo]:
		"""Get all browser pages/tabs using SessionManager (source of truth)."""
		# Build TargetInfo dicts from SessionManager owned data (crystal clear ownership)
		result = []
		for target_id, target in self.get_all_targets().items():
			# Create TargetInfo dict
			target_info: TargetInfo = {
				'targetId': target.target_id,
				'type': target.target_type,
				'title': target.title,
				'url': target.url,
				'attached': True,
				'canAccessOpener': False,
			}

			# Apply filters
			if self._is_valid_target(
				target_info,
				include_http=include_http,
				include_about=include_about,
				include_pages=include_pages,
				include_iframes=include_iframes,
				include_workers=include_workers,
				include_chrome=include_chrome,
				include_chrome_extensions=include_chrome_extensions,
				include_chrome_error=include_chrome_error,
			):
				result.append(target_info)

		return result

	@staticmethod
	def _is_valid_target(
		target_info: TargetInfo,
		include_http: bool = True,
		include_chrome: bool = False,
		include_chrome_extensions: bool = False,
		include_chrome_error: bool = False,
		include_about: bool = True,
		include_iframes: bool = True,
		include_pages: bool = True,
		include_workers: bool = False,
	) -> bool:
		"""Check if a target should be processed.

		Args:
			target_info: Target info dict from CDP

		Returns:
			True if target should be processed, False if it should be skipped
		"""
		target_type = target_info.get('type', '')
		url = target_info.get('url', '')

		url_allowed, type_allowed = False, False

		# Always allow new tab pages (chrome://new-tab-page/, chrome://newtab/, about:blank)
		# so they can be redirected to about:blank in connect()
		from browser_use.security import is_new_tab_page

		if is_new_tab_page(url):
			url_allowed = True

		if url.startswith('chrome-error://') and include_chrome_error:
			url_allowed = True

		if url.startswith('chrome://') and include_chrome:
			url_allowed = True

		if url.startswith('chrome-extension://') and include_chrome_extensions:
			url_allowed = True

		# dont allow about:srcdoc! there are also other rare about: pages that we want to avoid
		if url == 'about:blank' and include_about:
			url_allowed = True

		if (url.startswith('http://') or url.startswith('https://')) and include_http:
			url_allowed = True

		if target_type in ('service_worker', 'shared_worker', 'worker') and include_workers:
			type_allowed = True

		if target_type in ('page', 'tab') and include_pages:
			type_allowed = True

		if target_type in ('iframe', 'webview') and include_iframes:
			type_allowed = True
			# Chrome often reports empty URLs for cross-origin iframe targets (OOPIFs)
			# initially via attachedToTarget, but they are still valid and accessible via CDP.
			# Allow them through so get_all_frames() can resolve their frame trees.
			if not url:
				url_allowed = True

		return url_allowed and type_allowed

	def _get_session_for_target(self, target_id: TargetID) -> 'CDPSession | None':
		"""Internal: Get ANY valid session for a target (picks first available).

		⚠️ INTERNAL API - Use browser_session.get_or_create_cdp_session() instead!
		This method has no validation, no focus management, no recovery.

		Args:
			target_id: Target ID to get session for

		Returns:
			CDPSession if exists, None if target has detached
		"""
		session_ids = self._target_sessions.get(target_id, set())
		if not session_ids:
			# Check if this is the focused target - indicates stale focus that needs cleanup
			if self.browser_session.agent_focus_target_id == target_id:
				self.logger.warning(
					f'[SessionManager] ⚠️ Attempted to get session for stale focused target {target_id[:8]}... '
					f'Clearing stale focus and triggering recovery.'
				)

				# Clear stale focus immediately (defense in depth)
				self.browser_session.agent_focus_target_id = None

				# Trigger recovery if not already in progress
				if not self.focus.in_progress:
					self.logger.warning('[SessionManager] Recovery was not in progress! Triggering now.')
					self.focus.request(target_id, task_name='recover_agent_focus_from_stale_get')
			return None
		return self._sessions.get(next(iter(session_ids)))

	def get_all_page_targets(self) -> list:
		"""Get all page/tab targets using owned data.

		Returns:
			List of Target objects for all page/tab targets
		"""
		page_targets = []
		for target in self._targets.values():
			if target.target_type in ('page', 'tab'):
				page_targets.append(target)
		return page_targets

	async def validate_session(self, target_id: TargetID) -> bool:
		"""Check if a target still has active sessions.

		Args:
			target_id: Target ID to validate

		Returns:
			True if target has active sessions, False if it should be removed
		"""
		if target_id not in self._target_sessions:
			return False
		return len(self._target_sessions[target_id]) > 0

	async def clear(self) -> None:
		"""Clear all owned data structures for cleanup."""
		async with self._lock:
			# Clear owned data (single source of truth)
			self._targets.clear()
			self._sessions.clear()
			self._target_sessions.clear()
			self._session_to_target.clear()
			self.lifecycle.clear()
			self.navigation_policy.new_page_targets.clear()
			self.navigation_policy.blocked_navigation_urls.clear()
			self.navigation_policy.setup_failures.clear()
			self.navigation_policy.fetch_sessions.clear()

		self.logger.info('[SessionManager] Cleared all owned data (targets, sessions, mappings)')

	async def is_target_valid(self, target_id: TargetID) -> bool:
		"""Check if a target is still valid and has active sessions.

		Args:
			target_id: Target ID to validate

		Returns:
			True if target is valid and has active sessions, False otherwise
		"""
		if target_id not in self._target_sessions:
			return False
		return len(self._target_sessions[target_id]) > 0

	def get_target_id_from_session_id(self, session_id: SessionID) -> TargetID | None:
		"""Look up which target a session belongs to.

		Args:
			session_id: The session ID to look up

		Returns:
			Target ID if found, None otherwise
		"""
		return self._session_to_target.get(session_id)

	def get_target(self, target_id: TargetID) -> 'Target | None':
		"""Get target from owned data.

		Args:
			target_id: Target ID to get

		Returns:
			Target object if found, None otherwise
		"""
		return self._targets.get(target_id)

	def get_all_targets(self) -> dict[TargetID, 'Target']:
		"""Get all targets (read-only access to owned data).

		Returns:
			Dict mapping target_id to Target objects
		"""
		return self._targets

	def get_all_target_ids(self) -> list[TargetID]:
		"""Get all target IDs from owned data.

		Returns:
			List of all target IDs
		"""
		return list(self._targets.keys())

	def get_all_sessions(self) -> dict[SessionID, 'CDPSession']:
		"""Get all sessions (read-only access to owned data).

		Returns:
			Dict mapping session_id to CDPSession objects
		"""
		return self._sessions

	def get_session(self, session_id: SessionID) -> 'CDPSession | None':
		"""Get session from owned data.

		Args:
			session_id: Session ID to get

		Returns:
			CDPSession object if found, None otherwise
		"""
		return self._sessions.get(session_id)

	def get_all_sessions_for_target(self, target_id: TargetID) -> list['CDPSession']:
		"""Get ALL sessions attached to a target from owned data.

		Args:
			target_id: Target ID to get sessions for

		Returns:
			List of all CDPSession objects for this target
		"""
		session_ids = self._target_sessions.get(target_id, set())
		return [self._sessions[sid] for sid in session_ids if sid in self._sessions]

	def get_target_sessions_mapping(self) -> dict[TargetID, set[SessionID]]:
		"""Get target->sessions mapping (read-only access).

		Returns:
			Dict mapping target_id to set of session_ids
		"""
		return self._target_sessions

	def get_focused_target(self) -> 'Target | None':
		"""Get the target that currently has agent focus.

		Convenience method that uses browser_session.agent_focus_target_id.

		Returns:
			Target object if agent has focus, None otherwise
		"""
		if not self.browser_session.agent_focus_target_id:
			return None
		return self.get_target(self.browser_session.agent_focus_target_id)

	async def _handle_target_attached(self, event: AttachedToTargetEvent) -> None:
		"""Handle Target.attachedToTarget event.

		Called automatically by Chrome when a new target/session is created.
		This is the ONLY place where sessions are added to the pool.
		"""
		target_id = event['targetInfo']['targetId']
		session_id = event['sessionId']
		target_type = event['targetInfo']['type']
		target_info = event['targetInfo']
		waiting_for_debugger = event.get('waitingForDebugger', False)
		if waiting_for_debugger and target_type in ('page', 'tab') and self.navigation_policy.active:
			self.navigation_policy.new_page_targets.add(target_id)

		self.logger.debug(
			f'[SessionManager] Target attached: {target_id[:8]}... (session={session_id[:8]}..., '
			f'type={target_type}, waitingForDebugger={waiting_for_debugger})'
		)

		# Defensive check: browser may be shutting down and _cdp_client_root could be None
		if self.browser_session._cdp_client_root is None:
			self.logger.debug(
				f'[SessionManager] Skipping target attach for {target_id[:8]}... - browser shutting down (no CDP client)'
			)
			return

		# Enable auto-attach for this session's children (do this FIRST, outside lock)
		try:
			await self.browser_session._cdp_client_root.send.Target.setAutoAttach(
				params={'autoAttach': True, 'waitForDebuggerOnStart': False, 'flatten': True}, session_id=session_id
			)
		except Exception as e:
			error_str = str(e)
			# Expected for short-lived targets (workers, temp iframes) that detach before this executes
			if '-32001' not in error_str and 'Session with given id not found' not in error_str:
				self.logger.debug(f'[SessionManager] Auto-attach failed for {target_type}: {e}')

		async with self._lock:
			# Track this session for the target
			if target_id not in self._target_sessions:
				self._target_sessions[target_id] = set()

			self._target_sessions[target_id].add(session_id)
			self._session_to_target[session_id] = target_id

			# Create or update Target inside the same lock so that get_target() is never
			# called in the window between _target_sessions being set and _targets being set.
			if target_id not in self._targets:
				target = Target(
					target_id=target_id,
					target_type=target_type,
					url=target_info.get('url', 'about:blank'),
					title=target_info.get('title', 'Unknown title'),
				)
				self._targets[target_id] = target
				self.logger.debug(f'[SessionManager] Created target {target_id[:8]}... (type={target_type})')
			else:
				# Update existing target info
				existing_target = self._targets[target_id]
				existing_target.url = target_info.get('url', existing_target.url)
				existing_target.title = target_info.get('title', existing_target.title)

		# Create CDPSession (communication channel)
		assert self.browser_session._cdp_client_root is not None, 'Root CDP client required'

		cdp_session = CDPSession(
			cdp_client=self.browser_session._cdp_client_root,
			target_id=target_id,
			session_id=session_id,
		)

		# Add to sessions dict
		self._sessions[session_id] = cdp_session

		self.logger.debug(
			f'[SessionManager] Created session {session_id[:8]}... for target {target_id[:8]}... '
			f'(total sessions: {len(self._sessions)})'
		)

		# Enable lifecycle/network monitoring and the URL gate before a paused
		# page is allowed to execute. Proxy-only non-page targets still need Fetch.
		try:
			if target_type in ('page', 'tab'):
				await self.lifecycle.enable_page_monitoring(cdp_session)
			else:
				await self.navigation_policy.enable_fetch_for_session(cdp_session, target_type=target_type)
		except Exception as error:
			policy_gate_missing = (
				self.navigation_policy.active
				and target_type in ('page', 'tab')
				and target_id not in self.navigation_policy.fetch_sessions
			)
			if policy_gate_missing:
				self.navigation_policy.setup_failures[target_id] = f'{type(error).__name__}: {error}'
				await self.navigation_policy.remediate_blocked_navigation(
					target_id, target_info.get('url', ''), setup_failed=True
				)
				return
			# URL policy only governs page targets. Never strand workers or other
			# unrelated targets at the debugger boundary when optional setup fails.
			await self._resume_target_if_waiting(session_id, waiting_for_debugger)
			raise

		# Resume execution if waiting for debugger
		await self._resume_target_if_waiting(session_id, waiting_for_debugger)

	async def _resume_target_if_waiting(self, session_id: SessionID, waiting_for_debugger: bool) -> None:
		if not waiting_for_debugger:
			return
		try:
			assert self.browser_session._cdp_client_root is not None
			await self.browser_session._cdp_client_root.send.Runtime.runIfWaitingForDebugger(session_id=session_id)
		except Exception as error:
			self.logger.warning(f'[SessionManager] Failed to resume execution: {error}')

	async def _handle_target_info_changed(self, event: dict) -> None:
		"""Handle Target.targetInfoChanged event.

		Updates target title/URL without polling getTargetInfo().
		Chrome fires this automatically when title or URL changes.
		"""
		target_info = event.get('targetInfo', {})
		target_id = target_info.get('targetId')

		if not target_id:
			return

		updated_url = target_info.get('url', '')
		target_type: str | None = None
		async with self._lock:
			# Update target if it exists (source of truth for url/title)
			if target_id in self._targets:
				target = self._targets[target_id]

				target.title = target_info.get('title', target.title)
				target.url = target_info.get('url', target.url)
				target_type = target.target_type

		# Fetch is the pre-request boundary. Target-info enforcement is a
		# containment fallback for non-network schemes and unexpected CDP gaps.
		if not self.navigation_policy.active or target_type not in ('page', 'tab') or not updated_url:
			return
		if self.navigation_policy.is_url_allowed(updated_url):
			if not is_new_tab_page(updated_url):
				self.navigation_policy.new_page_targets.discard(target_id)
			self.navigation_policy.blocked_navigation_urls.pop(target_id, None)
			return
		await self.navigation_policy.remediate_blocked_navigation(target_id, updated_url)

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
		fetch_owner_detached = False

		async with self._lock:
			tracked_target = self._targets.get(target_id)
			target_type = tracked_target.target_type if tracked_target else None
			fetch_owner_detached = self.navigation_policy.fetch_sessions.get(target_id) == session_id
			if fetch_owner_detached:
				self.navigation_policy.fetch_sessions.pop(target_id, None)

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
					self.logger.debug(f'[SessionManager] No sessions remain for target {target_id[:8]}..., removing target')

					target_fully_removed = True

					# Check if agent_focus points to this target
					agent_focus_lost = self.browser_session.agent_focus_target_id == target_id

					# Immediately clear stale focus to prevent operations on detached target
					if agent_focus_lost:
						self.logger.debug(
							f'[SessionManager] Clearing stale agent_focus_target_id {target_id[:8]}... '
							f'to prevent operations on detached target'
						)
						self.browser_session.agent_focus_target_id = None

					# Get target type before removing (needed for TabClosedEvent dispatch)
					target = self._targets.get(target_id)
					target_type = target.target_type if target else None

					# Remove target (entity) from owned data
					if target_id in self._targets:
						self._targets.pop(target_id)
						self.logger.debug(
							f'[SessionManager] Removed target {target_id[:8]}... (remaining targets: {len(self._targets)})'
						)

					# Clean up tracking
					del self._target_sessions[target_id]
					self.lifecycle.remove_target(target_id)
			else:
				# Target not tracked - already removed or never attached
				self.logger.debug(
					f'[SessionManager] Session detached from untracked target: target={target_id[:8]}... '
					f'session={session_id[:8]}... (target was already removed or attach event was missed)'
				)

			# Remove session from owned sessions dict
			if session_id in self._sessions:
				self._sessions.pop(session_id)
				self.logger.debug(
					f'[SessionManager] Removed session {session_id[:8]}... (remaining sessions: {len(self._sessions)})'
				)

			# Remove from reverse mapping
			if session_id in self._session_to_target:
				del self._session_to_target[session_id]
			self.lifecycle.remove_session(session_id)
			if target_fully_removed:
				self.navigation_policy.new_page_targets.discard(target_id)
				self.navigation_policy.blocked_navigation_urls.pop(target_id, None)
				self.navigation_policy.setup_failures.pop(target_id, None)
				self.navigation_policy.fetch_sessions.pop(target_id, None)

		# Keep one Fetch owner when Chrome detaches one of several flattened
		# sessions for a still-live target.
		if fetch_owner_detached and not target_fully_removed and not self.browser_session._intentional_stop:
			replacement_session = self._get_session_for_target(target_id)
			try:
				if replacement_session is None:
					raise RuntimeError('no replacement CDP session is available')
				await self.navigation_policy.enable_fetch_for_session(
					replacement_session,
					target_type=target_type or 'unknown',
				)
			except Exception as error:
				if self.navigation_policy.active and target_type in ('page', 'tab'):
					self.navigation_policy.setup_failures[target_id] = f'{type(error).__name__}: {error}'
					self.navigation_policy.blocked_navigation_urls.pop(target_id, None)
					target = self._targets.get(target_id)
					await self.navigation_policy.remediate_blocked_navigation(
						target_id,
						target.url if target else '',
						setup_failed=True,
					)
				else:
					self.logger.debug(f'[SessionManager] Could not rebind Fetch for {target_id}: {error}')

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
			# Create recovery task instead of awaiting directly - allows concurrent operations to wait on same recovery
			self.focus.request(target_id, task_name='recover_agent_focus')

	async def _initialize_existing_targets(self) -> None:
		"""Discover and initialize all existing targets at startup.

		Attaches to each target and initializes it SYNCHRONOUSLY.
		Chrome will also fire attachedToTarget events, but _handle_target_attached() is
		idempotent (checks if target already in pool), so duplicate handling is safe.

		This eliminates race conditions - monitoring is guaranteed ready before navigation.
		"""
		cdp_client = self.browser_session._cdp_client_root
		assert cdp_client is not None

		# Get all existing targets
		targets_result = await cdp_client.send.Target.getTargets()
		existing_targets = targets_result.get('targetInfos', [])

		self.logger.debug(f'[SessionManager] Discovered {len(existing_targets)} existing targets')

		# Track target IDs for verification
		target_ids_to_wait_for = []

		# Just attach to ALL existing targets - Chrome fires attachedToTarget events
		# The on_attached handler (via create_task) does ALL the work
		for target in existing_targets:
			target_id = target['targetId']
			target_type = target.get('type', 'unknown')

			try:
				# Just attach - event handler does everything
				await cdp_client.send.Target.attachToTarget(params={'targetId': target_id, 'flatten': True})
				target_ids_to_wait_for.append(target_id)
			except Exception as e:
				self.logger.debug(
					f'[SessionManager] Failed to attach to existing target {target_id[:8]}... (type={target_type}): {e}'
				)

		# Wait for event handlers to complete their work (they run via create_task)
		# Use event-driven approach instead of polling for better performance
		ready_event = asyncio.Event()

		async def check_all_ready():
			"""Check if all sessions are ready and signal completion."""
			while True:
				ready_count = 0
				for tid in target_ids_to_wait_for:
					target = self._targets.get(tid)
					target_type = target.target_type if target else 'unknown'
					if self.lifecycle.target_monitoring_ready(tid, target_type):
						ready_count += 1

				if ready_count == len(target_ids_to_wait_for):
					ready_event.set()
					return

				await asyncio.sleep(0.05)

		# Start checking in background
		check_task = create_task_with_error_handling(
			check_all_ready(), name='check_all_targets_ready', logger_instance=self.logger
		)

		try:
			# Wait for completion with timeout
			await asyncio.wait_for(ready_event.wait(), timeout=2.0)
		except TimeoutError:
			# Timeout - count what's ready
			ready_count = 0
			for tid in target_ids_to_wait_for:
				target = self._targets.get(tid)
				target_type = target.target_type if target else 'unknown'
				if self.lifecycle.target_monitoring_ready(tid, target_type):
					ready_count += 1
			self.logger.warning(
				f'[SessionManager] Initialization timeout after 2.0s: {ready_count}/{len(target_ids_to_wait_for)} sessions ready'
			)
			if self.navigation_policy.active:
				# A connected page without its Fetch gate would make the policy
				# advisory. Ignore transient pages that have already disappeared,
				# but reject any live page whose setup never completed.
				try:
					current_targets = (await cdp_client.send.Target.getTargets()).get('targetInfos', [])
				except Exception:
					current_targets = existing_targets
				for target_info in current_targets:
					if target_info.get('type') not in ('page', 'tab'):
						continue
					target_id = target_info['targetId']
					if not self.lifecycle.target_monitoring_ready(target_id, target_info['type']):
						self.navigation_policy.setup_failures.setdefault(
							target_id,
							'initialization timed out before Fetch was installed',
						)
		finally:
			check_task.cancel()
			try:
				await check_task
			except asyncio.CancelledError:
				pass

		if self.navigation_policy.active and self.navigation_policy.setup_failures:
			failures = ', '.join(f'{target_id}: {error}' for target_id, error in self.navigation_policy.setup_failures.items())
			raise RuntimeError(f'URL policy interception could not be installed: {failures}')
