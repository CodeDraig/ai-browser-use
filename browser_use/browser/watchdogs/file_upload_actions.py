"""File upload behavior for default browser actions."""

import os

from browser_use.browser.events import UploadFileEvent
from browser_use.browser.views import BrowserError
from browser_use.dom.service import EnhancedDOMTreeNode


class FileUploadActions:
	"""Resolve file-input sessions, validate files, and populate file inputs."""

	def __init__(self, browser_session) -> None:
		self.browser_session = browser_session

	@property
	def logger(self):
		return self.browser_session.logger

	async def _get_session_id_for_element(self, element_node: EnhancedDOMTreeNode) -> str | None:
		"""Get the appropriate CDP session ID for an element based on its frame."""
		if element_node.frame_id:
			# Element is in an iframe, need to get session for that frame
			try:
				all_targets = self.browser_session.session_manager.get_all_targets()

				# Find the target for this frame
				for target_id, target in all_targets.items():
					if target.target_type == 'iframe' and element_node.frame_id in str(target_id):
						# Create temporary session for iframe target without switching focus
						temp_session = await self.browser_session.get_or_create_cdp_session(target_id, focus=False)
						return temp_session.session_id

				# If frame not found in targets, use main target session
				self.logger.debug(f'Frame {element_node.frame_id} not found in targets, using main session')
			except Exception as e:
				self.logger.debug(f'Error getting frame session: {e}, using main session')

		# Use main target session - get_or_create_cdp_session validates focus automatically
		cdp_session = await self.browser_session.get_or_create_cdp_session()
		return cdp_session.session_id

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

			# Get CDP client and session
			cdp_client = self.browser_session.cdp_client
			session_id = await self._get_session_id_for_element(element_node)

			# Validate file before upload
			if os.path.exists(event.file_path):
				file_size = os.path.getsize(event.file_path)
				if file_size == 0:
					msg = f'Upload failed - file {event.file_path} is empty (0 bytes).'
					raise BrowserError(message=msg, long_term_memory=msg)
				self.logger.debug(f'📎 File {event.file_path} validated ({file_size} bytes)')

			# Set file(s) to upload
			backend_node_id = element_node.backend_node_id
			await cdp_client.send.DOM.setFileInputFiles(
				params={
					'files': [event.file_path],
					'backendNodeId': backend_node_id,
				},
				session_id=session_id,
			)

			self.logger.info(f'📎 Uploaded file {event.file_path} to element {index_for_logging}')
		except Exception as e:
			raise
