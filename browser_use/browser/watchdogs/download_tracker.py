from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cdp_use.cdp.browser import DownloadProgressEvent as CDPDownloadProgressEvent
from cdp_use.cdp.browser import DownloadWillBeginEvent
from cdp_use.cdp.target import SessionID, TargetID

from browser_use.browser.events import DownloadStartedEvent, FileDownloadedEvent
from browser_use.browser.watchdogs.download_paths import (
	is_path_contained,
	sanitize_download_filename,
	unique_download_filename,
)
from browser_use.runtime import create_task_with_error_handling

if TYPE_CHECKING:
	from browser_use.browser.watchdogs.downloads_watchdog import DownloadsWatchdog


class DownloadTracker:
	"""Own browser download listeners, progress callbacks, and completion tracking."""

	def __init__(self, watchdog: DownloadsWatchdog) -> None:
		self.watchdog = watchdog

	async def attach_to_target(self, target_id: TargetID) -> None:
		"""Set up download monitoring for a specific target."""

		# Define CDP event handlers outside of try to avoid indentation/scope issues
		def download_will_begin_handler(event: DownloadWillBeginEvent, session_id: SessionID | None) -> None:
			self.watchdog.logger.debug(f'[DownloadsWatchdog] Download will begin: {event}')
			# Cache info for later completion event handling (esp. remote browsers)
			guid = event.get('guid', '')
			url = event.get('url', '')
			# Sanitize at the ingress so every downstream consumer sees a safe basename.
			suggested_filename = sanitize_download_filename(event.get('suggestedFilename', 'download'))
			try:
				assert suggested_filename, 'CDP DownloadWillBegin missing suggestedFilename'
				self.watchdog._cdp_downloads_info[guid] = {
					'url': url,
					'suggested_filename': suggested_filename,
					'handled': False,
				}
			except (AssertionError, KeyError):
				pass

			# Call direct callbacks first (for click handlers waiting for downloads)
			download_info = {
				'guid': guid,
				'url': url,
				'suggested_filename': suggested_filename,
				'auto_download': False,
			}
			self.watchdog.logger.debug(
				f'[DownloadsWatchdog] Calling {len(self.watchdog._download_start_callbacks)} start callbacks'
			)
			for callback in self.watchdog._download_start_callbacks:
				try:
					self.watchdog.logger.debug(f'[DownloadsWatchdog] Calling start callback: {callback}')
					callback(download_info)
				except Exception as e:
					self.watchdog.logger.debug(f'[DownloadsWatchdog] Error in download start callback: {e}')

			# Emit DownloadStartedEvent so other components can react
			self.watchdog.event_bus.dispatch(
				DownloadStartedEvent(
					guid=guid,
					url=url,
					suggested_filename=suggested_filename,
					auto_download=False,  # CDP-triggered downloads are user-initiated
				)
			)

			# Create and track the task
			task = create_task_with_error_handling(
				self._handle_cdp_download(event, target_id, session_id),
				name='handle_cdp_download',
				logger_instance=self.watchdog.logger,
				suppress_exceptions=True,
			)
			self.watchdog._cdp_event_tasks.add(task)
			# Remove from set when done
			task.add_done_callback(lambda t: self.watchdog._cdp_event_tasks.discard(t))

		def download_progress_handler(event: CDPDownloadProgressEvent, session_id: SessionID | None) -> None:
			guid = event.get('guid', '')
			state = event.get('state', '')
			received_bytes = int(event.get('receivedBytes', 0))
			total_bytes = int(event.get('totalBytes', 0))

			# Call direct callbacks first (for click handlers tracking progress)
			progress_info = {
				'guid': guid,
				'received_bytes': received_bytes,
				'total_bytes': total_bytes,
				'state': state,
			}
			for callback in self.watchdog._download_progress_callbacks:
				try:
					callback(progress_info)
				except Exception as e:
					self.watchdog.logger.debug(f'[DownloadsWatchdog] Error in download progress callback: {e}')

			# Emit progress event for all states so listeners can track progress
			from browser_use.browser.events import DownloadProgressEvent as DownloadProgressEventInternal

			self.watchdog.event_bus.dispatch(
				DownloadProgressEventInternal(
					guid=guid,
					received_bytes=received_bytes,
					total_bytes=total_bytes,
					state=state,
				)
			)

			# Check if download is complete
			if state == 'completed':
				file_path = event.get('filePath')
				if self.watchdog.browser_session.is_local:
					if file_path:
						self.watchdog.logger.debug(f'[DownloadsWatchdog] Download completed: {file_path}')
						# Track the download
						self._track_download(file_path, guid=guid)
						# Mark as handled to prevent fallback duplicate dispatch
						try:
							if guid in self.watchdog._cdp_downloads_info:
								self.watchdog._cdp_downloads_info[guid]['handled'] = True
						except (KeyError, AttributeError):
							pass
					else:
						# No filePath provided - detect by comparing with initial snapshot
						self.watchdog.logger.debug('[DownloadsWatchdog] No filePath in progress event; detecting via filesystem')
						downloads_path = self.watchdog.browser_session.browser_profile.downloads_path
						if downloads_path:
							downloads_dir = Path(downloads_path).expanduser().resolve()
							if downloads_dir.exists():
								for f in downloads_dir.iterdir():
									if (
										f.is_file()
										and not f.name.startswith('.')
										and f.name not in self.watchdog._initial_downloads_snapshot
									):
										# Check file has content before processing
										if f.stat().st_size > 4:
											# Found a new file! Add to snapshot immediately to prevent duplicate detection
											self.watchdog._initial_downloads_snapshot.add(f.name)
											self.watchdog.logger.debug(f'[DownloadsWatchdog] Detected new download: {f.name}')
											self._track_download(str(f))
											# Mark as handled
											try:
												if guid in self.watchdog._cdp_downloads_info:
													self.watchdog._cdp_downloads_info[guid]['handled'] = True
											except (KeyError, AttributeError):
												pass
											break
				else:
					# Remote browser: do not touch local filesystem. Fallback to downloadPath+suggestedFilename
					info = self.watchdog._cdp_downloads_info.get(guid, {})
					try:
						suggested_filename = info.get('suggested_filename') or (Path(file_path).name if file_path else 'download')
						downloads_path = str(self.watchdog.browser_session.browser_profile.downloads_path or '')
						effective_path = file_path or str(Path(downloads_path) / suggested_filename)
						file_name = Path(effective_path).name
						file_ext = Path(file_name).suffix.lower().lstrip('.')
						# Call direct callbacks first so click handlers waiting on the
						# download (e.g. _execute_click_with_download_detection) resolve.
						# The local branch does this inside _track_download(); the remote
						# branch previously only emitted the event, so the click action
						# timed out waiting for on_download_complete even though the
						# download had finished (see issue #5132).
						complete_info = {
							'guid': guid,
							'url': info.get('url', ''),
							'path': str(effective_path),
							'file_name': file_name,
							'file_size': 0,
							'file_type': file_ext if file_ext else None,
							'auto_download': False,
						}
						for callback in self.watchdog._download_complete_callbacks:
							try:
								callback(complete_info)
							except Exception as e:
								self.watchdog.logger.debug(f'[DownloadsWatchdog] Error in download complete callback: {e}')
						self.watchdog.event_bus.dispatch(
							FileDownloadedEvent(
								guid=guid,
								url=info.get('url', ''),
								path=str(effective_path),
								file_name=file_name,
								file_size=0,
								file_type=file_ext if file_ext else None,
							)
						)
						self.watchdog.logger.debug(f'[DownloadsWatchdog] ✅ (remote) Download completed: {effective_path}')
					finally:
						if guid in self.watchdog._cdp_downloads_info:
							del self.watchdog._cdp_downloads_info[guid]

		try:
			downloads_path_raw = self.watchdog.browser_session.browser_profile.downloads_path
			if not downloads_path_raw:
				# logger.info(f'[DownloadsWatchdog] No downloads path configured, skipping target: {target_id}')
				return  # No downloads path configured

			# Check if we already have a download listener on this session
			# to prevent duplicate listeners from being added
			# Note: Since download listeners are set up once per browser session, not per target,
			# we just track if we've set up the browser-level listener
			if self.watchdog._download_cdp_session_setup:
				self.watchdog.logger.debug('[DownloadsWatchdog] Download listener already set up for browser session')
				return

			# logger.debug(f'[DownloadsWatchdog] Setting up CDP download listener for target: {target_id}')

			# Use CDP session for download events but store reference in watchdog
			if not self.watchdog._download_cdp_session_setup:
				# Set up CDP session for downloads (only once per browser session)
				cdp_client = self.watchdog.browser_session.cdp_client

				# Set download behavior to allow downloads and enable events
				downloads_path = self.watchdog.browser_session.browser_profile.downloads_path
				if not downloads_path:
					self.watchdog.logger.warning('[DownloadsWatchdog] No downloads path configured, skipping CDP download setup')
					return
				# Ensure path is properly expanded (~ -> absolute path)
				expanded_downloads_path = Path(downloads_path).expanduser().resolve()
				await cdp_client.send.Browser.setDownloadBehavior(
					params={
						'behavior': 'allow',
						'downloadPath': str(expanded_downloads_path),  # Use expanded absolute path
						'eventsEnabled': True,
					}
				)

				# Register the handlers with CDP
				cdp_client.register.Browser.downloadWillBegin(download_will_begin_handler)  # type: ignore[arg-type]
				cdp_client.register.Browser.downloadProgress(download_progress_handler)  # type: ignore[arg-type]

				self.watchdog._download_cdp_session_setup = True
				self.watchdog.logger.debug('[DownloadsWatchdog] Set up CDP download listeners')

			# No need to track individual targets since download listener is browser-level
			# logger.debug(f'[DownloadsWatchdog] Successfully set up CDP download listener for target: {target_id}')

		except Exception as e:
			self.watchdog.logger.warning(
				f'[DownloadsWatchdog] Failed to set up CDP download listener for target {target_id}: {e}'
			)

		# Set up network monitoring for this target (catches ALL download variants)
		await self.watchdog.network_monitor.setup(target_id)

	def _track_download(self, file_path: str, guid: str | None = None) -> None:
		"""Track a completed download and dispatch the appropriate event.

		Args:
			file_path: The path to the downloaded file
			guid: Optional CDP download GUID for correlation with DownloadStartedEvent
		"""
		try:
			# Get file info
			path = Path(file_path)
			if path.exists():
				file_size = path.stat().st_size
				self.watchdog.logger.debug(f'[DownloadsWatchdog] Tracked download: {path.name} ({file_size} bytes)')

				# Get file extension for file_type
				file_ext = path.suffix.lower().lstrip('.')

				# Call direct callbacks first (for click handlers waiting for downloads)
				complete_info = {
					'guid': guid,
					'url': str(path),
					'path': str(path),
					'file_name': path.name,
					'file_size': file_size,
					'file_type': file_ext if file_ext else None,
					'auto_download': False,
				}
				for callback in self.watchdog._download_complete_callbacks:
					try:
						callback(complete_info)
					except Exception as e:
						self.watchdog.logger.debug(f'[DownloadsWatchdog] Error in download complete callback: {e}')

				# Dispatch download event
				from browser_use.browser.events import FileDownloadedEvent

				self.watchdog.event_bus.dispatch(
					FileDownloadedEvent(
						guid=guid,
						url=str(path),  # Use the file path as URL for local files
						path=str(path),
						file_name=path.name,
						file_size=file_size,
					)
				)
			else:
				self.watchdog.logger.warning(f'[DownloadsWatchdog] Downloaded file not found: {file_path}')
		except Exception as e:
			self.watchdog.logger.error(f'[DownloadsWatchdog] Error tracking download: {e}')

	async def _handle_cdp_download(
		self, event: DownloadWillBeginEvent, target_id: TargetID, session_id: SessionID | None
	) -> None:
		"""Handle a CDP Page.downloadWillBegin event."""
		downloads_dir = (
			Path(
				self.watchdog.browser_session.browser_profile.downloads_path
				or f'{tempfile.gettempdir()}/browser_use_downloads.{str(self.watchdog.browser_session.id)[-4:]}'
			)
			.expanduser()
			.resolve()
		)  # Ensure path is properly expanded

		# Initialize variables that may be used outside try blocks
		unique_filename = None
		file_size = 0
		expected_path = None
		download_result = None
		download_url = event.get('url', '')
		suggested_filename = sanitize_download_filename(event.get('suggestedFilename', 'download'))
		guid = event.get('guid', '')

		try:
			self.watchdog.logger.debug(
				f'[DownloadsWatchdog] ⬇️ File download starting: {suggested_filename} from {download_url[:100]}...'
			)
			self.watchdog.logger.debug(f'[DownloadsWatchdog] Full CDP event: {event}')

			# Since Browser.setDownloadBehavior is already configured, the browser will download the file
			# We just need to wait for it to appear in the downloads directory
			expected_path = downloads_dir / suggested_filename

			# For remote browsers, don't poll local filesystem; downloadProgress handler will emit the event
			if not self.watchdog.browser_session.is_local:
				return
		except Exception as e:
			self.watchdog.logger.error(f'[DownloadsWatchdog] ❌ Error handling CDP download: {type(e).__name__} {e}')

		# If we reach here, the fetch method failed, so wait for native download
		# Poll the downloads directory for new files
		self.watchdog.logger.debug(
			f'[DownloadsWatchdog] Checking if browser auto-download saved the file for us: {suggested_filename}'
		)

		# Poll for new files
		max_wait = 20  # seconds
		start_time = asyncio.get_event_loop().time()

		while asyncio.get_event_loop().time() - start_time < max_wait:  # noqa: ASYNC110
			await asyncio.sleep(5.0)  # Check every 5 seconds

			if Path(downloads_dir).exists():
				for file_path in Path(downloads_dir).iterdir():
					# Skip hidden files and files that were already there
					if (
						file_path.is_file()
						and not file_path.name.startswith('.')
						and file_path.name not in self.watchdog._initial_downloads_snapshot
					):
						# Add to snapshot immediately to prevent duplicate detection
						self.watchdog._initial_downloads_snapshot.add(file_path.name)
						# Check if file has content (> 4 bytes)
						try:
							file_size = file_path.stat().st_size
							if file_size > 4:
								# Found a new download!
								self.watchdog.logger.debug(
									f'[DownloadsWatchdog] ✅ Found downloaded file: {file_path} ({file_size} bytes)'
								)

								# Determine file type from extension
								file_ext = file_path.suffix.lower().lstrip('.')
								file_type = file_ext if file_ext else None

								# Dispatch download event
								# Skip if already handled by progress/JS fetch
								info = self.watchdog._cdp_downloads_info.get(guid, {})
								if info.get('handled'):
									return
								self.watchdog.event_bus.dispatch(
									FileDownloadedEvent(
										guid=guid,
										url=download_url,
										path=str(file_path),
										file_name=file_path.name,
										file_size=file_size,
										file_type=file_type,
									)
								)
							# Mark as handled after dispatch
							try:
								if guid in self.watchdog._cdp_downloads_info:
									self.watchdog._cdp_downloads_info[guid]['handled'] = True
							except (KeyError, AttributeError):
								pass
							return
						except Exception as e:
							self.watchdog.logger.debug(f'[DownloadsWatchdog] Error checking file {file_path}: {e}')

		self.watchdog.logger.warning(f'[DownloadsWatchdog] Download did not complete within {max_wait} seconds')

	async def _handle_download(self, download: Any) -> None:
		"""Handle a download event."""
		download_id = f'{id(download)}'
		self.watchdog._active_downloads[download_id] = download
		self.watchdog.logger.debug(
			f'[DownloadsWatchdog] ⬇️ Handling download: {download.suggested_filename} from {download.url[:100]}...'
		)

		# Debug: Check if download is already being handled elsewhere
		failure = (
			await download.failure()
		)  # TODO: it always fails for some reason, figure out why connect_over_cdp makes accept_downloads not work
		self.watchdog.logger.warning(f'[DownloadsWatchdog] ❌ Download state - canceled: {failure}, url: {download.url}')
		# logger.info(f'[DownloadsWatchdog] Active downloads count: {len(self.watchdog._active_downloads)}')

		try:
			current_step = 'getting_download_info'
			# Get download info immediately
			url = download.url
			suggested_filename = sanitize_download_filename(download.suggested_filename)

			current_step = 'determining_download_directory'
			# Determine download directory from browser profile
			downloads_dir = self.watchdog.browser_session.browser_profile.downloads_path
			if not downloads_dir:
				downloads_dir = str(Path.home() / 'Downloads')
			else:
				downloads_dir = str(downloads_dir)  # Ensure it's a string

			# Check if Playwright already auto-downloaded the file (due to CDP setup)
			original_path = Path(downloads_dir) / suggested_filename
			if not is_path_contained(original_path, downloads_dir):
				self.watchdog.logger.error(
					f'[DownloadsWatchdog] Refusing to handle download outside downloads_dir: {original_path}'
				)
				return
			if original_path.exists() and original_path.stat().st_size > 0:
				self.watchdog.logger.debug(
					f'[DownloadsWatchdog] File already downloaded by Playwright: {original_path} ({original_path.stat().st_size} bytes)'
				)

				# Use the existing file instead of creating a duplicate
				download_path = original_path
				file_size = original_path.stat().st_size
				unique_filename = suggested_filename
			else:
				current_step = 'generating_unique_filename'
				# Ensure unique filename
				unique_filename = await unique_download_filename(downloads_dir, suggested_filename)
				download_path = Path(downloads_dir) / unique_filename

				self.watchdog.logger.debug(f'[DownloadsWatchdog] Download started: {unique_filename} from {url[:100]}...')

				current_step = 'calling_save_as'
				# Save the download using Playwright's save_as method
				self.watchdog.logger.debug(f'[DownloadsWatchdog] Saving download to: {download_path}')
				self.watchdog.logger.debug(f'[DownloadsWatchdog] Download path exists: {download_path.parent.exists()}')
				self.watchdog.logger.debug(
					f'[DownloadsWatchdog] Download path writable: {os.access(download_path.parent, os.W_OK)}'
				)

				try:
					self.watchdog.logger.debug('[DownloadsWatchdog] About to call download.save_as()...')
					await download.save_as(str(download_path))
					self.watchdog.logger.debug(f'[DownloadsWatchdog] Successfully saved download to: {download_path}')
					current_step = 'save_as_completed'
				except Exception as save_error:
					self.watchdog.logger.error(f'[DownloadsWatchdog] save_as() failed with error: {save_error}')
					raise save_error

				# Get file info
				file_size = download_path.stat().st_size if download_path.exists() else 0

			# Determine file type from extension
			file_ext = download_path.suffix.lower().lstrip('.')
			file_type = file_ext if file_ext else None

			# Try to get MIME type from response headers if available
			mime_type = None
			# Note: Playwright doesn't expose response headers directly from Download object

			# Check if this was a PDF auto-download
			auto_download = False
			if file_type == 'pdf':
				auto_download = self.watchdog._is_auto_download_enabled()

			# Emit download event
			self.watchdog.event_bus.dispatch(
				FileDownloadedEvent(
					url=url,
					path=str(download_path),
					file_name=suggested_filename,
					file_size=file_size,
					file_type=file_type,
					mime_type=mime_type,
					from_cache=False,
					auto_download=auto_download,
				)
			)

			self.watchdog.logger.debug(
				f'[DownloadsWatchdog] ✅ Download completed: {suggested_filename} ({file_size} bytes) saved to {download_path}'
			)

			# File is now tracked on filesystem, no need to track in memory

		except Exception as e:
			self.watchdog.logger.error(
				f'[DownloadsWatchdog] Error handling download at step "{locals().get("current_step", "unknown")}", error: {e}'
			)
			self.watchdog.logger.error(
				f'[DownloadsWatchdog] Download state - URL: {download.url}, filename: {download.suggested_filename}'
			)
		finally:
			# Clean up tracking
			if download_id in self.watchdog._active_downloads:
				del self.watchdog._active_downloads[download_id]
