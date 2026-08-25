"""Event-driven browser session."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, cast, overload
from urllib.parse import urlparse, urlunparse
from uuid import UUID

import httpx
from bubus import EventBus
from cdp_use import CDPClient
from cdp_use.cdp.fetch import AuthRequiredEvent
from cdp_use.cdp.network import Cookie
from cdp_use.cdp.target import SessionID, TargetID
from cdp_use.cdp.target.commands import CreateTargetParameters
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from uuid_extensions import uuid7str

from browser_use.browser._cdp_timeout import TimeoutWrappedCDPClient
from browser_use.browser.cloud.cloud import CloudBrowserAuthError, CloudBrowserClient, CloudBrowserError

# CDP logging is now handled by setup_logging() in logging_config.py
# It automatically sets CDP logs to the same level as browser_use logs
from browser_use.browser.cloud.views import CreateBrowserRequest, ProxyCountryCode
from browser_use.browser.event_bus import ResilientEventBus as _ResilientEventBus
from browser_use.browser.events import (
	AgentFocusChangedEvent,
	BrowserConnectedEvent,
	BrowserErrorEvent,
	BrowserLaunchEvent,
	BrowserLaunchResult,
	BrowserReconnectedEvent,
	BrowserReconnectingEvent,
	BrowserStartEvent,
	BrowserStateRequestEvent,
	BrowserStopEvent,
	BrowserStoppedEvent,
	CloseTabEvent,
	FileDownloadedEvent,
	NavigateToUrlEvent,
	NavigationCompleteEvent,
	NavigationStartedEvent,
	SwitchTabEvent,
	TabClosedEvent,
	TabCreatedEvent,
)
from browser_use.browser.profile import (
	CLOUD_PROXY_UNSET,
	BrowserProfile,
	ProxySettings,
	resolve_browser_profile,
)
from browser_use.browser.views import BrowserStateSummary, TabInfo
from browser_use.browser.watchdogs.registry import WatchdogRegistry
from browser_use.dom.browser_state import BrowserDomState
from browser_use.dom.views import SerializedDOMState
from browser_use.logging_utils import log_pretty_url
from browser_use.runtime import create_task_with_error_handling
from browser_use.security import is_new_tab_page

if TYPE_CHECKING:
	from browser_use.actor.page import Page
	from browser_use.browser.demo_mode import DemoMode
	from browser_use.browser.session_manager import CDPSession, Target
	from browser_use.browser.watchdogs.captcha_watchdog import CaptchaWaitResult

DEFAULT_BROWSER_PROFILE = BrowserProfile()

_LOGGED_UNIQUE_SESSION_IDS = set()  # track unique session IDs that have been logged to make sure we always assign a unique enough id to new sessions and avoid ambiguity in logs
red = '\033[91m'
reset = '\033[0m'


class BrowserSession(BaseModel):
	"""Event-driven browser session.

	This class provides a 2-layer architecture:
	- High-level event handling for agents/tools
	- Direct CDP/Playwright calls for browser operations

	Supports both event-driven and imperative calling styles.

	Browser configuration is stored in the browser_profile, session identity in direct fields:
	```python
	# Direct settings (recommended for most users)
	session = BrowserSession(headless=True, user_data_dir='./profile')

	# Or use a profile (for advanced use cases)
	session = BrowserSession(browser_profile=BrowserProfile(...))

	# Access session fields directly, browser settings via profile or property
	print(session.id)  # Session field
	```
	"""

	model_config = ConfigDict(
		arbitrary_types_allowed=True,
		validate_assignment=True,
		extra='forbid',
		revalidate_instances='never',  # resets private attrs on every model rebuild
	)

	# Overload 1: Cloud browser mode (use cloud-specific params)
	@overload
	def __init__(
		self,
		*,
		# Cloud browser params - use these for cloud mode
		cloud_profile_id: UUID | str | None = None,
		cloud_proxy_country_code: ProxyCountryCode | None = None,
		cloud_timeout: int | None = None,
		use_cloud: bool | None = None,
		cloud_browser_params: CreateBrowserRequest | None = None,
		# Common params that work with cloud
		id: str | None = None,
		headers: dict[str, str] | None = None,
		allowed_domains: list[str] | None = None,
		prohibited_domains: list[str] | None = None,
		keep_alive: bool | None = None,
		minimum_wait_page_load_time: float | None = None,
		wait_for_network_idle_page_load_time: float | None = None,
		wait_between_actions: float | None = None,
		captcha_solver: bool | None = None,
		auto_download_pdfs: bool | None = None,
		cookie_whitelist_domains: list[str] | None = None,
		cross_origin_iframes: bool | None = None,
		highlight_elements: bool | None = None,
		dom_highlight_elements: bool | None = None,
		paint_order_filtering: bool | None = None,
		max_iframes: int | None = None,
		max_iframe_depth: int | None = None,
	) -> None: ...

	# Overload 2: Local browser mode (use local browser params)
	@overload
	def __init__(
		self,
		*,
		# Core configuration for local
		id: str | None = None,
		cdp_url: str | None = None,
		browser_profile: BrowserProfile | None = None,
		# Local browser launch params
		executable_path: str | Path | None = None,
		headless: bool | None = None,
		user_data_dir: str | Path | None = None,
		args: list[str] | None = None,
		downloads_path: str | Path | None = None,
		# Common params
		headers: dict[str, str] | None = None,
		allowed_domains: list[str] | None = None,
		prohibited_domains: list[str] | None = None,
		keep_alive: bool | None = None,
		minimum_wait_page_load_time: float | None = None,
		wait_for_network_idle_page_load_time: float | None = None,
		wait_between_actions: float | None = None,
		auto_download_pdfs: bool | None = None,
		cookie_whitelist_domains: list[str] | None = None,
		cross_origin_iframes: bool | None = None,
		highlight_elements: bool | None = None,
		dom_highlight_elements: bool | None = None,
		paint_order_filtering: bool | None = None,
		max_iframes: int | None = None,
		max_iframe_depth: int | None = None,
		# All other local params
		env: dict[str, str | float | bool] | None = None,
		ignore_default_args: list[str] | Literal[True] | None = None,
		channel: str | None = None,
		chromium_sandbox: bool | None = None,
		devtools: bool | None = None,
		traces_dir: str | Path | None = None,
		accept_downloads: bool | None = None,
		permissions: list[str] | None = None,
		user_agent: str | None = None,
		screen: dict | None = None,
		viewport: dict | None = None,
		no_viewport: bool | None = None,
		device_scale_factor: float | None = None,
		record_har_content: str | None = None,
		record_har_mode: str | None = None,
		record_har_path: str | Path | None = None,
		record_video_dir: str | Path | None = None,
		record_video_framerate: int | None = None,
		record_video_size: dict | None = None,
		storage_state: str | Path | dict[str, Any] | None = None,
		disable_security: bool | None = None,
		deterministic_rendering: bool | None = None,
		proxy: ProxySettings | None = None,
		enable_default_extensions: bool | None = None,
		captcha_solver: bool | None = None,
		window_size: dict | None = None,
		window_position: dict | None = None,
		filter_highlight_ids: bool | None = None,
		profile_directory: str | None = None,
	) -> None: ...

	def __init__(
		self,
		# Core configuration
		id: str | None = None,
		cdp_url: str | None = None,
		is_local: bool = False,
		browser_profile: BrowserProfile | None = None,
		# Cloud browser params (don't mix with local browser params)
		cloud_profile_id: UUID | str | None = None,
		cloud_proxy_country_code: ProxyCountryCode | None = CLOUD_PROXY_UNSET,  # type: ignore[assignment]
		cloud_timeout: int | None = None,
		# BrowserProfile fields that can be passed directly
		# From BrowserConnectArgs
		headers: dict[str, str] | None = None,
		# From BrowserLaunchArgs
		env: dict[str, str | float | bool] | None = None,
		executable_path: str | Path | None = None,
		headless: bool | None = None,
		args: list[str] | None = None,
		ignore_default_args: list[str] | Literal[True] | None = None,
		channel: str | None = None,
		chromium_sandbox: bool | None = None,
		devtools: bool | None = None,
		downloads_path: str | Path | None = None,
		traces_dir: str | Path | None = None,
		# From BrowserContextArgs
		accept_downloads: bool | None = None,
		permissions: list[str] | None = None,
		user_agent: str | None = None,
		screen: dict | None = None,
		viewport: dict | None = None,
		no_viewport: bool | None = None,
		device_scale_factor: float | None = None,
		record_har_content: str | None = None,
		record_har_mode: str | None = None,
		record_har_path: str | Path | None = None,
		record_video_dir: str | Path | None = None,
		record_video_framerate: int | None = None,
		record_video_size: dict | None = None,
		# From BrowserLaunchPersistentContextArgs
		user_data_dir: str | Path | None = None,
		# From BrowserNewContextArgs
		storage_state: str | Path | dict[str, Any] | None = None,
		# BrowserProfile specific fields
		## Cloud Browser Fields
		use_cloud: bool | None = None,
		cloud_browser_params: CreateBrowserRequest | None = None,
		## Other params
		disable_security: bool | None = None,
		deterministic_rendering: bool | None = None,
		allowed_domains: list[str] | None = None,
		prohibited_domains: list[str] | None = None,
		keep_alive: bool | None = None,
		proxy: ProxySettings | None = None,
		enable_default_extensions: bool | None = None,
		captcha_solver: bool | None = None,
		window_size: dict | None = None,
		window_position: dict | None = None,
		minimum_wait_page_load_time: float | None = None,
		wait_for_network_idle_page_load_time: float | None = None,
		wait_between_actions: float | None = None,
		filter_highlight_ids: bool | None = None,
		auto_download_pdfs: bool | None = None,
		profile_directory: str | None = None,
		cookie_whitelist_domains: list[str] | None = None,
		# DOM extraction layer configuration
		cross_origin_iframes: bool | None = None,
		highlight_elements: bool | None = None,
		dom_highlight_elements: bool | None = None,
		paint_order_filtering: bool | None = None,
		# Iframe processing limits
		max_iframes: int | None = None,
		max_iframe_depth: int | None = None,
	):
		# Following the same pattern as AgentSettings in service.py
		# Only pass non-None values to avoid validation errors
		# Also filter _UNSET sentinel values (used for proxy params)
		profile_kwargs = {
			k: v
			for k, v in locals().items()
			if k
			not in [
				'self',
				'__class__',
				'browser_profile',
				'id',
				'cloud_profile_id',
				'cloud_proxy_country_code',
				'cloud_timeout',
			]
			and v is not None
			and v is not CLOUD_PROXY_UNSET
		}

		resolved_browser_profile = resolve_browser_profile(
			browser_profile=browser_profile,
			cdp_url=cdp_url,
			cloud_profile_id=cloud_profile_id,
			cloud_proxy_country_code=cloud_proxy_country_code,
			cloud_timeout=cloud_timeout,
			profile_kwargs=profile_kwargs,
		)

		# Initialize the Pydantic model
		super().__init__(
			id=id or str(uuid7str()),
			browser_profile=resolved_browser_profile,
		)

	# Session configuration (session identity only)
	id: str = Field(default_factory=lambda: str(uuid7str()), description='Unique identifier for this browser session')

	# Browser configuration (reusable profile)
	browser_profile: BrowserProfile = Field(
		default_factory=lambda: DEFAULT_BROWSER_PROFILE,
		description='BrowserProfile() options to use for the session, otherwise a default profile will be used',
	)

	# LLM screenshot resizing configuration
	llm_screenshot_size: tuple[int, int] | None = Field(
		default=None,
		description='Target size (width, height) to resize screenshots before sending to LLM. Coordinates from LLM will be scaled back to original viewport size.',
	)

	@classmethod
	def from_system_chrome(cls, profile_directory: str | None = None, **kwargs: Any) -> Self:
		"""Create a BrowserSession using system's Chrome installation and profile"""
		from browser_use.browser.chrome import find_chrome_executable, get_chrome_profile_path, list_chrome_profiles

		executable_path = find_chrome_executable()
		if executable_path is None:
			raise RuntimeError(
				'Chrome not found. Please install Chrome or use Browser() with explicit executable_path.\n'
				'Expected locations:\n'
				'  macOS: /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n'
				'  Linux: /usr/bin/google-chrome or /usr/bin/chromium\n'
				'  Windows: C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
			)

		user_data_dir = get_chrome_profile_path(None, executable_path=executable_path)
		if user_data_dir is None:
			raise RuntimeError(
				'Could not detect Chrome profile directory for your platform.\n'
				'Expected locations:\n'
				'  macOS: ~/Library/Application Support/Google/Chrome\n'
				'  Linux: ~/.config/google-chrome or ~/.config/chromium\n'
				'  Windows: %LocalAppData%\\Google\\Chrome\\User Data'
			)

		# Auto-select profile if not specified
		profiles = list_chrome_profiles()
		if profile_directory is None:
			if profiles:
				# Use first available profile
				profile_directory = profiles[0]['directory']
				logging.getLogger('browser_use').info(
					f'Auto-selected Chrome profile: {profiles[0]["name"]} ({profile_directory})'
				)
			else:
				profile_directory = 'Default'

		return cls(
			executable_path=executable_path,
			user_data_dir=user_data_dir,
			profile_directory=profile_directory,
			**kwargs,
		)

	@classmethod
	def list_chrome_profiles(cls) -> list[dict[str, str]]:
		"""List available Chrome profiles on the system"""
		from browser_use.browser.chrome import list_chrome_profiles

		return list_chrome_profiles()

	# Convenience properties for common browser settings
	@property
	def cdp_url(self) -> str | None:
		"""CDP URL from browser profile."""
		return self.browser_profile.cdp_url

	@property
	def is_local(self) -> bool:
		"""Whether this is a local browser instance from browser profile."""
		return self.browser_profile.is_local

	@property
	def is_cdp_connected(self) -> bool:
		"""Check if the CDP WebSocket connection is alive and usable.

		Returns True only if the root CDP client exists and its WebSocket is in OPEN state.
		A dead/closing/closed WebSocket returns False, preventing handlers from dispatching
		CDP commands that would hang until timeout on a broken connection.
		"""
		if self._cdp_client_root is None or self._cdp_client_root.ws is None:
			return False
		try:
			from websockets.protocol import State

			return self._cdp_client_root.ws.state is State.OPEN
		except Exception:
			return False

	async def wait_if_captcha_solving(self, timeout: float | None = None) -> CaptchaWaitResult | None:
		"""Wait if a captcha is currently being solved by the browser proxy.

		Returns:
			A CaptchaWaitResult if we had to wait, or None if no captcha was in progress.
		"""
		if self.watchdogs.captcha is not None:
			return await self.watchdogs.captcha.wait_if_captcha_solving(timeout=timeout)
		return None

	@property
	def is_reconnecting(self) -> bool:
		"""Whether a WebSocket reconnection attempt is currently in progress."""
		return self._reconnecting

	@property
	def demo_mode(self) -> DemoMode | None:
		"""Lazy init demo mode helper when enabled."""
		if not self.browser_profile.demo_mode:
			return None
		if self._demo_mode is None:
			from browser_use.browser.demo_mode import DemoMode

			self._demo_mode = DemoMode(self)
		return self._demo_mode

	@property
	def dom_state(self) -> BrowserDomState:
		"""DOM-derived state and operations for this browser session."""
		return self._dom_state

	@property
	def watchdogs(self) -> WatchdogRegistry:
		"""Watchdogs attached to this browser session."""
		return self._watchdogs

	@property
	def session_manager(self) -> Any:
		"""Targets, CDP sessions, and frame routing for this browser session."""
		return self._session_manager

	# Main shared event bus for all browser session + all watchdogs
	event_bus: EventBus = Field(default_factory=_ResilientEventBus)

	# Mutable public state - which target has agent focus
	agent_focus_target_id: TargetID | None = None

	# Mutable private state shared between watchdogs
	_cdp_client_root: CDPClient | None = PrivateAttr(default=None)
	_connection_lock: Any = PrivateAttr(default=None)  # asyncio.Lock for preventing concurrent connections

	_session_manager: Any = PrivateAttr()
	_dom_state: BrowserDomState = PrivateAttr()
	_watchdogs: WatchdogRegistry = PrivateAttr()
	_downloaded_files: list[str] = PrivateAttr(default_factory=list)  # Track files downloaded during this session
	_closed_popup_messages: list[str] = PrivateAttr(default_factory=list)  # Store messages from auto-closed JavaScript dialogs

	_cloud_browser_client: CloudBrowserClient = PrivateAttr(default_factory=lambda: CloudBrowserClient())
	_demo_mode: DemoMode | None = PrivateAttr(default=None)

	# WebSocket reconnection state
	# Max wait = attempts * timeout_per_attempt + sum(delays) + small buffer
	# Default: 3 * 15s + (1+2+4)s + 2s = 54s
	RECONNECT_WAIT_TIMEOUT: float = 54.0
	_reconnecting: bool = PrivateAttr(default=False)
	_reconnect_event: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
	_reconnect_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
	_reconnect_task: asyncio.Task | None = PrivateAttr(default=None)
	_intentional_stop: bool = PrivateAttr(default=False)

	_logger: Any = PrivateAttr(default=None)

	@property
	def logger(self) -> Any:
		"""Get instance-specific logger with session ID in the name"""
		# **regenerate it every time** because our id and str(self) can change as browser connection state changes
		# if self._logger is None or not self._cdp_client_root:
		# 	self._logger = logging.getLogger(f'browser_use.{self}')
		return logging.getLogger(f'browser_use.{self}')

	@cached_property
	def _id_for_logs(self) -> str:
		"""Get human-friendly semi-unique identifier for differentiating different BrowserSession instances in logs"""
		str_id = self.id[-4:]  # default to last 4 chars of truly random uuid, less helpful than cdp port but always unique enough
		port_number = (self.cdp_url or 'no-cdp').rsplit(':', 1)[-1].split('/', 1)[0].strip()
		port_is_random = not port_number.startswith('922')
		port_is_unique_enough = port_number not in _LOGGED_UNIQUE_SESSION_IDS
		if port_number and port_number.isdigit() and port_is_random and port_is_unique_enough:
			# if cdp port is random/unique enough to identify this session, use it as our id in logs
			_LOGGED_UNIQUE_SESSION_IDS.add(port_number)
			str_id = port_number
		return str_id

	@property
	def _tab_id_for_logs(self) -> str:
		return self.agent_focus_target_id[-2:] if self.agent_focus_target_id else f'{red}--{reset}'

	def __repr__(self) -> str:
		return f'BrowserSession🅑 {self._id_for_logs} 🅣 {self._tab_id_for_logs} (cdp_url={self.cdp_url}, profile={self.browser_profile})'

	def __str__(self) -> str:
		return f'BrowserSession🅑 {self._id_for_logs} 🅣 {self._tab_id_for_logs}'

	async def reset(self) -> None:
		"""Clear all cached CDP sessions with proper cleanup."""
		await self._reset()

	async def _reset(self, *, preserve_owned_local_browser: bool = False) -> None:
		"""Clear cached connection state, optionally retaining an owned local process."""
		local_browser = self.watchdogs.local_browser
		if (
			preserve_owned_local_browser
			and local_browser is not None
			and local_browser._subprocess is not None
			and not local_browser.owns_browser_process
		):
			# The process can exit after stop() decides to preserve it. Finish the
			# dead owner's profile/temp cleanup before the registry discards it.
			await local_browser._cleanup_owned_browser_resources()
		preserve_local_browser = bool(
			preserve_owned_local_browser and local_browser is not None and local_browser.owns_browser_process
		)

		# Suppress auto-reconnect callback during teardown
		self._intentional_stop = True
		# Cancel any in-flight reconnection task
		if self._reconnect_task and not self._reconnect_task.done():
			self._reconnect_task.cancel()
			self._reconnect_task = None
		self._reconnecting = False
		self._reconnect_event.set()  # unblock any waiters

		cdp_status = 'connected' if self._cdp_client_root else 'not connected'
		session_mgr_status = 'exists' if self.session_manager else 'None'
		self.logger.debug(
			f'🔄 Resetting browser session (CDP: {cdp_status}, SessionManager: {session_mgr_status}, '
			f'focus: {self.agent_focus_target_id[-4:] if self.agent_focus_target_id else "None"})'
		)

		# Clear session manager (which owns _targets, _sessions, _target_sessions)
		await self.session_manager.clear()

		# Close CDP WebSocket before clearing to prevent stale event handlers
		if self._cdp_client_root:
			try:
				await self._cdp_client_root.stop()
				self.logger.debug('Closed CDP client WebSocket during reset')
			except Exception as e:
				self.logger.debug(f'Error closing CDP client during reset: {e}')

		self._cdp_client_root = None  # type: ignore
		self.dom_state.clear()
		self._downloaded_files.clear()

		self.agent_focus_target_id = None
		if self.is_local and not preserve_local_browser:
			self.browser_profile.cdp_url = None

		self.watchdogs.reset(preserve_local_browser=preserve_local_browser)
		if self._demo_mode:
			self._demo_mode.reset()
			self._demo_mode = None

		self._intentional_stop = False
		self.logger.info('✅ Browser session reset complete')

	def model_post_init(self, __context) -> None:
		"""Register event handlers after model initialization."""
		from browser_use.browser.session_manager import SessionManager

		self._session_manager = SessionManager(self)
		self._dom_state = BrowserDomState(self)
		self._watchdogs = WatchdogRegistry(self)
		self._connection_lock = asyncio.Lock()
		# Initialize reconnect event as set (no reconnection pending)
		self._reconnect_event = asyncio.Event()
		self._reconnect_event.set()
		self._attach_core_event_handlers()

	def _attach_core_event_handlers(self) -> None:
		"""Attach BrowserSession lifecycle handlers to the current event bus."""

		# Check if handlers are already registered to prevent duplicates
		from browser_use.browser.watchdog_base import BaseWatchdog

		start_handlers = self.event_bus.handlers.get('BrowserStartEvent', [])
		start_handler_names = [getattr(h, '__name__', str(h)) for h in start_handlers]

		if any('on_BrowserStartEvent' in name for name in start_handler_names):
			raise RuntimeError(
				'[BrowserSession] Duplicate handler registration attempted! '
				'on_BrowserStartEvent is already registered. '
				'This likely means BrowserSession was initialized multiple times with the same EventBus.'
			)

		BaseWatchdog.attach_handler_to_session(self, BrowserStartEvent, self.on_BrowserStartEvent)
		BaseWatchdog.attach_handler_to_session(self, BrowserStopEvent, self.on_BrowserStopEvent)
		BaseWatchdog.attach_handler_to_session(self, NavigateToUrlEvent, self.on_NavigateToUrlEvent)
		BaseWatchdog.attach_handler_to_session(self, SwitchTabEvent, self.on_SwitchTabEvent)
		BaseWatchdog.attach_handler_to_session(self, TabCreatedEvent, self.on_TabCreatedEvent)
		BaseWatchdog.attach_handler_to_session(self, TabClosedEvent, self.on_TabClosedEvent)
		BaseWatchdog.attach_handler_to_session(self, AgentFocusChangedEvent, self.on_AgentFocusChangedEvent)
		BaseWatchdog.attach_handler_to_session(self, FileDownloadedEvent, self.on_FileDownloadedEvent)
		BaseWatchdog.attach_handler_to_session(self, CloseTabEvent, self.on_CloseTabEvent)

	def _renew_event_bus(self) -> None:
		"""Replace a stopped event bus and attach this session's lifecycle handlers."""
		self.event_bus = _ResilientEventBus()
		self._attach_core_event_handlers()
		self.watchdogs.reattach_preserved_local_browser()

	async def start(self) -> None:
		"""Start the browser session."""
		start_event = self.event_bus.dispatch(BrowserStartEvent())
		await start_event
		# Ensure any exceptions from the event handler are propagated
		await start_event.event_result(raise_if_any=True, raise_if_none=False)

	async def _dispatch_stop_event(self, *, force: bool) -> None:
		"""Run all stop handlers and propagate cleanup failures before reset."""
		stop_event = self.event_bus.dispatch(BrowserStopEvent(force=force))
		await stop_event
		await stop_event.event_result(raise_if_any=True, raise_if_none=False)

	async def kill(self) -> None:
		"""Kill the browser session and reset all state."""
		previous_intentional_stop = self._intentional_stop
		self._intentional_stop = True
		self.logger.debug('🛑 kill() called - stopping browser with force=True and resetting state')

		try:
			await self._finalize_session_artifacts()
			await self._dispatch_stop_event(force=True)
		except Exception:
			# The caller must be able to retry kill() with the same process owner,
			# CDP endpoint, watchdog registry, and event bus.
			self._intentional_stop = previous_intentional_stop
			raise
		# Stop the event bus
		await self.event_bus.stop(clear=True, timeout=5)
		# Reset all state
		await self.reset()
		# Create a fresh event bus with the session lifecycle handlers attached
		self._renew_event_bus()

	async def stop(self) -> None:
		"""Disconnect while preserving a BrowserSession-owned local browser.

		Cloud and externally managed CDP sessions retain their existing stop
		behavior. URL-policy enforcement is inactive until this session reconnects.
		"""
		previous_intentional_stop = self._intentional_stop
		self._intentional_stop = True
		self.logger.debug('⏸️  stop() called - stopping browser gracefully (force=False) and resetting state')

		try:
			await self._finalize_session_artifacts()
			await self._dispatch_stop_event(force=False)
		except Exception:
			self._intentional_stop = previous_intentional_stop
			raise

		# A non-forced stop handler cleans an already-dead owned process. Decide
		# preservation only after every handler has completed.
		local_browser = self.watchdogs.local_browser
		preserve_owned_local_browser = bool(local_browser is not None and local_browser.owns_browser_process)

		# Stop the event bus
		await self.event_bus.stop(clear=True, timeout=5)
		# Reset all state
		await self._reset(preserve_owned_local_browser=preserve_owned_local_browser)
		# Create a fresh event bus with the session lifecycle handlers attached
		self._renew_event_bus()

	async def _finalize_session_artifacts(self) -> None:
		"""Finalize persisted session output before any handler can disconnect CDP."""
		from browser_use.browser.events import SaveStorageStateEvent

		save_event = self.event_bus.dispatch(SaveStorageStateEvent())
		await save_event

		recording = self.watchdogs.recording
		if recording is not None and recording.is_recording:
			try:
				await recording.stop_recording()
			except Exception as error:
				self.logger.warning(f'Failed to finalize browser recording before disconnect: {error}')

		har_recording = self.watchdogs.har_recording
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
		retained_local_browser = self.watchdogs.local_browser
		if (
			self.is_local
			and self._cdp_client_root is None
			and retained_local_browser is not None
			and retained_local_browser._subprocess is not None
			and not retained_local_browser.owns_browser_process
		):
			await retained_local_browser._cleanup_owned_browser_resources()
			self.browser_profile.cdp_url = None

		# Initialize and attach all watchdogs FIRST so LocalBrowserWatchdog can handle BrowserLaunchEvent
		await self.watchdogs.attach()

		try:
			# If no CDP URL, launch local browser or cloud browser
			if not self.cdp_url:
				if self.browser_profile.use_cloud or self.browser_profile.cloud_browser_params is not None:
					# Use cloud browser service
					try:
						# Use cloud_browser_params if provided, otherwise create empty request
						cloud_params = self.browser_profile.cloud_browser_params or CreateBrowserRequest()
						cloud_browser_response = await self._cloud_browser_client.create_browser(cloud_params)
						self.browser_profile.cdp_url = cloud_browser_response.cdpUrl
						self.browser_profile.is_local = False
						self.logger.info('🌤️ Successfully connected to cloud browser service')
					except CloudBrowserAuthError:
						raise
					except CloudBrowserError as e:
						raise CloudBrowserError(f'Failed to create cloud browser: {e}')
				elif self.is_local:
					# Launch local browser using event-driven approach
					launch_event = self.event_bus.dispatch(BrowserLaunchEvent())
					await launch_event

					# Get the CDP URL from LocalBrowserWatchdog handler result
					launch_result: BrowserLaunchResult = cast(
						BrowserLaunchResult, await launch_event.event_result(raise_if_none=True, raise_if_any=True)
					)
					self.browser_profile.cdp_url = launch_result.cdp_url
				else:
					raise ValueError('Got BrowserSession(is_local=False) but no cdp_url was provided to connect to!')

			assert self.cdp_url and '://' in self.cdp_url

			# Use lock to prevent concurrent connection attempts (race condition protection)
			async with self._connection_lock:
				# Only connect if not already connected
				if self._cdp_client_root is None:
					# Setup browser via CDP (for both local and remote cases)
					# Global timeout prevents connect() from hanging indefinitely on
					# slow/broken WebSocket connections (common on Lambda → remote browser)
					try:
						await asyncio.wait_for(self.connect(cdp_url=self.cdp_url), timeout=15.0)
					except TimeoutError:
						# Timeout cancels connect() via CancelledError, which bypasses
						# connect()'s `except Exception` cleanup (CancelledError is BaseException).
						# Clean up the partially-initialized client so future start attempts
						# don't skip reconnection due to _cdp_client_root being non-None.
						cdp_client = cast(CDPClient | None, self._cdp_client_root)
						if cdp_client is not None:
							try:
								await cdp_client.stop()
							except Exception:
								pass
							self._cdp_client_root = None
							try:
								await self.session_manager.clear()
							except Exception:
								pass
						self.agent_focus_target_id = None
						raise RuntimeError(
							f'connect() timed out after 15s — CDP connection to {self.cdp_url} is too slow or unresponsive'
						)
					assert self.cdp_client is not None

					# Notify that browser is connected (single place)
					# Ensure BrowserConnected handlers (storage_state restore) complete before
					# start() returns so cookies/storage are applied before navigation.
					await self.event_bus.dispatch(BrowserConnectedEvent(cdp_url=self.cdp_url))

					if self.browser_profile.demo_mode:
						try:
							demo = self.demo_mode
							if demo:
								await demo.ensure_ready()
						except Exception as exc:
							self.logger.warning(f'[DemoMode] Failed to inject demo overlay: {exc}')
				else:
					self.logger.debug('Already connected to CDP, skipping reconnection')
					if self.browser_profile.demo_mode:
						try:
							demo = self.demo_mode
							if demo:
								await demo.ensure_ready()
						except Exception as exc:
							self.logger.warning(f'[DemoMode] Failed to inject demo overlay: {exc}')

			# Return the CDP URL for other components
			return {'cdp_url': self.cdp_url}

		except Exception as e:
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='BrowserStartEventError',
					message=f'Failed to start browser: {type(e).__name__} {e}',
					details={'cdp_url': self.cdp_url, 'is_local': self.is_local},
				)
			)
			if self.is_local and not isinstance(e, (CloudBrowserAuthError, CloudBrowserError)):
				self.logger.warning(
					'Local browser failed to start. Cloud browsers require no local install and work out of the box.\n'
					'         Try: Browser(use_cloud=True)  |  Get an API key: https://cloud.browser-use.com?utm_source=oss&utm_medium=browser_launch_failure'
				)
			raise

	async def on_NavigateToUrlEvent(self, event: NavigateToUrlEvent) -> None:
		"""Handle navigation requests - core browser functionality."""
		self.logger.debug(f'[on_NavigateToUrlEvent] Received NavigateToUrlEvent: url={event.url}, new_tab={event.new_tab}')
		if not self.agent_focus_target_id:
			self.logger.warning('Cannot navigate - browser not connected')
			return

		target_id = None
		current_target_id = self.agent_focus_target_id

		# If new_tab=True but we're already in a new tab, set new_tab=False
		current_target = self.session_manager.get_target(current_target_id)
		if event.new_tab and is_new_tab_page(current_target.url):
			self.logger.debug(f'[on_NavigateToUrlEvent] Already on blank tab ({current_target.url}), reusing')
			event.new_tab = False

		try:
			# Find or create target for navigation
			self.logger.debug(f'[on_NavigateToUrlEvent] Processing new_tab={event.new_tab}')

			if event.new_tab:
				page_targets = self.session_manager.get_all_page_targets()
				self.logger.debug(f'[on_NavigateToUrlEvent] Found {len(page_targets)} existing tabs')

				# Look for existing about:blank tab that's not the current one
				for idx, target in enumerate(page_targets):
					self.logger.debug(f'[on_NavigateToUrlEvent] Tab {idx}: url={target.url}, targetId={target.target_id}')
					if target.url == 'about:blank' and target.target_id != current_target_id:
						target_id = target.target_id
						self.logger.debug(f'Reusing existing about:blank tab #{target_id[-4:]}')
						break

				# Create new tab if no reusable one found
				if not target_id:
					self.logger.debug('[on_NavigateToUrlEvent] No reusable about:blank tab found, creating new tab...')
					try:
						target_id = await self._cdp_create_new_page('about:blank')
						self.logger.debug(f'Created new tab #{target_id[-4:]}')
						# Dispatch TabCreatedEvent for new tab
						await self.event_bus.dispatch(TabCreatedEvent(target_id=target_id, url='about:blank'))
					except Exception as e:
						self.logger.error(f'[on_NavigateToUrlEvent] Failed to create new tab: {type(e).__name__}: {e}')
						# Fall back to using current tab
						target_id = current_target_id
						self.logger.warning(f'[on_NavigateToUrlEvent] Falling back to current tab #{target_id[-4:]}')
			else:
				# Use current tab
				target_id = target_id or current_target_id

			# Switch to target tab if needed (for both new_tab=True and new_tab=False)
			if self.agent_focus_target_id is None or self.agent_focus_target_id != target_id:
				self.logger.debug(
					f'[on_NavigateToUrlEvent] Switching to target tab {target_id[-4:]} (current: {self.agent_focus_target_id[-4:] if self.agent_focus_target_id else "none"})'
				)
				# Activate target (bring to foreground)
				await self.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
			else:
				self.logger.debug(f'[on_NavigateToUrlEvent] Already on target tab {target_id[-4:]}, skipping SwitchTabEvent')

			assert self.agent_focus_target_id is not None and self.agent_focus_target_id == target_id, (
				'Agent focus not updated to new target_id after SwitchTabEvent should have switched to it'
			)

			# Dispatch navigation started
			await self.event_bus.dispatch(NavigationStartedEvent(target_id=target_id, url=event.url))

			# Navigate to URL with proper lifecycle waiting
			loading_status = await self._navigate_and_wait(
				event.url,
				target_id,
				timeout=event.timeout_ms / 1000 if event.timeout_ms is not None else None,
				wait_until=event.wait_until,
				nav_timeout=event.event_timeout,
			)
			committed_url = await self._get_navigation_event_url(target_id, event.url)

			# Close any extension options pages that might have opened
			await self._close_extension_options_pages()

			# Dispatch navigation complete
			self.logger.debug(f'Dispatching NavigationCompleteEvent for {committed_url} (tab #{target_id[-4:]})')
			await self.event_bus.dispatch(
				NavigationCompleteEvent(
					target_id=target_id,
					url=committed_url,
					status=None,  # CDP doesn't provide status directly
					loading_status=loading_status,  # non-None when readiness timed out
				)
			)
			await self.event_bus.dispatch(AgentFocusChangedEvent(target_id=target_id, url=committed_url))

			# Note: These should be handled by dedicated watchdogs:
			# - Security checks (security_watchdog)
			# - Page health checks (crash_watchdog)
			# - Dialog handling (dialog_watchdog)
			# - Download handling (downloads_watchdog)
			# - DOM rebuilding (dom_watchdog)

		except Exception as e:
			self.logger.error(f'Navigation failed: {type(e).__name__}: {e}')
			if target_id:
				committed_url = await self._get_navigation_event_url(target_id, event.url)
				await self.event_bus.dispatch(
					NavigationCompleteEvent(
						target_id=target_id,
						url=committed_url,
						error_message=f'{type(e).__name__}: {e}',
					)
				)
				await self.event_bus.dispatch(AgentFocusChangedEvent(target_id=target_id, url=committed_url))
			raise

	async def _get_navigation_event_url(self, target_id: str, requested_url: str) -> str:
		"""Resolve the committed URL for navigation events, failing closed under URL restrictions."""
		committed_url = await self._get_committed_navigation_url(target_id)
		if committed_url is not None:
			return committed_url
		has_url_policy = bool(self.browser_profile.allowed_domains or self.browser_profile.prohibited_domains)
		return '' if has_url_policy else requested_url

	async def _get_committed_navigation_url(self, target_id: str) -> str | None:
		"""Return Chrome's committed main-frame URL, or None when it cannot be verified."""
		try:
			cdp_session = await self.get_or_create_cdp_session(target_id, focus=False)
			history = await cdp_session.cdp_client.send.Page.getNavigationHistory(session_id=cdp_session.session_id)
			entries = history.get('entries') or []
			current_index = history.get('currentIndex')
			if not isinstance(current_index, int) or current_index < 0 or current_index >= len(entries):
				return None
			url = entries[current_index].get('url')
			return url if isinstance(url, str) and url else None
		except Exception as exc:
			self.logger.warning(f'Could not verify committed navigation URL for target {target_id[-4:]}: {exc}')
			return None

	async def _navigate_and_wait(
		self,
		url: str,
		target_id: str,
		timeout: float | None = None,
		wait_until: str = 'load',
		nav_timeout: float | None = None,
	) -> str | None:
		"""Navigate to URL and wait for page readiness using CDP lifecycle events.

		Polls the per-target lifecycle event buffer (fed by SessionManager's single
		global Page.lifecycleEvent handler).
		wait_until controls the minimum acceptable signal: 'commit', 'domcontentloaded', 'load', 'networkidle'.
		nav_timeout controls the timeout for the CDP Page.navigate() call itself (defaults to 20.0s).

		Returns None when the requested readiness signal was observed, or a
		'timeout...' status string when the wait timed out — callers surface it via
		NavigationCompleteEvent.loading_status so downstream consumers know the page
		may not be fully loaded.
		"""
		cdp_session = await self.get_or_create_cdp_session(target_id, focus=False)

		if timeout is None:
			target = self.session_manager.get_target(target_id)
			current_url = target.url
			same_domain = (
				url.split('/')[2] == current_url.split('/')[2]
				if url.startswith('http') and current_url.startswith('http')
				else False
			)
			timeout = 3.0 if same_domain else 8.0

		nav_start_time = asyncio.get_event_loop().time()

		# Wrap Page.navigate() with timeout — heavy sites can block here for 10s+
		# Use nav_timeout parameter if provided, otherwise default to 20.0
		if nav_timeout is None:
			nav_timeout = 20.0
		try:
			nav_result = await asyncio.wait_for(
				cdp_session.cdp_client.send.Page.navigate(
					params={'url': url, 'transitionType': 'address_bar'},
					session_id=cdp_session.session_id,
				),
				timeout=nav_timeout,
			)
		except TimeoutError:
			duration_ms = (asyncio.get_event_loop().time() - nav_start_time) * 1000
			raise RuntimeError(f'Page.navigate() timed out after {nav_timeout}s ({duration_ms:.0f}ms) for {url}')

		if nav_result.get('errorText'):
			raise RuntimeError(f'Navigation failed: {nav_result["errorText"]}')

		if wait_until == 'commit':
			duration_ms = (asyncio.get_event_loop().time() - nav_start_time) * 1000
			self.logger.debug(f'✅ Page ready for {url} (commit, {duration_ms:.0f}ms)')
			return None

		navigation_id = nav_result.get('loaderId')

		# Page.navigate omits loaderId for same-document navigations (#fragment,
		# History API): the navigation is already committed and Chrome emits no new
		# load/DOMContentLoaded lifecycle events for it — waiting would only burn
		# the timeout against stale events from the previous document load.
		if not navigation_id:
			duration_ms = (asyncio.get_event_loop().time() - nav_start_time) * 1000
			self.logger.debug(f'✅ Page ready for {url} (same-document navigation, {duration_ms:.0f}ms)')
			return None
		start_time = asyncio.get_event_loop().time()
		seen_events = []

		# Per-target buffer owned by SessionManager — NOT a per-session attribute, whose
		# feeding handler used to get replaced whenever another target attached.
		lifecycle_events = self.session_manager.get_lifecycle_events(target_id)

		# Acceptable events by readiness level (higher is always acceptable)
		acceptable_events: set[str] = {'networkIdle'}
		if wait_until in ('load', 'domcontentloaded'):
			acceptable_events.add('load')
		if wait_until == 'domcontentloaded':
			acceptable_events.add('DOMContentLoaded')

		poll_interval = 0.05
		while (asyncio.get_event_loop().time() - start_time) < timeout:
			try:
				for event_data in list(lifecycle_events):
					event_name = event_data.get('name')
					event_loader_id = event_data.get('loaderId')

					event_str = f'{event_name}(loader={event_loader_id[:8] if event_loader_id else "none"})'
					if event_str not in seen_events:
						seen_events.append(event_str)

					# Skip events from a previous document in this frame (stale entries
					# carry the old loaderId; the buffer may hold pre-navigation events).
					if event_loader_id and navigation_id and event_loader_id != navigation_id:
						continue

					# Defense for events without a usable loaderId: only trust them if
					# they arrived after this navigation started.
					if not event_loader_id and event_data.get('timestamp', 0) < nav_start_time:
						continue

					if event_name in acceptable_events:
						duration_ms = (asyncio.get_event_loop().time() - nav_start_time) * 1000
						self.logger.debug(f'✅ Page ready for {url} ({event_name}, {duration_ms:.0f}ms)')
						return None

			except Exception as e:
				self.logger.debug(f'Error polling lifecycle events: {e}')

			await asyncio.sleep(poll_interval)

		duration_ms = (asyncio.get_event_loop().time() - nav_start_time) * 1000
		if not seen_events:
			self.logger.error(
				f'❌ No lifecycle events received for {url} after {duration_ms:.0f}ms! '
				f'Monitoring may have failed. Target: {cdp_session.target_id[:8]}'
			)
			return f'timeout after {timeout}s: no lifecycle events received (monitoring may have failed)'
		self.logger.warning(f'⚠️ Page readiness timeout ({timeout}s, {duration_ms:.0f}ms) for {url}')
		return f'timeout after {timeout}s waiting for {wait_until!r} (saw: {", ".join(seen_events[-5:])})'

	async def on_SwitchTabEvent(self, event: SwitchTabEvent) -> TargetID:
		"""Handle tab switching - core browser functionality."""
		if not self.agent_focus_target_id:
			raise RuntimeError('Cannot switch tabs - browser not connected')

		# Get all page targets
		page_targets = self.session_manager.get_all_page_targets()
		if event.target_id is None:
			# Most recently opened page
			if page_targets:
				# Update the target id to be the id of the most recently opened page, then proceed to switch to it
				event.target_id = page_targets[-1].target_id
			else:
				# No pages open at all, create a new one (handles switching to it automatically)
				assert self._cdp_client_root is not None, 'CDP client root not initialized - browser may not be connected yet'
				new_target = await self._cdp_client_root.send.Target.createTarget(params={'url': 'about:blank'})
				target_id = new_target['targetId']
				# Don't await, these may circularly trigger SwitchTabEvent and could deadlock, dispatch to enqueue and return
				self.event_bus.dispatch(TabCreatedEvent(url='about:blank', target_id=target_id))
				self.event_bus.dispatch(AgentFocusChangedEvent(target_id=target_id, url='about:blank'))
				return target_id

		# Switch to the target
		assert event.target_id is not None, 'target_id must be set at this point'
		# Ensure session exists and update agent focus (only for page/tab targets)
		cdp_session = await self.get_or_create_cdp_session(target_id=event.target_id, focus=True)

		# Visually switch to the tab in the browser
		# The Force Background Tab extension prevents Chrome from auto-switching when links create new tabs,
		# but we still want the agent to be able to explicitly switch tabs when needed
		await cdp_session.cdp_client.send.Target.activateTarget(params={'targetId': event.target_id})

		# Get target to access url
		target = self.session_manager.get_target(event.target_id)

		# dispatch focus changed event
		await self.event_bus.dispatch(
			AgentFocusChangedEvent(
				target_id=target.target_id,
				url=target.url,
			)
		)
		return target.target_id

	async def on_CloseTabEvent(self, event: CloseTabEvent) -> None:
		"""Handle tab closure - update focus if needed."""
		try:
			# Dispatch tab closed event
			await self.event_bus.dispatch(TabClosedEvent(target_id=event.target_id))

			# Try to close the target, but don't fail if it's already closed
			try:
				cdp_session = await self.get_or_create_cdp_session(target_id=None, focus=False)
				await cdp_session.cdp_client.send.Target.closeTarget(params={'targetId': event.target_id})
			except Exception as e:
				self.logger.debug(f'Target may already be closed: {e}')
		except Exception as e:
			self.logger.warning(f'Error during tab close cleanup: {e}')

	async def on_TabCreatedEvent(self, event: TabCreatedEvent) -> None:
		"""Handle tab creation - apply viewport settings to new tab."""
		# Note: Tab switching prevention is handled by the Force Background Tab extension
		# The extension automatically keeps focus on the current tab when new tabs are created

		# Apply viewport settings if configured
		if self.browser_profile.viewport and not self.browser_profile.no_viewport:
			try:
				viewport_width = self.browser_profile.viewport.width
				viewport_height = self.browser_profile.viewport.height
				device_scale_factor = self.browser_profile.device_scale_factor or 1.0

				self.logger.info(
					f'Setting viewport to {viewport_width}x{viewport_height} with device scale factor {device_scale_factor} whereas original device scale factor was {self.browser_profile.device_scale_factor}'
				)
				# Use the helper method with the new tab's target_id
				await self._cdp_set_viewport(viewport_width, viewport_height, device_scale_factor, target_id=event.target_id)

				self.logger.debug(f'Applied viewport {viewport_width}x{viewport_height} to tab {event.target_id[-8:]}')
			except Exception as e:
				self.logger.warning(f'Failed to set viewport for new tab {event.target_id[-8:]}: {e}')

	async def on_TabClosedEvent(self, event: TabClosedEvent) -> None:
		"""Handle tab closure - update focus if needed."""
		if not self.agent_focus_target_id:
			return

		# Get current tab index
		current_target_id = self.agent_focus_target_id

		# If the closed tab was the current one, find a new target
		if current_target_id == event.target_id:
			await self.event_bus.dispatch(SwitchTabEvent(target_id=None))

	async def on_AgentFocusChangedEvent(self, event: AgentFocusChangedEvent) -> None:
		"""Handle agent focus change - update focus and clear cache."""
		self.logger.debug(f'🔄 AgentFocusChangedEvent received: target_id=...{event.target_id[-4:]} url={event.url}')

		# Clear cached DOM state since focus changed
		if self.watchdogs.dom:
			self.watchdogs.dom.clear_cache()

		# Clear cached browser state
		self.dom_state.clear()
		self.logger.debug('🔄 Cached browser state cleared')

		# Update agent focus if a specific target_id is provided (only for page/tab targets)
		if event.target_id:
			# Ensure session exists and update agent focus (validates target_type internally)
			await self.get_or_create_cdp_session(target_id=event.target_id, focus=True)

			# Apply viewport settings to the newly focused tab
			if self.browser_profile.viewport and not self.browser_profile.no_viewport:
				try:
					viewport_width = self.browser_profile.viewport.width
					viewport_height = self.browser_profile.viewport.height
					device_scale_factor = self.browser_profile.device_scale_factor or 1.0

					# Use the helper method with the current tab's target_id
					await self._cdp_set_viewport(viewport_width, viewport_height, device_scale_factor, target_id=event.target_id)

					self.logger.debug(f'Applied viewport {viewport_width}x{viewport_height} to tab {event.target_id[-8:]}')
				except Exception as e:
					self.logger.warning(f'Failed to set viewport for tab {event.target_id[-8:]}: {e}')
		else:
			raise RuntimeError('AgentFocusChangedEvent received with no target_id for newly focused tab')

	async def on_FileDownloadedEvent(self, event: FileDownloadedEvent) -> None:
		"""Track downloaded files during this session."""
		self.logger.debug(f'FileDownloadedEvent received: {event.file_name} at {event.path}')
		if event.path and event.path not in self._downloaded_files:
			self._downloaded_files.append(event.path)
			self.logger.info(f'📁 Tracked download: {event.file_name} ({len(self._downloaded_files)} total downloads in session)')
		else:
			if not event.path:
				self.logger.warning(f'FileDownloadedEvent has no path: {event}')
			else:
				self.logger.debug(f'File already tracked: {event.path}')

	def _cloud_session_id_from_cdp_url(self) -> str | None:
		"""Derive cloud browser session ID from a Browser Use CDP URL."""
		if not self.cdp_url:
			return None
		host = urlparse(self.cdp_url).hostname or ''
		match = re.match(r'^([0-9a-fA-F-]{36})\.cdp\d+\.browser-use\.com$', host)
		return match.group(1) if match else None

	async def on_BrowserStopEvent(self, event: BrowserStopEvent) -> None:
		"""Handle browser stop request."""

		try:
			# Check if we should keep the browser alive
			if self.browser_profile.keep_alive and not event.force:
				self.event_bus.dispatch(BrowserStoppedEvent(reason='Kept alive due to keep_alive=True'))
				return

			# Clean up cloud browser session for both:
			# 1) native use_cloud sessions (current_session_id set by create_browser)
			# 2) reconnected cdp_url sessions (derive UUID from host)
			cloud_session_id = self._cloud_browser_client.current_session_id or self._cloud_session_id_from_cdp_url()
			if cloud_session_id:
				try:
					await self._cloud_browser_client.stop_browser(cloud_session_id)
					self.logger.info(f'🌤️ Cloud browser session cleaned up: {cloud_session_id}')
				except Exception as e:
					self.logger.debug(f'Failed to cleanup cloud browser session {cloud_session_id}: {e}')
				finally:
					# Always close the httpx client to free connection pool memory
					try:
						await self._cloud_browser_client.close()
					except Exception:
						pass

			# Public stop()/kill() reset only after every stop handler completes,
			# keeping artifact finalization and process cleanup ahead of CDP teardown.
			# LocalBrowserWatchdog listens for BrowserStopEvent and dispatches BrowserKillEvent
			stop_event = self.event_bus.dispatch(BrowserStoppedEvent(reason='Stopped by request'))
			await stop_event

		except Exception as e:
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='BrowserStopEventError',
					message=f'Failed to stop browser: {type(e).__name__} {e}',
					details={'cdp_url': self.cdp_url, 'is_local': self.is_local},
				)
			)

	# region - ========== CDP-based replacements for browser_context operations ==========
	@property
	def cdp_client(self) -> CDPClient:
		"""Get the cached root CDP cdp_session.cdp_client. The client is created and started in self.connect()."""
		assert self._cdp_client_root is not None, 'CDP client not initialized - browser may not be connected yet'
		return self._cdp_client_root

	async def new_page(self, url: str | None = None) -> Page:
		"""Create a new page, raising if a policy-blocked target cannot be confirmed closed."""

		# Import here to avoid circular import
		from browser_use.actor.page import Page as Target

		if not self.session_manager.url_policy_active:
			params: CreateTargetParameters = {'url': url or 'about:blank'}
			result = await self.cdp_client.send.Target.createTarget(params)
			return Target(self, result['targetId'])

		# Target.createTarget(url=...) may begin the initial navigation before the
		# auto-attached page session has Fetch interception installed. Under policy,
		# create only an inert page and do not navigate until the manager confirms
		# that this target's top-frame Fetch owner is ready.
		result = await self.cdp_client.send.Target.createTarget({'url': 'about:blank'})
		target_id = result['targetId']
		self.session_manager.mark_new_page_target(target_id)
		cdp_session = await self.session_manager.wait_for_policy_ready_page(target_id)
		page = Target(self, target_id, session_id=cdp_session.session_id)

		if url is None or url == 'about:blank':
			return page

		if not self.session_manager._is_url_allowed(url):
			await self.session_manager._remediate_blocked_navigation(
				target_id,
				url,
				require_target_closed=True,
			)
			return page

		# Page.goto intentionally keeps policy rejection event-driven. The Fetch
		# router may close this newly created target if an allowed URL redirects to
		# a disallowed destination; callers still receive the Page handle.
		await page.goto(url)
		return page

	async def get_current_page(self) -> Page | None:
		"""Get the current page as an actor Page."""
		if not self.agent_focus_target_id:
			return None

		from browser_use.actor.page import Page as Target

		return Target(self, self.agent_focus_target_id)

	async def must_get_current_page(self) -> Page:
		"""Get the current page as an actor Page."""
		page = await self.get_current_page()
		if not page:
			raise RuntimeError('No current target found')

		return page

	async def get_pages(self) -> list[Page]:
		"""Get all available pages using SessionManager (source of truth)."""
		# Import here to avoid circular import
		from browser_use.actor.page import Page as PageActor

		page_targets = self.session_manager.get_all_page_targets() if self.session_manager else []

		targets = []
		for target in page_targets:
			targets.append(PageActor(self, target.target_id))

		return targets

	def get_focused_target(self) -> Target | None:
		"""Get the target that currently has agent focus.

		Returns:
			Target object if agent has focus, None otherwise.
		"""
		if not self.session_manager:
			return None
		return self.session_manager.get_focused_target()

	def get_page_targets(self) -> list[Target]:
		"""Get all page/tab targets (excludes iframes, workers, etc.).

		Returns:
			List of Target objects for all page/tab targets.
		"""
		if not self.session_manager:
			return []
		return self.session_manager.get_all_page_targets()

	async def close_page(self, page: Page | str) -> None:
		"""Close a page by Page object or target ID."""
		from cdp_use.cdp.target.commands import CloseTargetParameters

		# Import here to avoid circular import
		from browser_use.actor.page import Page as Target

		if isinstance(page, Target):
			target_id = page._target_id
		else:
			target_id = str(page)

		params: CloseTargetParameters = {'targetId': target_id}
		await self.cdp_client.send.Target.closeTarget(params)

	async def cookies(self) -> list[Cookie]:
		"""Get cookies, optionally filtered by URLs."""

		result = await self.cdp_client.send.Storage.getCookies()
		return result['cookies']

	async def clear_cookies(self) -> None:
		"""Clear all cookies."""
		await self.cdp_client.send.Network.clearBrowserCookies()

	async def export_storage_state(self, output_path: str | Path | None = None) -> dict[str, Any]:
		"""Export all browser cookies and storage to storage_state format.

		Extracts decrypted cookies via CDP, bypassing keychain encryption.

		Args:
			output_path: Optional path to save storage_state.json. If None, returns dict only.

		Returns:
			Storage state dict with cookies in Playwright format.

		"""
		from pathlib import Path

		# Get all cookies using Storage.getCookies (returns decrypted cookies from all domains)
		cookies = await self._cdp_get_cookies()

		# Convert CDP cookie format to Playwright storage_state format
		storage_state = {
			'cookies': [
				{
					'name': c['name'],
					'value': c['value'],
					'domain': c['domain'],
					'path': c['path'],
					'expires': c.get('expires', -1),
					'httpOnly': c.get('httpOnly', False),
					'secure': c.get('secure', False),
					'sameSite': c.get('sameSite', 'Lax'),
				}
				for c in cookies
			],
			'origins': [],  # Could add localStorage/sessionStorage extraction if needed
		}

		if output_path:
			import json

			output_file = Path(output_path).expanduser().resolve()
			output_file.parent.mkdir(parents=True, exist_ok=True)
			output_file.write_text(json.dumps(storage_state, indent=2, ensure_ascii=False), encoding='utf-8')
			self.logger.info(f'💾 Exported {len(cookies)} cookies to {output_file}')

		return storage_state

	async def get_or_create_cdp_session(self, target_id: TargetID | None = None, focus: bool = True) -> CDPSession:
		"""Get CDP session for a target from the event-driven pool.

		With autoAttach=True, sessions are created automatically by Chrome and added
		to the pool via Target.attachedToTarget events. This method retrieves them.

		Args:
			target_id: Target ID to get session for. If None, uses current agent focus.
			focus: If True, switches agent focus to this target (page targets only).

		Returns:
			CDPSession for the specified target.

		Raises:
			ValueError: If target doesn't exist or session is not available.
		"""
		assert self._cdp_client_root is not None, 'Root CDP client not initialized'
		assert self.session_manager is not None, 'SessionManager not initialized'

		# If no target_id specified, ensure current agent focus is valid and wait for recovery if needed
		if target_id is None:
			# Validate and wait for focus recovery if stale (centralized protection)
			focus_valid = await self.session_manager.ensure_valid_focus(timeout=5.0)
			if not focus_valid:
				raise ValueError(
					'No valid agent focus available - target may have detached and recovery failed. '
					'This indicates browser is in an unstable state.'
				)

			assert self.agent_focus_target_id is not None, 'Focus validation passed but agent_focus_target_id is None'
			target_id = self.agent_focus_target_id

		session = self.session_manager._get_session_for_target(target_id)

		if not session:
			# Session not in pool yet - wait for attach event
			self.logger.debug(f'[SessionManager] Waiting for target {target_id[:8]}... to attach...')

			# Wait up to 2 seconds for the attach event
			for attempt in range(20):
				await asyncio.sleep(0.1)
				session = self.session_manager._get_session_for_target(target_id)
				if session:
					self.logger.debug(f'[SessionManager] Target appeared after {attempt * 100}ms')
					break

			if not session:
				# Timeout - target doesn't exist
				raise ValueError(f'Target {target_id} not found - may have detached or never existed')

		# Validate session is still active
		is_valid = await self.session_manager.validate_session(target_id)
		if not is_valid:
			raise ValueError(f'Target {target_id} has detached - no active sessions')

		# Update focus if requested
		# CRITICAL: Only allow focus change to 'page' type targets, not iframes/workers
		if focus and self.agent_focus_target_id != target_id:
			# Get target type from SessionManager
			target = self.session_manager.get_target(target_id)
			target_type = target.target_type if target else 'unknown'

			if target_type == 'page':
				# Format current focus safely (could be None after detach)
				current_focus = self.agent_focus_target_id[:8] if self.agent_focus_target_id else 'None'
				self.logger.debug(f'[SessionManager] Switching focus: {current_focus}... → {target_id[:8]}...')
				self.agent_focus_target_id = target_id
			else:
				# Ignore focus request for non-page targets (iframes, workers, etc.)
				# These can detach at any time, causing agent_focus to point to dead target
				current_focus = self.agent_focus_target_id[:8] if self.agent_focus_target_id else 'None'
				self.logger.debug(
					f'[SessionManager] Ignoring focus request for {target_type} target {target_id[:8]}... '
					f'(agent_focus stays on {current_focus}...)'
				)

		# Resume if waiting for debugger (non-essential, don't let it block connect)
		if focus:
			try:
				await asyncio.wait_for(
					session.cdp_client.send.Runtime.runIfWaitingForDebugger(session_id=session.session_id),
					timeout=3.0,
				)
			except Exception:
				pass  # May fail if not waiting, or timeout — either is fine

		return session

	async def set_extra_headers(self, headers: dict[str, str], target_id: TargetID | None = None) -> None:
		"""Set extra HTTP headers using CDP Network.setExtraHTTPHeaders.

		These headers will be sent with every HTTP request made by the target.
		Network domain must be enabled first (done automatically for page targets
		in SessionManager._enable_page_monitoring).

		Args:
			headers: Dictionary of header name -> value pairs to inject into every request.
			target_id: Target to set headers on. Defaults to the current agent focus target.
		"""
		if target_id is None:
			if not self.agent_focus_target_id:
				return
			target_id = self.agent_focus_target_id

		cdp_session = await self.get_or_create_cdp_session(target_id, focus=False)
		# Ensure Network domain is enabled (idempotent - safe to call multiple times)
		await cdp_session.cdp_client.send.Network.enable(session_id=cdp_session.session_id)
		await cdp_session.cdp_client.send.Network.setExtraHTTPHeaders(
			params={'headers': cast(Any, headers)}, session_id=cdp_session.session_id
		)

	# endregion - ========== CDP-based ... ==========

	# region - ========== Helper Methods ==========
	async def get_browser_state_summary(
		self,
		include_screenshot: bool = True,
		cached: bool = False,
		include_recent_events: bool = False,
	) -> BrowserStateSummary:
		if (
			cached
			and self.dom_state.cached_browser_state_summary is not None
			and self.dom_state.cached_browser_state_summary.dom_state
		):
			# Don't use cached state if it has 0 interactive elements
			selector_map = self.dom_state.cached_browser_state_summary.dom_state.selector_map

			# Don't use cached state if we need a screenshot but the cached state doesn't have one
			if include_screenshot and not self.dom_state.cached_browser_state_summary.screenshot:
				self.logger.debug('⚠️ Cached browser state has no screenshot, fetching fresh state with screenshot')
				# Fall through to fetch fresh state with screenshot
			elif selector_map and len(selector_map) > 0:
				self.logger.debug('🔄 Using pre-cached browser state summary for open tab')
				return self.dom_state.cached_browser_state_summary
			else:
				self.logger.debug('⚠️ Cached browser state has 0 interactive elements, fetching fresh state')
				# Fall through to fetch fresh state

		# Dispatch the event and wait for result
		event: BrowserStateRequestEvent = cast(
			BrowserStateRequestEvent,
			self.event_bus.dispatch(
				BrowserStateRequestEvent(
					include_dom=True,
					include_screenshot=include_screenshot,
					include_recent_events=include_recent_events,
				)
			),
		)

		# The handler returns the BrowserStateSummary directly. If the complete state
		# request times out, return a non-actionable state so the model can recover
		# without exposing selectors from an earlier page.
		try:
			result = await event.event_result(raise_if_none=True, raise_if_any=True)
		except TimeoutError:
			state_error = (
				'Browser state capture timed out. The current DOM and screenshot are unavailable, '
				'so no element indices are safe to use. Recover with navigation, waiting, or another non-indexed action.'
			)
			empty_dom_state = SerializedDOMState(_root=None, selector_map={})

			# Clear every action lookup path before calling the model.
			self.dom_state.update_cached_selector_map({})
			if self.watchdogs.dom is not None:
				self.watchdogs.dom.clear_cache()

			cached_state = self.dom_state.cached_browser_state_summary
			current_target = (
				self.session_manager.get_target(self.agent_focus_target_id)
				if self.session_manager is not None and self.agent_focus_target_id is not None
				else None
			)
			url = (
				current_target.url
				if current_target and current_target.url
				else cached_state.url
				if cached_state
				else 'about:blank'
			)
			title = (
				current_target.title
				if current_target and current_target.title
				else cached_state.title
				if cached_state
				else 'Browser state unavailable'
			)
			tabs = [TabInfo(url=url, title=title, target_id=current_target.target_id)] if current_target else []

			result = BrowserStateSummary(
				dom_state=empty_dom_state,
				url=url,
				title=title,
				tabs=tabs,
				screenshot=None,
				browser_errors=[state_error],
				state_error=state_error,
			)
			self.dom_state.cached_browser_state_summary = result

		assert result is not None and result.dom_state is not None
		return result

	async def connect(self, cdp_url: str | None = None) -> Self:
		"""Connect to a remote chromium-based browser via CDP using cdp-use.

		This MUST succeed or the browser is unusable. Fails hard on any error.
		"""

		self.browser_profile.cdp_url = cdp_url or self.cdp_url
		if not self.cdp_url:
			raise RuntimeError('Cannot setup CDP connection without CDP URL')

		# Prevent duplicate connections - clean up existing connection first
		if self._cdp_client_root is not None:
			self.logger.warning(
				'⚠️ connect() called but CDP client already exists! Cleaning up old connection before creating new one.'
			)
			old_cdp_client = self._cdp_client_root
			was_intentional_stop = self._intentional_stop
			self._intentional_stop = True
			self._cdp_client_root = None
			self.agent_focus_target_id = None
			try:
				await old_cdp_client.stop()
			except Exception as e:
				self.logger.debug(f'Error stopping old CDP client: {e}')
			finally:
				# Every pooled session belongs to the retired root client. Clear the
				# connection generation before monitoring the replacement client.
				await self.session_manager.clear()
				self._intentional_stop = was_intentional_stop

		if not self.cdp_url.startswith('ws'):
			# If it's an HTTP URL, fetch the WebSocket URL from /json/version endpoint
			parsed_url = urlparse(self.cdp_url)
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
				headers = dict(self.browser_profile.headers or {})
				from browser_use.version import get_browser_use_version

				headers.setdefault('User-Agent', f'browser-use/{get_browser_use_version()}')
				version_info = await client.get(url, headers=headers)
				self.logger.debug(f'Raw version info: {str(version_info)}')
				self.browser_profile.cdp_url = version_info.json()['webSocketDebuggerUrl']

		assert self.cdp_url is not None, 'CDP URL is None.'

		browser_location = 'local browser' if self.is_local else 'remote browser'
		self.logger.debug(f'🌎 Connecting to existing chromium-based browser via CDP: {self.cdp_url} -> ({browser_location})')

		try:
			# Create and store the CDP client for direct CDP communication
			headers = dict(getattr(self.browser_profile, 'headers', None) or {})
			if not self.is_local:
				from browser_use.version import get_browser_use_version

				headers.setdefault('User-Agent', f'browser-use/{get_browser_use_version()}')
			self._cdp_client_root = TimeoutWrappedCDPClient(
				self.cdp_url,
				additional_headers=headers or None,
				max_ws_frame_size=200 * 1024 * 1024,  # Use 200MB limit to handle pages with very large DOMs
			)
			assert self._cdp_client_root is not None
			await self._cdp_client_root.start()

			# Initialize event-driven session manager FIRST (before enabling autoAttach)
			# SessionManager will:
			# 1. Register attach/detach event handlers
			# 2. Discover and attach to all existing targets
			# 3. Initialize sessions and enable lifecycle monitoring
			# 4. Enable autoAttach for future targets
			await self.session_manager.start_monitoring()
			self.logger.debug('Event-driven session manager started')

			# Enable auto-attach so Chrome automatically notifies us when NEW targets attach/detach
			# This is the foundation of event-driven session management
			await self._cdp_client_root.send.Target.setAutoAttach(
				params={
					'autoAttach': True,
					'waitForDebuggerOnStart': self.session_manager.url_policy_active,
					'flatten': True,
				}
			)
			self.logger.debug('CDP client connected with auto-attach enabled')

			# Get browser targets from SessionManager (source of truth)
			# SessionManager has already discovered all targets via start_monitoring()
			page_targets_from_manager = self.session_manager.get_all_page_targets()

			# Check for chrome://newtab pages and redirect them to about:blank (in parallel)
			from browser_use.security import is_new_tab_page

			async def _redirect_newtab(target):
				target_url = target.url
				target_id = target.target_id
				self.logger.debug(f'🔄 Redirecting {target_url} to about:blank for target {target_id}')
				try:
					session = await self.get_or_create_cdp_session(target_id, focus=False)
					await session.cdp_client.send.Page.navigate(params={'url': 'about:blank'}, session_id=session.session_id)
					target.url = 'about:blank'
				except Exception as e:
					self.logger.warning(f'Failed to redirect {target_url}: {e}')

			redirect_tasks = [
				_redirect_newtab(target)
				for target in page_targets_from_manager
				if is_new_tab_page(target.url) and target.url != 'about:blank'
			]
			if redirect_tasks:
				await asyncio.gather(*redirect_tasks, return_exceptions=True)

			# Ensure we have at least one page
			if not page_targets_from_manager:
				new_target = await self._cdp_client_root.send.Target.createTarget(params={'url': 'about:blank'})
				target_id = new_target['targetId']
				self.logger.debug(f'📄 Created new blank page: {target_id}')
			else:
				target_id = page_targets_from_manager[0].target_id
				self.logger.debug(f'📄 Using existing page: {target_id}')

			# Set up initial focus using the public API
			# Note: get_or_create_cdp_session() will wait for attach event and set focus
			try:
				await self.get_or_create_cdp_session(target_id, focus=True)
				# agent_focus_target_id is now set by get_or_create_cdp_session
				self.logger.debug(f'📄 Agent focus set to {target_id[:8]}...')
			except ValueError as e:
				raise RuntimeError(f'Failed to get session for initial target {target_id}: {e}') from e

			# Note: Lifecycle monitoring is enabled automatically in SessionManager._handle_target_attached()
			# when targets attach, so no manual enablement needed!

			# Enable proxy authentication handling if configured
			await self._setup_proxy_auth()

			# Attach WS drop detection callback for auto-reconnection
			self._intentional_stop = False
			self._attach_ws_drop_callback()

			# Verify the target is working
			if self.agent_focus_target_id:
				target = self.session_manager.get_target(self.agent_focus_target_id)
				if target.title == 'Unknown title':
					self.logger.warning('Target created but title is unknown (may be normal for about:blank)')

			# Dispatch TabCreatedEvent for all initial tabs (so watchdogs can initialize)
			for idx, target in enumerate(page_targets_from_manager):
				target_url = target.url
				self.logger.debug(f'Dispatching TabCreatedEvent for initial tab {idx}: {target_url}')
				self.event_bus.dispatch(TabCreatedEvent(url=target_url, target_id=target.target_id))

			# Dispatch initial focus event
			if page_targets_from_manager:
				initial_url = page_targets_from_manager[0].url
				self.event_bus.dispatch(AgentFocusChangedEvent(target_id=page_targets_from_manager[0].target_id, url=initial_url))
				self.logger.debug(f'Initial agent focus set to tab 0: {initial_url}')

		except Exception as e:
			# Fatal error - browser is not usable without CDP connection
			self.logger.error(f'❌ FATAL: Failed to setup CDP connection: {e}')
			self.logger.error('❌ Browser cannot continue without CDP connection')

			# Clear SessionManager state
			if self.session_manager:
				try:
					await self.session_manager.clear()
					self.logger.debug('Cleared SessionManager state after initialization failure')
				except Exception as cleanup_error:
					self.logger.debug(f'Error clearing SessionManager: {cleanup_error}')

			# Close CDP client WebSocket and unregister handlers
			if self._cdp_client_root:
				try:
					await self._cdp_client_root.stop()  # Close WebSocket and unregister handlers
					self.logger.debug('Closed CDP client WebSocket after initialization failure')
				except Exception as cleanup_error:
					self.logger.debug(f'Error closing CDP client: {cleanup_error}')

				await self.session_manager.clear()
			self._cdp_client_root = None
			self.agent_focus_target_id = None
			# Re-raise as a fatal error
			raise RuntimeError(f'Failed to establish CDP connection to browser: {e}') from e

		return self

	async def _setup_proxy_auth(self) -> None:
		"""Enable CDP Fetch auth handling for authenticated proxy, if credentials provided.

		Handles HTTP proxy authentication challenges (Basic/Proxy) by providing
		configured credentials from BrowserProfile.
		"""

		assert self._cdp_client_root

		try:
			proxy_cfg = self.browser_profile.proxy
			username = proxy_cfg.username if proxy_cfg else None
			password = proxy_cfg.password if proxy_cfg else None
			if not username or not password:
				self.logger.debug('Proxy credentials not provided; skipping proxy auth setup')
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
						assert self._cdp_client_root
						try:
							await self._cdp_client_root.send.Fetch.continueWithAuth(
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
							self.logger.debug(f'Proxy auth respond failed: {type(e).__name__}: {e}')

					# schedule
					create_task_with_error_handling(
						_respond(), name='auth_respond', logger_instance=self.logger, suppress_exceptions=True
					)
				else:
					# Default behaviour for non-proxy challenges: let browser handle
					async def _default():
						assert self._cdp_client_root
						try:
							await self._cdp_client_root.send.Fetch.continueWithAuth(
								params={'requestId': request_id, 'authChallengeResponse': {'response': 'Default'}},
								session_id=session_id,
							)
						except Exception as e:
							self.logger.debug(f'Default auth respond failed: {type(e).__name__}: {e}')

					if request_id:
						create_task_with_error_handling(
							_default(), name='auth_default', logger_instance=self.logger, suppress_exceptions=True
						)

			# Register event handler on root client
			try:
				self._cdp_client_root.register.Fetch.authRequired(_on_auth_required)
				self.logger.debug('Registered Fetch.authRequired handlers')
			except Exception as e:
				self.logger.debug(f'Failed to register authRequired handlers: {type(e).__name__}: {e}')

		except Exception as e:
			self.logger.debug(f'Skipping proxy auth setup: {type(e).__name__}: {e}')

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
		assert self.cdp_url, 'Cannot reconnect without a CDP URL'

		old_focus_target_id = self.agent_focus_target_id

		# 1. Stop old CDPClient (WS is already dead, this just cleans internal state)
		if self._cdp_client_root:
			try:
				await self._cdp_client_root.stop()
			except Exception as e:
				self.logger.debug(f'Error stopping old CDP client during reconnect: {e}')
			self._cdp_client_root = None

			# 2. Clear SessionManager (all sessions are stale)
			try:
				await self.session_manager.clear()
			except Exception as e:
				self.logger.debug(f'Error clearing SessionManager during reconnect: {e}')

		self.agent_focus_target_id = None

		# 3. Create new CDPClient with the same cdp_url
		headers = dict(getattr(self.browser_profile, 'headers', None) or {})
		if not self.is_local:
			from browser_use.version import get_browser_use_version

			headers.setdefault('User-Agent', f'browser-use/{get_browser_use_version()}')
		self._cdp_client_root = TimeoutWrappedCDPClient(
			self.cdp_url,
			additional_headers=headers or None,
			max_ws_frame_size=200 * 1024 * 1024,
		)
		await self._cdp_client_root.start()

		# 4. Re-initialize SessionManager
		await self.session_manager.start_monitoring()

		# 5. Re-enable autoAttach
		await self._cdp_client_root.send.Target.setAutoAttach(
			params={
				'autoAttach': True,
				'waitForDebuggerOnStart': self.session_manager.url_policy_active,
				'flatten': True,
			}
		)

		# 6. Re-discover page targets and restore focus
		page_targets = self.session_manager.get_all_page_targets()

		# Prefer the old focus target if it still exists
		restored = False
		if old_focus_target_id:
			for target in page_targets:
				if target.target_id == old_focus_target_id:
					await self.get_or_create_cdp_session(old_focus_target_id, focus=True)
					restored = True
					self.logger.debug(f'🔄 Restored agent focus to previous target {old_focus_target_id[:8]}...')
					break

		if not restored:
			if page_targets:
				fallback_id = page_targets[0].target_id
				await self.get_or_create_cdp_session(fallback_id, focus=True)
				self.logger.debug(f'🔄 Agent focus set to fallback target {fallback_id[:8]}...')
			else:
				# No pages exist — create one
				new_target = await self._cdp_client_root.send.Target.createTarget(params={'url': 'about:blank'})
				target_id = new_target['targetId']
				await self.get_or_create_cdp_session(target_id, focus=True)
				self.logger.debug(f'🔄 Created new blank page during reconnect: {target_id[:8]}...')

		# 7. Re-enable proxy auth if configured
		await self._setup_proxy_auth()

		# 8. Attach the WS drop detection callback to the new client
		self._attach_ws_drop_callback()

	async def _auto_reconnect(self, max_attempts: int = 3) -> None:
		"""Attempt to reconnect with exponential backoff.

		Dispatches BrowserReconnectingEvent before each attempt and
		BrowserReconnectedEvent on success.
		"""
		async with self._reconnect_lock:
			if self._reconnecting:
				return  # already in progress from another caller
			self._reconnecting = True
			self._reconnect_event.clear()

		start_time = time.time()
		delays = [1.0, 2.0, 4.0]

		try:
			for attempt in range(1, max_attempts + 1):
				self.event_bus.dispatch(
					BrowserReconnectingEvent(
						cdp_url=self.cdp_url or '',
						attempt=attempt,
						max_attempts=max_attempts,
					)
				)
				self.logger.warning(f'🔄 WebSocket reconnection attempt {attempt}/{max_attempts}...')

				try:
					await asyncio.wait_for(self.reconnect(), timeout=15.0)
					# Success
					downtime = time.time() - start_time
					self.event_bus.dispatch(
						BrowserReconnectedEvent(
							cdp_url=self.cdp_url or '',
							attempt=attempt,
							downtime_seconds=downtime,
						)
					)
					self.logger.info(f'🔄 WebSocket reconnected after {downtime:.1f}s (attempt {attempt})')
					return
				except Exception as e:
					self.logger.warning(f'🔄 Reconnection attempt {attempt} failed: {type(e).__name__}: {e}')
					if attempt < max_attempts:
						delay = delays[attempt - 1] if attempt - 1 < len(delays) else delays[-1]
						await asyncio.sleep(delay)

			# All attempts exhausted
			self.logger.error(f'🔄 All {max_attempts} reconnection attempts failed')
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='ReconnectionFailed',
					message=f'Failed to reconnect after {max_attempts} attempts ({time.time() - start_time:.1f}s)',
					details={'cdp_url': self.cdp_url or '', 'max_attempts': max_attempts},
				)
			)
		finally:
			self._reconnecting = False
			self._reconnect_event.set()  # wake up all waiters regardless of outcome

	def _attach_ws_drop_callback(self) -> None:
		"""Attach a done callback to the CDPClient's message handler task to detect WS drops."""
		if not self._cdp_client_root or not hasattr(self._cdp_client_root, '_message_handler_task'):
			return

		cdp_client = self._cdp_client_root
		task = cdp_client._message_handler_task
		if task is None or task.done():
			return

		def _on_message_handler_done(fut: asyncio.Future) -> None:
			# Ignore callbacks from a connection generation that has already been replaced.
			if self._cdp_client_root is not cdp_client:
				return

			# Guard: skip if intentionally stopped, already reconnecting, or no cdp_url
			if self._intentional_stop or self._reconnecting or not self.cdp_url:
				return

			# The message handler task exiting means the WS connection dropped
			exc = fut.exception() if not fut.cancelled() else None
			self.logger.warning(
				f'🔌 CDP WebSocket message handler exited unexpectedly'
				f'{f": {type(exc).__name__}: {exc}" if exc else " (connection closed)"}'
			)

			# Fire auto-reconnect as an asyncio task
			try:
				loop = asyncio.get_running_loop()
				self._reconnect_task = loop.create_task(self._auto_reconnect())
			except RuntimeError:
				# No running event loop — can't reconnect
				self.logger.error('🔌 No event loop available for auto-reconnect')

		task.add_done_callback(_on_message_handler_done)

	async def get_tabs(self) -> list[TabInfo]:
		"""Get information about all open tabs using cached target data."""
		tabs = []

		# Safety check - return empty list if browser not connected yet
		if not self.session_manager:
			return tabs

		# Get all page targets from SessionManager
		page_targets = self.session_manager.get_all_page_targets()

		for i, target in enumerate(page_targets):
			target_id = target.target_id
			url = target.url
			title = target.title

			try:
				# Skip JS execution for chrome:// pages and new tab pages
				if is_new_tab_page(url) or url.startswith('chrome://'):
					# Use URL as title for chrome pages, or mark new tabs as unusable
					if is_new_tab_page(url):
						title = ''
					elif not title:
						# For chrome:// pages without a title, use the URL itself
						title = url

				# Special handling for PDF pages without titles
				if (not title or title == '') and (url.endswith('.pdf') or 'pdf' in url):
					# PDF pages might not have a title, use URL filename
					try:
						from urllib.parse import urlparse

						filename = urlparse(url).path.split('/')[-1]
						if filename:
							title = filename
					except Exception:
						pass

			except Exception as e:
				# Fallback to basic title handling
				self.logger.debug(f'⚠️ Failed to get target info for tab #{i}: {log_pretty_url(url)} - {type(e).__name__}')

				if is_new_tab_page(url):
					title = ''
				elif url.startswith('chrome://'):
					title = url
				else:
					title = ''

			tab_info = TabInfo(
				target_id=target_id,
				url=url,
				title=title,
				parent_target_id=None,
			)
			tabs.append(tab_info)

		return tabs

	# endregion - ========== Helper Methods ==========

	# region - ========== ID Lookup Methods ==========

	async def get_current_page_url(self) -> str:
		"""Get the URL of the current page."""
		if self.agent_focus_target_id:
			target = self.session_manager.get_target(self.agent_focus_target_id)
			return target.url
		return 'about:blank'

	async def get_current_page_title(self) -> str:
		"""Get the title of the current page."""
		if self.agent_focus_target_id:
			target = self.session_manager.get_target(self.agent_focus_target_id)
			return target.title
		return 'Unknown page title'

	async def navigate_to(self, url: str, new_tab: bool = False) -> None:
		"""Navigate to a URL using the standard event system.

		Args:
			url: URL to navigate to
			new_tab: Whether to open in a new tab
		"""
		from browser_use.browser.events import NavigateToUrlEvent

		event = self.event_bus.dispatch(NavigateToUrlEvent(url=url, new_tab=new_tab))
		await event
		await event.event_result(raise_if_any=True, raise_if_none=False)

	# endregion - ========== ID Lookup Methods ==========

	# region - ========== DOM Helper Methods ==========

	async def _close_extension_options_pages(self) -> None:
		"""Close any extension options/welcome pages that have opened."""
		try:
			# Get all page targets from SessionManager
			page_targets = self.session_manager.get_all_page_targets()

			for target in page_targets:
				target_url = target.url
				target_id = target.target_id

				# Check if this is an extension options/welcome page
				if 'chrome-extension://' in target_url and (
					'options.html' in target_url or 'welcome.html' in target_url or 'onboarding.html' in target_url
				):
					self.logger.info(f'[BrowserSession] 🚫 Closing extension options page: {target_url}')
					try:
						await self._cdp_close_page(target_id)
					except Exception as e:
						self.logger.debug(f'[BrowserSession] Could not close extension page {target_id}: {e}')

		except Exception as e:
			self.logger.debug(f'[BrowserSession] Error closing extension options pages: {e}')

	async def send_demo_mode_log(self, message: str, level: str = 'info', metadata: dict[str, Any] | None = None) -> None:
		"""Send a message to the in-browser demo panel if enabled."""
		if not self.browser_profile.demo_mode:
			return
		demo = self.demo_mode
		if not demo:
			return
		try:
			await demo.send_log(message=message, level=level, metadata=metadata or {})
		except Exception as exc:
			self.logger.debug(f'[DemoMode] Failed to send log: {exc}')

	@property
	def downloaded_files(self) -> list[str]:
		"""Get list of files downloaded during this browser session.

		Returns:
			list[str]: List of absolute file paths to downloaded files in this session
		"""
		return self._downloaded_files.copy()

	# endregion - ========== Helper Methods ==========

	# region - ========== CDP-based replacements for browser_context operations ==========

	async def _cdp_create_new_page(self, url: str = 'about:blank', background: bool = False, new_window: bool = False) -> str:
		"""Create a new page/tab using CDP Target.createTarget. Returns target ID."""
		# Only include newWindow when True, letting Chrome auto-create window as needed
		params = CreateTargetParameters(url=url, background=background)
		if new_window:
			params['newWindow'] = True
		# Use the root CDP client to create tabs at the browser level
		if self._cdp_client_root:
			result = await self._cdp_client_root.send.Target.createTarget(params=params)
		else:
			# Fallback to using cdp_client if root is not available
			result = await self.cdp_client.send.Target.createTarget(params=params)
		return result['targetId']

	async def _cdp_close_page(self, target_id: TargetID) -> None:
		"""Close a page/tab using CDP Target.closeTarget."""
		await self.cdp_client.send.Target.closeTarget(params={'targetId': target_id})

	async def _cdp_get_cookies(self) -> list[Cookie]:
		"""Get cookies using CDP Network.getCookies."""
		cdp_session = await self.get_or_create_cdp_session(target_id=None)
		result = await asyncio.wait_for(
			cdp_session.cdp_client.send.Storage.getCookies(session_id=cdp_session.session_id), timeout=8.0
		)
		return result.get('cookies', [])

	async def _cdp_set_cookies(self, cookies: list[Cookie]) -> None:
		"""Set cookies using CDP Storage.setCookies."""
		if not self.agent_focus_target_id or not cookies:
			return

		cdp_session = await self.get_or_create_cdp_session(target_id=None)
		# Storage.setCookies expects params dict with 'cookies' key
		await cdp_session.cdp_client.send.Storage.setCookies(
			params={'cookies': cookies},  # type: ignore[arg-type]
			session_id=cdp_session.session_id,
		)

	async def _cdp_clear_cookies(self) -> None:
		"""Clear all cookies using CDP Network.clearBrowserCookies."""
		cdp_session = await self.get_or_create_cdp_session()
		await cdp_session.cdp_client.send.Storage.clearCookies(session_id=cdp_session.session_id)

	async def _cdp_set_geolocation(self, latitude: float, longitude: float, accuracy: float = 100) -> None:
		"""Set geolocation using CDP Emulation.setGeolocationOverride."""
		await self.cdp_client.send.Emulation.setGeolocationOverride(
			params={'latitude': latitude, 'longitude': longitude, 'accuracy': accuracy}
		)

	async def _cdp_clear_geolocation(self) -> None:
		"""Clear geolocation override using CDP."""
		await self.cdp_client.send.Emulation.clearGeolocationOverride()

	async def _cdp_add_init_script(self, script: str) -> str:
		"""Add script to evaluate on new document using CDP Page.addScriptToEvaluateOnNewDocument."""
		assert self._cdp_client_root is not None
		cdp_session = await self.get_or_create_cdp_session()

		result = await cdp_session.cdp_client.send.Page.addScriptToEvaluateOnNewDocument(
			params={'source': script, 'runImmediately': True}, session_id=cdp_session.session_id
		)
		return result['identifier']

	async def _cdp_remove_init_script(self, identifier: str) -> None:
		"""Remove script added with addScriptToEvaluateOnNewDocument."""
		cdp_session = await self.get_or_create_cdp_session(target_id=None)
		await cdp_session.cdp_client.send.Page.removeScriptToEvaluateOnNewDocument(
			params={'identifier': identifier}, session_id=cdp_session.session_id
		)

	async def _cdp_set_viewport(
		self, width: int, height: int, device_scale_factor: float = 1.0, mobile: bool = False, target_id: str | None = None
	) -> None:
		"""Set viewport using CDP Emulation.setDeviceMetricsOverride.

		Args:
			width: Viewport width
			height: Viewport height
			device_scale_factor: Device scale factor (default 1.0)
			mobile: Whether to emulate mobile device (default False)
			target_id: Optional target ID to set viewport for. If not provided, uses agent_focus.
		"""
		if target_id:
			# Set viewport for specific target
			cdp_session = await self.get_or_create_cdp_session(target_id, focus=False)
		elif self.agent_focus_target_id:
			# Use current focus - use safe API with focus=False to avoid changing focus
			try:
				cdp_session = await self.get_or_create_cdp_session(self.agent_focus_target_id, focus=False)
			except ValueError:
				self.logger.warning('Cannot set viewport: focused target has no sessions')
				return
		else:
			self.logger.warning('Cannot set viewport: no target_id provided and agent_focus not initialized')
			return

		await cdp_session.cdp_client.send.Emulation.setDeviceMetricsOverride(
			params={'width': width, 'height': height, 'deviceScaleFactor': device_scale_factor, 'mobile': mobile},
			session_id=cdp_session.session_id,
		)

	async def _cdp_get_origins(self) -> list[dict[str, Any]]:
		"""Get origins with localStorage and sessionStorage using CDP."""
		origins = []
		cdp_session = await self.get_or_create_cdp_session(target_id=None)

		try:
			# Enable DOMStorage domain to track storage
			await cdp_session.cdp_client.send.DOMStorage.enable(session_id=cdp_session.session_id)

			try:
				# Get all frames to find unique origins
				frames_result = await cdp_session.cdp_client.send.Page.getFrameTree(session_id=cdp_session.session_id)

				# Extract unique origins from frames
				unique_origins = set()

				def _extract_origins(frame_tree):
					"""Recursively extract origins from frame tree."""
					frame = frame_tree.get('frame', {})
					origin = frame.get('securityOrigin')
					if origin and origin != 'null':
						unique_origins.add(origin)

					# Process child frames
					for child in frame_tree.get('childFrames', []):
						_extract_origins(child)

				async def _get_storage_items(origin: str, is_local_storage: bool) -> list[dict[str, str]] | None:
					"""Helper to get storage items for an origin."""
					storage_type = 'localStorage' if is_local_storage else 'sessionStorage'
					try:
						result = await cdp_session.cdp_client.send.DOMStorage.getDOMStorageItems(
							params={'storageId': {'securityOrigin': origin, 'isLocalStorage': is_local_storage}},
							session_id=cdp_session.session_id,
						)

						items = []
						for item in result.get('entries', []):
							if len(item) == 2:  # Each item is [key, value]
								items.append({'name': item[0], 'value': item[1]})

						return items if items else None
					except Exception as e:
						self.logger.debug(f'Failed to get {storage_type} for {origin}: {e}')
						return None

				_extract_origins(frames_result.get('frameTree', {}))

				# For each unique origin, get localStorage and sessionStorage
				for origin in unique_origins:
					origin_data = {'origin': origin}

					# Get localStorage
					local_storage = await _get_storage_items(origin, is_local_storage=True)
					if local_storage:
						origin_data['localStorage'] = local_storage

					# Get sessionStorage
					session_storage = await _get_storage_items(origin, is_local_storage=False)
					if session_storage:
						origin_data['sessionStorage'] = session_storage

					# Only add origin if it has storage data
					if 'localStorage' in origin_data or 'sessionStorage' in origin_data:
						origins.append(origin_data)

			finally:
				# Always disable DOMStorage tracking when done
				await cdp_session.cdp_client.send.DOMStorage.disable(session_id=cdp_session.session_id)

		except Exception as e:
			self.logger.warning(f'Failed to get origins: {e}')

		return origins

	async def _cdp_get_storage_state(self) -> dict:
		"""Get storage state (cookies, localStorage, sessionStorage) using CDP."""
		# Use the _cdp_get_cookies helper which handles session attachment
		cookies = await self._cdp_get_cookies()

		# Get origins with localStorage/sessionStorage
		origins = await self._cdp_get_origins()

		return {
			'cookies': cookies,
			'origins': origins,
		}

	async def _cdp_navigate(self, url: str, target_id: TargetID | None = None) -> None:
		"""Navigate to URL using CDP Page.navigate."""
		# Use provided target_id or fall back to agent_focus_target_id

		assert self._cdp_client_root is not None, 'CDP client not initialized - browser may not be connected yet'
		assert self.agent_focus_target_id is not None, 'Agent focus not initialized - browser may not be connected yet'

		target_id_to_use = target_id or self.agent_focus_target_id
		cdp_session = await self.get_or_create_cdp_session(target_id_to_use, focus=True)

		# Use helper to navigate on the target
		await cdp_session.cdp_client.send.Page.navigate(params={'url': url}, session_id=cdp_session.session_id)

	async def take_screenshot(
		self,
		path: str | None = None,
		full_page: bool = False,
		format: str = 'png',
		quality: int | None = None,
		clip: dict | None = None,
	) -> bytes:
		"""Take a screenshot using CDP.

		Args:
			path: Optional file path to save screenshot
			full_page: Capture entire scrollable page beyond viewport
			format: Image format ('png', 'jpeg', 'webp')
			quality: Quality 0-100 for JPEG format
			clip: Region to capture {'x': int, 'y': int, 'width': int, 'height': int}

		Returns:
			Screenshot data as bytes
		"""
		import base64

		from cdp_use.cdp.page import CaptureScreenshotParameters

		cdp_session = await self.get_or_create_cdp_session()

		# Build parameters dict explicitly to satisfy TypedDict expectations
		params: CaptureScreenshotParameters = {
			'format': format,
			'captureBeyondViewport': full_page,
		}

		if quality is not None and format == 'jpeg':
			params['quality'] = quality

		if clip:
			params['clip'] = {
				'x': clip['x'],
				'y': clip['y'],
				'width': clip['width'],
				'height': clip['height'],
				'scale': 1,
			}

		params = CaptureScreenshotParameters(**params)

		result = await cdp_session.cdp_client.send.Page.captureScreenshot(params=params, session_id=cdp_session.session_id)

		if not result or 'data' not in result:
			raise Exception('Screenshot failed - no data returned')

		screenshot_data = base64.b64decode(result['data'])

		if path:
			Path(path).write_bytes(screenshot_data)

		return screenshot_data
