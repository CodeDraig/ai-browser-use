from __future__ import annotations

import asyncio
import gc
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar

from dotenv import load_dotenv

from browser_use.llm.base import BaseChatModel
from browser_use.llm.messages import ContentPartImageParam, ContentPartTextParam
from browser_use.tokens.service import TokenCost

load_dotenv()

from bubus import EventBus
from uuid_extensions import uuid7str

from browser_use import Browser, BrowserProfile
from browser_use.agent.construction import AgentConstruction
from browser_use.agent.execution import AgentExecution
from browser_use.agent.history import AgentHistory, AgentHistoryList, AgentStructuredOutput
from browser_use.agent.history_replay import AgentHistoryReplay

# Lazy import for gif to avoid heavy agent.views import at startup
# from browser_use.agent.gif import create_history_gif
from browser_use.agent.message_manager.service import (
	MessageManager,
)
from browser_use.agent.model_interaction import AgentModelInteraction
from browser_use.agent.model_settings import AgentModelSettings
from browser_use.agent.results import ActionResult, AgentOutput
from browser_use.agent.settings import AgentSettings, MessageCompactionSettings
from browser_use.agent.state import AgentState, AgentStepInfo
from browser_use.agent.state_restoration import AgentStateRestoration
from browser_use.agent.variables import DetectedVariable
from browser_use.browser.events import _get_timeout
from browser_use.browser.views import BrowserStateHistory, BrowserStateSummary
from browser_use.config import get_environment_config
from browser_use.logging_utils import log_pretty_path, time_execution_async, time_execution_sync
from browser_use.runtime import SignalHandler
from browser_use.security import SensitiveData, warn_sensitive_data_domain_constraints
from browser_use.tools.service import Tools
from browser_use.version import check_latest_browser_use_version

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
	from browser_use.filesystem.file_system import FileSystem
	from browser_use.screenshots.service import ScreenshotService
	from browser_use.tools.registry.views import ActionModel


Context = TypeVar('Context')


AgentHookFunc = Callable[['Agent'], Awaitable[None]]


