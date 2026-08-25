import asyncio
import logging
from typing import TYPE_CHECKING

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import ClickCoordinateEvent, ClickElementEvent, SwitchTabEvent
from browser_use.browser.views import BrowserError
from browser_use.runtime import create_task_with_error_handling
from browser_use.tools.actions.dropdowns import get_dropdown_options
from browser_use.tools.errors import handle_browser_error
from browser_use.tools.utils import get_click_description
from browser_use.tools.views import ClickElementAction, ClickElementActionIndexOnly, GetDropdownOptionsAction

if TYPE_CHECKING:
	from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.tools.service')

ClickElementEvent.model_rebuild()


def _convert_llm_coordinates_to_viewport(
	llm_x: int,
	llm_y: int,
	browser_session: BrowserSession,
) -> tuple[int, int]:
	"""Convert coordinates from the LLM screenshot size to the browser viewport."""
	if browser_session.llm_screenshot_size and browser_session.dom_state.original_viewport_size:
		original_width, original_height = browser_session.dom_state.original_viewport_size
		llm_width, llm_height = browser_session.llm_screenshot_size
		actual_x = int((llm_x / llm_width) * original_width)
		actual_y = int((llm_y / llm_height) * original_height)

		logger.info(
			f'🔄 Converting coordinates: LLM ({llm_x}, {llm_y}) @ {llm_width}x{llm_height} '
			f'→ Viewport ({actual_x}, {actual_y}) @ {original_width}x{original_height}'
		)
		return actual_x, actual_y
	return llm_x, llm_y


def register_click_action(tools: 'Tools') -> None:
	"""Register the click action with or without coordinate support based on current setting."""
	# Remove existing click action if present
	if 'click' in tools.registry.registry.actions:
		del tools.registry.registry.actions['click']

	if tools._coordinate_clicking_enabled:
		# Register click action WITH coordinate support
		@tools.registry.action(
			'Click element by index or coordinates. Use coordinates only if the index is not available. Either provide coordinates or index.',
			param_model=ClickElementAction,
		)
		async def click(params: ClickElementAction, browser_session: BrowserSession):
			# Validate that either index or coordinates are provided
			if params.index is None and (params.coordinate_x is None or params.coordinate_y is None):
				return ActionResult(error='Must provide either index or both coordinate_x and coordinate_y')

			# Try index-based clicking first if index is provided
			if params.index is not None:
				return await tools._click_by_index(params, browser_session)
			# Coordinate-based clicking when index is not provided
			else:
				return await tools._click_by_coordinate(params, browser_session)
	else:
		# Register click action WITHOUT coordinate support (index only)
		@tools.registry.action(
			'Click element by index.',
			param_model=ClickElementActionIndexOnly,
		)
		async def click(params: ClickElementActionIndexOnly, browser_session: BrowserSession):
			return await tools._click_by_index(params, browser_session)


