from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Literal, cast

from browser_use import Browser, BrowserProfile, BrowserSession
from browser_use.agent.history import AgentStructuredOutput
from browser_use.agent.message_manager.service import MessageManager
from browser_use.agent.prompts import SystemPrompt
from browser_use.agent.results import AgentOutput
from browser_use.agent.url_detection import is_placeholder_url, sanitize_url_candidate
from browser_use.browser.session import DEFAULT_BROWSER_PROFILE
from browser_use.llm.base import BaseChatModel
from browser_use.tools.registry.views import ActionModel
from browser_use.tools.service import Tools

if TYPE_CHECKING:
	from browser_use.agent.service import Agent

logger = logging.getLogger(__name__)


class AgentConstruction:
	"""Construct browser, tools, messages, action schemas, and initial actions."""

	def __init__(self, agent: Agent) -> None:
		self.agent = agent

	def create_browser_session(
		self,
		browser_profile: BrowserProfile | None,
		browser: Browser | None,
		demo_mode: bool | None,
	) -> BrowserSession:
		"""Resolve profile precedence and create or reuse the browser session."""
		base_profile = browser_profile or DEFAULT_BROWSER_PROFILE
		if base_profile is DEFAULT_BROWSER_PROFILE:
			base_profile = base_profile.model_copy()
		if demo_mode is not None and base_profile.demo_mode != demo_mode:
			base_profile = base_profile.model_copy(update={'demo_mode': demo_mode})

		if browser is not None:
			if demo_mode is not None and browser.browser_profile.demo_mode != demo_mode:
				browser.browser_profile = browser.browser_profile.model_copy(update={'demo_mode': demo_mode})
			return browser

		from uuid_extensions import uuid7str

		return BrowserSession(
			browser_profile=base_profile,
			id=uuid7str()[:-4] + self.agent.id[-4:],
		)

	def configure_tools(
		self,
		tools: Tools | None,
		use_vision: bool | Literal['auto'],
		display_files_in_done_text: bool,
		llm: BaseChatModel,
		output_model_schema: type[AgentStructuredOutput] | None,
	) -> type[AgentStructuredOutput] | None:
		"""Resolve tools, model capabilities, and structured-output precedence."""
		if tools is None:
			excluded = ['screenshot'] if use_vision != 'auto' else []
			tools = Tools(exclude_actions=excluded, display_files_in_done_text=display_files_in_done_text)
		self.agent.tools = tools
		if use_vision != 'auto':
			self.agent.tools.exclude_action('screenshot')

		model_name = getattr(llm, 'model', '').lower()
		if any(
			pattern in model_name
			for pattern in ('claude-sonnet-4', 'claude-opus-4', 'claude-fable-5', 'gemini-3-pro', 'browser-use/')
		):
			self.agent.tools.set_coordinate_clicking(True)

		tools_output_model = self.agent.tools.get_output_model()
		if output_model_schema is not None and tools_output_model is not None:
			if output_model_schema is not tools_output_model:
				logger.warning(
					f'output_model_schema ({output_model_schema.__name__}) differs from Tools output_model '
					f'({tools_output_model.__name__}). Using Agent output_model_schema.'
				)
		elif output_model_schema is None and tools_output_model is not None:
			output_model_schema = cast(type[AgentStructuredOutput], tools_output_model)
		if output_model_schema is not None:
			self.agent.tools.use_structured_output_action(output_model_schema)
		return output_model_schema

	def create_message_manager(
		self,
		override_system_message: str | None,
		extend_system_message: str | None,
		llm_screenshot_size: tuple[int, int] | None,
	) -> MessageManager:
		"""Construct the message manager from the normalized owning Agent state."""
		from browser_use.llm.anthropic.chat import ChatAnthropic

		is_anthropic = isinstance(self.agent.llm, ChatAnthropic)
		is_browser_use_model = 'browser-use/' in self.agent.llm.model.lower()
		return MessageManager(
			task=self.agent.task,
			system_message=SystemPrompt(
				max_actions_per_step=self.agent.settings.max_actions_per_step,
				override_system_message=override_system_message,
				extend_system_message=extend_system_message,
				use_thinking=self.agent.settings.use_thinking,
				flash_mode=self.agent.settings.flash_mode,
				is_anthropic=is_anthropic,
				is_browser_use_model=is_browser_use_model,
				model_name=self.agent.llm.model,
			).get_system_message(),
			file_system=self.agent.file_system,
			state=self.agent.state.message_manager_state,
			use_thinking=self.agent.settings.use_thinking,
			include_attributes=self.agent.settings.include_attributes,
			sensitive_data=self.agent.sensitive_data,
			max_history_items=self.agent.settings.max_history_items,
			vision_detail_level=self.agent.settings.vision_detail_level,
			include_tool_call_examples=self.agent.settings.include_tool_call_examples,
			include_recent_events=self.agent.include_recent_events,
			sample_images=self.agent.sample_images,
			llm_screenshot_size=llm_screenshot_size,
			max_clickable_elements_length=self.agent.settings.max_clickable_elements_length,
		)

	def _enhance_task_with_schema(self, task: str, output_model_schema: type[AgentStructuredOutput] | None) -> str:
		"""Enhance task description with output schema information if provided."""
		if output_model_schema is None:
			return task

		try:
			schema = output_model_schema.model_json_schema()
			import json

			schema_json = json.dumps(schema, indent=2)

			enhancement = f'\nExpected output format: {output_model_schema.__name__}\n{schema_json}'
			return task + enhancement
		except Exception as e:
			self.agent.logger.debug(f'Could not parse output schema: {e}')

		return task

	def _setup_action_models(self) -> None:
		"""Setup dynamic action models from tools registry"""
		# Initially only include actions with no filters
		self.agent.ActionModel = self.agent.tools.registry.create_action_model()
		# Create output model with the dynamic actions
		if self.agent.settings.flash_mode:
			self.agent.AgentOutput = AgentOutput.type_with_custom_actions_flash_mode(self.agent.ActionModel)
		elif self.agent.settings.use_thinking:
			self.agent.AgentOutput = AgentOutput.type_with_custom_actions(self.agent.ActionModel)
		else:
			self.agent.AgentOutput = AgentOutput.type_with_custom_actions_no_thinking(self.agent.ActionModel)

		# used to force the done action when max_steps is reached
		self.agent.DoneActionModel = self.agent.tools.registry.create_action_model(include_actions=['done'])
		if self.agent.settings.flash_mode:
			self.agent.DoneAgentOutput = AgentOutput.type_with_custom_actions_flash_mode(self.agent.DoneActionModel)
		elif self.agent.settings.use_thinking:
			self.agent.DoneAgentOutput = AgentOutput.type_with_custom_actions(self.agent.DoneActionModel)
		else:
			self.agent.DoneAgentOutput = AgentOutput.type_with_custom_actions_no_thinking(self.agent.DoneActionModel)

	def _extract_start_url(self, task: str) -> str | None:
		"""Extract URL from task string using naive pattern matching."""

		# Remove email addresses from task before looking for URLs
		task_without_emails = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '', task)

		# Look for common URL patterns
		patterns = [
			r'(?:https?|file)://[^\s<>"\']+',  # Full URLs
			r'(?:www\.)?[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}(?:/[^\s<>"\']*)?',  # Domain names with subdomains and optional paths
		]

		# File extensions that should be excluded from URL detection
		# These are likely files rather than web pages to navigate to
		excluded_extensions = {
			# Documents
			'pdf',
			'doc',
			'docx',
			'xls',
			'xlsx',
			'ppt',
			'pptx',
			'odt',
			'ods',
			'odp',
			# Text files
			'txt',
			'md',
			'csv',
			'json',
			'xml',
			'yaml',
			'yml',
			# Archives
			'zip',
			'rar',
			'7z',
			'tar',
			'gz',
			'bz2',
			'xz',
			# Images
			'jpg',
			'jpeg',
			'png',
			'gif',
			'bmp',
			'svg',
			'webp',
			'ico',
			# Audio/Video
			'mp3',
			'mp4',
			'avi',
			'mkv',
			'mov',
			'wav',
			'flac',
			'ogg',
			# Code/Data
			'py',
			'js',
			'css',
			'java',
			'cpp',
			# Academic/Research
			'bib',
			'bibtex',
			'tex',
			'latex',
			'cls',
			'sty',
			# Other common file types
			'exe',
			'msi',
			'dmg',
			'pkg',
			'deb',
			'rpm',
			'iso',
			# GitHub/Project paths
			'polynomial',
		}

		excluded_words = {
			'never',
			'dont',
			'not',
			"don't",
		}

		found_urls = []
		matched_spans: list[tuple[int, int]] = []
		for pattern in patterns:
			matches = re.finditer(pattern, task_without_emails)
			for match in matches:
				url = match.group(0)
				original_position = match.start()  # Store original position before URL modification

				# Skip fragments of URLs already matched by earlier pattern
				if any(match.start() < end and match.end() > start for start, end in matched_spans):
					continue
				matched_spans.append((match.start(), match.end()))

				# Remove trailing punctuation that's not part of URLs
				url = sanitize_url_candidate(url)

				if is_placeholder_url(url):
					self.agent.logger.debug(f'Excluding placeholder URL from auto-navigation: {url}')
					continue

				url_lower = url.lower()
				has_scheme = url_lower.startswith(('http://', 'https://', 'file://'))

				# Check if URL ends with file extension
				should_exclude = False
				if not url_lower.startswith('file://'):
					for ext in excluded_extensions:
						if f'.{ext}' in url_lower:
							should_exclude = True
							break
					if not has_scheme and '.htm' in url_lower:
						should_exclude = True

				if should_exclude:
					self.agent.logger.debug(f'Excluding URL with file extension from auto-navigation: {url}')
					continue

				# If in the 20 characters before the url position is a word in excluded_words skip to avoid "Never go to this url"
				context_start = max(0, original_position - 20)
				context_text = task_without_emails[context_start:original_position]
				if any(word.lower() in context_text.lower() for word in excluded_words):
					self.agent.logger.debug(
						f'Excluding URL with word in excluded words from auto-navigation: {url} (context: "{context_text.strip()}")'
					)
					continue

				# Add https:// if missing (after excluded words check to avoid position calculation issues)
				if not has_scheme:
					url = 'https://' + url

				found_urls.append(url)

		unique_urls = list(set(found_urls))
		# If multiple URLs found, skip directly_open_urling
		if len(unique_urls) > 1:
			self.agent.logger.debug(f'Multiple URLs found ({len(found_urls)}), skipping directly_open_url to avoid ambiguity')
			return None

		# If exactly one URL found, return it
		if len(unique_urls) == 1:
			return unique_urls[0]

		return None

	def _convert_initial_actions(self, actions: list[dict[str, dict[str, Any]]]) -> list[ActionModel]:
		"""Convert dictionary-based actions to ActionModel instances"""
		converted_actions = []
		action_model = self.agent.ActionModel
		for action_dict in actions:
			# Each action_dict should have a single key-value pair
			action_name = next(iter(action_dict))
			params = action_dict[action_name]

			# Get the parameter model for this action from registry
			action_info = self.agent.tools.registry.registry.actions[action_name]
			param_model = action_info.param_model

			# Create validated parameters using the appropriate param model
			validated_params = param_model(**params)

			# Create ActionModel instance with the validated parameters
			action_model = self.agent.ActionModel(**{action_name: validated_params})
			converted_actions.append(action_model)

		return converted_actions
