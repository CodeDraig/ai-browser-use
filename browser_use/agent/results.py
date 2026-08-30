"""Agent action, planning, and model-output result models."""

import traceback
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator

from browser_use.tools.registry.views import ActionModel


class JudgementResult(BaseModel):
	"""LLM judgement of an agent trace."""

	reasoning: str | None = Field(default=None, description='Explanation of the judgement')
	verdict: bool = Field(description='Whether the trace was successful or not')
	failure_reason: str | None = Field(default=None, description='Explanation of an unsuccessful trace')
	impossible_task: bool = Field(default=False, description='Whether the task was impossible to complete')
	reached_captcha: bool = Field(default=False, description='Whether the agent encountered captcha challenges')


class ActionResult(BaseModel):
	"""Result of executing an action."""

	is_done: bool | None = False
	success: bool | None = None
	judgement: JudgementResult | None = None
	error: str | None = None
	attachments: list[str] | None = None
	images: list[dict[str, Any]] | None = None
	long_term_memory: str | None = None
	extracted_content: str | None = None
	include_extracted_content_only_once: bool = False
	metadata: dict | None = None

	@model_validator(mode='after')
	def validate_success_requires_done(self) -> 'ActionResult':
		if self.success is True and self.is_done is not True:
			raise ValueError(
				'success=True can only be set when is_done=True. '
				'For regular actions that succeed, leave success as None. '
				'Use success=False only for actions that fail.'
			)
		return self


class RerunSummaryAction(BaseModel):
	"""AI-generated summary for rerun completion."""

	summary: str = Field(description='Summary of what happened during the rerun')
	success: bool = Field(description='Whether the rerun completed successfully based on visual inspection')
	completion_status: Literal['complete', 'partial', 'failed'] = Field(description='Status of rerun completion')


class StepMetadata(BaseModel):
	"""Timing metadata for a single step."""

	step_start_time: float
	step_end_time: float
	step_number: int
	step_interval: float | None

	@property
	def duration_seconds(self) -> float:
		return self.step_end_time - self.step_start_time


class PlanItem(BaseModel):
	text: str
	status: Literal['pending', 'current', 'done', 'skipped'] = 'pending'


class AgentOutput(BaseModel):
	model_config = ConfigDict(arbitrary_types_allowed=True, extra='forbid')
	thinking: str | None = None
	evaluation_previous_goal: str | None = Field(...)
	memory: str | None = Field(...)
	next_goal: str | None = Field(...)
	current_plan_item: int | None = None
	plan_update: list[str] | None = None
	action: list[ActionModel] = Field(..., json_schema_extra={'min_items': 1})

	@classmethod
	def model_json_schema(cls, **kwargs):
		schema = super().model_json_schema(**kwargs)
		schema['required'] = ['evaluation_previous_goal', 'memory', 'next_goal', 'action']
		return schema

	@staticmethod
	def type_with_custom_actions(custom_actions: type[ActionModel]) -> type['AgentOutput']:
		return create_model(
			'AgentOutput',
			__base__=AgentOutput,
			action=(
				list[custom_actions],
				Field(..., description='List of actions to execute', json_schema_extra={'min_items': 1}),
			),  # type: ignore
			__module__=AgentOutput.__module__,
		)

	@staticmethod
	def type_with_custom_actions_no_thinking(custom_actions: type[ActionModel]) -> type['AgentOutput']:
		class AgentOutputNoThinking(AgentOutput):
			@classmethod
			def model_json_schema(cls, **kwargs):
				schema = super().model_json_schema(**kwargs)
				del schema['properties']['thinking']
				schema['required'] = ['evaluation_previous_goal', 'memory', 'next_goal', 'action']
				return schema

		return create_model(
			'AgentOutput',
			__base__=AgentOutputNoThinking,
			action=(list[custom_actions], Field(..., json_schema_extra={'min_items': 1})),  # type: ignore
			__module__=AgentOutputNoThinking.__module__,
		)

	@staticmethod
	def type_with_custom_actions_flash_mode(custom_actions: type[ActionModel]) -> type['AgentOutput']:
		class AgentOutputFlashMode(AgentOutput):
			evaluation_previous_goal: str | None = None
			next_goal: str | None = None

			@classmethod
			def model_json_schema(cls, **kwargs):
				schema = super().model_json_schema(**kwargs)
				del schema['properties']['thinking']
				del schema['properties']['evaluation_previous_goal']
				del schema['properties']['next_goal']
				schema['properties'].pop('current_plan_item', None)
				schema['properties'].pop('plan_update', None)
				schema['required'] = ['memory', 'action']
				return schema

		return create_model(
			'AgentOutput',
			__base__=AgentOutputFlashMode,
			action=(list[custom_actions], Field(..., json_schema_extra={'min_items': 1})),  # type: ignore
			__module__=AgentOutputFlashMode.__module__,
		)


class AgentError:
	"""Container for agent error handling."""

	VALIDATION_ERROR = 'Invalid model output format. Please follow the correct schema.'
	RATE_LIMIT_ERROR = 'Rate limit reached. Waiting before retry.'
	NO_VALID_ACTION = 'No valid action found'

	@staticmethod
	def format_error(error: Exception, include_trace: bool = False) -> str:
		if isinstance(error, ValidationError):
			return f'{AgentError.VALIDATION_ERROR}\nDetails: {error}'
		from openai import RateLimitError

		if isinstance(error, RateLimitError):
			return AgentError.RATE_LIMIT_ERROR
		error_string = str(error)
		if 'LLM response missing required fields' in error_string or 'Expected format: AgentOutput' in error_string:
			main_error = error_string.split('\n')[0]
			message = f'{main_error}\n\nThe previous response had an invalid output structure. Please stick to the required output format. \n\n'
			if include_trace:
				message += f'\n\nFull stacktrace:\n{traceback.format_exc()}'
			return message
		if include_trace:
			return f'{error}\nStacktrace:\n{traceback.format_exc()}'
		return str(error)
