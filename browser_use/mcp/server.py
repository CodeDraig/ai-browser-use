"""MCP Server for browser-use - exposes browser automation capabilities via Model Context Protocol.

This server provides tools for:
- Running autonomous browser tasks with an AI agent
- Direct browser control (navigation, clicking, typing, etc.)
- Content extraction from web pages
- File system operations

Usage:
    browser-use --mcp

Or as an MCP server in Claude Desktop or other MCP clients:
    {
        "mcpServers": {
            "browser-use": {
                "command": "browser-use",
                "args": ["--mcp"],
                "env": {
                    "OPENAI_API_KEY": "sk-proj-1234567890",
                }
            }
        }
    }
"""

import os
import sys

# Set environment variables BEFORE any browser_use imports to prevent early logging
os.environ['BROWSER_USE_LOGGING_LEVEL'] = 'critical'
os.environ['BROWSER_USE_SETUP_LOGGING'] = 'false'

import asyncio
import logging
from pathlib import Path
from typing import Any

# Configure logging for MCP mode - redirect to stderr but preserve critical diagnostics
logging.basicConfig(
	stream=sys.stderr, level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', force=True
)

# Add browser-use to path if running from source
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import and configure logging to use stderr before other imports
from browser_use.logging_config import setup_logging


def _configure_mcp_server_logging():
	"""Configure logging for MCP server mode - redirect all logs to stderr to prevent JSON RPC interference."""
	# Set environment to suppress browser-use logging during server mode
	os.environ['BROWSER_USE_LOGGING_LEVEL'] = 'warning'
	os.environ['BROWSER_USE_SETUP_LOGGING'] = 'false'  # Prevent automatic logging setup

	# Configure logging to stderr for MCP mode - preserve warnings and above for troubleshooting
	setup_logging(stream=sys.stderr, log_level='warning', force_setup=True)

	# Also configure the root logger and all existing loggers to use stderr
	logging.root.handlers = []
	stderr_handler = logging.StreamHandler(sys.stderr)
	stderr_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
	logging.root.addHandler(stderr_handler)
	logging.root.setLevel(logging.CRITICAL)

	# Configure all existing loggers to use stderr and CRITICAL level
	for name in list(logging.root.manager.loggerDict.keys()):
		logger_obj = logging.getLogger(name)
		logger_obj.handlers = []
		logger_obj.setLevel(logging.CRITICAL)
		logger_obj.addHandler(stderr_handler)
		logger_obj.propagate = False


# Configure MCP server logging before any browser_use imports to capture early log lines
_configure_mcp_server_logging()

# Additional suppression - disable all logging completely for MCP mode
logging.disable(logging.CRITICAL)

# Import browser_use modules
from browser_use.browser import BrowserSession
from browser_use.config import load_browser_use_config
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm.openai.chat import ChatOpenAI
from browser_use.tools.service import Tools

logger = logging.getLogger(__name__)


def _ensure_all_loggers_use_stderr():
	"""Ensure ALL loggers only output to stderr, not stdout."""
	# Get the stderr handler
	stderr_handler = None
	for handler in logging.root.handlers:
		if hasattr(handler, 'stream') and handler.stream == sys.stderr:  # type: ignore
			stderr_handler = handler
			break

	if not stderr_handler:
		stderr_handler = logging.StreamHandler(sys.stderr)
		stderr_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

	# Configure root logger
	logging.root.handlers = [stderr_handler]
	logging.root.setLevel(logging.CRITICAL)

	# Configure all existing loggers
	for name in list(logging.root.manager.loggerDict.keys()):
		logger_obj = logging.getLogger(name)
		logger_obj.handlers = [stderr_handler]
		logger_obj.setLevel(logging.CRITICAL)
		logger_obj.propagate = False


# Ensure stderr logging after all imports
_ensure_all_loggers_use_stderr()


# Try to import MCP SDK
try:
	import mcp.server.stdio
	import mcp.types as types
	from mcp.server import NotificationOptions, Server
	from mcp.server.models import InitializationOptions

	MCP_AVAILABLE = True

	# Configure MCP SDK logging to stderr as well
	mcp_logger = logging.getLogger('mcp')
	mcp_logger.handlers = []
	mcp_logger.addHandler(logging.root.handlers[0] if logging.root.handlers else logging.StreamHandler(sys.stderr))
	mcp_logger.setLevel(logging.ERROR)
	mcp_logger.propagate = False
except ImportError:
	MCP_AVAILABLE = False
	logger.error('MCP SDK not installed. Install with: pip install mcp')
	sys.exit(1)

from browser_use.mcp.agent_operations import McpAgentOperations
from browser_use.mcp.browser_operations import McpBrowserOperations
from browser_use.mcp.session_registry import McpSessionRegistry
from browser_use.mcp.tool_catalog import get_mcp_tools


