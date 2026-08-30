import asyncio
import base64
import inspect
import json
import os
import sys
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast

import cloudpickle
import httpx

from browser_use.sandbox.serialization import _extract_all_params, _parse_with_type_annotation
from browser_use.sandbox.source_inspection import _get_function_source_without_decorator, _get_imports_used_in_function
from browser_use.sandbox.views import (
	BrowserCreatedData,
	ErrorData,
	LogData,
	ResultData,
	SandboxError,
	SSEEvent,
	SSEEventType,
)

if TYPE_CHECKING:
	from browser_use.browser import BrowserSession

T = TypeVar('T')
P = ParamSpec('P')


def get_terminal_width() -> int:
	"""Get terminal width, default to 80 if unable to detect"""
	try:
		return os.get_terminal_size().columns
	except (AttributeError, OSError):
		return 80


async def _call_callback(callback: Callable[..., Any], *args: Any) -> None:
	"""Call a callback that can be either sync or async"""
	result = callback(*args)
	if asyncio.iscoroutine(result):
		await result


def sandbox(
	BROWSER_USE_API_KEY: str | None = None,
	cloud_profile_id: str | None = None,
	cloud_proxy_country_code: str | None = None,
	cloud_timeout: int | None = None,
	server_url: str | None = None,
	log_level: str = 'INFO',
	quiet: bool = False,
	headers: dict[str, str] | None = None,
	on_browser_created: Callable[[BrowserCreatedData], None]
	| Callable[[BrowserCreatedData], Coroutine[Any, Any, None]]
	| None = None,
	on_instance_ready: Callable[[], None] | Callable[[], Coroutine[Any, Any, None]] | None = None,
	on_log: Callable[[LogData], None] | Callable[[LogData], Coroutine[Any, Any, None]] | None = None,
	on_result: Callable[[ResultData], None] | Callable[[ResultData], Coroutine[Any, Any, None]] | None = None,
	on_error: Callable[[ErrorData], None] | Callable[[ErrorData], Coroutine[Any, Any, None]] | None = None,
	**env_vars: str,
) -> Callable[[Callable[Concatenate['BrowserSession', P], Coroutine[Any, Any, T]]], Callable[P, Coroutine[Any, Any, T]]]:
	"""Decorator to execute browser automation code in a sandbox environment.

	The decorated function MUST have 'browser: Browser' as its first parameter.
	The browser parameter will be automatically injected - do NOT pass it when calling the decorated function.
	All other parameters (explicit or from closure) will be captured and sent via cloudpickle.

	Args:
	    BROWSER_USE_API_KEY: API key (defaults to BROWSER_USE_API_KEY env var)
	    cloud_profile_id: The ID of the profile to use for the browser session
	    cloud_proxy_country_code: Country code for proxy location (e.g., 'us', 'uk', 'fr')
	    cloud_timeout: The timeout for the browser session in minutes (max 240 = 4 hours)
	    server_url: Sandbox server URL (defaults to https://sandbox.api.browser-use.com/sandbox-stream)
	    log_level: Logging level (INFO, DEBUG, WARNING, ERROR)
	    quiet: Suppress console output
	    headers: Additional HTTP headers to send with the request
	    on_browser_created: Callback when browser is created
	    on_instance_ready: Callback when instance is ready
	    on_log: Callback for log events
	    on_result: Callback when execution completes
	    on_error: Callback for errors
	    **env_vars: Additional environment variables

	Example:
	    @sandbox()
	    async def task(browser: Browser, url: str, max_steps: int) -> str:
	        agent = Agent(task=url, browser=browser)
	        await agent.run(max_steps=max_steps)
	        return "done"

	    # Call with:
	    result = await task(url="https://example.com", max_steps=10)

	    # With cloud parameters:
	    @sandbox(cloud_proxy_country_code='us', cloud_timeout=60)
	    async def task_with_proxy(browser: Browser) -> str:
	        ...
	"""

	def decorator(
		func: Callable[Concatenate['BrowserSession', P], Coroutine[Any, Any, T]],
	) -> Callable[P, Coroutine[Any, Any, T]]:
		# Validate function has browser parameter
		sig = inspect.signature(func)
		if 'browser' not in sig.parameters:
			raise TypeError(f'{func.__name__}() must have a "browser" parameter')

		browser_param = sig.parameters['browser']
		if browser_param.annotation != inspect.Parameter.empty:
			annotation_str = str(browser_param.annotation)
			if 'Browser' not in annotation_str:
				raise TypeError(f'{func.__name__}() browser parameter must be typed as Browser, got {annotation_str}')

		@wraps(func)
		async def wrapper(*args, **kwargs) -> T:
			# 1. Get API key
			api_key = BROWSER_USE_API_KEY or os.getenv('BROWSER_USE_API_KEY')
			if not api_key:
				raise SandboxError('BROWSER_USE_API_KEY is required')

			# 2. Extract all parameters (explicit + closure)
			all_params = _extract_all_params(func, args, kwargs)

			# 3. Get function source without decorator and only needed imports
			func_source = _get_function_source_without_decorator(func)
			needed_imports = _get_imports_used_in_function(func)

			# Always include Browser import since it's required for the function signature
			if needed_imports:
				needed_imports = 'from browser_use import Browser\n' + needed_imports
			else:
				needed_imports = 'from browser_use import Browser'

			# 4. Pickle parameters using cloudpickle for robust serialization
			pickled_params = base64.b64encode(cloudpickle.dumps(all_params)).decode()

			# 5. Determine which params are in the function signature vs closure/globals
			func_param_names = {p.name for p in sig.parameters.values() if p.name != 'browser'}
			non_explicit_params = {k: v for k, v in all_params.items() if k not in func_param_names}
			explicit_params = {k: v for k, v in all_params.items() if k in func_param_names}

			# Inject closure variables and globals as module-level vars
			var_injections = []
			for var_name in non_explicit_params.keys():
				var_injections.append(f"{var_name} = _params['{var_name}']")

			var_injection_code = '\n'.join(var_injections) if var_injections else '# No closure variables or globals'

			# Build function call
			if explicit_params:
				function_call = (
					f'await {func.__name__}(browser=browser, **{{k: _params[k] for k in {list(explicit_params.keys())!r}}})'
				)
			else:
				function_call = f'await {func.__name__}(browser=browser)'

			# 6. Create wrapper code that unpickles params and calls function
			execution_code = f"""import cloudpickle
import base64

# Imports used in function
{needed_imports}

# Unpickle all parameters (explicit, closure, and globals)
_pickled_params = base64.b64decode({repr(pickled_params)})
_params = cloudpickle.loads(_pickled_params)

# Inject closure variables and globals into module scope
{var_injection_code}

# Original function (decorator removed)
{func_source}

# Wrapper function that passes explicit params
async def run(browser):
	return {function_call}

"""

			# 9. Send to server
			payload: dict[str, Any] = {'code': base64.b64encode(execution_code.encode()).decode()}

			combined_env: dict[str, str] = env_vars.copy() if env_vars else {}
			combined_env['LOG_LEVEL'] = log_level.upper()
			payload['env'] = combined_env

			# Add cloud parameters if provided
			if cloud_profile_id is not None:
				payload['cloud_profile_id'] = cloud_profile_id
			if cloud_proxy_country_code is not None:
				payload['cloud_proxy_country_code'] = cloud_proxy_country_code
			if cloud_timeout is not None:
				payload['cloud_timeout'] = cloud_timeout

			url = server_url or 'https://sandbox.api.browser-use.com/sandbox-stream'

			request_headers = {'X-API-Key': api_key}
			if headers:
				request_headers.update(headers)

			# 10. Handle SSE streaming
			_NO_RESULT = object()
			execution_result = _NO_RESULT
			live_url_shown = False
			execution_started = False
			received_final_event = False

			async with httpx.AsyncClient(timeout=1800.0) as client:
				async with client.stream('POST', url, json=payload, headers=request_headers) as response:
					response.raise_for_status()

					try:
						async for line in response.aiter_lines():
							if not line or not line.startswith('data: '):
								continue

							event_json = line[6:]
							try:
								event = SSEEvent.from_json(event_json)

								if event.type == SSEEventType.BROWSER_CREATED:
									assert isinstance(event.data, BrowserCreatedData)

									if on_browser_created:
										try:
											await _call_callback(on_browser_created, event.data)
										except Exception as e:
											if not quiet:
												print(f'⚠️  Error in on_browser_created callback: {e}')

									if not quiet and event.data.live_url and not live_url_shown:
										width = get_terminal_width()
										print('\n' + '━' * width)
										print('👁️  LIVE BROWSER VIEW (Click to watch)')
										print(f'🔗 {event.data.live_url}')
										print('━' * width)
										live_url_shown = True

								elif event.type == SSEEventType.LOG:
									assert isinstance(event.data, LogData)
									message = event.data.message
									level = event.data.level

									if on_log:
										try:
											await _call_callback(on_log, event.data)
										except Exception as e:
											if not quiet:
												print(f'⚠️  Error in on_log callback: {e}')

									if level == 'stdout':
										if not quiet:
											if not execution_started:
												width = get_terminal_width()
												print('\n' + '─' * width)
												print('⚡ Runtime Output')
												print('─' * width)
												execution_started = True
											print(f'  {message}', end='')
									elif level == 'stderr':
										if not quiet:
											if not execution_started:
												width = get_terminal_width()
												print('\n' + '─' * width)
												print('⚡ Runtime Output')
												print('─' * width)
												execution_started = True
											print(f'⚠️  {message}', end='', file=sys.stderr)
									elif level == 'info':
										if not quiet:
											if 'credit' in message.lower():
												import re

												match = re.search(r'\$[\d,]+\.?\d*', message)
												if match:
													print(f'💰 You have {match.group()} credits')
											else:
												print(f'ℹ️  {message}')
									else:
										if not quiet:
											print(f'  {message}')

								elif event.type == SSEEventType.INSTANCE_READY:
									if on_instance_ready:
										try:
											await _call_callback(on_instance_ready)
										except Exception as e:
											if not quiet:
												print(f'⚠️  Error in on_instance_ready callback: {e}')

									if not quiet:
										print('✅ Browser ready, starting execution...\n')

								elif event.type == SSEEventType.RESULT:
									assert isinstance(event.data, ResultData)
									exec_response = event.data.execution_response
									received_final_event = True

									if on_result:
										try:
											await _call_callback(on_result, event.data)
										except Exception as e:
											if not quiet:
												print(f'⚠️  Error in on_result callback: {e}')

									if exec_response.success:
										execution_result = exec_response.result
										if not quiet and execution_started:
											width = get_terminal_width()
											print('\n' + '─' * width)
											print()
									else:
										error_msg = exec_response.error or 'Unknown error'
										raise SandboxError(f'Execution failed: {error_msg}')

								elif event.type == SSEEventType.ERROR:
									assert isinstance(event.data, ErrorData)
									received_final_event = True

									if on_error:
										try:
											await _call_callback(on_error, event.data)
										except Exception as e:
											if not quiet:
												print(f'⚠️  Error in on_error callback: {e}')

									raise SandboxError(f'Execution failed: {event.data.error}')

							except (json.JSONDecodeError, ValueError):
								continue

					except (httpx.RemoteProtocolError, httpx.ReadError, httpx.StreamClosed) as e:
						# With deterministic handshake, these should never happen
						# If they do, it's a real error
						raise SandboxError(
							f'Stream error: {e.__class__.__name__}: {e or "connection closed unexpectedly"}'
						) from e

			# 11. Parse result with type annotation
			if execution_result is not _NO_RESULT:
				return_annotation = func.__annotations__.get('return')
				if return_annotation:
					parsed_result = _parse_with_type_annotation(execution_result, return_annotation)
					return parsed_result
				return execution_result  # type: ignore[return-value]

			raise SandboxError('No result received from execution')

		# Update wrapper signature to remove browser parameter
		wrapper.__annotations__ = func.__annotations__.copy()
		if 'browser' in wrapper.__annotations__:
			del wrapper.__annotations__['browser']

		params = [p for p in sig.parameters.values() if p.name != 'browser']
		wrapper.__signature__ = sig.replace(parameters=params)  # type: ignore[attr-defined]

		return cast(Callable[P, Coroutine[Any, Any, T]], wrapper)

	return decorator
