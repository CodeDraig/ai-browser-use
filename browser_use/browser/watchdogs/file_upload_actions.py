"""File upload behavior for default browser actions."""

import os

from browser_use.actor.upload import FileUploader
from browser_use.browser.events import UploadFileEvent
from browser_use.browser.views import BrowserError


class FileUploadActions:
	"""Resolve file-input sessions, validate files, and populate file inputs."""

	def __init__(self, browser_session) -> None:
		self.browser_session = browser_session
		self.file_uploader = FileUploader(browser_session)

	@property
	def logger(self):
		return self.browser_session.logger

	async def handle_upload_file(self, event: UploadFileEvent) -> None:
		"""Handle file upload request with CDP."""
		try:
			# Use the provided node
			element_node = event.node
			index_for_logging = self.browser_session.dom_state.get_selector_index(element_node)

			# Check if it's a file input
			if not self.browser_session.dom_state.is_file_input(element_node):
				msg = f'Upload failed - element {index_for_logging} is not a file input.'
				raise BrowserError(message=msg, long_term_memory=msg)

			# Validate file before upload
			if os.path.exists(event.file_path):
				file_size = os.path.getsize(event.file_path)
				if file_size == 0:
					msg = f'Upload failed - file {event.file_path} is empty (0 bytes).'
					raise BrowserError(message=msg, long_term_memory=msg)
				self.logger.debug(f'📎 File {event.file_path} validated ({file_size} bytes)')

			await self.file_uploader.upload(element_node, event.file_path)

			self.logger.info(f'📎 Uploaded file {event.file_path} to element {index_for_logging}')
		except Exception as e:
			raise
