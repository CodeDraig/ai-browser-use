from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Generic

from pydantic import BaseModel, ConfigDict
from typing_extensions import TypeVar

from browser_use.agent.results import ActionResult, AgentOutput, StepMetadata
from browser_use.browser.views import BrowserStateHistory
from browser_use.dom.history import DOMInteractedElement
from browser_use.dom.tree import DOMSelectorMap
from browser_use.security import SensitiveData, collect_sensitive_data_values, redact_sensitive_string
from browser_use.tokens.views import UsageSummary

logger = logging.getLogger(__name__)


class AgentHistory(BaseModel):
	"""History item for agent actions"""

	model_output: AgentOutput | None
	result: list[ActionResult]
	state: BrowserStateHistory
	metadata: StepMetadata | None = None
	state_message: str | None = None

	model_config = ConfigDict(arbitrary_types_allowed=True, protected_namespaces=())

	@staticmethod
	def get_interacted_element(model_output: AgentOutput, selector_map: DOMSelectorMap) -> list[DOMInteractedElement | None]:
		elements = []
		for action in model_output.action:
			index = action.get_index()
			if index is not None and index in selector_map:
				el = selector_map[index]
				elements.append(DOMInteractedElement.load_from_enhanced_dom_tree(el))
			else:
				elements.append(None)
		return elements

	def _filter_sensitive_data_from_string(self, value: str, sensitive_data: SensitiveData | None) -> str:
		"""Filter out sensitive data from a string value"""
		if not sensitive_data:
			return value

		sensitive_values = collect_sensitive_data_values(sensitive_data)

		# If there are no valid sensitive data entries, just return the original value
		if not sensitive_values:
			return value

		return redact_sensitive_string(value, sensitive_values)

	def _filter_sensitive_data_from_dict(self, data: dict[str, Any], sensitive_data: SensitiveData | None) -> dict[str, Any]:
		"""Recursively filter sensitive data from a dictionary"""
		if not sensitive_data:
			return data

		filtered_data = {}
		for key, value in data.items():
			if isinstance(value, str):
				filtered_data[key] = self._filter_sensitive_data_from_string(value, sensitive_data)
			elif isinstance(value, dict):
				filtered_data[key] = self._filter_sensitive_data_from_dict(value, sensitive_data)
			elif isinstance(value, list):
				filtered_data[key] = [
					self._filter_sensitive_data_from_string(item, sensitive_data)
					if isinstance(item, str)
					else self._filter_sensitive_data_from_dict(item, sensitive_data)
					if isinstance(item, dict)
					else item
					for item in value
				]
			else:
				filtered_data[key] = value
		return filtered_data

	def model_dump(self, sensitive_data: SensitiveData | None = None, **kwargs) -> dict[str, Any]:
		"""Custom serialization handling circular references and filtering sensitive data"""

		# Handle action serialization
		model_output_dump = None
		if self.model_output:
			action_dump = [action.model_dump(exclude_none=True, mode='json') for action in self.model_output.action]

			# Filter sensitive data only from input action parameters if sensitive_data is provided
			if sensitive_data:
				action_dump = [
					self._filter_sensitive_data_from_dict(action, sensitive_data) if 'input' in action else action
					for action in action_dump
				]

			model_output_dump = {
				'evaluation_previous_goal': self.model_output.evaluation_previous_goal,
				'memory': self.model_output.memory,
				'next_goal': self.model_output.next_goal,
				'action': action_dump,  # This preserves the actual action data
			}
			# Only include thinking if it's present
			if self.model_output.thinking is not None:
				model_output_dump['thinking'] = self.model_output.thinking
			if self.model_output.current_plan_item is not None:
				model_output_dump['current_plan_item'] = self.model_output.current_plan_item
			if self.model_output.plan_update is not None:
				model_output_dump['plan_update'] = self.model_output.plan_update

		# Handle result serialization - don't filter ActionResult data
		# as it should contain meaningful information for the agent
		result_dump = [r.model_dump(exclude_none=True, mode='json') for r in self.result]

		return {
			'model_output': model_output_dump,
			'result': result_dump,
			'state': self.state.to_dict(),
			'metadata': self.metadata.model_dump() if self.metadata else None,
			'state_message': self.state_message,
		}


