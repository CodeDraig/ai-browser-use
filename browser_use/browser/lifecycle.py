from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from cdp_use import CDPClient

from browser_use.browser.cloud.cloud import CloudBrowserAuthError, CloudBrowserError
from browser_use.browser.cloud.views import CreateBrowserRequest
from browser_use.browser.event_bus import ResilientEventBus as _ResilientEventBus
from browser_use.browser.events import (
	AgentFocusChangedEvent,
	BrowserConnectedEvent,
	BrowserErrorEvent,
	BrowserLaunchEvent,
	BrowserLaunchResult,
	BrowserStartEvent,
	BrowserStopEvent,
	BrowserStoppedEvent,
	CloseTabEvent,
	FileDownloadedEvent,
	NavigateToUrlEvent,
	SwitchTabEvent,
	TabClosedEvent,
	TabCreatedEvent,
)

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession


class BrowserLifecycle:
	"""Own browser start/stop events, artifact finalization, and bus renewal ordering."""

	def __init__(self, session: BrowserSession) -> None:
		self.session = session

	def _attach_core_event_handlers(self) -> None:
		"""Attach BrowserSession lifecycle handlers to the current event bus."""
		from browser_use.browser.watchdog_base import BaseWatchdog

		# Check if handlers are already registered to prevent duplicates
		start_handlers = self.session.event_bus.handlers.get('BrowserStartEvent', [])
		start_handler_names = [getattr(h, '__name__', str(h)) for h in start_handlers]

		if any('on_BrowserStartEvent' in name for name in start_handler_names):
			raise RuntimeError(
				'[BrowserSession] Duplicate handler registration attempted! '
				'on_BrowserStartEvent is already registered. '
				'This likely means BrowserSession was initialized multiple times with the same EventBus.'
			)

		BaseWatchdog.attach_handler_to_session(self.session, BrowserStartEvent, self.on_BrowserStartEvent)
		BaseWatchdog.attach_handler_to_session(self.session, BrowserStopEvent, self.on_BrowserStopEvent)
		BaseWatchdog.attach_handler_to_session(self.session, NavigateToUrlEvent, self.session.navigation.on_NavigateToUrlEvent)
		BaseWatchdog.attach_handler_to_session(self.session, SwitchTabEvent, self.session.on_SwitchTabEvent)
		BaseWatchdog.attach_handler_to_session(self.session, TabCreatedEvent, self.session.on_TabCreatedEvent)
		BaseWatchdog.attach_handler_to_session(self.session, TabClosedEvent, self.session.on_TabClosedEvent)
		BaseWatchdog.attach_handler_to_session(self.session, AgentFocusChangedEvent, self.session.on_AgentFocusChangedEvent)
		BaseWatchdog.attach_handler_to_session(self.session, FileDownloadedEvent, self.session.on_FileDownloadedEvent)
		BaseWatchdog.attach_handler_to_session(self.session, CloseTabEvent, self.session.on_CloseTabEvent)

	def _renew_event_bus(self) -> None:
		"""Replace a stopped event bus and attach this session's lifecycle handlers."""
		self.session.event_bus = _ResilientEventBus()
		self._attach_core_event_handlers()
		self.session.watchdogs.reattach_preserved_local_browser()

	async def start(self) -> None:
		"""Start the browser session."""
		start_event = self.session.event_bus.dispatch(BrowserStartEvent())
		await start_event
		# Ensure any exceptions from the event handler are propagated
		await start_event.event_result(raise_if_any=True, raise_if_none=False)

	async def _dispatch_stop_event(self, *, force: bool) -> None:
		"""Run all stop handlers and propagate cleanup failures before reset."""
		stop_event = self.session.event_bus.dispatch(BrowserStopEvent(force=force))
		await stop_event
		await stop_event.event_result(raise_if_any=True, raise_if_none=False)

	async def kill(self) -> None:
		"""Kill the browser session and reset all state."""
		previous_intentional_stop = self.session._intentional_stop
		self.session._intentional_stop = True
		self.session.logger.debug('🛑 kill() called - stopping browser with force=True and resetting state')

		try:
			await self._finalize_session_artifacts()
			await self._dispatch_stop_event(force=True)
		except Exception:
			# The caller must be able to retry kill() with the same process owner,
			# CDP endpoint, watchdog registry, and event bus.
			self.session._intentional_stop = previous_intentional_stop
			raise
		# Stop the event bus
		await self.session.event_bus.stop(clear=True, timeout=5)
		# Reset all state
		await self.session.reset()
		# Create a fresh event bus with the session lifecycle handlers attached
		self._renew_event_bus()

	async def stop(self) -> None:
		"""Disconnect while preserving a BrowserSession-owned local browser.

		Cloud and externally managed CDP sessions retain their existing stop
		behavior. URL-policy enforcement is inactive until this session reconnects.
		"""
		previous_intentional_stop = self.session._intentional_stop
		self.session._intentional_stop = True
		self.session.logger.debug('⏸️  stop() called - stopping browser gracefully (force=False) and resetting state')

		try:
			await self._finalize_session_artifacts()
			await self._dispatch_stop_event(force=False)
		except Exception:
			self.session._intentional_stop = previous_intentional_stop
			raise

		# A non-forced stop handler cleans an already-dead owned process. Decide
		# preservation only after every handler has completed.
		local_browser = self.session.watchdogs.local_browser
		preserve_owned_local_browser = bool(local_browser is not None and local_browser.owns_browser_process)

		# Stop the event bus
		await self.session.event_bus.stop(clear=True, timeout=5)
		# Reset all state
		await self.session._reset(preserve_owned_local_browser=preserve_owned_local_browser)
		# Create a fresh event bus with the session lifecycle handlers attached
		self._renew_event_bus()

	async def _finalize_session_artifacts(self) -> None:
		"""Finalize persisted session output before any handler can disconnect CDP."""
		from browser_use.browser.events import SaveStorageStateEvent

		save_event = self.session.event_bus.dispatch(SaveStorageStateEvent())
		await save_event

		recording = self.session.watchdogs.recording
		if recording is not None and recording.is_recording:
			try:
				await recording.stop_recording()
			except Exception as error:
				self.session.logger.warning(f'Failed to finalize browser recording before disconnect: {error}')

		har_recording = self.session.watchdogs.har_recording
		if har_recording is not None:
			await har_recording.finalize()

	async def on_BrowserStartEvent(self, event: BrowserStartEvent) -> dict[str, str]:
		"""Handle browser start request.

		Returns:
			Dict with 'cdp_url' key containing the CDP URL

		Note: This method is idempotent - calling start() multiple times is safe.
		- If already connected, it skips reconnection
		- If you need to reset state, call stop() or kill() first
		"""

		# A retained local process can die while stop() is disconnected. Clear its
		# stale endpoint and finish owned-resource cleanup before deciding whether
		# start() should reconnect or launch a fresh browser.
		retained_local_browser = self.session.watchdogs.local_browser
		if (
			self.session.is_local
			and self.session._cdp_client_root is None
			and retained_local_browser is not None
			and retained_local_browser._subprocess is not None
			and not retained_local_browser.owns_browser_process
		):
			await retained_local_browser._cleanup_owned_browser_resources()
			self.session.browser_profile.cdp_url = None

		# Initialize and attach all watchdogs FIRST so LocalBrowserWatchdog can handle BrowserLaunchEvent
		await self.session.watchdogs.attach()

		try:
			# If no CDP URL, launch local browser or cloud browser
			if not self.session.cdp_url:
				if self.session.browser_profile.use_cloud or self.session.browser_profile.cloud_browser_params is not None:
					# Use cloud browser service
					try:
						# Use cloud_browser_params if provided, otherwise create empty request
						cloud_params = self.session.browser_profile.cloud_browser_params or CreateBrowserRequest()
						cloud_browser_response = await self.session._cloud_browser_client.create_browser(cloud_params)
						self.session.browser_profile.cdp_url = cloud_browser_response.cdpUrl
						self.session.browser_profile.is_local = False
						self.session.logger.info('🌤️ Successfully connected to cloud browser service')
					except CloudBrowserAuthError:
						raise
					except CloudBrowserError as e:
						raise CloudBrowserError(f'Failed to create cloud browser: {e}')
				elif self.session.is_local:
					# Launch local browser using event-driven approach
					launch_event = self.session.event_bus.dispatch(BrowserLaunchEvent())
					await launch_event

					# Get the CDP URL from LocalBrowserWatchdog handler result
					launch_result: BrowserLaunchResult = cast(
						BrowserLaunchResult, await launch_event.event_result(raise_if_none=True, raise_if_any=True)
					)
					self.session.browser_profile.cdp_url = launch_result.cdp_url
				else:
					raise ValueError('Got BrowserSession(is_local=False) but no cdp_url was provided to connect to!')

			assert self.session.cdp_url and '://' in self.session.cdp_url

			# Use lock to prevent concurrent connection attempts (race condition protection)
			async with self.session._connection_lock:
				# Only connect if not already connected
				if self.session._cdp_client_root is None:
					# Setup browser via CDP (for both local and remote cases)
					# Global timeout prevents connect() from hanging indefinitely on
					# slow/broken WebSocket connections (common on Lambda → remote browser)
					try:
						await asyncio.wait_for(self.session.connect(cdp_url=self.session.cdp_url), timeout=15.0)
					except TimeoutError:
						# Timeout cancels connect() via CancelledError, which bypasses
						# connect()'s `except Exception` cleanup (CancelledError is BaseException).
						# Clean up the partially-initialized client so future start attempts
						# don't skip reconnection due to _cdp_client_root being non-None.
						cdp_client = cast(CDPClient | None, self.session._cdp_client_root)
						if cdp_client is not None:
							try:
								await cdp_client.stop()
							except Exception:
								pass
							self.session._cdp_client_root = None
							try:
								await self.session.session_manager.clear()
							except Exception:
								pass
						self.session.agent_focus_target_id = None
						raise RuntimeError(
							f'connect() timed out after 15s — CDP connection to {self.session.cdp_url} is too slow or unresponsive'
						)
					assert self.session.cdp_client is not None

					# Notify that browser is connected (single place)
					# Ensure BrowserConnected handlers (storage_state restore) complete before
					# start() returns so cookies/storage are applied before navigation.
					await self.session.event_bus.dispatch(BrowserConnectedEvent(cdp_url=self.session.cdp_url))

					if self.session.browser_profile.demo_mode:
						try:
							demo = self.session.demo_mode
							if demo:
								await demo.ensure_ready()
						except Exception as exc:
							self.session.logger.warning(f'[DemoMode] Failed to inject demo overlay: {exc}')
				else:
					self.session.logger.debug('Already connected to CDP, skipping reconnection')
					if self.session.browser_profile.demo_mode:
						try:
							demo = self.session.demo_mode
							if demo:
								await demo.ensure_ready()
						except Exception as exc:
							self.session.logger.warning(f'[DemoMode] Failed to inject demo overlay: {exc}')

			# Return the CDP URL for other components
			return {'cdp_url': self.session.cdp_url}

		except Exception as e:
			self.session.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='BrowserStartEventError',
					message=f'Failed to start browser: {type(e).__name__} {e}',
					details={'cdp_url': self.session.cdp_url, 'is_local': self.session.is_local},
				)
			)
			if self.session.is_local and not isinstance(e, (CloudBrowserAuthError, CloudBrowserError)):
				self.session.logger.warning(
					'Local browser failed to start. Cloud browsers require no local install and work out of the box.\n'
					'         Try: Browser(use_cloud=True)  |  Get an API key: https://cloud.browser-use.com?utm_source=oss&utm_medium=browser_launch_failure'
				)
			raise

	async def on_BrowserStopEvent(self, event: BrowserStopEvent) -> None:
		"""Handle browser stop request."""

		try:
			# Check if we should keep the browser alive
			if self.session.browser_profile.keep_alive and not event.force:
				self.session.event_bus.dispatch(BrowserStoppedEvent(reason='Kept alive due to keep_alive=True'))
				return

			# Clean up cloud browser session for both:
			# 1) native use_cloud sessions (current_session_id set by create_browser)
			# 2) reconnected cdp_url sessions (derive UUID from host)
			cloud_session_id = (
				self.session._cloud_browser_client.current_session_id or self.session._cloud_session_id_from_cdp_url()
			)
			if cloud_session_id:
				try:
					await self.session._cloud_browser_client.stop_browser(cloud_session_id)
					self.session.logger.info(f'🌤️ Cloud browser session cleaned up: {cloud_session_id}')
				except Exception as e:
					self.session.logger.debug(f'Failed to cleanup cloud browser session {cloud_session_id}: {e}')
				finally:
					# Always close the httpx client to free connection pool memory
					try:
						await self.session._cloud_browser_client.close()
					except Exception:
						pass

			# Public stop()/kill() reset only after every stop handler completes,
			# keeping artifact finalization and process cleanup ahead of CDP teardown.
			# LocalBrowserWatchdog listens for BrowserStopEvent and dispatches BrowserKillEvent
			stop_event = self.session.event_bus.dispatch(BrowserStoppedEvent(reason='Stopped by request'))
			await stop_event

		except Exception as e:
			self.session.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='BrowserStopEventError',
					message=f'Failed to stop browser: {type(e).__name__} {e}',
					details={'cdp_url': self.session.cdp_url, 'is_local': self.session.is_local},
				)
			)
