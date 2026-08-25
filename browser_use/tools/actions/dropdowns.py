import logging
from typing import TYPE_CHECKING

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import GetDropdownOptionsEvent
from browser_use.tools.views import GetDropdownOptionsAction, SelectDropdownOptionAction

if TYPE_CHECKING:
	from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.tools.service')


async def get_dropdown_options(params: GetDropdownOptionsAction, browser_session: BrowserSession) -> ActionResult:
	"""Get all options from a native dropdown or ARIA menu"""
	# Look up the node from the selector map
	node = await browser_session.dom_state.get_dom_element_by_index(params.index)
	if node is None:
		msg = f'Element index {params.index} not available - page may have changed. Try refreshing browser state.'
		logger.warning(f'⚠️ {msg}')
		return ActionResult(extracted_content=msg)

	# Dispatch GetDropdownOptionsEvent to the event handler

	event = browser_session.event_bus.dispatch(GetDropdownOptionsEvent(node=node))
	dropdown_data = await event.event_result(timeout=3.0, raise_if_none=True, raise_if_any=True)

	if not dropdown_data:
		raise ValueError('Failed to get dropdown options - no data returned')

	# Use structured memory from the handler
	return ActionResult(
		extracted_content=dropdown_data['short_term_memory'],
		long_term_memory=dropdown_data['long_term_memory'],
		include_extracted_content_only_once=True,
	)


def register_dropdown_actions(tools: 'Tools') -> None:
	"""Register dropdown inspection and selection."""
	# Dropdown Actions

	@tools.registry.action(
		'',
		param_model=GetDropdownOptionsAction,
	)
	async def dropdown_options(params: GetDropdownOptionsAction, browser_session: BrowserSession):
		return await get_dropdown_options(params, browser_session)

	@tools.registry.action(
		'Set the option of a <select> element.',
		param_model=SelectDropdownOptionAction,
	)
	async def select_dropdown(params: SelectDropdownOptionAction, browser_session: BrowserSession):
		"""Select dropdown option by the text of the option you want to select"""
		# Look up the node from the selector map
		node = await browser_session.dom_state.get_dom_element_by_index(params.index)
		if node is None:
			msg = f'Element index {params.index} not available - page may have changed. Try refreshing browser state.'
			logger.warning(f'⚠️ {msg}')
			return ActionResult(extracted_content=msg)

		# Dispatch SelectDropdownOptionEvent to the event handler
		from browser_use.browser.events import SelectDropdownOptionEvent

		event = browser_session.event_bus.dispatch(SelectDropdownOptionEvent(node=node, text=params.text))
		selection_data = await event.event_result()

		if not selection_data:
			raise ValueError('Failed to select dropdown option - no data returned')

		# Check if the selection was successful
		if selection_data.get('success') == 'true':
			# Extract the message from the returned data
			msg = selection_data.get('message', f'Selected option: {params.text}')
			return ActionResult(
				extracted_content=msg,
				long_term_memory=f"Selected dropdown option '{params.text}' at index {params.index}",
			)
		else:
			# Handle structured error response
			# TODO: raise BrowserError instead of returning ActionResult
			if 'short_term_memory' in selection_data and 'long_term_memory' in selection_data:
				return ActionResult(
					extracted_content=selection_data['short_term_memory'],
					long_term_memory=selection_data['long_term_memory'],
					include_extracted_content_only_once=True,
				)
			else:
				# Fallback to regular error
				error_msg = selection_data.get('error', f'Failed to select option: {params.text}')
				return ActionResult(error=error_msg)
