"""Public token accounting facade."""

import os
from datetime import datetime

from dotenv import load_dotenv

from browser_use.llm.base import BaseChatModel
from browser_use.llm.views import ChatInvokeUsage
from browser_use.tokens.accounting import UsageAccounting
from browser_use.tokens.pricing import PricingService
from browser_use.tokens.views import (
	ModelPricing,
	ModelUsageStats,
	ModelUsageTokens,
	TokenCostCalculated,
	TokenUsageEntry,
	UsageSummary,
)

load_dotenv()


class TokenCost:
	"""Stable facade for token usage tracking and model pricing."""

	CACHE_DIR_NAME = PricingService.CACHE_DIR_NAME
	CACHE_DURATION = PricingService.CACHE_DURATION
	DEFAULT_PRICING_URL = PricingService.DEFAULT_PRICING_URL

	def __init__(self, include_cost: bool = False, pricing_url: str | None = None):
		resolved_include_cost = include_cost or os.getenv('BROWSER_USE_CALCULATE_COST', 'false').lower() == 'true'
		self._pricing = PricingService(resolved_include_cost, pricing_url)
		self._accounting = UsageAccounting(self._pricing)

	@property
	def include_cost(self) -> bool:
		return self._pricing.include_cost

	@property
	def pricing_url(self) -> str:
		return self._pricing.pricing_url

	@property
	def usage_history(self) -> list[TokenUsageEntry]:
		return self._accounting.usage_history

	@usage_history.setter
	def usage_history(self, value: list[TokenUsageEntry]) -> None:
		self._accounting.usage_history = value

	@property
	def registered_llms(self) -> dict[str, BaseChatModel]:
		return self._accounting.registered_llms

	async def initialize(self) -> None:
		await self._pricing.initialize()

	async def get_model_pricing(self, model_name: str) -> ModelPricing | None:
		return await self._pricing.get_model_pricing(model_name)

	async def calculate_cost(self, model: str, usage: ChatInvokeUsage) -> TokenCostCalculated | None:
		return await self._pricing.calculate_cost(model, usage)

	def add_usage(self, model: str, usage: ChatInvokeUsage) -> TokenUsageEntry:
		return self._accounting.add_usage(model, usage)

	def register_llm(self, llm: BaseChatModel) -> BaseChatModel:
		return self._accounting.register_llm(llm)

	def get_usage_tokens_for_model(self, model: str) -> ModelUsageTokens:
		return self._accounting.get_usage_tokens_for_model(model)

	async def get_usage_summary(self, model: str | None = None, since: datetime | None = None) -> UsageSummary:
		return await self._accounting.get_usage_summary(model, since)

	async def log_usage_summary(self) -> None:
		await self._accounting.log_usage_summary()

	async def get_cost_by_model(self) -> dict[str, ModelUsageStats]:
		return await self._accounting.get_cost_by_model()

	def clear_history(self) -> None:
		self._accounting.clear_history()

	async def refresh_pricing_data(self) -> None:
		await self._pricing.refresh()

	async def clean_old_caches(self, keep_count: int = 3) -> None:
		await self._pricing.clean_old_caches(keep_count)

	async def ensure_pricing_loaded(self) -> None:
		await self._pricing.ensure_loaded()
