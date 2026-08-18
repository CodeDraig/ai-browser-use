import logging
import time
from collections.abc import Callable, Coroutine
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

R = TypeVar('R')
P = ParamSpec('P')


def _call_logger(args: tuple[Any, ...], kwargs: dict[str, Any]) -> logging.Logger:
	if args and (instance_logger := getattr(args[0], 'logger', None)):
		return instance_logger
	if 'agent' in kwargs:
		return getattr(kwargs['agent'], 'logger')
	if 'browser_session' in kwargs:
		return getattr(kwargs['browser_session'], 'logger')
	return logging.getLogger(__name__)


def _log_slow_call(name: str, started_at: float, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
	execution_time = time.time() - started_at
	if execution_time > 0.25:
		_call_logger(args, kwargs).debug(f'⏳ {name.strip("-")}() took {execution_time:.2f}s')


def time_execution_sync(additional_text: str = '') -> Callable[[Callable[P, R]], Callable[P, R]]:
	def decorator(func: Callable[P, R]) -> Callable[P, R]:
		@wraps(func)
		def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
			started_at = time.time()
			result = func(*args, **kwargs)
			_log_slow_call(additional_text, started_at, args, kwargs)
			return result

		return wrapper

	return decorator


def time_execution_async(
	additional_text: str = '',
) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
	def decorator(func: Callable[P, Coroutine[Any, Any, R]]) -> Callable[P, Coroutine[Any, Any, R]]:
		@wraps(func)
		async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
			started_at = time.time()
			result = await func(*args, **kwargs)
			_log_slow_call(additional_text, started_at, args, kwargs)
			return result

		return wrapper

	return decorator


def log_pretty_path(path: str | Path | None) -> str:
	"""Pretty-print a path, shortening the home directory and current directory."""
	if not path or not str(path).strip():
		return ''
	if not isinstance(path, (str, Path)):
		return f'<{type(path).__name__}>'

	pretty_path = str(path).replace(str(Path.home()), '~').replace(str(Path.cwd().resolve()), '.')
	if pretty_path.strip() and ' ' in pretty_path:
		pretty_path = f'"{pretty_path}"'
	return pretty_path


def log_pretty_url(value: str, max_len: int | None = 22) -> str:
	"""Pretty-print a URL without its web scheme or conventional www prefix."""
	value = value.replace('https://', '').replace('http://', '').replace('www.', '')
	if max_len is not None and len(value) > max_len:
		return value[:max_len] + '…'
	return value