AgentStructuredOutput = TypeVar('AgentStructuredOutput', bound=BaseModel)


class AgentHistoryList(BaseModel, Generic[AgentStructuredOutput]):
	"""List of AgentHistory messages, i.e. the history of the agent's actions and thoughts."""

	history: list[AgentHistory]
	usage: UsageSummary | None = None

	_output_model_schema: type[AgentStructuredOutput] | None = None

	def total_duration_seconds(self) -> float:
		"""Get total duration of all steps in seconds"""
		total = 0.0
		for h in self.history:
			if h.metadata:
				total += h.metadata.duration_seconds
		return total

	def __len__(self) -> int:
		"""Return the number of history items"""
		return len(self.history)

	def __str__(self) -> str:
		"""Representation of the AgentHistoryList object"""
		return f'AgentHistoryList(all_results={self.action_results()}, all_model_outputs={self.model_actions()})'

	def add_item(self, history_item: AgentHistory) -> None:
		"""Add a history item to the list"""
		self.history.append(history_item)

	def __repr__(self) -> str:
		"""Representation of the AgentHistoryList object"""
		return self.__str__()

	def save_to_file(self, filepath: str | Path, sensitive_data: SensitiveData | None = None) -> None:
		"""Save history to JSON file with proper serialization and optional sensitive data filtering"""
		try:
			Path(filepath).parent.mkdir(parents=True, exist_ok=True)
			data = self.model_dump(sensitive_data=sensitive_data)
			with open(filepath, 'w', encoding='utf-8') as f:
				json.dump(data, f, indent=2, ensure_ascii=False)
		except Exception as e:
			raise e

	# def save_as_playwright_script(
	# 	self,
	# 	output_path: str | Path,
	# 	sensitive_data_keys: list[str] | None = None,
	# 	browser_config: BrowserConfig | None = None,
	# 	context_config: BrowserContextConfig | None = None,
	# ) -> None:
	# 	"""
	# 	Generates a Playwright script based on the agent's history and saves it to a file.
	# 	Args:
	# 		output_path: The path where the generated Python script will be saved.
	# 		sensitive_data_keys: A list of keys used as placeholders for sensitive data
	# 							 (e.g., ['username_placeholder', 'password_placeholder']).
	# 							 These will be loaded from environment variables in the
	# 							 generated script.
	# 		browser_config: Configuration of the original Browser instance.
	# 		context_config: Configuration of the original BrowserContext instance.
	# 	"""
	# 	from browser_use.agent.playwright_script_generator import PlaywrightScriptGenerator

	# 	try:
	# 		serialized_history = self.model_dump()['history']
	# 		generator = PlaywrightScriptGenerator(serialized_history, sensitive_data_keys, browser_config, context_config)

	# 		script_content = generator.generate_script_content()
	# 		path_obj = Path(output_path)
	# 		path_obj.parent.mkdir(parents=True, exist_ok=True)
	# 		with open(path_obj, 'w', encoding='utf-8') as f:
	# 			f.write(script_content)
	# 	except Exception as e:
	# 		raise e

	def model_dump(self, **kwargs) -> dict[str, Any]:
		"""Custom serialization that properly uses AgentHistory's model_dump"""
		return {
			'history': [h.model_dump(**kwargs) for h in self.history],
		}

	@classmethod
	def load_from_dict(cls, data: dict[str, Any], output_model: type[AgentOutput]) -> AgentHistoryList:
		"""Load a current-format history and validate custom action models.

		History files are persisted artifacts, so loading must not repair missing
		fields or silently reinterpret an older schema. Pydantic validation is the
		single authority for rejecting incomplete or historical data.
		"""
		if not isinstance(data, dict) or not isinstance(data.get('history'), list):
			return cls.model_validate(data)

		history_entries = data['history']
		validated_entries: list[dict[str, Any]] = []
		for raw_entry in history_entries:
			if not isinstance(raw_entry, dict):
				return cls.model_validate(data)
			entry = dict(raw_entry)
			raw_output = entry.get('model_output')
			if isinstance(raw_output, dict):
				entry['model_output'] = output_model.model_validate(raw_output)
			validated_entries.append(entry)

		return cls.model_validate({**data, 'history': validated_entries})

	@classmethod
	def load_from_file(cls, filepath: str | Path, output_model: type[AgentOutput]) -> AgentHistoryList:
		"""Load history from JSON file"""
		with open(filepath, encoding='utf-8') as f:
			data = json.load(f)
		return cls.load_from_dict(data, output_model)

	def last_action(self) -> None | dict:
		"""Last action in history"""
		if self.history and self.history[-1].model_output:
			actions = self.history[-1].model_output.action
			if actions:
				return actions[-1].model_dump(exclude_none=True, mode='json')
		return None

	def errors(self) -> list[str | None]:
		"""Get all errors from history, with None for steps without errors"""
		errors = []
		for h in self.history:
			step_errors = [r.error for r in h.result if r.error]

			# each step can have only one error
			errors.append(step_errors[0] if step_errors else None)
		return errors

	def final_result(self) -> None | str:
		"""Final result from history"""
		if self.history and len(self.history[-1].result) > 0:
			last_result = self.history[-1].result[-1]
			if last_result.extracted_content:
				return last_result.extracted_content
		return None

	def is_done(self) -> bool:
		"""Check if the agent is done"""
		if self.history and len(self.history[-1].result) > 0:
			last_result = self.history[-1].result[-1]
			return last_result.is_done is True
		return False

	def is_successful(self) -> bool | None:
		"""Check if the agent completed successfully - the agent decides in the last step if it was successful or not. None if not done yet."""
		if self.history and len(self.history[-1].result) > 0:
			last_result = self.history[-1].result[-1]
			if last_result.is_done is True:
				return last_result.success
		return None

	def has_errors(self) -> bool:
		"""Check if the agent has any non-None errors"""
		return any(error is not None for error in self.errors())

	def judgement(self) -> dict | None:
		"""Get the judgement result as a dictionary if it exists"""
		if self.history and len(self.history[-1].result) > 0:
			last_result = self.history[-1].result[-1]
			if last_result.judgement:
				return last_result.judgement.model_dump()
		return None

	def is_judged(self) -> bool:
		"""Check if the agent trace has been judged"""
		if self.history and len(self.history[-1].result) > 0:
			last_result = self.history[-1].result[-1]
			return last_result.judgement is not None
		return False

	def is_validated(self) -> bool | None:
		"""Check if the judge validated the agent execution (verdict is True). Returns None if not judged yet."""
		if self.history and len(self.history[-1].result) > 0:
			last_result = self.history[-1].result[-1]
			if last_result.judgement:
				return last_result.judgement.verdict
		return None

	def urls(self) -> list[str | None]:
		"""Get all unique URLs from history"""
		return [h.state.url if h.state.url is not None else None for h in self.history]

	def screenshot_paths(self, n_last: int | None = None, return_none_if_not_screenshot: bool = True) -> list[str | None]:
		"""Get all screenshot paths from history"""
		if n_last == 0:
			return []
		if n_last is None:
			if return_none_if_not_screenshot:
				return [h.state.screenshot_path if h.state.screenshot_path is not None else None for h in self.history]
			else:
				return [h.state.screenshot_path for h in self.history if h.state.screenshot_path is not None]
		else:
			if return_none_if_not_screenshot:
				return [h.state.screenshot_path if h.state.screenshot_path is not None else None for h in self.history[-n_last:]]
			else:
				return [h.state.screenshot_path for h in self.history[-n_last:] if h.state.screenshot_path is not None]

	def screenshots(self, n_last: int | None = None, return_none_if_not_screenshot: bool = True) -> list[str | None]:
		"""Get all screenshots from history as base64 strings"""
		if n_last == 0:
			return []

		history_items = self.history if n_last is None else self.history[-n_last:]
		screenshots = []

		for item in history_items:
			screenshot_b64 = item.state.get_screenshot()
			if screenshot_b64:
				screenshots.append(screenshot_b64)
			else:
				if return_none_if_not_screenshot:
					screenshots.append(None)
				# If return_none_if_not_screenshot is False, we skip None values

		return screenshots

	def action_names(self) -> list[str]:
		"""Get all action names from history"""
		action_names = []
		for action in self.model_actions():
			actions = list(action.keys())
			if actions:
				action_names.append(actions[0])
		return action_names

	def model_outputs(self) -> list[AgentOutput]:
		"""Get all model outputs from history"""
		return [h.model_output for h in self.history if h.model_output]

	# get all actions with params
	def model_actions(self) -> list[dict]:
		"""Get all actions from history"""
		outputs = []

		for h in self.history:
			if h.model_output:
				# Guard against None interacted_element before zipping
				interacted_elements = h.state.interacted_element or [None] * len(h.model_output.action)
				for action, interacted_element in zip(h.model_output.action, interacted_elements):
					output = action.model_dump(exclude_none=True, mode='json')
					output['interacted_element'] = interacted_element
					outputs.append(output)
		return outputs

	def action_history(self) -> list[list[dict]]:
		"""Get truncated action history with only essential fields"""
		step_outputs = []

		for h in self.history:
			step_actions = []
			if h.model_output:
				# Guard against None interacted_element before zipping
				interacted_elements = h.state.interacted_element or [None] * len(h.model_output.action)
				# Zip actions with interacted elements and results
				for action, interacted_element, result in zip(h.model_output.action, interacted_elements, h.result):
					action_output = action.model_dump(exclude_none=True, mode='json')
					action_output['interacted_element'] = interacted_element
					# Only keep long_term_memory from result
					action_output['result'] = result.long_term_memory if result and result.long_term_memory else None
					step_actions.append(action_output)
			step_outputs.append(step_actions)

		return step_outputs

	def action_results(self) -> list[ActionResult]:
		"""Get all results from history"""
		results = []
		for h in self.history:
			results.extend([r for r in h.result if r])
		return results

	def extracted_content(self) -> list[str]:
		"""Get all extracted content from history"""
		content = []
		for h in self.history:
			content.extend([r.extracted_content for r in h.result if r.extracted_content])
		return content

	def model_actions_filtered(self, include: list[str] | None = None) -> list[dict]:
		"""Get all model actions from history as JSON"""
		if include is None:
			include = []
		outputs = self.model_actions()
		result = []
		for o in outputs:
			for i in include:
				if i == list(o.keys())[0]:
					result.append(o)
		return result

	def number_of_steps(self) -> int:
		"""Get the number of steps in the history"""
		return len(self.history)

	def agent_steps(self) -> list[str]:
		"""Format agent history as readable step descriptions for judge evaluation."""
		steps = []

		# Iterate through history items (each is an AgentHistory)
		for i, h in enumerate(self.history):
			step_text = f'Step {i + 1}:\n'

			# Get actions from model_output
			if h.model_output and h.model_output.action:
				# Use model_dump with mode='json' to serialize enums properly
				actions_list = [action.model_dump(exclude_none=True, mode='json') for action in h.model_output.action]
				action_json = json.dumps(actions_list, indent=1)
				step_text += f'Actions: {action_json}\n'

			# Get results (already a list[ActionResult] in h.result)
			if h.result:
				for j, result in enumerate(h.result):
					if result.extracted_content:
						content = str(result.extracted_content)
						step_text += f'Result {j + 1}: {content}\n'

					if result.error:
						error = str(result.error)
						step_text += f'Error {j + 1}: {error}\n'

			steps.append(step_text)

		return steps

	@property
	def structured_output(self) -> AgentStructuredOutput | None:
		"""Get the structured output from the history

		Returns:
			The structured output if both final_result and _output_model_schema are available,
			otherwise None
		"""
		final_result = self.final_result()
		if final_result is not None and self._output_model_schema is not None:
			return self._output_model_schema.model_validate_json(final_result)

		return None

	def get_structured_output(self, output_model: type[AgentStructuredOutput]) -> AgentStructuredOutput | None:
		"""Get the structured output from history, parsing with the provided schema.

		Use this method when accessing structured output from sandbox execution,
		since the _output_model_schema private attribute is not preserved during serialization.

		Args:
			output_model: The Pydantic model class to parse the output with

		Returns:
			The parsed structured output, or None if no final result exists
		"""
		final_result = self.final_result()
		if final_result is not None:
			return output_model.model_validate_json(final_result)
		return None
