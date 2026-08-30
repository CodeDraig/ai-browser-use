from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import pyotp
from pydantic import BaseModel

from browser_use.browser import BrowserSession
from browser_use.browser.views import BrowserError
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm.base import BaseChatModel
from browser_use.logging_utils import time_execution_async
from browser_use.security import SensitiveData, is_new_tab_page, matching_sensitive_values
from browser_use.tools.registry.views import ActionRegistry

if TYPE_CHECKING:
	from browser_use.tools.registry.service import Registry

logger = logging.getLogger(__name__)


class ActionExecutor:
	"""Inject action dependencies, substitute secrets, and execute actions."""

	def __init__(self, registry_service: Registry) -> None:
		self.registry_service = registry_service

	@property
	def registry(self) -> ActionRegistry:
		return self.registry_service.registry

	@time_execution_async('--execute_action')
	async def execute_action(
		self,
		action_name: str,
		params: dict,
		browser_session: BrowserSession | None = None,
		page_extraction_llm: BaseChatModel | None = None,
		file_system: FileSystem | None = None,
		sensitive_data: SensitiveData | None = None,
		available_file_paths: list[str] | None = None,
		extraction_schema: dict | None = None,
	) -> Any:
		"""Execute a registered action with simplified parameter handling"""
		if action_name not in self.registry.actions:
			raise ValueError(f'Action {action_name} not found')

		action = self.registry.actions[action_name]
		try:
			# Create the validated Pydantic model
			try:
				validated_params = action.param_model(**params)
			except Exception as e:
				raise ValueError(f'Invalid parameters {params} for action {action_name}: {type(e)}: {e}') from e

			if sensitive_data:
				# Get current URL if browser_session is provided
				current_url = None
				if browser_session and browser_session.agent_focus_target_id:
					try:
						# Get current page info from session_manager
						target = browser_session.session_manager.get_target(browser_session.agent_focus_target_id)
						if target:
							current_url = target.url
					except Exception:
						pass
				validated_params = self._replace_sensitive_data(validated_params, sensitive_data, current_url)

			# Build special context dict
			special_context = {
				'browser_session': browser_session,
				'page_extraction_llm': page_extraction_llm,
				'available_file_paths': available_file_paths,
				'has_sensitive_data': action_name == 'input' and bool(sensitive_data),
				'file_system': file_system,
				'extraction_schema': extraction_schema,
			}

			# Only pass sensitive_data to actions that explicitly need it (input)
			if action_name == 'input':
				special_context['sensitive_data'] = sensitive_data

			# Add CDP-related parameters if browser_session is available
			if browser_session:
				# Add page_url
				try:
					special_context['page_url'] = await browser_session.get_current_page_url()
				except Exception:
					special_context['page_url'] = None

				# Add cdp_client
				special_context['cdp_client'] = browser_session.cdp_client

			# All functions are now normalized to accept kwargs only
			# Call with params and unpacked special context
			try:
				return await action.function(params=validated_params, **special_context)
			except Exception as e:
				raise

		except BrowserError as e:
			# BrowserError can carry structured short/long-term memory for the LLM
			# (e.g. available dropdown options) — let Tools.act format it instead of
			# flattening it into a generic RuntimeError string. Only errors with
			# long_term_memory bypass: handle_browser_error re-raises without it,
			# which would escape Tools.act instead of returning an ActionResult.
			if e.long_term_memory is not None:
				raise
			raise RuntimeError(f'Error executing action {action_name}: {str(e)}') from e
		except ValueError as e:
			# Preserve ValueError messages from validation
			if 'requires browser_session but none provided' in str(e) or 'requires page_extraction_llm but none provided' in str(
				e
			):
				raise RuntimeError(str(e)) from e
			else:
				raise RuntimeError(f'Error executing action {action_name}: {str(e)}') from e
		except TimeoutError as e:
			raise RuntimeError(f'Error executing action {action_name} due to timeout.') from e
		except Exception as e:
			raise RuntimeError(f'Error executing action {action_name}: {str(e)}') from e

	def _log_sensitive_data_usage(self, placeholders_used: set[str], current_url: str | None) -> None:
		"""Log when sensitive data is being used on a page"""
		if placeholders_used:
			url_info = f' on {current_url}' if current_url and not is_new_tab_page(current_url) else ''
			logger.info(f'🔒 Using sensitive data placeholders: {", ".join(sorted(placeholders_used))}{url_info}')

	def _replace_sensitive_data(
		self, params: BaseModel, sensitive_data: SensitiveData, current_url: str | None = None
	) -> BaseModel:
		"""
		Replaces sensitive data placeholders in params with actual values.

		Args:
			params: The parameter object containing <secret>placeholder</secret> tags
			sensitive_data: Domain pattern to placeholder/value mappings.
			current_url: Optional current URL for domain matching

		Returns:
			BaseModel: The parameter object with placeholders replaced by actual values
		"""
		secret_pattern = re.compile(r'<secret>(.*?)</secret>')

		# Set to track all missing placeholders across the full object
		all_missing_placeholders = set()
		# Set to track successfully replaced placeholders
		replaced_placeholders = set()

		applicable_secrets = matching_sensitive_values(sensitive_data, current_url)

		def recursively_replace_secrets(value: str | dict | list) -> str | dict | list:
			if isinstance(value, str):
				# 1. Handle tagged secrets: <secret>label</secret>
				matches = secret_pattern.findall(value)
				for placeholder in matches:
					if placeholder in applicable_secrets:
						# generate a totp code if secret is suffixed with bu_2fa_code
						if placeholder.endswith('bu_2fa_code'):
							totp = pyotp.TOTP(applicable_secrets[placeholder], digits=6)
							replacement_value = totp.now()
						else:
							replacement_value = applicable_secrets[placeholder]

						value = value.replace(f'<secret>{placeholder}</secret>', replacement_value)
						replaced_placeholders.add(placeholder)
					else:
						# Keep track of missing placeholders
						all_missing_placeholders.add(placeholder)

				# 2. Handle literal secrets: "user_name" (no tags)
				# This handles cases where the LLM forgets to use tags but uses the exact placeholder name
				if value in applicable_secrets:
					placeholder_name = value
					if placeholder_name.endswith('bu_2fa_code'):
						totp = pyotp.TOTP(applicable_secrets[placeholder_name], digits=6)
						value = totp.now()
					else:
						value = applicable_secrets[placeholder_name]
					replaced_placeholders.add(placeholder_name)

				return value
			elif isinstance(value, dict):
				return {k: recursively_replace_secrets(v) for k, v in value.items()}
			elif isinstance(value, list):
				return [recursively_replace_secrets(v) for v in value]
			return value

		params_dump = params.model_dump()
		processed_params = recursively_replace_secrets(params_dump)

		# Log sensitive data usage
		self._log_sensitive_data_usage(replaced_placeholders, current_url)

		# Log a warning if any placeholders are missing
		if all_missing_placeholders:
			logger.warning(f'Missing or empty keys in sensitive_data dictionary: {", ".join(all_missing_placeholders)}')

		return type(params).model_validate(processed_params)

	# @time_execution_sync('--create_action_model')
