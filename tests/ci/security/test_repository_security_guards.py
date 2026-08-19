import importlib
import importlib.util
import inspect
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


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


def test_removed_constructor_aliases_and_wrappers_are_absent():
	import pytest
	from pydantic import ValidationError

	from browser_use.actor.page import Page
	from browser_use.agent.service import Agent
	from browser_use.agent.views import ActionResult, AgentHistoryList, AgentOutput
	from browser_use.browser.profile import BrowserProfile
	from browser_use.browser.session import BrowserSession
	from browser_use.llm.base import BaseChatModel

	assert not {'browser_session', 'controller', 'skill_ids'} & set(inspect.signature(Agent).parameters)
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
