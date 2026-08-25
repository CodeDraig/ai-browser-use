"""Security watchdog for enforcing URL access policies."""

from typing import TYPE_CHECKING, ClassVar

from bubus import BaseEvent

from browser_use.browser.events import (
	BrowserErrorEvent,
	NavigateToUrlEvent,
	NavigationCompleteEvent,
	TabCreatedEvent,
)
from browser_use.browser.watchdog_base import BaseWatchdog
from browser_use.security import is_ip_address, is_url_allowed_by_policy

if TYPE_CHECKING:
	pass


class SecurityWatchdog(BaseWatchdog):
	"""Monitors and enforces security policies for URL access."""

	# Event contracts
	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [
		NavigateToUrlEvent,
		NavigationCompleteEvent,
		TabCreatedEvent,
	]
	EMITS: ClassVar[list[type[BaseEvent]]] = [
		BrowserErrorEvent,
	]

	async def on_NavigateToUrlEvent(self, event: NavigateToUrlEvent) -> None:
		"""Check if navigation URL is allowed before navigation starts."""
		# Security check BEFORE navigation
		if not self._is_url_allowed(event.url):
			self.logger.warning(f'⛔️ Blocking navigation to disallowed URL: {event.url}')
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='NavigationBlocked',
					message=f'Navigation blocked to disallowed URL: {event.url}',
					details={'url': event.url, 'reason': 'not_in_allowed_domains'},
				)
			)
			# Stop event propagation by raising exception
			raise ValueError(f'Navigation to {event.url} blocked by security policy')

	async def on_NavigationCompleteEvent(self, event: NavigationCompleteEvent) -> None:
		"""Check if navigated URL is allowed (catches redirects to blocked domains)."""
		# Check if the navigated URL is allowed (in case of redirects)
		if not self._is_url_allowed(event.url):
			self.logger.warning(f'⛔️ Navigation to non-allowed URL detected: {event.url}')

			# Dispatch browser error
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='NavigationBlocked',
					message=f'Navigation blocked to non-allowed URL: {event.url} - redirecting to about:blank',
					details={'url': event.url, 'target_id': event.target_id},
				)
			)
			# Navigate to about:blank to keep session alive
			# Agent will see the error and can continue with other tasks
			try:
				session = await self.browser_session.get_or_create_cdp_session(target_id=event.target_id)
				await session.cdp_client.send.Page.navigate(params={'url': 'about:blank'}, session_id=session.session_id)
				self.logger.info(f'⛔️ Navigated to about:blank after blocked URL: {event.url}')
			except Exception as e:
				self.logger.error(f'⛔️ Failed to navigate to about:blank: {type(e).__name__} {e}')

	async def on_TabCreatedEvent(self, event: TabCreatedEvent) -> None:
		"""Check if new tab URL is allowed."""
		if not self._is_url_allowed(event.url):
			self.logger.warning(f'⛔️ New tab created with disallowed URL: {event.url}')

			# Dispatch error and try to close the tab
			self.event_bus.dispatch(
				BrowserErrorEvent(
					error_type='TabCreationBlocked',
					message=f'Tab created with non-allowed URL: {event.url}',
					details={'url': event.url, 'target_id': event.target_id},
				)
			)

			# Try to close the offending tab
			try:
				await self.browser_session._cdp_close_page(event.target_id)
				self.logger.info(f'⛔️ Closed new tab with non-allowed URL: {event.url}')
			except Exception as e:
				self.logger.error(f'⛔️ Failed to close new tab with non-allowed URL: {type(e).__name__} {e}')

	def _is_ip_address(self, host: str) -> bool:
		"""Compatibility wrapper for direct security-watchdog tests."""
		return is_ip_address(host)

	def _is_url_allowed(self, url: str) -> bool:
		"""Check if a URL is allowed based on the allowed_domains configuration.

		Args:
			url: The URL to check

		Returns:
			True if the URL is allowed, False otherwise
		"""

		profile = self.browser_session.browser_profile
		return is_url_allowed_by_policy(
			url,
			allowed_domains=profile.allowed_domains,
			prohibited_domains=profile.prohibited_domains,
			block_ip_addresses=profile.block_ip_addresses,
			log_warnings=True,
		)
