import ast
import importlib
import importlib.util
import inspect
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_removed_agent_skills_and_browser_close_examples_do_not_return():
	"""Python documentation must use the retained Agent and Browser facades."""
	documentation_roots = (
		REPOSITORY_ROOT / 'skills' / 'open-source' / 'references',
		REPOSITORY_ROOT / 'skills' / 'cloud' / 'references',
	)

	for root in documentation_roots:
		for path in root.rglob('*.md'):
			for python_block in re.findall(r'```python[^\n]*\n(.*?)```', path.read_text(encoding='utf-8'), re.DOTALL):
				assert 'await browser.close()' not in python_block, path
				if 'Agent(' in python_block:
					assert re.search(r'\bskills\s*=', python_block) is None, path

	for path in (REPOSITORY_ROOT / 'examples').rglob('*.py'):
		tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
		browser_names: set[str] = set()
		for node in ast.walk(tree):
			if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
				call_name = getattr(node.value.func, 'id', None)
				if call_name in {'Browser', 'BrowserSession'}:
					browser_names.update(target.id for target in node.targets if isinstance(target, ast.Name))
			elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and isinstance(node.value, ast.Call):
				if getattr(node.value.func, 'id', None) in {'Browser', 'BrowserSession'}:
					browser_names.add(node.target.id)

		for node in ast.walk(tree):
			if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'Agent':
				assert all(keyword.arg != 'skills' for keyword in node.keywords), path
			if (
				isinstance(node, ast.Call)
				and isinstance(node.func, ast.Attribute)
				and node.func.attr == 'close'
				and isinstance(node.func.value, ast.Name)
				and node.func.value.id in browser_names
			):
				raise AssertionError(path)


def test_removed_tracking_surfaces_do_not_return():
	assert not (REPOSITORY_ROOT / 'browser_use' / 'telemetry').exists()
	assert not (REPOSITORY_ROOT / 'browser_use' / 'observability.py').exists()
	assert not (REPOSITORY_ROOT / 'browser_use' / 'agent' / 'cloud_events.py').exists()
	assert not (REPOSITORY_ROOT / 'browser_use' / 'sync' / 'service.py').exists()
	assert not (REPOSITORY_ROOT / 'vendor' / 'browser-harness' / 'src' / 'browser_harness' / 'telemetry.py').exists()

	scanned = [
		REPOSITORY_ROOT / 'browser_use',
		REPOSITORY_ROOT / 'vendor' / 'browser-harness',
		REPOSITORY_ROOT / 'pyproject.toml',
		REPOSITORY_ROOT / '.github' / 'workflows',
	]
	for root in scanned:
		paths = root.rglob('*') if root.is_dir() else [root]
		for path in paths:
			if path.is_file() and path.suffix in {'.py', '.toml', '.yaml', '.yml'}:
				content = path.read_text(encoding='utf-8')
				assert 'lmnr' not in content.lower(), path
				assert 'ANONYMIZED_TELEMETRY' not in content, path
				assert 'BROWSER_USE_CLOUD_SYNC' not in content, path
				assert 'from browser_use.telemetry' not in content, path
				assert 'browser_use.observability' not in content, path
				assert '@observe' not in content, path
				assert 'telemetry.capture' not in content, path

	documentation_and_examples = [
		REPOSITORY_ROOT / '.env.example',
		REPOSITORY_ROOT / 'AGENTS.md',
		REPOSITORY_ROOT / 'browser_use' / 'mcp' / '.dxtignore',
		REPOSITORY_ROOT / 'examples',
		REPOSITORY_ROOT / 'skills',
	]
	for root in documentation_and_examples:
		paths = root.rglob('*') if root.is_dir() else [root]
		for path in paths:
			if not path.is_file() or (
				path.suffix not in {'.py', '.md', '.mdx'} and path.name not in {'.env.example', '.dxtignore'}
			):
				continue
			content = path.read_text(encoding='utf-8').lower()
			for forbidden in ('laminar', 'lmnr', 'telemetry', 'observability', 'browser_use_cloud_sync'):
				assert forbidden not in content, path


