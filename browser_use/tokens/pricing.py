"""Model pricing retrieval, caching, and cost calculation."""

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx

from browser_use.config import get_environment_config
from browser_use.llm.views import ChatInvokeUsage
from browser_use.tokens.custom_pricing import CUSTOM_MODEL_PRICING
from browser_use.tokens.mappings import MODEL_TO_LITELLM
from browser_use.tokens.openrouter_pricing import get_openrouter_model_pricing, is_openrouter_pricing_model
from browser_use.tokens.views import CachedPricingData, ModelPricing, TokenCostCalculated

logger = logging.getLogger(__name__)


def xdg_cache_home() -> Path:
	return get_environment_config().cache_home


class PricingService:
	"""Own pricing sources, cache state, and cost arithmetic."""

	CACHE_DIR_NAME = 'browser_use/token_cost'
	CACHE_DURATION = timedelta(days=1)
	DEFAULT_PRICING_URL = 'https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json'

	def __init__(self, include_cost: bool, pricing_url: str | None = None) -> None:
		self.include_cost = include_cost
		self.pricing_url = pricing_url or get_environment_config().BROWSER_USE_MODEL_PRICING_URL or self.DEFAULT_PRICING_URL
		self.pricing_model_names: dict[str, str] = {}
		self.pricing_data: dict[str, Any] | None = None
		self.initialized = False
		self.cache_dir = xdg_cache_home() / self.CACHE_DIR_NAME

	async def initialize(self) -> None:
		if not self.initialized:
			if self.include_cost:
				await self.load_pricing_data()
			self.initialized = True

	async def load_pricing_data(self) -> None:
		cache_file = await self.find_valid_cache()
		if cache_file:
			await self.load_from_cache(cache_file)
		else:
			await self.fetch_and_cache_pricing_data()

	async def find_valid_cache(self) -> Path | None:
		try:
			self.cache_dir.mkdir(parents=True, exist_ok=True)
			cache_files = sorted(self.cache_dir.glob('*.json'), key=lambda file: file.stat().st_mtime, reverse=True)
			for cache_file in cache_files:
				is_valid, should_delete = await self.get_cache_status(cache_file)
				if is_valid:
					return cache_file
				if should_delete:
					try:
						os.remove(cache_file)
					except Exception:
						pass
		except Exception:
			pass
		return None

	async def get_cache_status(self, cache_file: Path) -> tuple[bool, bool]:
		try:
			if not cache_file.exists():
				return False, False
			cached = CachedPricingData.model_validate_json(await anyio.Path(cache_file).read_text())
			if datetime.now() - cached.timestamp >= self.CACHE_DURATION:
				return False, True
			return self.cache_source_matches(cached), False
		except Exception:
			return False, True

	def cache_source_matches(self, cached: CachedPricingData) -> bool:
		if cached.source_url is None:
			return self.pricing_url == self.DEFAULT_PRICING_URL
		return cached.source_url == self.pricing_url

	async def load_from_cache(self, cache_file: Path) -> None:
		try:
			cached = CachedPricingData.model_validate_json(await anyio.Path(cache_file).read_text())
			self.pricing_data = cached.data
		except Exception as error:
			logger.debug(f'Error loading cached pricing data from {cache_file}: {error}')
			await self.fetch_and_cache_pricing_data()

	async def fetch_and_cache_pricing_data(self) -> None:
		try:
			async with httpx.AsyncClient() as client:
				response = await client.get(self.pricing_url, timeout=30)
				response.raise_for_status()
				self.pricing_data = response.json()
			cached = CachedPricingData(timestamp=datetime.now(), source_url=self.pricing_url, data=self.pricing_data or {})
			self.cache_dir.mkdir(parents=True, exist_ok=True)
			cache_file = self.cache_dir / f'pricing_{datetime.now():%Y%m%d_%H%M%S}.json'
			await anyio.Path(cache_file).write_text(cached.model_dump_json(indent=2))
		except Exception as error:
			logger.debug(f'Error fetching pricing data: {error}')
			self.pricing_data = {}

	async def get_model_pricing(self, model_name: str) -> ModelPricing | None:
		if model_name in CUSTOM_MODEL_PRICING:
			return self._model_pricing(model_name, CUSTOM_MODEL_PRICING[model_name])
		if not self.initialized:
			await self.initialize()
		if is_openrouter_pricing_model(model_name):
			openrouter_pricing = await get_openrouter_model_pricing(model_name)
			if openrouter_pricing is not None:
				return openrouter_pricing
		litellm_model_name = MODEL_TO_LITELLM.get(model_name, model_name)
		if self.pricing_data and litellm_model_name in self.pricing_data:
			return self._model_pricing(model_name, self.pricing_data[litellm_model_name])
		return await get_openrouter_model_pricing(model_name)

	@staticmethod
	def _model_pricing(model_name: str, data: dict[str, Any]) -> ModelPricing:
		return ModelPricing(
			model=model_name,
			input_cost_per_token=data.get('input_cost_per_token'),
			output_cost_per_token=data.get('output_cost_per_token'),
			max_tokens=data.get('max_tokens'),
			max_input_tokens=data.get('max_input_tokens'),
			max_output_tokens=data.get('max_output_tokens'),
			cache_read_input_token_cost=data.get('cache_read_input_token_cost'),
			cache_creation_input_token_cost=data.get('cache_creation_input_token_cost'),
			cache_creation_1h_input_token_cost=data.get('cache_creation_1h_input_token_cost'),
		)

	async def calculate_cost(self, model: str, usage: ChatInvokeUsage) -> TokenCostCalculated | None:
		if not self.include_cost:
			return None
		data = await self.get_model_pricing(self.pricing_model_names.get(model, model))
		if data is None:
			return None
		uncached_prompt_tokens = usage.prompt_tokens - (usage.prompt_cached_tokens or 0)
		pricing_multiplier = usage.pricing_multiplier or 1.0
		cache_creation_5m_tokens = usage.prompt_cache_creation_5m_tokens
		cache_creation_1h_tokens = usage.prompt_cache_creation_1h_tokens
		if cache_creation_5m_tokens is not None or cache_creation_1h_tokens is not None:
			cache_creation_cost = (cache_creation_5m_tokens or 0) * (data.cache_creation_input_token_cost or 0) + (
				cache_creation_1h_tokens or 0
			) * (data.cache_creation_1h_input_token_cost or data.cache_creation_input_token_cost or 0)
		else:
			cache_creation_cost = (
				usage.prompt_cache_creation_tokens * data.cache_creation_input_token_cost
				if data.cache_creation_input_token_cost and usage.prompt_cache_creation_tokens
				else None
			)
		return TokenCostCalculated(
			new_prompt_tokens=usage.prompt_tokens,
			new_prompt_cost=uncached_prompt_tokens * (data.input_cost_per_token or 0) * pricing_multiplier,
			prompt_read_cached_tokens=usage.prompt_cached_tokens,
			prompt_read_cached_cost=usage.prompt_cached_tokens * data.cache_read_input_token_cost * pricing_multiplier
			if usage.prompt_cached_tokens and data.cache_read_input_token_cost
			else None,
			prompt_cached_creation_tokens=usage.prompt_cache_creation_tokens,
			prompt_cache_creation_cost=cache_creation_cost * pricing_multiplier if cache_creation_cost is not None else None,
			completion_tokens=usage.completion_tokens,
			completion_cost=usage.completion_tokens * float(data.output_cost_per_token or 0) * pricing_multiplier,
		)

	async def refresh(self) -> None:
		if self.include_cost:
			await self.fetch_and_cache_pricing_data()

	async def clean_old_caches(self, keep_count: int = 3) -> None:
		try:
			own_files: list[Path] = []
			for cache_file in self.cache_dir.glob('*.json'):
				try:
					cached = CachedPricingData.model_validate_json(cache_file.read_text())
					if self.cache_source_matches(cached):
						own_files.append(cache_file)
				except Exception:
					pass
			own_files.sort(key=lambda file: file.stat().st_mtime)
			for cache_file in own_files[:-keep_count]:
				try:
					os.remove(cache_file)
				except Exception:
					pass
		except Exception as error:
			logger.debug(f'Error cleaning old cache files: {error}')

	async def ensure_loaded(self) -> None:
		if not self.initialized and self.include_cost:
			await self.initialize()
