import logging
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from browser_use.browser import BrowserSession
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm.base import BaseChatModel
from browser_use.logging_utils import time_execution_async
from browser_use.security import SensitiveData
from browser_use.tools.registry.execution import ActionExecutor
from browser_use.tools.registry.registration import ActionRegistrar
from browser_use.tools.registry.signature import ActionSignatureNormalizer
from browser_use.tools.registry.views import (
	ActionModel,
	ActionRegistry,
)

Context = TypeVar('Context')

logger = logging.getLogger(__name__)


class Registry(Generic[Context]):
	"""Service for registering and managing actions"""

	def __init__(self, exclude_actions: list[str] | None = None):
		self.registry = ActionRegistry()
		# Create a new list to avoid mutable default argument issues
		self.exclude_actions = list(exclude_actions) if exclude_actions is not None else []
		self.signatures = ActionSignatureNormalizer()
		self.executor = ActionExecutor(self)
		self.registrar = ActionRegistrar(self)

	def exclude_action(self, action_name: str) -> None:
		"""Exclude an action from the registry after initialization.

		If the action is already registered, it will be removed from the registry.
		The action is also added to the exclude_actions list to prevent re-registration.
		"""
		# Add to exclude list to prevent future registration
		if action_name not in self.exclude_actions:
			self.exclude_actions.append(action_name)

		# Remove from registry if already registered
		if action_name in self.registry.actions:
			del self.registry.actions[action_name]
			logger.debug(f'Excluded action "{action_name}" from registry')

	def action(
		self,
		description: str,
		param_model: type[BaseModel] | None = None,
		domains: list[str] | None = None,
		allowed_domains: list[str] | None = None,
		terminates_sequence: bool = False,
	):
		return self.registrar.action(
			description,
			param_model=param_model,
			domains=domains,
			allowed_domains=allowed_domains,
			terminates_sequence=terminates_sequence,
		)

	@time_execution_async('--execute_action')
	async def execute_action(
		self,
		action_name: str,
		params: dict[str, Any],
		browser_session: BrowserSession | None = None,
		page_extraction_llm: BaseChatModel | None = None,
		file_system: FileSystem | None = None,
		sensitive_data: SensitiveData | None = None,
		available_file_paths: list[str] | None = None,
		extraction_schema: dict | None = None,
	) -> Any:
		return await self.executor.execute_action(
			action_name,
			params,
			browser_session=browser_session,
			page_extraction_llm=page_extraction_llm,
			file_system=file_system,
			sensitive_data=sensitive_data,
			available_file_paths=available_file_paths,
			extraction_schema=extraction_schema,
		)

	def create_action_model(self, include_actions: list[str] | None = None, page_url: str | None = None) -> type[ActionModel]:
		return self.registrar.create_action_model(include_actions, page_url)

	def get_prompt_description(self, page_url: str | None = None) -> str:
		return self.registrar.get_prompt_description(page_url)