def test_repository_has_no_github_actions_workflows():
	workflows_dir = REPOSITORY_ROOT / '.github' / 'workflows'
	workflow_files = [path for path in workflows_dir.rglob('*') if path.is_file()] if workflows_dir.exists() else []
	assert workflow_files == []


def test_generic_utils_and_flat_sensitive_data_contract_are_absent():
	assert not (REPOSITORY_ROOT / 'browser_use' / 'utils.py').exists()
	assert importlib.util.find_spec('browser_use.utils') is None

	for root in (REPOSITORY_ROOT / 'browser_use', REPOSITORY_ROOT / 'examples', REPOSITORY_ROOT / 'skills'):
		for path in root.rglob('*'):
			if not path.is_file() or path.suffix not in {'.py', '.md', '.mdx'}:
				continue
			content = path.read_text(encoding='utf-8')
			assert 'browser_use.utils' not in content, path
			assert 'dict[str, str | dict[str, str]]' not in content, path
			assert 'global_placeholders' not in content, path

	dead_definitions = (
		'check_env_variables',
		'is_unsafe_pattern',
		'merge_dicts',
		'singleton',
		'_get_openai_bad_request_error',
		'_get_groq_bad_request_error',
	)
	python_source = '\n'.join(path.read_text(encoding='utf-8') for path in (REPOSITORY_ROOT / 'browser_use').rglob('*.py'))
	for name in dead_definitions:
		assert f'def {name}(' not in python_source


def test_removed_alternate_agent_surface_is_absent():
	removed_package = 'be' + 'ta'
	removed_example = removed_package + '_agent'
	removed_test = 'test_' + removed_example + '.py'
	removed_document = removed_package.upper() + '_AGENT_INTEGRATION_FEATURES.md'
	assert not (REPOSITORY_ROOT / 'browser_use' / removed_package).exists()
	assert not (REPOSITORY_ROOT / 'examples' / removed_example).exists()
	assert not (REPOSITORY_ROOT / 'tests' / 'ci' / removed_test).exists()
	assert not (REPOSITORY_ROOT / removed_document).exists()

	removed_import = 'browser_use.' + removed_package
	removed_error = removed_package.title() + 'AgentError'
	assert importlib.util.find_spec(removed_import) is None
	agent_package = importlib.import_module('browser_use.agent')
	assert not hasattr(agent_package, removed_error)
	for root in (REPOSITORY_ROOT / 'browser_use', REPOSITORY_ROOT / 'examples', REPOSITORY_ROOT / 'tests'):
		for path in root.rglob('*.py'):
			content = path.read_text(encoding='utf-8')
			assert removed_import not in content, path
			assert removed_error not in content, path


def test_legacy_tools_surfaces_are_absent():
	assert not (REPOSITORY_ROOT / 'browser_use' / 'controller').exists()
	assert not (REPOSITORY_ROOT / 'browser_use' / 'tools' / 'default_actions.py').exists()

	browser_use = importlib.import_module('browser_use')
	tool_views = importlib.import_module('browser_use.tools.views')
	tools_service = importlib.import_module('browser_use.tools.service')

	assert not hasattr(browser_use, 'Controller')
	assert not hasattr(tool_views, 'SearchGoogleAction')
	assert not hasattr(tool_views, 'GoToUrlAction')
	assert '__getattr__' not in tools_service.Tools.__dict__
	assert not hasattr(tools_service.Tools(), 'navigate')

	removed_action_module = 'browser_use.tools.' + 'default_actions'
	for path in (REPOSITORY_ROOT / 'browser_use').rglob('*.py'):
		assert removed_action_module not in path.read_text(encoding='utf-8'), path

	extraction_source = (REPOSITORY_ROOT / 'browser_use' / 'tools' / 'actions' / 'extraction.py').read_text(encoding='utf-8')
	assert 'isinstance(params, dict)' not in extraction_source


