import asyncio
import logging
import os
from typing import TYPE_CHECKING

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import TypeTextEvent, UploadFileEvent
from browser_use.browser.views import BrowserError
from browser_use.dom.service import EnhancedDOMTreeNode
from browser_use.filesystem.file_system import FileSystem
from browser_use.runtime import create_task_with_error_handling
from browser_use.security import SensitiveData
from browser_use.tools.errors import handle_browser_error
from browser_use.tools.views import InputTextAction, UploadFileAction

if TYPE_CHECKING:
	from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.tools.service')

TypeTextEvent.model_rebuild()
UploadFileEvent.model_rebuild()


def _detect_sensitive_key_name(text: str, sensitive_data: SensitiveData | None) -> str | None:
	"""Detect which sensitive key name corresponds to the given text value."""
	if not sensitive_data or not text:
		return None

	# Collect all sensitive values and their keys
	for domain_values in sensitive_data.values():
		for key, value in domain_values.items():
			if value and value == text:
				return key

	return None


def _is_autocomplete_field(node: EnhancedDOMTreeNode) -> bool:
	"""Detect if a node is an autocomplete/combobox field from its attributes."""
	attrs = node.attributes or {}
	if attrs.get('role') == 'combobox':
		return True
	aria_ac = attrs.get('aria-autocomplete', '')
	if aria_ac and aria_ac != 'none':
		return True
	if attrs.get('list'):
		return True
	haspopup = attrs.get('aria-haspopup', '')
	if haspopup and haspopup != 'false' and (attrs.get('aria-controls') or attrs.get('aria-owns')):
		return True
	return False


