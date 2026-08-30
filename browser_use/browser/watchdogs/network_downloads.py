from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import anyio
from cdp_use.cdp.network import ResponseReceivedEvent
from cdp_use.cdp.target import TargetID

from browser_use.browser.events import FileDownloadedEvent
from browser_use.browser.watchdogs.download_paths import is_path_contained, sanitize_download_filename
from browser_use.runtime import create_task_with_error_handling

if TYPE_CHECKING:
	from browser_use.browser.watchdogs.downloads_watchdog import DownloadsWatchdog


_NETWORK_DOWNLOAD_FILE_EXTENSIONS = {
	'pdf',
	'doc',
	'docx',
	'xls',
	'xlsx',
	'ppt',
	'pptx',
	'csv',
	'tsv',
	'txt',
	'json',
	'xml',
	'zip',
	'gz',
	'tar',
	'jpg',
	'jpeg',
	'png',
	'gif',
	'webp',
}
_GENERIC_TEXT_ATTACHMENT_NAMES = {'f', 'download', 'response', 'data', 'callback'}


def _filename_from_content_disposition(content_disposition: str) -> str | None:
	filename_match = re.search(r"""filename[^;=\n]*=((['"]).*?\2|[^;\n]*)""", content_disposition)
	if filename_match:
		return filename_match.group(1).strip('\'"')
	return None


def _has_file_extension(value: str | None) -> bool:
	if not value:
		return False
	return Path(urlparse(value).path if '://' in value else value).suffix.lower().lstrip('.') in _NETWORK_DOWNLOAD_FILE_EXTENSIONS


def _is_generic_text_attachment(url: str, content_type: str, suggested_filename: str | None) -> bool:
	mime = content_type.split(';', 1)[0].strip().lower()
	if mime not in {'text/plain', 'application/json', 'text/javascript', 'application/javascript'}:
		return False
	if _has_file_extension(url):
		return False
	if not suggested_filename:
		return False
	filename = Path(suggested_filename).name.lower()
	stem = Path(filename).stem
	ext = Path(filename).suffix.lower().lstrip('.')
	return stem in _GENERIC_TEXT_ATTACHMENT_NAMES and ext in {'', 'txt', 'json'}


def should_auto_download_network_response(
	url: str,
	content_type: str,
	is_pdf: bool,
	is_download_attachment: bool,
	suggested_filename: str | None,
) -> bool:
	if is_pdf:
		return True
	if not is_download_attachment:
		return False
	if _is_generic_text_attachment(url, content_type, suggested_filename):
		return False
	return True


