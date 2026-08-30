from __future__ import annotations

import asyncio
import inspect
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from browser_use.agent.judge import construct_judge_messages
from browser_use.agent.message_manager.utils import save_conversation
from browser_use.agent.results import AgentOutput, JudgementResult
from browser_use.agent.state import AgentStepInfo
from browser_use.agent.url_detection import (
	substitute_url_candidates,
)
from browser_use.browser.views import BrowserStateSummary
from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelOutputTruncatedError, ModelProviderError, ModelRateLimitError
from browser_use.llm.messages import BaseMessage, ContentPartTextParam, UserMessage
from browser_use.logging_utils import time_execution_async

if TYPE_CHECKING:
	from browser_use.agent.service import Agent

logger = logging.getLogger(__name__)


class AgentModelInteraction:
	"""Owns model interaction behavior for an Agent."""

	def __init__(self, agent: Agent) -> None:
		self.agent = agent

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

	async def _judge_trace(self) -> JudgementResult | None:
		"""Judge the trace of the agent"""
		task = self.agent.task
		final_result = self.agent.history.final_result() or ''
		agent_steps = self.agent.history.agent_steps()
		screenshot_paths = [p for p in self.agent.history.screenshot_paths() if p is not None]

		# Construct input messages for judge evaluation
		input_messages = construct_judge_messages(
			task=task,
			final_result=final_result,
			agent_steps=agent_steps,
			screenshot_paths=screenshot_paths,
			max_images=10,
			ground_truth=self.agent.settings.ground_truth,
			use_vision=self.agent.settings.use_vision,
		)

		# Call LLM with JudgementResult as output format
		kwargs: dict = {'output_format': JudgementResult}

		# Only pass request_type for ChatBrowserUse (other providers don't support it)
		if self.agent.judge_llm.provider == 'browser-use':
			kwargs['request_type'] = 'judge'
			kwargs['session_id'] = self.agent.session_id

		try:
			response = await self.agent.judge_llm.ainvoke(input_messages, **kwargs)
			judgement: JudgementResult = response.completion  # type: ignore[assignment]
			return judgement
		except Exception as e:
			self.agent.logger.error(f'Judge trace failed: {e}')
			# Return a default judgement on failure
			return None

	async def _judge_and_log(self) -> None:
		"""Run judge evaluation and log the verdict.

		The judge verdict is attached to the action result but does NOT override
		last_result.success, which remains the agent's self-report.
		"""
		judgement = await self._judge_trace()

		# Attach judgement to last action result
		if self.agent.history.history[-1].result[-1].is_done:
			last_result = self.agent.history.history[-1].result[-1]
			last_result.judgement = judgement

			# Get self-reported success
			self_reported_success = last_result.success

			# Log the verdict based on self-reported success and judge verdict
			if judgement:
				# If both self-reported and judge agree on success, don't log
				if self_reported_success is True and judgement.verdict is True:
					return

				judge_log = '\n'
				# If agent reported success but judge thinks it failed, show warning
				if self_reported_success is True and judgement.verdict is False:
					judge_log += '⚠️  \033[33mAgent reported success but judge thinks task failed\033[0m\n'

				# Otherwise, show full judge result
				verdict_color = '\033[32m' if judgement.verdict else '\033[31m'
				verdict_text = '✅ PASS' if judgement.verdict else '❌ FAIL'
				judge_log += f'⚖️  {verdict_color}Judge Verdict: {verdict_text}\033[0m\n'
				if judgement.failure_reason:
					judge_log += f'   Failure Reason: {judgement.failure_reason}\n'
				if judgement.reached_captcha:
					self.agent.logger.warning(
						'Agent was blocked by a captcha. Cloud browsers include stealth fingerprinting and proxy rotation to avoid this.\n'
						'         Try: Browser(use_cloud=True)  |  Get an API key: https://cloud.browser-use.com?utm_source=oss&utm_medium=captcha_nudge'
					)
				judge_log += f'   {judgement.reasoning}\n'
				self.agent.logger.info(judge_log)

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

	def _replace_urls_in_text(self, text: str) -> tuple[str, dict[str, str]]:
		"""Replace URLs in a text string"""

		replaced_urls: dict[str, str] = {}

		def replace_url(match: re.Match) -> str:
			"""Url can only have 1 query and 1 fragment"""
			import hashlib

			original_url = match.group(0)

			# Find where the query/fragment starts
			query_start = original_url.find('?')
			fragment_start = original_url.find('#')

			# Find the earliest position of query or fragment
			after_path_start = len(original_url)  # Default: no query/fragment
			if query_start != -1:
				after_path_start = min(after_path_start, query_start)
			if fragment_start != -1:
				after_path_start = min(after_path_start, fragment_start)

			# Split URL into base (up to path) and after_path (query + fragment)
			base_url = original_url[:after_path_start]
			after_path = original_url[after_path_start:]

			# If after_path is within the limit, don't shorten
			if len(after_path) <= self.agent._url_shortening_limit:
				return original_url

			# If after_path is too long, truncate and add hash
			if after_path:
				truncated_after_path = after_path[: self.agent._url_shortening_limit]
				# Create a short hash of the full after_path content
				hash_obj = hashlib.md5(after_path.encode('utf-8'))
				short_hash = hash_obj.hexdigest()[:7]
				# Create shortened URL
				shortened = f'{base_url}{truncated_after_path}...{short_hash}'
				# Only use shortened URL if it's actually shorter than the original
				if len(shortened) < len(original_url):
					replaced_urls[shortened] = original_url
					return shortened

			return original_url

		return substitute_url_candidates(text, replace_url), replaced_urls

	def _process_messsages_and_replace_long_urls_shorter_ones(self, input_messages: list[BaseMessage]) -> dict[str, str]:
		"""Replace long URLs with shorter ones
		? @dev edits input_messages in place

		returns:
			tuple[filtered_input_messages, urls we replaced {shorter_url: original_url}]
		"""
		from browser_use.llm.messages import AssistantMessage, UserMessage

		urls_replaced: dict[str, str] = {}

		# Process each message, in place
		for message in input_messages:
			# no need to process SystemMessage, we have control over that anyway
			if isinstance(message, (UserMessage, AssistantMessage)):
				if isinstance(message.content, str):
					# Simple string content
					message.content, replaced_urls = self._replace_urls_in_text(message.content)
					urls_replaced.update(replaced_urls)

				elif isinstance(message.content, list):
					# List of content parts
					for part in message.content:
						if isinstance(part, ContentPartTextParam):
							part.text, replaced_urls = self._replace_urls_in_text(part.text)
							urls_replaced.update(replaced_urls)

		return urls_replaced

	@staticmethod
	def _recursive_process_all_strings_inside_pydantic_model(model: BaseModel, url_replacements: dict[str, str]) -> None:
		"""Recursively process all strings inside a Pydantic model, replacing shortened URLs with originals in place."""
		for field_name, field_value in model.__dict__.items():
			if isinstance(field_value, str):
				# Replace shortened URLs with original URLs in string
				processed_string = AgentModelInteraction._replace_shortened_urls_in_string(field_value, url_replacements)
				setattr(model, field_name, processed_string)
			elif isinstance(field_value, BaseModel):
				# Recursively process nested Pydantic models
				AgentModelInteraction._recursive_process_all_strings_inside_pydantic_model(field_value, url_replacements)
			elif isinstance(field_value, dict):
				# Process dictionary values in place
				AgentModelInteraction._recursive_process_dict(field_value, url_replacements)
			elif isinstance(field_value, (list, tuple)):
				processed_value = AgentModelInteraction._recursive_process_list_or_tuple(field_value, url_replacements)
				setattr(model, field_name, processed_value)

	@staticmethod
	def _recursive_process_dict(dictionary: dict, url_replacements: dict[str, str]) -> None:
		"""Helper method to process dictionaries."""
		for k, v in dictionary.items():
			if isinstance(v, str):
				dictionary[k] = AgentModelInteraction._replace_shortened_urls_in_string(v, url_replacements)
			elif isinstance(v, BaseModel):
				AgentModelInteraction._recursive_process_all_strings_inside_pydantic_model(v, url_replacements)
			elif isinstance(v, dict):
				AgentModelInteraction._recursive_process_dict(v, url_replacements)
			elif isinstance(v, (list, tuple)):
				dictionary[k] = AgentModelInteraction._recursive_process_list_or_tuple(v, url_replacements)

	@staticmethod
	def _recursive_process_list_or_tuple(container: list | tuple, url_replacements: dict[str, str]) -> list | tuple:
		"""Helper method to process lists and tuples."""
		if isinstance(container, tuple):
			# For tuples, create a new tuple with processed items
			processed_items = []
			for item in container:
				if isinstance(item, str):
					processed_items.append(AgentModelInteraction._replace_shortened_urls_in_string(item, url_replacements))
				elif isinstance(item, BaseModel):
					AgentModelInteraction._recursive_process_all_strings_inside_pydantic_model(item, url_replacements)
					processed_items.append(item)
				elif isinstance(item, dict):
					AgentModelInteraction._recursive_process_dict(item, url_replacements)
					processed_items.append(item)
				elif isinstance(item, (list, tuple)):
					processed_items.append(AgentModelInteraction._recursive_process_list_or_tuple(item, url_replacements))
				else:
					processed_items.append(item)
			return tuple(processed_items)
		else:
			# For lists, modify in place
			for i, item in enumerate(container):
				if isinstance(item, str):
					container[i] = AgentModelInteraction._replace_shortened_urls_in_string(item, url_replacements)
				elif isinstance(item, BaseModel):
					AgentModelInteraction._recursive_process_all_strings_inside_pydantic_model(item, url_replacements)
				elif isinstance(item, dict):
					AgentModelInteraction._recursive_process_dict(item, url_replacements)
				elif isinstance(item, (list, tuple)):
					container[i] = AgentModelInteraction._recursive_process_list_or_tuple(item, url_replacements)
			return container

	@staticmethod
	def _replace_shortened_urls_in_string(text: str, url_replacements: dict[str, str]) -> str:
		"""Replace all shortened URLs in a string with their original URLs."""
		result = text
		for shortened_url, original_url in url_replacements.items():
			result = result.replace(shortened_url, original_url)
		return result

	@time_execution_async('--get_next_action')
	async def get_model_output(self, input_messages: list[BaseMessage]) -> AgentOutput:
		"""Get next action from LLM based on current state"""

		urls_replaced = self._process_messsages_and_replace_long_urls_shorter_ones(input_messages)

		# Build kwargs for ainvoke
		# Note: ChatBrowserUse will automatically generate action descriptions from output_format schema
		kwargs: dict = {'output_format': self.agent.AgentOutput, 'session_id': self.agent.session_id}

		try:
			response = await self.agent.llm.ainvoke(input_messages, **kwargs)
			parsed: AgentOutput = response.completion  # type: ignore[assignment]

			# Replace any shortened URLs in the LLM response back to original URLs
			if urls_replaced:
				self._recursive_process_all_strings_inside_pydantic_model(parsed, urls_replaced)

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
