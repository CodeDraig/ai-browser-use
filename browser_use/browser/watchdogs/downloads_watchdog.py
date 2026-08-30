"""Downloads watchdog for monitoring and handling file downloads."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from bubus import BaseEvent
from pydantic import PrivateAttr

from browser_use.browser.events import (
	BrowserLaunchEvent,
	BrowserStateRequestEvent,
	BrowserStoppedEvent,
	DownloadProgressEvent,
	DownloadStartedEvent,
	FileDownloadedEvent,
	NavigationCompleteEvent,
	TabClosedEvent,
	TabCreatedEvent,
)
from browser_use.browser.watchdog_base import BaseWatchdog
from browser_use.browser.watchdogs.download_tracker import DownloadTracker
from browser_use.browser.watchdogs.network_downloads import NetworkDownloadMonitor
from browser_use.browser.watchdogs.pdf_download import PdfDownloadController

if TYPE_CHECKING:
	pass


class DownloadsWatchdog(BaseWatchdog):
	"""Monitors downloads and handles file download events."""

	# Events this watchdog listens to (for documentation)
	LISTENS_TO: ClassVar[list[type[BaseEvent[Any]]]] = [
		BrowserLaunchEvent,
		BrowserStateRequestEvent,
		BrowserStoppedEvent,
		TabCreatedEvent,
		TabClosedEvent,
		NavigationCompleteEvent,
	]

	# Events this watchdog emits
	EMITS: ClassVar[list[type[BaseEvent[Any]]]] = [
		DownloadProgressEvent,
		DownloadStartedEvent,
		FileDownloadedEvent,
	]

	# Private state
	_sessions_with_listeners: set[str] = PrivateAttr(default_factory=set)  # Track sessions that already have download listeners
	_active_downloads: dict[str, Any] = PrivateAttr(default_factory=dict)
	_pdf_viewer_cache: dict[str, bool] = PrivateAttr(default_factory=dict)  # Cache PDF viewer status by target URL
	_download_cdp_session_setup: bool = PrivateAttr(default=False)  # Track if CDP session is set up
	_download_cdp_session: Any = PrivateAttr(default=None)  # Store CDP session reference
	_cdp_event_tasks: set[asyncio.Task] = PrivateAttr(default_factory=set)  # Track CDP event handler tasks
	_cdp_downloads_info: dict[str, dict[str, Any]] = PrivateAttr(default_factory=dict)  # Map guid -> info
	_session_pdf_urls: dict[str, str] = PrivateAttr(default_factory=dict)  # URL -> path for PDFs downloaded this session
	_initial_downloads_snapshot: set[str] = PrivateAttr(default_factory=set)  # Files present when watchdog started
	_network_monitored_targets: set[str] = PrivateAttr(default_factory=set)  # Track targets with network monitoring enabled
	_detected_downloads: set[str] = PrivateAttr(default_factory=set)  # Track detected download URLs to avoid duplicates
	_network_callback_registered: bool = PrivateAttr(default=False)  # Track if global network callback is registered

	# Direct callback support for download waiting (bypasses event bus for synchronization)
	_download_start_callbacks: list[Any] = PrivateAttr(default_factory=list)  # Callbacks for download start
	_download_progress_callbacks: list[Any] = PrivateAttr(default_factory=list)  # Callbacks for download progress
	_download_complete_callbacks: list[Any] = PrivateAttr(default_factory=list)  # Callbacks for download complete
	_tracker: DownloadTracker = PrivateAttr()
	_network_monitor: NetworkDownloadMonitor = PrivateAttr()

	def model_post_init(self, __context: Any) -> None:
		self._tracker = DownloadTracker(self)
		self._network_monitor = NetworkDownloadMonitor(self)

	@property
	def tracker(self) -> DownloadTracker:
		return self._tracker

	@property
	def network_monitor(self) -> NetworkDownloadMonitor:
		return self._network_monitor

	def register_download_callbacks(
		self,
		on_start: Any | None = None,
		on_progress: Any | None = None,
		on_complete: Any | None = None,
	) -> None:
		"""Register direct callbacks for download events

		Callbacks called sync from CDP event handlers, so click
		handlers receive download notif without waiting for event bus to process
		"""
		self.logger.debug(
			f'[DownloadsWatchdog] Registering callbacks: start={on_start is not None}, progress={on_progress is not None}, complete={on_complete is not None}'
		)
		if on_start:
			self._download_start_callbacks.append(on_start)
			self.logger.debug(
				f'[DownloadsWatchdog] Registered start callback, now have {len(self._download_start_callbacks)} start callbacks'
			)
		if on_progress:
			self._download_progress_callbacks.append(on_progress)
		if on_complete:
			self._download_complete_callbacks.append(on_complete)

	def unregister_download_callbacks(
		self,
		on_start: Any | None = None,
		on_progress: Any | None = None,
		on_complete: Any | None = None,
	) -> None:
		"""Unregister previously registered download callbacks."""
		if on_start and on_start in self._download_start_callbacks:
			self._download_start_callbacks.remove(on_start)
		if on_progress and on_progress in self._download_progress_callbacks:
			self._download_progress_callbacks.remove(on_progress)
		if on_complete and on_complete in self._download_complete_callbacks:
			self._download_complete_callbacks.remove(on_complete)

	async def on_BrowserLaunchEvent(self, event: BrowserLaunchEvent) -> None:
		self.logger.debug(f'[DownloadsWatchdog] Received BrowserLaunchEvent, EventBus ID: {id(self.event_bus)}')
		# Ensure downloads directory exists
		downloads_path = self.browser_session.browser_profile.downloads_path
		if downloads_path:
			expanded_path = Path(downloads_path).expanduser().resolve()
			expanded_path.mkdir(parents=True, exist_ok=True)
			self.logger.debug(f'[DownloadsWatchdog] Ensured downloads directory exists: {expanded_path}')

			# Capture initial files to detect new downloads reliably
			if expanded_path.exists():
				for f in expanded_path.iterdir():
					if f.is_file() and not f.name.startswith('.'):
						self._initial_downloads_snapshot.add(f.name)
				self.logger.debug(
					f'[DownloadsWatchdog] Captured initial downloads: {len(self._initial_downloads_snapshot)} files'
				)

	async def on_TabCreatedEvent(self, event: TabCreatedEvent) -> None:
		"""Monitor new tabs for downloads."""
		# logger.info(f'[DownloadsWatchdog] TabCreatedEvent received for tab {event.target_id[-4:]}: {event.url}')

		# Assert downloads path is configured (should always be set by BrowserProfile default)
		assert self.browser_session.browser_profile.downloads_path is not None, 'Downloads path must be configured'

		if event.target_id:
			# logger.info(f'[DownloadsWatchdog] Found target for tab {event.target_id}, calling attach_to_target')
			await self.tracker.attach_to_target(event.target_id)
		else:
			self.logger.warning(f'[DownloadsWatchdog] No target found for tab {event.target_id}')

	async def on_TabClosedEvent(self, event: TabClosedEvent) -> None:
		"""Stop monitoring closed tabs."""
		pass  # No cleanup needed, browser context handles target lifecycle

	async def on_BrowserStateRequestEvent(self, event: BrowserStateRequestEvent) -> None:
		"""Handle browser state request events."""
		# Use public API - automatically validates and waits for recovery if needed
		self.logger.debug(f'[DownloadsWatchdog] on_BrowserStateRequestEvent started, event_id={event.event_id[-4:]}')
		try:
			cdp_session = await self.browser_session.get_or_create_cdp_session()
		except ValueError:
			self.logger.warning(f'[DownloadsWatchdog] No valid focus, skipping BrowserStateRequestEvent {event.event_id[-4:]}')
			return  # No valid focus, skip

		self.logger.debug(
			f'[DownloadsWatchdog] About to call get_current_page_url(), target_id={cdp_session.target_id[-4:] if cdp_session.target_id else "None"}'
		)
		url = await self.browser_session.get_current_page_url()
		self.logger.debug(f'[DownloadsWatchdog] Got URL: {url[:80] if url else "None"}')

		if not url:
			self.logger.warning(f'[DownloadsWatchdog] No URL found for BrowserStateRequestEvent {event.event_id[-4:]}')
			return

		target_id = cdp_session.target_id
		self.logger.debug(f'[DownloadsWatchdog] About to dispatch NavigationCompleteEvent for target {target_id[-4:]}')
		self.event_bus.dispatch(
			NavigationCompleteEvent(
				event_type='NavigationCompleteEvent',
				url=url,
				target_id=target_id,
				event_parent_id=event.event_id,
			)
		)
		self.logger.debug('[DownloadsWatchdog] Successfully completed BrowserStateRequestEvent')

	async def on_BrowserStoppedEvent(self, event: BrowserStoppedEvent) -> None:
		"""Clean up when browser stops."""
		# Cancel all CDP event handler tasks
		for task in list(self._cdp_event_tasks):
			if not task.done():
				task.cancel()
		# Wait for all tasks to complete cancellation
		if self._cdp_event_tasks:
			await asyncio.gather(*self._cdp_event_tasks, return_exceptions=True)
		self._cdp_event_tasks.clear()

		# Clean up CDP session
		# CDP sessions are now cached and managed by BrowserSession
		self._download_cdp_session = None
		self._download_cdp_session_setup = False

		# Clear other state
		self._sessions_with_listeners.clear()
		self._active_downloads.clear()
		self._pdf_viewer_cache.clear()
		self._session_pdf_urls.clear()
		self._network_monitored_targets.clear()
		self._detected_downloads.clear()
		self._initial_downloads_snapshot.clear()
		self._network_callback_registered = False

	async def on_NavigationCompleteEvent(self, event: NavigationCompleteEvent) -> None:
		"""Check for PDFs after navigation completes."""
		self.logger.debug(f'[DownloadsWatchdog] NavigationCompleteEvent received for {event.url}, tab #{event.target_id[-4:]}')

		# Clear PDF cache for the navigated URL since content may have changed
		if event.url in self._pdf_viewer_cache:
			del self._pdf_viewer_cache[event.url]

		# Check if auto-download is enabled
		auto_download_enabled = self._is_auto_download_enabled()
		if not auto_download_enabled:
			return

		# Note: Using network-based PDF detection that doesn't require JavaScript

		target_id = event.target_id
		self.logger.debug(f'[DownloadsWatchdog] Got target_id={target_id} for tab #{event.target_id[-4:]}')

		is_pdf = await PdfDownloadController(self).check_for_pdf_viewer(target_id)

		if is_pdf:
			self.logger.debug(f'[DownloadsWatchdog] 📄 PDF detected at {event.url}, triggering auto-download...')
			download_path = await PdfDownloadController(self).trigger_pdf_download(target_id)
			if not download_path:
				self.logger.warning(f'[DownloadsWatchdog] ⚠️ PDF download failed for {event.url}')

	def _is_auto_download_enabled(self) -> bool:
		"""Check if auto-download PDFs is enabled in browser profile."""
		return self.browser_session.browser_profile.auto_download_pdfs


# Fix Pydantic circular dependency - this will be called from session.py after BrowserSession is defined
