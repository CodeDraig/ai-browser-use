import asyncio
import logging
import math
import os
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from pydantic import BaseModel

from browser_use.agent.views import ActionModel, ActionResult
from browser_use.browser import BrowserSession
from browser_use.browser.views import BrowserError
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm.base import BaseChatModel
from browser_use.logging_utils import time_execution_sync
from browser_use.security import SensitiveData
from browser_use.tools.actions.clicks import register_click_action
from browser_use.tools.actions.completion import register_done_action
from browser_use.tools.actions.registration import register_default_actions
from browser_use.tools.errors import handle_browser_error
from browser_use.tools.registry.service import Registry
from browser_use.tools.views import ClickElementAction, ClickElementActionIndexOnly

logger = logging.getLogger(__name__)

Context = TypeVar('Context')
T = TypeVar('T', bound=BaseModel)

# Global per-action timeout: last-resort guard against hung event handlers.
# Individual CDP calls (Page.navigate etc.) have their own shorter timeouts,
# but event-bus `await event` and `event_result()` calls have none — if a
# watchdog handler blocks on a dead CDP WebSocket, the action can hang past
# any agent-level watchdog. This cap ensures every action returns within a
# bounded window with an ActionResult(error=...) instead of hanging silently.
#
# The default (180s) sits above the longest built-in inner timeout — the extract
# action's page_extraction_llm.ainvoke at 120s — plus comfortable grace, so
# slow-but-valid LLM-backed actions aren't truncated. Override per-call via
# BROWSER_USE_ACTION_TIMEOUT_S env var or tools.act(action_timeout=...).
_ACTION_TIMEOUT_FALLBACK_S = 180.0


def _parse_env_action_timeout(raw: str | None) -> float:
	"""Parse BROWSER_USE_ACTION_TIMEOUT_S defensively.

	Accepts only finite positive values. Empty, non-numeric, inf, nan, or
	non-positive values fall back to the hardcoded default with a warning
	— these would otherwise make every action time out immediately (nan)
	or disable the hang guard entirely (inf / negative / zero).
	"""
	if raw is None or raw == '':
		return _ACTION_TIMEOUT_FALLBACK_S
	try:
		parsed = float(raw)
	except ValueError:
		logging.getLogger(__name__).warning(
			'Invalid BROWSER_USE_ACTION_TIMEOUT_S=%r; falling back to %.0fs',
			raw,
			_ACTION_TIMEOUT_FALLBACK_S,
		)
		return _ACTION_TIMEOUT_FALLBACK_S
	if not math.isfinite(parsed) or parsed <= 0:
		logging.getLogger(__name__).warning(
			'BROWSER_USE_ACTION_TIMEOUT_S=%r is not a finite positive number; falling back to %.0fs',
			raw,
			_ACTION_TIMEOUT_FALLBACK_S,
		)
		return _ACTION_TIMEOUT_FALLBACK_S
	return parsed


_DEFAULT_ACTION_TIMEOUT_S = _parse_env_action_timeout(os.getenv('BROWSER_USE_ACTION_TIMEOUT_S'))


def _coerce_valid_action_timeout(value: float | None) -> float:
	"""Normalize a caller-supplied action_timeout to a finite positive value.

	Mirrors the env-var guard so the public `tools.act(action_timeout=...)`
	override path has the same defenses: nan / inf / <=0 make actions either
	time out immediately or never, which would silently defeat the hang
	guard this module exists to provide. Fall back to the env-derived
	default with a warning instead.
	"""
	if value is None:
		return _DEFAULT_ACTION_TIMEOUT_S
	if not math.isfinite(value) or value <= 0:
		logging.getLogger(__name__).warning(
			'action_timeout=%r is not a finite positive number; falling back to %.0fs',
			value,
			_DEFAULT_ACTION_TIMEOUT_S,
		)
		return _DEFAULT_ACTION_TIMEOUT_S
	return float(value)


