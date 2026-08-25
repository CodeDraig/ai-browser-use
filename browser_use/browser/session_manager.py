"""Event-driven CDP session management.

Manages CDP sessions by listening to Target.attachedToTarget and Target.detachedFromTarget
events, ensuring the session pool always reflects the current browser state.
"""

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Any

from cdp_use import CDPClient
from cdp_use.cdp.fetch import EnableParameters, RequestPattern, RequestPausedEvent
from cdp_use.cdp.target import AttachedToTargetEvent, DetachedFromTargetEvent, SessionID, TargetID
from pydantic import BaseModel, ConfigDict, PrivateAttr

from browser_use.dom.views import EnhancedDOMTreeNode, TargetInfo
from browser_use.runtime import create_task_with_error_handling
from browser_use.security import is_new_tab_page, is_url_allowed_by_policy, url_policy_configured

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

	_TARGET_CLOSE_ATTEMPTS = 2
	_TARGET_CLOSE_CONFIRMATION_TIMEOUT = 1.0
	_TARGET_CLOSE_POLL_INTERVAL = 0.05

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
		self._lifecycle_events: dict[TargetID, deque[dict[str, Any]]] = {}
		self._main_frame_ids: dict[SessionID, str] = {}
		self._new_page_targets: set[TargetID] = set()
		self._blocked_navigation_urls: dict[TargetID, str] = {}
		self._policy_setup_failures: dict[TargetID, str] = {}
		self._fetch_sessions: dict[TargetID, SessionID] = {}

		self._lock = asyncio.Lock()
		self._recovery_lock = asyncio.Lock()
		self._fetch_setup_lock = asyncio.Lock()

		# Focus recovery coordination - event-driven instead of polling
		self._recovery_in_progress: bool = False
		self._recovery_complete_event: asyncio.Event | None = None
		self._recovery_task: asyncio.Task | None = None

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
			# ONE global handler for all targets: route by session_id -> target_id.
			# Registering per-session closures instead would clobber each other in
			# cdp-use's single-slot registry (one handler per CDP method).
			if not session_id:
				return
			target_id = self.get_target_id_from_session_id(session_id)
			if not target_id:
				return
			event_name = event.get('name', 'unknown')
			self.get_lifecycle_events(target_id).append(
				{
					'name': event_name,
					'loaderId': event.get('loaderId'),
					'timestamp': asyncio.get_event_loop().time(),
				}
			)
			# Keep a newly attached page marked through its entire redirect chain.
			# Clearing on the first allowed Fetch request would make a blocked
			# redirect destination look like navigation in an existing tab.
			if event_name == 'load':
				target = self.get_target(target_id)
				if target is not None and not is_new_tab_page(target.url):
					self._new_page_targets.discard(target_id)

		def on_request_paused(event: RequestPausedEvent, session_id: SessionID | None = None):
			# cdp-use stores one callback per CDP method. Keep Fetch routing here so
			# URL policy and authenticated-proxy handling cannot replace each other.
			create_task_with_error_handling(
				self._handle_request_paused(event, session_id),
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

	@property
	def url_policy_active(self) -> bool:
		"""Whether top-level navigation must be intercepted for this profile."""
		profile = self.browser_session.browser_profile
		return url_policy_configured(
			allowed_domains=profile.allowed_domains,
			prohibited_domains=profile.prohibited_domains,
			block_ip_addresses=profile.block_ip_addresses,
		)

	def _is_url_allowed(self, url: str) -> bool:
		profile = self.browser_session.browser_profile
		return is_url_allowed_by_policy(
			url,
			allowed_domains=profile.allowed_domains,
			prohibited_domains=profile.prohibited_domains,
			block_ip_addresses=profile.block_ip_addresses,
			log_warnings=True,
		)

	def mark_new_page_target(self, target_id: TargetID) -> None:
		"""Mark a caller-created blank target for blocked-tab containment."""
		self._new_page_targets.add(target_id)

	async def wait_for_policy_ready_page(self, target_id: TargetID, *, timeout: float = 2.0) -> 'CDPSession':
		"""Wait until a page target has its top-frame Fetch gate installed.

		A policy-enabled caller must not navigate a newly created target before
		this succeeds. Setup failures close the target and surface a hard error so
		no caller can accidentally continue with an ungated page.
		"""
		if not self.url_policy_active:
			raise RuntimeError('wait_for_policy_ready_page() requires an active URL policy')

		loop = asyncio.get_running_loop()
		deadline = loop.time() + timeout
		while loop.time() < deadline:
			setup_failure = self._policy_setup_failures.get(target_id)
			if setup_failure:
				await self._remediate_blocked_navigation(target_id, 'about:blank', setup_failed=True)
				raise RuntimeError(f'URL policy interception could not be installed for target {target_id}: {setup_failure}')

			target = self._targets.get(target_id)
			if target is not None and self._target_monitoring_ready(target_id, target.target_type):
				fetch_session_id = self._fetch_sessions.get(target_id)
				cdp_session = self._sessions.get(fetch_session_id) if fetch_session_id else None
				if cdp_session is not None:
					return cdp_session

			await asyncio.sleep(0.05)

		setup_failure = f'timed out after {timeout:.1f}s waiting for top-frame Fetch interception'
		self._policy_setup_failures[target_id] = setup_failure
		await self._remediate_blocked_navigation(target_id, 'about:blank', setup_failed=True)
		raise RuntimeError(f'URL policy interception could not be installed for target {target_id}: {setup_failure}')

	async def _continue_fetch_request(self, request_id: str, session_id: SessionID | None) -> None:
		cdp_client = self.browser_session._cdp_client_root
		if cdp_client is None:
			return
		try:
			await cdp_client.send.Fetch.continueRequest(params={'requestId': request_id}, session_id=session_id)
		except Exception as error:
			# Detach races are normal after a tab closes.
			self.logger.debug(f'[SessionManager] Could not continue Fetch request {request_id}: {error}')

	async def _handle_request_paused(
		self,
		event: RequestPausedEvent,
		session_id: SessionID | None,
	) -> None:
		"""Resolve every paused request once and gate top-level documents."""
		request_id = event.get('requestId') or event.get('request_id')
		if not request_id:
			return

		if not self.url_policy_active or not session_id or event.get('resourceType') != 'Document':
			await self._continue_fetch_request(request_id, session_id)
			return

		target_id = self.get_target_id_from_session_id(session_id)
		target = self.get_target(target_id) if target_id else None
		main_frame_id = self._main_frame_ids.get(session_id)
		frame_id = event.get('frameId')
		if not target_id or not target or target.target_type not in ('page', 'tab'):
			await self._continue_fetch_request(request_id, session_id)
			return
		if not main_frame_id:
			# A page session is not safe to run under policy unless its top frame was
			# identified before Fetch interception was enabled.
			await self._fail_fetch_request(request_id, session_id)
			await self._remediate_blocked_navigation(target_id, event['request']['url'], setup_failed=True)
			return
		if frame_id != main_frame_id:
			# Subframes and subresources are deliberately outside this policy.
			await self._continue_fetch_request(request_id, session_id)
			return

		url = event.get('request', {}).get('url', '')
		if self._is_url_allowed(url):
			self._blocked_navigation_urls.pop(target_id, None)
			await self._continue_fetch_request(request_id, session_id)
			return

		# Failing the intercepted request prevents DNS, proxy, and destination
		# traffic before containment changes the target state.
		await self._fail_fetch_request(request_id, session_id)
		await self._remediate_blocked_navigation(target_id, url)

	async def _fail_fetch_request(self, request_id: str, session_id: SessionID) -> None:
		cdp_client = self.browser_session._cdp_client_root
		if cdp_client is None:
			return
		try:
			await cdp_client.send.Fetch.failRequest(
				params={'requestId': request_id, 'errorReason': 'BlockedByClient'},
				session_id=session_id,
			)
		except Exception as error:
			self.logger.debug(f'[SessionManager] Could not fail blocked Fetch request {request_id}: {error}')

	async def _remediate_blocked_navigation(
		self,
		target_id: TargetID,
		url: str,
		*,
		setup_failed: bool = False,
		require_target_closed: bool = False,
	) -> None:
		"""Contain one blocked navigation, optionally requiring confirmed target closure."""
		already_reported = self._blocked_navigation_urls.get(target_id) == url
		if already_reported and not require_target_closed:
			return

		is_new_page = target_id in self._new_page_targets
		if not already_reported:
			self._blocked_navigation_urls[target_id] = url

			from browser_use.browser.events import BrowserErrorEvent

			error_type = 'TabCreationBlocked' if is_new_page else 'NavigationBlocked'
			reason = 'policy_setup_failed' if setup_failed else 'not_in_allowed_domains'
			self.browser_session.event_bus.dispatch(
				BrowserErrorEvent(
					error_type=error_type,
					message=f'Navigation blocked to disallowed URL: {url}',
					details={'url': url, 'target_id': target_id, 'reason': reason},
				)
			)

		cdp_client = self.browser_session._cdp_client_root
		if cdp_client is None:
			if require_target_closed:
				raise RuntimeError(
					f'URL policy blocked {url}, but target {target_id} could not be confirmed closed: CDP client unavailable'
				)
			return
		try:
			if is_new_page or setup_failed:
				if require_target_closed:
					await self._close_target_with_confirmation(target_id, url)
				else:
					await cdp_client.send.Target.closeTarget(params={'targetId': target_id})
				self.logger.warning(f'⛔️ Closed target blocked by URL policy: {url}')
			else:
				cdp_session = self._get_session_for_target(target_id)
				if cdp_session:
					await cdp_client.send.Page.navigate(
						params={'url': 'about:blank'},
						session_id=cdp_session.session_id,
					)
				self.logger.warning(f'⛔️ Replaced blocked top-level navigation with about:blank: {url}')
		except Exception as error:
			self.logger.warning(f'⛔️ Failed to contain blocked navigation to {url}: {type(error).__name__}: {error}')
			if require_target_closed:
				raise

	async def _close_target_with_confirmation(self, target_id: TargetID, url: str) -> None:
		"""Close one target and confirm disappearance from Chromium's target inventory."""
		cdp_client = self.browser_session._cdp_client_root
		if cdp_client is None:
			raise RuntimeError(
				f'URL policy blocked {url}, but target {target_id} could not be confirmed closed: CDP client unavailable'
			)

		last_close_error: Exception | None = None
		last_inventory_error: Exception | None = None
		loop = asyncio.get_running_loop()

		for _attempt in range(self._TARGET_CLOSE_ATTEMPTS):
			try:
				await cdp_client.send.Target.closeTarget(params={'targetId': target_id})
				last_close_error = None
			except Exception as error:
				last_close_error = error

			deadline = loop.time() + self._TARGET_CLOSE_CONFIRMATION_TIMEOUT
			while True:
				try:
					targets_result = await cdp_client.send.Target.getTargets()
					last_inventory_error = None
					if not any(target_info.get('targetId') == target_id for target_info in targets_result.get('targetInfos', [])):
						return
				except Exception as error:
					last_inventory_error = error

				if loop.time() >= deadline:
					break
				await asyncio.sleep(self._TARGET_CLOSE_POLL_INTERVAL)

		details: list[str] = []
		if last_close_error is not None:
			details.append(f'last close error: {type(last_close_error).__name__}: {last_close_error}')
		if last_inventory_error is not None:
			details.append(f'last inventory error: {type(last_inventory_error).__name__}: {last_inventory_error}')
		detail_suffix = f' ({"; ".join(details)})' if details else ''
		raise RuntimeError(
			f'URL policy blocked {url}, but target {target_id} could not be confirmed closed '
			f'after {self._TARGET_CLOSE_ATTEMPTS} attempts{detail_suffix}'
		)

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

	async def get_all_frames(self) -> tuple[dict[str, dict], dict[str, str]]:
		"""Get a complete frame hierarchy from all browser targets.

		Returns:
			Tuple of (all_frames, target_sessions) where:
			- all_frames: dict mapping frame_id -> frame info dict with all metadata
			- target_sessions: dict mapping target_id -> session_id for active sessions
		"""
		all_frames = {}  # frame_id -> FrameInfo dict
		target_sessions = {}  # target_id -> session_id (keep sessions alive during collection)

		# Check if cross-origin iframe support is enabled
		include_cross_origin = self.browser_session.browser_profile.cross_origin_iframes

		# Get all targets - only include iframes if cross-origin support is enabled
		targets = await self.get_all_pages(
			include_http=True,
			include_about=True,
			include_pages=True,
			include_iframes=include_cross_origin,  # Only include iframe targets if flag is set
			include_workers=False,
			include_chrome=False,
			include_chrome_extensions=False,
			include_chrome_error=include_cross_origin,  # Only include error pages if cross-origin is enabled
		)
		all_targets = targets

		# First pass: collect frame trees from ALL targets
		for target in all_targets:
			target_id = target['targetId']

			# Skip iframe targets if cross-origin support is disabled
			if not include_cross_origin and target.get('type') == 'iframe':
				continue

			# When cross-origin support is disabled, only process the current target
			if not include_cross_origin:
				# Only process the current focus target
				if self.browser_session.agent_focus_target_id and target_id != self.browser_session.agent_focus_target_id:
					continue
				# Use the existing agent_focus target's session - use safe API with focus=False
				try:
					cdp_session = await self.browser_session.get_or_create_cdp_session(
						self.browser_session.agent_focus_target_id, focus=False
					)
				except ValueError:
					continue  # Skip if no session available
			else:
				# Get cached session for this target (don't change focus - iterating frames)
				try:
					cdp_session = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
				except ValueError:
					continue  # Target may have detached between discovery and session creation

			if cdp_session:
				target_sessions[target_id] = cdp_session.session_id

				try:
					# Try to get frame tree (not all target types support this)
					frame_tree_result = await cdp_session.cdp_client.send.Page.getFrameTree(session_id=cdp_session.session_id)

					# Process the frame tree recursively
					def process_frame_tree(node, parent_frame_id=None):
						"""Recursively process frame tree and add to all_frames."""
						frame = node.get('frame', {})
						current_frame_id = frame.get('id')

						if current_frame_id:
							# For iframe targets, check if the frame has a parentId field
							# This indicates it's an OOPIF with a parent in another target
							actual_parent_id = frame.get('parentId') or parent_frame_id

							# Create frame info with all CDP response data plus our additions
							frame_info = {
								**frame,  # Include all original frame data: id, url, parentId, etc.
								'frameTargetId': target_id,  # Target that can access this frame
								'parentFrameId': actual_parent_id,  # Use parentId from frame if available
								'childFrameIds': [],  # Will be populated below
								'isCrossOrigin': False,  # Will be determined based on context
								'isValidTarget': self._is_valid_target(
									target,
									include_http=True,
									include_about=True,
									include_pages=True,
									include_iframes=True,
									include_workers=False,
									include_chrome=False,  # chrome://newtab, chrome://settings, etc. are not valid frames we can control (for sanity reasons)
									include_chrome_extensions=False,  # chrome-extension://
									include_chrome_error=False,  # chrome-error://  (e.g. when iframes fail to load or are blocked by uBlock Origin)
								),
							}

							# Check if frame is cross-origin based on crossOriginIsolatedContextType
							cross_origin_type = frame.get('crossOriginIsolatedContextType')
							if cross_origin_type and cross_origin_type != 'NotIsolated':
								frame_info['isCrossOrigin'] = True

							# For iframe targets, the frame itself is likely cross-origin
							if target.get('type') == 'iframe':
								frame_info['isCrossOrigin'] = True

							# Skip cross-origin frames if support is disabled
							if not include_cross_origin and frame_info.get('isCrossOrigin'):
								return  # Skip this frame and its children

							# Add child frame IDs (note: OOPIFs won't appear here)
							child_frames = node.get('childFrames', [])
							for child in child_frames:
								child_frame = child.get('frame', {})
								child_frame_id = child_frame.get('id')
								if child_frame_id:
									frame_info['childFrameIds'].append(child_frame_id)

							# Store or merge frame info
							if current_frame_id in all_frames:
								# Frame already seen from another target, merge info
								existing = all_frames[current_frame_id]
								# If this is an iframe target, it has direct access to the frame
								if target.get('type') == 'iframe':
									existing['frameTargetId'] = target_id
									existing['isCrossOrigin'] = True
							else:
								all_frames[current_frame_id] = frame_info

							# Process child frames recursively (only if we're not skipping this frame)
							if include_cross_origin or not frame_info.get('isCrossOrigin'):
								for child in child_frames:
									process_frame_tree(child, current_frame_id)

					# Process the entire frame tree
					process_frame_tree(frame_tree_result.get('frameTree', {}))

				except Exception as e:
					# Target doesn't support Page domain or has no frames
					self.logger.debug(f'Failed to get frame tree for target {target_id}: {e}')

		# Second pass: populate backend node IDs and parent target IDs
		# Only do this if cross-origin support is enabled
		if include_cross_origin:
			await self._populate_frame_metadata(all_frames, target_sessions)

		return all_frames, target_sessions

	async def _populate_frame_metadata(self, all_frames: dict[str, dict], target_sessions: dict[str, str]) -> None:
		"""Populate additional frame metadata like backend node IDs and parent target IDs.

		Args:
			all_frames: Frame hierarchy dict to populate
			target_sessions: Active target sessions
		"""
		for frame_id_iter, frame_info in all_frames.items():
			parent_frame_id = frame_info.get('parentFrameId')

			if parent_frame_id and parent_frame_id in all_frames:
				parent_frame_info = all_frames[parent_frame_id]
				parent_target_id = parent_frame_info.get('frameTargetId')

				# Store parent target ID
				frame_info['parentTargetId'] = parent_target_id

				# Try to get backend node ID from parent context
				if parent_target_id in target_sessions:
					assert parent_target_id is not None
					parent_session_id = target_sessions[parent_target_id]
					try:
						# Enable DOM domain
						await self.browser_session.cdp_client.send.DOM.enable(session_id=parent_session_id)

						# Get frame owner info to find backend node ID
						frame_owner = await self.browser_session.cdp_client.send.DOM.getFrameOwner(
							params={'frameId': frame_id_iter}, session_id=parent_session_id
						)

						if frame_owner:
							frame_info['backendNodeId'] = frame_owner.get('backendNodeId')
							frame_info['nodeId'] = frame_owner.get('nodeId')

					except Exception:
						# Frame owner not available (likely cross-origin)
						pass

	async def find_frame_target(self, frame_id: str, all_frames: dict[str, dict] | None = None) -> dict | None:
		"""Find the frame info for a specific frame ID.

		Args:
			frame_id: The frame ID to search for
			all_frames: Optional pre-built frame hierarchy. If None, will call get_all_frames()

		Returns:
			Frame info dict if found, None otherwise
		"""
		if all_frames is None:
			all_frames, _ = await self.get_all_frames()

		return all_frames.get(frame_id)

	async def cdp_client_for_target(self, target_id: TargetID) -> CDPSession:
		return await self.browser_session.get_or_create_cdp_session(target_id, focus=False)

	async def cdp_client_for_frame(self, frame_id: str) -> CDPSession:
		"""Get a CDP client attached to the target containing the specified frame.

		Builds a unified frame hierarchy from all targets to find the correct target
		for any frame, including OOPIFs (Out-of-Process iframes).

		Args:
			frame_id: The frame ID to search for

		Returns:
			Tuple of (cdp_cdp_session, target_id) for the target containing the frame

		Raises:
			ValueError: If the frame is not found in any target
		"""
		# If cross-origin iframes are disabled, just use the main session
		if not self.browser_session.browser_profile.cross_origin_iframes:
			return await self.browser_session.get_or_create_cdp_session()

		# Get complete frame hierarchy
		all_frames, target_sessions = await self.get_all_frames()

		# Find the requested frame
		frame_info = await self.find_frame_target(frame_id, all_frames)

		if frame_info:
			target_id = frame_info.get('frameTargetId')

			if target_id in target_sessions:
				assert target_id is not None
				# Use existing session
				session_id = target_sessions[target_id]
				# Return the client with session attached (don't change focus)
				return await self.browser_session.get_or_create_cdp_session(target_id, focus=False)

		# Frame not found
		raise ValueError(f"Frame with ID '{frame_id}' not found in any target")

	async def cdp_client_for_node(self, node: EnhancedDOMTreeNode) -> CDPSession:
		"""Get CDP client for a specific DOM node based on its frame.

		IMPORTANT: backend_node_id is only valid in the session where the DOM was captured.
		We trust the node's session_id/frame_id/target_id instead of searching all sessions.
		"""

		# Strategy 1: If node has session_id, try to use that exact session (most specific)
		if node.session_id:
			try:
				# Find the CDP session by session_id from SessionManager
				cdp_session = self.get_session(node.session_id)
				if cdp_session:
					# Get target to log URL
					target = self.get_target(cdp_session.target_id)
					target_url = target.url if target else 'detached target'
					self.logger.debug(f'✅ Using session from node.session_id for node {node.backend_node_id}: {target_url}')
					return cdp_session
			except Exception as e:
				self.logger.debug(f'Failed to get session by session_id {node.session_id}: {e}')

		# Strategy 2: If node has frame_id, use that frame's session
		if node.frame_id:
			try:
				cdp_session = await self.cdp_client_for_frame(node.frame_id)
				target = self.get_target(cdp_session.target_id)
				target_url = target.url if target else 'detached target'
				self.logger.debug(f'✅ Using session from node.frame_id for node {node.backend_node_id}: {target_url}')
				return cdp_session
			except Exception as e:
				self.logger.debug(f'Failed to get session for frame {node.frame_id}: {e}')

		# Strategy 3: If node has target_id, use that target's session
		if node.target_id:
			try:
				cdp_session = await self.browser_session.get_or_create_cdp_session(target_id=node.target_id, focus=False)
				target = self.get_target(cdp_session.target_id)
				target_url = target.url if target else 'detached target'
				self.logger.debug(f'✅ Using session from node.target_id for node {node.backend_node_id}: {target_url}')
				return cdp_session
			except Exception as e:
				self.logger.debug(f'Failed to get session for target {node.target_id}: {e}')

		# Strategy 4: Fallback to agent_focus_target_id (the page where agent is currently working)
		if self.browser_session.agent_focus_target_id:
			target = self.get_target(self.browser_session.agent_focus_target_id)
			try:
				# Use safe API with focus=False to avoid changing focus
				cdp_session = await self.browser_session.get_or_create_cdp_session(
					self.browser_session.agent_focus_target_id, focus=False
				)
				if target:
					self.logger.warning(
						f'⚠️ Node {node.backend_node_id} has no session/frame/target info. Using agent_focus session: {target.url}'
					)
				return cdp_session
			except ValueError:
				pass  # Fall through to last resort

		# Last resort: use main session
		self.logger.error(f'❌ No session info for node {node.backend_node_id} and no agent_focus available. Using main session.')
		return await self.browser_session.get_or_create_cdp_session()

	def get_lifecycle_events(self, target_id: TargetID) -> 'deque[dict[str, Any]]':
		"""Get (creating if needed) the lifecycle event buffer for a target."""
		events = self._lifecycle_events.get(target_id)
		if events is None:
			events = deque(maxlen=50)
			self._lifecycle_events[target_id] = events
		return events

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
				if not self._recovery_in_progress:
					self.logger.warning('[SessionManager] Recovery was not in progress! Triggering now.')
					self._recovery_task = create_task_with_error_handling(
						self._recover_agent_focus(target_id),
						name='recover_agent_focus_from_stale_get',
						logger_instance=self.logger,
						suppress_exceptions=False,
					)
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
			self._lifecycle_events.clear()
			self._main_frame_ids.clear()
			self._new_page_targets.clear()
			self._blocked_navigation_urls.clear()
			self._policy_setup_failures.clear()
			self._fetch_sessions.clear()

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

	async def ensure_valid_focus(self, timeout: float = 3.0) -> bool:
		"""Ensure agent_focus_target_id points to a valid, attached CDP session.

		If the focus target is stale (detached), this method waits for automatic recovery.
		Uses event-driven coordination instead of polling for efficiency.

		Args:
			timeout: Maximum time to wait for recovery in seconds (default: 3.0)

		Returns:
			True if focus is valid or successfully recovered, False if no focus or recovery failed
		"""
		if not self.browser_session.agent_focus_target_id:
			# No focus at all - might be initial state or complete failure
			if self._recovery_in_progress and self._recovery_complete_event:
				# Recovery is happening, wait for it
				try:
					await asyncio.wait_for(self._recovery_complete_event.wait(), timeout=timeout)
					# Check again after recovery - simple existence check
					focus_id = self.browser_session.agent_focus_target_id
					return bool(focus_id and self._get_session_for_target(focus_id))
				except TimeoutError:
					self.logger.error(f'[SessionManager] ❌ Timed out waiting for recovery after {timeout}s')
					return False
			return False

		# Simple existence check - does the focused target have a session?
		cdp_session = self._get_session_for_target(self.browser_session.agent_focus_target_id)
		if cdp_session:
			# Session exists - validate it's still active
			is_valid = await self.validate_session(self.browser_session.agent_focus_target_id)
			if is_valid:
				return True

		# Focus is stale - wait for recovery using event instead of polling
		stale_target_id = self.browser_session.agent_focus_target_id
		self.logger.warning(
			f'[SessionManager] ⚠️ Stale agent_focus detected (target {stale_target_id[:8] if stale_target_id else "None"}... detached), '
			f'waiting for recovery...'
		)

		# Check if recovery is already in progress
		if not self._recovery_in_progress:
			self.logger.warning(
				'[SessionManager] ⚠️ Recovery not in progress for stale focus! '
				'This indicates a bug - recovery should have been triggered.'
			)
			return False

		# Wait for recovery complete event (event-driven, not polling!)
		if self._recovery_complete_event:
			try:
				start_time = asyncio.get_event_loop().time()
				await asyncio.wait_for(self._recovery_complete_event.wait(), timeout=timeout)
				elapsed = asyncio.get_event_loop().time() - start_time

				# Verify recovery succeeded - simple existence check
				focus_id = self.browser_session.agent_focus_target_id
				if focus_id and self._get_session_for_target(focus_id):
					self.logger.info(
						f'[SessionManager] ✅ Agent focus recovered to {self.browser_session.agent_focus_target_id[:8]}... '
						f'after {elapsed * 1000:.0f}ms'
					)
					return True
				else:
					self.logger.error(
						f'[SessionManager] ❌ Recovery completed but focus still invalid after {elapsed * 1000:.0f}ms'
					)
					return False

			except TimeoutError:
				self.logger.error(
					f'[SessionManager] ❌ Recovery timed out after {timeout}s '
					f'(was: {stale_target_id[:8] if stale_target_id else "None"}..., '
					f'now: {self.browser_session.agent_focus_target_id[:8] if self.browser_session.agent_focus_target_id else "None"})'
				)
				return False
		else:
			self.logger.error('[SessionManager] ❌ Recovery event not initialized')
			return False

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
		if waiting_for_debugger and target_type in ('page', 'tab') and self.url_policy_active:
			self._new_page_targets.add(target_id)

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
				await self._enable_page_monitoring(cdp_session)
			else:
				await self._enable_fetch_for_session(cdp_session, target_type=target_type)
		except Exception as error:
			policy_gate_missing = (
				self.url_policy_active and target_type in ('page', 'tab') and target_id not in self._fetch_sessions
			)
			if policy_gate_missing:
				self._policy_setup_failures[target_id] = f'{type(error).__name__}: {error}'
				await self._remediate_blocked_navigation(target_id, target_info.get('url', ''), setup_failed=True)
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
		if not self.url_policy_active or target_type not in ('page', 'tab') or not updated_url:
			return
		if self._is_url_allowed(updated_url):
			if not is_new_tab_page(updated_url):
				self._new_page_targets.discard(target_id)
			self._blocked_navigation_urls.pop(target_id, None)
			return
		await self._remediate_blocked_navigation(target_id, updated_url)

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
			fetch_owner_detached = self._fetch_sessions.get(target_id) == session_id
			if fetch_owner_detached:
				self._fetch_sessions.pop(target_id, None)

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
					self._lifecycle_events.pop(target_id, None)
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
			self._main_frame_ids.pop(session_id, None)
			if target_fully_removed:
				self._new_page_targets.discard(target_id)
				self._blocked_navigation_urls.pop(target_id, None)
				self._policy_setup_failures.pop(target_id, None)
				self._fetch_sessions.pop(target_id, None)

		# Keep one Fetch owner when Chrome detaches one of several flattened
		# sessions for a still-live target.
		if fetch_owner_detached and not target_fully_removed and not self.browser_session._intentional_stop:
			replacement_session = self._get_session_for_target(target_id)
			try:
				if replacement_session is None:
					raise RuntimeError('no replacement CDP session is available')
				await self._enable_fetch_for_session(
					replacement_session,
					target_type=target_type or 'unknown',
				)
			except Exception as error:
				if self.url_policy_active and target_type in ('page', 'tab'):
					self._policy_setup_failures[target_id] = f'{type(error).__name__}: {error}'
					self._blocked_navigation_urls.pop(target_id, None)
					target = self._targets.get(target_id)
					await self._remediate_blocked_navigation(
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
			if not self._recovery_in_progress:
				self._recovery_task = create_task_with_error_handling(
					self._recover_agent_focus(target_id),
					name='recover_agent_focus',
					logger_instance=self.logger,
					suppress_exceptions=False,
				)

	async def _recover_agent_focus(self, crashed_target_id: TargetID) -> None:
		"""Auto-recover agent_focus when the focused target crashes/detaches.

		Uses recovery lock to prevent concurrent recovery attempts from creating multiple emergency tabs.
		Coordinates with ensure_valid_focus() via events for efficient waiting.

		Args:
			crashed_target_id: The target ID that was lost
		"""
		try:
			# Prevent concurrent recovery attempts
			async with self._recovery_lock:
				# Set recovery state INSIDE lock to prevent race conditions
				if self._recovery_in_progress:
					self.logger.debug('[SessionManager] Recovery already in progress, waiting for it to complete')
					# Wait for ongoing recovery instead of starting a new one
					if self._recovery_complete_event:
						try:
							await asyncio.wait_for(self._recovery_complete_event.wait(), timeout=5.0)
						except TimeoutError:
							self.logger.error('[SessionManager] Timed out waiting for ongoing recovery')
					return

				# Set recovery state
				self._recovery_in_progress = True
				self._recovery_complete_event = asyncio.Event()

				if self.browser_session._cdp_client_root is None:
					self.logger.debug('[SessionManager] Skipping focus recovery - browser shutting down (no CDP client)')
					return

				# Check if another recovery already fixed agent_focus
				if self.browser_session.agent_focus_target_id and self.browser_session.agent_focus_target_id != crashed_target_id:
					self.logger.debug(
						f'[SessionManager] Agent focus already recovered by concurrent operation '
						f'(now: {self.browser_session.agent_focus_target_id[:8]}...), skipping recovery'
					)
					return

				# Note: agent_focus_target_id may already be None (cleared in _handle_target_detached)
				current_focus_desc = (
					f'{self.browser_session.agent_focus_target_id[:8]}...'
					if self.browser_session.agent_focus_target_id
					else 'None (already cleared)'
				)

				self.logger.warning(
					f'[SessionManager] Agent focus target {crashed_target_id[:8]}... detached! '
					f'Current focus: {current_focus_desc}. Auto-recovering by switching to another target...'
				)

			# Perform recovery (outside lock to allow concurrent operations)
			# Try to find another valid page target
			page_targets = self.get_all_page_targets()

			new_target_id = None
			is_existing_tab = False

			if page_targets:
				# Switch to most recent page that's not the crashed one
				new_target_id = page_targets[-1].target_id
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

			# Wait for CDP attach event to create session
			# Note: This polling is necessary - waiting for external Chrome CDP event
			# _handle_target_attached will add session to pool when Chrome fires attachedToTarget
			new_session = None
			for attempt in range(20):  # Wait up to 2 seconds
				await asyncio.sleep(0.1)
				new_session = self._get_session_for_target(new_target_id)
				if new_session:
					break

			if new_session:
				self.browser_session.agent_focus_target_id = new_target_id
				self.logger.info(f'[SessionManager] ✅ Agent focus recovered: {new_target_id[:8]}...')

				# Visually activate the tab in browser (only for existing tabs)
				if is_existing_tab:
					try:
						assert self.browser_session._cdp_client_root is not None
						await self.browser_session._cdp_client_root.send.Target.activateTarget(params={'targetId': new_target_id})
						self.logger.debug(f'[SessionManager] Activated tab {new_target_id[:8]}... in browser UI')
					except Exception as e:
						self.logger.debug(f'[SessionManager] Failed to activate tab visually: {e}')

				# Get target to access url (from owned data)
				target = self.get_target(new_target_id)
				target_url = target.url if target else 'about:blank'

				# Dispatch focus changed event
				from browser_use.browser.events import AgentFocusChangedEvent

				self.browser_session.event_bus.dispatch(AgentFocusChangedEvent(target_id=new_target_id, url=target_url))
				return

			# Recovery failed - create emergency fallback tab
			self.logger.error(
				f'[SessionManager] ❌ Failed to get session for {new_target_id[:8]}... after 2s, creating emergency fallback tab'
			)

			fallback_target_id = await self.browser_session._cdp_create_new_page('about:blank')
			self.logger.warning(f'[SessionManager] Created emergency fallback tab {fallback_target_id[:8]}...')

			# Try one more time with fallback
			# Note: This polling is necessary - waiting for external Chrome CDP event
			for _ in range(20):
				await asyncio.sleep(0.1)
				fallback_session = self._get_session_for_target(fallback_target_id)
				if fallback_session:
					self.browser_session.agent_focus_target_id = fallback_target_id
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
		finally:
			# Always signal completion and reset recovery state
			# This allows all waiting operations to proceed (success or failure)
			if self._recovery_complete_event:
				self._recovery_complete_event.set()
			self._recovery_in_progress = False
			self._recovery_task = None
			self.logger.debug('[SessionManager] Recovery state reset')

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
					if self._target_monitoring_ready(tid, target_type):
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
				if self._target_monitoring_ready(tid, target_type):
					ready_count += 1
			self.logger.warning(
				f'[SessionManager] Initialization timeout after 2.0s: {ready_count}/{len(target_ids_to_wait_for)} sessions ready'
			)
			if self.url_policy_active:
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
					if not self._target_monitoring_ready(target_id, target_info['type']):
						self._policy_setup_failures.setdefault(
							target_id,
							'initialization timed out before Fetch was installed',
						)
		finally:
			check_task.cancel()
			try:
				await check_task
			except asyncio.CancelledError:
				pass

		if self.url_policy_active and self._policy_setup_failures:
			failures = ', '.join(f'{target_id}: {error}' for target_id, error in self._policy_setup_failures.items())
			raise RuntimeError(f'URL policy interception could not be installed: {failures}')

	def _target_monitoring_ready(self, target_id: TargetID, target_type: str) -> bool:
		"""Return whether any usable session owns the required target monitoring."""
		sessions = [
			self._sessions[session_id]
			for session_id in self._target_sessions.get(target_id, set())
			if session_id in self._sessions
		]
		if not sessions:
			return False
		if target_type not in ('page', 'tab'):
			return True
		if not any(session._lifecycle_events is not None for session in sessions):
			return False
		if not self.url_policy_active:
			return True
		fetch_session_id = self._fetch_sessions.get(target_id)
		return fetch_session_id in self._main_frame_ids

	async def _enable_page_monitoring(self, cdp_session: 'CDPSession') -> None:
		"""Enable lifecycle events and network monitoring for a page target.

		This is called once per page when it's created, avoiding handler accumulation.
		Registers a SINGLE lifecycle handler per session that stores events for navigations to consume.

		Args:
			cdp_session: The CDP session to enable monitoring on
		"""
		try:
			# Enable Page domain first (required for lifecycle events)
			await cdp_session.cdp_client.send.Page.enable(session_id=cdp_session.session_id)

			# Enable lifecycle events (load, DOMContentLoaded, networkIdle, etc.)
			await cdp_session.cdp_client.send.Page.setLifecycleEventsEnabled(
				params={'enabled': True}, session_id=cdp_session.session_id
			)

			# Enable network monitoring for networkIdle detection
			await cdp_session.cdp_client.send.Network.enable(session_id=cdp_session.session_id)

			if self.url_policy_active:
				frame_tree = await cdp_session.cdp_client.send.Page.getFrameTree(session_id=cdp_session.session_id)
				main_frame_id = frame_tree.get('frameTree', {}).get('frame', {}).get('id')
				if not main_frame_id:
					raise RuntimeError(f'Could not identify the top frame for target {cdp_session.target_id}')
				self._main_frame_ids[cdp_session.session_id] = main_frame_id

			await self._enable_fetch_for_session(cdp_session, target_type='page')

			# Event storage and the Page.lifecycleEvent handler live in SessionManager
			# (one global handler registered in start_monitoring, routed by session_id):
			# cdp-use's registry is single-slot per method, so a per-session registration
			# here would replace the previous tab's handler and freeze its event buffer.
			# Expose the shared per-target buffer on the session for readiness checks.
			cdp_session._lifecycle_events = self.get_lifecycle_events(cdp_session.target_id)

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
				if self.url_policy_active:
					raise

	async def _enable_fetch_for_session(self, cdp_session: 'CDPSession', *, target_type: str) -> None:
		"""Enable one target-scoped Fetch configuration for policy and proxy auth."""
		proxy = self.browser_session.browser_profile.proxy
		has_proxy_credentials = bool(proxy and proxy.username and proxy.password)
		needs_policy = self.url_policy_active and target_type in ('page', 'tab')
		if not has_proxy_credentials and not needs_policy:
			return

		params: EnableParameters = {'handleAuthRequests': has_proxy_credentials}
		if needs_policy:
			policy_patterns: list[RequestPattern] = [{'urlPattern': '*', 'resourceType': 'Document', 'requestStage': 'Request'}]
			params['patterns'] = policy_patterns
		elif has_proxy_credentials:
			# Preserve the prior authenticated-proxy behavior. The centralized
			# requestPaused handler immediately continues these requests.
			params['patterns'] = [{'urlPattern': '*'}]

		# Chrome permits multiple flattened CDP sessions for the same target. Fetch
		# interception stacks across them, so enabling every session pauses one
		# logical request repeatedly. Give each target exactly one Fetch owner.
		async with self._fetch_setup_lock:
			current_session_id = self._fetch_sessions.get(cdp_session.target_id)
			if current_session_id is not None and current_session_id in self._sessions:
				return
			await cdp_session.cdp_client.send.Fetch.enable(params=params, session_id=cdp_session.session_id)
			self._fetch_sessions[cdp_session.target_id] = cdp_session.session_id
			self._policy_setup_failures.pop(cdp_session.target_id, None)
		self.logger.debug(
			f'[SessionManager] Fetch enabled for {target_type} session {cdp_session.session_id[:8]}... '
			f'(policy={needs_policy}, proxy_auth={has_proxy_credentials})'
		)
