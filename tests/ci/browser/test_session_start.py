"""
Test script for BrowserSession.start() method to ensure proper initialization,
concurrency handling, and error handling.

Tests cover:
- Calling .start() on a session that's already started
- Simultaneously calling .start() from two parallel coroutines
- Calling .start() on a session that's started but has a closed browser connection
- Calling .stop() on a session that hasn't been started yet
"""

import asyncio
import logging
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import psutil
import pytest
from cdp_use import CDPClient

from browser_use.browser.profile import (
	BROWSERUSE_DEFAULT_CHANNEL,
	BrowserChannel,
	BrowserProfile,
)
from browser_use.browser.session import BrowserSession
from browser_use.browser.watchdogs.har_recording_watchdog import HarRecordingWatchdog
from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog
from browser_use.browser.watchdogs.recording_watchdog import RecordingWatchdog
from browser_use.config import get_environment_config

# Set up test logging
logger = logging.getLogger('browser_session_start_tests')
# logger.setLevel(logging.DEBUG)


# run with pytest -k test_user_data_dir_not_allowed_to_corrupt_default_profile


class TestBrowserSessionStart:
	"""Tests for BrowserSession.start() method initialization and concurrency."""

	@pytest.fixture(scope='module')
	async def browser_profile(self):
		"""Create and provide a BrowserProfile with headless mode."""
		profile = BrowserProfile(headless=True, user_data_dir=None, keep_alive=False)
		yield profile

	@pytest.fixture(scope='function')
	async def browser_session(self, browser_profile):
		"""Create a BrowserSession instance without starting it."""
		session = BrowserSession(browser_profile=browser_profile)
		yield session
		await session.kill()

	async def test_start_already_started_session(self, browser_session):
		"""Test calling .start() on a session that's already started."""
		# logger.info('Testing start on already started session')

		# Start the session for the first time
		await browser_session.start()
		assert browser_session._cdp_client_root is not None

		# Start the session again - should return immediately without re-initialization
		await browser_session.start()
		assert browser_session._cdp_client_root is not None

	@pytest.mark.parametrize('lifecycle_method', ['stop', 'kill'])
	async def test_start_after_lifecycle_reset_reconnects_without_duplicate_handlers(
		self, browser_session: BrowserSession, lifecycle_method: str
	):
		"""A renewed bus keeps core handlers and lazily reattaches watchdogs once."""
		from browser_use.browser.events import BrowserStartEvent

		await browser_session.start()
		original_bus = browser_session.event_bus
		await getattr(browser_session, lifecycle_method)()

		assert browser_session.event_bus is not original_bus
		assert len(browser_session.event_bus.handlers.get(BrowserStartEvent.__name__, [])) == 1
		assert browser_session.watchdogs._attached is False

		await browser_session.start()
		assert browser_session._cdp_client_root is not None
		assert browser_session.watchdogs._attached is True
		handler_counts = {event_type: len(handlers) for event_type, handlers in browser_session.event_bus.handlers.items()}

		await browser_session.start()
		assert {
			event_type: len(handlers) for event_type, handlers in browser_session.event_bus.handlers.items()
		} == handler_counts

	async def test_connect_replaces_existing_client_and_sessions(self, browser_session):
		"""Repeated connect must not retain sessions bound to the retired CDP client."""
		await browser_session.start()
		old_cdp_client = browser_session._cdp_client_root
		old_session_ids = set(browser_session.session_manager.get_all_sessions())

		await browser_session.connect(browser_session.cdp_url)

		new_cdp_client = browser_session._cdp_client_root
		new_sessions = browser_session.session_manager.get_all_sessions()
		assert new_cdp_client is not None
		assert new_cdp_client is not old_cdp_client
		assert old_session_ids.isdisjoint(new_sessions)
		assert new_sessions
		assert all(session.cdp_client is new_cdp_client for session in new_sessions.values())

	@pytest.mark.parametrize('keep_alive', [False, True])
	async def test_stop_preserves_owned_process_cdp_endpoint_and_tabs(self, keep_alive: bool):
		"""Explicit stop disconnects; start reuses the exact owned Chromium."""
		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=True,
				user_data_dir=None,
				keep_alive=keep_alive,
				enable_default_extensions=False,
				captcha_solver=False,
			)
		)
		pid = -1
		try:
			await session.start()
			watchdog = session.watchdogs.local_browser
			assert watchdog is not None
			assert watchdog._subprocess is not None
			pid = watchdog._subprocess.pid
			cdp_url = session.cdp_url
			page = await session.must_get_current_page()
			await page.goto('data:text/html,<title>preserved</title><p>same tab</p>')
			await asyncio.sleep(0.1)
			target_ids = {target.target_id for target in session.session_manager.get_all_page_targets()}

			await session.stop()

			assert session._cdp_client_root is None
			assert session.cdp_url == cdp_url
			assert psutil.pid_exists(pid)
			assert session.watchdogs.local_browser is watchdog
			assert watchdog._subprocess is not None and watchdog._subprocess.pid == pid
			assert session.watchdogs._local_browser_attached is True

			await session.start()

			assert session.cdp_url == cdp_url
			assert session.watchdogs.local_browser is watchdog
			assert watchdog._subprocess is not None and watchdog._subprocess.pid == pid
			assert target_ids <= {target.target_id for target in session.session_manager.get_all_page_targets()}
			stop_handler_names = [
				getattr(handler, '__name__', '') for handler in session.event_bus.handlers.get('BrowserStopEvent', [])
			]
			assert sum(name == 'LocalBrowserWatchdog.on_BrowserStopEvent' for name in stop_handler_names) == 1
		finally:
			await session.kill()

		assert session.cdp_url is None
		assert session.watchdogs.local_browser is None
		assert pid == -1 or not psutil.pid_exists(pid)

	async def test_kill_after_stop_cleans_preserved_process_and_owned_metadata(self, tmp_path):
		"""kill() works directly on the renewed bus without an intervening start()."""
		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=True,
				user_data_dir=None,
				keep_alive=True,
				enable_default_extensions=False,
				captcha_solver=False,
			)
		)
		await session.start()
		watchdog = session.watchdogs.local_browser
		assert watchdog is not None and watchdog._subprocess is not None
		pid = watchdog._subprocess.pid
		owned_temp_dir = tmp_path / 'browseruse-tmp-owned-browser-profile'
		owned_temp_dir.mkdir()
		original_user_data_dir = str(tmp_path / 'original-browser-profile')
		watchdog._temp_dirs_to_cleanup.append(owned_temp_dir)
		watchdog._original_user_data_dir = original_user_data_dir

		await session.stop()

		assert session.watchdogs.local_browser is watchdog
		assert watchdog._temp_dirs_to_cleanup == [owned_temp_dir]
		assert watchdog._original_user_data_dir == original_user_data_dir
		assert owned_temp_dir.exists()
		assert psutil.pid_exists(pid)

		await session.kill()

		assert not owned_temp_dir.exists()
		assert str(session.browser_profile.user_data_dir) == original_user_data_dir
		assert session.cdp_url is None
		assert not psutil.pid_exists(pid)

	async def test_dead_process_after_stop_is_discarded_before_fresh_start(self):
		"""A retained process that exits while disconnected cannot supply stale CDP state."""
		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=True,
				user_data_dir=None,
				keep_alive=True,
				enable_default_extensions=False,
				captcha_solver=False,
			)
		)
		old_pid = -1
		new_pid = -1
		try:
			await session.start()
			watchdog = session.watchdogs.local_browser
			assert watchdog is not None and watchdog._subprocess is not None
			process = watchdog._subprocess
			old_pid = process.pid

			await session.stop()
			assert session.watchdogs.local_browser is watchdog

			process.kill()
			await asyncio.to_thread(process.wait, timeout=5.0)
			assert not watchdog.owns_browser_process

			# A second stop runs on the renewed bus and discards the dead owner,
			# profile metadata, and stale CDP endpoint.
			await session.stop()
			assert session.watchdogs.local_browser is None
			assert session.cdp_url is None

			await session.start()
			fresh_watchdog = session.watchdogs.local_browser
			assert fresh_watchdog is not None and fresh_watchdog._subprocess is not None
			new_pid = fresh_watchdog._subprocess.pid
			assert new_pid != old_pid
			assert fresh_watchdog.owns_browser_process
		finally:
			await session.kill()

		assert old_pid == -1 or not psutil.pid_exists(old_pid)
		assert new_pid == -1 or not psutil.pid_exists(new_pid)

	async def test_kill_failure_retains_process_ownership_and_can_be_retried(self, tmp_path):
		"""A failed forced cleanup must not discard the only retry handle."""
		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=True,
				user_data_dir=None,
				is_local=True,
				cdp_url='http://127.0.0.1:9229',
				enable_default_extensions=False,
				captcha_solver=False,
			)
		)
		await session.watchdogs.attach()
		watchdog = session.watchdogs.local_browser
		assert watchdog is not None

		process = MagicMock(spec=psutil.Process)
		process.pid = 4242
		process.is_running.return_value = True
		process.status.return_value = psutil.STATUS_RUNNING
		process.terminate.side_effect = psutil.AccessDenied(pid=process.pid)
		watchdog._subprocess = process
		owned_temp_dir = tmp_path / 'browseruse-tmp-retryable-kill'
		owned_temp_dir.mkdir()
		watchdog._temp_dirs_to_cleanup = [owned_temp_dir]
		original_user_data_dir = str(tmp_path / 'original-profile')
		watchdog._original_user_data_dir = original_user_data_dir
		original_bus = session.event_bus

		with pytest.raises(RuntimeError, match='Failed to terminate owned browser process 4242'):
			await session.kill()

		assert session.event_bus is original_bus
		assert session.watchdogs.local_browser is watchdog
		assert watchdog._subprocess is process
		assert watchdog._temp_dirs_to_cleanup == [owned_temp_dir]
		assert watchdog._original_user_data_dir == original_user_data_dir
		assert owned_temp_dir.exists()
		assert session.cdp_url == 'http://127.0.0.1:9229'
		assert session._intentional_stop is False

		# The process exited independently before the retry. A second kill now
		# confirms that state and completes all owned-resource cleanup.
		process.terminate.side_effect = None
		process.is_running.return_value = False
		await session.kill()

		assert session.watchdogs.local_browser is None
		assert session.cdp_url is None
		assert not owned_temp_dir.exists()
		assert str(session.browser_profile.user_data_dir) == original_user_data_dir

	async def test_cleanup_process_raises_when_process_survives_terminate_and_kill(self):
		process = MagicMock(spec=psutil.Process)
		process.pid = 4343
		process.is_running.return_value = True
		process.status.return_value = psutil.STATUS_RUNNING

		with (
			patch('browser_use.browser.watchdogs.local_browser_watchdog.asyncio.sleep', new=AsyncMock()),
			pytest.raises(RuntimeError, match=r'remained alive after terminate\(\) and kill\(\)'),
		):
			await LocalBrowserWatchdog._cleanup_process(process)

		process.terminate.assert_called_once_with()
		process.kill.assert_called_once_with()

	@pytest.mark.parametrize('keep_alive', [False, True])
	async def test_stop_discards_dead_owned_process_and_stale_endpoint(self, tmp_path, keep_alive: bool):
		"""A dead process is cleanup state, not a browser that start() can reuse."""
		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=True,
				user_data_dir=None,
				keep_alive=keep_alive,
				is_local=True,
				cdp_url='http://127.0.0.1:9230',
				enable_default_extensions=False,
				captcha_solver=False,
			)
		)
		await session.watchdogs.attach()
		watchdog = session.watchdogs.local_browser
		assert watchdog is not None

		dead_process = MagicMock(spec=psutil.Process)
		dead_process.pid = 4444
		dead_process.is_running.return_value = False
		watchdog._subprocess = dead_process
		owned_temp_dir = tmp_path / 'browseruse-tmp-dead-process'
		owned_temp_dir.mkdir()
		watchdog._temp_dirs_to_cleanup = [owned_temp_dir]
		original_user_data_dir = str(tmp_path / 'original-profile')
		watchdog._original_user_data_dir = original_user_data_dir

		await session.stop()

		assert session.watchdogs.local_browser is None
		assert session.cdp_url is None
		assert not owned_temp_dir.exists()
		assert str(session.browser_profile.user_data_dir) == original_user_data_dir
		dead_process.terminate.assert_not_called()
		dead_process.kill.assert_not_called()

	async def test_reset_rechecks_process_liveness_before_preserving_owner(self, tmp_path):
		"""A process that dies after stop's first check must not leave a stale owner."""
		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=True,
				user_data_dir=None,
				is_local=True,
				cdp_url='http://127.0.0.1:9231',
				enable_default_extensions=False,
				captcha_solver=False,
			)
		)
		await session.watchdogs.attach()
		watchdog = session.watchdogs.local_browser
		assert watchdog is not None

		dead_process = MagicMock(spec=psutil.Process)
		dead_process.pid = 4545
		dead_process.is_running.return_value = False
		watchdog._subprocess = dead_process
		owned_temp_dir = tmp_path / 'browseruse-tmp-reset-race'
		owned_temp_dir.mkdir()
		watchdog._temp_dirs_to_cleanup = [owned_temp_dir]

		await session._reset(preserve_owned_local_browser=True)

		assert session.watchdogs.local_browser is None
		assert session.cdp_url is None
		assert not owned_temp_dir.exists()
		dead_process.terminate.assert_not_called()
		dead_process.kill.assert_not_called()

	@pytest.mark.parametrize('lifecycle_method', ['stop', 'kill'])
	async def test_lifecycle_propagates_stop_handler_failure_without_resetting_state(self, lifecycle_method: str):
		"""Both public teardown paths must inspect BrowserStopEvent failures."""
		from browser_use.browser.events import BrowserStopEvent

		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=True,
				user_data_dir=None,
				is_local=False,
				cdp_url='http://127.0.0.1:9232',
				captcha_solver=False,
			)
		)
		original_bus = session.event_bus

		async def fail_stop(_event: BrowserStopEvent) -> None:
			raise RuntimeError('stop handler failed')

		session.event_bus.on(BrowserStopEvent, fail_stop)

		with pytest.raises(RuntimeError, match='stop handler failed'):
			await getattr(session, lifecycle_method)()

		assert session.event_bus is original_bus
		assert session.cdp_url == 'http://127.0.0.1:9232'
		assert session._intentional_stop is False
		await session.event_bus.stop(clear=True, timeout=1.0)

	def test_process_liveness_distinguishes_exit_zombie_and_uncertain_status(self):
		gone = MagicMock(spec=psutil.Process)
		gone.is_running.side_effect = psutil.NoSuchProcess(pid=4646)
		assert not LocalBrowserWatchdog._process_is_alive(gone)

		zombie = MagicMock(spec=psutil.Process)
		zombie.is_running.return_value = True
		zombie.status.return_value = psutil.STATUS_ZOMBIE
		assert not LocalBrowserWatchdog._process_is_alive(zombie)

		uncertain = MagicMock(spec=psutil.Process)
		uncertain.is_running.return_value = True
		uncertain.status.side_effect = psutil.AccessDenied(pid=4747)
		assert LocalBrowserWatchdog._process_is_alive(uncertain)

	@pytest.mark.parametrize('lifecycle_method', ['stop', 'kill'])
	async def test_lifecycle_finalizes_recording_and_har_before_disconnect(self, lifecycle_method: str):
		"""Artifacts finish while the CDP client is still available."""
		session = BrowserSession(browser_profile=BrowserProfile(headless=True, user_data_dir=None))
		order: list[str] = []
		cdp_client = MagicMock(spec=CDPClient)

		async def disconnect() -> None:
			order.append('disconnect')

		async def finalize_recording() -> None:
			assert session._cdp_client_root is cdp_client
			order.append('recording')

		async def finalize_har() -> None:
			assert session._cdp_client_root is cdp_client
			order.append('har')

		cdp_client.stop = AsyncMock(side_effect=disconnect)
		session._cdp_client_root = cdp_client
		session.watchdogs.recording = cast(
			RecordingWatchdog,
			SimpleNamespace(
				is_recording=True,
				stop_recording=AsyncMock(side_effect=finalize_recording),
			),
		)
		session.watchdogs.har_recording = cast(
			HarRecordingWatchdog,
			SimpleNamespace(finalize=AsyncMock(side_effect=finalize_har)),
		)

		await getattr(session, lifecycle_method)()

		assert order == ['recording', 'har', 'disconnect']

	# @pytest.mark.skip(reason="Race condition - DOMWatchdog tries to inject scripts into tab that's being closed")
	# async def test_page_lifecycle_management(self, browser_session: BrowserSession):
	# 	"""Test session handles page lifecycle correctly."""
	# 	# logger.info('Testing page lifecycle management')

	# 	# Start the session and get initial state
	# 	await browser_session.start()
	# 	initial_tabs = await browser_session.get_tabs()
	# 	initial_count = len(initial_tabs)

	# 	# Get current tab info
	# 	current_url = await browser_session.get_current_page_url()
	# 	assert current_url is not None

	# 	# Get current tab ID
	# 	current_tab_id = browser_session.agent_focus.target_id if browser_session.agent_focus else None
	# 	assert current_tab_id is not None

	# 	# Close the current tab using the event system
	# 	from browser_use.browser.events import CloseTabEvent

	# 	close_event = browser_session.event_bus.dispatch(CloseTabEvent(target_id=current_tab_id))
	# 	await close_event

	# 	# Operations should still work - may create new page or use existing
	# 	tabs_after_close = await browser_session.get_tabs()
	# 	assert isinstance(tabs_after_close, list)

	# 	# Create a new tab explicitly
	# 	event = browser_session.event_bus.dispatch(NavigateToUrlEvent(url='about:blank', new_tab=True))
	# 	await event
	# 	await event.event_result(raise_if_any=True, raise_if_none=False)

	# 	# Should have at least one tab now
	# 	final_tabs = await browser_session.get_tabs()
	# 	assert len(final_tabs) >= 1

	async def test_user_data_dir_not_allowed_to_corrupt_default_profile(self):
		"""Test user_data_dir handling for different browser channels and version mismatches."""
		# Test 1: Chromium with default user_data_dir and default channel should work fine
		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=True,
				user_data_dir=get_environment_config().default_user_data_dir,
				channel=BROWSERUSE_DEFAULT_CHANNEL,  # chromium
				keep_alive=False,
			),
		)

		try:
			await session.start()
			assert session._cdp_client_root is not None
			# Verify the user_data_dir wasn't changed
			assert session.browser_profile.user_data_dir == get_environment_config().default_user_data_dir
		finally:
			await session.kill()

		# Test 2: Chrome with default user_data_dir should change dir AND copy to temp
		profile2 = BrowserProfile(
			headless=True,
			user_data_dir=get_environment_config().default_user_data_dir,
			channel=BrowserChannel.CHROME,
			keep_alive=False,
		)

		# The validator should have changed the user_data_dir to avoid corruption
		# And then _copy_profile copies it to a temp directory (Chrome only)
		assert profile2.user_data_dir != get_environment_config().default_user_data_dir
		assert 'browser-use-user-data-dir-' in str(profile2.user_data_dir)

		# Test 3: Edge with default user_data_dir should also change
		profile3 = BrowserProfile(
			headless=True,
			user_data_dir=get_environment_config().default_user_data_dir,
			channel=BrowserChannel.MSEDGE,
			keep_alive=False,
		)

		assert profile3.user_data_dir != get_environment_config().default_user_data_dir
		assert profile3.user_data_dir == get_environment_config().default_user_data_dir.parent / 'default-msedge'
		assert 'browser-use-user-data-dir-' not in str(profile3.user_data_dir)


