"""Token usage collection and reporting."""

import logging
from datetime import datetime

from browser_use.llm.base import BaseChatModel
from browser_use.llm.views import ChatInvokeUsage
from browser_use.runtime import create_task_with_error_handling
from browser_use.tokens.openrouter_pricing import is_openrouter_pricing_model
from browser_use.tokens.pricing import PricingService
from browser_use.tokens.views import ModelUsageStats, ModelUsageTokens, TokenCostCalculated, TokenUsageEntry, UsageSummary

logger = logging.getLogger(__name__)
cost_logger = logging.getLogger('cost')


def format_tokens(tokens: int) -> str:
	if tokens >= 1_000_000_000:
		return f'{tokens / 1_000_000_000:.1f}B'
	if tokens >= 1_000_000:
		return f'{tokens / 1_000_000:.1f}M'
	if tokens >= 1_000:
		return f'{tokens / 1_000:.1f}k'
	return str(tokens)


class UsageAccounting:
	"""Own usage history, LLM instrumentation, and reporting."""

	def __init__(self, pricing: PricingService) -> None:
		self.pricing = pricing
		self.usage_history: list[TokenUsageEntry] = []
		self.registered_llms: dict[str, BaseChatModel] = {}

	def add_usage(self, model: str, usage: ChatInvokeUsage) -> TokenUsageEntry:
		entry = TokenUsageEntry(model=model, timestamp=datetime.now(), usage=usage)
		self.usage_history.append(entry)
		return entry

	async def log_usage(self, model: str, usage: TokenUsageEntry) -> None:
		if not self.pricing.initialized:
			await self.pricing.initialize()
		cost = await self.pricing.calculate_cost(model, usage.usage)
		input_part = self.build_input_tokens_display(usage.usage, cost)
		completion_tokens = format_tokens(usage.usage.completion_tokens)
		if self.pricing.include_cost and cost and cost.completion_cost > 0:
			output_part = f'📤 \033[92m{completion_tokens} (${cost.completion_cost:.4f})\033[0m'
		else:
			output_part = f'📤 \033[92m{completion_tokens}\033[0m'
		cost_logger.debug(f'🧠 \033[96m{model}\033[0m | {input_part} | {output_part}')

	def build_input_tokens_display(self, usage: ChatInvokeUsage, cost: TokenCostCalculated | None) -> str:
		parts: list[str] = []
		if usage.prompt_cached_tokens or usage.prompt_cache_creation_tokens:
			new_tokens = usage.prompt_tokens - (usage.prompt_cached_tokens or 0)
			if new_tokens > 0:
				formatted = format_tokens(new_tokens)
				price = (
					f' (${cost.new_prompt_cost:.4f})' if self.pricing.include_cost and cost and cost.new_prompt_cost > 0 else ''
				)
				parts.append(f'🆕 \033[93m{formatted}{price}\033[0m')
			if usage.prompt_cached_tokens:
				formatted = format_tokens(usage.prompt_cached_tokens)
				price = (
					f' (${cost.prompt_read_cached_cost:.4f})'
					if self.pricing.include_cost and cost and cost.prompt_read_cached_cost
					else ''
				)
				parts.append(f'💾 \033[94m{formatted}{price}\033[0m')
			if usage.prompt_cache_creation_tokens:
				formatted = format_tokens(usage.prompt_cache_creation_tokens)
				price = (
					f' (${cost.prompt_cache_creation_cost:.4f})'
					if self.pricing.include_cost and cost and cost.prompt_cache_creation_cost
					else ''
				)
				parts.append(f'🔧 \033[94m{formatted}{price}\033[0m')
		if not parts:
			formatted = format_tokens(usage.prompt_tokens)
			price = f' (${cost.new_prompt_cost:.4f})' if self.pricing.include_cost and cost and cost.new_prompt_cost > 0 else ''
			parts.append(f'📥 \033[93m{formatted}{price}\033[0m')
		return ' + '.join(parts)

	def register_llm(self, llm: BaseChatModel) -> BaseChatModel:
		instance_id = str(id(llm))
		if instance_id in self.registered_llms:
			logger.debug(f'LLM instance {instance_id} ({llm.provider}_{llm.model}) is already registered')
			return llm
		self.registered_llms[instance_id] = llm
		self.pricing.pricing_model_names[llm.model] = self.get_pricing_model_name(llm)
		original_ainvoke = llm.ainvoke
		accounting = self

		async def tracked_ainvoke(messages, output_format=None, **kwargs):
			result = await original_ainvoke(messages, output_format, **kwargs)
			if result.usage:
				usage = accounting.add_usage(llm.model, result.usage)
				logger.debug(f'Token cost service: {usage}')
				create_task_with_error_handling(
					accounting.log_usage(llm.model, usage), name='log_token_usage', suppress_exceptions=True
				)
			return result

		object.__setattr__(llm, 'ainvoke', tracked_ainvoke)
		return llm

	@staticmethod
	def get_pricing_model_name(llm: BaseChatModel) -> str:
		model = str(llm.model)
		base_url = str(getattr(llm, 'base_url', '') or '').rstrip('/')
		if llm.provider == 'openrouter' or base_url == 'https://openrouter.ai/api/v1':
			if not is_openrouter_pricing_model(model):
				return f'openrouter/{model}'
		return model

	def get_usage_tokens_for_model(self, model: str) -> ModelUsageTokens:
		entries = [entry for entry in self.usage_history if entry.model == model]
		return ModelUsageTokens(
			model=model,
			prompt_tokens=sum(e.usage.prompt_tokens for e in entries),
			prompt_cached_tokens=sum(e.usage.prompt_cached_tokens or 0 for e in entries),
			completion_tokens=sum(e.usage.completion_tokens for e in entries),
			total_tokens=sum(e.usage.prompt_tokens + e.usage.completion_tokens for e in entries),
		)

	async def get_usage_summary(self, model: str | None = None, since: datetime | None = None) -> UsageSummary:
		entries = self.usage_history
		if model:
			entries = [entry for entry in entries if entry.model == model]
		if since:
			entries = [entry for entry in entries if entry.timestamp >= since]
		model_stats: dict[str, ModelUsageStats] = {}
		prompt_cost = completion_cost = cached_cost = cache_creation_cost = 0.0
		for entry in entries:
			stats = model_stats.setdefault(entry.model, ModelUsageStats(model=entry.model))
			stats.prompt_tokens += entry.usage.prompt_tokens
			stats.completion_tokens += entry.usage.completion_tokens
			stats.total_tokens += entry.usage.prompt_tokens + entry.usage.completion_tokens
			stats.invocations += 1
			if self.pricing.include_cost:
				cost = await self.pricing.calculate_cost(entry.model, entry.usage)
				if cost:
					stats.cost += cost.total_cost
					prompt_cost += cost.prompt_cost
					completion_cost += cost.completion_cost
					cached_cost += cost.prompt_read_cached_cost or 0
					cache_creation_cost += cost.prompt_cache_creation_cost or 0
		for stats in model_stats.values():
			if stats.invocations:
				stats.average_tokens_per_invocation = stats.total_tokens / stats.invocations
		prompt_tokens = sum(entry.usage.prompt_tokens for entry in entries)
		completion_tokens = sum(entry.usage.completion_tokens for entry in entries)
		return UsageSummary(
			total_prompt_tokens=prompt_tokens,
			total_prompt_cost=prompt_cost,
			total_prompt_cached_tokens=sum(e.usage.prompt_cached_tokens or 0 for e in entries),
			total_prompt_cached_cost=cached_cost,
			total_prompt_cache_creation_tokens=sum(e.usage.prompt_cache_creation_tokens or 0 for e in entries),
			total_prompt_cache_creation_cost=cache_creation_cost,
			total_completion_tokens=completion_tokens,
			total_completion_cost=completion_cost,
			total_tokens=prompt_tokens + completion_tokens,
			total_cost=prompt_cost + completion_cost,
			entry_count=len(entries),
			by_model=model_stats,
		)

	async def log_usage_summary(self) -> None:
		if not self.usage_history:
			return
		summary = await self.get_usage_summary()
		if summary.entry_count == 0:
			return
		if len(summary.by_model) > 1:
			cost = f' ($\033[95m{summary.total_cost:.4f}\033[0m)' if self.pricing.include_cost and summary.total_cost > 0 else ''
			cost_logger.debug(
				f'💲 \033[1mTotal Usage Summary\033[0m: \033[94m{format_tokens(summary.total_tokens)} tokens\033[0m{cost}'
			)
		for model, stats in summary.by_model.items():
			cost = f' ($\033[95m{stats.cost:.4f}\033[0m)' if self.pricing.include_cost and stats.cost > 0 else ''
			cost_logger.debug(
				f'  🤖 \033[96m{model}\033[0m: \033[94m{format_tokens(stats.total_tokens)} tokens\033[0m{cost} | ⬅️ \033[93m{format_tokens(stats.prompt_tokens)}\033[0m | ➡️ \033[92m{format_tokens(stats.completion_tokens)}\033[0m | 📞 {stats.invocations} calls | 📈 {format_tokens(int(stats.average_tokens_per_invocation))}/call'
			)

	async def get_cost_by_model(self) -> dict[str, ModelUsageStats]:
		return (await self.get_usage_summary()).by_model

	def clear_history(self) -> None:
		self.usage_history = []
