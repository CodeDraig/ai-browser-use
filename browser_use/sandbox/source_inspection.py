import ast
import inspect
import textwrap
from collections.abc import Callable
from typing import get_args, get_origin


def _get_function_source_without_decorator(func: Callable) -> str:
	"""Get function source code with decorator removed"""
	source = inspect.getsource(func)
	source = textwrap.dedent(source)

	# Parse and remove decorator
	tree = ast.parse(source)
	for node in ast.walk(tree):
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
			node.decorator_list = []
			break

	return ast.unparse(tree)


def _get_imports_used_in_function(func: Callable) -> str:
	"""Extract only imports that are referenced in the function body or type annotations"""
	# Get all names referenced in the function
	code = func.__code__
	referenced_names = set(code.co_names)

	# Also get names from type annotations (recursively for complex types like Union, Literal, etc.)
	def extract_type_names(annotation):
		"""Recursively extract all type names from annotation"""
		if annotation is None or annotation == inspect.Parameter.empty:
			return

		# Handle Pydantic generics (e.g., AgentHistoryList[MyModel]) - check this FIRST
		# Pydantic generics have __pydantic_generic_metadata__ with 'origin' and 'args'
		pydantic_meta = getattr(annotation, '__pydantic_generic_metadata__', None)
		if pydantic_meta and pydantic_meta.get('origin'):
			# Add the origin class name (e.g., 'AgentHistoryList')
			origin_class = pydantic_meta['origin']
			if hasattr(origin_class, '__name__'):
				referenced_names.add(origin_class.__name__)
			# Recursively extract from generic args (e.g., MyModel)
			for arg in pydantic_meta.get('args', ()):
				extract_type_names(arg)
			return

		# Handle simple types with __name__
		if hasattr(annotation, '__name__'):
			referenced_names.add(annotation.__name__)

		# Handle string annotations
		if isinstance(annotation, str):
			referenced_names.add(annotation)

		# Handle generic types like Union[X, Y], Literal['x'], etc.
		origin = get_origin(annotation)
		args = get_args(annotation)

		if origin:
			# Add the origin type name (e.g., 'Union', 'Literal')
			if hasattr(origin, '__name__'):
				referenced_names.add(origin.__name__)

		# Recursively extract from generic args
		if args:
			for arg in args:
				extract_type_names(arg)

	sig = inspect.signature(func)
	for param in sig.parameters.values():
		if param.annotation != inspect.Parameter.empty:
			extract_type_names(param.annotation)

	# Get return annotation (also extract recursively)
	if 'return' in func.__annotations__:
		extract_type_names(func.__annotations__['return'])

	# Get the module where function is defined
	module = inspect.getmodule(func)
	if not module or not hasattr(module, '__file__') or module.__file__ is None:
		return ''

	try:
		with open(module.__file__) as f:
			module_source = f.read()

		tree = ast.parse(module_source)
		needed_imports: list[str] = []

		for node in tree.body:
			if isinstance(node, ast.Import):
				# import X, Y
				for alias in node.names:
					import_name = alias.asname if alias.asname else alias.name
					if import_name in referenced_names:
						needed_imports.append(ast.unparse(node))
						break
			elif isinstance(node, ast.ImportFrom):
				# from X import Y, Z
				imported_names = []
				for alias in node.names:
					import_name = alias.asname if alias.asname else alias.name
					if import_name in referenced_names:
						imported_names.append(alias)

				if imported_names:
					# Create filtered import statement
					filtered_import = ast.ImportFrom(module=node.module, names=imported_names, level=node.level)
					needed_imports.append(ast.unparse(filtered_import))

		return '\n'.join(needed_imports)
	except Exception:
		return ''
