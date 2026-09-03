"""Current browser-use environment and profile configuration."""

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvironmentConfig(BaseSettings):
	"""Typed view of the current browser-use environment configuration."""

	model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', case_sensitive=True, extra='allow')

	_dirs_created: bool = PrivateAttr(default=False)

	# Logging
	BROWSER_USE_LOGGING_LEVEL: str = Field(default='info')
	CDP_LOGGING_LEVEL: str = Field(default='WARNING')
	BROWSER_USE_DEBUG_LOG_FILE: str | None = Field(default=None)
	BROWSER_USE_INFO_LOG_FILE: str | None = Field(default=None)

	# Path configuration
	XDG_CACHE_HOME: str = Field(default='~/.cache')
	XDG_CONFIG_HOME: str = Field(default='~/.config')
	BROWSER_USE_CONFIG_DIR: str | None = Field(default=None)
	BROWSER_USE_CONFIG_PATH: str | None = Field(default=None)

	# LLM API keys
	OPENAI_API_KEY: str = Field(default='')
	ANTHROPIC_API_KEY: str = Field(default='')
	GOOGLE_API_KEY: str = Field(default='')
	DEEPSEEK_API_KEY: str = Field(default='')
	GROK_API_KEY: str = Field(default='')
	NOVITA_API_KEY: str = Field(default='')
	AZURE_OPENAI_ENDPOINT: str = Field(default='')
	AZURE_OPENAI_KEY: str = Field(default='')
	SKIP_LLM_API_KEY_VERIFICATION: bool = Field(default=False)
	DEFAULT_LLM: str = Field(default='')

	# Runtime hints
	IN_DOCKER: bool = Field(default=False)
	IS_IN_EVALS: bool = Field(default=False)
	WIN_FONT_DIR: str = Field(default='C:\\Windows\\Fonts')

	# MCP-specific environment variables
	BROWSER_USE_HEADLESS: bool | None = Field(default=None)
	BROWSER_USE_ALLOWED_DOMAINS: str | None = Field(default=None)
	BROWSER_USE_LLM_MODEL: str | None = Field(default=None)

	# Proxy environment variables
	BROWSER_USE_PROXY_URL: str | None = Field(default=None)
	BROWSER_USE_NO_PROXY: str | None = Field(default=None)
	BROWSER_USE_PROXY_USERNAME: str | None = Field(default=None)
	BROWSER_USE_PROXY_PASSWORD: str | None = Field(default=None)

	# Extension environment variables
	BROWSER_USE_DISABLE_EXTENSIONS: bool | None = Field(default=None)

	@property
	def logging_level(self) -> str:
		return self.BROWSER_USE_LOGGING_LEVEL.lower()

	@property
	def cache_home(self) -> Path:
		return Path(self.XDG_CACHE_HOME).expanduser().resolve()

	@property
	def config_home(self) -> Path:
		return Path(self.XDG_CONFIG_HOME).expanduser().resolve()

	@property
	def config_dir(self) -> Path:
		path = Path(self.BROWSER_USE_CONFIG_DIR or self.config_home / 'browseruse').expanduser().resolve()
		self._ensure_dirs(path)
		return path

	@property
	def config_path(self) -> Path:
		if self.BROWSER_USE_CONFIG_PATH:
			return Path(self.BROWSER_USE_CONFIG_PATH).expanduser()
		return self.config_dir / 'config.json'

	@property
	def profiles_dir(self) -> Path:
		self._ensure_dirs(self.config_dir)
		return self.config_dir / 'profiles'

	@property
	def default_user_data_dir(self) -> Path:
		return self.profiles_dir / 'default'

	@property
	def extensions_dir(self) -> Path:
		self._ensure_dirs(self.config_dir)
		return self.config_dir / 'extensions'

	def _ensure_dirs(self, config_dir: Path) -> None:
		if self._dirs_created:
			return
		config_dir.mkdir(parents=True, exist_ok=True)
		(config_dir / 'profiles').mkdir(parents=True, exist_ok=True)
		(config_dir / 'extensions').mkdir(parents=True, exist_ok=True)
		self._dirs_created = True


def get_environment_config() -> EnvironmentConfig:
	"""Read a fresh environment configuration."""
	return EnvironmentConfig()


class DBStyleEntry(BaseModel):
	"""Database-style entry with UUID and metadata."""

	id: str = Field(default_factory=lambda: str(uuid4()))
	default: bool = Field(default=False)
	created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class BrowserProfileEntry(DBStyleEntry):
	"""Browser profile configuration entry."""

	model_config = ConfigDict(extra='allow')

	headless: bool | None = None
	user_data_dir: str | None = None
	allowed_domains: list[str] | None = None
	downloads_path: str | None = None

	@model_validator(mode='before')
	@classmethod
	def reject_unknown_profile_fields(cls, value: Any) -> Any:
		if not isinstance(value, Mapping):
			return value

		from browser_use.browser.profile import BrowserProfile

		accepted_names = set(BrowserProfile.model_fields)
		for field in BrowserProfile.model_fields.values():
			validation_alias = field.validation_alias
			if isinstance(validation_alias, str):
				accepted_names.add(validation_alias)
			elif isinstance(validation_alias, AliasChoices):
				accepted_names.update(choice for choice in validation_alias.choices if isinstance(choice, str))

		unknown_names = set(value) - accepted_names - set(DBStyleEntry.model_fields)
		if unknown_names:
			raise ValueError(f'Unknown browser profile fields: {", ".join(sorted(unknown_names))}')
		return value


