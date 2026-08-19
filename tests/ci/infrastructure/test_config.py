"""Tests for lazy loading configuration system."""

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from browser_use.browser.profile import CHROME_DOCKER_ARGS, BrowserProfile
from browser_use.config import get_environment_config, load_browser_use_config, load_config_file


class TestEnvironmentConfig:
	"""Test that each environment configuration read sees current variables."""

	def test_config_reads_env_vars_lazily(self):
		"""Test that a fresh configuration reads current environment variables."""
		# Set an env var
		original_value = os.environ.get('BROWSER_USE_LOGGING_LEVEL', '')
		try:
			os.environ['BROWSER_USE_LOGGING_LEVEL'] = 'debug'
			assert get_environment_config().logging_level == 'debug'

			# Change the env var
			os.environ['BROWSER_USE_LOGGING_LEVEL'] = 'info'
			assert get_environment_config().logging_level == 'info'

			# Delete the env var to test default
			del os.environ['BROWSER_USE_LOGGING_LEVEL']
			assert get_environment_config().logging_level == 'info'  # default value
		finally:
			# Restore original value
			if original_value:
				os.environ['BROWSER_USE_LOGGING_LEVEL'] = original_value
			else:
				os.environ.pop('BROWSER_USE_LOGGING_LEVEL', None)

	def test_api_keys_lazy_loading(self):
		"""Test API keys are loaded lazily."""
		original_value = os.environ.get('OPENAI_API_KEY', '')
		try:
			# Test empty default
			os.environ.pop('OPENAI_API_KEY', None)
			assert get_environment_config().OPENAI_API_KEY == ''

			# Set a value
			os.environ['OPENAI_API_KEY'] = 'test-key-123'
			assert get_environment_config().OPENAI_API_KEY == 'test-key-123'

			# Change the value
			os.environ['OPENAI_API_KEY'] = 'new-key-456'
			assert get_environment_config().OPENAI_API_KEY == 'new-key-456'
		finally:
			if original_value:
				os.environ['OPENAI_API_KEY'] = original_value
			else:
				os.environ.pop('OPENAI_API_KEY', None)

	def test_path_configuration(self):
		"""Test path configuration variables."""
		original_value = os.environ.get('XDG_CACHE_HOME', '')
		try:
			# Test custom path
			test_path = '/tmp/test-cache'
			os.environ['XDG_CACHE_HOME'] = test_path
			# Use Path().resolve() to handle symlinks (e.g., /tmp -> /private/tmp on macOS)
			from pathlib import Path

			assert get_environment_config().cache_home == Path(test_path).resolve()

			# Test default path expansion
			os.environ.pop('XDG_CACHE_HOME', None)
			assert '/.cache' in str(get_environment_config().cache_home)
		finally:
			if original_value:
				os.environ['XDG_CACHE_HOME'] = original_value
			else:
				os.environ.pop('XDG_CACHE_HOME', None)

	def test_in_docker_is_explicit_and_defaults_to_false(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
		monkeypatch.delenv('IN_DOCKER', raising=False)
		assert get_environment_config().IN_DOCKER is False
		assert BrowserProfile(enable_default_extensions=False).chromium_sandbox is True

		monkeypatch.setenv('IN_DOCKER', 'true')
		assert get_environment_config().IN_DOCKER is True
		docker_profile = BrowserProfile(
			enable_default_extensions=False,
			user_data_dir=tmp_path / 'docker-profile',
		)
		assert docker_profile.chromium_sandbox is False
		assert set(CHROME_DOCKER_ARGS) <= set(docker_profile.get_args())

		monkeypatch.setenv('IN_DOCKER', 'false')
		assert get_environment_config().IN_DOCKER is False
		local_profile = BrowserProfile(
			enable_default_extensions=False,
			user_data_dir=tmp_path / 'local-profile',
		)
		assert local_profile.chromium_sandbox is True
		assert '--no-sandbox' not in local_profile.get_args()


def test_missing_config_creates_current_default(tmp_path: Path):
	config_path = tmp_path / 'config.json'

	loaded = load_config_file(config_path)

	assert config_path.exists()
	assert set(loaded.model_dump()) == {'browser_profile', 'llm', 'agent'}
	assert load_config_file(config_path).model_dump() == loaded.model_dump()


def test_valid_current_config_loads_without_rewriting(tmp_path: Path):
	config_path = tmp_path / 'config.json'
	config_path.write_text(
		json.dumps({'browser_profile': {}, 'llm': {}, 'agent': {}}, indent=2) + '\n',
		encoding='utf-8',
	)
	before = config_path.read_bytes()

	loaded = load_config_file(config_path)

	assert loaded.model_dump() == {'browser_profile': {}, 'llm': {}, 'agent': {}}
	assert config_path.read_bytes() == before


def test_current_profile_fields_aliases_and_storage_metadata_are_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
	config_path = tmp_path / 'config.json'
	config_path.write_text(
		json.dumps(
			{
				'browser_profile': {
					'profile': {
						'id': 'profile',
						'default': True,
						'created_at': '2026-01-01T00:00:00',
						'use_cloud': True,
						'window_size': {'width': 1440, 'height': 900},
						'browser_binary_path': '/current/browser',
					}
				},
				'llm': {},
				'agent': {},
			}
		),
		encoding='utf-8',
	)

	loaded = load_config_file(config_path)
	assert loaded.browser_profile['profile'].use_cloud is True  # type: ignore[attr-defined]
	monkeypatch.setenv('BROWSER_USE_CONFIG_PATH', str(config_path))
	runtime_profile = load_browser_use_config()['browser_profile']
	assert runtime_profile['window_size'] == {'width': 1440, 'height': 900}
	assert runtime_profile['browser_binary_path'] == '/current/browser'
	assert not {'id', 'default', 'created_at'} & set(runtime_profile)


@pytest.mark.parametrize('unknown_name', ['cloud_browser', 'window_width', 'window_height', 'windwo_size'])
def test_unknown_persisted_profile_fields_fail_without_rewriting(tmp_path: Path, unknown_name: str):
	config_path = tmp_path / 'config.json'
	config_path.write_text(
		json.dumps(
			{
				'browser_profile': {'profile': {'id': 'profile', 'default': True, unknown_name: True}},
				'llm': {},
				'agent': {},
			}
		),
		encoding='utf-8',
	)
	before = config_path.read_bytes()

	with pytest.raises(ValueError, match=rf'Unknown browser profile fields: {unknown_name}'):
		load_config_file(config_path)

	assert config_path.read_bytes() == before


@pytest.mark.parametrize(
	'kwargs',
	[
		{'cloud_browser': True},
		{'window_width': 1440},
		{'window_height': 900},
		{'windwo_size': {'width': 1440, 'height': 900}},
	],
)
def test_browser_profile_rejects_every_unknown_keyword(kwargs: dict[str, object]):
	with pytest.raises(ValidationError, match='Extra inputs are not permitted'):
		BrowserProfile(**kwargs)  # type: ignore[arg-type]


def test_browser_profile_accepts_current_fields_and_aliases():
	profile = BrowserProfile(
		use_cloud=True,
		window_size={'width': 1440, 'height': 900},
		browser_binary_path='/current/browser',  # type: ignore[call-arg]
	)

	assert profile.use_cloud is True
	assert profile.window_size is not None
	assert profile.window_size.model_dump() == {'width': 1440, 'height': 900}
	assert str(profile.executable_path) == '/current/browser'


@pytest.mark.parametrize(
	'contents',
	[
		'{not-json',
		json.dumps({'profiles': {}}),
	],
)
def test_invalid_or_old_config_fails_without_replacement(tmp_path: Path, contents: str):
	config_path = tmp_path / 'config.json'
	config_path.write_text(contents, encoding='utf-8')
	before = config_path.read_bytes()

	with pytest.raises(ValueError, match='Failed to load current configuration'):
		load_config_file(config_path)

	assert config_path.read_bytes() == before


def test_mcp_environment_overrides_current_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
	config_path = tmp_path / 'config.json'
	load_config_file(config_path)
	monkeypatch.setenv('BROWSER_USE_CONFIG_PATH', str(config_path))
	monkeypatch.setenv('BROWSER_USE_HEADLESS', 'true')
	monkeypatch.setenv('BROWSER_USE_ALLOWED_DOMAINS', 'example.com, *.example.org')
	monkeypatch.setenv('BROWSER_USE_PROXY_URL', 'http://proxy.test:8080')
	monkeypatch.setenv('OPENAI_API_KEY', 'env-key')
	monkeypatch.setenv('BROWSER_USE_LLM_MODEL', 'env-model')
	monkeypatch.setenv('BROWSER_USE_DISABLE_EXTENSIONS', 'true')

	config = load_browser_use_config()

	assert config['browser_profile']['headless'] is True
	assert config['browser_profile']['allowed_domains'] == ['example.com', '*.example.org']
	assert config['browser_profile']['proxy'] == {'server': 'http://proxy.test:8080'}
	assert config['browser_profile']['enable_default_extensions'] is False
	assert config['llm']['api_key'] == 'env-key'
	assert config['llm']['model'] == 'env-model'