def test_default_action_protocol_order_is_stable():
	from browser_use.tools.service import Tools

	assert list(Tools().registry.registry.actions) == [
		'done',
		'search',
		'navigate',
		'go_back',
		'wait',
		'click',
		'input',
		'upload_file',
		'switch',
		'close',
		'extract',
		'search_page',
		'find_elements',
		'scroll',
		'send_keys',
		'find_text',
		'screenshot',
		'save_as_pdf',
		'dropdown_options',
		'select_dropdown',
		'write_file',
		'replace_file',
		'read_file',
		'evaluate',
	]


def test_default_action_adapters_do_not_mirror_actor_interactors():
	from browser_use.browser.watchdogs.click_actions import ClickActions
	from browser_use.browser.watchdogs.dropdown_actions import DropdownActions
	from browser_use.browser.watchdogs.keyboard_actions import KeyboardActions
	from browser_use.browser.watchdogs.scroll_actions import ScrollActions
	from browser_use.browser.watchdogs.text_input_actions import TextInputActions

	assert not {'is_element_occluded', 'click_element', '_click_on_coordinate'} & set(ClickActions.__dict__)
	assert not {'type_to_page', 'character_key_info', 'key_code_for_character'} & set(KeyboardActions.__dict__)
	assert '_input_text_element_node_impl' not in TextInputActions.__dict__
	assert not {'_scroll_with_cdp_gesture', '_scroll_element_container'} & set(ScrollActions.__dict__)
	assert '_handle_aria_combobox_options' not in DropdownActions.__dict__


def test_mcp_server_does_not_mirror_operation_owners():
	from browser_use.mcp.browser_operations import McpBrowserOperations
	from browser_use.mcp.server import BrowserUseServer

	forwarding_names = {
		'_init_browser_session',
		'_retry_with_browser_use_agent',
		'_navigate',
		'_click',
		'_type_text',
		'_get_browser_state',
		'_get_html',
		'_screenshot',
		'_extract_content',
		'_scroll',
		'_go_back',
		'_close_browser',
		'_list_tabs',
		'_switch_tab',
		'_close_tab',
		'_track_session',
		'_update_session_activity',
		'_list_sessions',
		'_close_session',
		'_close_all_sessions',
		'_cleanup_expired_sessions',
		'_start_cleanup_task',
	}
	assert not forwarding_names & set(BrowserUseServer.__dict__)
	assert '_retry_with_browser_use_agent' not in McpBrowserOperations.__dict__
	assert '_close_browser' not in McpBrowserOperations.__dict__


def test_token_cost_does_not_mirror_private_owner_methods():
	from browser_use.tokens.service import TokenCost

	private_mirrors = {
		'_pricing_model_names',
		'_pricing_data',
		'_initialized',
		'_cache_dir',
		'_load_pricing_data',
		'_find_valid_cache',
		'_get_cache_status',
		'_load_from_cache',
		'_fetch_and_cache_pricing_data',
		'_log_usage',
		'_build_input_tokens_display',
		'_get_pricing_model_name',
		'_format_tokens',
	}
	assert not private_mirrors & set(TokenCost.__dict__)


