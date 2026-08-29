"""History navigation, refresh, and wait behavior for default browser actions."""

import asyncio

from browser_use.browser.events import GoBackEvent, GoForwardEvent, RefreshEvent, WaitEvent


class NavigationActions:
	"""Navigate browser history, refresh the active target, and perform bounded waits."""

	def __init__(self, browser_session) -> None:
		self.browser_session = browser_session

	@property
	def logger(self):
		return self.browser_session.logger

	async def handle_go_back(self, event: GoBackEvent) -> None:
		"""Handle navigate back request with CDP."""
		cdp_session = await self.browser_session.get_or_create_cdp_session()
		try:
			# Get CDP client and session

			# Get navigation history
			history = await cdp_session.cdp_client.send.Page.getNavigationHistory(session_id=cdp_session.session_id)
			current_index = history['currentIndex']
			entries = history['entries']

			# Check if we can go back
			if current_index <= 0:
				self.logger.warning('⚠️ Cannot go back - no previous entry in history')
				return

			# Navigate to the previous entry
			previous_entry_id = entries[current_index - 1]['id']
			await cdp_session.cdp_client.send.Page.navigateToHistoryEntry(
				params={'entryId': previous_entry_id}, session_id=cdp_session.session_id
			)

			# Wait for navigation
			await asyncio.sleep(0.5)
			# Navigation is handled by BrowserSession via events

			self.logger.info(f'🔙 Navigated back to {entries[current_index - 1]["url"]}')
		except Exception as e:
			raise

	async def handle_go_forward(self, event: GoForwardEvent) -> None:
		"""Handle navigate forward request with CDP."""
		cdp_session = await self.browser_session.get_or_create_cdp_session()
		try:
			# Get navigation history
			history = await cdp_session.cdp_client.send.Page.getNavigationHistory(session_id=cdp_session.session_id)
			current_index = history['currentIndex']
			entries = history['entries']

			# Check if we can go forward
			if current_index >= len(entries) - 1:
				self.logger.warning('⚠️ Cannot go forward - no next entry in history')
				return

			# Navigate to the next entry
			next_entry_id = entries[current_index + 1]['id']
			await cdp_session.cdp_client.send.Page.navigateToHistoryEntry(
				params={'entryId': next_entry_id}, session_id=cdp_session.session_id
			)

			# Wait for navigation
			await asyncio.sleep(0.5)
			# Navigation is handled by BrowserSession via events

			self.logger.info(f'🔜 Navigated forward to {entries[current_index + 1]["url"]}')
		except Exception as e:
			raise

	async def handle_refresh(self, event: RefreshEvent) -> None:
		"""Handle target refresh request with CDP."""
		cdp_session = await self.browser_session.get_or_create_cdp_session()
		try:
			# Reload the target
			await cdp_session.cdp_client.send.Page.reload(session_id=cdp_session.session_id)

			# Wait for reload
			await asyncio.sleep(1.0)

			# Note: We don't clear cached state here - let the next state fetch rebuild as needed

			# Navigation is handled by BrowserSession via events

			self.logger.info('🔄 Target refreshed')
		except Exception as e:
			raise

	async def handle_wait(self, event: WaitEvent) -> None:
		"""Handle wait request."""
		try:
			# Cap wait time at maximum
			actual_seconds = min(max(event.seconds, 0), event.max_seconds)
			if actual_seconds != event.seconds:
				self.logger.info(f'🕒 Waiting for {actual_seconds} seconds (capped from {event.seconds}s)')
			else:
				self.logger.info(f'🕒 Waiting for {actual_seconds} seconds')

			await asyncio.sleep(actual_seconds)
		except Exception as e:
			raise
