from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from cdp_use.cdp.target import TargetID

from browser_use.runtime import create_task_with_error_handling

if TYPE_CHECKING:
	from browser_use.browser.session_manager import SessionManager


class FocusRecovery:
	"""Coordinate validation and recovery of the agent-focused browser target."""

	def __init__(self, manager: SessionManager) -> None:
		self.manager = manager
		self._recovery_lock = asyncio.Lock()
		self._recovery_in_progress = False
		self._recovery_complete_event: asyncio.Event | None = None
		self._recovery_task: asyncio.Task | None = None

	@property
	def in_progress(self) -> bool:
		return self._recovery_in_progress

	def request(self, crashed_target_id: TargetID, *, task_name: str) -> None:
		if self._recovery_in_progress:
			return
		self._recovery_task = create_task_with_error_handling(
			self._recover_agent_focus(crashed_target_id),
			name=task_name,
			logger_instance=self.manager.logger,
			suppress_exceptions=False,
		)

	async def ensure_valid_focus(self, timeout: float = 3.0) -> bool:
		"""Ensure agent_focus_target_id points to a valid, attached CDP session.

		If the focus target is stale (detached), this method waits for automatic recovery.
		Uses event-driven coordination instead of polling for efficiency.

		Args:
			timeout: Maximum time to wait for recovery in seconds (default: 3.0)

		Returns:
			True if focus is valid or successfully recovered, False if no focus or recovery failed
		"""
		if not self.manager.browser_session.agent_focus_target_id:
			# No focus at all - might be initial state or complete failure
			if self._recovery_in_progress and self._recovery_complete_event:
				# Recovery is happening, wait for it
				try:
					await asyncio.wait_for(self._recovery_complete_event.wait(), timeout=timeout)
					# Check again after recovery - simple existence check
					focus_id = self.manager.browser_session.agent_focus_target_id
					return bool(focus_id and self.manager._get_session_for_target(focus_id))
				except TimeoutError:
					self.manager.logger.error(f'[SessionManager] ❌ Timed out waiting for recovery after {timeout}s')
					return False
			return False

		# Simple existence check - does the focused target have a session?
		cdp_session = self.manager._get_session_for_target(self.manager.browser_session.agent_focus_target_id)
		if cdp_session:
			# Session exists - validate it's still active
			is_valid = await self.manager.validate_session(self.manager.browser_session.agent_focus_target_id)
			if is_valid:
				return True

		# Focus is stale - wait for recovery using event instead of polling
		stale_target_id = self.manager.browser_session.agent_focus_target_id
		self.manager.logger.warning(
			f'[SessionManager] ⚠️ Stale agent_focus detected (target {stale_target_id[:8] if stale_target_id else "None"}... detached), '
			f'waiting for recovery...'
		)

		# Check if recovery is already in progress
		if not self._recovery_in_progress:
			self.manager.logger.warning(
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
				focus_id = self.manager.browser_session.agent_focus_target_id
				if focus_id and self.manager._get_session_for_target(focus_id):
					self.manager.logger.info(
						f'[SessionManager] ✅ Agent focus recovered to {self.manager.browser_session.agent_focus_target_id[:8]}... '
						f'after {elapsed * 1000:.0f}ms'
					)
					return True
				else:
					self.manager.logger.error(
						f'[SessionManager] ❌ Recovery completed but focus still invalid after {elapsed * 1000:.0f}ms'
					)
					return False

			except TimeoutError:
				self.manager.logger.error(
					f'[SessionManager] ❌ Recovery timed out after {timeout}s '
					f'(was: {stale_target_id[:8] if stale_target_id else "None"}..., '
					f'now: {self.manager.browser_session.agent_focus_target_id[:8] if self.manager.browser_session.agent_focus_target_id else "None"})'
				)
				return False
		else:
			self.manager.logger.error('[SessionManager] ❌ Recovery event not initialized')
			return False

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
					self.manager.logger.debug('[SessionManager] Recovery already in progress, waiting for it to complete')
					# Wait for ongoing recovery instead of starting a new one
					if self._recovery_complete_event:
						try:
							await asyncio.wait_for(self._recovery_complete_event.wait(), timeout=5.0)
						except TimeoutError:
							self.manager.logger.error('[SessionManager] Timed out waiting for ongoing recovery')
					return

				# Set recovery state
				self._recovery_in_progress = True
				self._recovery_complete_event = asyncio.Event()

				if self.manager.browser_session._cdp_client_root is None:
					self.manager.logger.debug('[SessionManager] Skipping focus recovery - browser shutting down (no CDP client)')
					return

				# Check if another recovery already fixed agent_focus
				if (
					self.manager.browser_session.agent_focus_target_id
					and self.manager.browser_session.agent_focus_target_id != crashed_target_id
				):
					self.manager.logger.debug(
						f'[SessionManager] Agent focus already recovered by concurrent operation '
						f'(now: {self.manager.browser_session.agent_focus_target_id[:8]}...), skipping recovery'
					)
					return

				# Note: agent_focus_target_id may already be None (cleared in _handle_target_detached)
				current_focus_desc = (
					f'{self.manager.browser_session.agent_focus_target_id[:8]}...'
					if self.manager.browser_session.agent_focus_target_id
					else 'None (already cleared)'
				)

				self.manager.logger.warning(
					f'[SessionManager] Agent focus target {crashed_target_id[:8]}... detached! '
					f'Current focus: {current_focus_desc}. Auto-recovering by switching to another target...'
				)

			# Perform recovery (outside lock to allow concurrent operations)
			# Try to find another valid page target
			page_targets = self.manager.get_all_page_targets()

			new_target_id = None
			is_existing_tab = False

			if page_targets:
				# Switch to most recent page that's not the crashed one
				new_target_id = page_targets[-1].target_id
				is_existing_tab = True
				self.manager.logger.info(f'[SessionManager] Switching agent_focus to existing tab {new_target_id[:8]}...')
			else:
				# No pages exist - create a new one
				self.manager.logger.warning('[SessionManager] No tabs remain! Creating new tab for agent...')
				new_target_id = await self.manager.browser_session.cdp.create_new_page('about:blank')
				self.manager.logger.info(f'[SessionManager] Created new tab {new_target_id[:8]}... for agent')

				# Dispatch TabCreatedEvent so watchdogs can initialize
				from browser_use.browser.events import TabCreatedEvent

				self.manager.browser_session.event_bus.dispatch(TabCreatedEvent(url='about:blank', target_id=new_target_id))

			# Wait for CDP attach event to create session
			# Note: This polling is necessary - waiting for external Chrome CDP event
			# _handle_target_attached will add session to pool when Chrome fires attachedToTarget
			new_session = None
			for attempt in range(20):  # Wait up to 2 seconds
				await asyncio.sleep(0.1)
				new_session = self.manager._get_session_for_target(new_target_id)
				if new_session:
					break

			if new_session:
				self.manager.browser_session.agent_focus_target_id = new_target_id
				self.manager.logger.info(f'[SessionManager] ✅ Agent focus recovered: {new_target_id[:8]}...')

				# Visually activate the tab in browser (only for existing tabs)
				if is_existing_tab:
					try:
						assert self.manager.browser_session._cdp_client_root is not None
						await self.manager.browser_session._cdp_client_root.send.Target.activateTarget(
							params={'targetId': new_target_id}
						)
						self.manager.logger.debug(f'[SessionManager] Activated tab {new_target_id[:8]}... in browser UI')
					except Exception as e:
						self.manager.logger.debug(f'[SessionManager] Failed to activate tab visually: {e}')

				# Get target to access url (from owned data)
				target = self.manager.get_target(new_target_id)
				target_url = target.url if target else 'about:blank'

				# Dispatch focus changed event
				from browser_use.browser.events import AgentFocusChangedEvent

				self.manager.browser_session.event_bus.dispatch(AgentFocusChangedEvent(target_id=new_target_id, url=target_url))
				return

			# Recovery failed - create emergency fallback tab
			self.manager.logger.error(
				f'[SessionManager] ❌ Failed to get session for {new_target_id[:8]}... after 2s, creating emergency fallback tab'
			)

			fallback_target_id = await self.manager.browser_session.cdp.create_new_page('about:blank')
			self.manager.logger.warning(f'[SessionManager] Created emergency fallback tab {fallback_target_id[:8]}...')

			# Try one more time with fallback
			# Note: This polling is necessary - waiting for external Chrome CDP event
			for _ in range(20):
				await asyncio.sleep(0.1)
				fallback_session = self.manager._get_session_for_target(fallback_target_id)
				if fallback_session:
					self.manager.browser_session.agent_focus_target_id = fallback_target_id
					self.manager.logger.warning(
						f'[SessionManager] ⚠️ Agent focus set to emergency fallback: {fallback_target_id[:8]}...'
					)

					from browser_use.browser.events import AgentFocusChangedEvent, TabCreatedEvent

					self.manager.browser_session.event_bus.dispatch(
						TabCreatedEvent(url='about:blank', target_id=fallback_target_id)
					)
					self.manager.browser_session.event_bus.dispatch(
						AgentFocusChangedEvent(target_id=fallback_target_id, url='about:blank')
					)
					return

			# Complete failure - this should never happen
			self.manager.logger.critical(
				'[SessionManager] 🚨 CRITICAL: Failed to recover agent_focus even with fallback! Agent may be in broken state.'
			)

		except Exception as e:
			self.manager.logger.error(f'[SessionManager] ❌ Error during agent_focus recovery: {type(e).__name__}: {e}')
		finally:
			# Always signal completion and reset recovery state
			# This allows all waiting operations to proceed (success or failure)
			if self._recovery_complete_event:
				self._recovery_complete_event.set()
			self._recovery_in_progress = False
			self._recovery_task = None
			self.manager.logger.debug('[SessionManager] Recovery state reset')