def test_removed_constructor_aliases_and_wrappers_are_absent():
	import pytest
	from pydantic import ValidationError

	from browser_use.actor.page import Page
	from browser_use.agent.construction import AgentConstruction
	from browser_use.agent.execution import AgentExecution
	from browser_use.agent.history import AgentHistoryList
	from browser_use.agent.history_replay import AgentHistoryReplay
	from browser_use.agent.model_interaction import AgentModelInteraction
	from browser_use.agent.model_settings import AgentModelSettings
	from browser_use.agent.results import ActionResult, AgentOutput
	from browser_use.agent.service import Agent
	from browser_use.agent.state_restoration import AgentStateRestoration
	from browser_use.browser.profile import BrowserProfile
	from browser_use.browser.session import BrowserSession
	from browser_use.browser.watchdogs.security_watchdog import SecurityWatchdog
	from browser_use.llm.base import BaseChatModel

	assert not (REPOSITORY_ROOT / 'browser_use' / 'agent' / 'views.py').exists()
	assert not (REPOSITORY_ROOT / 'browser_use' / 'agent' / 'configuration.py').exists()
	assert not (REPOSITORY_ROOT / 'browser_use' / 'dom' / 'views.py').exists()

	assert '_is_ip_address' not in SecurityWatchdog.__dict__

	assert not {'browser_session', 'controller', 'skill_ids', 'skills', 'skill_service'} & set(
		inspect.signature(Agent).parameters
	)
	for method in (
		'run',
		'run_sync',
		'add_new_task',
		'pause',
		'resume',
		'stop',
		'close',
		'save_history',
		'rerun_history',
		'load_and_rerun',
		'detect_variables',
	):
		assert hasattr(Agent, method)
	for method in ('step', 'take_step', 'multi_act', 'save_file_system_state', 'log_completion'):
		assert not hasattr(Agent, method)
	assert Agent.__module__ == 'browser_use.agent.service'
	assert not hasattr(__import__('browser_use.agent.service', fromlist=['_PythonAgent']), '_PythonAgent')
	assert all(
		component.__module__.startswith('browser_use.agent.')
		for component in (
			AgentConstruction,
			AgentModelSettings,
			AgentStateRestoration,
			AgentModelInteraction,
			AgentExecution,
			AgentHistoryReplay,
		)
	)
	assert not {
		'profile_id',
		'proxy_country_code',
		'timeout',
		'cloud_browser',
		'window_width',
		'window_height',
	} & set(inspect.signature(BrowserSession).parameters)
	assert not {'window_width', 'window_height', 'cloud_browser'} & set(BrowserProfile.model_fields)
	for kwargs in (
		{'cloud_browser': True},
		{'window_width': 1440},
		{'window_height': 900},
		{'windwo_size': {'width': 1440, 'height': 900}},
	):
		with pytest.raises(ValidationError, match='Extra inputs are not permitted'):
			BrowserProfile.model_validate(kwargs)
	assert not hasattr(ActionResult, 'include_in_memory')
	assert not hasattr(BaseChatModel, 'model_name')
	assert not hasattr(AgentOutput, 'current_state')
	assert not hasattr(AgentHistoryList, 'model_thoughts')
	assert not hasattr(Page, 'navigate')

	from browser_use.actor import utils as actor_utils

	assert not hasattr(actor_utils, 'get_key_info')

	from browser_use.llm import BaseChatModel as exported_base
	from browser_use.llm import models

	assert exported_base is BaseChatModel
	assert models is not None
	for name in (
		'BaseMessage',
		'UserMessage',
		'SystemMessage',
		'AssistantMessage',
		'ContentText',
		'ContentImage',
		'ContentRefusal',
	):
		assert not hasattr(importlib.import_module('browser_use.llm'), name)

	with pytest.raises(ImportError):
		exec('from browser_use.llm import BaseMessage', {})
	from browser_use.llm.messages import BaseMessage

	assert BaseMessage is not None


