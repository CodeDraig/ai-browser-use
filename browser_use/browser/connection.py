from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

import httpx
from cdp_use.cdp.fetch import AuthRequiredEvent
from cdp_use.cdp.target import SessionID

from browser_use.browser._cdp_timeout import TimeoutWrappedCDPClient
from browser_use.browser.events import (
	AgentFocusChangedEvent,
	BrowserErrorEvent,
	BrowserReconnectedEvent,
	BrowserReconnectingEvent,
	TabCreatedEvent,
)
from browser_use.runtime import create_task_with_error_handling

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession


class BrowserConnection:
	"""Own CDP connection establishment, proxy auth, and reconnection state transitions."""

	def __init__(self, session: BrowserSession) -> None:
		self.session = session

	async def connect(self, cdp_url: str | None = None) -> BrowserSession:
		"""Connect to a remote chromium-based browser via CDP using cdp-use.

		This MUST succeed or the browser is unusable. Fails hard on any error.
		"""

		self.session.browser_profile.cdp_url = cdp_url or self.session.cdp_url
		if not self.session.cdp_url:
			raise RuntimeError('Cannot setup CDP connection without CDP URL')

		# Prevent duplicate connections - clean up existing connection first
		if self.session._cdp_client_root is not None:
			self.session.logger.warning(
				'⚠️ connect() called but CDP client already exists! Cleaning up old connection before creating new one.'
			)
			old_cdp_client = self.session._cdp_client_root
			was_intentional_stop = self.session._intentional_stop
			self.session._intentional_stop = True
			self.session._cdp_client_root = None
			self.session.agent_focus_target_id = None
			try:
				await old_cdp_client.stop()
			except Exception as e:
				self.session.logger.debug(f'Error stopping old CDP client: {e}')
			finally:
				# Every pooled session belongs to the retired root client. Clear the
				# connection generation before monitoring the replacement client.
				await self.session.session_manager.clear()
				self.session._intentional_stop = was_intentional_stop

		if not self.session.cdp_url.startswith('ws'):
			# If it's an HTTP URL, fetch the WebSocket URL from /json/version endpoint
			parsed_url = urlparse(self.session.cdp_url)
			path = parsed_url.path.rstrip('/')

			if not path.endswith('/json/version'):
				path = path + '/json/version'

			url = urlunparse(
				(parsed_url.scheme, parsed_url.netloc, path, parsed_url.params, parsed_url.query, parsed_url.fragment)
			)

			# Run a tiny HTTP client to query for the WebSocket URL from the /json/version endpoint
			# Default httpx timeout is 5s which can race the global wait_for(connect(), 15s).
			# Use 30s as a safety net for direct connect() callers; the wait_for is the real deadline.
			# For localhost/127.0.0.1, disable trust_env to prevent proxy env vars (HTTP_PROXY, HTTPS_PROXY)
			# from routing local requests through a proxy, which causes 502 errors on Windows.
			# Remote CDP URLs should still respect proxy settings.
			is_localhost = parsed_url.hostname in ('localhost', '127.0.0.1', '::1')
			async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), trust_env=not is_localhost) as client:
				headers = dict(self.session.browser_profile.headers or {})
				from browser_use.version import get_browser_use_version

				headers.setdefault('User-Agent', f'browser-use/{get_browser_use_version()}')
				version_info = await client.get(url, headers=headers)
				self.session.logger.debug(f'Raw version info: {str(version_info)}')
				self.session.browser_profile.cdp_url = version_info.json()['webSocketDebuggerUrl']

		assert self.session.cdp_url is not None, 'CDP URL is None.'

		browser_location = 'local browser' if self.session.is_local else 'remote browser'
		self.session.logger.debug(
			f'🌎 Connecting to existing chromium-based browser via CDP: {self.session.cdp_url} -> ({browser_location})'
		)

		try:
			# Create and store the CDP client for direct CDP communication
			headers = dict(getattr(self.session.browser_profile, 'headers', None) or {})
			if not self.session.is_local:
				from browser_use.version import get_browser_use_version

				headers.setdefault('User-Agent', f'browser-use/{get_browser_use_version()}')
			self.session._cdp_client_root = TimeoutWrappedCDPClient(
				self.session.cdp_url,
				additional_headers=headers or None,
				max_ws_frame_size=200 * 1024 * 1024,  # Use 200MB limit to handle pages with very large DOMs
			)
			assert self.session._cdp_client_root is not None
			await self.session._cdp_client_root.start()

			# Initialize event-driven session manager FIRST (before enabling autoAttach)
			# SessionManager will:
			# 1. Register attach/detach event handlers
			# 2. Discover and attach to all existing targets
			# 3. Initialize sessions and enable lifecycle monitoring
			# 4. Enable autoAttach for future targets
			await self.session.session_manager.start_monitoring()
			self.session.logger.debug('Event-driven session manager started')

			# Enable auto-attach so Chrome automatically notifies us when NEW targets attach/detach
			# This is the foundation of event-driven session management
			await self.session._cdp_client_root.send.Target.setAutoAttach(
				params={
					'autoAttach': True,
					'waitForDebuggerOnStart': self.session.session_manager.navigation_policy.active,
					'flatten': True,
				}
			)
			self.session.logger.debug('CDP client connected with auto-attach enabled')

			# Get browser targets from SessionManager (source of truth)
			# SessionManager has already discovered all targets via start_monitoring()
			page_targets_from_manager = self.session.session_manager.get_all_page_targets()

			# Check for chrome://newtab pages and redirect them to about:blank (in parallel)
			from browser_use.security import is_new_tab_page

			async def _redirect_newtab(target):
				target_url = target.url
				target_id = target.target_id
				self.session.logger.debug(f'🔄 Redirecting {target_url} to about:blank for target {target_id}')
				try:
					session = await self.session.get_or_create_cdp_session(target_id, focus=False)
					await session.cdp_client.send.Page.navigate(params={'url': 'about:blank'}, session_id=session.session_id)
					target.url = 'about:blank'
				except Exception as e:
					self.session.logger.warning(f'Failed to redirect {target_url}: {e}')

			redirect_tasks = [
				_redirect_newtab(target)
				for target in page_targets_from_manager
				if is_new_tab_page(target.url) and target.url != 'about:blank'
			]
			if redirect_tasks:
				await asyncio.gather(*redirect_tasks, return_exceptions=True)

			# Ensure we have at least one page
			if not page_targets_from_manager:
				new_target = await self.session._cdp_client_root.send.Target.createTarget(params={'url': 'about:blank'})
				target_id = new_target['targetId']
				self.session.logger.debug(f'📄 Created new blank page: {target_id}')
			else:
				target_id = page_targets_from_manager[0].target_id
				self.session.logger.debug(f'📄 Using existing page: {target_id}')

			# Set up initial focus using the public API
			# Note: get_or_create_cdp_session() will wait for attach event and set focus
			try:
				await self.session.get_or_create_cdp_session(target_id, focus=True)
				# agent_focus_target_id is now set by get_or_create_cdp_session
				self.session.logger.debug(f'📄 Agent focus set to {target_id[:8]}...')
			except ValueError as e:
				raise RuntimeError(f'Failed to get session for initial target {target_id}: {e}') from e

			# Note: Lifecycle monitoring is enabled automatically in SessionManager._handle_target_attached()
			# when targets attach, so no manual enablement needed!

			# Enable proxy authentication handling if configured
			await self._setup_proxy_auth()

			# Attach WS drop detection callback for auto-reconnection
			self.session._intentional_stop = False
			self._attach_ws_drop_callback()

			# Verify the target is working
			if self.session.agent_focus_target_id:
				target = self.session.session_manager.get_target(self.session.agent_focus_target_id)
				if target.title == 'Unknown title':
					self.session.logger.warning('Target created but title is unknown (may be normal for about:blank)')

			# Dispatch TabCreatedEvent for all initial tabs (so watchdogs can initialize)
			for idx, target in enumerate(page_targets_from_manager):
				target_url = target.url
				self.session.logger.debug(f'Dispatching TabCreatedEvent for initial tab {idx}: {target_url}')
				self.session.event_bus.dispatch(TabCreatedEvent(url=target_url, target_id=target.target_id))

			# Dispatch initial focus event
			if page_targets_from_manager:
				initial_url = page_targets_from_manager[0].url
				self.session.event_bus.dispatch(
					AgentFocusChangedEvent(target_id=page_targets_from_manager[0].target_id, url=initial_url)
				)
				self.session.logger.debug(f'Initial agent focus set to tab 0: {initial_url}')

		except Exception as e:
			# Fatal error - browser is not usable without CDP connection
			self.session.logger.error(f'❌ FATAL: Failed to setup CDP connection: {e}')
			self.session.logger.error('❌ Browser cannot continue without CDP connection')

			# Clear SessionManager state
			if self.session.session_manager:
				try:
					await self.session.session_manager.clear()
					self.session.logger.debug('Cleared SessionManager state after initialization failure')
				except Exception as cleanup_error:
					self.session.logger.debug(f'Error clearing SessionManager: {cleanup_error}')

			# Close CDP client WebSocket and unregister handlers
			if self.session._cdp_client_root:
				try:
					await self.session._cdp_client_root.stop()  # Close WebSocket and unregister handlers
					self.session.logger.debug('Closed CDP client WebSocket after initialization failure')
				except Exception as cleanup_error:
					self.session.logger.debug(f'Error closing CDP client: {cleanup_error}')

				await self.session.session_manager.clear()
			self.session._cdp_client_root = None
			self.session.agent_focus_target_id = None
			# Re-raise as a fatal error
			raise RuntimeError(f'Failed to establish CDP connection to browser: {e}') from e

		return self.session

	async def _setup_proxy_auth(self) -> None:
		"""Enable CDP Fetch auth handling for authenticated proxy, if credentials provided.

		Handles HTTP proxy authentication challenges (Basic/Proxy) by providing
		configured credentials from BrowserProfile.
		"""

		assert self.session._cdp_client_root

		try:
			proxy_cfg = self.session.browser_profile.proxy
			username = proxy_cfg.username if proxy_cfg else None
			password = proxy_cfg.password if proxy_cfg else None
			if not username or not password:
				self.session.logger.debug('Proxy credentials not provided; skipping proxy auth setup')
				return

			def _on_auth_required(event: AuthRequiredEvent, session_id: SessionID | None = None):
				# event keys may be snake_case or camelCase depending on generator; handle both
				request_id = event.get('requestId') or event.get('request_id')
				if not request_id:
					return

				challenge = event.get('authChallenge') or event.get('auth_challenge') or {}
				source = (challenge.get('source') or '').lower()
				# Only respond to proxy challenges
				if source == 'proxy' and request_id:

					async def _respond():
						assert self.session._cdp_client_root
						try:
							await self.session._cdp_client_root.send.Fetch.continueWithAuth(
								params={
									'requestId': request_id,
									'authChallengeResponse': {
										'response': 'ProvideCredentials',
										'username': username,
										'password': password,
									},
								},
								session_id=session_id,
							)
						except Exception as e:
							self.session.logger.debug(f'Proxy auth respond failed: {type(e).__name__}: {e}')

					# schedule
					create_task_with_error_handling(
						_respond(), name='auth_respond', logger_instance=self.session.logger, suppress_exceptions=True
					)
				else:
					# Default behaviour for non-proxy challenges: let browser handle
					async def _default():
						assert self.session._cdp_client_root
						try:
							await self.session._cdp_client_root.send.Fetch.continueWithAuth(
								params={'requestId': request_id, 'authChallengeResponse': {'response': 'Default'}},
								session_id=session_id,
							)
						except Exception as e:
							self.session.logger.debug(f'Default auth respond failed: {type(e).__name__}: {e}')

					if request_id:
						create_task_with_error_handling(
							_default(), name='auth_default', logger_instance=self.session.logger, suppress_exceptions=True
						)

			# Register event handler on root client
			try:
				self.session._cdp_client_root.register.Fetch.authRequired(_on_auth_required)
				self.session.logger.debug('Registered Fetch.authRequired handlers')
			except Exception as e:
				self.session.logger.debug(f'Failed to register authRequired handlers: {type(e).__name__}: {e}')

		except Exception as e:
			self.session.logger.debug(f'Skipping proxy auth setup: {type(e).__name__}: {e}')

	async def reconnect(self) -> None:
		"""Re-establish the CDP WebSocket connection to an already-running browser.

		This is a lightweight reconnection that:
		1. Stops the old CDPClient (WS already dead, just clean state)
		2. Clears SessionManager (all CDP sessions are invalid post-disconnect)
		3. Creates a new CDPClient with the same cdp_url
		4. Re-initializes SessionManager and re-enables autoAttach
		5. Re-discovers page targets and restores agent focus
		6. Re-enables proxy auth if configured
		"""
		assert self.session.cdp_url, 'Cannot reconnect without a CDP URL'

		old_focus_target_id = self.session.agent_focus_target_id

		# 1. Stop old CDPClient (WS is already dead, this just cleans internal state)
		if self.session._cdp_client_root:
			try:
				await self.session._cdp_client_root.stop()
			except Exception as e:
				self.session.logger.debug(f'Error stopping old CDP client during reconnect: {e}')
			self.session._cdp_client_root = None

			# 2. Clear SessionManager (all sessions are stale)
			try:
				await self.session.session_manager.clear()
			except Exception as e:
				self.session.logger.debug(f'Error clearing SessionManager during reconnect: {e}')

		self.session.agent_focus_target_id = None

		# 3. Create new CDPClient with the same cdp_url
		headers = dict(getattr(self.session.browser_profile, 'headers', None) or {})
		if not self.session.is_local:
			from browser_use.version import get_browser_use_version

			headers.setdefault('User-Agent', f'browser-use/{get_browser_use_version()}')
		self.session._cdp_client_root = TimeoutWrappedCDPClient(
			self.session.cdp_url,
			additional_headers=headers or None,
			max_ws_frame_size=200 * 1024 * 1024,
		)
		await self.session._cdp_client_root.start()

		# 4. Re-initialize SessionManager
		await self.session.session_manager.start_monitoring()

		# 5. Re-enable autoAttach
		await self.session._cdp_client_root.send.Target.setAutoAttach(
			params={
				'autoAttach': True,
				'waitForDebuggerOnStart': self.session.session_manager.navigation_policy.active,
				'flatten': True,
			}
		)

		# 6. Re-discover page targets and restore focus
		page_targets = self.session.session_manager.get_all_page_targets()

		# Prefer the old focus target if it still exists
		restored = False
		if old_focus_target_id:
			for target in page_targets:
				if target.target_id == old_focus_target_id:
					await self.session.get_or_create_cdp_session(old_focus_target_id, focus=True)
					restored = True
					self.session.logger.debug(f'🔄 Restored agent focus to previous target {old_focus_target_id[:8]}...')
					break

		if not restored:
			if page_targets:
				fallback_id = page_targets[0].target_id
				await self.session.get_or_create_cdp_session(fallback_id, focus=True)
				self.session.logger.debug(f'🔄 Agent focus set to fallback target {fallback_id[:8]}...')
			else:
				# No pages exist — create one
				new_target = await self.session._cdp_client_root.send.Target.createTarget(params={'url': 'about:blank'})
				target_id = new_target['targetId']
				await self.session.get_or_create_cdp_session(target_id, focus=True)
				self.session.logger.debug(f'🔄 Created new blank page during reconnect: {target_id[:8]}...')

		# 7. Re-enable proxy auth if configured
		await self._setup_proxy_auth()

		# 8. Attach the WS drop detection callback to the new client
		self._attach_ws_drop_callback()

	async def _auto_reconnect(self, max_attempts: int = 3) -> None:
		"""Attempt to reconnect with exponential backoff.

		Dispatches BrowserReconnectingEvent before each attempt and
		BrowserReconnectedEvent on success.
		"""
		async with self.session._reconnect_lock:
			if self.session._reconnecting:
				return  # already in progress from another caller
			self.session._reconnecting = True
			self.session._reconnect_event.clear()

		start_time = time.time()
		delays = [1.0, 2.0, 4.0]

		try:
			for attempt in range(1, max_attempts + 1):
				self.session.event_bus.dispatch(
					BrowserReconnectingEvent(
						cdp_url=self.session.cdp_url or '',
						attempt=attempt,
						max_attempts=max_attempts,
					)
				)
				self.session.logger.warning(f'🔄 WebSocket reconnection attempt {attempt}/{max_attempts}...')

				try:
					await asyncio.wait_for(self.reconnect(), timeout=15.0)
					# Success
					downtime = time.time() - start_time
					self.session.event_bus.dispatch(
						BrowserReconnectedEvent(
							cdp_url=self.session.cdp_url or '',
							attempt=attempt,
							downtime_seconds=downtime,
						)
					)
					self.session.logger.info(f'🔄 WebSocket reconnected after {downtime:.1f}s (attempt {attempt})')
					return
				except Exception as e:
					self.session.logger.warning(f'🔄 Reconnection attempt {attempt} failed: {type(e).__name__}: {e}')
					if attempt < max_attempts:
						delay = delays[attempt - 1] if attempt - 1 < len(delays) else delays[-1]
						await asyncio.sleep(delay)

			# All attempts exhausted
			self.session.logger.error(f'🔄 All {max_attempts} reconnection attempts failed')
			self.session.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='ReconnectionFailed',
					message=f'Failed to reconnect after {max_attempts} attempts ({time.time() - start_time:.1f}s)',
					details={'cdp_url': self.session.cdp_url or '', 'max_attempts': max_attempts},
				)
			)
		finally:
			self.session._reconnecting = False
			self.session._reconnect_event.set()  # wake up all waiters regardless of outcome

	def _attach_ws_drop_callback(self) -> None:
		"""Attach a done callback to the CDPClient's message handler task to detect WS drops."""
		if not self.session._cdp_client_root or not hasattr(self.session._cdp_client_root, '_message_handler_task'):
			return

		cdp_client = self.session._cdp_client_root
		task = cdp_client._message_handler_task
		if task is None or task.done():
			return

		def _on_message_handler_done(fut: asyncio.Future) -> None:
			# Ignore callbacks from a connection generation that has already been replaced.
			if self.session._cdp_client_root is not cdp_client:
				return

			# Guard: skip if intentionally stopped, already reconnecting, or no cdp_url
			if self.session._intentional_stop or self.session._reconnecting or not self.session.cdp_url:
				return

			# The message handler task exiting means the WS connection dropped
			exc = fut.exception() if not fut.cancelled() else None
			self.session.logger.warning(
				f'🔌 CDP WebSocket message handler exited unexpectedly'
				f'{f": {type(exc).__name__}: {exc}" if exc else " (connection closed)"}'
			)

			# Fire auto-reconnect as an asyncio task
			try:
				loop = asyncio.get_running_loop()
				self.session._reconnect_task = loop.create_task(self._auto_reconnect())
			except RuntimeError:
				# No running event loop — can't reconnect
				self.session.logger.error('🔌 No event loop available for auto-reconnect')

		task.add_done_callback(_on_message_handler_done)