class BrowserUseServer:
	"""MCP Server for browser-use capabilities."""

	def __init__(self, session_timeout_minutes: int = 10):
		# Ensure all logging goes to stderr (in case new loggers were created)
		_ensure_all_loggers_use_stderr()

		self.server = Server('browser-use')
		self.config = load_browser_use_config()
		self.browser_session: BrowserSession | None = None
		self.tools: Tools | None = None
		self.llm: ChatOpenAI | None = None
		self.file_system: FileSystem | None = None
		self.agent_operations = McpAgentOperations(self)
		self.browser_operations = McpBrowserOperations(self)

		self.session_registry = McpSessionRegistry(self, session_timeout_minutes)

		# Setup handlers
		self._setup_handlers()

	def _setup_handlers(self):
		"""Setup MCP server handlers."""

		@self.server.list_tools()
		async def handle_list_tools() -> list[types.Tool]:
			"""List all available browser-use tools."""
			return get_mcp_tools()

		@self.server.list_resources()
		async def handle_list_resources() -> list[types.Resource]:
			"""List available resources (none for browser-use)."""
			return []

		@self.server.list_prompts()
		async def handle_list_prompts() -> list[types.Prompt]:
			"""List available prompts (none for browser-use)."""
			return []

		@self.server.call_tool()
		async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent | types.ImageContent]:
			"""Handle tool execution."""
			try:
				result = await self._execute_tool(name, arguments or {})
				if isinstance(result, list):
					return result
				return [types.TextContent(type='text', text=result)]
			except Exception as e:
				logger.error(f'Tool execution failed: {e}', exc_info=True)
				return [types.TextContent(type='text', text=f'Error: {str(e)}')]

	async def _execute_tool(
		self, tool_name: str, arguments: dict[str, Any]
	) -> str | list[types.TextContent | types.ImageContent]:
		"""Execute a browser-use tool. Returns str for most tools, or a content list for tools with image output."""

		# Agent-based tools
		if tool_name == 'retry_with_browser_use_agent':
			return await self.agent_operations._retry_with_browser_use_agent(
				task=arguments['task'],
				max_steps=arguments.get('max_steps', 100),
				model=arguments.get('model'),
				allowed_domains=arguments.get('allowed_domains'),
				use_vision=arguments.get('use_vision', True),
			)

		# Browser session management tools (don't require active session)
		if tool_name == 'browser_list_sessions':
			return await self.session_registry.list_sessions()

		elif tool_name == 'browser_close_session':
			return await self.session_registry.close_session(arguments['session_id'])

		elif tool_name == 'browser_close_all':
			return await self.session_registry.close_all_sessions()

		# Direct browser control tools (require active session)
		elif tool_name.startswith('browser_'):
			# Ensure browser session exists
			if not self.browser_session:
				await self.browser_operations._init_browser_session()

			if tool_name == 'browser_navigate':
				return await self.browser_operations._navigate(arguments['url'], arguments.get('new_tab', False))

			elif tool_name == 'browser_click':
				return await self.browser_operations._click(
					index=arguments.get('index'),
					coordinate_x=arguments.get('coordinate_x'),
					coordinate_y=arguments.get('coordinate_y'),
					new_tab=arguments.get('new_tab', False),
				)

			elif tool_name == 'browser_type':
				return await self.browser_operations._type_text(arguments['index'], arguments['text'])

			elif tool_name == 'browser_get_state':
				state_json, screenshot_b64 = await self.browser_operations._get_browser_state(
					arguments.get('include_screenshot', False)
				)
				content: list[types.TextContent | types.ImageContent] = [types.TextContent(type='text', text=state_json)]
				if screenshot_b64:
					content.append(types.ImageContent(type='image', data=screenshot_b64, mimeType='image/png'))
				return content

			elif tool_name == 'browser_get_html':
				return await self.browser_operations._get_html(arguments.get('selector'))

			elif tool_name == 'browser_screenshot':
				meta_json, screenshot_b64 = await self.browser_operations._screenshot(arguments.get('full_page', False))
				content: list[types.TextContent | types.ImageContent] = [types.TextContent(type='text', text=meta_json)]
				if screenshot_b64:
					content.append(types.ImageContent(type='image', data=screenshot_b64, mimeType='image/png'))
				return content

			elif tool_name == 'browser_extract_content':
				return await self.browser_operations._extract_content(arguments['query'], arguments.get('extract_links', False))

			elif tool_name == 'browser_scroll':
				return await self.browser_operations._scroll(arguments.get('direction', 'down'))

			elif tool_name == 'browser_go_back':
				return await self.browser_operations._go_back()

			elif tool_name == 'browser_list_tabs':
				return await self.browser_operations._list_tabs()

			elif tool_name == 'browser_switch_tab':
				return await self.browser_operations._switch_tab(arguments['tab_id'])

			elif tool_name == 'browser_close_tab':
				return await self.browser_operations._close_tab(arguments['tab_id'])

		return f'Unknown tool: {tool_name}'

	async def run(self):
		"""Run the MCP server."""
		# Start the cleanup task
		await self.session_registry.start_cleanup_task()

		if sys.stdin is None:
			raise RuntimeError('MCP stdio transport requires stdin, but this process was launched without one.')

		async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
			try:
				await self.server.run(
					read_stream,
					write_stream,
					InitializationOptions(
						server_name='browser-use',
						server_version='0.1.0',
						capabilities=self.server.get_capabilities(
							notification_options=NotificationOptions(),
							experimental_capabilities={},
						),
					),
				)
			except BrokenPipeError:
				logger.warning('MCP client disconnected while writing to stdio; shutting down server cleanly.')


async def main(session_timeout_minutes: int = 10):
	if not MCP_AVAILABLE:
		print('MCP SDK is required. Install with: pip install mcp', file=sys.stderr)
		sys.exit(1)

	server = BrowserUseServer(session_timeout_minutes=session_timeout_minutes)
	await server.run()


if __name__ == '__main__':
	asyncio.run(main())