class TestBrowserSessionReusePatterns:
	"""Tests for all browser re-use patterns documented in docs/customize/real-browser.mdx"""

	async def test_sequential_agents_same_profile_different_browser(self, mock_llm):
		"""Test Sequential Agents, Same Profile, Different Browser pattern"""
		from browser_use import Agent
		from browser_use.browser.profile import BrowserProfile

		# Create a reusable profile
		reused_profile = BrowserProfile(
			user_data_dir=None,  # Use temp dir for testing
			headless=True,
		)

		# First agent
		agent1 = Agent(
			task='The first task...',
			llm=mock_llm,
			browser_profile=reused_profile,
		)
		await agent1.run()

		# Verify first agent's session is closed
		assert agent1.browser_session is not None
		assert not agent1.browser_session._cdp_client_root is not None

		# Second agent with same profile
		agent2 = Agent(
			task='The second task...',
			llm=mock_llm,
			browser_profile=reused_profile,
			# Disable memory for tests
		)
		await agent2.run()

		# Verify second agent created a new session
		assert agent2.browser_session is not None
		assert agent1.browser_session is not agent2.browser_session
		assert not agent2.browser_session._cdp_client_root is not None

	async def test_sequential_agents_same_profile_same_browser(self, mock_llm):
		"""Test Sequential Agents, Same Profile, Same Browser pattern"""
		from browser_use import Agent, BrowserSession

		# Create a reusable session with keep_alive
		reused_session = BrowserSession(
			browser_profile=BrowserProfile(
				user_data_dir=None,  # Use temp dir for testing
				headless=True,
				keep_alive=True,  # Don't close browser after agent.run()
			),
		)

		try:
			# Start the session manually (agents will reuse this initialized session)
			await reused_session.start()

			# First agent
			agent1 = Agent(
				task='The first task...',
				llm=mock_llm,
				browser=reused_session,
				# Disable memory for tests
			)
			await agent1.run()

			# Verify session is still alive
			assert reused_session._cdp_client_root is not None

			# Second agent reusing the same session
			agent2 = Agent(
				task='The second task...',
				llm=mock_llm,
				browser=reused_session,
				# Disable memory for tests
			)
			await agent2.run()

			# Verify same browser was used (using __eq__ to check browser_pid, cdp_url)
			assert agent1.browser_session == agent2.browser_session
			assert agent1.browser_session == reused_session
			assert reused_session._cdp_client_root is not None

		finally:
			await reused_session.kill()


