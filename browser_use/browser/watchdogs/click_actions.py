"""Click, print, and click-triggered download behavior for default browser actions."""

import asyncio

from browser_use.actor.interaction import ElementInteractor
from browser_use.browser.events import ClickCoordinateEvent, ClickElementEvent
from browser_use.browser.views import BrowserError
from browser_use.dom.service import EnhancedDOMTreeNode


class ClickActions:
	"""Execute element and coordinate clicks while preserving click side effects."""

	def __init__(self, browser_session) -> None:
		self.browser_session = browser_session
		self.element_interactor = ElementInteractor(browser_session)

	@property
	def logger(self):
		return self.browser_session.logger

	async def _execute_click_with_download_detection(
		self,
		click_coro,
		download_start_timeout: float = 0.5,
		download_complete_timeout: float = 30.0,
	) -> dict | None:
		"""Execute a click operation and automatically wait for any triggered download

		Args:
			click_coro: Coroutine that performs the click (should return click_metadata dict or None)
			download_start_timeout: Time to wait for download to start after click (seconds)
			download_complete_timeout: Time to wait for download to complete once started (seconds)

		Returns:
			Click metadata dict, potentially with 'download' key containing download info.
			If a download times out but is still in progress, includes 'download_in_progress' with status.
		"""
		import time

		download_started = asyncio.Event()
		download_completed = asyncio.Event()
		download_info: dict = {}
		progress_info: dict = {'last_update': 0.0, 'received_bytes': 0, 'total_bytes': 0, 'state': ''}

		def on_download_start(info: dict) -> None:
			"""Direct callback when download starts (called from CDP handler)."""
			if info.get('auto_download'):
				return  # ignore auto-downloads
			download_info['guid'] = info.get('guid', '')
			download_info['url'] = info.get('url', '')
			download_info['suggested_filename'] = info.get('suggested_filename', 'download')
			download_started.set()
			self.logger.debug(f'[ClickWithDownload] Download started: {download_info["suggested_filename"]}')

		def on_download_progress(info: dict) -> None:
			"""Direct callback when download progress updates (called from CDP handler)."""
			# Match by guid if available
			if download_info.get('guid') and info.get('guid') != download_info['guid']:
				return  # different download
			progress_info['last_update'] = time.time()
			progress_info['received_bytes'] = info.get('received_bytes', 0)
			progress_info['total_bytes'] = info.get('total_bytes', 0)
			progress_info['state'] = info.get('state', '')
			self.logger.debug(
				f'[ClickWithDownload] Progress: {progress_info["received_bytes"]}/{progress_info["total_bytes"]} bytes ({progress_info["state"]})'
			)

		def on_download_complete(info: dict) -> None:
			"""Direct callback when download completes (called from CDP handler)."""
			if info.get('auto_download'):
				return  # ignore auto-downloads
			# Match by guid if available, otherwise accept any non-auto download
			if download_info.get('guid') and info.get('guid') and info.get('guid') != download_info['guid']:
				return  # different download
			download_info['path'] = info.get('path', '')
			download_info['file_name'] = info.get('file_name', '')
			download_info['file_size'] = info.get('file_size', 0)
			download_info['file_type'] = info.get('file_type')
			download_info['mime_type'] = info.get('mime_type')
			download_completed.set()
			self.logger.debug(f'[ClickWithDownload] Download completed: {download_info["file_name"]}')

		# Get the downloads watchdog and register direct callbacks
		downloads_watchdog = self.browser_session.watchdogs.downloads
		self.logger.debug(f'[ClickWithDownload] downloads_watchdog={downloads_watchdog is not None}')
		if downloads_watchdog:
			self.logger.debug('[ClickWithDownload] Registering download callbacks...')
			downloads_watchdog.register_download_callbacks(
				on_start=on_download_start,
				on_progress=on_download_progress,
				on_complete=on_download_complete,
			)
		else:
			self.logger.warning('[ClickWithDownload] No downloads_watchdog available!')

		try:
			# Perform the click
			click_metadata = await click_coro

			# Check for validation errors - return them immediately without waiting for downloads
			if isinstance(click_metadata, dict) and 'validation_error' in click_metadata:
				return click_metadata

			# Wait briefly to see if a download starts
			try:
				await asyncio.wait_for(download_started.wait(), timeout=download_start_timeout)

				# Download started!
				self.logger.info(f'📥 Download started: {download_info.get("suggested_filename", "unknown")}')

				# Now wait for it to complete with longer timeout
				try:
					await asyncio.wait_for(download_completed.wait(), timeout=download_complete_timeout)

					# Download completed successfully
					msg = f'Downloaded file: {download_info["file_name"]} ({download_info["file_size"]} bytes) saved to {download_info["path"]}'
					self.logger.info(f'💾 {msg}')

					# Merge download info into click_metadata
					if click_metadata is None:
						click_metadata = {}
					click_metadata['download'] = {
						'path': download_info['path'],
						'file_name': download_info['file_name'],
						'file_size': download_info['file_size'],
						'file_type': download_info.get('file_type'),
						'mime_type': download_info.get('mime_type'),
					}
				except TimeoutError:
					# Download timed out - check if it's still in progress
					if click_metadata is None:
						click_metadata = {}

					filename = download_info.get('suggested_filename', 'unknown')
					received = progress_info.get('received_bytes', 0)
					total = progress_info.get('total_bytes', 0)
					state = progress_info.get('state', 'unknown')
					last_update = progress_info.get('last_update', 0.0)
					time_since_update = time.time() - last_update if last_update > 0 else float('inf')

					# Check if download is still actively progressing (received update in last 5 seconds)
					is_still_active = time_since_update < 5.0 and state == 'inProgress'

					if is_still_active:
						# Download is still progressing - suggest waiting
						if total > 0:
							percent = (received / total) * 100
							progress_str = f'{percent:.1f}% ({received:,}/{total:,} bytes)'
						else:
							progress_str = f'{received:,} bytes downloaded (total size unknown)'

						msg = (
							f'Download timed out after {download_complete_timeout}s but is still in progress: '
							f'{filename} - {progress_str}. '
							f'The download appears to be progressing normally. Consider using the wait action '
							f'to allow more time for the download to complete.'
						)
						self.logger.warning(f'⏱️ {msg}')
						click_metadata['download_in_progress'] = {
							'file_name': filename,
							'received_bytes': received,
							'total_bytes': total,
							'state': state,
							'message': msg,
						}
					else:
						# Download may be stalled or completed
						if received > 0:
							msg = (
								f'Download timed out after {download_complete_timeout}s: {filename}. '
								f'Last progress: {received:,} bytes received. '
								f'The download may have stalled or completed - check the downloads folder.'
							)
						else:
							msg = (
								f'Download timed out after {download_complete_timeout}s: {filename}. '
								f'No progress data received - the download may have failed to start properly.'
							)
						self.logger.warning(f'⏱️ {msg}')
						click_metadata['download_timeout'] = {
							'file_name': filename,
							'received_bytes': received,
							'total_bytes': total,
							'message': msg,
						}
			except TimeoutError:
				# No download started within grace period
				pass

			return click_metadata if isinstance(click_metadata, dict) else None

		finally:
			# Unregister download callbacks
			if downloads_watchdog:
				downloads_watchdog.unregister_download_callbacks(
					on_start=on_download_start,
					on_progress=on_download_progress,
					on_complete=on_download_complete,
				)

	def _is_print_related_element(self, element_node: EnhancedDOMTreeNode) -> bool:
		"""Check if an element is related to printing (print buttons, print dialogs, etc.).

		Primary check: onclick attribute (most reliable for print detection)
		Fallback: button text/value (for cases without onclick)
		"""
		# Primary: Check onclick attribute for print-related functions (most reliable)
		onclick = element_node.attributes.get('onclick', '').lower() if element_node.attributes else ''
		if onclick and 'print' in onclick:
			# Matches: window.print(), PrintElem(), print(), etc.
			return True

		return False

	async def _handle_print_button_click(self, element_node: EnhancedDOMTreeNode) -> dict | None:
		"""Handle print button by directly generating PDF via CDP instead of opening dialog.

		Returns:
			Metadata dict with download path if successful, None otherwise
		"""
		try:
			import base64
			import os
			from pathlib import Path

			# Get CDP session
			cdp_session = await self.browser_session.get_or_create_cdp_session(focus=True)

			# Generate PDF using CDP Page.printToPDF
			result = await asyncio.wait_for(
				cdp_session.cdp_client.send.Page.printToPDF(
					params={
						'printBackground': True,
						'preferCSSPageSize': True,
					},
					session_id=cdp_session.session_id,
				),
				timeout=15.0,  # 15 second timeout for PDF generation
			)

			pdf_data = result.get('data')
			if not pdf_data:
				self.logger.warning('⚠️ PDF generation returned no data')
				return None

			# Decode base64 PDF data
			pdf_bytes = base64.b64decode(pdf_data)

			# Get downloads path
			downloads_path = self.browser_session.browser_profile.downloads_path
			if not downloads_path:
				self.logger.warning('⚠️ No downloads path configured, cannot save PDF')
				return None

			# Generate filename from page title or URL
			try:
				page_title = await asyncio.wait_for(self.browser_session.get_current_page_title(), timeout=2.0)
				# Sanitize title for filename
				import re

				safe_title = re.sub(r'[^\w\s-]', '', page_title)[:50]  # Max 50 chars
				filename = f'{safe_title}.pdf' if safe_title else 'print.pdf'
			except Exception:
				filename = 'print.pdf'

			# Ensure downloads directory exists
			downloads_dir = Path(downloads_path).expanduser().resolve()
			downloads_dir.mkdir(parents=True, exist_ok=True)

			# Generate unique filename if file exists
			final_path = downloads_dir / filename
			if final_path.exists():
				base, ext = os.path.splitext(filename)
				counter = 1
				while (downloads_dir / f'{base} ({counter}){ext}').exists():
					counter += 1
				final_path = downloads_dir / f'{base} ({counter}){ext}'

			# Write PDF to file
			import anyio

			async with await anyio.open_file(final_path, 'wb') as f:
				await f.write(pdf_bytes)

			file_size = final_path.stat().st_size
			self.logger.info(f'✅ Generated PDF via CDP: {final_path} ({file_size:,} bytes)')

			# Dispatch FileDownloadedEvent
			from browser_use.browser.events import FileDownloadedEvent

			page_url = await self.browser_session.get_current_page_url()
			self.browser_session.event_bus.dispatch(
				FileDownloadedEvent(
					url=page_url,
					path=str(final_path),
					file_name=final_path.name,
					file_size=file_size,
					file_type='pdf',
					mime_type='application/pdf',
					auto_download=False,  # This was intentional (user clicked print)
				)
			)

			return {'pdf_generated': True, 'path': str(final_path)}

		except TimeoutError:
			self.logger.warning('⏱️ PDF generation timed out')
			return None
		except Exception as e:
			self.logger.warning(f'⚠️ Failed to generate PDF via CDP: {type(e).__name__}: {e}')
			return None

	async def handle_click_element(self, event: ClickElementEvent) -> dict | None:
		"""Handle click request with CDP. Automatically waits for file downloads if triggered."""
		try:
			# Check if session is alive before attempting any operations
			if not self.browser_session.agent_focus_target_id:
				error_msg = 'Cannot execute click: browser session is corrupted (target_id=None). Session may have crashed.'
				self.logger.error(f'{error_msg}')
				raise BrowserError(error_msg)

			# Use the provided node
			element_node = event.node
			index_for_logging = self.browser_session.dom_state.get_selector_index(element_node)

			# Check if element is a file input (should not be clicked)
			if self.browser_session.dom_state.is_file_input(element_node):
				msg = f'Index {index_for_logging} - has an element which opens file upload dialog. To upload files please use a specific function to upload files'
				self.logger.info(f'{msg}')
				return {'validation_error': msg}

			# Detect print-related elements and handle them specially
			is_print_element = self._is_print_related_element(element_node)
			if is_print_element:
				self.logger.info(
					f'🖨️ Detected print button (index {index_for_logging}), generating PDF directly instead of opening dialog...'
				)
				click_metadata = await self._handle_print_button_click(element_node)
				if click_metadata and click_metadata.get('pdf_generated'):
					msg = f'Generated PDF: {click_metadata.get("path")}'
					self.logger.info(f'💾 {msg}')
					return click_metadata
				else:
					self.logger.warning('⚠️ PDF generation failed, falling back to regular click')

			# Execute click with automatic download detection
			click_metadata = await self._execute_click_with_download_detection(self.click_element(element_node))

			# Check for validation errors
			if isinstance(click_metadata, dict) and 'validation_error' in click_metadata:
				self.logger.info(f'{click_metadata["validation_error"]}')
				return click_metadata

			# Build success message for non-download clicks
			if 'download' not in (click_metadata or {}):
				msg = f'Clicked button {element_node.node_name}: {element_node.get_all_children_text(max_depth=2)}'
				self.logger.debug(f'🖱️ {msg}')
			self.logger.debug(f'Element xpath: {element_node.xpath}')

			return click_metadata

		except Exception:
			raise

	async def handle_click_coordinate(self, event: ClickCoordinateEvent) -> dict | None:
		"""Handle click at coordinates with CDP. Automatically waits for file downloads if triggered."""
		try:
			# Check if session is alive before attempting any operations
			if not self.browser_session.agent_focus_target_id:
				error_msg = 'Cannot execute click: browser session is corrupted (target_id=None). Session may have crashed.'
				self.logger.error(f'{error_msg}')
				raise BrowserError(error_msg)

			# If force=True, skip safety checks and click directly (with download detection)
			if event.force:
				self.logger.debug(f'Force clicking at coordinates ({event.coordinate_x}, {event.coordinate_y})')
				return await self._execute_click_with_download_detection(
					self._click_on_coordinate(event.coordinate_x, event.coordinate_y, force=True)
				)

			# Get element at coordinates for safety checks
			element_node = await self.browser_session.dom_state.get_dom_element_at_coordinates(
				event.coordinate_x, event.coordinate_y
			)
			if element_node is None:
				# No element found, click directly (with download detection)
				self.logger.debug(
					f'No element found at coordinates ({event.coordinate_x}, {event.coordinate_y}), proceeding with click anyway'
				)
				return await self._execute_click_with_download_detection(
					self._click_on_coordinate(event.coordinate_x, event.coordinate_y, force=False)
				)

			# Safety check: file input
			if self.browser_session.dom_state.is_file_input(element_node):
				msg = f'Cannot click at ({event.coordinate_x}, {event.coordinate_y}) - element is a file input. To upload files please use upload_file action'
				self.logger.info(f'{msg}')
				return {'validation_error': msg}

			# Safety check: select element
			tag_name = element_node.tag_name.lower() if element_node.tag_name else ''
			if tag_name == 'select':
				msg = f'Cannot click at ({event.coordinate_x}, {event.coordinate_y}) - element is a <select>. Use dropdown_options action instead.'
				self.logger.info(f'{msg}')
				return {'validation_error': msg}

			# Safety check: print-related elements
			is_print_element = self._is_print_related_element(element_node)
			if is_print_element:
				self.logger.info(
					f'🖨️ Detected print button at ({event.coordinate_x}, {event.coordinate_y}), generating PDF directly instead of opening dialog...'
				)
				click_metadata = await self._handle_print_button_click(element_node)
				if click_metadata and click_metadata.get('pdf_generated'):
					msg = f'Generated PDF: {click_metadata.get("path")}'
					self.logger.info(f'💾 {msg}')
					return click_metadata
				else:
					self.logger.warning('⚠️ PDF generation failed, falling back to regular click')

			# All safety checks passed, click at coordinates (with download detection)
			return await self._execute_click_with_download_detection(
				self._click_on_coordinate(event.coordinate_x, event.coordinate_y, force=False)
			)

		except Exception:
			raise

	async def is_element_occluded(self, backend_node_id: int, x: float, y: float, cdp_session) -> bool:
		return await self.element_interactor.is_element_occluded(backend_node_id, x, y, cdp_session)

	async def click_element(self, element_node) -> dict | None:
		return await self.element_interactor.click_element(element_node)

	async def _click_on_coordinate(self, coordinate_x: int, coordinate_y: int, force: bool = False) -> dict | None:
		return await self.element_interactor.click_coordinate(coordinate_x, coordinate_y, force=force)
