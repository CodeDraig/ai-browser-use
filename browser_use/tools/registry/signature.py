import asyncio
import functools
import inspect
from collections.abc import Callable
from inspect import Parameter, iscoroutinefunction, signature
from types import UnionType
from typing import Optional, Union, get_args, get_origin

from pydantic import BaseModel, create_model

from browser_use.browser import BrowserSession
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm.base import BaseChatModel
from browser_use.tools.registry.views import ActionModel, SpecialActionParameters


class ActionSignatureNormalizer:
	"""Normalize action signatures and derive their Pydantic parameter models."""

	def _get_special_param_types(self) -> dict[str, type | UnionType | None]:
		"""Get the expected types for special parameters from SpecialActionParameters"""
		# Manually define the expected types to avoid issues with Optional handling.
		# we should try to reduce this list to 0 if possible, give as few standardized objects to all the actions
		# but each driver should decide what is relevant to expose the action methods,
		# e.g. CDP client, 2fa code getters, sensitive_data wrappers, other context, etc.
		return {
			'context': None,  # Context is a TypeVar, so we can't validate type
			'browser_session': BrowserSession,
			'page_url': str,
			'cdp_client': None,  # CDPClient type from cdp_use, but we don't import it here
			'page_extraction_llm': BaseChatModel,
			'available_file_paths': list,
			'has_sensitive_data': bool,
			'file_system': FileSystem,
			'extraction_schema': None,  # dict | None, skip type validation
		}

	def normalize_action_function_signature(
		self,
		func: Callable,
		description: str,
		param_model: type[BaseModel] | None = None,
	) -> tuple[Callable, type[BaseModel]]:
		"""
		Normalize action function to accept only kwargs.

		Returns:
			- Normalized function that accepts (*_, params: ParamModel, **special_params)
			- The param model to use for registration
		"""
		sig = signature(func)
		parameters = list(sig.parameters.values())
		special_param_types = self._get_special_param_types()
		special_param_names = set(special_param_types.keys())

		# Step 1: Validate no **kwargs in original function signature
		# if it needs default values it must use a dedicated param_model: BaseModel instead
		for param in parameters:
			if param.kind == Parameter.VAR_KEYWORD:
				raise ValueError(
					f"Action '{func.__name__}' has **{param.name} which is not allowed. "
					f'Actions must have explicit positional parameters only.'
				)

		# Step 2: Separate special and action parameters
		action_params = []
		special_params = []
		param_model_provided = param_model is not None

		for i, param in enumerate(parameters):
			# Check if this is a Type 1 pattern (first param is BaseModel)
			if i == 0 and param_model_provided and param.name not in special_param_names:
				# This is Type 1 pattern - skip the params argument
				continue

			if param.name in special_param_names:
				# Validate special parameter type
				expected_type = special_param_types.get(param.name)
				if param.annotation != Parameter.empty and expected_type is not None:
					# Handle Optional types - normalize both sides
					param_type = param.annotation
					origin = get_origin(param_type)
					if origin is Union:
						args = get_args(param_type)
						# Find non-None type
						param_type = next((arg for arg in args if arg is not type(None)), param_type)

					# Check if types are compatible (exact match, subclass, or generic list)
					types_compatible = (
						param_type == expected_type
						or (
							inspect.isclass(param_type)
							and inspect.isclass(expected_type)
							and issubclass(param_type, expected_type)
						)
						or
						# Handle list[T] vs list comparison
						(expected_type is list and (param_type is list or get_origin(param_type) is list))
					)

					if not types_compatible:
						expected_type_name = getattr(expected_type, '__name__', str(expected_type))
						param_type_name = getattr(param_type, '__name__', str(param_type))
						raise ValueError(
							f"Action '{func.__name__}' parameter '{param.name}: {param_type_name}' "
							f"conflicts with special argument injected by tools: '{param.name}: {expected_type_name}'"
						)
				special_params.append(param)
			else:
				action_params.append(param)

		# Step 3: Create or validate param model
		if not param_model_provided:
			# Type 2: Generate param model from action params
			if action_params:
				params_dict = {}
				for param in action_params:
					annotation = param.annotation if param.annotation != Parameter.empty else str
					default = ... if param.default == Parameter.empty else param.default
					params_dict[param.name] = (annotation, default)

				param_model = create_model(f'{func.__name__}_Params', __base__=ActionModel, **params_dict)
			else:
				# No action params, create empty model
				param_model = create_model(
					f'{func.__name__}_Params',
					__base__=ActionModel,
				)
		assert param_model is not None, f'param_model is None for {func.__name__}'

		# Step 4: Create normalized wrapper function
		@functools.wraps(func)
		async def normalized_wrapper(*args, params: BaseModel | None = None, **kwargs):
			"""Normalized action that only accepts kwargs"""
			# Validate no positional args
			if args:
				raise TypeError(f'{func.__name__}() does not accept positional arguments, only keyword arguments are allowed')

			# Prepare arguments for original function
			call_args = []
			call_kwargs = {}

			# Handle Type 1 pattern (first arg is the param model)
			if param_model_provided and parameters and parameters[0].name not in special_param_names:
				if params is None:
					raise ValueError(f"{func.__name__}() missing required 'params' argument")
				# For Type 1, we'll use the params object as first argument
				pass
			else:
				# Type 2 pattern - need to unpack params
				# If params is None, try to create it from kwargs
				if params is None and action_params:
					# Extract action params from kwargs
					action_kwargs = {}
					for param in action_params:
						if param.name in kwargs:
							action_kwargs[param.name] = kwargs[param.name]
					if action_kwargs:
						# Use the param_model which has the correct types defined
						params = param_model(**action_kwargs)

			# Build call_args by iterating through original function parameters in order
			params_dict = params.model_dump() if params is not None else {}

			for i, param in enumerate(parameters):
				# Skip first param for Type 1 pattern (it's the model itself)
				if param_model_provided and i == 0 and param.name not in special_param_names:
					call_args.append(params)
				elif param.name in special_param_names:
					# This is a special parameter
					if param.name in kwargs:
						value = kwargs[param.name]
						# Check if required special param is None
						if value is None and param.default == Parameter.empty:
							if param.name == 'browser_session':
								raise ValueError(f'Action {func.__name__} requires browser_session but none provided.')
							elif param.name == 'page_extraction_llm':
								raise ValueError(f'Action {func.__name__} requires page_extraction_llm but none provided.')
							elif param.name == 'file_system':
								raise ValueError(f'Action {func.__name__} requires file_system but none provided.')
							elif param.name == 'page':
								raise ValueError(f'Action {func.__name__} requires page but none provided.')
							elif param.name == 'available_file_paths':
								raise ValueError(f'Action {func.__name__} requires available_file_paths but none provided.')
							elif param.name == 'file_system':
								raise ValueError(f'Action {func.__name__} requires file_system but none provided.')
							else:
								raise ValueError(f"{func.__name__}() missing required special parameter '{param.name}'")
						call_args.append(value)
					elif param.default != Parameter.empty:
						call_args.append(param.default)
					else:
						# Special param is required but not provided
						if param.name == 'browser_session':
							raise ValueError(f'Action {func.__name__} requires browser_session but none provided.')
						elif param.name == 'page_extraction_llm':
							raise ValueError(f'Action {func.__name__} requires page_extraction_llm but none provided.')
						elif param.name == 'file_system':
							raise ValueError(f'Action {func.__name__} requires file_system but none provided.')
						elif param.name == 'page':
							raise ValueError(f'Action {func.__name__} requires page but none provided.')
						elif param.name == 'available_file_paths':
							raise ValueError(f'Action {func.__name__} requires available_file_paths but none provided.')
						elif param.name == 'file_system':
							raise ValueError(f'Action {func.__name__} requires file_system but none provided.')
						else:
							raise ValueError(f"{func.__name__}() missing required special parameter '{param.name}'")
				else:
					# This is an action parameter
					if param.name in params_dict:
						call_args.append(params_dict[param.name])
					elif param.default != Parameter.empty:
						call_args.append(param.default)
					else:
						raise ValueError(f"{func.__name__}() missing required parameter '{param.name}'")

			# Call original function with positional args
			if iscoroutinefunction(func):
				return await func(*call_args)
			else:
				return await asyncio.to_thread(func, *call_args)

		# Update wrapper signature to be kwargs-only
		new_params = [Parameter('params', Parameter.KEYWORD_ONLY, default=None, annotation=Optional[param_model])]

		# Add special params as keyword-only
		for sp in special_params:
			new_params.append(Parameter(sp.name, Parameter.KEYWORD_ONLY, default=sp.default, annotation=sp.annotation))

		# Add **kwargs to accept and ignore extra params
		new_params.append(Parameter('kwargs', Parameter.VAR_KEYWORD))

		normalized_wrapper.__signature__ = sig.replace(parameters=new_params)  # type: ignore[attr-defined]

		return normalized_wrapper, param_model

	# @time_execution_sync('--create_param_model')
	def create_param_model(self, function: Callable) -> type[BaseModel]:
		"""Creates a Pydantic model from function signature"""
		sig = signature(function)
		special_param_names = set(SpecialActionParameters.model_fields.keys())
		params = {
			name: (param.annotation, ... if param.default == param.empty else param.default)
			for name, param in sig.parameters.items()
			if name not in special_param_names
		}
		# TODO: make the types here work
		return create_model(
			f'{function.__name__}_parameters',
			__base__=ActionModel,
			**params,  # type: ignore
		)