class TestBrowserSessionEventSystem:
	"""Tests for the new event system integration in BrowserSession."""

	@pytest.fixture(scope='function')
	async def browser_session(self):
		"""Create a BrowserSession instance for event system testing."""
		profile = BrowserProfile(headless=True, user_data_dir=None, keep_alive=False, captcha_solver=False)
		session = BrowserSession(browser_profile=profile)
		yield session
		await session.kill()

	async def test_event_bus_initialization(self, browser_session):
		"""Test that event bus is properly initialized with unique name."""
		# Event bus should be created during __init__
		assert browser_session.event_bus is not None
		assert browser_session.event_bus.name.startswith('EventBus_')
		# Event bus name format may vary, just check it exists

	async def test_event_handlers_registration(self, browser_session: BrowserSession):
		"""Test that event handlers are properly registered."""
		# Attach all watchdogs to register their handlers
		await browser_session.watchdogs.attach()

		# Check that handlers are registered in the event bus
		from browser_use.browser.events import (
			BrowserStartEvent,
			BrowserStateRequestEvent,
			BrowserStopEvent,
			ClickElementEvent,
			CloseTabEvent,
			ScreenshotEvent,
			ScrollEvent,
			TypeTextEvent,
		)

		# These event types should have handlers registered
		event_types_with_handlers = [
			BrowserStartEvent,
			BrowserStopEvent,
			ClickElementEvent,
			TypeTextEvent,
			ScrollEvent,
			CloseTabEvent,
			BrowserStateRequestEvent,
			ScreenshotEvent,
		]

		for event_type in event_types_with_handlers:
			handlers = browser_session.event_bus.handlers.get(event_type.__name__, [])
			assert len(handlers) > 0, f'No handlers registered for {event_type.__name__}'

		counts = {event_type: len(handlers) for event_type, handlers in browser_session.event_bus.handlers.items()}
		await browser_session.watchdogs.attach()
		assert {event_type: len(handlers) for event_type, handlers in browser_session.event_bus.handlers.items()} == counts
		assert browser_session.watchdogs.dom is not None
		assert browser_session.watchdogs.downloads is not None
		assert browser_session.watchdogs.storage_state is not None
		assert browser_session.watchdogs.har_recording is None
		assert browser_session.watchdogs.captcha is None

	async def test_direct_event_dispatching(self, browser_session):
		"""Test direct event dispatching without using the public API."""
		from browser_use.browser.events import BrowserConnectedEvent, BrowserStartEvent

		# Dispatch BrowserStartEvent directly
		start_event = browser_session.event_bus.dispatch(BrowserStartEvent())

		# Wait for event to complete
		await start_event

		# Check if BrowserConnectedEvent was dispatched
		assert browser_session._cdp_client_root is not None

		# Check event history
		event_history = list(browser_session.event_bus.event_history.values())
		assert len(event_history) >= 2  # BrowserStartEvent + BrowserConnectedEvent + others

		# Find the BrowserConnectedEvent in history
		started_events = [e for e in event_history if isinstance(e, BrowserConnectedEvent)]
		assert len(started_events) >= 1
		assert started_events[0].cdp_url is not None

	async def test_event_system_error_handling(self, browser_session):
		"""Test error handling in event system."""
		from browser_use.browser.events import BrowserStartEvent

		# Create session with invalid CDP URL to trigger error
		error_session = BrowserSession(
			browser_profile=BrowserProfile(headless=True),
			cdp_url='http://localhost:99999',  # Invalid port
		)

		try:
			# Dispatch start event directly - should trigger error handling
			start_event = error_session.event_bus.dispatch(BrowserStartEvent())

			# The event bus catches and logs the error, but the event awaits successfully
			await start_event

			# The session should not be initialized due to the error
			assert error_session._cdp_client_root is None, 'Session should not be initialized after connection error'

			# Verify the error was logged in the event history (good enough for error handling test)
			assert len(error_session.event_bus.event_history) > 0, 'Event should be tracked even with errors'

		finally:
			await error_session.kill()

	async def test_concurrent_event_dispatching(self, browser_session: BrowserSession):
		"""Test that concurrent events are handled properly."""
		from browser_use.browser.events import ScreenshotEvent

		# Start browser first
		await browser_session.start()

		# Dispatch multiple events concurrently
		screenshot_event1 = browser_session.event_bus.dispatch(ScreenshotEvent())
		screenshot_event2 = browser_session.event_bus.dispatch(ScreenshotEvent())

		# Both should complete successfully
		results = await asyncio.gather(screenshot_event1, screenshot_event2, return_exceptions=True)

		# Check that no exceptions were raised
		for result in results:
			assert not isinstance(result, Exception), f'Event failed with: {result}'

	# async def test_many_parallel_browser_sessions(self):
	# 	"""Test spawning 12 parallel browser_sessions with different settings and ensure they all work"""
	# 	from browser_use import BrowserSession

	# 	browser_sessions = []

	# 	for i in range(3):
	# 		browser_sessions.append(
	# 			BrowserSession(
	# 				browser_profile=BrowserProfile(
	# 					user_data_dir=None,
	# 					headless=True,
	# 					keep_alive=True,
	# 				),
	# 			)
	# 		)
	# 	for i in range(3):
	# 		browser_sessions.append(
	# 			BrowserSession(
	# 				browser_profile=BrowserProfile(
	# 					user_data_dir=Path(tempfile.mkdtemp(prefix=f'browseruse-tmp-{i}')),
	# 					headless=True,
	# 					keep_alive=True,
	# 				),
	# 			)
	# 		)
	# 	for i in range(3):
	# 		browser_sessions.append(
	# 			BrowserSession(
	# 				browser_profile=BrowserProfile(
	# 					user_data_dir=None,
	# 					headless=True,
	# 					keep_alive=False,
	# 				),
	# 			)
	# 		)
	# 	for i in range(3):
	# 		browser_sessions.append(
	# 			BrowserSession(
	# 				browser_profile=BrowserProfile(
	# 					user_data_dir=Path(tempfile.mkdtemp(prefix=f'browseruse-tmp-{i}')),
	# 					headless=True,
	# 					keep_alive=False,
	# 				),
	# 			)
	# 		)

	# 	print('Starting many parallel browser sessions...')
	# 	await asyncio.gather(*[browser_session.start() for browser_session in browser_sessions])

	# 	print('Ensuring all parallel browser sessions are connected and usable...')
	# 	new_tab_tasks = []
	# 	for browser_session in browser_sessions:
	# 		assert browser_session._cdp_client_root is not None
	# 		assert browser_session._cdp_client_root is not None
	# 		new_tab_tasks.append(browser_session.create_new_tab('chrome://version'))
	# 	await asyncio.gather(*new_tab_tasks)

	# 	print('killing every 3rd browser_session to test parallel shutdown')
	# 	kill_tasks = []
	# 	for i in range(0, len(browser_sessions), 3):
	# 		kill_tasks.append(browser_sessions[i].kill())
	# 		browser_sessions[i] = None
	# 	results = await asyncio.gather(*kill_tasks, return_exceptions=True)
	# 	# Check that no exceptions were raised during cleanup
	# 	for i, result in enumerate(results):
	# 		if isinstance(result, Exception):
	# 			print(f'Warning: Browser session kill raised exception: {type(result).__name__}: {result}')

	# 	print('ensuring the remaining browser_sessions are still connected and usable')
	# 	new_tab_tasks = []
	# 	screenshot_tasks = []
	# 	for browser_session in filter(bool, browser_sessions):
	# 		assert browser_session._cdp_client_root is not None
	# 		assert browser_session._cdp_client_root is not None
	# 		new_tab_tasks.append(browser_session.create_new_tab('chrome://version'))
	# 		screenshot_tasks.append(browser_session.take_screenshot())
	# 	await asyncio.gather(*new_tab_tasks)
	# 	await asyncio.gather(*screenshot_tasks)

	# 	kill_tasks = []
	# 	print('killing the remaining browser_sessions')
	# 	for browser_session in filter(bool, browser_sessions):
	# 		kill_tasks.append(browser_session.kill())
	# 	results = await asyncio.gather(*kill_tasks, return_exceptions=True)
	# 	# Check that no exceptions were raised during cleanup
	# 	for i, result in enumerate(results):
	# 		if isinstance(result, Exception):
	# 			print(f'Warning: Browser session kill raised exception: {type(result).__name__}: {result}')
