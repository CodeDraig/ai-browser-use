import logging
from typing import TYPE_CHECKING

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import CloseTabEvent, SwitchTabEvent
from browser_use.tools.views import CloseTabAction, SwitchTabAction

if TYPE_CHECKING:
	from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.tools.service')


def register_tab_actions(tools: 'Tools') -> None:
	"""Register tab switching and closing actions."""

	@tools.registry.action(
		'Switch to another open tab by tab_id. Tab IDs are shown in browser state tabs list (last 4 chars of target_id). Use when you need to work with content in a different tab.',
		param_model=SwitchTabAction,
		terminates_sequence=True,
	)
	async def switch(params: SwitchTabAction, browser_session: BrowserSession):
		# Simple switch tab logic
		try:
			target_id = await browser_session.session_manager.get_target_id_from_tab_id(params.tab_id)

			event = browser_session.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
			await event
			new_target_id = await event.event_result(raise_if_any=False, raise_if_none=False)  # Don't raise on errors

			if new_target_id:
				memory = f'Switched to tab #{new_target_id[-4:]}'
			else:
				memory = f'Switched to tab #{params.tab_id}'

			logger.info(f'🔄  {memory}')
			return ActionResult(extracted_content=memory, long_term_memory=memory)
		except Exception as e:
			logger.warning(f'Tab switch may have failed: {e}')
			memory = f'Attempted to switch to tab #{params.tab_id}'
			return ActionResult(extracted_content=memory, long_term_memory=memory)

	@tools.registry.action(
		'Close a tab by tab_id. Tab IDs are shown in browser state tabs list (last 4 chars of target_id). Use to clean up tabs you no longer need.',
		param_model=CloseTabAction,
	)
	async def close(params: CloseTabAction, browser_session: BrowserSession):
		# Simple close tab logic
		try:
			target_id = await browser_session.session_manager.get_target_id_from_tab_id(params.tab_id)

			# Dispatch close tab event - handle stale target IDs gracefully
			event = browser_session.event_bus.dispatch(CloseTabEvent(target_id=target_id))
			await event
			await event.event_result(raise_if_any=False, raise_if_none=False)  # Don't raise on errors

			memory = f'Closed tab #{params.tab_id}'
			logger.info(f'🗑️  {memory}')
			return ActionResult(
				extracted_content=memory,
				long_term_memory=memory,
			)
		except Exception as e:
			# Handle stale target IDs gracefully
			logger.warning(f'Tab {params.tab_id} may already be closed: {e}')
			memory = f'Tab #{params.tab_id} closed (was already closed or invalid)'
			return ActionResult(
				extracted_content=memory,
				long_term_memory=memory,
			)
