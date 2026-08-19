import logging
from typing import TYPE_CHECKING

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import ScrollEvent, ScrollToTextEvent, SendKeysEvent
from browser_use.filesystem.file_system import FileSystem
from browser_use.tools.views import ScreenshotAction, ScrollAction, SendKeysAction

if TYPE_CHECKING:
	from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.tools.service')

ScrollEvent.model_rebuild()


def register_viewport_actions(tools: 'Tools') -> None:
	"""Register scrolling, key input, text targeting, and screenshots."""

	@tools.registry.action(
		"""Scroll by pages. REQUIRED: down=True/False (True=scroll down, False=scroll up, default=True). Optional: pages=0.5-10.0 (default 1.0). Use index for scroll elements (dropdowns/custom UI). High pages (10) reaches bottom. Multi-page scrolls sequentially. Viewport-based height, fallback 1000px/page.""",
		param_model=ScrollAction,
	)
	async def scroll(params: ScrollAction, browser_session: BrowserSession):
		try:
			# Look up the node from the selector map if index is provided
			# Special case: index 0 means scroll the whole page (root/body element)
			node = None
			if params.index is not None and params.index != 0:
				node = await browser_session.get_dom_element_by_index(params.index)
				if node is None:
					# Element does not exist
					msg = f'Element index {params.index} not found in browser state'
					return ActionResult(error=msg)

			direction = 'down' if params.down else 'up'
			target = f'element {params.index}' if params.index is not None and params.index != 0 else ''

			# Get actual viewport height for more accurate scrolling
			try:
				cdp_session = await browser_session.get_or_create_cdp_session()
				metrics = await cdp_session.cdp_client.send.Page.getLayoutMetrics(session_id=cdp_session.session_id)

				# Use cssVisualViewport for the most accurate representation
				css_viewport = metrics.get('cssVisualViewport', {})
				css_layout_viewport = metrics.get('cssLayoutViewport', {})

				# Get viewport height, prioritizing cssVisualViewport
				viewport_height = int(css_viewport.get('clientHeight') or css_layout_viewport.get('clientHeight', 1000))

				logger.debug(f'Detected viewport height: {viewport_height}px')
			except Exception as e:
				viewport_height = 1000  # Fallback to 1000px
				logger.debug(f'Failed to get viewport height, using fallback 1000px: {e}')

			# For multiple pages (>=1.0), scroll one page at a time to ensure each scroll completes
			if params.pages >= 1.0:
				import asyncio

				num_full_pages = int(params.pages)
				remaining_fraction = params.pages - num_full_pages

				completed_scrolls = 0

				# Scroll one page at a time
				for i in range(num_full_pages):
					try:
						pixels = viewport_height  # Use actual viewport height
						if not params.down:
							pixels = -pixels

						event = browser_session.event_bus.dispatch(
							ScrollEvent(direction=direction, amount=abs(pixels), node=node)
						)
						await event
						await event.event_result(raise_if_any=True, raise_if_none=False)
						completed_scrolls += 1

						# Small delay to ensure scroll completes before next one
						await asyncio.sleep(0.15)

					except Exception as e:
						logger.warning(f'Scroll {i + 1}/{num_full_pages} failed: {e}')
						# Continue with remaining scrolls even if one fails

				# Handle fractional page if present
				if remaining_fraction > 0:
					try:
						pixels = int(remaining_fraction * viewport_height)
						if not params.down:
							pixels = -pixels

						event = browser_session.event_bus.dispatch(
							ScrollEvent(direction=direction, amount=abs(pixels), node=node)
						)
						await event
						await event.event_result(raise_if_any=True, raise_if_none=False)
						completed_scrolls += remaining_fraction

					except Exception as e:
						logger.warning(f'Fractional scroll failed: {e}')

				if params.pages == 1.0:
					long_term_memory = f'Scrolled {direction} {target} {viewport_height}px'.replace('  ', ' ')
				else:
					long_term_memory = f'Scrolled {direction} {target} {completed_scrolls:.1f} pages'.replace('  ', ' ')
			else:
				# For fractional pages <1.0, do single scroll
				pixels = int(params.pages * viewport_height)
				event = browser_session.event_bus.dispatch(
					ScrollEvent(direction='down' if params.down else 'up', amount=pixels, node=node)
				)
				await event
				await event.event_result(raise_if_any=True, raise_if_none=False)
				long_term_memory = f'Scrolled {direction} {target} {params.pages} pages'.replace('  ', ' ')

			msg = f'🔍 {long_term_memory}'
			logger.info(msg)
			return ActionResult(extracted_content=msg, long_term_memory=long_term_memory)
		except Exception as e:
			logger.error(f'Failed to dispatch ScrollEvent: {type(e).__name__}: {e}')
			error_msg = 'Failed to execute scroll action.'
			return ActionResult(error=error_msg)

	@tools.registry.action(
		'',
		param_model=SendKeysAction,
	)
	async def send_keys(params: SendKeysAction, browser_session: BrowserSession):
		# Dispatch send keys event
		try:
			event = browser_session.event_bus.dispatch(SendKeysEvent(keys=params.keys))
			await event
			await event.event_result(raise_if_any=True, raise_if_none=False)
			memory = f'Sent keys: {params.keys}'
			msg = f'⌨️  {memory}'
			logger.info(msg)
			return ActionResult(extracted_content=memory, long_term_memory=memory)
		except Exception as e:
			logger.error(f'Failed to dispatch SendKeysEvent: {type(e).__name__}: {e}')
			error_msg = f'Failed to send keys: {str(e)}'
			return ActionResult(error=error_msg)

	@tools.registry.action('Scroll to text.')
	async def find_text(text: str, browser_session: BrowserSession):  # type: ignore
		# Dispatch scroll to text event
		event = browser_session.event_bus.dispatch(ScrollToTextEvent(text=text))

		try:
			# The handler returns None on success or raises an exception if text not found
			await event.event_result(raise_if_any=True, raise_if_none=False)
			memory = f'Scrolled to text: {text}'
			msg = f'🔍  {memory}'
			logger.info(msg)
			return ActionResult(extracted_content=memory, long_term_memory=memory)
		except Exception as e:
			# Text not found
			msg = f"Text '{text}' not found or not visible on page"
			logger.info(msg)
			return ActionResult(
				extracted_content=msg,
				long_term_memory=f"Tried scrolling to text '{text}' but it was not found",
			)

	@tools.registry.action(
		'Take a screenshot of the current viewport. If file_name is provided, saves to that file and returns the path. '
		'Otherwise, screenshot is included in the next browser_state observation.',
		param_model=ScreenshotAction,
	)
	async def screenshot(
		params: ScreenshotAction,
		browser_session: BrowserSession,
		file_system: FileSystem,
	):
		"""Take screenshot, optionally saving to file."""
		if params.file_name:
			# Save screenshot to file
			file_name = params.file_name
			if not file_name.lower().endswith('.png'):
				file_name = f'{file_name}.png'
			file_name = FileSystem.sanitize_filename(file_name)

			screenshot_bytes = await browser_session.take_screenshot(full_page=False)
			file_path = file_system.get_dir() / file_name
			file_path.write_bytes(screenshot_bytes)

			result = f'Screenshot saved to {file_name}'
			logger.info(f'📸 {result}. Full path: {file_path}')
			return ActionResult(
				extracted_content=result,
				long_term_memory=f'{result}. Full path: {file_path}',
				attachments=[str(file_path)],
			)
		else:
			# Flag for next observation
			memory = 'Requested screenshot for next observation'
			logger.info(f'📸 {memory}')
			return ActionResult(
				extracted_content=memory,
				metadata={'include_screenshot': True},
			)