class Agent(Generic[Context, AgentStructuredOutput]):
	tools: Tools[Context]
	file_system: FileSystem
	file_system_path: str
	screenshot_service: ScreenshotService
	version: str
	source: str
	ActionModel: type[ActionModel]
	DoneActionModel: type[ActionModel]
	AgentOutput: type[AgentOutput]
	DoneAgentOutput: type[AgentOutput]

	@time_execution_sync('--init')
	def __init__(
		self,
		task: str,
		llm: BaseChatModel | None = None,
		# Optional parameters
		browser_profile: BrowserProfile | None = None,
		browser: Browser | None = None,
		tools: Tools[Context] | None = None,
		# Initial agent run parameters
		sensitive_data: SensitiveData | None = None,
		initial_actions: list[dict[str, dict[str, Any]]] | None = None,
		# Cloud Callbacks
		register_new_step_callback: (
			Callable[[BrowserStateSummary, AgentOutput, int], None]  # Sync callback
			| Callable[[BrowserStateSummary, AgentOutput, int], Awaitable[None]]  # Async callback
			| None
		) = None,
		register_done_callback: (
			Callable[[AgentHistoryList], Awaitable[None]]  # Async Callback
			| Callable[[AgentHistoryList], None]  # Sync Callback
			| None
		) = None,
		register_external_agent_status_raise_error_callback: Callable[[], Awaitable[bool]] | None = None,
		register_should_stop_callback: Callable[[], Awaitable[bool]] | None = None,
		# Agent settings
		output_model_schema: type[AgentStructuredOutput] | None = None,
		extraction_schema: dict | None = None,
		use_vision: bool | Literal['auto'] = True,
		save_conversation_path: str | Path | None = None,
		save_conversation_path_encoding: str | None = 'utf-8',
		max_failures: int = 5,
		override_system_message: str | None = None,
		extend_system_message: str | None = None,
		generate_gif: bool | str = False,
		available_file_paths: list[str] | None = None,
		include_attributes: list[str] | None = None,
		max_actions_per_step: int = 5,
		use_thinking: bool = True,
		flash_mode: bool = False,
		demo_mode: bool | None = None,
		max_history_items: int | None = None,
		page_extraction_llm: BaseChatModel | None = None,
		fallback_llm: BaseChatModel | None = None,
		use_judge: bool = True,
		ground_truth: str | None = None,
		judge_llm: BaseChatModel | None = None,
		injected_agent_state: AgentState | None = None,
		source: str | None = None,
		file_system_path: str | None = None,
		task_id: str | None = None,
		calculate_cost: bool = False,
		pricing_url: str | None = None,
		display_files_in_done_text: bool = True,
		include_tool_call_examples: bool = False,
		vision_detail_level: Literal['auto', 'low', 'high'] = 'auto',
		llm_timeout: int | None = None,
		step_timeout: int = 180,
		directly_open_url: bool = True,
		include_recent_events: bool = False,
		sample_images: list[ContentPartTextParam | ContentPartImageParam] | None = None,
		final_response_after_failure: bool = True,
		enable_planning: bool = True,
		planning_replan_on_stall: int = 3,
		planning_exploration_limit: int = 5,
		loop_detection_window: int = 20,
		loop_detection_enabled: bool = True,
		llm_screenshot_size: tuple[int, int] | None = None,
		message_compaction: MessageCompactionSettings | bool | None = True,
		max_clickable_elements_length: int = 40000,
		_url_shortening_limit: int = 25,
		enable_signal_handler: bool = True,
	):
		self._model_settings = AgentModelSettings(self)
		self._construction = AgentConstruction(self)
		self._state_restoration = AgentStateRestoration(self)
		self._model_interaction = AgentModelInteraction(self)
		self._execution = AgentExecution(self)
		self._history_replay = AgentHistoryReplay(self)
		(
			llm,
			page_extraction_llm,
			judge_llm,
			flash_mode,
			enable_planning,
			llm_screenshot_size,
			llm_timeout,
			available_file_paths,
		) = self._model_settings.normalize_model_settings(
			llm,
			page_extraction_llm,
			judge_llm,
			flash_mode,
			enable_planning,
			llm_screenshot_size,
			llm_timeout,
			available_file_paths,
		)

		self.id = task_id or uuid7str()
		self.task_id: str = self.id
		self.session_id: str = uuid7str()

		self.browser_session = self._construction.create_browser_session(browser_profile, browser, demo_mode)

		self._demo_mode_enabled: bool = bool(self.browser_profile.demo_mode) if self.browser_session else False
		if self._demo_mode_enabled and getattr(self.browser_profile, 'headless', False):
			self.logger.warning(
				'Demo mode is enabled but the browser is headless=True; set headless=False to view the in-browser panel.'
			)

		# Initialize available file paths as direct attribute
		self.available_file_paths = available_file_paths

		output_model_schema = self._construction.configure_tools(
			tools, use_vision, display_files_in_done_text, llm, output_model_schema
		)
		self.output_model_schema = output_model_schema

		# Per-page extract uses a schema only when the caller explicitly asks for one.
		# It must NOT inherit output_model_schema: that describes the final task result
		# (e.g. {summary, step_results}), which is the wrong shape for a single-page
		# extraction and, on the browser-use gateway, routes extract into the agent
		# action protocol and breaks it.
		self.extraction_schema = extraction_schema

		# Core components - task enhancement now has access to output_model_schema from tools
		self.task = self._construction._enhance_task_with_schema(task, output_model_schema)
		self.llm = llm
		self.judge_llm = judge_llm

		# Fallback LLM configuration
		self._fallback_llm: BaseChatModel | None = fallback_llm
		self._using_fallback_llm: bool = False
		self._original_llm: BaseChatModel = llm  # Store original for reference
		self.directly_open_url = directly_open_url
		self.include_recent_events = include_recent_events
		self._url_shortening_limit = _url_shortening_limit

		self.sensitive_data = sensitive_data

		self.sample_images = sample_images

		if isinstance(message_compaction, bool):
			message_compaction = MessageCompactionSettings(enabled=message_compaction)

		self.settings = AgentSettings(
			use_vision=use_vision,
			vision_detail_level=vision_detail_level,
			save_conversation_path=save_conversation_path,
			save_conversation_path_encoding=save_conversation_path_encoding,
			max_failures=max_failures,
			override_system_message=override_system_message,
			extend_system_message=extend_system_message,
			generate_gif=generate_gif,
			include_attributes=include_attributes,
			max_actions_per_step=max_actions_per_step,
			use_thinking=use_thinking,
			flash_mode=flash_mode,
			max_history_items=max_history_items,
			page_extraction_llm=page_extraction_llm,
			calculate_cost=calculate_cost,
			include_tool_call_examples=include_tool_call_examples,
			llm_timeout=llm_timeout,
			step_timeout=step_timeout,
			final_response_after_failure=final_response_after_failure,
			use_judge=use_judge,
			ground_truth=ground_truth,
			enable_planning=enable_planning,
			planning_replan_on_stall=planning_replan_on_stall,
			planning_exploration_limit=planning_exploration_limit,
			loop_detection_window=loop_detection_window,
			loop_detection_enabled=loop_detection_enabled,
			message_compaction=message_compaction,
			max_clickable_elements_length=max_clickable_elements_length,
		)

		# Token cost service
		self.token_cost_service = TokenCost(include_cost=calculate_cost, pricing_url=pricing_url)
		self.token_cost_service.register_llm(llm)
		self.token_cost_service.register_llm(page_extraction_llm)
		self.token_cost_service.register_llm(judge_llm)
		if self.settings.message_compaction and self.settings.message_compaction.compaction_llm:
			self.token_cost_service.register_llm(self.settings.message_compaction.compaction_llm)

		# Store signal handler setting (not part of AgentSettings as it's runtime behavior)
		self.enable_signal_handler = enable_signal_handler

		# Initialize state
		self.state = injected_agent_state or AgentState()

		# Configure loop detector window size from settings
		self.state.loop_detector.window_size = self.settings.loop_detection_window

		# Initialize history
		self.history = AgentHistoryList(history=[], usage=None)

		# Initialize agent directory
		import time

		timestamp = int(time.time())
		base_tmp = Path(tempfile.gettempdir())
		self.agent_directory = base_tmp / f'browser_use_agent_{self.id}_{timestamp}'

		# Initialize file system and screenshot service
		self._state_restoration._set_file_system(file_system_path)
		self._state_restoration._set_screenshot_service()

		# Action setup
		self._construction._setup_action_models()
		self._state_restoration._set_browser_use_version_and_source(source)

		initial_url = None

		# only load url if no initial actions are provided
		if self.directly_open_url and not self.state.follow_up_task and not initial_actions:
			initial_url = self._construction._extract_start_url(self.task)
			if initial_url:
				self.logger.info(f'🔗 Found URL in task: {initial_url}, adding as initial action...')
				initial_actions = [{'navigate': {'url': initial_url, 'new_tab': False}}]

		self.initial_url = initial_url

		self.initial_actions = self._construction._convert_initial_actions(initial_actions) if initial_actions else None
		# Verify we can connect to the model
		self._model_settings._verify_and_setup_llm()

		# TODO: move this logic to the LLMs
		# Handle users trying to use use_vision=True with DeepSeek models
		if 'deepseek' in self.llm.model.lower():
			self.logger.warning('⚠️ DeepSeek models do not support use_vision=True yet. Setting use_vision=False for now...')
			self.settings.use_vision = False

		# Handle users trying to use use_vision=True with XAI models that don't support it
		# grok-3 variants and grok-code don't support vision; grok-2 and grok-4 do
		model_lower = self.llm.model.lower()
		if 'grok-3' in model_lower or 'grok-code' in model_lower:
			self.logger.warning('⚠️ This XAI model does not support use_vision=True yet. Setting use_vision=False for now...')
			self.settings.use_vision = False

		logger.debug(
			f'{" +vision" if self.settings.use_vision else ""}'
			f' extraction_model={self.settings.page_extraction_llm.model if self.settings.page_extraction_llm else "Unknown"}'
			f'{" +file_system" if self.file_system else ""}'
		)

		# Store llm_screenshot_size in browser_session so tools can access it
		self.browser_session.llm_screenshot_size = llm_screenshot_size

		self._message_manager = self._construction.create_message_manager(
			override_system_message,
			extend_system_message,
			llm_screenshot_size,
		)

		warn_sensitive_data_domain_constraints(
			self.logger,
			self.sensitive_data,
			self.browser_profile.allowed_domains,
		)

		# Callbacks
		self.register_new_step_callback = register_new_step_callback
		self.register_done_callback = register_done_callback
		self.register_should_stop_callback = register_should_stop_callback
		self.register_external_agent_status_raise_error_callback = register_external_agent_status_raise_error_callback

		# Event bus with WAL persistence
		# Default to ~/.config/browseruse/events/{agent_session_id}.jsonl
		# wal_path = get_environment_config().config_dir / 'events' / f'{self.session_id}.jsonl'
		self.eventbus = EventBus(name=f'Agent_{str(self.id)[-4:]}')

		if self.settings.save_conversation_path:
			self.settings.save_conversation_path = Path(self.settings.save_conversation_path).expanduser().resolve()
			self.logger.info(f'💬 Saving conversation to {log_pretty_path(self.settings.save_conversation_path)}')

		# Initialize download tracking
		assert self.browser_session is not None, 'BrowserSession is not set up'
		self.has_downloads_path = self.browser_session.browser_profile.downloads_path is not None
		if self.has_downloads_path:
			self._last_known_downloads: list[str] = []
			self.logger.debug('📁 Initialized download tracking for agent')

		# Event-based pause control (kept out of AgentState for serialization)
		self._external_pause_event = asyncio.Event()
		self._external_pause_event.set()

	@property
	def logger(self) -> logging.Logger:
		"""Get instance-specific logger with task ID in the name"""
		# logger may be called in __init__ so we don't assume self.* attributes have been initialized
		_task_id = task_id[-4:] if (task_id := getattr(self, 'task_id', None)) else '----'
		_browser_session_id = browser_session.id[-4:] if (browser_session := getattr(self, 'browser_session', None)) else '----'
		_current_target_id = (
			browser_session.agent_focus_target_id[-2:]
			if (browser_session := getattr(self, 'browser_session', None)) and browser_session.agent_focus_target_id
			else '--'
		)
		return logging.getLogger(f'browser_use.Agent🅰 {_task_id} ⇢ 🅑 {_browser_session_id} 🅣 {_current_target_id}')

	@property
	def browser_profile(self) -> BrowserProfile:
		assert self.browser_session is not None, 'BrowserSession is not set up'
		return self.browser_session.browser_profile

	@property
	def is_using_fallback_llm(self) -> bool:
		"""Check if the agent is currently using the fallback LLM."""
		return self._using_fallback_llm

	@property
	def current_llm_model(self) -> str:
		"""Get the model name of the currently active LLM."""
		return self.llm.model if hasattr(self.llm, 'model') else 'unknown'

	def add_new_task(self, new_task: str) -> None:
		"""Add a new task to the agent, keeping the same task_id as tasks are continuous"""
		# Simply delegate to message manager - no need for new task_id or events
		# The task continues with new instructions, it doesn't end and start a new one
		self.task = new_task
		self._message_manager.add_new_task(new_task)
		# Mark as follow-up task and recreate eventbus (gets shut down after each run)
		self.state.follow_up_task = True
		# Reset control flags so agent can continue
		self.state.stopped = False
		self.state.paused = False
		agent_id_suffix = str(self.id)[-4:].replace('-', '_')
		if agent_id_suffix and agent_id_suffix[0].isdigit():
			agent_id_suffix = 'a' + agent_id_suffix
		self.eventbus = EventBus(name=f'Agent_{agent_id_suffix}')

	async def _log_agent_run(self) -> None:
		"""Log the agent run"""
		# Blue color for task
		self.logger.info(f'\033[34m🎯 Task: {self.task}\033[0m')

		self.logger.debug(f'🤖 Browser-Use Library Version {self.version} ({self.source})')

		# Check for latest version and log upgrade message if needed
		if get_environment_config().BROWSER_USE_VERSION_CHECK:
			latest_version = await check_latest_browser_use_version()
			if latest_version and latest_version != self.version:
				self.logger.info(
					f'📦 Newer version available: {latest_version} (current: {self.version}). Upgrade with: uv add browser-use=={latest_version}'
				)

	def _log_first_step_startup(self) -> None:
		"""Log startup message only on the first step"""
		if len(self.history.history) == 0:
			self.logger.info(
				f'Starting a browser-use agent with version {self.version}, with provider={self.llm.provider} and model={self.llm.model}'
			)

	def _log_final_outcome_messages(self) -> None:
		"""Log helpful messages to user based on agent run outcome"""
		# Check if agent failed
		is_successful = self.history.is_successful()

		if is_successful is False or is_successful is None:
			# Get final result to check for specific failure reasons
			final_result = self.history.final_result()
			final_result_str = str(final_result).lower() if final_result else ''

			# Check for captcha/cloudflare related failures
			captcha_keywords = ['captcha', 'cloudflare', 'recaptcha', 'challenge', 'bot detection', 'access denied']
			has_captcha_issue = any(keyword in final_result_str for keyword in captcha_keywords)

			if has_captcha_issue:
				self.logger.warning(
					'Agent was blocked by a captcha. Cloud browsers include stealth fingerprinting and proxy rotation to avoid this.\n'
					'         Try: Browser(use_cloud=True)  |  Get an API key: https://cloud.browser-use.com?utm_source=oss&utm_medium=captcha_nudge'
				)

			# General failure message
			self.logger.info('')
			self.logger.info('Did the Agent not work as expected? Let us fix this!')
			self.logger.info('   Open a short issue on GitHub: https://github.com/browser-use/browser-use/issues')

	@time_execution_async('--run')
	async def run(
		self,
		max_steps: int = 500,
		on_step_start: AgentHookFunc | None = None,
		on_step_end: AgentHookFunc | None = None,
	) -> AgentHistoryList[AgentStructuredOutput]:
		"""Execute the task with maximum number of steps"""

		loop = asyncio.get_event_loop()
		agent_run_error: str | None = None  # Initialize error tracking variable
		should_delay_close = False

		# Set up the  signal handler with callbacks specific to this agent
		signal_handler = SignalHandler(
			loop=loop,
			pause_callback=self.pause,
			resume_callback=self.resume,
			custom_exit_callback=None,
			exit_on_second_int=True,
			disabled=not self.enable_signal_handler,
		)
		signal_handler.register()

		try:
			await self._log_agent_run()

			self.logger.debug(
				f'🔧 Agent setup: Agent Session ID {self.session_id[-4:]}, Task ID {self.task_id[-4:]}, Browser Session ID {self.browser_session.id[-4:] if self.browser_session else "None"} {"(connecting via CDP)" if (self.browser_session and self.browser_session.cdp_url) else "(launching local browser)"}'
			)

			# Initialize timing for session and task
			self._session_start_time = time.time()
			self._task_start_time = self._session_start_time  # Initialize task start time

			# Mark the session initialized on its first run.
			if not self.state.session_initialized:
				self.state.session_initialized = True

			# Log startup message on first step (only if we haven't already done steps)
			self._log_first_step_startup()
			# Start browser session and attach watchdogs
			await self.browser_session.start()
			if self._demo_mode_enabled:
				await self._execution._demo_mode_log(f'Started task: {self.task}', 'info', {'tag': 'task'})
				await self._execution._demo_mode_log(
					'Demo mode active - follow the side panel for live thoughts and actions.',
					'info',
					{'tag': 'status'},
				)

			# Normally there was no try catch here but the callback can raise an InterruptedError.
			# Wrap with step_timeout so initial actions (usually a single URL navigate) can't
			# hang indefinitely on a silent CDP WebSocket — without this the agent would take
			# zero steps and return with an empty history while any outer watchdog waits.
			try:
				await asyncio.wait_for(
					self._execution._execute_initial_actions(),
					timeout=self.settings.step_timeout,
				)
			except InterruptedError:
				pass
			except TimeoutError:
				initial_timeout_msg = (
					f'Initial actions timed out after {self.settings.step_timeout}s '
					f'(browser may be unresponsive). Proceeding to main execution loop.'
				)
				self.logger.error(f'⏰ {initial_timeout_msg}')
				self.state.last_result = [ActionResult(error=initial_timeout_msg)]
				self.state.consecutive_failures += 1
			except Exception as e:
				raise e

			self.logger.debug(
				f'🔄 Starting main execution loop with max {max_steps} steps (currently at step {self.state.n_steps})...'
			)
			while self.state.n_steps <= max_steps:
				current_step = self.state.n_steps - 1  # Convert to 0-indexed for step_info

				# Use the consolidated pause state management
				if self.state.paused:
					self.logger.debug(f'⏸️ Step {self.state.n_steps}: Agent paused, waiting to resume...')
					await self._external_pause_event.wait()
					signal_handler.reset()

				# Check if we should stop due to too many failures, if final_response_after_failure is True, we try one last time
				if (self.state.consecutive_failures) >= self.settings.max_failures + int(
					self.settings.final_response_after_failure
				):
					self.logger.error(f'❌ Stopping due to {self.settings.max_failures} consecutive failures')
					agent_run_error = f'Stopped due to {self.settings.max_failures} consecutive failures'
					break

				# Check control flags before each step
				if self.state.stopped:
					self.logger.info('🛑 Agent stopped')
					agent_run_error = 'Agent stopped programmatically'
					break

				step_info = AgentStepInfo(step_number=current_step, max_steps=max_steps)
				is_done = await self._execution._execute_step(current_step, max_steps, step_info, on_step_start, on_step_end)

				if is_done:
					# Agent has marked the task as done
					if self._demo_mode_enabled and self.history.history:
						final_result_text = self.history.final_result() or 'Task completed'
						await self._execution._demo_mode_log(f'Final Result: {final_result_text}', 'success', {'tag': 'task'})

					should_delay_close = True
					break
			else:
				agent_run_error = 'Failed to complete task in maximum steps'

				self.history.add_item(
					AgentHistory(
						model_output=None,
						result=[ActionResult(error=agent_run_error)],
						state=BrowserStateHistory(
							url='',
							title='',
							tabs=[],
							interacted_element=[],
							screenshot_path=None,
						),
						metadata=None,
					)
				)

				self.logger.info(f'❌ {agent_run_error}')

			self.history.usage = await self.token_cost_service.get_usage_summary()

			# set the model output schema and call it on the fly
			if self.history._output_model_schema is None and self.output_model_schema is not None:
				self.history._output_model_schema = self.output_model_schema

			return self.history

		except KeyboardInterrupt:
			# Already handled by our signal handler, but catch any direct KeyboardInterrupt as well
			self.logger.debug('Got KeyboardInterrupt during execution, returning current history')
			agent_run_error = 'KeyboardInterrupt'

			self.history.usage = await self.token_cost_service.get_usage_summary()

			return self.history

		except Exception as e:
			self.logger.error(f'Agent run failed with exception: {e}', exc_info=True)
			agent_run_error = str(e)
			raise e

		finally:
			if should_delay_close and self._demo_mode_enabled and agent_run_error is None:
				await asyncio.sleep(30)
			if agent_run_error:
				await self._execution._demo_mode_log(f'Agent stopped: {agent_run_error}', 'error', {'tag': 'run'})
			# Log token usage summary
			await self.token_cost_service.log_usage_summary()

			# Unregister signal handlers before cleanup
			signal_handler.unregister()

			# Generate GIF if needed before stopping event bus
			if self.settings.generate_gif:
				output_path: str = 'agent_history.gif'
				if isinstance(self.settings.generate_gif, str):
					output_path = self.settings.generate_gif

				# Lazy import gif module to avoid heavy startup cost
				from browser_use.agent.gif import create_history_gif

				create_history_gif(task=self.task, history=self.history, output_path=output_path)

			# Log final messages to user based on outcome
			self._log_final_outcome_messages()

			# Stop the event bus gracefully, waiting for all events to be processed
			# Configurable via TIMEOUT_AgentEventBusStop env var (default: 3.0s)
			await self.eventbus.stop(clear=True, timeout=_get_timeout('TIMEOUT_AgentEventBusStop', 3.0))

			await self.close()

	async def rerun_history(
		self,
		history: AgentHistoryList,
		max_retries: int = 3,
		skip_failures: bool = False,
		delay_between_actions: float = 2.0,
		max_step_interval: float = 45.0,
		summary_llm: BaseChatModel | None = None,
		ai_step_llm: BaseChatModel | None = None,
		wait_for_elements: bool = False,
	) -> list[ActionResult]:
		"""Replay a prior history using the history-replay component."""
		return await self._history_replay.rerun_history(
			history,
			max_retries,
			skip_failures,
			delay_between_actions,
			max_step_interval,
			summary_llm,
			ai_step_llm,
			wait_for_elements,
		)

	async def load_and_rerun(
		self,
		history_file: str | Path | None = None,
		variables: dict[str, str] | None = None,
		**kwargs,
	) -> list[ActionResult]:
		"""Load and replay history, optionally substituting detected variables."""
		return await self._history_replay.load_and_rerun(history_file, variables, **kwargs)

	def save_history(self, file_path: str | Path | None = None) -> None:
		"""Save the history to a file with sensitive data filtering"""
		if not file_path:
			file_path = 'AgentHistory.json'
		self.history.save_to_file(file_path, sensitive_data=self.sensitive_data)

	def pause(self) -> None:
		"""Pause the agent before the next step"""
		print('\n\n⏸️ Paused the agent and left the browser open.\n\tPress [Enter] to resume or [Ctrl+C] again to quit.')
		self.state.paused = True
		self._external_pause_event.clear()

	def resume(self) -> None:
		"""Resume the agent"""
		# TODO: Locally the browser got closed
		print('----------------------------------------------------------------------')
		print('▶️  Resuming agent execution where it left off...\n')
		self.state.paused = False
		self._external_pause_event.set()

	def stop(self) -> None:
		"""Stop the agent"""
		self.logger.info('⏹️ Agent stopping')
		self.state.stopped = True

		# Signal pause event to unblock any waiting code so it can check the stopped state
		self._external_pause_event.set()

		# Task stopped

	@property
	def message_manager(self) -> MessageManager:
		return self._message_manager

	async def close(self):
		"""Close all resources"""
		try:
			# Only close browser if keep_alive is False (or not set)
			if self.browser_session is not None:
				if not self.browser_session.browser_profile.keep_alive:
					# Kill the browser session - this dispatches BrowserStopEvent,
					# stops the EventBus with clear=True, and recreates a fresh EventBus
					await self.browser_session.kill()
				else:
					# keep_alive=True sessions shouldn't keep the event loop alive after agent.run()
					await self.browser_session.event_bus.stop(
						clear=False,
						timeout=_get_timeout('TIMEOUT_BrowserSessionEventBusStopOnAgentClose', 1.0),
					)
					try:
						self.browser_session.event_bus.event_queue = None
						self.browser_session.event_bus._on_idle = None
					except Exception:
						pass

			# Force garbage collection
			gc.collect()

			# Debug: Log remaining threads and asyncio tasks
			import threading

			threads = threading.enumerate()
			self.logger.debug(f'🧵 Remaining threads ({len(threads)}): {[t.name for t in threads]}')

			# Get all asyncio tasks
			tasks = asyncio.all_tasks(asyncio.get_event_loop())
			# Filter out the current task (this close() coroutine)
			other_tasks = [t for t in tasks if t != asyncio.current_task()]
			if other_tasks:
				self.logger.debug(f'⚡ Remaining asyncio tasks ({len(other_tasks)}):')
				for task in other_tasks[:10]:  # Limit to first 10 to avoid spam
					self.logger.debug(f'  - {task.get_name()}: {task}')

		except Exception as e:
			self.logger.error(f'Error during cleanup: {e}')

	def run_sync(
		self,
		max_steps: int = 500,
		on_step_start: AgentHookFunc | None = None,
		on_step_end: AgentHookFunc | None = None,
	) -> AgentHistoryList[AgentStructuredOutput]:
		"""Synchronous wrapper around the async run method for easier usage without asyncio."""
		import asyncio

		return asyncio.run(self.run(max_steps=max_steps, on_step_start=on_step_start, on_step_end=on_step_end))

	def detect_variables(self) -> dict[str, DetectedVariable]:
		"""Detect reusable variables in agent history."""
		return self._history_replay.detect_variables()
