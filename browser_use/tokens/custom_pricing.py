"""
Custom model pricing for models not available in LiteLLM's pricing data.

Prices are per token (not per 1M tokens).
"""

from typing import Any

# Custom model pricing data
# Format matches LiteLLM's model_prices_and_context_window.json structure
CUSTOM_MODEL_PRICING: dict[str, dict[str, Any]] = {
	'claude-sonnet-4-6': {
		'input_cost_per_token': 3.00 / 1_000_000,
		'output_cost_per_token': 15.00 / 1_000_000,
		'cache_read_input_token_cost': 0.30 / 1_000_000,
		'cache_creation_input_token_cost': 3.75 / 1_000_000,
		'cache_creation_1h_input_token_cost': 6.00 / 1_000_000,
		'max_tokens': None,
		'max_input_tokens': None,
		'max_output_tokens': None,
	},
	'anthropic/claude-sonnet-4.6': {
		'input_cost_per_token': 3.00 / 1_000_000,
		'output_cost_per_token': 15.00 / 1_000_000,
		'cache_read_input_token_cost': 0.30 / 1_000_000,
		'cache_creation_input_token_cost': 3.75 / 1_000_000,
		'cache_creation_1h_input_token_cost': 6.00 / 1_000_000,
		'max_tokens': None,
		'max_input_tokens': None,
		'max_output_tokens': None,
	},
	'claude-opus-4-6': {
		'input_cost_per_token': 5.00 / 1_000_000,
		'output_cost_per_token': 25.00 / 1_000_000,
		'cache_read_input_token_cost': 0.50 / 1_000_000,
		'cache_creation_input_token_cost': 6.25 / 1_000_000,
		'cache_creation_1h_input_token_cost': 10.00 / 1_000_000,
		'max_tokens': None,
		'max_input_tokens': None,
		'max_output_tokens': None,
	},
	'anthropic/claude-opus-4.6': {
		'input_cost_per_token': 5.00 / 1_000_000,
		'output_cost_per_token': 25.00 / 1_000_000,
		'cache_read_input_token_cost': 0.50 / 1_000_000,
		'cache_creation_input_token_cost': 6.25 / 1_000_000,
		'cache_creation_1h_input_token_cost': 10.00 / 1_000_000,
		'max_tokens': None,
		'max_input_tokens': None,
		'max_output_tokens': None,
	},
	'claude-fable-5': {
		'input_cost_per_token': 10.00 / 1_000_000,
		'output_cost_per_token': 50.00 / 1_000_000,
		'cache_read_input_token_cost': 1.00 / 1_000_000,
		'cache_creation_input_token_cost': 12.50 / 1_000_000,
		'cache_creation_1h_input_token_cost': 20.00 / 1_000_000,
		'max_tokens': 1_000_000,
		'max_input_tokens': 1_000_000,
		'max_output_tokens': 128_000,
	},
	'anthropic/claude-fable-5': {
		'input_cost_per_token': 10.00 / 1_000_000,
		'output_cost_per_token': 50.00 / 1_000_000,
		'cache_read_input_token_cost': 1.00 / 1_000_000,
		'cache_creation_input_token_cost': 12.50 / 1_000_000,
		'cache_creation_1h_input_token_cost': 20.00 / 1_000_000,
		'max_tokens': 1_000_000,
		'max_input_tokens': 1_000_000,
		'max_output_tokens': 128_000,
	},
}
