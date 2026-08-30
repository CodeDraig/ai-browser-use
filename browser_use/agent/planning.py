from __future__ import annotations

from typing import TYPE_CHECKING

from browser_use.agent.results import AgentOutput, PlanItem
from browser_use.browser.views import BrowserStateSummary
from browser_use.llm.messages import UserMessage

if TYPE_CHECKING:
	from browser_use.agent.service import Agent


class AgentPlanningPolicy:
	"""Own plan advancement, exploration nudges, and loop detection policy."""

	def __init__(self, agent: Agent) -> None:
		self.agent = agent

	def update_plan_from_model_output(self, model_output: AgentOutput) -> None:
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

	def render_plan_description(self) -> str | None:
		"""Render the current plan as a text description for injection into agent context."""
		if not self.agent.settings.enable_planning or self.agent.state.plan is None:
			return None

		markers = {'done': '[x]', 'current': '[>]', 'pending': '[ ]', 'skipped': '[-]'}
		lines = []
		for i, step in enumerate(self.agent.state.plan):
			marker = markers.get(step.status, '[ ]')
			lines.append(f'{marker} {i}: {step.text}')
		return '\n'.join(lines)

	def inject_replan_nudge(self) -> None:
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

	def inject_exploration_nudge(self) -> None:
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

	def inject_loop_detection_nudge(self) -> None:
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

	def update_loop_detector_actions(self) -> None:
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

	def update_loop_detector_page_state(self, browser_state_summary: BrowserStateSummary) -> None:
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
