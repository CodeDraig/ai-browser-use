from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from browser_use.browser.events import (
	AgentFocusChangedEvent,
	NavigateToUrlEvent,
	NavigationCompleteEvent,
	NavigationStartedEvent,
	SwitchTabEvent,
	TabCreatedEvent,
)
from browser_use.security import is_new_tab_page

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession


class BrowserNavigation:
	"""Own navigation orchestration and readiness reporting for a browser session."""

	def __init__(self, session: BrowserSession) -> None:
		self.session = session

	async def on_NavigateToUrlEvent(self, event: NavigateToUrlEvent) -> None:
		"""Handle navigation requests - core browser functionality."""
		self.session.logger.debug(
			f'[on_NavigateToUrlEvent] Received NavigateToUrlEvent: url={event.url}, new_tab={event.new_tab}'
		)
		if not self.session.agent_focus_target_id:
			self.session.logger.warning('Cannot navigate - browser not connected')
			return

		target_id = None
		current_target_id = self.session.agent_focus_target_id

		# If new_tab=True but we're already in a new tab, set new_tab=False
		current_target = self.session.session_manager.get_target(current_target_id)
		if event.new_tab and is_new_tab_page(current_target.url):
			self.session.logger.debug(f'[on_NavigateToUrlEvent] Already on blank tab ({current_target.url}), reusing')
			event.new_tab = False

		try:
			# Find or create target for navigation
			self.session.logger.debug(f'[on_NavigateToUrlEvent] Processing new_tab={event.new_tab}')

			if event.new_tab:
				page_targets = self.session.session_manager.get_all_page_targets()
				self.session.logger.debug(f'[on_NavigateToUrlEvent] Found {len(page_targets)} existing tabs')

				# Look for existing about:blank tab that's not the current one
				for idx, target in enumerate(page_targets):
					self.session.logger.debug(f'[on_NavigateToUrlEvent] Tab {idx}: url={target.url}, targetId={target.target_id}')
					if target.url == 'about:blank' and target.target_id != current_target_id:
						target_id = target.target_id
						self.session.logger.debug(f'Reusing existing about:blank tab #{target_id[-4:]}')
						break

				# Create new tab if no reusable one found
				if not target_id:
					self.session.logger.debug('[on_NavigateToUrlEvent] No reusable about:blank tab found, creating new tab...')
					try:
						target_id = await self.session.cdp.create_new_page('about:blank')
						self.session.logger.debug(f'Created new tab #{target_id[-4:]}')
						# Dispatch TabCreatedEvent for new tab
						await self.session.event_bus.dispatch(TabCreatedEvent(target_id=target_id, url='about:blank'))
					except Exception as e:
						self.session.logger.error(f'[on_NavigateToUrlEvent] Failed to create new tab: {type(e).__name__}: {e}')
						# Fall back to using current tab
						target_id = current_target_id
						self.session.logger.warning(f'[on_NavigateToUrlEvent] Falling back to current tab #{target_id[-4:]}')
			else:
				# Use current tab
				target_id = target_id or current_target_id

			# Switch to target tab if needed (for both new_tab=True and new_tab=False)
			if self.session.agent_focus_target_id is None or self.session.agent_focus_target_id != target_id:
				self.session.logger.debug(
					f'[on_NavigateToUrlEvent] Switching to target tab {target_id[-4:]} (current: {self.session.agent_focus_target_id[-4:] if self.session.agent_focus_target_id else "none"})'
				)
				# Activate target (bring to foreground)
				await self.session.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
			else:
				self.session.logger.debug(
					f'[on_NavigateToUrlEvent] Already on target tab {target_id[-4:]}, skipping SwitchTabEvent'
				)

			assert self.session.agent_focus_target_id is not None and self.session.agent_focus_target_id == target_id, (
				'Agent focus not updated to new target_id after SwitchTabEvent should have switched to it'
			)

			# Dispatch navigation started
			await self.session.event_bus.dispatch(NavigationStartedEvent(target_id=target_id, url=event.url))

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
			await self.session._close_extension_options_pages()

			# Dispatch navigation complete
			self.session.logger.debug(f'Dispatching NavigationCompleteEvent for {committed_url} (tab #{target_id[-4:]})')
			await self.session.event_bus.dispatch(
				NavigationCompleteEvent(
					target_id=target_id,
					url=committed_url,
					status=None,  # CDP doesn't provide status directly
					loading_status=loading_status,  # non-None when readiness timed out
				)
			)
			await self.session.event_bus.dispatch(AgentFocusChangedEvent(target_id=target_id, url=committed_url))

			# Note: These should be handled by dedicated watchdogs:
			# - Security checks (security_watchdog)
			# - Page health checks (crash_watchdog)
			# - Dialog handling (dialog_watchdog)
			# - Download handling (downloads_watchdog)
			# - DOM rebuilding (dom_watchdog)

		except Exception as e:
			self.session.logger.error(f'Navigation failed: {type(e).__name__}: {e}')
			if target_id:
				committed_url = await self._get_navigation_event_url(target_id, event.url)
				await self.session.event_bus.dispatch(
					NavigationCompleteEvent(
						target_id=target_id,
						url=committed_url,
						error_message=f'{type(e).__name__}: {e}',
					)
				)
				await self.session.event_bus.dispatch(AgentFocusChangedEvent(target_id=target_id, url=committed_url))
			raise

	async def _get_navigation_event_url(self, target_id: str, requested_url: str) -> str:
		"""Resolve the committed URL for navigation events, failing closed under URL restrictions."""
		committed_url = await self._get_committed_navigation_url(target_id)
		if committed_url is not None:
			return committed_url
		has_url_policy = bool(self.session.browser_profile.allowed_domains or self.session.browser_profile.prohibited_domains)
		return '' if has_url_policy else requested_url

	async def _get_committed_navigation_url(self, target_id: str) -> str | None:
		"""Return Chrome's committed main-frame URL, or None when it cannot be verified."""
		try:
			cdp_session = await self.session.get_or_create_cdp_session(target_id, focus=False)
			history = await cdp_session.cdp_client.send.Page.getNavigationHistory(session_id=cdp_session.session_id)
			entries = history.get('entries') or []
			current_index = history.get('currentIndex')
			if not isinstance(current_index, int) or current_index < 0 or current_index >= len(entries):
				return None
			url = entries[current_index].get('url')
			return url if isinstance(url, str) and url else None
		except Exception as exc:
			self.session.logger.warning(f'Could not verify committed navigation URL for target {target_id[-4:]}: {exc}')
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
		cdp_session = await self.session.get_or_create_cdp_session(target_id, focus=False)

		if timeout is None:
			target = self.session.session_manager.get_target(target_id)
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
			self.session.logger.debug(f'✅ Page ready for {url} (commit, {duration_ms:.0f}ms)')
			return None

		navigation_id = nav_result.get('loaderId')

		# Page.navigate omits loaderId for same-document navigations (#fragment,
		# History API): the navigation is already committed and Chrome emits no new
		# load/DOMContentLoaded lifecycle events for it — waiting would only burn
		# the timeout against stale events from the previous document load.
		if not navigation_id:
			duration_ms = (asyncio.get_event_loop().time() - nav_start_time) * 1000
			self.session.logger.debug(f'✅ Page ready for {url} (same-document navigation, {duration_ms:.0f}ms)')
			return None
		start_time = asyncio.get_event_loop().time()
		seen_events = []

		# Per-target buffer owned by SessionManager — NOT a per-session attribute, whose
		# feeding handler used to get replaced whenever another target attached.
		lifecycle_events = self.session.session_manager.lifecycle.get_lifecycle_events(target_id)

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
						self.session.logger.debug(f'✅ Page ready for {url} ({event_name}, {duration_ms:.0f}ms)')
						return None

			except Exception as e:
				self.session.logger.debug(f'Error polling lifecycle events: {e}')

			await asyncio.sleep(poll_interval)

		duration_ms = (asyncio.get_event_loop().time() - nav_start_time) * 1000
		if not seen_events:
			self.session.logger.error(
				f'❌ No lifecycle events received for {url} after {duration_ms:.0f}ms! '
				f'Monitoring may have failed. Target: {cdp_session.target_id[:8]}'
			)
			return f'timeout after {timeout}s: no lifecycle events received (monitoring may have failed)'
		self.session.logger.warning(f'⚠️ Page readiness timeout ({timeout}s, {duration_ms:.0f}ms) for {url}')
		return f'timeout after {timeout}s waiting for {wait_until!r} (saw: {", ".join(seen_events[-5:])})'
