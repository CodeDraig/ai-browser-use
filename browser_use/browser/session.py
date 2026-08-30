"""Event-driven browser session."""

from __future__ import annotations

import asyncio
import logging
import re
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, cast, overload
from urllib.parse import urlparse
from uuid import UUID

from bubus import EventBus
from cdp_use import CDPClient
from cdp_use.cdp.network import Cookie
from cdp_use.cdp.target import TargetID
from cdp_use.cdp.target.commands import CreateTargetParameters
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from uuid_extensions import uuid7str

from browser_use.browser.cdp import BrowserCDP
from browser_use.browser.cloud.cloud import CloudBrowserClient

# CDP logging is now handled by setup_logging() in logging_config.py
# It automatically sets CDP logs to the same level as browser_use logs
from browser_use.browser.cloud.views import CreateBrowserRequest, ProxyCountryCode
from browser_use.browser.connection import BrowserConnection
from browser_use.browser.event_bus import ResilientEventBus as _ResilientEventBus
from browser_use.browser.events import (
	AgentFocusChangedEvent,
	BrowserStateRequestEvent,
	CloseTabEvent,
	FileDownloadedEvent,
	NavigateToUrlEvent,
	SwitchTabEvent,
	TabClosedEvent,
	TabCreatedEvent,
)
from browser_use.browser.lifecycle import BrowserLifecycle
from browser_use.browser.navigation import BrowserNavigation
from browser_use.browser.profile import (
	CLOUD_PROXY_UNSET,
	BrowserProfile,
	ProxySettings,
	resolve_browser_profile,
)
from browser_use.browser.views import BrowserStateSummary, TabInfo
from browser_use.browser.watchdogs.registry import WatchdogRegistry
from browser_use.dom.browser_state import BrowserDomState
from browser_use.dom.serialized_state import SerializedDOMState
from browser_use.logging_utils import log_pretty_url
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

	@property
	def cdp(self) -> BrowserCDP:
		"""Raw Chrome DevTools Protocol operations for this session."""
		return self._cdp

	@property
	def navigation(self) -> BrowserNavigation:
		"""Navigation orchestration and readiness tracking for this session."""
		return self._navigation

	@property
	def connection(self) -> BrowserConnection:
		"""CDP connection and reconnection lifecycle for this session."""
		return self._connection

	@property
	def lifecycle(self) -> BrowserLifecycle:
		"""Start and stop lifecycle for this session."""
		return self._lifecycle

	# Main shared event bus for all browser session + all watchdogs
	event_bus: EventBus = Field(default_factory=_ResilientEventBus)

	# Mutable public state - which target has agent focus
	agent_focus_target_id: TargetID | None = None

	# Mutable private state shared between watchdogs
	_cdp_client_root: CDPClient | None = PrivateAttr(default=None)
	_connection_lock: Any = PrivateAttr(default=None)  # asyncio.Lock for preventing concurrent connections

	_session_manager: Any = PrivateAttr()
	_cdp: BrowserCDP = PrivateAttr()
	_navigation: BrowserNavigation = PrivateAttr()
	_connection: BrowserConnection = PrivateAttr()
	_lifecycle: BrowserLifecycle = PrivateAttr()
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
		self._cdp = BrowserCDP(self)
		self._navigation = BrowserNavigation(self)
		self._connection = BrowserConnection(self)
		self._lifecycle = BrowserLifecycle(self)
		self._dom_state = BrowserDomState(self)
		self._watchdogs = WatchdogRegistry(self)
		self._connection_lock = asyncio.Lock()
		# Initialize reconnect event as set (no reconnection pending)
		self._reconnect_event = asyncio.Event()
		self._reconnect_event.set()
		self.lifecycle._attach_core_event_handlers()

	async def start(self) -> None:
		"""Start the browser session."""
		await self.lifecycle.start()

	async def kill(self) -> None:
		"""Kill the browser session and reset all state."""
		await self.lifecycle.kill()

	async def stop(self) -> None:
		"""Disconnect while preserving an owned local browser when possible."""
		await self.lifecycle.stop()

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
				await self.cdp.set_viewport(viewport_width, viewport_height, device_scale_factor, target_id=event.target_id)

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
					await self.cdp.set_viewport(viewport_width, viewport_height, device_scale_factor, target_id=event.target_id)

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

		if not self.session_manager.navigation_policy.active:
			params: CreateTargetParameters = {'url': url or 'about:blank'}
			result = await self.cdp_client.send.Target.createTarget(params)
			return Target(self, result['targetId'])

		# Target.createTarget(url=...) may begin the initial navigation before the
		# auto-attached page session has Fetch interception installed. Under policy,
		# create only an inert page and do not navigate until the manager confirms
		# that this target's top-frame Fetch owner is ready.
		result = await self.cdp_client.send.Target.createTarget({'url': 'about:blank'})
		target_id = result['targetId']
		self.session_manager.navigation_policy.mark_new_page_target(target_id)
		cdp_session = await self.session_manager.navigation_policy.wait_for_policy_ready_page(target_id)
		page = Target(self, target_id, session_id=cdp_session.session_id)

		if url is None or url == 'about:blank':
			return page

		if not self.session_manager.navigation_policy.is_url_allowed(url):
			await self.session_manager.navigation_policy.remediate_blocked_navigation(
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
		cookies = await self.cdp.get_cookies()

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
			focus_valid = await self.session_manager.focus.ensure_valid_focus(timeout=5.0)
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
		"""Connect this session to a Chromium CDP endpoint."""
		await self.connection.connect(cdp_url)
		return self

	async def reconnect(self) -> None:
		"""Reconnect this session to its current Chromium CDP endpoint."""
		await self.connection.reconnect()

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
						await self.cdp.close_page(target_id)
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
