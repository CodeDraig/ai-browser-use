"""
Test retry logic with exponential backoff for LLM clients.
"""

import time
from unittest.mock import MagicMock, patch

import pytest


class TestChatGoogleRetries:
	"""Test retry logic for ChatGoogle."""

	@pytest.fixture
	def mock_env(self, monkeypatch):
		"""Set up environment for ChatGoogle."""
		monkeypatch.setenv('GOOGLE_API_KEY', 'test-api-key')

	@pytest.mark.asyncio
	async def test_retries_on_503_with_exponential_backoff(self, mock_env):
		"""Test that 503 errors trigger retries with exponential backoff."""
		from browser_use.llm.exceptions import ModelProviderError
		from browser_use.llm.google.chat import ChatGoogle
		from browser_use.llm.messages import UserMessage

		attempt_times: list[float] = []
		attempt_count = 0

		# Mock the genai client
		with patch('browser_use.llm.google.chat.genai') as mock_genai:
			mock_client = MagicMock()
			mock_genai.Client.return_value = mock_client

			async def mock_generate(*args, **kwargs):
				nonlocal attempt_count
				attempt_times.append(time.monotonic())
				attempt_count += 1

				if attempt_count < 3:
					raise ModelProviderError(message='Service unavailable', status_code=503, model='gemini-2.0-flash')
				else:
					# Success on third attempt
					mock_response = MagicMock()
					mock_response.text = 'Success!'
					mock_response.usage_metadata = MagicMock(
						prompt_token_count=10, candidates_token_count=5, total_token_count=15, cached_content_token_count=0
					)
					mock_response.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(text='Success!')]))]
					return mock_response

			# Mock the aio.models.generate_content path
			mock_client.aio.models.generate_content = mock_generate

			client = ChatGoogle(model='gemini-2.0-flash', api_key='test', retry_base_delay=0.1, retry_max_delay=1.0)
			result = await client.ainvoke([UserMessage(content='test')])

		assert attempt_count == 3
		assert result.completion == 'Success!'

		# Verify exponential backoff
		delay_1 = attempt_times[1] - attempt_times[0]
		delay_2 = attempt_times[2] - attempt_times[1]

		assert 0.05 <= delay_1 <= 0.3, f'First delay {delay_1:.3f}s not in expected range'
		assert 0.1 <= delay_2 <= 0.5, f'Second delay {delay_2:.3f}s not in expected range'
		assert delay_2 > delay_1, 'Second delay should be longer than first'

	@pytest.mark.asyncio
	async def test_no_retry_on_400(self, mock_env):
		"""Test that 400 errors do NOT trigger retries."""
		from browser_use.llm.exceptions import ModelProviderError
		from browser_use.llm.google.chat import ChatGoogle
		from browser_use.llm.messages import UserMessage

		attempt_count = 0

		with patch('browser_use.llm.google.chat.genai') as mock_genai:
			mock_client = MagicMock()
			mock_genai.Client.return_value = mock_client

			async def mock_generate(*args, **kwargs):
				nonlocal attempt_count
				attempt_count += 1
				raise ModelProviderError(message='Bad request', status_code=400, model='gemini-2.0-flash')

			mock_client.aio.models.generate_content = mock_generate

			client = ChatGoogle(model='gemini-2.0-flash', api_key='test', retry_base_delay=0.01)

			with pytest.raises(ModelProviderError):
				await client.ainvoke([UserMessage(content='test')])

		# Should only attempt once (400 is not retryable)
		assert attempt_count == 1

	@pytest.mark.asyncio
	async def test_retries_on_429_rate_limit(self, mock_env):
		"""Test that 429 rate limit errors trigger retries."""
		from browser_use.llm.exceptions import ModelProviderError
		from browser_use.llm.google.chat import ChatGoogle
		from browser_use.llm.messages import UserMessage

		attempt_count = 0

		with patch('browser_use.llm.google.chat.genai') as mock_genai:
			mock_client = MagicMock()
			mock_genai.Client.return_value = mock_client

			async def mock_generate(*args, **kwargs):
				nonlocal attempt_count
				attempt_count += 1

				if attempt_count < 2:
					raise ModelProviderError(message='Rate limit exceeded', status_code=429, model='gemini-2.0-flash')
				else:
					mock_response = MagicMock()
					mock_response.text = 'Success after rate limit!'
					mock_response.usage_metadata = MagicMock(
						prompt_token_count=10, candidates_token_count=5, total_token_count=15, cached_content_token_count=0
					)
					mock_response.candidates = [MagicMock(content=MagicMock(parts=[MagicMock(text='Success after rate limit!')]))]
					return mock_response

			mock_client.aio.models.generate_content = mock_generate

			client = ChatGoogle(model='gemini-2.0-flash', api_key='test', retry_base_delay=0.01)
			result = await client.ainvoke([UserMessage(content='test')])

		assert attempt_count == 2
		assert result.completion == 'Success after rate limit!'


if __name__ == '__main__':
	pytest.main([__file__, '-v'])
