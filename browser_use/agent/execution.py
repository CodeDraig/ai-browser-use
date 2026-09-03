from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import TYPE_CHECKING, Any

from browser_use.agent.action_sequence import ActionSequenceExecutor
from browser_use.agent.history import AgentHistory
from browser_use.agent.planning import AgentPlanningPolicy
from browser_use.agent.results import ActionResult, AgentError, AgentOutput, StepMetadata
from browser_use.agent.state import AgentStepInfo
from browser_use.browser.views import BrowserStateHistory, BrowserStateSummary
from browser_use.llm.messages import UserMessage
from browser_use.logging_utils import time_execution_async
from browser_use.tools.registry.views import ActionModel

if TYPE_CHECKING:
	from browser_use.agent.service import Agent, AgentHookFunc

logger = logging.getLogger(__name__)


class AgentExecution:
	"""Owns execution behavior for an Agent."""

	def __init__(self, agent: Agent) -> None:
		self.agent = agent
		self.planning = AgentPlanningPolicy(agent)
		self.action_sequence = ActionSequenceExecutor(agent, self)

	async def _check_and_update_downloads(self, context: str = '') -> None:
		"""Check for new downloads and update available file paths."""
		if not self.agent.has_downloads_path:
			return

		assert self.agent.browser_session is not None, 'BrowserSession is not set up'

		try:
			current_downloads = self.agent.browser_session.downloaded_files
			if current_downloads != self.agent._last_known_downloads:
				self._update_available_file_paths(current_downloads)
				self.agent._last_known_downloads = current_downloads
				if context:
					self.agent.logger.debug(f'📁 {context}: Updated available files')
		except Exception as e:
			error_context = f' {context}' if context else ''
			self.agent.logger.debug(f'📁 Failed to check for downloads{error_context}: {type(e).__name__}: {e}')

	def _update_available_file_paths(self, downloads: list[str]) -> None:
		"""Update available_file_paths with downloaded files."""
		if not self.agent.has_downloads_path:
			return

		current_files = set(self.agent.available_file_paths or [])
		new_files = set(downloads) - current_files

		if new_files:
			self.agent.available_file_paths = list(current_files | new_files)

			self.agent.logger.info(
				f'📁 Added {len(new_files)} downloaded files to available_file_paths (total: {len(self.agent.available_file_paths)} files)'
			)
			for file_path in new_files:
				self.agent.logger.info(f'📄 New file available: {file_path}')
		else:
			self.agent.logger.debug(f'📁 No new downloads detected (tracking {len(current_files)} files)')

	def save_file_system_state(self) -> None:
		"""Save current file system state to agent state"""
		if self.agent.file_system:
			self.agent.state.file_system_state = self.agent.file_system.get_state()
		else:
			self.agent.logger.error('💾 File system is not set up. Cannot save state.')
			raise ValueError('File system is not set up. Cannot save state.')

	async def _check_stop_or_pause(self) -> None:
		"""Check if the agent should stop or pause, and handle accordingly."""

		# Check new should_stop_callback - sets stopped state cleanly without raising
		if self.agent.register_should_stop_callback:
			if await self.agent.register_should_stop_callback():
				self.agent.logger.info('External callback requested stop')
				self.agent.state.stopped = True
				raise InterruptedError

		if self.agent.register_external_agent_status_raise_error_callback:
			if await self.agent.register_external_agent_status_raise_error_callback():
				raise InterruptedError

		if self.agent.state.stopped:
			raise InterruptedError

		if self.agent.state.paused:
			raise InterruptedError

	@time_execution_async('--step')
	async def step(self, step_info: AgentStepInfo | None = None) -> None:
		"""Execute one step of the task"""
		# Initialize timing first, before any exceptions can occur

		self.step_start_time = time.time()

		browser_state_summary = None

		try:
			# Phase 1: Prepare context and timing
			browser_state_summary = await self._prepare_context(step_info)

			# Clear previous step state after context preparation (which needs
			# them for the "previous action result" prompt) but before the LLM
			# call, so a timeout during _get_next_action or _execute_actions
			# won't leave stale data from the previous step.
			self.agent.state.last_model_output = None
			self.agent.state.last_result = None

			# Phase 2: Get model output and execute actions
			await self.agent._model_interaction._get_next_action(browser_state_summary)
			await self.action_sequence.execute_actions()

			# Phase 3: Post-processing
			await self._post_process()

		except Exception as e:
			# Handle ALL exceptions in one place
			await self._handle_step_error(e)

		finally:
			await self._finalize(browser_state_summary)

	async def _prepare_context(self, step_info: AgentStepInfo | None = None) -> BrowserStateSummary:
		"""Prepare the context for the step: browser state, action models, page actions"""
		# step_start_time is now set in step() method

		assert self.agent.browser_session is not None, 'BrowserSession is not set up'

		self.agent.logger.debug(f'🌐 Step {self.agent.state.n_steps}: Getting browser state...')
		# Always take screenshots for all steps
		self.agent.logger.debug('📸 Requesting browser state with include_screenshot=True')
		browser_state_summary = await self.agent.browser_session.get_browser_state_summary(
			include_screenshot=True,
			include_recent_events=self.agent.include_recent_events,
		)
		if browser_state_summary.screenshot:
			self.agent.logger.debug(f'📸 Got browser state WITH screenshot, length: {len(browser_state_summary.screenshot)}')
		else:
			self.agent.logger.debug('📸 Got browser state WITHOUT screenshot')

		# Check for new downloads after getting browser state (catches PDF auto-downloads and previous step downloads)
		await self._check_and_update_downloads(f'Step {self.agent.state.n_steps}: after getting browser state')

		self._log_step_context(browser_state_summary)
		await self._check_stop_or_pause()

		# Update action models with page-specific actions
		self.agent.logger.debug(f'📝 Step {self.agent.state.n_steps}: Updating action models...')
		await self.agent._model_interaction._update_action_models_for_page(browser_state_summary.url)

		# Get page-specific filtered actions
		page_filtered_actions = self.agent.tools.registry.get_prompt_description(browser_state_summary.url)

		# Page-specific actions will be included directly in the browser_state message
		self.agent.logger.debug(f'💬 Step {self.agent.state.n_steps}: Creating state messages for context...')

		# Render plan description for injection into agent context
		plan_description = self.planning.render_plan_description()

		self.agent._message_manager.prepare_step_state(
			browser_state_summary=browser_state_summary,
			model_output=self.agent.state.last_model_output,
			result=self.agent.state.last_result,
			step_info=step_info,
			sensitive_data=self.agent.sensitive_data,
		)

		await self.agent._model_interaction._maybe_compact_messages(step_info)

		self.agent._message_manager.create_state_messages(
			browser_state_summary=browser_state_summary,
			model_output=self.agent.state.last_model_output,
			result=self.agent.state.last_result,
			step_info=step_info,
			use_vision=self.agent.settings.use_vision,
			page_filtered_actions=page_filtered_actions if page_filtered_actions else None,
			sensitive_data=self.agent.sensitive_data,
			available_file_paths=self.agent.available_file_paths,  # Always pass current available_file_paths
			plan_description=plan_description,
			skip_state_update=True,
		)

		await self._inject_budget_warning(step_info)
		self.planning.inject_replan_nudge()
		self.planning.inject_exploration_nudge()
		self.planning.update_loop_detector_page_state(browser_state_summary)
		self.planning.inject_loop_detection_nudge()
		await self._force_done_after_last_step(step_info)
		await self._force_done_after_failure()
		return browser_state_summary

	async def _post_process(self) -> None:
		"""Handle post-action processing like download tracking and result logging"""
		assert self.agent.browser_session is not None, 'BrowserSession is not set up'

		# Check for new downloads after executing actions
		await self._check_and_update_downloads('after executing actions')

		# Update plan state from model output
		if self.agent.state.last_model_output is not None:
			self.planning.update_plan_from_model_output(self.agent.state.last_model_output)

		# Record executed actions for loop detection
		self.planning.update_loop_detector_actions()

		# check for action errors - only count single-action steps toward consecutive failures;
		# multi-action steps with errors are handled by loop detection and replan nudges instead
		if self.agent.state.last_result and len(self.agent.state.last_result) == 1 and self.agent.state.last_result[-1].error:
			self.agent.state.consecutive_failures += 1
			self.agent.logger.debug(
				f'🔄 Step {self.agent.state.n_steps}: Consecutive failures: {self.agent.state.consecutive_failures}'
			)
			return

		if self.agent.state.consecutive_failures > 0:
			self.agent.state.consecutive_failures = 0
			self.agent.logger.debug(
				f'🔄 Step {self.agent.state.n_steps}: Consecutive failures reset to: {self.agent.state.consecutive_failures}'
			)

		# Log completion results
		if self.agent.state.last_result and len(self.agent.state.last_result) > 0 and self.agent.state.last_result[-1].is_done:
			success = self.agent.state.last_result[-1].success
			if success:
				# Green color for success
				self.agent.logger.info(
					f'\n📄 \033[32m Final Result:\033[0m \n{self.agent.state.last_result[-1].extracted_content}\n\n'
				)
			else:
				# Red color for failure
				self.agent.logger.info(
					f'\n📄 \033[31m Final Result:\033[0m \n{self.agent.state.last_result[-1].extracted_content}\n\n'
				)
			if self.agent.state.last_result[-1].attachments:
				total_attachments = len(self.agent.state.last_result[-1].attachments)
				for i, file_path in enumerate(self.agent.state.last_result[-1].attachments):
					self.agent.logger.info(f'👉 Attachment {i + 1 if total_attachments > 1 else ""}: {file_path}')

	async def _handle_step_error(self, error: Exception) -> None:
		"""Handle all types of errors that can occur during a step"""

		# Handle InterruptedError specially
		if isinstance(error, InterruptedError):
			error_msg = 'The agent was interrupted mid-step' + (f' - {str(error)}' if str(error) else '')
			# NOTE: This is not an error, it's a normal part of the execution when the user interrupts the agent
			self.agent.logger.warning(f'{error_msg}')
			return

		# Handle browser closed/disconnected errors
		if self._is_connection_like_error(error):
			# If reconnection is in progress, wait for it instead of stopping
			if self.agent.browser_session.is_reconnecting:
				wait_timeout = self.agent.browser_session.RECONNECT_WAIT_TIMEOUT
				self.agent.logger.warning(
					f'🔄 Connection error during reconnection, waiting up to {wait_timeout}s for reconnect: {error}'
				)
				try:
					await asyncio.wait_for(self.agent.browser_session._reconnect_event.wait(), timeout=wait_timeout)
				except TimeoutError:
					pass

				# Check if reconnection succeeded
				if self.agent.browser_session.is_cdp_connected:
					self.agent.logger.info('🔄 Reconnection succeeded, retrying step...')
					self.agent.state.last_result = [ActionResult(error=f'Connection lost and recovered: {error}')]
					return

			# Not reconnecting or reconnection failed — check if truly terminal
			if self._is_browser_closed_error(error):
				self.agent.logger.warning(f'🛑 Browser closed or disconnected: {error}')
				self.agent.state.stopped = True
				self.agent._external_pause_event.set()
				return

		# Handle all other exceptions
		include_trace = self.agent.logger.isEnabledFor(logging.DEBUG)
		error_msg = AgentError.format_error(error, include_trace=include_trace)
		max_total_failures = self.agent.settings.max_failures + int(self.agent.settings.final_response_after_failure)
		prefix = f'❌ Result failed {self.agent.state.consecutive_failures + 1}/{max_total_failures} times: '
		self.agent.state.consecutive_failures += 1

		# Use WARNING for partial failures, ERROR only when max failures reached
		is_final_failure = self.agent.state.consecutive_failures >= max_total_failures
		log_level = logging.ERROR if is_final_failure else logging.WARNING

		if 'Could not parse response' in error_msg or 'tool_use_failed' in error_msg:
			# give model a hint how output should look like
			self.agent.logger.log(log_level, f'Model: {self.agent.llm.model} failed')
			self.agent.logger.log(log_level, f'{prefix}{error_msg}')
		else:
			self.agent.logger.log(log_level, f'{prefix}{error_msg}')

		await self._demo_mode_log(f'Step error: {error_msg}', 'error', {'step': self.agent.state.n_steps})
		self.agent.state.last_result = [ActionResult(error=error_msg)]
		return None

	def _is_connection_like_error(self, error: Exception) -> bool:
		"""Check if the error looks like a CDP/WebSocket connection failure.

		Unlike _is_browser_closed_error(), this does NOT check if the CDP client is None
		or if reconnection is in progress — it purely looks at the error signature.
		"""
		error_str = str(error).lower()
		return (
			isinstance(error, ConnectionError)
			or 'websocket connection closed' in error_str
			or 'connection closed' in error_str
			or 'browser has been closed' in error_str
			or 'browser closed' in error_str
			or 'no browser' in error_str
		)

	def _is_browser_closed_error(self, error: Exception) -> bool:
		"""Check if the browser has been closed or disconnected.

		Only returns True when the error itself is a CDP/WebSocket connection failure
		AND the CDP client is gone AND we're not actively reconnecting.
		Avoids false positives on unrelated errors (element not found, timeouts,
		parse errors) that happen to coincide with a transient None state during
		reconnects or resets.
		"""
		# During reconnection, don't treat connection errors as terminal
		if self.agent.browser_session.is_reconnecting:
			return False

		error_str = str(error).lower()
		is_connection_error = (
			isinstance(error, ConnectionError)
			or 'websocket connection closed' in error_str
			or 'connection closed' in error_str
			or 'browser has been closed' in error_str
			or 'browser closed' in error_str
			or 'no browser' in error_str
		)
		return is_connection_error and self.agent.browser_session._cdp_client_root is None

	async def _finalize(self, browser_state_summary: BrowserStateSummary | None) -> None:
		"""Finalize the step with history, logging, and events"""
		step_end_time = time.time()
		if not self.agent.state.last_result:
			return

		if browser_state_summary:
			step_interval = None
			if len(self.agent.history.history) > 0:
				last_history_item = self.agent.history.history[-1]

				if last_history_item.metadata:
					previous_end_time = last_history_item.metadata.step_end_time
					previous_start_time = last_history_item.metadata.step_start_time
					step_interval = max(0, previous_end_time - previous_start_time)
			metadata = StepMetadata(
				step_number=self.agent.state.n_steps,
				step_start_time=self.step_start_time,
				step_end_time=step_end_time,
				step_interval=step_interval,
			)

			# Use _make_history_item like main branch
			await self._make_history_item(
				self.agent.state.last_model_output,
				browser_state_summary,
				self.agent.state.last_result,
				metadata,
				state_message=self.agent._message_manager.last_state_message_text,
			)

		# Log step completion summary
		summary_message = self._log_step_completion_summary(self.step_start_time, self.agent.state.last_result)
		if summary_message:
			await self._demo_mode_log(summary_message, 'info', {'step': self.agent.state.n_steps})

		# Save file system state after step completion
		self.save_file_system_state()

		# Increment step counter after step is fully completed
		self.agent.state.n_steps += 1

	async def _inject_budget_warning(self, step_info: AgentStepInfo | None = None) -> None:
		"""Inject a prominent budget warning when the agent has used >= 75% of its step budget.

		This gives the LLM advance notice to wrap up, save partial results, and call done
		rather than exhausting all steps with nothing saved.
		"""
		if step_info is None or step_info.max_steps <= 0:
			return

		steps_used = step_info.step_number + 1  # Convert 0-indexed to 1-indexed
		budget_ratio = steps_used / step_info.max_steps

		if budget_ratio >= 0.75 and not step_info.is_last_step():
			steps_remaining = step_info.max_steps - steps_used
			pct = int(budget_ratio * 100)
			msg = (
				f'BUDGET WARNING: You have used {steps_used}/{step_info.max_steps} steps '
				f'({pct}%). {steps_remaining} steps remaining. '
				f'If the task cannot be completed in the remaining steps, prioritize: '
				f'(1) consolidate your results (save to files if the file system is in use), '
				f'(2) call done with what you have. '
				f'Partial results are far more valuable than exhausting all steps with nothing saved.'
			)
			self.agent.logger.info(f'Step budget warning: {steps_used}/{step_info.max_steps} ({pct}%)')
			self.agent._message_manager._add_context_message(UserMessage(content=msg))

	async def _force_done_after_last_step(self, step_info: AgentStepInfo | None = None) -> None:
		"""Handle special processing for the last step"""
		if step_info and step_info.is_last_step():
			# Add last step warning if needed
			msg = 'You reached max_steps - this is your last step. Your only tool available is the "done" tool. No other tool is available. All other tools which you see in history or examples are not available.'
			msg += '\nIf the task is not yet fully finished as requested by the user, set success in "done" to false! E.g. if not all steps are fully completed. Else success to true.'
			msg += '\nInclude everything you found out for the ultimate task in the done text.'
			self.agent.logger.debug('Last step finishing up')
			self.agent._message_manager._add_context_message(UserMessage(content=msg))
			self.agent.AgentOutput = self.agent.DoneAgentOutput

	async def _force_done_after_failure(self) -> None:
		"""Force done after failure"""
		# Create recovery message
		if (
			self.agent.state.consecutive_failures >= self.agent.settings.max_failures
			and self.agent.settings.final_response_after_failure
		):
			msg = f'You failed {self.agent.settings.max_failures} times. Therefore we terminate the agent.'
			msg += '\nYour only tool available is the "done" tool. No other tool is available. All other tools which you see in history or examples are not available.'
			msg += '\nIf the task is not yet fully finished as requested by the user, set success in "done" to false! E.g. if not all steps are fully completed. Else success to true.'
			msg += '\nInclude everything you found out for the ultimate task in the done text.'

			self.agent.logger.debug('Force done action, because we reached max_failures.')
			self.agent._message_manager._add_context_message(UserMessage(content=msg))
			self.agent.AgentOutput = self.agent.DoneAgentOutput

	async def _make_history_item(
		self,
		model_output: AgentOutput | None,
		browser_state_summary: BrowserStateSummary,
		result: list[ActionResult],
		metadata: StepMetadata | None = None,
		state_message: str | None = None,
	) -> None:
		"""Create and store history item"""

		if model_output:
			interacted_elements = AgentHistory.get_interacted_element(model_output, browser_state_summary.dom_state.selector_map)
		else:
			interacted_elements = [None]

		# Store screenshot and get path
		screenshot_path = None
		if browser_state_summary.screenshot:
			self.agent.logger.debug(
				f'📸 Storing screenshot for step {self.agent.state.n_steps}, screenshot length: {len(browser_state_summary.screenshot)}'
			)
			screenshot_path = await self.agent.screenshot_service.store_screenshot(
				browser_state_summary.screenshot, self.agent.state.n_steps
			)
			self.agent.logger.debug(f'📸 Screenshot stored at: {screenshot_path}')
		else:
			self.agent.logger.debug(f'📸 No screenshot in browser_state_summary for step {self.agent.state.n_steps}')

		state_history = BrowserStateHistory(
			url=browser_state_summary.url,
			title=browser_state_summary.title,
			tabs=browser_state_summary.tabs,
			interacted_element=interacted_elements,
			screenshot_path=screenshot_path,
		)

		history_item = AgentHistory(
			model_output=model_output,
			result=result,
			state=state_history,
			metadata=metadata,
			state_message=state_message,
		)

		self.agent.history.add_item(history_item)

	def _log_step_context(self, browser_state_summary: BrowserStateSummary) -> None:
		"""Log step context information"""
		url = browser_state_summary.url if browser_state_summary else ''
		url_short = url[:50] + '...' if len(url) > 50 else url
		interactive_count = len(browser_state_summary.dom_state.selector_map) if browser_state_summary else 0
		self.agent.logger.info('\n')
		self.agent.logger.info(f'📍 Step {self.agent.state.n_steps}:')
		self.agent.logger.debug(f'Evaluating page with {interactive_count} interactive elements on: {url_short}')

	def _prepare_demo_message(self, message: str, limit: int = 600) -> str:
		# Previously truncated long entries; keep full text for better context in demo panel
		return message.strip()

	async def _demo_mode_log(self, message: str, level: str = 'info', metadata: dict[str, Any] | None = None) -> None:
		if not self.agent._demo_mode_enabled or not message or self.agent.browser_session is None:
			return
		try:
			await self.agent.browser_session.send_demo_mode_log(
				message=self._prepare_demo_message(message),
				level=level,
				metadata=metadata or {},
			)
		except Exception as exc:
			self.agent.logger.debug(f'[DemoMode] Failed to send overlay log: {exc}')

	def _log_step_completion_summary(self, step_start_time: float, result: list[ActionResult]) -> str | None:
		"""Log step completion summary with action count, timing, and success/failure stats"""
		if not result:
			return None

		step_duration = time.time() - step_start_time
		action_count = len(result)

		# Count success and failures
		success_count = sum(1 for r in result if not r.error)
		failure_count = action_count - success_count

		# Format success/failure indicators
		success_indicator = f'✅ {success_count}' if success_count > 0 else ''
		failure_indicator = f'❌ {failure_count}' if failure_count > 0 else ''
		status_parts = [part for part in [success_indicator, failure_indicator] if part]
		status_str = ' | '.join(status_parts) if status_parts else '✅ 0'

		message = (
			f'📍 Step {self.agent.state.n_steps}: Ran {action_count} action{"" if action_count == 1 else "s"} '
			f'in {step_duration:.2f}s: {status_str}'
		)
		self.agent.logger.debug(message)
		return message

	async def _execute_step(
		self,
		step: int,
		max_steps: int,
		step_info: AgentStepInfo,
		on_step_start: AgentHookFunc | None = None,
		on_step_end: AgentHookFunc | None = None,
	) -> bool:
		"""
		Execute a single step with timeout.

		Returns:
			bool: True if task is done, False otherwise
		"""
		if on_step_start is not None:
			await on_step_start(self.agent)

		await self._demo_mode_log(
			f'Starting step {step + 1}/{max_steps}',
			'info',
			{'step': step + 1, 'total_steps': max_steps},
		)

		self.agent.logger.debug(f'🚶 Starting step {step + 1}/{max_steps}...')

		try:
			await asyncio.wait_for(
				self.step(step_info),
				timeout=self.agent.settings.step_timeout,
			)
			self.agent.logger.debug(f'✅ Completed step {step + 1}/{max_steps}')
		except TimeoutError:
			# Handle step timeout gracefully
			error_msg = f'Step {step + 1} timed out after {self.agent.settings.step_timeout} seconds'
			self.agent.logger.error(f'⏰ {error_msg}')
			await self._demo_mode_log(error_msg, 'error', {'step': step + 1})
			self.agent.state.consecutive_failures += 1
			self.agent.state.last_result = [ActionResult(error=error_msg)]
			# Ensure step counter advances on timeout — _finalize() may have
			# been skipped or returned early due to the cancellation.
			if self.agent.state.n_steps == step + 1:
				self.agent.state.n_steps += 1

		if on_step_end is not None:
			await on_step_end(self.agent)

		if self.agent.history.is_done():
			await self.log_completion()

			# Run full judge before done callback if enabled
			if self.agent.settings.use_judge:
				await self.agent._model_interaction.judge.judge_and_log()

			if self.agent.register_done_callback:
				if inspect.iscoroutinefunction(self.agent.register_done_callback):
					await self.agent.register_done_callback(self.agent.history)
				else:
					self.agent.register_done_callback(self.agent.history)

			return True

		return False

	@time_execution_async('--multi_act')
	async def multi_act(self, actions: list[ActionModel]) -> list[ActionResult]:
		"""Execute an action batch with the existing timing boundary."""
		return await self.action_sequence.multi_act(actions)

	async def log_completion(self) -> None:
		"""Log the completion of the task"""
		# self.agent._task_end_time = time.time()
		# self.agent._task_duration = self.agent._task_end_time - self.agent._task_start_time TODO: this is not working when using take_step
		if self.agent.history.is_successful():
			self.agent.logger.info('✅ Task completed successfully')
			await self._demo_mode_log('Task completed successfully', 'success', {'tag': 'task'})
