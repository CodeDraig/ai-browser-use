from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import anyio
from cdp_use.cdp.target import TargetID

from browser_use.browser.events import FileDownloadedEvent
from browser_use.browser.watchdogs.download_paths import is_path_contained

if TYPE_CHECKING:
	from browser_use.browser.watchdogs.downloads_watchdog import DownloadsWatchdog


class PdfDownloadController:
	"""Detect browser PDF viewers and persist their source document."""

	def __init__(self, watchdog: DownloadsWatchdog) -> None:
		self.watchdog = watchdog

	async def check_for_pdf_viewer(self, target_id: TargetID) -> bool:
		"""Check if the current target is a PDF using network-based detection.

		This method avoids JavaScript execution that can crash WebSocket connections.
		Returns True if a PDF is detected and should be downloaded.
		"""
		self.watchdog.logger.debug(f'[DownloadsWatchdog] Checking if target {target_id} is PDF viewer...')

		# Use safe API - focus=False to avoid changing focus during PDF check
		try:
			session = await self.watchdog.browser_session.get_or_create_cdp_session(target_id, focus=False)
		except ValueError as e:
			self.watchdog.logger.warning(f'[DownloadsWatchdog] No session found for {target_id}: {e}')
			return False

		# Get URL from target
		target = self.watchdog.browser_session.session_manager.get_target(target_id)
		if not target:
			self.watchdog.logger.warning(f'[DownloadsWatchdog] No target found for {target_id}')
			return False
		page_url = target.url

		# Check cache first
		if page_url in self.watchdog._pdf_viewer_cache:
			cached_result = self.watchdog._pdf_viewer_cache[page_url]
			self.watchdog.logger.debug(f'[DownloadsWatchdog] Using cached PDF check result for {page_url}: {cached_result}')
			return cached_result

		try:
			# Method 1: Check URL patterns (fastest, most reliable)
			url_is_pdf = self._check_url_for_pdf(page_url)
			if url_is_pdf:
				self.watchdog.logger.debug(f'[DownloadsWatchdog] PDF detected via URL pattern: {page_url}')
				self.watchdog._pdf_viewer_cache[page_url] = True
				return True
			chrome_pdf_viewer = self._is_chrome_pdf_viewer_url(page_url)
			if chrome_pdf_viewer:
				self.watchdog.logger.debug(f'[DownloadsWatchdog] Chrome PDF viewer detected: {page_url}')
				self.watchdog._pdf_viewer_cache[page_url] = True
				return True

			# Not a PDF
			self.watchdog._pdf_viewer_cache[page_url] = False
			return False

		except Exception as e:
			self.watchdog.logger.warning(f'[DownloadsWatchdog] ❌ Error checking for PDF viewer: {e}')
			self.watchdog._pdf_viewer_cache[page_url] = False
			return False

	def _check_url_for_pdf(self, url: str) -> bool:
		"""Check if URL indicates a PDF file."""
		if not url:
			return False

		url_lower = url.lower()

		# Direct PDF file extensions
		if url_lower.endswith('.pdf'):
			return True

		# PDF in path
		if '.pdf' in url_lower:
			return True

		# PDF MIME type in URL parameters
		if any(
			param in url_lower
			for param in [
				'content-type=application/pdf',
				'content-type=application%2fpdf',
				'mimetype=application/pdf',
				'type=application/pdf',
			]
		):
			return True

		return False

	def _is_chrome_pdf_viewer_url(self, url: str) -> bool:
		"""Check if this is Chrome's internal PDF viewer URL."""
		if not url:
			return False

		url_lower = url.lower()

		# Chrome PDF viewer uses chrome-extension:// URLs
		if 'chrome-extension://' in url_lower and 'pdf' in url_lower:
			return True

		# Chrome PDF viewer internal URLs
		if url_lower.startswith('chrome://') and 'pdf' in url_lower:
			return True

		return False

	async def _check_network_headers_for_pdf(self, target_id: TargetID) -> bool:
		"""Infer PDF via navigation history/URL; headers are not available post-navigation in this context."""
		try:
			import asyncio

			# Get CDP session
			temp_session = await self.watchdog.browser_session.get_or_create_cdp_session(target_id, focus=False)

			# Get navigation history to find the main resource
			history = await asyncio.wait_for(
				temp_session.cdp_client.send.Page.getNavigationHistory(session_id=temp_session.session_id), timeout=3.0
			)

			current_entry = history.get('entries', [])
			if current_entry:
				current_index = history.get('currentIndex', 0)
				if 0 <= current_index < len(current_entry):
					current_url = current_entry[current_index].get('url', '')

					# Check if the URL itself suggests PDF
					if self._check_url_for_pdf(current_url):
						return True

			# Note: CDP doesn't easily expose response headers for completed navigations
			# For more complex cases, we'd need to set up Network.responseReceived listeners
			# before navigation, but that's overkill for most PDF detection cases

			return False

		except Exception as e:
			self.watchdog.logger.debug(f'[DownloadsWatchdog] Network headers check failed (non-critical): {e}')
			return False

	async def trigger_pdf_download(self, target_id: TargetID) -> str | None:
		"""Trigger download of a PDF from Chrome's PDF viewer.

		Returns the download path if successful, None otherwise.
		"""
		self.watchdog.logger.debug(f'[DownloadsWatchdog] trigger_pdf_download called for target_id={target_id}')

		if not self.watchdog.browser_session.browser_profile.downloads_path:
			self.watchdog.logger.warning('[DownloadsWatchdog] ❌ No downloads path configured, cannot save PDF download')
			return None

		downloads_path = self.watchdog.browser_session.browser_profile.downloads_path
		self.watchdog.logger.debug(f'[DownloadsWatchdog] Downloads path: {downloads_path}')

		try:
			# Create a temporary CDP session for this target without switching focus
			import asyncio

			self.watchdog.logger.debug(f'[DownloadsWatchdog] Creating CDP session for PDF download from target {target_id}')
			temp_session = await self.watchdog.browser_session.get_or_create_cdp_session(target_id, focus=False)

			# Try to get the PDF URL with timeout
			result = await asyncio.wait_for(
				temp_session.cdp_client.send.Runtime.evaluate(
					params={
						'expression': """
				(() => {
					// For Chrome's PDF viewer, the actual URL is in window.location.href
					// The embed element's src is often "about:blank"
					const embedElement = document.querySelector('embed[type="application/x-google-chrome-pdf"]') ||
										document.querySelector('embed[type="application/pdf"]');
					if (embedElement) {
						// Chrome PDF viewer detected - use the page URL
						return { url: window.location.href };
					}
					// Fallback to window.location.href anyway
					return { url: window.location.href };
				})()
				""",
						'returnByValue': True,
					},
					session_id=temp_session.session_id,
				),
				timeout=5.0,  # 5 second timeout to prevent hanging
			)
			pdf_info = result.get('result', {}).get('value', {})

			pdf_url = pdf_info.get('url', '')
			if not pdf_url:
				self.watchdog.logger.warning(f'[DownloadsWatchdog] ❌ Could not determine PDF URL for download {pdf_info}')
				return None

			# Generate filename from URL
			pdf_filename = os.path.basename(pdf_url.split('?')[0])  # Remove query params
			if not pdf_filename or not pdf_filename.endswith('.pdf'):
				parsed = urlparse(pdf_url)
				pdf_filename = os.path.basename(parsed.path) or 'document.pdf'
				if not pdf_filename.endswith('.pdf'):
					pdf_filename += '.pdf'

			self.watchdog.logger.debug(f'[DownloadsWatchdog] Generated filename: {pdf_filename}')

			# Check if already downloaded in this session
			self.watchdog.logger.debug(
				f'[DownloadsWatchdog] PDF_URL: {pdf_url}, session_pdf_urls: {self.watchdog._session_pdf_urls}'
			)
			if pdf_url in self.watchdog._session_pdf_urls:
				existing_path = self.watchdog._session_pdf_urls[pdf_url]
				self.watchdog.logger.debug(f'[DownloadsWatchdog] PDF already downloaded in session: {existing_path}')
				return existing_path

			# Generate unique filename if file exists from previous run
			downloads_dir = str(self.watchdog.browser_session.browser_profile.downloads_path)
			os.makedirs(downloads_dir, exist_ok=True)
			final_filename = pdf_filename
			existing_files = os.listdir(downloads_dir)
			if pdf_filename in existing_files:
				# Generate unique name with (1), (2), etc.
				base, ext = os.path.splitext(pdf_filename)
				counter = 1
				while f'{base} ({counter}){ext}' in existing_files:
					counter += 1
				final_filename = f'{base} ({counter}){ext}'
				self.watchdog.logger.debug(f'[DownloadsWatchdog] File exists, using: {final_filename}')

			self.watchdog.logger.debug(f'[DownloadsWatchdog] Starting PDF download from: {pdf_url[:100]}...')

			# Download using JavaScript fetch to leverage browser cache
			try:
				# Properly escape the URL to prevent JavaScript injection
				escaped_pdf_url = json.dumps(pdf_url)

				result = await asyncio.wait_for(
					temp_session.cdp_client.send.Runtime.evaluate(
						params={
							'expression': f"""
					(async () => {{
						try {{
							// Use fetch with cache: 'force-cache' to prioritize cached version
							const response = await fetch({escaped_pdf_url}, {{
								cache: 'force-cache'
							}});
							if (!response.ok) {{
								throw new Error(`HTTP error! status: ${{response.status}}`);
							}}
							const blob = await response.blob();
							const arrayBuffer = await blob.arrayBuffer();
							const uint8Array = new Uint8Array(arrayBuffer);
							
							// Check if served from cache
							const fromCache = response.headers.has('age') || 
											 !response.headers.has('date');
											 
							return {{ 
								data: Array.from(uint8Array),
								fromCache: fromCache,
								responseSize: uint8Array.length,
								transferSize: response.headers.get('content-length') || 'unknown'
							}};
						}} catch (error) {{
							throw new Error(`Fetch failed: ${{error.message}}`);
						}}
					}})()
					""",
							'awaitPromise': True,
							'returnByValue': True,
						},
						session_id=temp_session.session_id,
					),
					timeout=10.0,  # 10 second timeout for download operation
				)
				download_result = result.get('result', {}).get('value', {})

				if download_result and download_result.get('data') and len(download_result['data']) > 0:
					# Ensure downloads directory exists
					downloads_dir = str(self.watchdog.browser_session.browser_profile.downloads_path)
					os.makedirs(downloads_dir, exist_ok=True)
					download_path = os.path.join(downloads_dir, final_filename)
					if not is_path_contained(download_path, downloads_dir):
						self.watchdog.logger.error(
							f'[DownloadsWatchdog] Refusing to write PDF outside downloads_dir: {download_path}'
						)
						return None

					# Save the PDF asynchronously
					async with await anyio.open_file(download_path, 'wb') as f:
						await f.write(bytes(download_result['data']))

					# Verify file was written successfully
					if os.path.exists(download_path):
						actual_size = os.path.getsize(download_path)
						self.watchdog.logger.debug(
							f'[DownloadsWatchdog] PDF file written successfully: {download_path} ({actual_size} bytes)'
						)
					else:
						self.watchdog.logger.error(f'[DownloadsWatchdog] ❌ Failed to write PDF file to: {download_path}')
						return None

					# Log cache information
					cache_status = 'from cache' if download_result.get('fromCache') else 'from network'
					response_size = download_result.get('responseSize', 0)
					self.watchdog.logger.debug(
						f'[DownloadsWatchdog] ✅ Auto-downloaded PDF ({cache_status}, {response_size:,} bytes): {download_path}'
					)

					# Store URL->path mapping for this session
					self.watchdog._session_pdf_urls[pdf_url] = download_path

					# Emit file downloaded event
					self.watchdog.logger.debug(f'[DownloadsWatchdog] Dispatching FileDownloadedEvent for {final_filename}')
					self.watchdog.event_bus.dispatch(
						FileDownloadedEvent(
							url=pdf_url,
							path=download_path,
							file_name=final_filename,
							file_size=response_size,
							file_type='pdf',
							mime_type='application/pdf',
							from_cache=download_result.get('fromCache', False),
							auto_download=True,
						)
					)

					# No need to detach - session is cached
					return download_path
				else:
					self.watchdog.logger.warning(f'[DownloadsWatchdog] No data received when downloading PDF from {pdf_url}')
					return None

			except Exception as e:
				self.watchdog.logger.warning(
					f'[DownloadsWatchdog] Failed to auto-download PDF from {pdf_url}: {type(e).__name__}: {e}'
				)
				return None

		except TimeoutError:
			self.watchdog.logger.debug('[DownloadsWatchdog] PDF download operation timed out')
			return None
		except Exception as e:
			self.watchdog.logger.error(f'[DownloadsWatchdog] Error in PDF download: {type(e).__name__}: {e}')
			return None
