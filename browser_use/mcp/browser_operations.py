from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from browser_use import ActionModel, Agent
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.config import get_default_llm, get_default_profile
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm import ChatAWSBedrock
from browser_use.llm.openai.chat import ChatOpenAI
from browser_use.tools.service import Tools

if TYPE_CHECKING:
	from browser_use.mcp.server import BrowserUseServer

logger = logging.getLogger(__name__)


class McpBrowserOperations:
	"""Execute MCP browser operations independently of protocol registration."""

	def __init__(self, server: BrowserUseServer) -> None:
		self.server = server

	async def _init_browser_session(self, allowed_domains: list[str] | None = None, **kwargs):
		"""Initialize browser session using config"""
		if self.server.browser_session:
			return

		# Ensure all logging goes to stderr before browser initialization
		from browser_use.mcp.server import _ensure_all_loggers_use_stderr

		_ensure_all_loggers_use_stderr()

		logger.debug('Initializing browser session...')

		# Get profile config
		profile_config = get_default_profile(self.server.config)

		# Merge profile config with defaults and overrides
		profile_data = {
			'downloads_path': str(Path.home() / 'Downloads' / 'browser-use-mcp'),
			'wait_between_actions': 0.5,
			'keep_alive': True,
			'user_data_dir': '~/.config/browseruse/profiles/default',
			'device_scale_factor': 1.0,
			'disable_security': False,
			'headless': False,
			**profile_config,  # Config values override defaults
		}

		# Tool parameter overrides (highest priority)
		if allowed_domains is not None:
			profile_data['allowed_domains'] = allowed_domains

		# Merge any additional kwargs that are valid BrowserProfile fields
		for key, value in kwargs.items():
			profile_data[key] = value

		# Create browser profile
		profile = BrowserProfile(**profile_data)

		# Create browser session
		self.server.browser_session = BrowserSession(browser_profile=profile)
		await self.server.browser_session.start()

		# Track the session for management
		self.server._track_session(self.server.browser_session)

		# Create tools for direct actions
		self.server.tools = Tools()

		# Initialize LLM from config
		llm_config = get_default_llm(self.server.config)
		base_url = llm_config.get('base_url', None)
		kwargs = {}
		if base_url:
			kwargs['base_url'] = base_url
		if api_key := llm_config.get('api_key'):
			self.server.llm = ChatOpenAI(
				model=llm_config.get('model', 'gpt-o4-mini'),
				api_key=api_key,
				temperature=llm_config.get('temperature', 0.7),
				**kwargs,
			)

		# Initialize FileSystem for extraction actions
		file_system_path = profile_config.get('file_system_path', '~/.browser-use-mcp')
		self.server.file_system = FileSystem(base_dir=Path(file_system_path).expanduser())

		logger.debug('Browser session initialized')

	async def _retry_with_browser_use_agent(
		self,
		task: str,
		max_steps: int = 100,
		model: str | None = None,
		allowed_domains: list[str] | None = None,
		use_vision: bool = True,
	) -> str:
		"""Run an autonomous agent task."""
		logger.debug(f'Running agent task: {task}')

		# Get LLM config
		llm_config = get_default_llm(self.server.config)

		# Get LLM provider
		model_provider = llm_config.get('model_provider') or os.getenv('MODEL_PROVIDER')

		# Get Bedrock-specific config
		if model_provider and model_provider.lower() == 'bedrock':
			llm_model = llm_config.get('model') or os.getenv('MODEL') or 'us.anthropic.claude-sonnet-4-6'
			aws_region = llm_config.get('region') or os.getenv('REGION')
			if not aws_region:
				aws_region = 'us-east-1'
			aws_sso_auth = llm_config.get('aws_sso_auth', False)
			llm = ChatAWSBedrock(
				model=llm_model,  # or any Bedrock model
				aws_region=aws_region,
				aws_sso_auth=aws_sso_auth,
			)
		else:
			api_key = llm_config.get('api_key') or os.getenv('OPENAI_API_KEY')
			if not api_key:
				return 'Error: OPENAI_API_KEY not set in config or environment'

			# Use explicit model from tool call, otherwise fall back to configured default
			llm_model = model or llm_config.get('model', 'gpt-4o')

			base_url = llm_config.get('base_url', None)
			kwargs = {}
			if base_url:
				kwargs['base_url'] = base_url
			llm = ChatOpenAI(
				model=llm_model,
				api_key=api_key,
				temperature=llm_config.get('temperature', 0.7),
				**kwargs,
			)

		# Get profile config and merge with tool parameters
		profile_config = get_default_profile(self.server.config)

		# Override allowed_domains only when the client supplied a non-empty list.
		# Treating an empty list as an override would silently disable any
		# admin-configured allowlist on the default profile, since
		# SecurityWatchdog interprets allowed_domains=[] as "no restrictions".
		if allowed_domains:
			profile_config['allowed_domains'] = allowed_domains

		# Create browser profile using config
		profile = BrowserProfile(**profile_config)

		# Create and run agent
		agent = Agent(
			task=task,
			llm=llm,
			browser_profile=profile,
			use_vision=use_vision,
		)

		try:
			history = await agent.run(max_steps=max_steps)

			# Format results
			results = []
			results.append(f'Task completed in {len(history.history)} steps')
			results.append(f'Success: {history.is_successful()}')

			# Get final result if available
			final_result = history.final_result()
			if final_result:
				results.append(f'\nFinal result:\n{final_result}')

			# Include any errors
			errors = history.errors()
			if errors:
				results.append(f'\nErrors encountered:\n{json.dumps(errors, indent=2)}')

			# Include URLs visited
			urls = history.urls()
			if urls:
				# Filter out None values and convert to strings
				valid_urls = [str(url) for url in urls if url is not None]
				if valid_urls:
					results.append(f'\nURLs visited: {", ".join(valid_urls)}')

			return '\n'.join(results)

		except Exception as e:
			logger.error(f'Agent task failed: {e}', exc_info=True)
			return f'Agent task failed: {str(e)}'
		finally:
			# Clean up
			await agent.close()

	async def _navigate(self, url: str, new_tab: bool = False) -> str:
		"""Navigate to a URL."""
		if not self.server.browser_session:
			return 'Error: No browser session active'

		# Update session activity
		self.server._update_session_activity(self.server.browser_session.id)

		from browser_use.browser.events import NavigateToUrlEvent

		if new_tab:
			event = self.server.browser_session.event_bus.dispatch(NavigateToUrlEvent(url=url, new_tab=True))
			await event
			return f'Opened new tab with URL: {url}'
		else:
			event = self.server.browser_session.event_bus.dispatch(NavigateToUrlEvent(url=url))
			await event
			return f'Navigated to: {url}'

	async def _click(
		self,
		index: int | None = None,
		coordinate_x: int | None = None,
		coordinate_y: int | None = None,
		new_tab: bool = False,
	) -> str:
		"""Click an element by index or at viewport coordinates."""
		if not self.server.browser_session:
			return 'Error: No browser session active'

		# Update session activity
		self.server._update_session_activity(self.server.browser_session.id)

		# Coordinate-based clicking
		if coordinate_x is not None and coordinate_y is not None:
			from browser_use.browser.events import ClickCoordinateEvent

			event = self.server.browser_session.event_bus.dispatch(
				ClickCoordinateEvent(coordinate_x=coordinate_x, coordinate_y=coordinate_y)
			)
			await event
			return f'Clicked at coordinates ({coordinate_x}, {coordinate_y})'

		# Index-based clicking
		if index is None:
			return 'Error: Provide either index or both coordinate_x and coordinate_y'

		# Get the element
		element = await self.server.browser_session.dom_state.get_dom_element_by_index(index)
		if not element:
			return f'Element with index {index} not found'

		if new_tab:
			# For links, extract href and open in new tab
			href = element.attributes.get('href')
			if href:
				# Convert relative href to absolute URL
				state = await self.server.browser_session.get_browser_state_summary()
				current_url = state.url
				if href.startswith('/'):
					# Relative URL - construct full URL
					from urllib.parse import urlparse

					parsed = urlparse(current_url)
					full_url = f'{parsed.scheme}://{parsed.netloc}{href}'
				else:
					full_url = href

				# Open link in new tab
				from browser_use.browser.events import NavigateToUrlEvent

				event = self.server.browser_session.event_bus.dispatch(NavigateToUrlEvent(url=full_url, new_tab=True))
				await event
				return f'Clicked element {index} and opened in new tab {full_url[:20]}...'
			else:
				# For non-link elements, just do a normal click
				from browser_use.browser.events import ClickElementEvent

				event = self.server.browser_session.event_bus.dispatch(ClickElementEvent(node=element))
				await event
				return f'Clicked element {index} (new tab not supported for non-link elements)'
		else:
			# Normal click
			from browser_use.browser.events import ClickElementEvent

			event = self.server.browser_session.event_bus.dispatch(ClickElementEvent(node=element))
			await event
			return f'Clicked element {index}'

	async def _type_text(self, index: int, text: str) -> str:
		"""Type text into an element."""
		if not self.server.browser_session:
			return 'Error: No browser session active'

		element = await self.server.browser_session.dom_state.get_dom_element_by_index(index)
		if not element:
			return f'Element with index {index} not found'

		from browser_use.browser.events import TypeTextEvent

		# Conservative heuristic to detect potentially sensitive data
		# Only flag very obvious patterns to minimize false positives
		is_potentially_sensitive = len(text) >= 6 and (
			# Email pattern: contains @ and a domain-like suffix
			('@' in text and '.' in text.split('@')[-1] if '@' in text else False)
			# Mixed alphanumeric with reasonable complexity (likely API keys/tokens)
			or (
				len(text) >= 16
				and any(char.isdigit() for char in text)
				and any(char.isalpha() for char in text)
				and any(char in '.-_' for char in text)
			)
		)

		# Use generic key names to avoid information leakage about detection patterns
		sensitive_key_name = None
		if is_potentially_sensitive:
			if '@' in text and '.' in text.split('@')[-1]:
				sensitive_key_name = 'email'
			else:
				sensitive_key_name = 'credential'

		event = self.server.browser_session.event_bus.dispatch(
			TypeTextEvent(node=element, text=text, is_sensitive=is_potentially_sensitive, sensitive_key_name=sensitive_key_name)
		)
		await event

		if is_potentially_sensitive:
			if sensitive_key_name:
				return f'Typed <{sensitive_key_name}> into element {index}'
			else:
				return f'Typed <sensitive> into element {index}'
		else:
			return f"Typed '{text}' into element {index}"

	async def _get_browser_state(self, include_screenshot: bool = False) -> tuple[str, str | None]:
		"""Get current browser state. Returns (state_json, screenshot_b64 | None)."""
		if not self.server.browser_session:
			return 'Error: No browser session active', None

		state = await self.server.browser_session.get_browser_state_summary()

		result: dict[str, Any] = {
			'url': state.url,
			'title': state.title,
			'tabs': [{'url': tab.url, 'title': tab.title} for tab in state.tabs],
			'interactive_elements': [],
		}

		# Add viewport info so the LLM knows the coordinate space
		if state.page_info:
			pi = state.page_info
			result['viewport'] = {
				'width': pi.viewport_width,
				'height': pi.viewport_height,
			}
			result['page'] = {
				'width': pi.page_width,
				'height': pi.page_height,
			}
			result['scroll'] = {
				'x': pi.scroll_x,
				'y': pi.scroll_y,
			}

		# Add interactive elements with their indices
		for index, element in state.dom_state.selector_map.items():
			elem_info: dict[str, Any] = {
				'index': index,
				'tag': element.tag_name,
				'text': element.get_all_children_text(max_depth=2)[:100],
			}
			if element.attributes.get('placeholder'):
				elem_info['placeholder'] = element.attributes['placeholder']
			if element.attributes.get('href'):
				elem_info['href'] = element.attributes['href']
			result['interactive_elements'].append(elem_info)

		# Return screenshot separately as ImageContent instead of embedding base64 in JSON
		screenshot_b64 = None
		if include_screenshot and state.screenshot:
			screenshot_b64 = state.screenshot
			# Include viewport dimensions in JSON so LLM can map pixels to coordinates
			if state.page_info:
				result['screenshot_dimensions'] = {
					'width': state.page_info.viewport_width,
					'height': state.page_info.viewport_height,
				}

		return json.dumps(result, indent=2), screenshot_b64

	async def _get_html(self, selector: str | None = None) -> str:
		"""Get raw HTML of the page or a specific element."""
		if not self.server.browser_session:
			return 'Error: No browser session active'

		self.server._update_session_activity(self.server.browser_session.id)

		cdp_session = await self.server.browser_session.get_or_create_cdp_session(target_id=None, focus=False)
		if not cdp_session:
			return 'Error: No active CDP session'

		if selector:
			js = (
				f'(function(){{ const el = document.querySelector({json.dumps(selector)}); return el ? el.outerHTML : null; }})()'
			)
		else:
			js = 'document.documentElement.outerHTML'

		result = await cdp_session.cdp_client.send.Runtime.evaluate(
			params={'expression': js, 'returnByValue': True},
			session_id=cdp_session.session_id,
		)
		html = result.get('result', {}).get('value')
		if html is None:
			return f'No element found for selector: {selector}' if selector else 'Error: Could not get page HTML'
		return html

	async def _screenshot(self, full_page: bool = False) -> tuple[str, str | None]:
		"""Take a screenshot. Returns (metadata_json, screenshot_b64 | None)."""
		if not self.server.browser_session:
			return 'Error: No browser session active', None

		import base64

		self.server._update_session_activity(self.server.browser_session.id)

		data = await self.server.browser_session.take_screenshot(full_page=full_page)
		b64 = base64.b64encode(data).decode()

		# Return screenshot separately as ImageContent instead of embedding base64 in JSON
		state = await self.server.browser_session.get_browser_state_summary()
		result: dict[str, Any] = {
			'size_bytes': len(data),
		}
		if state.page_info:
			result['viewport'] = {
				'width': state.page_info.viewport_width,
				'height': state.page_info.viewport_height,
			}
		return json.dumps(result), b64

	async def _extract_content(self, query: str, extract_links: bool = False) -> str:
		"""Extract content from current page."""
		if not self.server.llm:
			return 'Error: LLM not initialized (set OPENAI_API_KEY)'

		if not self.server.file_system:
			return 'Error: FileSystem not initialized'

		if not self.server.browser_session:
			return 'Error: No browser session active'

		if not self.server.tools:
			return 'Error: Tools not initialized'

		state = await self.server.browser_session.get_browser_state_summary()

		# Use the extract action
		# Create a dynamic action model that matches the tools's expectations
		from pydantic import create_model

		# Create action model dynamically
		ExtractAction = create_model(
			'ExtractAction',
			__base__=ActionModel,
			extract=dict[str, Any],
		)

		# Use model_validate because Pyright does not understand the dynamic model
		action = ExtractAction.model_validate(
			{
				'extract': {'query': query, 'extract_links': extract_links},
			}
		)
		action_result = await self.server.tools.act(
			action=action,
			browser_session=self.server.browser_session,
			page_extraction_llm=self.server.llm,
			file_system=self.server.file_system,
		)

		return action_result.extracted_content or 'No content extracted'

	async def _scroll(self, direction: str = 'down') -> str:
		"""Scroll the page."""
		if not self.server.browser_session:
			return 'Error: No browser session active'

		from browser_use.browser.events import ScrollEvent

		# Scroll by a standard amount (500 pixels)
		event = self.server.browser_session.event_bus.dispatch(
			ScrollEvent(
				direction=direction,  # type: ignore
				amount=500,
			)
		)
		await event
		return f'Scrolled {direction}'

	async def _go_back(self) -> str:
		"""Go back in browser history."""
		if not self.server.browser_session:
			return 'Error: No browser session active'

		from browser_use.browser.events import GoBackEvent

		event = self.server.browser_session.event_bus.dispatch(GoBackEvent())
		await event
		return 'Navigated back'

	async def _close_browser(self) -> str:
		"""Close the browser session."""
		if self.server.browser_session:
			await self.server.browser_session.kill()
			self.server.browser_session = None
			self.server.tools = None
			return 'Browser closed'
		return 'No browser session to close'

	async def _list_tabs(self) -> str:
		"""List all open tabs."""
		if not self.server.browser_session:
			return 'Error: No browser session active'

		tabs_info = await self.server.browser_session.get_tabs()
		tabs = []
		for i, tab in enumerate(tabs_info):
			tabs.append({'tab_id': tab.target_id[-4:], 'url': tab.url, 'title': tab.title or ''})
		return json.dumps(tabs, indent=2)

	async def _switch_tab(self, tab_id: str) -> str:
		"""Switch to a different tab."""
		if not self.server.browser_session:
			return 'Error: No browser session active'

		from browser_use.browser.events import SwitchTabEvent

		target_id = await self.server.browser_session.session_manager.get_target_id_from_tab_id(tab_id)
		event = self.server.browser_session.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
		await event
		state = await self.server.browser_session.get_browser_state_summary()
		return f'Switched to tab {tab_id}: {state.url}'

	async def _close_tab(self, tab_id: str) -> str:
		"""Close a specific tab."""
		if not self.server.browser_session:
			return 'Error: No browser session active'

		from browser_use.browser.events import CloseTabEvent

		target_id = await self.server.browser_session.session_manager.get_target_id_from_tab_id(tab_id)
		event = self.server.browser_session.event_bus.dispatch(CloseTabEvent(target_id=target_id))
		await event
		current_url = await self.server.browser_session.get_current_page_url()
		return f'Closed tab # {tab_id}, now on {current_url}'
