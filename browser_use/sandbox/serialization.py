from __future__ import annotations

import dataclasses
import enum
import inspect
from collections.abc import Callable
from typing import Any, Union, cast, get_args, get_origin


def _extract_all_params(func: Callable, args: tuple, kwargs: dict) -> dict[str, Any]:
	"""Extract all parameters including explicit params and closure variables

	Args:
		func: The function being decorated
		args: Positional arguments passed to the function
		kwargs: Keyword arguments passed to the function

	Returns:
		Dictionary of all parameters {name: value}
	"""
	sig = inspect.signature(func)
	bound_args = sig.bind_partial(*args, **kwargs)
	bound_args.apply_defaults()

	all_params: dict[str, Any] = {}

	# 1. Extract explicit parameters (skip 'browser' and 'self')
	for param_name, param_value in bound_args.arguments.items():
		if param_name == 'browser':
			continue
		if param_name == 'self' and hasattr(param_value, '__dict__'):
			# Extract self attributes as individual variables
			for attr_name, attr_value in param_value.__dict__.items():
				all_params[attr_name] = attr_value
		else:
			all_params[param_name] = param_value

	# 2. Extract closure variables
	if func.__closure__:
		closure_vars = func.__code__.co_freevars
		closure_values = [cell.cell_contents for cell in func.__closure__]

		for name, value in zip(closure_vars, closure_values):
			# Skip if already captured from explicit params
			if name in all_params:
				continue
			# Special handling for 'self' in closures
			if name == 'self' and hasattr(value, '__dict__'):
				for attr_name, attr_value in value.__dict__.items():
					if attr_name not in all_params:
						all_params[attr_name] = attr_value
			else:
				all_params[name] = value

	# 3. Extract referenced globals (like logger, module-level vars, etc.)
	#    Let cloudpickle handle serialization instead of special-casing
	for name in func.__code__.co_names:
		if name in all_params:
			continue
		if name in func.__globals__:
			all_params[name] = func.__globals__[name]

	return all_params


def _parse_with_type_annotation(data: Any, annotation: Any) -> Any:
	"""Parse data with type annotation without validation, recursively handling nested types

	This function reconstructs Pydantic models, dataclasses, and enums from JSON dicts
	without running validation logic. It recursively parses nested fields to ensure
	complete type fidelity.
	"""
	try:
		if data is None:
			return None

		origin = get_origin(annotation)
		args = get_args(annotation)

		# Handle Union types
		if origin is Union or (hasattr(annotation, '__class__') and annotation.__class__.__name__ == 'UnionType'):
			union_args = args or getattr(annotation, '__args__', [])
			for arg in union_args:
				if arg is type(None) and data is None:
					return None
				if arg is not type(None):
					try:
						return _parse_with_type_annotation(data, arg)
					except Exception:
						continue
			return data

		# Handle List types
		if origin is list:
			if not isinstance(data, list):
				return data
			if args:
				return [_parse_with_type_annotation(item, args[0]) for item in data]
			return data

		# Handle Tuple types (JSON serializes tuples as lists)
		if origin is tuple:
			if not isinstance(data, (list, tuple)):
				return data
			if args:
				# Parse each element according to its type annotation
				parsed_items = []
				for i, item in enumerate(data):
					# Use the corresponding type arg, or the last one if fewer args than items
					type_arg = args[i] if i < len(args) else args[-1] if args else Any
					parsed_items.append(_parse_with_type_annotation(item, type_arg))
				return tuple(parsed_items)
			return tuple(data) if isinstance(data, list) else data

		# Handle Dict types
		if origin is dict:
			if not isinstance(data, dict):
				return data
			if len(args) == 2:
				return {_parse_with_type_annotation(k, args[0]): _parse_with_type_annotation(v, args[1]) for k, v in data.items()}
			return data

		# Handle Enum types
		if inspect.isclass(annotation) and issubclass(annotation, enum.Enum):
			if isinstance(data, str):
				try:
					return annotation[data]  # By name
				except KeyError:
					return annotation(data)  # By value
			return annotation(data)  # By value

		# Handle Pydantic v2 - use model_construct to skip validation and recursively parse nested fields
		# Get the actual class (unwrap generic if needed)
		# For Pydantic generics, get_origin() returns None, so check __pydantic_generic_metadata__ first
		pydantic_generic_meta = getattr(annotation, '__pydantic_generic_metadata__', None)
		if pydantic_generic_meta and pydantic_generic_meta.get('origin'):
			actual_class = pydantic_generic_meta['origin']
			generic_args = pydantic_generic_meta.get('args', ())
		else:
			actual_class = get_origin(annotation) or annotation
			generic_args = get_args(annotation)

		if hasattr(actual_class, 'model_construct'):
			if not isinstance(data, dict):
				return data
			# Recursively parse each field according to its type annotation
			if hasattr(actual_class, 'model_fields'):
				parsed_fields = {}
				for field_name, field_info in actual_class.model_fields.items():
					if field_name in data:
						field_annotation = field_info.annotation
						parsed_fields[field_name] = _parse_with_type_annotation(data[field_name], field_annotation)
				result = actual_class.model_construct(**parsed_fields)

				# Special handling for AgentHistoryList: extract and set _output_model_schema from generic type parameter
				if actual_class.__name__ == 'AgentHistoryList' and generic_args:
					output_model_schema = generic_args[0]
					# Only set if it's an actual model class, not a TypeVar
					if inspect.isclass(output_model_schema) and hasattr(output_model_schema, 'model_validate_json'):
						result._output_model_schema = output_model_schema

				return result
			# Fallback if model_fields not available
			return actual_class.model_construct(**data)

		# Handle Pydantic v1 - use construct to skip validation and recursively parse nested fields
		if hasattr(annotation, 'construct'):
			if not isinstance(data, dict):
				return data
			# Recursively parse each field if __fields__ is available
			if hasattr(annotation, '__fields__'):
				parsed_fields = {}
				for field_name, field_obj in annotation.__fields__.items():
					if field_name in data:
						field_annotation = field_obj.outer_type_
						parsed_fields[field_name] = _parse_with_type_annotation(data[field_name], field_annotation)
				return annotation.construct(**parsed_fields)
			# Fallback if __fields__ not available
			return annotation.construct(**data)

		# Handle dataclasses
		if dataclasses.is_dataclass(annotation) and isinstance(data, dict):
			# Get field type annotations
			field_types = {f.name: f.type for f in dataclasses.fields(annotation)}
			# Recursively parse each field
			parsed_fields = {}
			for field_name, field_type in field_types.items():
				if field_name in data:
					parsed_fields[field_name] = _parse_with_type_annotation(data[field_name], field_type)
			return cast(type[Any], annotation)(**parsed_fields)

		# Handle regular classes
		if inspect.isclass(annotation) and isinstance(data, dict):
			try:
				return annotation(**data)
			except Exception:
				pass

		return data

	except Exception:
		return data
