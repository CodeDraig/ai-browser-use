"""Click, print, and click-triggered download behavior for default browser actions."""

import asyncio

from browser_use.browser.events import ClickCoordinateEvent, ClickElementEvent
from browser_use.browser.views import BrowserError, URLNotAllowedError
from browser_use.dom.service import EnhancedDOMTreeNode


class ClickActions:
	"""Execute element and coordinate clicks while preserving click side effects."""

	def __init__(self, browser_session) -> None:
		self.browser_session = browser_session

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
		"""Check if an element is occluded by other elements at the given coordinates.

		Args:
			backend_node_id: The backend node ID of the target element
			x: X coordinate to check
			y: Y coordinate to check
			cdp_session: CDP session to use

		Returns:
			True if element is occluded, False if clickable
		"""
		try:
			session_id = cdp_session.session_id

			# Get target element info for comparison
			target_result = await cdp_session.cdp_client.send.DOM.resolveNode(
				params={'backendNodeId': backend_node_id}, session_id=session_id
			)

			if 'object' not in target_result:
				self.logger.debug('Could not resolve target element, assuming occluded')
				return True

			object_id = target_result['object']['objectId']

			# Get target element info
			target_info_result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
				params={
					'objectId': object_id,
					'functionDeclaration': """
					function() {
						const getElementInfo = (el) => {
							return {
								tagName: el.tagName,
								id: el.id || '',
								className: el.className || '',
								textContent: (el.textContent || '').substring(0, 100)
							};
						};


						const elementAtPoint = document.elementFromPoint(arguments[0], arguments[1]);
						if (!elementAtPoint) {
							return { targetInfo: getElementInfo(this), isClickable: false };
						}


						// Simple containment-based clickability logic
						let isClickable = this === elementAtPoint ||
							this.contains(elementAtPoint) ||
							elementAtPoint.contains(this);

						// Check label-input associations when containment check fails
						if (!isClickable) {
							const target = this;
							const atPoint = elementAtPoint;

							// Case 1: target is <input>, atPoint is its associated <label> (or child of that label)
							if (target.tagName === 'INPUT' && target.id) {
								const escapedId = CSS.escape(target.id);
								const assocLabel = document.querySelector('label[for="' + escapedId + '"]');
								if (assocLabel && (assocLabel === atPoint || assocLabel.contains(atPoint))) {
									isClickable = true;
								}
							}

							// Case 2: target is <input>, atPoint is inside a <label> ancestor that wraps the target
							if (!isClickable && target.tagName === 'INPUT') {
								let ancestor = atPoint;
								for (let i = 0; i < 3 && ancestor; i++) {
									if (ancestor.tagName === 'LABEL' && ancestor.contains(target)) {
										isClickable = true;
										break;
									}
									ancestor = ancestor.parentElement;
								}
							}

							// Case 3: target is <label>, atPoint is the associated <input>
							if (!isClickable && target.tagName === 'LABEL') {
								if (target.htmlFor && atPoint.tagName === 'INPUT' && atPoint.id === target.htmlFor) {
									isClickable = true;
								}
								// Also check if atPoint is an input inside the label
								if (!isClickable && atPoint.tagName === 'INPUT' && target.contains(atPoint)) {
									isClickable = true;
								}
							}
						}

						return {
							targetInfo: getElementInfo(this),
							elementAtPointInfo: getElementInfo(elementAtPoint),
							isClickable: isClickable
						};
					}
					""",
					'arguments': [{'value': x}, {'value': y}],
					'returnByValue': True,
				},
				session_id=session_id,
			)

			if 'result' not in target_info_result or 'value' not in target_info_result['result']:
				self.logger.debug('Could not get target element info, assuming occluded')
				return True

			target_data = target_info_result['result']['value']
			is_clickable = target_data.get('isClickable', False)

			if is_clickable:
				self.logger.debug('Element is clickable (target, contained, or semantically related)')
				return False
			else:
				target_info = target_data.get('targetInfo', {})
				element_at_point_info = target_data.get('elementAtPointInfo', {})
				self.logger.debug(
					f'Element is occluded. Target: {target_info.get("tagName", "unknown")} '
					f'(id={target_info.get("id", "none")}), '
					f'ElementAtPoint: {element_at_point_info.get("tagName", "unknown")} '
					f'(id={element_at_point_info.get("id", "none")})'
				)
				return True

		except Exception as e:
			self.logger.debug(f'Occlusion check failed: {e}, assuming not occluded')
			return False

	async def click_element(self, element_node) -> dict | None:
		"""
		Click an element using pure CDP with multiple fallback methods for getting element geometry.

		Args:
			element_node: The DOM element to click
		"""

		try:
			# Check if element is a file input or select dropdown - these should not be clicked
			tag_name = element_node.tag_name.lower() if element_node.tag_name else ''
			element_type = element_node.attributes.get('type', '').lower() if element_node.attributes else ''
			selector_index = self.browser_session.dom_state.get_selector_index(element_node)

			if tag_name == 'select':
				msg = f'Cannot click on <select> elements. Use dropdown_options(index={selector_index}) action instead.'
				# Return error dict instead of raising to avoid ERROR logs
				return {'validation_error': msg}

			if tag_name == 'input' and element_type == 'file':
				msg = f'Cannot click on file input element (index={selector_index}). File uploads must be handled using upload_file_to_element action.'
				# Return error dict instead of raising to avoid ERROR logs
				return {'validation_error': msg}

			# Get CDP client
			cdp_session = await self.browser_session.session_manager.cdp_client_for_node(element_node)

			# Get the correct session ID for the element's frame
			session_id = cdp_session.session_id

			# Get element bounds
			backend_node_id = element_node.backend_node_id

			# For checkbox/radio: capture pre-click state to verify toggle worked
			is_toggle_element = tag_name == 'input' and element_type in ('checkbox', 'radio')
			pre_click_checked: bool | None = None
			checkbox_object_id: str | None = None
			if is_toggle_element and backend_node_id:
				try:
					resolve_res = await cdp_session.cdp_client.send.DOM.resolveNode(
						params={'backendNodeId': backend_node_id}, session_id=session_id
					)
					obj_info = resolve_res.get('object', {})
					checkbox_object_id = obj_info.get('objectId') if obj_info else None
					if not checkbox_object_id:
						raise Exception('Failed to resolve checkbox element objectId')
					state_res = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': 'function() { return this.checked; }',
							'objectId': checkbox_object_id,
							'returnByValue': True,
						},
						session_id=session_id,
					)
					pre_click_checked = state_res.get('result', {}).get('value')
					self.logger.debug(f'Checkbox pre-click state: checked={pre_click_checked}')
				except Exception as e:
					self.logger.debug(f'Could not capture pre-click checkbox state: {e}')

			# Get viewport dimensions for visibility checks
			layout_metrics = await cdp_session.cdp_client.send.Page.getLayoutMetrics(session_id=session_id)
			viewport_width = layout_metrics['layoutViewport']['clientWidth']
			viewport_height = layout_metrics['layoutViewport']['clientHeight']

			# Scroll element into view FIRST before getting coordinates
			try:
				await cdp_session.cdp_client.send.DOM.scrollIntoViewIfNeeded(
					params={'backendNodeId': backend_node_id}, session_id=session_id
				)
				await asyncio.sleep(0.05)  # Wait for scroll to complete
				self.logger.debug('Scrolled element into view before getting coordinates')
			except Exception as e:
				self.logger.debug(f'Failed to scroll element into view: {e}')

			# Get element coordinates using the unified method AFTER scrolling
			element_rect = await self.browser_session.dom_state.get_element_coordinates(backend_node_id, cdp_session)

			# Convert rect to quads format if we got coordinates
			quads = []
			if element_rect:
				# Convert DOMRect to quad format
				x, y, w, h = element_rect.x, element_rect.y, element_rect.width, element_rect.height
				quads = [
					[
						x,
						y,  # top-left
						x + w,
						y,  # top-right
						x + w,
						y + h,  # bottom-right
						x,
						y + h,  # bottom-left
					]
				]
				self.logger.debug(
					f'Got coordinates from unified method: {element_rect.x}, {element_rect.y}, {element_rect.width}x{element_rect.height}'
				)

			# If we still don't have quads, fall back to JS click
			if not quads:
				self.logger.warning('Could not get element geometry from any method, falling back to JavaScript click')
				try:
					result = await cdp_session.cdp_client.send.DOM.resolveNode(
						params={'backendNodeId': backend_node_id},
						session_id=session_id,
					)
					assert 'object' in result and 'objectId' in result['object'], (
						'Failed to find DOM element based on backendNodeId, maybe page content changed?'
					)
					object_id = result['object']['objectId']

					await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': 'function() { this.click(); }',
							'objectId': object_id,
						},
						session_id=session_id,
					)
					await asyncio.sleep(0.05)
					# Navigation is handled by BrowserSession via events
					return None
				except Exception as js_e:
					self.logger.warning(f'CDP JavaScript click also failed: {js_e}')
					if 'No node with given id found' in str(js_e):
						raise Exception('Element with given id not found')
					else:
						raise Exception(f'Failed to click element: {js_e}')

			# Find the largest visible quad within the viewport
			best_quad = None
			best_area = 0

			for quad in quads:
				if len(quad) < 8:
					continue

				# Calculate quad bounds
				xs = [quad[i] for i in range(0, 8, 2)]
				ys = [quad[i] for i in range(1, 8, 2)]
				min_x, max_x = min(xs), max(xs)
				min_y, max_y = min(ys), max(ys)

				# Check if quad intersects with viewport
				if max_x < 0 or max_y < 0 or min_x > viewport_width or min_y > viewport_height:
					continue  # Quad is completely outside viewport

				# Calculate visible area (intersection with viewport)
				visible_min_x = max(0, min_x)
				visible_max_x = min(viewport_width, max_x)
				visible_min_y = max(0, min_y)
				visible_max_y = min(viewport_height, max_y)

				visible_width = visible_max_x - visible_min_x
				visible_height = visible_max_y - visible_min_y
				visible_area = visible_width * visible_height

				if visible_area > best_area:
					best_area = visible_area
					best_quad = quad

			if not best_quad:
				# No visible quad found, use the first quad anyway
				best_quad = quads[0]
				self.logger.warning('No visible quad found, using first quad')

			# Calculate center point of the best quad
			center_x = sum(best_quad[i] for i in range(0, 8, 2)) / 4
			center_y = sum(best_quad[i] for i in range(1, 8, 2)) / 4

			# Ensure click point is within viewport bounds
			center_x = max(0, min(viewport_width - 1, center_x))
			center_y = max(0, min(viewport_height - 1, center_y))

			# Check for occlusion before attempting CDP click
			is_occluded = await self.is_element_occluded(backend_node_id, center_x, center_y, cdp_session)

			if is_occluded:
				self.logger.debug('🚫 Element is occluded, falling back to JavaScript click')
				try:
					result = await cdp_session.cdp_client.send.DOM.resolveNode(
						params={'backendNodeId': backend_node_id},
						session_id=session_id,
					)
					assert 'object' in result and 'objectId' in result['object'], (
						'Failed to find DOM element based on backendNodeId'
					)
					object_id = result['object']['objectId']

					await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': 'function() { this.click(); }',
							'objectId': object_id,
						},
						session_id=session_id,
					)
					await asyncio.sleep(0.05)
					return None
				except Exception as js_e:
					self.logger.error(f'JavaScript click fallback failed: {js_e}')
					raise Exception(f'Failed to click occluded element: {js_e}')

			# Perform the click using CDP (element is not occluded)
			try:
				self.logger.debug(f'👆 Dragging mouse over element before clicking x: {center_x}px y: {center_y}px ...')
				# Move mouse to element
				await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
					params={
						'type': 'mouseMoved',
						'x': center_x,
						'y': center_y,
					},
					session_id=session_id,
				)
				await asyncio.sleep(0.05)

				# Mouse down
				self.logger.debug(f'👆🏾 Clicking x: {center_x}px y: {center_y}px ...')
				try:
					await asyncio.wait_for(
						cdp_session.cdp_client.send.Input.dispatchMouseEvent(
							params={
								'type': 'mousePressed',
								'x': center_x,
								'y': center_y,
								'button': 'left',
								'clickCount': 1,
							},
							session_id=session_id,
						),
						timeout=3.0,  # 3 second timeout for mousePressed
					)
					await asyncio.sleep(0.08)
				except TimeoutError:
					self.logger.debug('⏱️ Mouse down timed out (likely due to dialog), continuing...')
					# Don't sleep if we timed out

				# Mouse up
				try:
					await asyncio.wait_for(
						cdp_session.cdp_client.send.Input.dispatchMouseEvent(
							params={
								'type': 'mouseReleased',
								'x': center_x,
								'y': center_y,
								'button': 'left',
								'clickCount': 1,
							},
							session_id=session_id,
						),
						timeout=5.0,  # 5 second timeout for mouseReleased
					)
				except TimeoutError:
					self.logger.debug('⏱️ Mouse up timed out (possibly due to lag or dialog popup), continuing...')

				self.logger.debug('🖱️ Clicked successfully using x,y coordinates')

				# For checkbox/radio: verify state toggled, fall back to JS element.click() if not
				if is_toggle_element and pre_click_checked is not None and checkbox_object_id:
					try:
						await asyncio.sleep(0.05)
						state_res = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
							params={
								'functionDeclaration': 'function() { return this.checked; }',
								'objectId': checkbox_object_id,
								'returnByValue': True,
							},
							session_id=session_id,
						)
						post_click_checked = state_res.get('result', {}).get('value')
						if post_click_checked == pre_click_checked:
							# CDP mouse events didn't toggle the checkbox — try JS element.click()
							self.logger.debug(
								f'Checkbox state unchanged after CDP click (checked={pre_click_checked}), using JS fallback'
							)
							await cdp_session.cdp_client.send.Runtime.callFunctionOn(
								params={'functionDeclaration': 'function() { this.click(); }', 'objectId': checkbox_object_id},
								session_id=session_id,
							)
							await asyncio.sleep(0.05)
							final_res = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
								params={
									'functionDeclaration': 'function() { return this.checked; }',
									'objectId': checkbox_object_id,
									'returnByValue': True,
								},
								session_id=session_id,
							)
							post_click_checked = final_res.get('result', {}).get('value')
						self.logger.debug(f'Checkbox post-click state: checked={post_click_checked}')
						return {'click_x': center_x, 'click_y': center_y, 'checked': post_click_checked}
					except Exception as e:
						self.logger.debug(f'Checkbox state verification failed (non-critical): {e}')

				# Return coordinates as dict for metadata
				return {'click_x': center_x, 'click_y': center_y}

			except Exception as e:
				self.logger.warning(f'CDP click failed: {type(e).__name__}: {e}')
				# Fall back to JavaScript click via CDP
				try:
					result = await cdp_session.cdp_client.send.DOM.resolveNode(
						params={'backendNodeId': backend_node_id},
						session_id=session_id,
					)
					assert 'object' in result and 'objectId' in result['object'], (
						'Failed to find DOM element based on backendNodeId, maybe page content changed?'
					)
					object_id = result['object']['objectId']

					await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'functionDeclaration': 'function() { this.click(); }',
							'objectId': object_id,
						},
						session_id=session_id,
					)

					# Small delay for dialog dismissal
					await asyncio.sleep(0.1)

					return None
				except Exception as js_e:
					self.logger.warning(f'CDP JavaScript click also failed: {js_e}')
					raise Exception(f'Failed to click element: {e}')
			finally:
				# Always re-focus back to original top-level page session context in case click opened a new tab/popup/window/dialog/etc.
				# Use timeout to prevent hanging if dialog is blocking
				try:
					cdp_session = await asyncio.wait_for(self.browser_session.get_or_create_cdp_session(focus=True), timeout=3.0)
					await asyncio.wait_for(
						cdp_session.cdp_client.send.Runtime.runIfWaitingForDebugger(session_id=cdp_session.session_id),
						timeout=2.0,
					)
				except TimeoutError:
					self.logger.debug('⏱️ Refocus after click timed out (page may be blocked by dialog). Continuing...')
				except Exception as e:
					self.logger.debug(f'⚠️ Refocus error (non-critical): {type(e).__name__}: {e}')

		except URLNotAllowedError as e:
			raise e
		except BrowserError as e:
			raise e
		except Exception as e:
			# Extract key element info for error message
			selector_index = self.browser_session.dom_state.get_selector_index(element_node)
			element_info = f'<{element_node.tag_name or "unknown"}'
			if selector_index:
				element_info += f' index={selector_index}'
			element_info += '>'

			# Create helpful error message based on context
			error_detail = f'Failed to click element {element_info}. The element may not be interactable or visible.'

			# Add hint if element has index (common in code-use mode)
			if selector_index:
				error_detail += f' If the page changed after navigation/interaction, the index [{selector_index}] may be stale. Get fresh browser state before retrying.'

			raise BrowserError(
				message=f'Failed to click element: {str(e)}',
				long_term_memory=error_detail,
			)

	async def _click_on_coordinate(self, coordinate_x: int, coordinate_y: int, force: bool = False) -> dict | None:
		"""
		Click directly at coordinates using CDP Input.dispatchMouseEvent.

		Args:
			coordinate_x: X coordinate in viewport
			coordinate_y: Y coordinate in viewport
			force: If True, skip all safety checks (used when force=True in event)

		Returns:
			Dict with click coordinates or None
		"""
		try:
			# Get CDP session
			cdp_session = await self.browser_session.get_or_create_cdp_session()
			session_id = cdp_session.session_id

			self.logger.debug(f'👆 Moving mouse to ({coordinate_x}, {coordinate_y})...')

			# Move mouse to coordinates
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mouseMoved',
					'x': coordinate_x,
					'y': coordinate_y,
				},
				session_id=session_id,
			)
			await asyncio.sleep(0.05)

			# Mouse down
			self.logger.debug(f'👆🏾 Clicking at ({coordinate_x}, {coordinate_y})...')
			try:
				await asyncio.wait_for(
					cdp_session.cdp_client.send.Input.dispatchMouseEvent(
						params={
							'type': 'mousePressed',
							'x': coordinate_x,
							'y': coordinate_y,
							'button': 'left',
							'clickCount': 1,
						},
						session_id=session_id,
					),
					timeout=3.0,
				)
				await asyncio.sleep(0.05)
			except TimeoutError:
				self.logger.debug('⏱️ Mouse down timed out (likely due to dialog), continuing...')

			# Mouse up
			try:
				await asyncio.wait_for(
					cdp_session.cdp_client.send.Input.dispatchMouseEvent(
						params={
							'type': 'mouseReleased',
							'x': coordinate_x,
							'y': coordinate_y,
							'button': 'left',
							'clickCount': 1,
						},
						session_id=session_id,
					),
					timeout=5.0,
				)
			except TimeoutError:
				self.logger.debug('⏱️ Mouse up timed out (possibly due to lag or dialog popup), continuing...')

			self.logger.debug(f'🖱️ Clicked successfully at ({coordinate_x}, {coordinate_y})')

			# Return coordinates as metadata
			return {'click_x': coordinate_x, 'click_y': coordinate_y}

		except Exception as e:
			self.logger.error(f'Failed to click at coordinates ({coordinate_x}, {coordinate_y}): {type(e).__name__}: {e}')
			raise BrowserError(
				message=f'Failed to click at coordinates: {e}',
				long_term_memory=f'Failed to click at coordinates ({coordinate_x}, {coordinate_y}). The coordinates may be outside viewport or the page may have changed.',
			)
