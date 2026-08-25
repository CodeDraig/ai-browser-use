from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import TYPE_CHECKING, Any

from browser_use.agent.views import (
	ActionResult,
	AgentError,
	AgentHistory,
	AgentOutput,
	AgentStepInfo,
	BrowserStateHistory,
	PlanItem,
	StepMetadata,
)
from browser_use.browser.views import BrowserStateSummary
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
			if self.agent.browser_session:
				try:
					captcha_wait = await self.agent.browser_session.wait_if_captcha_solving()
					if captcha_wait and captcha_wait.waited:
						# Reset step timing to exclude the captcha wait from step duration metrics
						self.step_start_time = time.time()
						duration_s = captcha_wait.duration_ms / 1000
						outcome = captcha_wait.result  # 'success' | 'failed' | 'timeout'
						msg = f'Waited {duration_s:.1f}s for {captcha_wait.vendor} CAPTCHA to be solved. Result: {outcome}.'
						self.agent.logger.info(f'🔒 {msg}')
						# Inject the outcome so the LLM sees what happened
						captcha_result = ActionResult(long_term_memory=msg)
						if self.agent.state.last_result:
							self.agent.state.last_result.append(captcha_result)
						else:
							self.agent.state.last_result = [captcha_result]
				except Exception as e:
					self.agent.logger.warning(f'Phase 0 captcha wait failed (non-fatal): {e}')

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
			await self._execute_actions()

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
		plan_description = self._render_plan_description()

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
		self._inject_replan_nudge()
		self._inject_exploration_nudge()
		self._update_loop_detector_page_state(browser_state_summary)
		self._inject_loop_detection_nudge()
		await self._force_done_after_last_step(step_info)
		await self._force_done_after_failure()
		return browser_state_summary

	async def _execute_actions(self) -> None:
		"""Execute the actions from model output"""
		if self.agent.state.last_model_output is None:
			raise ValueError('No model output to execute actions from')

		result = await self.multi_act(self.agent.state.last_model_output.action)
		self.agent.state.last_result = result

	async def _post_process(self) -> None:
		"""Handle post-action processing like download tracking and result logging"""
		assert self.agent.browser_session is not None, 'BrowserSession is not set up'

		# Check for new downloads after executing actions
		await self._check_and_update_downloads('after executing actions')

		# Update plan state from model output
		if self.agent.state.last_model_output is not None:
			self._update_plan_from_model_output(self.agent.state.last_model_output)

		# Record executed actions for loop detection
		self._update_loop_detector_actions()

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

	def _update_plan_from_model_output(self, model_output: AgentOutput) -> None:
		"""Update the plan state from model output fields (current_plan_item, plan_update)."""
		if not self.agent.settings.enable_planning:
			return

		# If model provided a new plan via plan_update, replace the current plan
		if model_output.plan_update is not None:
			self.agent.state.plan = [PlanItem(text=step_text) for step_text in model_output.plan_update]
			self.agent.state.current_plan_item_index = 0
			self.agent.state.plan_generation_step = self.agent.state.n_steps
			if self.agent.state.plan:
				self.agent.state.plan[0].status = 'current'
			self.agent.logger.info(
				f'📋 Plan {"updated" if self.agent.state.plan_generation_step else "created"} with {len(self.agent.state.plan)} steps'
			)
			return

		# If model provided a step index update, advance the plan
		if model_output.current_plan_item is not None and self.agent.state.plan is not None:
			new_idx = model_output.current_plan_item
			# Clamp to valid range
			new_idx = max(0, min(new_idx, len(self.agent.state.plan) - 1))
			old_idx = self.agent.state.current_plan_item_index

			# Mark steps between old and new as done
			for i in range(old_idx, new_idx):
				if i < len(self.agent.state.plan) and self.agent.state.plan[i].status in ('current', 'pending'):
					self.agent.state.plan[i].status = 'done'

			# Mark the new step as current
			if new_idx < len(self.agent.state.plan):
				self.agent.state.plan[new_idx].status = 'current'

			self.agent.state.current_plan_item_index = new_idx

	def _render_plan_description(self) -> str | None:
		"""Render the current plan as a text description for injection into agent context."""
		if not self.agent.settings.enable_planning or self.agent.state.plan is None:
			return None

		markers = {'done': '[x]', 'current': '[>]', 'pending': '[ ]', 'skipped': '[-]'}
		lines = []
		for i, step in enumerate(self.agent.state.plan):
			marker = markers.get(step.status, '[ ]')
			lines.append(f'{marker} {i}: {step.text}')
		return '\n'.join(lines)

	def _inject_replan_nudge(self) -> None:
		"""Inject a replan nudge when stall detection threshold is met."""
		if not self.agent.settings.enable_planning or self.agent.state.plan is None:
			return
		if self.agent.settings.planning_replan_on_stall <= 0:
			return
		if self.agent.state.consecutive_failures >= self.agent.settings.planning_replan_on_stall:
			msg = (
				'REPLAN SUGGESTED: You have failed '
				f'{self.agent.state.consecutive_failures} consecutive times. '
				'Your current plan may need revision. '
				'Output a new `plan_update` with revised steps to recover.'
			)
			self.agent.logger.info(f'📋 Replan nudge injected after {self.agent.state.consecutive_failures} consecutive failures')
			self.agent._message_manager._add_context_message(UserMessage(content=msg))

	def _inject_exploration_nudge(self) -> None:
		"""Nudge the agent to create a plan (or call done) after exploring without one."""
		if not self.agent.settings.enable_planning or self.agent.state.plan is not None:
			return
		if self.agent.settings.planning_exploration_limit <= 0:
			return
		if self.agent.state.n_steps >= self.agent.settings.planning_exploration_limit:
			msg = (
				'PLANNING NUDGE: You have taken '
				f'{self.agent.state.n_steps} steps without creating a plan. '
				'If the task is complex, output a `plan_update` with clear todo items now. '
				'If the task is already done or nearly done, call `done` instead.'
			)
			self.agent.logger.info(f'📋 Exploration nudge injected after {self.agent.state.n_steps} steps without a plan')
			self.agent._message_manager._add_context_message(UserMessage(content=msg))

	def _inject_loop_detection_nudge(self) -> None:
		"""Inject an escalating nudge when behavioral loops are detected."""
		if not self.agent.settings.loop_detection_enabled:
			return
		nudge = self.agent.state.loop_detector.get_nudge_message()
		if nudge:
			self.agent.logger.info(
				f'🔁 Loop detection nudge injected (repetition={self.agent.state.loop_detector.max_repetition_count}, '
				f'stagnation={self.agent.state.loop_detector.consecutive_stagnant_pages})'
			)
			self.agent._message_manager._add_context_message(UserMessage(content=nudge))

	def _update_loop_detector_actions(self) -> None:
		"""Record the actions from the latest step into the loop detector."""
		if not self.agent.settings.loop_detection_enabled:
			return
		if self.agent.state.last_model_output is None:
			return
		# Actions to exclude: wait always hashes identically (instant false positive),
		# done is terminal, go_back is navigation recovery
		_LOOP_EXEMPT_ACTIONS = {'wait', 'done', 'go_back'}
		for action in self.agent.state.last_model_output.action:
			action_data = action.model_dump(exclude_unset=True)
			action_name = next(iter(action_data.keys()), 'unknown')
			if action_name in _LOOP_EXEMPT_ACTIONS:
				continue
			params = action_data.get(action_name, {})
			if not isinstance(params, dict):
				params = {}
			self.agent.state.loop_detector.record_action(action_name, params)

	def _update_loop_detector_page_state(self, browser_state_summary: BrowserStateSummary) -> None:
		"""Record the current page state for stagnation detection."""
		if not self.agent.settings.loop_detection_enabled:
			return
		url = browser_state_summary.url or ''
		element_count = len(browser_state_summary.dom_state.selector_map) if browser_state_summary.dom_state else 0
		# Use the DOM text representation for fingerprinting
		dom_text = ''
		if browser_state_summary.dom_state:
			try:
				dom_text = browser_state_summary.dom_state.llm_representation()
			except Exception:
				dom_text = ''
		self.agent.state.loop_detector.record_page_state(url, dom_text, element_count)

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
				await self.agent._model_interaction._judge_and_log()

			if self.agent.register_done_callback:
				if inspect.iscoroutinefunction(self.agent.register_done_callback):
					await self.agent.register_done_callback(self.agent.history)
				else:
					self.agent.register_done_callback(self.agent.history)

			return True

		return False

	@time_execution_async('--multi_act')
	async def multi_act(self, actions: list[ActionModel]) -> list[ActionResult]:
		"""Execute multiple actions with page-change guards.

		Two layers of protection prevent executing actions against stale DOM:
		  1. Static flag: actions tagged with terminates_sequence=True (navigate, search, go_back, switch)
		     automatically abort remaining queued actions.
		  2. Runtime detection: after every action, the current URL and focused target are compared
		     to pre-action values. Any change aborts the remaining queue.
		"""
		results: list[ActionResult] = []
		total_actions = len(actions)

		assert self.agent.browser_session is not None, 'BrowserSession is not set up'
		try:
			if (
				self.agent.browser_session.dom_state.cached_browser_state_summary is not None
				and self.agent.browser_session.dom_state.cached_browser_state_summary.dom_state is not None
			):
				cached_selector_map = dict(
					self.agent.browser_session.dom_state.cached_browser_state_summary.dom_state.selector_map
				)
			else:
				cached_selector_map = {}
		except Exception as e:
			self.agent.logger.error(f'Error getting cached selector map: {e}')
			cached_selector_map = {}

		for i, action in enumerate(actions):
			# Get action name from the action model BEFORE try block to ensure it's always available in except
			action_data = action.model_dump(exclude_unset=True)
			action_name = next(iter(action_data.keys())) if action_data else 'unknown'

			if i > 0:
				# ONLY ALLOW TO CALL `done` IF IT IS A SINGLE ACTION
				if action_data.get('done') is not None:
					msg = f'Done action is allowed only as a single action - stopped after action {i} / {total_actions}.'
					self.agent.logger.debug(msg)
					break

			# wait between actions (only after first action)
			if i > 0:
				self.agent.logger.debug(f'Waiting {self.agent.browser_profile.wait_between_actions} seconds between actions')
				await asyncio.sleep(self.agent.browser_profile.wait_between_actions)

			try:
				await self._check_stop_or_pause()

				# Log action before execution
				await self._log_action(action, action_name, i + 1, total_actions)

				# Capture pre-action state for runtime page-change detection
				pre_action_url = await self.agent.browser_session.get_current_page_url()
				pre_action_focus = self.agent.browser_session.agent_focus_target_id

				result = await self.agent.tools.act(
					action=action,
					browser_session=self.agent.browser_session,
					file_system=self.agent.file_system,
					page_extraction_llm=self.agent.settings.page_extraction_llm,
					sensitive_data=self.agent.sensitive_data,
					available_file_paths=self.agent.available_file_paths,
					extraction_schema=self.agent.extraction_schema,
				)

				if result.error:
					await self._demo_mode_log(
						f'Action "{action_name}" failed: {result.error}',
						'error',
						{'action': action_name, 'step': self.agent.state.n_steps},
					)
				elif result.is_done:
					completion_text = result.long_term_memory or result.extracted_content or 'Task marked as done.'
					level = 'success' if result.success is not False else 'warning'
					await self._demo_mode_log(
						completion_text,
						level,
						{'action': action_name, 'step': self.agent.state.n_steps},
					)

				results.append(result)

				if results[-1].is_done or results[-1].error or i == total_actions - 1:
					break

				# --- Page-change guards (only when more actions remain) ---

				# Layer 1: Static flag — action metadata declares it changes the page
				registered_action = self.agent.tools.registry.registry.actions.get(action_name)
				if registered_action and registered_action.terminates_sequence:
					self.agent.logger.info(
						f'Action "{action_name}" terminates sequence — skipping {total_actions - i - 1} remaining action(s)'
					)
					break

				# Layer 2: Runtime detection — URL or focus target changed
				post_action_url = await self.agent.browser_session.get_current_page_url()
				post_action_focus = self.agent.browser_session.agent_focus_target_id

				if post_action_url != pre_action_url or post_action_focus != pre_action_focus:
					self.agent.logger.info(
						f'Page changed after "{action_name}" — skipping {total_actions - i - 1} remaining action(s)'
					)
					break

			except Exception as e:
				# Re-raise InterruptedError so _check_stop_or_pause's stop/pause signal still propagates
				if isinstance(e, InterruptedError):
					raise
				# Re-raise browser/connection errors so _handle_step_error can handle reconnect/shutdown
				if self._is_connection_like_error(e):
					raise
				# Handle any exceptions during action execution
				self.agent.logger.error(f'❌ Executing action {i + 1} failed -> {type(e).__name__}: {e}')
				await self._demo_mode_log(
					f'Action "{action_name}" raised {type(e).__name__}: {e}',
					'error',
					{'action': action_name, 'step': self.agent.state.n_steps},
				)
				# Preserve partial results so the agent knows which actions succeeded before the failure
				results.append(ActionResult(error=f'{type(e).__name__}: {e}'))
				return results

		return results

	async def _log_action(self, action, action_name: str, action_num: int, total_actions: int) -> None:
		"""Log the action before execution with colored formatting"""
		# Color definitions
		blue = '\033[34m'  # Action name
		magenta = '\033[35m'  # Parameter names
		reset = '\033[0m'

		# Format action number and name
		if total_actions > 1:
			action_header = f'▶️  [{action_num}/{total_actions}] {blue}{action_name}{reset}:'
			plain_header = f'▶️  [{action_num}/{total_actions}] {action_name}:'
		else:
			action_header = f'▶️   {blue}{action_name}{reset}:'
			plain_header = f'▶️  {action_name}:'

		# Get action parameters
		action_data = action.model_dump(exclude_unset=True)
		params = action_data.get(action_name, {})

		# Build parameter parts with colored formatting
		param_parts = []
		plain_param_parts = []

		if params and isinstance(params, dict):
			for param_name, value in params.items():
				# Truncate long values for readability
				if isinstance(value, str) and len(value) > 150:
					display_value = value[:150] + '...'
				elif isinstance(value, list) and len(str(value)) > 200:
					display_value = str(value)[:200] + '...'
				else:
					display_value = value

				param_parts.append(f'{magenta}{param_name}{reset}: {display_value}')
				plain_param_parts.append(f'{param_name}: {display_value}')

		# Join all parts
		if param_parts:
			params_string = ', '.join(param_parts)
			self.agent.logger.info(f'  {action_header} {params_string}')
		else:
			self.agent.logger.info(f'  {action_header}')

		if self.agent._demo_mode_enabled:
			panel_message = plain_header
			if plain_param_parts:
				panel_message = f'{panel_message} {", ".join(plain_param_parts)}'
			await self._demo_mode_log(panel_message.strip(), 'action', {'action': action_name, 'step': self.agent.state.n_steps})

	async def log_completion(self) -> None:
		"""Log the completion of the task"""
		# self.agent._task_end_time = time.time()
		# self.agent._task_duration = self.agent._task_end_time - self.agent._task_start_time TODO: this is not working when using take_step
		if self.agent.history.is_successful():
			self.agent.logger.info('✅ Task completed successfully')
			await self._demo_mode_log('Task completed successfully', 'success', {'tag': 'task'})

	async def _execute_initial_actions(self) -> None:
		# Execute initial actions if provided
		if self.agent.initial_actions and not self.agent.state.follow_up_task:
			self.agent.logger.debug(f'⚡ Executing {len(self.agent.initial_actions)} initial actions...')
			result = await self.multi_act(self.agent.initial_actions)
			# update result 1 to mention that its was automatically loaded
			if result and self.agent.initial_url and result[0].long_term_memory:
				result[0].long_term_memory = f'Found initial url and automatically loaded it. {result[0].long_term_memory}'
			self.agent.state.last_result = result

			# Save initial actions to history as step 0 for rerun capability
			# Skip browser state capture for initial actions (usually just URL navigation)
			if self.agent.settings.flash_mode:
				model_output = self.agent.AgentOutput(
					evaluation_previous_goal=None,
					memory='Initial navigation',
					next_goal=None,
					action=self.agent.initial_actions,
				)
			else:
				model_output = self.agent.AgentOutput(
					evaluation_previous_goal='Start',
					memory=None,
					next_goal='Initial navigation',
					action=self.agent.initial_actions,
				)

			metadata = StepMetadata(step_number=0, step_start_time=time.time(), step_end_time=time.time(), step_interval=None)

			# Create minimal browser state history for initial actions
			state_history = BrowserStateHistory(
				url=self.agent.initial_url or '',
				title='Initial Actions',
				tabs=[],
				interacted_element=[None] * len(self.agent.initial_actions),  # No DOM elements needed
				screenshot_path=None,
			)

			history_item = AgentHistory(
				model_output=model_output,
				result=result,
				state=state_history,
				metadata=metadata,
			)

			self.agent.history.add_item(history_item)
			self.agent.logger.debug('📝 Saved initial actions to history as step 0')
			self.agent.logger.debug('Initial actions completed')
