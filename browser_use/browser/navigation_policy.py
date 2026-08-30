from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from cdp_use.cdp.fetch import EnableParameters, RequestPattern, RequestPausedEvent
from cdp_use.cdp.target import SessionID, TargetID

from browser_use.security import is_url_allowed_by_policy, url_policy_configured

if TYPE_CHECKING:
	from browser_use.browser.session_manager import CDPSession, SessionManager


class NavigationPolicy:
	"""Install Fetch interception and contain disallowed top-level navigation."""

	_TARGET_CLOSE_ATTEMPTS = 2
	_TARGET_CLOSE_CONFIRMATION_TIMEOUT = 1.0
	_TARGET_CLOSE_POLL_INTERVAL = 0.05

	def __init__(self, manager: SessionManager) -> None:
		self.manager = manager
		self.new_page_targets: set[TargetID] = set()
		self.blocked_navigation_urls: dict[TargetID, str] = {}
		self.setup_failures: dict[TargetID, str] = {}
		self.fetch_sessions: dict[TargetID, SessionID] = {}
		self._fetch_setup_lock = asyncio.Lock()

	@property
	def active(self) -> bool:
		"""Whether top-level navigation must be intercepted for this profile."""
		profile = self.manager.browser_session.browser_profile
		return url_policy_configured(
			allowed_domains=profile.allowed_domains,
			prohibited_domains=profile.prohibited_domains,
			block_ip_addresses=profile.block_ip_addresses,
		)

	def is_url_allowed(self, url: str) -> bool:
		profile = self.manager.browser_session.browser_profile
		return is_url_allowed_by_policy(
			url,
			allowed_domains=profile.allowed_domains,
			prohibited_domains=profile.prohibited_domains,
			block_ip_addresses=profile.block_ip_addresses,
			log_warnings=True,
		)

	def mark_new_page_target(self, target_id: TargetID) -> None:
		"""Mark a caller-created blank target for blocked-tab containment."""
		self.new_page_targets.add(target_id)

	async def wait_for_policy_ready_page(self, target_id: TargetID, *, timeout: float = 2.0) -> CDPSession:
		"""Wait until a page target has its top-frame Fetch gate installed.

		A policy-enabled caller must not navigate a newly created target before
		this succeeds. Setup failures close the target and surface a hard error so
		no caller can accidentally continue with an ungated page.
		"""
		if not self.active:
			raise RuntimeError('wait_for_policy_ready_page() requires an active URL policy')

		loop = asyncio.get_running_loop()
		deadline = loop.time() + timeout
		while loop.time() < deadline:
			setup_failure = self.setup_failures.get(target_id)
			if setup_failure:
				await self.remediate_blocked_navigation(target_id, 'about:blank', setup_failed=True)
				raise RuntimeError(f'URL policy interception could not be installed for target {target_id}: {setup_failure}')

			target = self.manager._targets.get(target_id)
			if target is not None and self.manager.lifecycle.target_monitoring_ready(target_id, target.target_type):
				fetch_session_id = self.fetch_sessions.get(target_id)
				cdp_session = self.manager._sessions.get(fetch_session_id) if fetch_session_id else None
				if cdp_session is not None:
					return cdp_session

			await asyncio.sleep(0.05)

		setup_failure = f'timed out after {timeout:.1f}s waiting for top-frame Fetch interception'
		self.setup_failures[target_id] = setup_failure
		await self.remediate_blocked_navigation(target_id, 'about:blank', setup_failed=True)
		raise RuntimeError(f'URL policy interception could not be installed for target {target_id}: {setup_failure}')

	async def _continue_fetch_request(self, request_id: str, session_id: SessionID | None) -> None:
		cdp_client = self.manager.browser_session._cdp_client_root
		if cdp_client is None:
			return
		try:
			await cdp_client.send.Fetch.continueRequest(params={'requestId': request_id}, session_id=session_id)
		except Exception as error:
			# Detach races are normal after a tab closes.
			self.manager.logger.debug(f'[SessionManager] Could not continue Fetch request {request_id}: {error}')

	async def handle_request_paused(
		self,
		event: RequestPausedEvent,
		session_id: SessionID | None,
	) -> None:
		"""Resolve every paused request once and gate top-level documents."""
		request_id = event.get('requestId') or event.get('request_id')
		if not request_id:
			return

		if not self.active or not session_id or event.get('resourceType') != 'Document':
			await self._continue_fetch_request(request_id, session_id)
			return

		target_id = self.manager.get_target_id_from_session_id(session_id)
		target = self.manager.get_target(target_id) if target_id else None
		main_frame_id = self.manager.lifecycle.main_frame_id(session_id)
		frame_id = event.get('frameId')
		if not target_id or not target or target.target_type not in ('page', 'tab'):
			await self._continue_fetch_request(request_id, session_id)
			return
		if not main_frame_id:
			# A page session is not safe to run under policy unless its top frame was
			# identified before Fetch interception was enabled.
			await self._fail_fetch_request(request_id, session_id)
			await self.remediate_blocked_navigation(target_id, event['request']['url'], setup_failed=True)
			return
		if frame_id != main_frame_id:
			# Subframes and subresources are deliberately outside this policy.
			await self._continue_fetch_request(request_id, session_id)
			return

		url = event.get('request', {}).get('url', '')
		if self.is_url_allowed(url):
			self.blocked_navigation_urls.pop(target_id, None)
			await self._continue_fetch_request(request_id, session_id)
			return

		# Failing the intercepted request prevents DNS, proxy, and destination
		# traffic before containment changes the target state.
		await self._fail_fetch_request(request_id, session_id)
		await self.remediate_blocked_navigation(target_id, url)

	async def _fail_fetch_request(self, request_id: str, session_id: SessionID) -> None:
		cdp_client = self.manager.browser_session._cdp_client_root
		if cdp_client is None:
			return
		try:
			await cdp_client.send.Fetch.failRequest(
				params={'requestId': request_id, 'errorReason': 'BlockedByClient'},
				session_id=session_id,
			)
		except Exception as error:
			self.manager.logger.debug(f'[SessionManager] Could not fail blocked Fetch request {request_id}: {error}')

	async def remediate_blocked_navigation(
		self,
		target_id: TargetID,
		url: str,
		*,
		setup_failed: bool = False,
		require_target_closed: bool = False,
	) -> None:
		"""Contain one blocked navigation, optionally requiring confirmed target closure."""
		already_reported = self.blocked_navigation_urls.get(target_id) == url
		if already_reported and not require_target_closed:
			return

		is_new_page = target_id in self.new_page_targets
		if not already_reported:
			self.blocked_navigation_urls[target_id] = url

			from browser_use.browser.events import BrowserErrorEvent

			error_type = 'TabCreationBlocked' if is_new_page else 'NavigationBlocked'
			reason = 'policy_setup_failed' if setup_failed else 'not_in_allowed_domains'
			self.manager.browser_session.event_bus.dispatch(
				BrowserErrorEvent(
					error_type=error_type,
					message=f'Navigation blocked to disallowed URL: {url}',
					details={'url': url, 'target_id': target_id, 'reason': reason},
				)
			)

		cdp_client = self.manager.browser_session._cdp_client_root
		if cdp_client is None:
			if require_target_closed:
				raise RuntimeError(
					f'URL policy blocked {url}, but target {target_id} could not be confirmed closed: CDP client unavailable'
				)
			return
		try:
			if is_new_page or setup_failed:
				if require_target_closed:
					await self.close_target_with_confirmation(target_id, url)
				else:
					await cdp_client.send.Target.closeTarget(params={'targetId': target_id})
				self.manager.logger.warning(f'⛔️ Closed target blocked by URL policy: {url}')
			else:
				cdp_session = self.manager._get_session_for_target(target_id)
				if cdp_session:
					await cdp_client.send.Page.navigate(
						params={'url': 'about:blank'},
						session_id=cdp_session.session_id,
					)
				self.manager.logger.warning(f'⛔️ Replaced blocked top-level navigation with about:blank: {url}')
		except Exception as error:
			self.manager.logger.warning(f'⛔️ Failed to contain blocked navigation to {url}: {type(error).__name__}: {error}')
			if require_target_closed:
				raise

	async def close_target_with_confirmation(self, target_id: TargetID, url: str) -> None:
		"""Close one target and confirm disappearance from Chromium's target inventory."""
		cdp_client = self.manager.browser_session._cdp_client_root
		if cdp_client is None:
			raise RuntimeError(
				f'URL policy blocked {url}, but target {target_id} could not be confirmed closed: CDP client unavailable'
			)

		last_close_error: Exception | None = None
		last_inventory_error: Exception | None = None
		loop = asyncio.get_running_loop()

		for _attempt in range(self._TARGET_CLOSE_ATTEMPTS):
			try:
				await cdp_client.send.Target.closeTarget(params={'targetId': target_id})
				last_close_error = None
			except Exception as error:
				last_close_error = error

			deadline = loop.time() + self._TARGET_CLOSE_CONFIRMATION_TIMEOUT
			while True:
				try:
					targets_result = await cdp_client.send.Target.getTargets()
					last_inventory_error = None
					if not any(target_info.get('targetId') == target_id for target_info in targets_result.get('targetInfos', [])):
						return
				except Exception as error:
					last_inventory_error = error

				if loop.time() >= deadline:
					break
				await asyncio.sleep(self._TARGET_CLOSE_POLL_INTERVAL)

		details: list[str] = []
		if last_close_error is not None:
			details.append(f'last close error: {type(last_close_error).__name__}: {last_close_error}')
		if last_inventory_error is not None:
			details.append(f'last inventory error: {type(last_inventory_error).__name__}: {last_inventory_error}')
		detail_suffix = f' ({"; ".join(details)})' if details else ''
		raise RuntimeError(
			f'URL policy blocked {url}, but target {target_id} could not be confirmed closed '
			f'after {self._TARGET_CLOSE_ATTEMPTS} attempts{detail_suffix}'
		)

	async def enable_fetch_for_session(self, cdp_session: CDPSession, *, target_type: str) -> None:
		"""Enable one target-scoped Fetch configuration for policy and proxy auth."""
		proxy = self.manager.browser_session.browser_profile.proxy
		has_proxy_credentials = bool(proxy and proxy.username and proxy.password)
		needs_policy = self.active and target_type in ('page', 'tab')
		if not has_proxy_credentials and not needs_policy:
			return

		params: EnableParameters = {'handleAuthRequests': has_proxy_credentials}
		if needs_policy:
			policy_patterns: list[RequestPattern] = [{'urlPattern': '*', 'resourceType': 'Document', 'requestStage': 'Request'}]
			params['patterns'] = policy_patterns
		elif has_proxy_credentials:
			# Preserve the prior authenticated-proxy behavior. The centralized
			# requestPaused handler immediately continues these requests.
			params['patterns'] = [{'urlPattern': '*'}]

		# Chrome permits multiple flattened CDP sessions for the same target. Fetch
		# interception stacks across them, so enabling every session pauses one
		# logical request repeatedly. Give each target exactly one Fetch owner.
		async with self._fetch_setup_lock:
			current_session_id = self.fetch_sessions.get(cdp_session.target_id)
			if current_session_id is not None and current_session_id in self.manager._sessions:
				return
			await cdp_session.cdp_client.send.Fetch.enable(params=params, session_id=cdp_session.session_id)
			self.fetch_sessions[cdp_session.target_id] = cdp_session.session_id
			self.setup_failures.pop(cdp_session.target_id, None)
		self.manager.logger.debug(
			f'[SessionManager] Fetch enabled for {target_type} session {cdp_session.session_id[:8]}... '
			f'(policy={needs_policy}, proxy_auth={has_proxy_credentials})'
		)
