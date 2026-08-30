from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, RootModel, create_model

from browser_use.tools.registry.views import ActionModel, ActionRegistry, RegisteredAction

if TYPE_CHECKING:
	from browser_use.tools.registry.service import Registry
	from browser_use.tools.registry.signature import ActionSignatureNormalizer


class ActionRegistrar:
	"""Register action schemas and derive ordered action-union models."""

	def __init__(self, registry_service: Registry) -> None:
		self.registry_service = registry_service

	@property
	def registry(self) -> ActionRegistry:
		return self.registry_service.registry

	@property
	def exclude_actions(self) -> list[str]:
		return self.registry_service.exclude_actions

	@property
	def signatures(self) -> ActionSignatureNormalizer:
		return self.registry_service.signatures

	def action(
		self,
		description: str,
		param_model: type[BaseModel] | None = None,
		domains: list[str] | None = None,
		allowed_domains: list[str] | None = None,
		terminates_sequence: bool = False,
	):
		"""Decorator for registering actions"""
		# Handle aliases: domains and allowed_domains are the same parameter
		if allowed_domains is not None and domains is not None:
			raise ValueError("Cannot specify both 'domains' and 'allowed_domains' - they are aliases for the same parameter")

		final_domains = allowed_domains if allowed_domains is not None else domains

		def decorator(func: Callable):
			# Skip registration if action is in exclude_actions
			if func.__name__ in self.exclude_actions:
				return func

			# Normalize the function signature
			normalized_func, actual_param_model = self.signatures.normalize_action_function_signature(
				func, description, param_model
			)

			action = RegisteredAction(
				name=func.__name__,
				description=description,
				function=normalized_func,
				param_model=actual_param_model,
				domains=final_domains,
				terminates_sequence=terminates_sequence,
			)
			self.registry.actions[func.__name__] = action

			# Return the normalized function so it can be called with kwargs
			return normalized_func

		return decorator

	def create_action_model(self, include_actions: list[str] | None = None, page_url: str | None = None) -> type[ActionModel]:
		"""Creates a Union of individual action models from registered actions,
		used by LLM APIs that support tool calling & enforce a schema.

		Each action model contains only the specific action being used,
		rather than all actions with most set to None.
		"""
		from typing import Union

		# Filter actions based on page_url if provided:
		#   if page_url is None, only include actions with no filters
		#   if page_url is provided, only include actions that match the URL

		available_actions: dict[str, RegisteredAction] = {}
		for name, action in self.registry.actions.items():
			if include_actions is not None and name not in include_actions:
				continue

			# If no page_url provided, only include actions with no filters
			if page_url is None:
				if action.domains is None:
					available_actions[name] = action
				continue

			# Check domain filter if present
			domain_is_allowed = self.registry._match_domains(action.domains, page_url)

			# Include action if domain filter matches
			if domain_is_allowed:
				available_actions[name] = action

		# Create individual action models for each action
		individual_action_models: list[type[BaseModel]] = []

		for name, action in available_actions.items():
			# Create an individual model for each action that contains only one field
			individual_model = create_model(
				f'{name.title().replace("_", "")}ActionModel',
				__base__=ActionModel,
				**{
					name: (
						action.param_model,
						Field(description=action.description),
					)  # type: ignore
				},
			)
			individual_action_models.append(individual_model)

		# If no actions available, return empty ActionModel
		if not individual_action_models:
			return create_model('EmptyActionModel', __base__=ActionModel)

		# Create proper Union type that maintains ActionModel interface
		if len(individual_action_models) == 1:
			# If only one action, return it directly (no Union needed)
			result_model = individual_action_models[0]

		# Meaning the length is more than 1
		else:
			# Create a Union type using RootModel that properly delegates ActionModel methods
			union_type = Union[tuple(individual_action_models)]  # type: ignore : Typing doesn't understand that the length is >= 2 (by design)

			class ActionModelUnion(RootModel[union_type]):  # type: ignore
				def get_index(self) -> int | None:
					"""Delegate get_index to the underlying action model"""
					if hasattr(self.root, 'get_index'):
						return self.root.get_index()  # type: ignore
					return None

				def set_index(self, index: int):
					"""Delegate set_index to the underlying action model"""
					if hasattr(self.root, 'set_index'):
						self.root.set_index(index)  # type: ignore

				def model_dump(self, **kwargs):
					"""Delegate model_dump to the underlying action model"""
					if hasattr(self.root, 'model_dump'):
						return self.root.model_dump(**kwargs)  # type: ignore
					return super().model_dump(**kwargs)

			# Set the name for better debugging
			ActionModelUnion.__name__ = 'ActionModel'
			ActionModelUnion.__qualname__ = 'ActionModel'

			result_model = ActionModelUnion

		return result_model  # type:ignore

	def get_prompt_description(self, page_url: str | None = None) -> str:
		"""Get a description of all actions for the prompt

		If page_url is provided, only include actions that are available for that URL
		based on their domain filters
		"""
		return self.registry.get_prompt_description(page_url=page_url)