def register_input_actions(tools: 'Tools') -> None:
	"""Register text entry and file upload actions."""

	@tools.registry.action(
		'Input text into element by index. Clears existing text by default; pass text="" to clear only, or clear=False to append.',
		param_model=InputTextAction,
	)
	async def input(
		params: InputTextAction,
		browser_session: BrowserSession,
		has_sensitive_data: bool = False,
		sensitive_data: SensitiveData | None = None,
	):
		# Look up the node from the selector map
		node = await browser_session.dom_state.get_dom_element_by_index(params.index)
		if node is None:
			msg = f'Element index {params.index} not available - page may have changed. Try refreshing browser state.'
			logger.warning(f'⚠️ {msg}')
			return ActionResult(extracted_content=msg)

		# Highlight the element being typed into (truly non-blocking)
		create_task_with_error_handling(
			browser_session.dom_state.highlight_interaction_element(node), name='highlight_type_element', suppress_exceptions=True
		)

		# Dispatch type text event with node
		try:
			# Detect which sensitive key is being used
			sensitive_key_name = None
			if has_sensitive_data and sensitive_data:
				sensitive_key_name = _detect_sensitive_key_name(params.text, sensitive_data)

			event = browser_session.event_bus.dispatch(
				TypeTextEvent(
					node=node,
					text=params.text,
					clear=params.clear,
					is_sensitive=has_sensitive_data,
					sensitive_key_name=sensitive_key_name,
				)
			)
			await event
			input_metadata = await event.event_result(raise_if_any=True, raise_if_none=False)

			# Create message with sensitive data handling
			if has_sensitive_data:
				if sensitive_key_name:
					msg = f'Typed {sensitive_key_name}'
					log_msg = f'Typed <{sensitive_key_name}>'
				else:
					msg = 'Typed sensitive data'
					log_msg = 'Typed <sensitive>'
			else:
				msg = f"Typed '{params.text}'"
				log_msg = f"Typed '{params.text}'"

			logger.debug(log_msg)

			# Check for value mismatch (non-sensitive only)
			actual_value = None
			if isinstance(input_metadata, dict):
				actual_value = input_metadata.pop('actual_value', None)

			if not has_sensitive_data and actual_value is not None and actual_value != params.text:
				msg += f"\n⚠️ Note: the field's actual value '{actual_value}' differs from typed text '{params.text}'. The page may have reformatted or autocompleted your input."

			# Check for autocomplete/combobox field — add mechanical delay for dropdown
			if _is_autocomplete_field(node):
				msg += '\n💡 This is an autocomplete field. Wait for suggestions to appear, then click the correct suggestion instead of pressing Enter.'
				# Only delay for true JS-driven autocomplete (combobox / aria-autocomplete),
				# not native <datalist> or loose aria-haspopup which the browser handles instantly
				attrs = node.attributes or {}
				if attrs.get('role') == 'combobox' or (attrs.get('aria-autocomplete', '') not in ('', 'none')):
					await asyncio.sleep(0.4)  # let JS dropdown populate before next action

			# Include input coordinates in metadata if available
			return ActionResult(
				extracted_content=msg,
				long_term_memory=msg,
				metadata=input_metadata if isinstance(input_metadata, dict) else None,
			)
		except BrowserError as e:
			return handle_browser_error(e)
		except Exception as e:
			# Log the full error for debugging
			logger.error(f'Failed to dispatch TypeTextEvent: {type(e).__name__}: {e}')
			error_msg = f'Failed to type text into element {params.index}: {e}'
			return ActionResult(error=error_msg)

	@tools.registry.action(
		'',
		param_model=UploadFileAction,
	)
	async def upload_file(
		params: UploadFileAction, browser_session: BrowserSession, available_file_paths: list[str], file_system: FileSystem
	):
		# Check if file is in available_file_paths (user-provided or downloaded files)
		# For remote browsers (is_local=False), we allow absolute remote paths even if not tracked locally
		if params.path not in available_file_paths:
			# Also check if it's a recently downloaded file that might not be in available_file_paths yet
			downloaded_files = browser_session.downloaded_files
			if params.path not in downloaded_files:
				# Finally, check if it's a file in the FileSystem service.
				# Only rewrite to the local FileSystem path on local sessions —
				# on remote sessions, params.path is meant to address a file on
				# the remote machine, and a coincidental basename collision with
				# a local managed file (e.g. `/tmp/note.md` colliding with a
				# local `note.md`) must not silently upload the local file.
				if browser_session.is_local and file_system and file_system.get_dir():
					# Check if the file is actually managed by the FileSystem service
					# The path should be just the filename for FileSystem files
					file_obj = file_system.get_file(params.path)
					if file_obj:
						# Construct the upload path from the FileSystem-owned basename
						# (file_obj.full_name), NOT from params.path. The agent-controlled
						# params.path may contain '..' traversal sequences that escape
						# data_dir when naively joined — get_file() matches by basename
						# so a path like '../../../note.md' would otherwise resolve to a
						# sibling file outside the FileSystem directory.
						# GHSA-j9hj-92j8-jv9h.
						file_system_path = str(file_system.get_dir() / file_obj.full_name)
						# Defense in depth: refuse any path that resolves outside data_dir.
						real_path = os.path.realpath(file_system_path)
						real_dir = os.path.realpath(str(file_system.get_dir()))
						if not (real_path == real_dir or real_path.startswith(real_dir + os.sep)):
							msg = f'Upload of {params.path!r} escapes FileSystem directory; refusing.'
							logger.error(f'❌ {msg}')
							return ActionResult(error=msg)
						params = UploadFileAction(index=params.index, path=file_system_path)
					else:
						msg = f'File path {params.path} is not available. To fix: The user must add this file path to the available_file_paths parameter when creating the Agent. Example: Agent(task="...", llm=llm, browser=browser, available_file_paths=["{params.path}"])'
						logger.error(f'❌ {msg}')
						return ActionResult(error=msg)
				else:
					# If browser is remote, allow passing a remote-accessible absolute path
					if not browser_session.is_local:
						pass
					else:
						msg = f'File path {params.path} is not available. To fix: The user must add this file path to the available_file_paths parameter when creating the Agent. Example: Agent(task="...", llm=llm, browser=browser, available_file_paths=["{params.path}"])'
						raise BrowserError(message=msg, long_term_memory=msg)

		# For local browsers, ensure the file exists and has content
		if browser_session.is_local:
			if not os.path.exists(params.path):
				msg = f'File {params.path} does not exist'
				return ActionResult(error=msg)
			file_size = os.path.getsize(params.path)
			if file_size == 0:
				msg = f'File {params.path} is empty (0 bytes). The file may not have been saved correctly.'
				return ActionResult(error=msg)

		# Get the selector map to find the node
		selector_map = await browser_session.dom_state.get_selector_map()
		if params.index not in selector_map:
			msg = f'Element with index {params.index} does not exist.'
			return ActionResult(error=msg)

		node = selector_map[params.index]

		# Try to find a file input element near the selected element
		file_input_node = browser_session.dom_state.find_file_input_near_element(node)

		# Highlight the file input element if found (truly non-blocking)
		if file_input_node:
			create_task_with_error_handling(
				browser_session.dom_state.highlight_interaction_element(file_input_node),
				name='highlight_file_input',
				suppress_exceptions=True,
			)

		# If not found near the selected element, fallback to finding the closest file input to current scroll position
		if file_input_node is None:
			logger.info(
				f'No file upload element found near index {params.index}, searching for closest file input to scroll position'
			)

			# Get current scroll position
			cdp_session = await browser_session.get_or_create_cdp_session()
			try:
				scroll_info = await cdp_session.cdp_client.send.Runtime.evaluate(
					params={'expression': 'window.scrollY || window.pageYOffset || 0'}, session_id=cdp_session.session_id
				)
				current_scroll_y = scroll_info.get('result', {}).get('value', 0)
			except Exception:
				current_scroll_y = 0

			# Find all file inputs in the selector map and pick the closest one to scroll position
			closest_file_input = None
			min_distance = float('inf')

			for idx, element in selector_map.items():
				if browser_session.dom_state.is_file_input(element):
					# Get element's Y position
					if element.absolute_position:
						element_y = element.absolute_position.y
						distance = abs(element_y - current_scroll_y)
						if distance < min_distance:
							min_distance = distance
							closest_file_input = element

			if closest_file_input:
				file_input_node = closest_file_input
				logger.info(f'Found file input closest to scroll position (distance: {min_distance}px)')

				# Highlight the fallback file input element (truly non-blocking)
				create_task_with_error_handling(
					browser_session.dom_state.highlight_interaction_element(file_input_node),
					name='highlight_file_input_fallback',
					suppress_exceptions=True,
				)
			else:
				msg = 'No file upload element found on the page'
				logger.error(msg)
				raise BrowserError(msg)
				# TODO: figure out why this fails sometimes + add fallback hail mary, just look for any file input on page

		# Dispatch upload file event with the file input node
		try:
			event = browser_session.event_bus.dispatch(UploadFileEvent(node=file_input_node, file_path=params.path))
			await event
			await event.event_result(raise_if_any=True, raise_if_none=False)
			msg = f'Successfully uploaded file to index {params.index}'
			logger.info(f'📁 {msg}')
			return ActionResult(
				extracted_content=msg,
				long_term_memory=f'Uploaded file {params.path} to element {params.index}',
			)
		except Exception as e:
			logger.error(f'Failed to upload file: {e}')
			raise BrowserError(f'Failed to upload file: {e}')
