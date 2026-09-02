"""Declarative Model Context Protocol tool catalog."""

import mcp.types as types


def get_mcp_tools() -> list[types.Tool]:
	return [
		# Agent tools
		# Direct browser control tools
		types.Tool(
			name='browser_navigate',
			description='Navigate to a URL in the browser',
			inputSchema={
				'type': 'object',
				'properties': {
					'url': {'type': 'string', 'description': 'The URL to navigate to'},
					'new_tab': {'type': 'boolean', 'description': 'Whether to open in a new tab', 'default': False},
				},
				'required': ['url'],
			},
		),
		types.Tool(
			name='browser_click',
			description='Click an element by index or at specific viewport coordinates. Use index for elements from browser_get_state, or coordinate_x/coordinate_y for pixel-precise clicking.',
			inputSchema={
				'type': 'object',
				'properties': {
					'index': {
						'type': 'integer',
						'description': 'The index of the element to click (from browser_get_state). Provide this OR coordinate_x+coordinate_y.',
					},
					'coordinate_x': {
						'type': 'integer',
						'description': 'X coordinate in pixels from the left edge of the viewport. Must be used together with coordinate_y. Provide this OR index.',
					},
					'coordinate_y': {
						'type': 'integer',
						'description': 'Y coordinate in pixels from the top edge of the viewport. Must be used together with coordinate_x. Provide this OR index.',
					},
					'new_tab': {
						'type': 'boolean',
						'description': 'Whether to open any resulting navigation in a new tab',
						'default': False,
					},
				},
			},
		),
		types.Tool(
			name='browser_type',
			description='Type text into an input field. Clears existing text by default; pass text="" to clear only.',
			inputSchema={
				'type': 'object',
				'properties': {
					'index': {
						'type': 'integer',
						'description': 'The index of the input element (from browser_get_state)',
					},
					'text': {
						'type': 'string',
						'description': 'The text to type. Pass an empty string ("") to clear the field without typing.',
					},
				},
				'required': ['index', 'text'],
			},
		),
		types.Tool(
			name='browser_get_state',
			description='Get the current state of the page including all interactive elements',
			inputSchema={
				'type': 'object',
				'properties': {
					'include_screenshot': {
						'type': 'boolean',
						'description': 'Whether to include a screenshot of the current page',
						'default': False,
					}
				},
			},
			annotations=types.ToolAnnotations(readOnlyHint=True),
		),
		types.Tool(
			name='browser_extract_content',
			description='Extract structured content from the current page based on a query',
			inputSchema={
				'type': 'object',
				'properties': {
					'query': {'type': 'string', 'description': 'What information to extract from the page'},
					'extract_links': {
						'type': 'boolean',
						'description': 'Whether to include links in the extraction',
						'default': False,
					},
				},
				'required': ['query'],
			},
		),
		types.Tool(
			name='browser_get_html',
			description='Get the raw HTML of the current page or a specific element by CSS selector',
			inputSchema={
				'type': 'object',
				'properties': {
					'selector': {
						'type': 'string',
						'description': 'Optional CSS selector to get HTML of a specific element. If omitted, returns full page HTML.',
					},
				},
			},
			annotations=types.ToolAnnotations(readOnlyHint=True),
		),
		types.Tool(
			name='browser_screenshot',
			description='Take a screenshot of the current page. Returns viewport metadata as text and the screenshot as an image.',
			inputSchema={
				'type': 'object',
				'properties': {
					'full_page': {
						'type': 'boolean',
						'description': 'Whether to capture the full scrollable page or just the visible viewport',
						'default': False,
					},
				},
			},
			annotations=types.ToolAnnotations(readOnlyHint=True),
		),
		types.Tool(
			name='browser_scroll',
			description='Scroll the page',
			inputSchema={
				'type': 'object',
				'properties': {
					'direction': {
						'type': 'string',
						'enum': ['up', 'down'],
						'description': 'Direction to scroll',
						'default': 'down',
					}
				},
			},
		),
		types.Tool(
			name='browser_go_back',
			description='Go back to the previous page',
			inputSchema={'type': 'object', 'properties': {}},
		),
		# Tab management
		types.Tool(
			name='browser_list_tabs',
			description='List all open tabs',
			inputSchema={'type': 'object', 'properties': {}},
			annotations=types.ToolAnnotations(readOnlyHint=True),
		),
		types.Tool(
			name='browser_switch_tab',
			description='Switch to a different tab',
			inputSchema={
				'type': 'object',
				'properties': {'tab_id': {'type': 'string', 'description': '4 Character Tab ID of the tab to switch to'}},
				'required': ['tab_id'],
			},
		),
		types.Tool(
			name='browser_close_tab',
			description='Close a tab',
			inputSchema={
				'type': 'object',
				'properties': {'tab_id': {'type': 'string', 'description': '4 Character Tab ID of the tab to close'}},
				'required': ['tab_id'],
			},
		),
		types.Tool(
			name='retry_with_browser_use_agent',
			description='Retry a task using the browser-use agent. Only use this as a last resort if you fail to interact with a page multiple times.',
			inputSchema={
				'type': 'object',
				'properties': {
					'task': {
						'type': 'string',
						'description': 'The high-level goal and detailed step-by-step description of the task the AI browser agent needs to attempt, along with any relevant data needed to complete the task and info about previous attempts.',
					},
					'max_steps': {
						'type': 'integer',
						'description': 'Maximum number of steps an agent can take.',
						'default': 100,
					},
					'model': {
						'type': 'string',
						'description': 'LLM model to use (e.g., gpt-4o, claude-3-opus-20240229). Defaults to the configured model.',
					},
					'allowed_domains': {
						'type': 'array',
						'items': {'type': 'string'},
						'description': (
							'List of domains the agent is allowed to visit (security feature). '
							'Omit to use the server-configured profile defaults. '
							'An empty list is treated the same as omitting the argument and '
							'will NOT disable server-configured restrictions.'
						),
					},
					'use_vision': {
						'type': 'boolean',
						'description': 'Whether to use vision capabilities (screenshots) for the agent',
						'default': True,
					},
				},
				'required': ['task'],
			},
		),
		# Browser session management tools
		types.Tool(
			name='browser_list_sessions',
			description='List all active browser sessions with their details and last activity time',
			inputSchema={'type': 'object', 'properties': {}},
			annotations=types.ToolAnnotations(readOnlyHint=True),
		),
		types.Tool(
			name='browser_close_session',
			description='Close a specific browser session by its ID',
			inputSchema={
				'type': 'object',
				'properties': {
					'session_id': {
						'type': 'string',
						'description': 'The browser session ID to close (get from browser_list_sessions)',
					}
				},
				'required': ['session_id'],
			},
		),
		types.Tool(
			name='browser_close_all',
			description='Close all active browser sessions and clean up resources',
			inputSchema={'type': 'object', 'properties': {}},
		),
	]
