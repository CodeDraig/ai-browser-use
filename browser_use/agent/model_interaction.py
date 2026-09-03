from __future__ import annotations

import asyncio
import inspect
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from browser_use.agent.judge import AgentJudge
from browser_use.agent.message_manager.utils import save_conversation
from browser_use.agent.results import AgentOutput
from browser_use.agent.state import AgentStepInfo
from browser_use.agent.url_shortening import AgentUrlShortener
from browser_use.browser.views import BrowserStateSummary
from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelOutputTruncatedError, ModelProviderError, ModelRateLimitError
from browser_use.llm.messages import BaseMessage, UserMessage
from browser_use.logging_utils import time_execution_async

if TYPE_CHECKING:
	from browser_use.agent.service import Agent

logger = logging.getLogger(__name__)


class AgentModelInteraction:
	"""Owns model interaction behavior for an Agent."""

	def __init__(self, agent: Agent, url_shortening_limit: int) -> None:
		self.agent = agent
		self.judge = AgentJudge(agent)
		self.url_shortener = AgentUrlShortener(url_shortening_limit)

	def _log_response(self, response: AgentOutput) -> None:
		"""Log the model response beside the interaction that produced it."""
		if response.thinking:
			self.agent.logger.debug(f'💡 Thinking:\n{response.thinking}')

		evaluation = response.evaluation_previous_goal or ''
		if evaluation:
			if 'success' in evaluation.lower():
				self.agent.logger.info(f'  \033[32m👍 Eval: {evaluation}\033[0m')
			elif 'failure' in evaluation.lower():
				self.agent.logger.info(f'  \033[31m⚠️ Eval: {evaluation}\033[0m')
			else:
				self.agent.logger.info(f'  ❔ Eval: {evaluation}')

		if response.memory:
			self.agent.logger.info(f'  🧠 Memory: {response.memory}')
		if response.next_goal:
			self.agent.logger.info(f'  \033[34m🎯 Next goal: {response.next_goal}\033[0m')

	async def _maybe_compact_messages(self, step_info: AgentStepInfo | None = None) -> None:
		"""Optionally compact message history to keep prompts small."""
		settings = self.agent.settings.message_compaction
		if not settings or not settings.enabled:
			return

		compaction_llm = settings.compaction_llm or self.agent.settings.page_extraction_llm or self.agent.llm
		await self.agent._message_manager.maybe_compact_messages(
			llm=compaction_llm,
			settings=settings,
			step_info=step_info,
		)

	async def _get_next_action(self, browser_state_summary: BrowserStateSummary) -> None:
		"""Execute LLM interaction with retry logic and handle callbacks"""
		input_messages = self.agent._message_manager.get_messages()
		self.agent.logger.debug(
			f'🤖 Step {self.agent.state.n_steps}: Calling LLM with {len(input_messages)} messages (model: {self.agent.llm.model})...'
		)

		try:
			model_output = await asyncio.wait_for(
				self._get_model_output_with_retry(input_messages), timeout=self.agent.settings.llm_timeout
			)
		except TimeoutError:
			raise TimeoutError(
				f'LLM call timed out after {self.agent.settings.llm_timeout} seconds. Keep your thinking and output short.'
			)

		self.agent.state.last_model_output = model_output

		# Check again for paused/stopped state after getting model output
		await self.agent._execution._check_stop_or_pause()

		# Handle callbacks and conversation saving
		await self._handle_post_llm_processing(browser_state_summary, input_messages)

		# check again if Ctrl+C was pressed before we commit the output to history
		await self.agent._execution._check_stop_or_pause()

	async def _get_model_output_with_retry(self, input_messages: list[BaseMessage]) -> AgentOutput:
		"""Get model output with retry logic for empty actions"""
		model_output = await self.get_model_output(input_messages)
		self.agent.logger.debug(
			f'✅ Step {self.agent.state.n_steps}: Got LLM response with {len(model_output.action) if model_output.action else 0} actions'
		)

		if (
			not model_output.action
			or not isinstance(model_output.action, list)
			or all(action.model_dump() == {} for action in model_output.action)
		):
			self.agent.logger.warning('Model returned empty action. Retrying...')

			clarification_message = UserMessage(
				content='You forgot to return an action. Please respond with a valid JSON action according to the expected schema with your assessment and next actions.'
			)

			retry_messages = input_messages + [clarification_message]
			model_output = await self.get_model_output(retry_messages)

			if not model_output.action or all(action.model_dump() == {} for action in model_output.action):
				self.agent.logger.warning('Model still returned empty after retry. Inserting safe noop action.')
				action_instance = self.agent.ActionModel()
				setattr(
					action_instance,
					'done',
					{
						'success': False,
						'text': 'No next action returned by LLM!',
					},
				)
				model_output.action = [action_instance]

		return model_output

	async def _handle_post_llm_processing(
		self,
		browser_state_summary: BrowserStateSummary,
		input_messages: list[BaseMessage],
	) -> None:
		"""Handle callbacks and conversation saving after LLM interaction"""
		if self.agent.register_new_step_callback and self.agent.state.last_model_output:
			if inspect.iscoroutinefunction(self.agent.register_new_step_callback):
				await self.agent.register_new_step_callback(
					browser_state_summary,
					self.agent.state.last_model_output,
					self.agent.state.n_steps,
				)
			else:
				self.agent.register_new_step_callback(
					browser_state_summary,
					self.agent.state.last_model_output,
					self.agent.state.n_steps,
				)

		if self.agent.settings.save_conversation_path and self.agent.state.last_model_output:
			# Treat save_conversation_path as a directory (consistent with other recording paths)
			conversation_dir = Path(self.agent.settings.save_conversation_path)
			conversation_filename = f'conversation_{self.agent.id}_{self.agent.state.n_steps}.txt'
			target = conversation_dir / conversation_filename
			await save_conversation(
				input_messages,
				self.agent.state.last_model_output,
				target,
				self.agent.settings.save_conversation_path_encoding,
			)

	def _remove_think_tags(self, text: str) -> str:
		THINK_TAGS = re.compile(r'<think>.*?</think>', re.DOTALL)
		STRAY_CLOSE_TAG = re.compile(r'.*?</think>', re.DOTALL)
		# Step 1: Remove well-formed <think>...</think>
		text = re.sub(THINK_TAGS, '', text)
		# Step 2: If there's an unmatched closing tag </think>,
		#         remove everything up to and including that.
		text = re.sub(STRAY_CLOSE_TAG, '', text)
		return text.strip()

	@time_execution_async('--get_next_action')
	async def get_model_output(self, input_messages: list[BaseMessage]) -> AgentOutput:
		"""Get next action from LLM based on current state"""

		urls_replaced = self.url_shortener.shorten_messages(input_messages)

		# Build kwargs for ainvoke
		# Model-specific adapters may generate action descriptions from the output schema.
		kwargs: dict = {'output_format': self.agent.AgentOutput, 'session_id': self.agent.session_id}

		try:
			response = await self.agent.llm.ainvoke(input_messages, **kwargs)
			parsed: AgentOutput = response.completion  # type: ignore[assignment]

			# Replace any shortened URLs in the LLM response back to original URLs
			if urls_replaced:
				self.url_shortener.restore_model_urls(parsed, urls_replaced)

			# cut the number of actions to max_actions_per_step if needed
			if len(parsed.action) > self.agent.settings.max_actions_per_step:
				parsed.action = parsed.action[: self.agent.settings.max_actions_per_step]

			if not (hasattr(self.agent.state, 'paused') and (self.agent.state.paused or self.agent.state.stopped)):
				self._log_response(parsed)
				await self._broadcast_model_state(parsed)

			self._log_next_action_summary(parsed)
			return parsed
		except ValidationError:
			# Just re-raise - Pydantic's validation errors are already descriptive
			raise
		except (ModelRateLimitError, ModelProviderError) as e:
			# Check if we can switch to a fallback LLM
			if not self._try_switch_to_fallback_llm(e):
				# No fallback available, re-raise the original error
				raise
			# Retry with the fallback LLM
			return await self.get_model_output(input_messages)

	def _try_switch_to_fallback_llm(self, error: ModelRateLimitError | ModelProviderError) -> bool:
		"""
		Attempt to switch to a fallback LLM after a rate limit or provider error.

		Returns True if successfully switched to a fallback, False if no fallback available.
		Once switched, the agent will use the fallback LLM for the rest of the run.
		"""
		# Already using fallback - can't switch again
		if self.agent._using_fallback_llm:
			self.agent.logger.warning(
				f'⚠️ Fallback LLM also failed ({type(error).__name__}: {error.message}), no more fallbacks available'
			)
			return False

		# Check if error is retryable (rate limit, auth errors, or server errors)
		# 401: API key invalid/expired - fallback to different provider
		# 402: Insufficient credits/payment required - fallback to different provider
		# 429: Rate limit exceeded
		# 500, 502, 503, 504: Server errors
		# ModelOutputTruncatedError: not retryable on the same model, but a fallback may have a higher cap
		retryable_status_codes = {401, 402, 429, 500, 502, 503, 504}
		is_retryable = isinstance(error, (ModelRateLimitError, ModelOutputTruncatedError)) or (
			hasattr(error, 'status_code') and error.status_code in retryable_status_codes
		)

		if not is_retryable:
			return False

		# Check if we have a fallback LLM configured
		if self.agent._fallback_llm is None:
			self.agent.logger.warning(f'⚠️ LLM error ({type(error).__name__}: {error.message}) but no fallback_llm configured')
			return False

		self._log_fallback_switch(error, self.agent._fallback_llm)

		# Switch to the fallback LLM
		self.agent.llm = self.agent._fallback_llm
		self.agent._using_fallback_llm = True

		# Register the fallback LLM for token cost tracking
		self.agent.token_cost_service.register_llm(self.agent._fallback_llm)

		return True

	def _log_fallback_switch(self, error: ModelRateLimitError | ModelProviderError, fallback: BaseChatModel) -> None:
		"""Log when switching to a fallback LLM."""
		original_model = self.agent._original_llm.model if hasattr(self.agent._original_llm, 'model') else 'unknown'
		fallback_model = fallback.model if hasattr(fallback, 'model') else 'unknown'
		error_type = type(error).__name__
		status_code = getattr(error, 'status_code', 'N/A')

		self.agent.logger.warning(
			f'⚠️ Primary LLM ({original_model}) failed with {error_type} (status={status_code}), '
			f'switching to fallback LLM ({fallback_model})'
		)

	def _log_next_action_summary(self, parsed: AgentOutput) -> None:
		"""Log a comprehensive summary of the next action(s)"""
		if not (self.agent.logger.isEnabledFor(logging.DEBUG) and parsed.action):
			return

		# Collect action details
		action_details = []
		for i, action in enumerate(parsed.action):
			action_data = action.model_dump(exclude_unset=True)
			action_name = next(iter(action_data.keys())) if action_data else 'unknown'
			action_params = action_data.get(action_name, {}) if action_data else {}

			# Format key parameters concisely
			param_summary = []
			if isinstance(action_params, dict):
				for key, value in action_params.items():
					if key == 'index':
						param_summary.append(f'#{value}')
					elif key == 'text' and isinstance(value, str):
						text_preview = value[:30] + '...' if len(value) > 30 else value
						param_summary.append(f'text="{text_preview}"')
					elif key == 'url':
						param_summary.append(f'url="{value}"')
					elif key == 'success':
						param_summary.append(f'success={value}')
					elif isinstance(value, (str, int, bool)):
						val_str = str(value)[:30] + '...' if len(str(value)) > 30 else str(value)
						param_summary.append(f'{key}={val_str}')

			param_str = f'({", ".join(param_summary)})' if param_summary else ''
			action_details.append(f'{action_name}{param_str}')

	async def _broadcast_model_state(self, parsed: AgentOutput) -> None:
		if not self.agent._demo_mode_enabled:
			return

		step_meta = {'step': self.agent.state.n_steps}

		if parsed.thinking:
			await self.agent._execution._demo_mode_log(parsed.thinking, 'thought', step_meta)

		if parsed.evaluation_previous_goal:
			eval_text = parsed.evaluation_previous_goal
			level = 'success' if 'success' in eval_text.lower() else 'warning' if 'failure' in eval_text.lower() else 'info'
			await self.agent._execution._demo_mode_log(eval_text, level, step_meta)

		if parsed.memory:
			await self.agent._execution._demo_mode_log(f'Memory: {parsed.memory}', 'info', step_meta)

		if parsed.next_goal:
			await self.agent._execution._demo_mode_log(f'Next goal: {parsed.next_goal}', 'info', step_meta)

	async def _update_action_models_for_page(self, page_url: str) -> None:
		"""Update action models with page-specific actions"""
		# Create new action model with current page's filtered actions
		self.agent.ActionModel = self.agent.tools.registry.create_action_model(page_url=page_url)
		# Update output model with the new actions
		if self.agent.settings.flash_mode:
			self.agent.AgentOutput = AgentOutput.type_with_custom_actions_flash_mode(self.agent.ActionModel)
		elif self.agent.settings.use_thinking:
			self.agent.AgentOutput = AgentOutput.type_with_custom_actions(self.agent.ActionModel)
		else:
			self.agent.AgentOutput = AgentOutput.type_with_custom_actions_no_thinking(self.agent.ActionModel)

		# Update done action model too
		self.agent.DoneActionModel = self.agent.tools.registry.create_action_model(include_actions=['done'], page_url=page_url)
		if self.agent.settings.flash_mode:
			self.agent.DoneAgentOutput = AgentOutput.type_with_custom_actions_flash_mode(self.agent.DoneActionModel)
		elif self.agent.settings.use_thinking:
			self.agent.DoneAgentOutput = AgentOutput.type_with_custom_actions(self.agent.DoneActionModel)
		else:
			self.agent.DoneAgentOutput = AgentOutput.type_with_custom_actions_no_thinking(self.agent.DoneActionModel)
