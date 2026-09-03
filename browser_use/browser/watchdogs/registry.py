"""Construction and lifecycle ownership for browser watchdogs."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession
	from browser_use.browser.watchdogs.aboutblank_watchdog import AboutBlankWatchdog
	from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog
	from browser_use.browser.watchdogs.dom_watchdog import DOMWatchdog
	from browser_use.browser.watchdogs.downloads_watchdog import DownloadsWatchdog
	from browser_use.browser.watchdogs.har_recording_watchdog import HarRecordingWatchdog
	from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog
	from browser_use.browser.watchdogs.permissions_watchdog import PermissionsWatchdog
	from browser_use.browser.watchdogs.popups_watchdog import PopupsWatchdog
	from browser_use.browser.watchdogs.recording_watchdog import RecordingWatchdog
	from browser_use.browser.watchdogs.screenshot_watchdog import ScreenshotWatchdog
	from browser_use.browser.watchdogs.security_watchdog import SecurityWatchdog
	from browser_use.browser.watchdogs.storage_state_watchdog import StorageStateWatchdog


class WatchdogRegistry:
	"""Own the watchdog instances attached to one browser session."""

	def __init__(self, browser_session: BrowserSession) -> None:
		self.browser_session = browser_session
		self._attached = False
		self._local_browser_attached = False
		self.downloads: DownloadsWatchdog | None = None
		self.aboutblank: AboutBlankWatchdog | None = None
		self.security: SecurityWatchdog | None = None
		self.storage_state: StorageStateWatchdog | None = None
		self.local_browser: LocalBrowserWatchdog | None = None
		self.default_action: DefaultActionWatchdog | None = None
		self.dom: DOMWatchdog | None = None
		self.screenshot: ScreenshotWatchdog | None = None
		self.permissions: PermissionsWatchdog | None = None
		self.recording: RecordingWatchdog | None = None
		self.popups: PopupsWatchdog | None = None
		self.har_recording: HarRecordingWatchdog | None = None

	def reset(self, *, preserve_local_browser: bool = False) -> None:
		preserved_local_browser = (
			self.local_browser
			if preserve_local_browser and self.local_browser is not None and self.local_browser.owns_browser_process
			else None
		)
		self._attached = False
		self._local_browser_attached = False
		self.downloads = None
		self.aboutblank = None
		self.security = None
		self.storage_state = None
		self.local_browser = preserved_local_browser
		self.default_action = None
		self.dom = None
		self.screenshot = None
		self.permissions = None
		self.recording = None
		self.popups = None
		self.har_recording = None

	def reattach_preserved_local_browser(self) -> None:
		"""Attach a retained process owner to the session's renewed event bus."""
		if self.local_browser is None or self._local_browser_attached:
			return
		self.local_browser.event_bus = self.browser_session.event_bus
		self.local_browser.attach_to_session()
		self._local_browser_attached = True

	async def attach(self) -> None:
		"""Initialize and attach all watchdogs with explicit handler registration."""
		# Prevent duplicate watchdog attachment
		if self._attached:
			self.browser_session.logger.debug('Watchdogs already attached, skipping duplicate attachment')
			return

		from browser_use.browser.watchdogs.aboutblank_watchdog import AboutBlankWatchdog

		# from browser_use.browser.crash_watchdog import CrashWatchdog
		from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog
		from browser_use.browser.watchdogs.dom_watchdog import DOMWatchdog
		from browser_use.browser.watchdogs.downloads_watchdog import DownloadsWatchdog
		from browser_use.browser.watchdogs.har_recording_watchdog import HarRecordingWatchdog
		from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog
		from browser_use.browser.watchdogs.permissions_watchdog import PermissionsWatchdog
		from browser_use.browser.watchdogs.popups_watchdog import PopupsWatchdog
		from browser_use.browser.watchdogs.recording_watchdog import RecordingWatchdog
		from browser_use.browser.watchdogs.screenshot_watchdog import ScreenshotWatchdog
		from browser_use.browser.watchdogs.security_watchdog import SecurityWatchdog
		from browser_use.browser.watchdogs.storage_state_watchdog import StorageStateWatchdog

		# Initialize DownloadsWatchdog
		DownloadsWatchdog.model_rebuild()
		self.downloads = DownloadsWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
		# self.browser_session.event_bus.on(BrowserLaunchEvent, self.downloads.on_BrowserLaunchEvent)
		# self.browser_session.event_bus.on(TabCreatedEvent, self.downloads.on_TabCreatedEvent)
		# self.browser_session.event_bus.on(TabClosedEvent, self.downloads.on_TabClosedEvent)
		# self.browser_session.event_bus.on(BrowserStoppedEvent, self.downloads.on_BrowserStoppedEvent)
		# self.browser_session.event_bus.on(NavigationCompleteEvent, self.downloads.on_NavigationCompleteEvent)
		self.downloads.attach_to_session()
		if self.browser_session.browser_profile.auto_download_pdfs:
			self.browser_session.logger.debug('📄 PDF auto-download enabled for this session')

		# Initialize StorageStateWatchdog conditionally
		# Enable when user provides either storage_state or user_data_dir (indicating they want persistence)
		should_enable_storage_state = (
			self.browser_session.browser_profile.storage_state is not None
			or self.browser_session.browser_profile.user_data_dir is not None
		)

		if should_enable_storage_state:
			StorageStateWatchdog.model_rebuild()
			self.storage_state = StorageStateWatchdog(
				event_bus=self.browser_session.event_bus,
				browser_session=self.browser_session,
				# More conservative defaults when auto-enabled
				auto_save_interval=60.0,  # 1 minute instead of 30 seconds
				save_on_change=False,  # Only save on shutdown by default
			)
			self.storage_state.attach_to_session()
			self.browser_session.logger.debug(
				f'🍪 StorageStateWatchdog enabled (storage_state: {bool(self.browser_session.browser_profile.storage_state)}, user_data_dir: {bool(self.browser_session.browser_profile.user_data_dir)})'
			)
		else:
			self.browser_session.logger.debug('🍪 StorageStateWatchdog disabled (no storage_state or user_data_dir configured)')

		# Initialize LocalBrowserWatchdog, or reuse the process owner retained by stop().
		if self.local_browser is None:
			LocalBrowserWatchdog.model_rebuild()
			self.local_browser = LocalBrowserWatchdog(
				event_bus=self.browser_session.event_bus,
				browser_session=self.browser_session,
			)
		if not self._local_browser_attached:
			self.local_browser.event_bus = self.browser_session.event_bus
			self.local_browser.attach_to_session()
			self._local_browser_attached = True

		# Initialize SecurityWatchdog (hooks NavigationWatchdog and implements allowed_domains restriction)
		SecurityWatchdog.model_rebuild()
		self.security = SecurityWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
		# Core navigation is now handled in BrowserSession directly
		# SecurityWatchdog only handles security policy enforcement
		self.security.attach_to_session()

		# Initialize AboutBlankWatchdog (handles about:blank pages and DVD loading animation on first load)
		AboutBlankWatchdog.model_rebuild()
		self.aboutblank = AboutBlankWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
		# self.browser_session.event_bus.on(BrowserStopEvent, self.aboutblank.on_BrowserStopEvent)
		# self.browser_session.event_bus.on(BrowserStoppedEvent, self.aboutblank.on_BrowserStoppedEvent)
		# self.browser_session.event_bus.on(TabCreatedEvent, self.aboutblank.on_TabCreatedEvent)
		# self.browser_session.event_bus.on(TabClosedEvent, self.aboutblank.on_TabClosedEvent)
		self.aboutblank.attach_to_session()

		# Initialize PopupsWatchdog (handles accepting and dismissing JS dialogs, alerts, confirm, onbeforeunload, etc.)
		PopupsWatchdog.model_rebuild()
		self.popups = PopupsWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
		# self.browser_session.event_bus.on(TabCreatedEvent, self.popups.on_TabCreatedEvent)
		# self.browser_session.event_bus.on(DialogCloseEvent, self.popups.on_DialogCloseEvent)
		self.popups.attach_to_session()

		# Initialize PermissionsWatchdog (handles granting and revoking browser permissions like clipboard, microphone, camera, etc.)
		PermissionsWatchdog.model_rebuild()
		self.permissions = PermissionsWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
		# self.browser_session.event_bus.on(BrowserConnectedEvent, self.permissions.on_BrowserConnectedEvent)
		self.permissions.attach_to_session()

		# Initialize DefaultActionWatchdog (handles all default actions like click, type, scroll, go back, go forward, refresh, wait, send keys, upload file, scroll to text, etc.)
		DefaultActionWatchdog.model_rebuild()
		self.default_action = DefaultActionWatchdog(
			event_bus=self.browser_session.event_bus, browser_session=self.browser_session
		)
		# self.browser_session.event_bus.on(ClickElementEvent, self.default_action.on_ClickElementEvent)
		# self.browser_session.event_bus.on(TypeTextEvent, self.default_action.on_TypeTextEvent)
		# self.browser_session.event_bus.on(ScrollEvent, self.default_action.on_ScrollEvent)
		# self.browser_session.event_bus.on(GoBackEvent, self.default_action.on_GoBackEvent)
		# self.browser_session.event_bus.on(GoForwardEvent, self.default_action.on_GoForwardEvent)
		# self.browser_session.event_bus.on(RefreshEvent, self.default_action.on_RefreshEvent)
		# self.browser_session.event_bus.on(WaitEvent, self.default_action.on_WaitEvent)
		# self.browser_session.event_bus.on(SendKeysEvent, self.default_action.on_SendKeysEvent)
		# self.browser_session.event_bus.on(UploadFileEvent, self.default_action.on_UploadFileEvent)
		# self.browser_session.event_bus.on(ScrollToTextEvent, self.default_action.on_ScrollToTextEvent)
		self.default_action.attach_to_session()

		# Initialize ScreenshotWatchdog (handles taking screenshots of the browser)
		ScreenshotWatchdog.model_rebuild()
		self.screenshot = ScreenshotWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
		# self.browser_session.event_bus.on(BrowserStartEvent, self.screenshot.on_BrowserStartEvent)
		# self.browser_session.event_bus.on(BrowserStoppedEvent, self.screenshot.on_BrowserStoppedEvent)
		# self.browser_session.event_bus.on(ScreenshotEvent, self.screenshot.on_ScreenshotEvent)
		self.screenshot.attach_to_session()

		# Initialize DOMWatchdog (handles building the DOM tree and detecting interactive elements, depends on ScreenshotWatchdog)
		DOMWatchdog.model_rebuild()
		self.dom = DOMWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
		# self.browser_session.event_bus.on(TabCreatedEvent, self.dom.on_TabCreatedEvent)
		# self.browser_session.event_bus.on(BrowserStateRequestEvent, self.dom.on_BrowserStateRequestEvent)
		self.dom.attach_to_session()

		# Initialize RecordingWatchdog (handles video recording)
		RecordingWatchdog.model_rebuild()
		self.recording = RecordingWatchdog(event_bus=self.browser_session.event_bus, browser_session=self.browser_session)
		self.recording.attach_to_session()

		# Initialize HarRecordingWatchdog if record_har_path is configured (handles HTTPS HAR capture)
		if self.browser_session.browser_profile.record_har_path:
			HarRecordingWatchdog.model_rebuild()
			self.har_recording = HarRecordingWatchdog(
				event_bus=self.browser_session.event_bus, browser_session=self.browser_session
			)
			self.har_recording.attach_to_session()

		# Mark watchdogs as attached to prevent duplicate attachment
		self._attached = True
