from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from cdp_use.cdp.network import Cookie
from cdp_use.cdp.target import TargetID
from cdp_use.cdp.target.commands import CreateTargetParameters

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession


class BrowserCDP:
	"""Raw Chrome DevTools Protocol operations owned by one browser session."""

	def __init__(self, session: BrowserSession) -> None:
		self.session = session

	async def create_new_page(self, url: str = 'about:blank', background: bool = False, new_window: bool = False) -> str:
		"""Create a new page/tab using CDP Target.createTarget. Returns target ID."""
		# Only include newWindow when True, letting Chrome auto-create window as needed
		params = CreateTargetParameters(url=url, background=background)
		if new_window:
			params['newWindow'] = True
		# Use the root CDP client to create tabs at the browser level
		if self.session._cdp_client_root:
			result = await self.session._cdp_client_root.send.Target.createTarget(params=params)
		else:
			# Fallback to using cdp_client if root is not available
			result = await self.session.cdp_client.send.Target.createTarget(params=params)
		return result['targetId']

	async def close_page(self, target_id: TargetID) -> None:
		"""Close a page/tab using CDP Target.closeTarget."""
		await self.session.cdp_client.send.Target.closeTarget(params={'targetId': target_id})

	async def get_cookies(self) -> list[Cookie]:
		"""Get cookies using CDP Network.getCookies."""
		cdp_session = await self.session.get_or_create_cdp_session(target_id=None)
		result = await asyncio.wait_for(
			cdp_session.cdp_client.send.Storage.getCookies(session_id=cdp_session.session_id), timeout=8.0
		)
		return result.get('cookies', [])

	async def set_cookies(self, cookies: list[Cookie]) -> None:
		"""Set cookies using CDP Storage.setCookies."""
		if not self.session.agent_focus_target_id or not cookies:
			return

		cdp_session = await self.session.get_or_create_cdp_session(target_id=None)
		# Storage.setCookies expects params dict with 'cookies' key
		await cdp_session.cdp_client.send.Storage.setCookies(
			params={'cookies': cookies},  # type: ignore[arg-type]
			session_id=cdp_session.session_id,
		)

	async def clear_cookies(self) -> None:
		"""Clear all cookies using CDP Network.clearBrowserCookies."""
		cdp_session = await self.session.get_or_create_cdp_session()
		await cdp_session.cdp_client.send.Storage.clearCookies(session_id=cdp_session.session_id)

	async def set_geolocation(self, latitude: float, longitude: float, accuracy: float = 100) -> None:
		"""Set geolocation using CDP Emulation.setGeolocationOverride."""
		await self.session.cdp_client.send.Emulation.setGeolocationOverride(
			params={'latitude': latitude, 'longitude': longitude, 'accuracy': accuracy}
		)

	async def clear_geolocation(self) -> None:
		"""Clear geolocation override using CDP."""
		await self.session.cdp_client.send.Emulation.clearGeolocationOverride()

	async def add_init_script(self, script: str) -> str:
		"""Add script to evaluate on new document using CDP Page.addScriptToEvaluateOnNewDocument."""
		assert self.session._cdp_client_root is not None
		cdp_session = await self.session.get_or_create_cdp_session()

		result = await cdp_session.cdp_client.send.Page.addScriptToEvaluateOnNewDocument(
			params={'source': script, 'runImmediately': True}, session_id=cdp_session.session_id
		)
		return result['identifier']

	async def remove_init_script(self, identifier: str) -> None:
		"""Remove script added with addScriptToEvaluateOnNewDocument."""
		cdp_session = await self.session.get_or_create_cdp_session(target_id=None)
		await cdp_session.cdp_client.send.Page.removeScriptToEvaluateOnNewDocument(
			params={'identifier': identifier}, session_id=cdp_session.session_id
		)

	async def set_viewport(
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
			cdp_session = await self.session.get_or_create_cdp_session(target_id, focus=False)
		elif self.session.agent_focus_target_id:
			# Use current focus - use safe API with focus=False to avoid changing focus
			try:
				cdp_session = await self.session.get_or_create_cdp_session(self.session.agent_focus_target_id, focus=False)
			except ValueError:
				self.session.logger.warning('Cannot set viewport: focused target has no sessions')
				return
		else:
			self.session.logger.warning('Cannot set viewport: no target_id provided and agent_focus not initialized')
			return

		await cdp_session.cdp_client.send.Emulation.setDeviceMetricsOverride(
			params={'width': width, 'height': height, 'deviceScaleFactor': device_scale_factor, 'mobile': mobile},
			session_id=cdp_session.session_id,
		)

	async def get_origins(self) -> list[dict[str, Any]]:
		"""Get origins with localStorage and sessionStorage using CDP."""
		origins = []
		cdp_session = await self.session.get_or_create_cdp_session(target_id=None)

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
						self.session.logger.debug(f'Failed to get {storage_type} for {origin}: {e}')
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
			self.session.logger.warning(f'Failed to get origins: {e}')

		return origins

	async def get_storage_state(self) -> dict:
		"""Get storage state (cookies, localStorage, sessionStorage) using CDP."""
		# Use the get_cookies helper which handles session attachment
		cookies = await self.get_cookies()

		# Get origins with localStorage/sessionStorage
		origins = await self.get_origins()

		return {
			'cookies': cookies,
			'origins': origins,
		}

	async def navigate(self, url: str, target_id: TargetID | None = None) -> None:
		"""Navigate to URL using CDP Page.navigate."""
		# Use provided target_id or fall back to agent_focus_target_id

		assert self.session._cdp_client_root is not None, 'CDP client not initialized - browser may not be connected yet'
		assert self.session.agent_focus_target_id is not None, 'Agent focus not initialized - browser may not be connected yet'

		target_id_to_use = target_id or self.session.agent_focus_target_id
		cdp_session = await self.session.get_or_create_cdp_session(target_id_to_use, focus=True)

		# Use helper to navigate on the target
		await cdp_session.cdp_client.send.Page.navigate(params={'url': url}, session_id=cdp_session.session_id)