class Tools(Generic[Context]):
	"""Registry and execution boundary for browser actions."""

	_click_by_index: Callable[[ClickElementAction | ClickElementActionIndexOnly, BrowserSession], Awaitable[ActionResult]]
	_click_by_coordinate: Callable[[ClickElementAction, BrowserSession], Awaitable[ActionResult]]

	def __init__(
		self,
		exclude_actions: list[str] | None = None,
		output_model: type[T] | None = None,
		display_files_in_done_text: bool = True,
	):
		self.registry = Registry[Context](exclude_actions if exclude_actions is not None else [])
		self.display_files_in_done_text = display_files_in_done_text
		self._output_model: type[BaseModel] | None = output_model
		self._coordinate_clicking_enabled = False
		register_default_actions(self, output_model)

	def use_structured_output_action(self, output_model: type[T]):
		self._output_model = output_model
		register_done_action(self, output_model)

	def get_output_model(self) -> type[BaseModel] | None:
		"""Get the output model if structured output is configured."""
		return self._output_model

	# Register ---------------------------------------------------------------

	def action(self, description: str, **kwargs):
		"""Decorator for registering custom actions

		@param description: Describe the LLM what the function does (better description == better function calling)
		"""
		return self.registry.action(description, **kwargs)

	def exclude_action(self, action_name: str) -> None:
		"""Exclude an action from the tools registry.

		This method can be used to remove actions after initialization,
		useful for enforcing constraints like disabling screenshot when use_vision != 'auto'.

		Args:
			action_name: Name of the action to exclude (e.g., 'screenshot')
		"""
		self.registry.exclude_action(action_name)

	def set_coordinate_clicking(self, enabled: bool) -> None:
		"""Enable or disable coordinate-based clicking.

		When enabled, the click action accepts both index and coordinate parameters.
		When disabled (default), only index-based clicking is available.

		This is automatically enabled for models that support coordinate clicking:
		- claude-sonnet-4-5
		- claude-opus-4-5
		- claude-fable-5
		- gemini-3-pro
		- browser-use/* models

		Args:
			enabled: True to enable coordinate clicking, False to disable
		"""
		if enabled == self._coordinate_clicking_enabled:
			return  # No change needed

		self._coordinate_clicking_enabled = enabled
		register_click_action(self)
		logger.debug(f'Coordinate clicking {"enabled" if enabled else "disabled"}')

	# Act --------------------------------------------------------------------
	@time_execution_sync('--act')
	async def act(
		self,
		action: ActionModel,
		browser_session: BrowserSession,
		page_extraction_llm: BaseChatModel | None = None,
		sensitive_data: SensitiveData | None = None,
		available_file_paths: list[str] | None = None,
		file_system: FileSystem | None = None,
		extraction_schema: dict | None = None,
		action_timeout: float | None = None,
	) -> ActionResult:
		"""Execute an action.

		action_timeout: per-action wall-clock cap (seconds). Prevents actions from hanging
		indefinitely when a CDP WebSocket goes silent — a common failure mode with remote
		browsers where internal CDP calls (tab switches, lifecycle waits) have no timeouts.
		Defaults to BROWSER_USE_ACTION_TIMEOUT_S env var or 180s (above the 120s
		page_extraction_llm cap used by the `extract` action).
		"""

		timeout_s = _coerce_valid_action_timeout(action_timeout)

		for action_name, params in action.model_dump(exclude_unset=True).items():
			if params is not None:
				try:
					result = await asyncio.wait_for(
						self.registry.execute_action(
							action_name=action_name,
							params=params,
							browser_session=browser_session,
							page_extraction_llm=page_extraction_llm,
							file_system=file_system,
							sensitive_data=sensitive_data,
							available_file_paths=available_file_paths,
							extraction_schema=extraction_schema,
						),
						timeout=timeout_s,
					)
				except BrowserError as e:
					logger.error(f'❌ Action {action_name} failed with BrowserError: {str(e)}')
					result = handle_browser_error(e)
				except TimeoutError:
					logger.error(
						f'❌ Action {action_name} hit the per-action timeout ({timeout_s:.0f}s) '
						f'— likely an unresponsive CDP connection. Returning error so the agent can recover.'
					)
					result = ActionResult(
						error=(
							f'Action {action_name} timed out after {timeout_s:.0f}s. '
							f'The browser may be unresponsive (dead CDP WebSocket). '
							f'Try again or a different approach.'
						)
					)
				except Exception as e:
					logger.error(f"Action '{action_name}' failed with error: {str(e)}")
					result = ActionResult(error=str(e))

				if isinstance(result, str):
					return ActionResult(extracted_content=result)
				elif isinstance(result, ActionResult):
					return result
				elif result is None:
					return ActionResult()
				else:
					raise ValueError(f'Invalid action result type: {type(result)} of {result}')
		return ActionResult()