class LLMEntry(DBStyleEntry):
	"""LLM configuration entry."""

	api_key: str | None = None
	model: str | None = None
	temperature: float | None = None
	max_tokens: int | None = None


class AgentEntry(DBStyleEntry):
	"""Agent configuration entry."""

	max_steps: int | None = None
	use_vision: bool | None = None
	system_prompt: str | None = None


class DBStyleConfigJSON(BaseModel):
	"""Current database-style configuration format."""

	model_config = ConfigDict(extra='forbid')

	browser_profile: dict[str, BrowserProfileEntry]
	llm: dict[str, LLMEntry]
	agent: dict[str, AgentEntry]


def create_default_config() -> DBStyleConfigJSON:
	"""Create a fresh default configuration."""
	new_config = DBStyleConfigJSON(browser_profile={}, llm={}, agent={})

	profile_id = str(uuid4())
	llm_id = str(uuid4())
	agent_id = str(uuid4())

	new_config.browser_profile[profile_id] = BrowserProfileEntry(id=profile_id, default=True, headless=False, user_data_dir=None)
	new_config.llm[llm_id] = LLMEntry(id=llm_id, default=True, model='gpt-4.1-mini', api_key='your-openai-api-key-here')
	new_config.agent[agent_id] = AgentEntry(id=agent_id, default=True)

	return new_config


def load_config_file(config_path: Path | None = None) -> DBStyleConfigJSON:
	"""Load the current configuration, creating it only when absent."""
	if config_path is None:
		config_path = get_environment_config().config_path
	if not config_path.exists():
		config_path.parent.mkdir(parents=True, exist_ok=True)
		new_config = create_default_config()
		with config_path.open('w', encoding='utf-8') as config_file:
			json.dump(new_config.model_dump(), config_file, indent=2)
		return new_config

	try:
		with config_path.open(encoding='utf-8') as config_file:
			data = json.load(config_file)
		return DBStyleConfigJSON.model_validate(data)
	except Exception as exc:
		raise ValueError(f'Failed to load current configuration from {config_path}: {exc}') from exc


def _default_entry(entries: Mapping[str, DBStyleEntry]) -> dict[str, Any]:
	for entry in entries.values():
		if entry.default:
			return entry.model_dump(exclude_none=True, exclude=set(DBStyleEntry.model_fields))
	if entries:
		return next(iter(entries.values())).model_dump(exclude_none=True, exclude=set(DBStyleEntry.model_fields))
	return {}


def load_browser_use_config() -> dict[str, Any]:
	"""Load current configuration with MCP environment overrides."""
	settings = get_environment_config()
	db_config = load_config_file(settings.config_path)
	config: dict[str, Any] = {
		'browser_profile': _default_entry(db_config.browser_profile),
		'llm': _default_entry(db_config.llm),
		'agent': _default_entry(db_config.agent),
	}

	if settings.BROWSER_USE_HEADLESS is not None:
		config['browser_profile']['headless'] = settings.BROWSER_USE_HEADLESS

	if settings.BROWSER_USE_ALLOWED_DOMAINS:
		domains = [domain.strip() for domain in settings.BROWSER_USE_ALLOWED_DOMAINS.split(',') if domain.strip()]
		config['browser_profile']['allowed_domains'] = domains

	proxy: dict[str, Any] = {}
	if settings.BROWSER_USE_PROXY_URL:
		proxy['server'] = settings.BROWSER_USE_PROXY_URL
	if settings.BROWSER_USE_NO_PROXY:
		proxy['bypass'] = ','.join(domain.strip() for domain in settings.BROWSER_USE_NO_PROXY.split(',') if domain.strip())
	if settings.BROWSER_USE_PROXY_USERNAME:
		proxy['username'] = settings.BROWSER_USE_PROXY_USERNAME
	if settings.BROWSER_USE_PROXY_PASSWORD:
		proxy['password'] = settings.BROWSER_USE_PROXY_PASSWORD
	if proxy:
		config['browser_profile']['proxy'] = proxy

	if settings.OPENAI_API_KEY:
		config['llm']['api_key'] = settings.OPENAI_API_KEY
	if settings.BROWSER_USE_LLM_MODEL:
		config['llm']['model'] = settings.BROWSER_USE_LLM_MODEL
	if settings.BROWSER_USE_DISABLE_EXTENSIONS is not None:
		config['browser_profile']['enable_default_extensions'] = not settings.BROWSER_USE_DISABLE_EXTENSIONS

	return config


def get_default_profile(config: dict[str, Any]) -> dict[str, Any]:
	"""Get the default browser profile from a loaded configuration."""
	return config.get('browser_profile', {})


def get_default_llm(config: dict[str, Any]) -> dict[str, Any]:
	"""Get the default LLM configuration from a loaded configuration."""
	return config.get('llm', {})
