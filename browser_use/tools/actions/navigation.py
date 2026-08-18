import asyncio
import logging
from typing import TYPE_CHECKING

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.events import GoBackEvent, NavigateToUrlEvent
from browser_use.tools.views import NavigateAction, NoParamsAction, SearchAction

if TYPE_CHECKING:
	from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.tools.service')


def register_navigation_actions(tools: 'Tools') -> None:
	"""Register search, navigation, history, and wait actions."""

	# Basic Navigation Actions
	@tools.registry.action(
		'',
		param_model=SearchAction,
		terminates_sequence=True,
	)
	async def search(params: SearchAction, browser_session: BrowserSession):
		import urllib.parse

		# Encode query for URL safety
		encoded_query = urllib.parse.quote_plus(params.query)

		# Build search URL based on search engine
		search_engines = {
			'duckduckgo': f'https://duckduckgo.com/?q={encoded_query}',
			'google': f'https://www.google.com/search?q={encoded_query}&udm=14',
			'bing': f'https://www.bing.com/search?q={encoded_query}',
		}

		if params.engine.lower() not in search_engines:
			return ActionResult(error=f'Unsupported search engine: {params.engine}. Options: duckduckgo, google, bing')

		search_url = search_engines[params.engine.lower()]

		# Simple tab logic: use current tab by default
		use_new_tab = False

		# Dispatch navigation event
		try:
			event = browser_session.event_bus.dispatch(
				NavigateToUrlEvent(
					url=search_url,
					new_tab=use_new_tab,
				)
			)
			await event
			await event.event_result(raise_if_any=True, raise_if_none=False)
			memory = f"Searched {params.engine.title()} for '{params.query}'"
			msg = f'🔍  {memory}'
			logger.info(msg)
			return ActionResult(extracted_content=memory, long_term_memory=memory)
		except Exception as e:
			logger.error(f'Failed to search {params.engine}: {e}')
			return ActionResult(error=f'Failed to search {params.engine} for "{params.query}": {str(e)}')

	@tools.registry.action(
		'',
		param_model=NavigateAction,
		terminates_sequence=True,
	)
	async def navigate(params: NavigateAction, browser_session: BrowserSession):
		try:
			# Dispatch navigation event
			event = browser_session.event_bus.dispatch(NavigateToUrlEvent(url=params.url, new_tab=params.new_tab))
			await event
			await event.event_result(raise_if_any=True, raise_if_none=False)

			# Health check: detect empty DOM for http/https pages and retry once.
			# Uses _root is None (truly blank) OR empty llm_representation() (no actionable
			# content for the LLM, e.g. SPA not yet rendered, empty body).
			# NOTE: llm_representation() returns a non-empty placeholder when _root is None,
			# so we must check _root is None separately — not rely on the repr string alone.
			def _page_appears_empty(s) -> bool:
				return s.dom_state._root is None or not s.dom_state.llm_representation().strip()

			if not params.new_tab:
				state = await browser_session.get_browser_state_summary(include_screenshot=False)
				url_is_http = state.url.lower().startswith(('http://', 'https://'))
				if url_is_http and _page_appears_empty(state):
					browser_session.logger.warning(
						f'⚠️ Empty DOM detected after navigation to {params.url}, waiting 3s and rechecking...'
					)
					await asyncio.sleep(3.0)
					state = await browser_session.get_browser_state_summary(include_screenshot=False)
					if state.url.lower().startswith(('http://', 'https://')) and _page_appears_empty(state):
						# Second attempt: reload the page and wait longer
						browser_session.logger.warning(f'⚠️ Still empty after 3s, attempting page reload for {params.url}...')
						reload_event = browser_session.event_bus.dispatch(NavigateToUrlEvent(url=params.url, new_tab=False))
						await reload_event
						await reload_event.event_result(raise_if_any=False, raise_if_none=False)
						await asyncio.sleep(5.0)
						state = await browser_session.get_browser_state_summary(include_screenshot=False)
						if state.url.lower().startswith(('http://', 'https://')) and state.dom_state._root is None:
							return ActionResult(
								error=f'Page loaded but returned empty content for {params.url}. '
								f'The page may require JavaScript that failed to render, use anti-bot measures, '
								f'or have a connection issue (e.g. tunnel/proxy error). Try a different URL or approach.'
							)

			if params.new_tab:
				memory = f'Opened new tab with URL {params.url}'
				msg = f'🔗  Opened new tab with url {params.url}'
			else:
				memory = f'Navigated to {params.url}'
				msg = f'🔗 {memory}'

			logger.info(msg)
			return ActionResult(extracted_content=msg, long_term_memory=memory)
		except Exception as e:
			error_msg = str(e)
			# Always log the actual error first for debugging
			browser_session.logger.error(f'❌ Navigation failed: {error_msg}')

			# Check if it's specifically a RuntimeError about CDP client
			if isinstance(e, RuntimeError) and 'CDP client not initialized' in error_msg:
				browser_session.logger.error('❌ Browser connection failed - CDP client not properly initialized')
				return ActionResult(error=f'Browser connection error: {error_msg}')
			# Check for network-related errors
			elif any(
				err in error_msg
				for err in [
					'ERR_NAME_NOT_RESOLVED',
					'ERR_INTERNET_DISCONNECTED',
					'ERR_CONNECTION_REFUSED',
					'ERR_TIMED_OUT',
					'ERR_TUNNEL_CONNECTION_FAILED',
					'net::',
				]
			):
				site_unavailable_msg = f'Navigation failed - site unavailable: {params.url}'
				browser_session.logger.warning(f'⚠️ {site_unavailable_msg} - {error_msg}')
				return ActionResult(error=site_unavailable_msg)
			else:
				# Return error in ActionResult instead of re-raising
				return ActionResult(error=f'Navigation failed: {str(e)}')

	@tools.registry.action('Go back', param_model=NoParamsAction, terminates_sequence=True)
	async def go_back(_: NoParamsAction, browser_session: BrowserSession):
		try:
			event = browser_session.event_bus.dispatch(GoBackEvent())
			await event
			memory = 'Navigated back'
			msg = f'🔙  {memory}'
			logger.info(msg)
			return ActionResult(extracted_content=memory)
		except Exception as e:
			logger.error(f'Failed to dispatch GoBackEvent: {type(e).__name__}: {e}')
			error_msg = f'Failed to go back: {str(e)}'
			return ActionResult(error=error_msg)

	@tools.registry.action('Wait for x seconds.')
	async def wait(seconds: int = 3):
		# Cap wait time at maximum 30 seconds
		actual_seconds = min(max(seconds - 1, 0), 30)
		memory = f'Waited for {seconds} seconds'
		logger.info(f'🕒 waited for {seconds} second{"" if seconds == 1 else "s"}')
		await asyncio.sleep(actual_seconds)
		return ActionResult(extracted_content=memory, long_term_memory=memory)
