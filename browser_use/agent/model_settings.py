from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from browser_use.config import get_environment_config
from browser_use.llm.base import BaseChatModel

if TYPE_CHECKING:
	from browser_use.agent.service import Agent

logger = logging.getLogger(__name__)


class AgentModelSettings:
	"""Normalize model settings and verify provider configuration."""

	def __init__(self, agent: Agent) -> None:
		self.agent = agent

	def normalize_model_settings(
		self,
		llm: BaseChatModel | None,
		page_extraction_llm: BaseChatModel | None,
		judge_llm: BaseChatModel | None,
		flash_mode: bool,
		enable_planning: bool,
		llm_screenshot_size: tuple[int, int] | None,
		llm_timeout: int | None,
		available_file_paths: list[str] | None,
	) -> tuple[BaseChatModel, BaseChatModel, BaseChatModel, bool, bool, tuple[int, int] | None, int, list[str]]:
		"""Resolve model-derived defaults without retaining independent state."""
		if llm_screenshot_size is not None:
			if not isinstance(llm_screenshot_size, tuple) or len(llm_screenshot_size) != 2:
				raise ValueError('llm_screenshot_size must be a tuple of (width, height)')
			width, height = llm_screenshot_size
			if not isinstance(width, int) or not isinstance(height, int):
				raise ValueError('llm_screenshot_size dimensions must be integers')
			if width < 100 or height < 100:
				raise ValueError('llm_screenshot_size dimensions must be at least 100 pixels')
			self.agent.logger.info(f'🖼️  LLM screenshot resizing enabled: {width}x{height}')

		if llm is None:
			default_llm_name = get_environment_config().DEFAULT_LLM
			if default_llm_name:
				from browser_use.llm.models import get_llm_by_name

				llm = get_llm_by_name(default_llm_name)
			else:
				raise ValueError('Agent requires an llm argument or the DEFAULT_LLM environment variable')

		if flash_mode:
			enable_planning = False

		if llm_screenshot_size is None:
			model_name = getattr(llm, 'model', '')
			if isinstance(model_name, str) and model_name.rsplit('/', 1)[-1].startswith('claude-sonnet'):
				llm_screenshot_size = (1400, 850)
				logger.info('🖼️  Auto-configured LLM screenshot size for Claude Sonnet: 1400x850')

		if page_extraction_llm is None:
			page_extraction_llm = llm
		if judge_llm is None:
			judge_llm = llm
		if llm_timeout is None:
			model_name = getattr(llm, 'model', '').lower()
			if 'gemini' in model_name:
				llm_timeout = 90 if '3-pro' in model_name else 75
			elif 'groq' in model_name:
				llm_timeout = 30
			elif any(name in model_name for name in ('o3', 'claude', 'sonnet', 'deepseek')):
				llm_timeout = 90
			else:
				llm_timeout = 75

		return (
			llm,
			page_extraction_llm,
			judge_llm,
			flash_mode,
			enable_planning,
			llm_screenshot_size,
			llm_timeout,
			available_file_paths if available_file_paths is not None else [],
		)

	def _verify_and_setup_llm(self):
		"""
		Verify that the LLM API keys are setup and the LLM API is responding properly.
		Also handles tool calling method detection if in auto mode.
		"""

		# Skip verification if already done
		if getattr(self.agent.llm, '_verified_api_keys', None) is True or get_environment_config().SKIP_LLM_API_KEY_VERIFICATION:
			setattr(self.agent.llm, '_verified_api_keys', True)
			return True
