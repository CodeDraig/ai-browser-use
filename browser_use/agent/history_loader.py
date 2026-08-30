from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from browser_use.agent.history import AgentHistoryList
from browser_use.agent.results import ActionResult
from browser_use.agent.variables import DetectedVariable

if TYPE_CHECKING:
	from browser_use.agent.history_replay import AgentHistoryReplay
	from browser_use.agent.service import Agent


class HistoryLoader:
	"""Load replay history and apply explicit variable substitutions."""

	def __init__(self, agent: Agent, replay: AgentHistoryReplay) -> None:
		self.agent = agent
		self.replay = replay

	async def load_and_rerun(
		self,
		history_file: str | Path | None = None,
		variables: dict[str, str] | None = None,
		**kwargs,
	) -> list[ActionResult]:
		"""
		Load history from file and rerun it, optionally substituting variables.

		Args:
			history_file: Path to the history file
			variables: Optional dict mapping variable names to new values (e.g. {'email': 'new@example.com'})
			**kwargs: Additional arguments passed to rerun_history:
				- max_retries: Maximum retries per action (default: 3)
				- skip_failures: Continue on failure (default: True)
				- delay_between_actions: Delay when no saved interval (default: 2.0s)
				- max_step_interval: Cap on saved step_interval (default: 45.0s)
				- summary_llm: Custom LLM for final summary
				- ai_step_llm: Custom LLM for extract re-evaluation
		"""
		if not history_file:
			history_file = 'AgentHistory.json'
		history = AgentHistoryList.load_from_file(history_file, self.agent.AgentOutput)

		# Substitute variables if provided
		if variables:
			history = self._substitute_variables_in_history(history, variables)

		return await self.replay.rerun_history(history, **kwargs)

	def detect_variables(self) -> dict[str, DetectedVariable]:
		"""Detect reusable variables in agent history"""
		from browser_use.agent.variable_detector import detect_variables_in_history

		return detect_variables_in_history(self.agent.history)

	def _substitute_variables_in_history(self, history: AgentHistoryList, variables: dict[str, str]) -> AgentHistoryList:
		"""Substitute variables in history with new values for rerunning with different data"""
		from browser_use.agent.variable_detector import detect_variables_in_history

		# Detect variables in the history
		detected_vars = detect_variables_in_history(history)

		# Build a mapping of original values to new values
		value_replacements: dict[str, str] = {}
		for var_name, new_value in variables.items():
			if var_name in detected_vars:
				old_value = detected_vars[var_name].original_value
				value_replacements[old_value] = new_value
			else:
				self.agent.logger.warning(f'Variable "{var_name}" not found in history, skipping substitution')

		if not value_replacements:
			self.agent.logger.info('No variables to substitute')
			return history

		# Create a deep copy of history to avoid modifying the original
		import copy

		modified_history = copy.deepcopy(history)

		# Substitute values in all actions
		substitution_count = 0
		for history_item in modified_history.history:
			if not history_item.model_output or not history_item.model_output.action:
				continue

			for action in history_item.model_output.action:
				# Handle both Pydantic models and dicts
				if hasattr(action, 'model_dump'):
					action_dict = action.model_dump()
				elif isinstance(action, dict):
					action_dict = action
				else:
					action_dict = vars(action) if hasattr(action, '__dict__') else {}

				# Substitute in all string fields
				substitution_count += self._substitute_in_dict(action_dict, value_replacements)

				# Update the action with modified values
				if hasattr(action, 'model_dump'):
					# For Pydantic RootModel, we need to recreate from the modified dict
					if hasattr(action, 'root'):
						# This is a RootModel - recreate it from the modified dict
						new_action = type(action).model_validate(action_dict)
						# Replace the root field in-place using object.__setattr__ to bypass Pydantic's immutability
						object.__setattr__(action, 'root', getattr(new_action, 'root'))
					else:
						# Regular Pydantic model - update fields in-place
						for key, val in action_dict.items():
							if hasattr(action, key):
								setattr(action, key, val)
				elif isinstance(action, dict):
					action.update(action_dict)

		self.agent.logger.info(
			f'Substituted {substitution_count} value(s) in {len(value_replacements)} variable type(s) in history'
		)
		return modified_history

	def _substitute_in_dict(self, data: dict, replacements: dict[str, str]) -> int:
		"""Recursively substitute values in a dictionary, returns count of substitutions made"""
		count = 0
		for key, value in data.items():
			if isinstance(value, str):
				# Replace if exact match
				if value in replacements:
					data[key] = replacements[value]
					count += 1
			elif isinstance(value, dict):
				# Recurse into nested dicts
				count += self._substitute_in_dict(value, replacements)
			elif isinstance(value, list):
				# Handle lists
				for i, item in enumerate(value):
					if isinstance(item, str) and item in replacements:
						value[i] = replacements[item]
						count += 1
					elif isinstance(item, dict):
						count += self._substitute_in_dict(item, replacements)
		return count