def test_browser_session_facade_contract_and_component_ownership():
	from browser_use.browser import session as session_module
	from browser_use.browser.event_bus import ResilientEventBus
	from browser_use.browser.focus_recovery import FocusRecovery
	from browser_use.browser.frame_resolver import FrameResolver
	from browser_use.browser.lifecycle import BrowserLifecycle
	from browser_use.browser.lifecycle_monitor import LifecycleMonitor
	from browser_use.browser.navigation import BrowserNavigation
	from browser_use.browser.navigation_policy import NavigationPolicy
	from browser_use.browser.session import BrowserSession
	from browser_use.browser.session_manager import CDPSession, SessionManager, Target
	from browser_use.browser.watchdogs.download_tracker import DownloadTracker
	from browser_use.browser.watchdogs.downloads_watchdog import DownloadsWatchdog
	from browser_use.browser.watchdogs.network_downloads import NetworkDownloadMonitor
	from browser_use.browser.watchdogs.registry import WatchdogRegistry
	from browser_use.dom.browser_state import BrowserDomState

	retained = {
		'start',
		'stop',
		'kill',
		'reset',
		'connect',
		'reconnect',
		'new_page',
		'get_current_page',
		'get_tabs',
		'navigate_to',
		'get_browser_state_summary',
		'get_or_create_cdp_session',
		'take_screenshot',
	}
	assert all(hasattr(BrowserSession, name) for name in retained)

	removed_or_moved = {
		'close',
		'get_state_as_text',
		'get_current_target_info',
		'get_target_id_from_tab_id',
		'get_target_id_from_url',
		'get_most_recently_opened_target_id',
		'_cdp_get_all_pages',
		'_cdp_grant_permissions',
		'get_all_frames',
		'find_frame_target',
		'cdp_client_for_target',
		'cdp_client_for_frame',
		'cdp_client_for_node',
		'get_dom_element_by_index',
		'get_selector_map',
		'get_index_by_class',
		'is_file_input',
		'add_highlights',
		'remove_highlights',
		'screenshot_element',
		'_get_element_bounds',
		'on_NavigateToUrlEvent',
		'_navigate_and_wait',
		'_get_navigation_event_url',
		'_get_committed_navigation_url',
		'_setup_proxy_auth',
		'_attach_ws_drop_callback',
		'_auto_reconnect',
		'on_BrowserStartEvent',
		'on_BrowserStopEvent',
		'_finalize_session_artifacts',
	}
	assert not any(hasattr(BrowserSession, name) for name in removed_or_moved)
	assert all(hasattr(SessionManager, name) for name in ('get_all_pages', 'get_target_id_from_tab_id'))
	assert not any(
		hasattr(SessionManager, name)
		for name in ('get_all_frames', 'cdp_client_for_node', 'ensure_valid_focus', 'get_lifecycle_events')
	)
	assert all(hasattr(FrameResolver, name) for name in ('get_all_frames', 'cdp_client_for_node'))
	assert hasattr(FocusRecovery, 'ensure_valid_focus')
	assert hasattr(LifecycleMonitor, 'enable_page_monitoring')
	assert hasattr(NavigationPolicy, 'handle_request_paused')
	assert hasattr(BrowserNavigation, 'on_NavigateToUrlEvent')
	assert hasattr(BrowserLifecycle, 'on_BrowserStartEvent')
	assert not any(
		hasattr(DownloadsWatchdog, name)
		for name in ('attach_to_target', '_setup_network_monitoring', 'download_file_from_url', '_track_download')
	)
	assert hasattr(DownloadTracker, 'attach_to_target')
	assert hasattr(NetworkDownloadMonitor, 'download_file_from_url')
	assert all(
		hasattr(BrowserDomState, name)
		for name in ('get_dom_element_by_index', 'get_selector_map', 'is_file_input', 'add_highlights', 'remove_highlights')
	)
	assert WatchdogRegistry is not None
	assert Target.__module__ == CDPSession.__module__ == 'browser_use.browser.session_manager'
	assert ResilientEventBus.__module__ == 'browser_use.browser.event_bus'
	assert not hasattr(session_module, 'Target')
	assert not hasattr(session_module, 'CDPSession')
	assert not hasattr(session_module, 'ResilientEventBus')


def test_removed_cli_config_and_dom_compatibility_surfaces_are_absent():
	import pytest

	from browser_use.skills.install import _build_parser

	with pytest.raises(SystemExit):
		_build_parser().parse_args(['install', '--force'])

	source = '\n'.join(path.read_text(encoding='utf-8') for path in (REPOSITORY_ROOT / 'browser_use').rglob('*.py'))
	for removed in (
		'_LEGACY_HINTS',
		'_legacy_command',
		'_legacy_migration_message',
		'browser_use_tui_main',
		'OldConfig',
		'FlatEnvConfig',
		'load_and_migrate_config',
		'is_running_in_docker',
		"Path('/.dockerenv')",
		'len(psutil.pids())',
		'\ndef get_key_info(',
		'include_in_memory',
		'model_thoughts',
		"data-browser-use-exclude')",
	):
		assert removed not in source, removed

	assert set(__import__('tomllib').loads((REPOSITORY_ROOT / 'pyproject.toml').read_text())['project']['scripts']) == {
		'browser-use'
	}
