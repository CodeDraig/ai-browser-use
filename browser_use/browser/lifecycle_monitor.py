from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Any

from cdp_use.cdp.page import LifecycleEventEvent
from cdp_use.cdp.target import SessionID, TargetID

from browser_use.security import is_new_tab_page

if TYPE_CHECKING:
	from browser_use.browser.session_manager import CDPSession, SessionManager


class LifecycleMonitor:
	"""Own per-target lifecycle buffers and page monitoring setup."""

	def __init__(self, manager: SessionManager) -> None:
		self.manager = manager
		self._lifecycle_events: dict[TargetID, deque[dict[str, Any]]] = {}
		self._main_frame_ids: dict[SessionID, str] = {}

	def handle_event(self, event: LifecycleEventEvent, session_id: SessionID | None) -> None:
		if not session_id:
			return
		target_id = self.manager.get_target_id_from_session_id(session_id)
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
		if event_name == 'load':
			target = self.manager.get_target(target_id)
			if target is not None and not is_new_tab_page(target.url):
				self.manager.navigation_policy.new_page_targets.discard(target_id)

	def get_lifecycle_events(self, target_id: TargetID) -> deque[dict[str, Any]]:
		events = self._lifecycle_events.get(target_id)
		if events is None:
			events = deque(maxlen=50)
			self._lifecycle_events[target_id] = events
		return events

	def clear(self) -> None:
		self._lifecycle_events.clear()
		self._main_frame_ids.clear()

	def remove_target(self, target_id: TargetID) -> None:
		self._lifecycle_events.pop(target_id, None)

	def remove_session(self, session_id: SessionID) -> None:
		self._main_frame_ids.pop(session_id, None)

	def main_frame_id(self, session_id: SessionID) -> str | None:
		return self._main_frame_ids.get(session_id)

	def target_monitoring_ready(self, target_id: TargetID, target_type: str) -> bool:
		"""Return whether any usable session owns the required target monitoring."""
		sessions = [
			self.manager._sessions[session_id]
			for session_id in self.manager._target_sessions.get(target_id, set())
			if session_id in self.manager._sessions
		]
		if not sessions:
			return False
		if target_type not in ('page', 'tab'):
			return True
		if not any(session._lifecycle_events is not None for session in sessions):
			return False
		if not self.manager.navigation_policy.active:
			return True
		fetch_session_id = self.manager.navigation_policy.fetch_sessions.get(target_id)
		return fetch_session_id in self._main_frame_ids

	async def enable_page_monitoring(self, cdp_session: CDPSession) -> None:
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

			if self.manager.navigation_policy.active:
				frame_tree = await cdp_session.cdp_client.send.Page.getFrameTree(session_id=cdp_session.session_id)
				main_frame_id = frame_tree.get('frameTree', {}).get('frame', {}).get('id')
				if not main_frame_id:
					raise RuntimeError(f'Could not identify the top frame for target {cdp_session.target_id}')
				self._main_frame_ids[cdp_session.session_id] = main_frame_id

			await self.manager.navigation_policy.enable_fetch_for_session(cdp_session, target_type='page')

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
				self.manager.logger.debug(
					f'[SessionManager] Target {cdp_session.target_id[:8]}... detached before monitoring could be enabled (normal for short-lived targets)'
				)
			else:
				self.manager.logger.warning(
					f'[SessionManager] Failed to enable monitoring for target {cdp_session.target_id[:8]}...: {e}'
				)
				if self.manager.navigation_policy.active:
					raise