def register_click_actions(tools: 'Tools') -> None:
	"""Register click handlers and the current click schema."""

	# Element Interaction Actions
	async def _detect_new_tab_opened(
		browser_session: BrowserSession,
		tabs_before: set[str],
	) -> str:
		"""Detect if a click opened a new tab and automatically switch to it."""
		try:
			# Brief delay to allow CDP Target.attachedToTarget events to propagate
			# and be processed by SessionManager._handle_target_attached
			await asyncio.sleep(0.05)

			tabs_after = await browser_session.get_tabs()
			new_tabs = [t for t in tabs_after if t.target_id not in tabs_before]
			if new_tabs:
				new_tab = new_tabs[0]
				new_tab_id = new_tab.target_id[-4:]
				# Auto-switch to the new tab so the agent can immediately interact with it
				try:
					switch_event = browser_session.event_bus.dispatch(SwitchTabEvent(target_id=new_tab.target_id))
					await switch_event
					await switch_event.event_result(raise_if_any=False, raise_if_none=False)
					return f'. Automatically switched to new tab (tab_id: {new_tab_id}).'
				except Exception:
					return f'. Note: This opened a new tab (tab_id: {new_tab_id}) - switch to it if you need to interact with the new page.'
		except Exception:
			pass
		return ''

	async def _click_by_coordinate(params: ClickElementAction, browser_session: BrowserSession) -> ActionResult:
		# Ensure coordinates are provided (type safety)
		if params.coordinate_x is None or params.coordinate_y is None:
			return ActionResult(error='Both coordinate_x and coordinate_y must be provided')

		try:
			# Convert coordinates from LLM size to original viewport size if resizing was used
			actual_x, actual_y = _convert_llm_coordinates_to_viewport(params.coordinate_x, params.coordinate_y, browser_session)

			# Capture tab IDs before click to detect new tabs
			tabs_before = {t.target_id for t in await browser_session.get_tabs()}

			# Highlight the coordinate being clicked (truly non-blocking)
			create_task_with_error_handling(
				browser_session.dom_state.highlight_coordinate_click(actual_x, actual_y),
				name='highlight_coordinate_click',
				suppress_exceptions=True,
			)

			# Dispatch ClickCoordinateEvent - handler will check for safety and click
			event = browser_session.event_bus.dispatch(
				ClickCoordinateEvent(coordinate_x=actual_x, coordinate_y=actual_y, force=True)
			)
			await event
			# Wait for handler to complete and get any exception or metadata
			click_metadata = await event.event_result(raise_if_any=True, raise_if_none=False)

			# Check for validation errors (only happens when force=False)
			if isinstance(click_metadata, dict) and 'validation_error' in click_metadata:
				error_msg = click_metadata['validation_error']
				return ActionResult(error=error_msg)

			memory = f'Clicked on coordinate {params.coordinate_x}, {params.coordinate_y}'
			memory += await _detect_new_tab_opened(browser_session, tabs_before)
			logger.info(f'🖱️ {memory}')

			return ActionResult(
				extracted_content=memory,
				metadata={'click_x': actual_x, 'click_y': actual_y},
			)
		except BrowserError as e:
			return handle_browser_error(e)
		except Exception as e:
			error_msg = f'Failed to click at coordinates ({params.coordinate_x}, {params.coordinate_y}): {e}'
			return ActionResult(error=error_msg)

	async def _click_by_index(
		params: ClickElementAction | ClickElementActionIndexOnly, browser_session: BrowserSession
	) -> ActionResult:
		assert params.index is not None
		try:
			assert params.index != 0, (
				'Cannot click on element with index 0. If there are no interactive elements use wait(), refresh(), etc. to troubleshoot'
			)

			# Look up the node from the selector map
			node = await browser_session.dom_state.get_dom_element_by_index(params.index)
			if node is None:
				msg = f'Element index {params.index} not available - page may have changed. Try refreshing browser state.'
				logger.warning(f'⚠️ {msg}')
				return ActionResult(extracted_content=msg)

			# Get description of clicked element
			element_desc = get_click_description(node)

			# Capture tab IDs before click to detect new tabs
			tabs_before = {t.target_id for t in await browser_session.get_tabs()}

			# Highlight the element being clicked (truly non-blocking)
			create_task_with_error_handling(
				browser_session.dom_state.highlight_interaction_element(node),
				name='highlight_click_element',
				suppress_exceptions=True,
			)

			event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
			await event
			# Wait for handler to complete and get any exception or metadata
			click_metadata = await event.event_result(raise_if_any=True, raise_if_none=False)

			# Check if result contains validation error (e.g., trying to click <select> or file input)
			if isinstance(click_metadata, dict) and 'validation_error' in click_metadata:
				error_msg = click_metadata['validation_error']
				# If it's a select element, try to get dropdown options as a helpful shortcut
				if 'Cannot click on <select> elements.' in error_msg:
					try:
						return await get_dropdown_options(
							params=GetDropdownOptionsAction(index=params.index), browser_session=browser_session
						)
					except Exception as dropdown_error:
						logger.debug(
							f'Failed to get dropdown options as shortcut during click on dropdown: {type(dropdown_error).__name__}: {dropdown_error}'
						)
				return ActionResult(error=error_msg)

			# Build memory with element info
			memory = f'Clicked {element_desc}'
			memory += await _detect_new_tab_opened(browser_session, tabs_before)
			logger.info(f'🖱️ {memory}')

			# Include click coordinates in metadata if available
			return ActionResult(
				extracted_content=memory,
				metadata=click_metadata if isinstance(click_metadata, dict) else None,
			)
		except BrowserError as e:
			return handle_browser_error(e)
		except Exception as e:
			error_msg = f'Failed to click element {params.index}: {str(e)}'
			return ActionResult(error=error_msg)

	# Store click handlers for re-registration
	tools._click_by_index = _click_by_index
	tools._click_by_coordinate = _click_by_coordinate

	# Register click action (index-only by default)
	register_click_action(tools)
