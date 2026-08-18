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
from browser_use.security import match_url_with_domain_pattern

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
		"""True iff `host` matches an IPv4 or IPv6 the browser would resolve.

		Mirrors WHATWG host canonicalization so non-standard IPv4 encodings
		(decimal, hex, octal, short-form, percent-encoded, Unicode digits)
		can't bypass `block_ip_addresses`. Never raises — unrecognizable
		hosts return False and fall through to domain-allowlist handling.
		"""
		import ipaddress
		import socket
		import unicodedata
		from urllib.parse import unquote

		bare = host.strip('[]')
		try:
			bare = unquote(bare)
		except Exception:
			pass
		try:
			bare = unicodedata.normalize('NFKC', bare)
		except Exception:
			pass
		# IDNA label separators NFKC misses (U+3002, U+FF61 → U+3002).
		bare = bare.replace('。', '.').replace('｡', '.')

		try:
			ipaddress.ip_address(bare)
			return True
		except Exception:
			pass
		# Non-standard IPv4 (decimal, hex, octal, short-form) — `inet_aton`
		# accepts the same liberal forms the kernel resolver does.
		try:
			socket.inet_aton(bare)
			return True
		except Exception:
			return False

	def _is_url_allowed(self, url: str) -> bool:
		"""Check if a URL is allowed based on the allowed_domains configuration.

		Args:
			url: The URL to check

		Returns:
			True if the URL is allowed, False otherwise
		"""

		# Always allow internal browser targets (before any other checks)
		if url in ['about:blank', 'chrome://new-tab-page/', 'chrome://new-tab-page', 'chrome://newtab/']:
			return True

		# Parse the URL to extract components
		from urllib.parse import urlparse

		try:
			parsed = urlparse(url)
		except Exception:
			# Invalid URL
			return False

		# Allow data: and blob: URLs (they don't have hostnames)
		if parsed.scheme in ['data', 'blob']:
			return True

		# Get the actual host (domain)
		host = parsed.hostname
		if not host:
			return False

		# Check if IP addresses should be blocked (before domain checks)
		if self.browser_session.browser_profile.block_ip_addresses:
			if self._is_ip_address(host):
				return False

		# If no allowed_domains specified, allow all URLs
		if (
			not self.browser_session.browser_profile.allowed_domains
			and not self.browser_session.browser_profile.prohibited_domains
		):
			return True

		# Use one parsed-component matcher for every collection shape. The former
		# set fast path compared hostnames only and therefore bypassed scheme policy.
		if self.browser_session.browser_profile.allowed_domains:
			allowed_domains = self.browser_session.browser_profile.allowed_domains
			return any(match_url_with_domain_pattern(url, pattern, log_warnings=True) for pattern in allowed_domains)

		# Check prohibited domains through the same matcher.
		if self.browser_session.browser_profile.prohibited_domains:
			prohibited_domains = self.browser_session.browser_profile.prohibited_domains
			return not any(match_url_with_domain_pattern(url, pattern, log_warnings=True) for pattern in prohibited_domains)

		return True
