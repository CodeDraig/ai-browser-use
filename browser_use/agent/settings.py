"""Agent configuration model family."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from browser_use.dom.serialized_state import DEFAULT_INCLUDE_ATTRIBUTES
from browser_use.llm.base import BaseChatModel


class MessageCompactionSettings(BaseModel):
	"""Summarizes older history into a compact memory block to reduce prompt size."""

	enabled: bool = True
	compact_every_n_steps: int = 25
	trigger_char_count: int | None = None
	trigger_token_count: int | None = None
	chars_per_token: float = 4.0
	keep_last_items: int = 6
	summary_max_chars: int = 6000
	include_read_state: bool = False
	compaction_llm: BaseChatModel | None = None

	@model_validator(mode='after')
	def _resolve_trigger_threshold(self) -> 'MessageCompactionSettings':
		if self.trigger_char_count is not None and self.trigger_token_count is not None:
			raise ValueError('Set trigger_char_count or trigger_token_count, not both.')
		if self.trigger_token_count is not None:
			self.trigger_char_count = int(self.trigger_token_count * self.chars_per_token)
		elif self.trigger_char_count is None:
			self.trigger_char_count = 40000
		return self


class AgentSettings(BaseModel):
	"""Configuration options for the Agent."""

	use_vision: bool | Literal['auto'] = True
	vision_detail_level: Literal['auto', 'low', 'high'] = 'auto'
	save_conversation_path: str | Path | None = None
	save_conversation_path_encoding: str | None = 'utf-8'
	max_failures: int = 5
	generate_gif: bool | str = False
	override_system_message: str | None = None
	extend_system_message: str | None = None
	include_attributes: list[str] | None = DEFAULT_INCLUDE_ATTRIBUTES
	max_actions_per_step: int = 5
	use_thinking: bool = True
	flash_mode: bool = False
	use_judge: bool = True
	ground_truth: str | None = None
	max_history_items: int | None = None
	message_compaction: MessageCompactionSettings | None = None
	enable_planning: bool = True
	planning_replan_on_stall: int = 3
	planning_exploration_limit: int = 5
	page_extraction_llm: BaseChatModel | None = None
	calculate_cost: bool = False
	include_tool_call_examples: bool = False
	llm_timeout: int = 60
	step_timeout: int = 180
	final_response_after_failure: bool = True
	loop_detection_window: int = 20
	loop_detection_enabled: bool = True
	max_clickable_elements_length: int = 40000
