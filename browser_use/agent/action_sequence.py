from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from browser_use.agent.history import AgentHistory
from browser_use.agent.results import ActionResult, StepMetadata
from browser_use.browser.views import BrowserStateHistory
from browser_use.tools.registry.views import ActionModel

if TYPE_CHECKING:
	from browser_use.agent.execution import AgentExecution
	from browser_use.agent.service import Agent


class ActionSequenceExecutor:
	"""Execute validated action queues while preserving ordering and abort rules."""

	def __init__(self, agent: Agent, coordinator: AgentExecution) -> None:
		self.agent = agent
		self.coordinator = coordinator

	async def execute_actions(self) -> None:
		"""Execute the actions from model output"""
		if self.agent.state.last_model_output is None:
			raise ValueError('No model output to execute actions from')

		result = await self.multi_act(self.agent.state.last_model_output.action)
		self.agent.state.last_result = result

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
				await self.coordinator._check_stop_or_pause()

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
					await self.coordinator._demo_mode_log(
						f'Action "{action_name}" failed: {result.error}',
						'error',
						{'action': action_name, 'step': self.agent.state.n_steps},
					)
				elif result.is_done:
					completion_text = result.long_term_memory or result.extracted_content or 'Task marked as done.'
					level = 'success' if result.success is not False else 'warning'
					await self.coordinator._demo_mode_log(
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
				if self.coordinator._is_connection_like_error(e):
					raise
				# Handle any exceptions during action execution
				self.agent.logger.error(f'❌ Executing action {i + 1} failed -> {type(e).__name__}: {e}')
				await self.coordinator._demo_mode_log(
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
			await self.coordinator._demo_mode_log(
				panel_message.strip(), 'action', {'action': action_name, 'step': self.agent.state.n_steps}
			)

	async def execute_initial_actions(self) -> None:
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