class NetworkDownloadMonitor:
	"""Detect and acquire downloadable network responses for monitored targets."""

	def __init__(self, watchdog: DownloadsWatchdog) -> None:
		self.watchdog = watchdog

	async def setup(self, target_id: TargetID) -> None:
		"""Set up network monitoring to detect PDFs and downloads from ALL sources.

		This catches:
		- Direct PDF navigation
		- PDFs in iframes
		- PDFs with embed/object tags
		- JavaScript-triggered downloads
		- Any Content-Disposition: attachment headers
		"""
		# Skip if already monitoring this target
		if target_id in self.watchdog._network_monitored_targets:
			self.watchdog.logger.debug(f'[DownloadsWatchdog] Network monitoring already enabled for target {target_id[-4:]}')
			return

		# Check if auto-download is enabled
		if not self.watchdog._is_auto_download_enabled():
			self.watchdog.logger.debug('[DownloadsWatchdog] Auto-download disabled, skipping network monitoring')
			return

		try:
			cdp_client = self.watchdog.browser_session.cdp_client

			# Register the global callback once
			if not self.watchdog._network_callback_registered:

				def on_response_received(event: ResponseReceivedEvent, session_id: str | None) -> None:
					"""Handle Network.responseReceived event to detect downloadable content.

					This callback is registered globally and uses session_id to determine the correct target.
					"""
					try:
						# Check if session_manager exists (may be None during browser shutdown)
						if not self.watchdog.browser_session.session_manager:
							self.watchdog.logger.warning(
								'[DownloadsWatchdog] Session manager not found, skipping network monitoring'
							)
							return

						# Look up target_id from session_id
						event_target_id = self.watchdog.browser_session.session_manager.get_target_id_from_session_id(session_id)
						if not event_target_id:
							# Session not in pool - might be a stale session or not yet tracked
							return

						# Only process events for targets we're monitoring
						if event_target_id not in self.watchdog._network_monitored_targets:
							return

						response = event.get('response', {})
						url = response.get('url', '')
						content_type = response.get('mimeType', '').lower()
						headers = {
							k.lower(): v for k, v in response.get('headers', {}).items()
						}  # Normalize for case-insensitive lookup
						request_type = event.get('type', '')

						# Skip non-HTTP URLs (data:, about:, chrome-extension:, etc.)
						if not url.startswith('http'):
							return

						# Skip fetch/XHR - real browsers don't download PDFs from programmatic requests
						if request_type in ('Fetch', 'XHR'):
							return

						# Check if it's a PDF
						is_pdf = 'application/pdf' in content_type

						# Check if it's marked as download via Content-Disposition header
						content_disposition = str(headers.get('content-disposition', '')).lower()
						is_download_attachment = 'attachment' in content_disposition

						# Filter out image/video/audio files even if marked as attachment
						# These are likely resources, not intentional downloads
						unwanted_content_types = [
							'image/',
							'video/',
							'audio/',
							'text/css',
							'text/javascript',
							'application/javascript',
							'application/x-javascript',
							'text/html',
							'application/json',
							'font/',
							'application/font',
							'application/x-font',
						]
						is_unwanted_type = any(content_type.startswith(prefix) for prefix in unwanted_content_types)
						if is_unwanted_type:
							return

						# Check URL extension to filter out obvious images/resources
						url_lower = url.lower().split('?')[0]  # Remove query params
						unwanted_extensions = [
							'.jpg',
							'.jpeg',
							'.png',
							'.gif',
							'.webp',
							'.svg',
							'.ico',
							'.css',
							'.js',
							'.woff',
							'.woff2',
							'.ttf',
							'.eot',
							'.mp4',
							'.webm',
							'.mp3',
							'.wav',
							'.ogg',
						]
						if any(url_lower.endswith(ext) for ext in unwanted_extensions):
							return

						# Only process if it's a PDF or download
						if not (is_pdf or is_download_attachment):
							return

						# Extract filename from Content-Disposition if available
						suggested_filename = _filename_from_content_disposition(content_disposition)

						if not should_auto_download_network_response(
							url=url,
							content_type=content_type,
							is_pdf=is_pdf,
							is_download_attachment=is_download_attachment,
							suggested_filename=suggested_filename,
						):
							return

						# If already downloaded this URL and file still exists, do nothing
						existing_path = self.watchdog._session_pdf_urls.get(url)
						if existing_path:
							if os.path.exists(existing_path):
								return
							# Stale cache entry, allow re-download
							del self.watchdog._session_pdf_urls[url]

						# Check if we've already processed this URL in this session
						if url in self.watchdog._detected_downloads:
							self.watchdog.logger.debug(f'[DownloadsWatchdog] Already detected download: {url[:80]}...')
							return

						# Mark as detected to avoid duplicates
						self.watchdog._detected_downloads.add(url)

						self.watchdog.logger.info(
							f'[DownloadsWatchdog] 🔍 Detected downloadable content via network: {url[:80]}...'
						)
						self.watchdog.logger.debug(
							f'[DownloadsWatchdog]   Content-Type: {content_type}, Is PDF: {is_pdf}, Is Attachment: {is_download_attachment}'
						)

						# Trigger download asynchronously in background (don't block event handler)
						async def download_in_background():
							# Don't permanently block re-processing this URL if download fails
							try:
								download_path = await self.download_file_from_url(
									url=url,
									target_id=event_target_id,  # Use target_id from session_id lookup
									content_type=content_type,
									suggested_filename=suggested_filename,
								)

								if download_path:
									self.watchdog.logger.info(f'[DownloadsWatchdog] ✅ Successfully downloaded: {download_path}')
								else:
									self.watchdog.logger.warning(f'[DownloadsWatchdog] ⚠️  Failed to download: {url[:80]}...')
							except Exception as e:
								self.watchdog.logger.error(
									f'[DownloadsWatchdog] Error downloading in background: {type(e).__name__}: {e}'
								)
							finally:
								# Allow future detections of the same URL
								self.watchdog._detected_downloads.discard(url)

						# Create background task
						task = create_task_with_error_handling(
							download_in_background(),
							name='download_in_background',
							logger_instance=self.watchdog.logger,
							suppress_exceptions=True,
						)
						self.watchdog._cdp_event_tasks.add(task)
						task.add_done_callback(lambda t: self.watchdog._cdp_event_tasks.discard(t))

					except Exception as e:
						self.watchdog.logger.error(
							f'[DownloadsWatchdog] Error in network response handler: {type(e).__name__}: {e}'
						)

				# Register the callback globally (once)
				cdp_client.register.Network.responseReceived(on_response_received)
				self.watchdog._network_callback_registered = True
				self.watchdog.logger.debug('[DownloadsWatchdog] ✅ Registered global network response callback')

			# Get or create CDP session for this target
			cdp_session = await self.watchdog.browser_session.get_or_create_cdp_session(target_id, focus=False)

			# Enable Network domain to monitor HTTP responses (per-target/per-session)
			await cdp_client.send.Network.enable(session_id=cdp_session.session_id)
			self.watchdog.logger.debug(f'[DownloadsWatchdog] Enabled Network domain for target {target_id[-4:]}')

			# Mark this target as monitored
			self.watchdog._network_monitored_targets.add(target_id)
			self.watchdog.logger.debug(f'[DownloadsWatchdog] ✅ Network monitoring enabled for target {target_id[-4:]}')

		except Exception as e:
			self.watchdog.logger.warning(f'[DownloadsWatchdog] Failed to set up network monitoring for target {target_id}: {e}')

	async def download_file_from_url(
		self, url: str, target_id: TargetID, content_type: str | None = None, suggested_filename: str | None = None
	) -> str | None:
		"""Generic method to download any file from a URL.

		Args:
			url: The URL to download
			target_id: The target ID for CDP session
			content_type: Optional content type (e.g., 'application/pdf')
			suggested_filename: Optional filename from Content-Disposition header

		Returns:
			Path to downloaded file, or None if download failed
		"""
		if not self.watchdog.browser_session.browser_profile.downloads_path:
			self.watchdog.logger.warning('[DownloadsWatchdog] No downloads path configured')
			return None

		# Check if already downloaded in this session
		if url in self.watchdog._session_pdf_urls:
			existing_path = self.watchdog._session_pdf_urls[url]
			if os.path.exists(existing_path):
				self.watchdog.logger.debug(f'[DownloadsWatchdog] File already downloaded in session: {existing_path}')
				return existing_path

			# Stale cache entry: the file was removed/cleaned up after we cached it.
			self.watchdog.logger.debug(
				f'[DownloadsWatchdog] Cached download path no longer exists, re-downloading: {existing_path}'
			)
			del self.watchdog._session_pdf_urls[url]

		try:
			# Get or create CDP session for this target
			temp_session = await self.watchdog.browser_session.get_or_create_cdp_session(target_id, focus=False)

			if suggested_filename:
				filename = sanitize_download_filename(suggested_filename)
			else:
				# Extract from URL
				filename = os.path.basename(url.split('?')[0])  # Remove query params
				if not filename or '.' not in filename:
					# Fallback: use content type to determine extension
					if content_type and 'pdf' in content_type:
						filename = 'document.pdf'
					else:
						filename = 'download'

			# Ensure downloads directory exists
			downloads_dir = str(self.watchdog.browser_session.browser_profile.downloads_path)
			os.makedirs(downloads_dir, exist_ok=True)

			# Generate unique filename if file exists
			final_filename = filename
			existing_files = os.listdir(downloads_dir)
			if filename in existing_files:
				base, ext = os.path.splitext(filename)
				counter = 1
				while f'{base} ({counter}){ext}' in existing_files:
					counter += 1
				final_filename = f'{base} ({counter}){ext}'
				self.watchdog.logger.debug(f'[DownloadsWatchdog] File exists, using: {final_filename}')

			self.watchdog.logger.debug(f'[DownloadsWatchdog] Downloading from: {url[:100]}...')

			# Download using JavaScript fetch to leverage browser cache
			escaped_url = json.dumps(url)

			result = await asyncio.wait_for(
				temp_session.cdp_client.send.Runtime.evaluate(
					params={
						'expression': f"""
				(async () => {{
					try {{
						const response = await fetch({escaped_url}, {{
							cache: 'force-cache'
						}});
						if (!response.ok) {{
							throw new Error(`HTTP error! status: ${{response.status}}`);
						}}
						const blob = await response.blob();
						const arrayBuffer = await blob.arrayBuffer();
						const uint8Array = new Uint8Array(arrayBuffer);

						return {{
							data: Array.from(uint8Array),
							responseSize: uint8Array.length
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
				timeout=15.0,  # 15 second timeout
			)

			download_result = result.get('result', {}).get('value', {})

			if download_result and download_result.get('data') and len(download_result['data']) > 0:
				download_path = os.path.join(downloads_dir, final_filename)
				if not is_path_contained(download_path, downloads_dir):
					self.watchdog.logger.error(
						f'[DownloadsWatchdog] Refusing to write download outside downloads_dir: {download_path}'
					)
					return None

				# Save the file asynchronously
				async with await anyio.open_file(download_path, 'wb') as f:
					await f.write(bytes(download_result['data']))

				# Verify file was written successfully
				if os.path.exists(download_path):
					actual_size = os.path.getsize(download_path)
					self.watchdog.logger.debug(f'[DownloadsWatchdog] File written: {download_path} ({actual_size} bytes)')

					# Determine file type
					file_ext = Path(final_filename).suffix.lower().lstrip('.')
					mime_type = content_type or f'application/{file_ext}'

					# Store URL->path mapping for this session
					self.watchdog._session_pdf_urls[url] = download_path

					# Emit file downloaded event
					self.watchdog.logger.debug(f'[DownloadsWatchdog] Dispatching FileDownloadedEvent for {final_filename}')
					self.watchdog.event_bus.dispatch(
						FileDownloadedEvent(
							url=url,
							path=download_path,
							file_name=final_filename,
							file_size=actual_size,
							file_type=file_ext if file_ext else None,
							mime_type=mime_type,
							auto_download=True,
						)
					)

					return download_path
				else:
					self.watchdog.logger.error(f'[DownloadsWatchdog] Failed to write file: {download_path}')
					return None
			else:
				self.watchdog.logger.warning(f'[DownloadsWatchdog] No data received when downloading from {url}')
				return None

		except TimeoutError:
			self.watchdog.logger.warning(f'[DownloadsWatchdog] Download timed out: {url[:80]}...')
			return None
		except Exception as e:
			self.watchdog.logger.warning(f'[DownloadsWatchdog] Download failed: {type(e).__name__}: {e}')
			return None
