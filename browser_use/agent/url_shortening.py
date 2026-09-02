"""Reversible shortening of long URLs in model messages and outputs."""

import hashlib
import re

from pydantic import BaseModel

from browser_use.agent.url_detection import substitute_url_candidates
from browser_use.llm.messages import AssistantMessage, BaseMessage, ContentPartTextParam, UserMessage


class AgentUrlShortener:
	"""Shorten long URL suffixes before model calls and restore them afterwards."""

	def __init__(self, suffix_limit: int) -> None:
		self.suffix_limit = suffix_limit

	def shorten_text(self, text: str) -> tuple[str, dict[str, str]]:
		"""Replace oversized URL query/fragment suffixes in text."""
		replaced_urls: dict[str, str] = {}

		def replace_url(match: re.Match[str]) -> str:
			original_url = match.group(0)
			query_start = original_url.find('?')
			fragment_start = original_url.find('#')
			suffix_start = len(original_url)
			if query_start != -1:
				suffix_start = min(suffix_start, query_start)
			if fragment_start != -1:
				suffix_start = min(suffix_start, fragment_start)

			base_url = original_url[:suffix_start]
			suffix = original_url[suffix_start:]
			if len(suffix) <= self.suffix_limit:
				return original_url

			if suffix:
				truncated_suffix = suffix[: self.suffix_limit]
				short_hash = hashlib.md5(suffix.encode('utf-8')).hexdigest()[:7]
				shortened = f'{base_url}{truncated_suffix}...{short_hash}'
				if len(shortened) < len(original_url):
					replaced_urls[shortened] = original_url
					return shortened
			return original_url

		return substitute_url_candidates(text, replace_url), replaced_urls

	def shorten_messages(self, input_messages: list[BaseMessage]) -> dict[str, str]:
		"""Shorten URLs in user and assistant messages in place."""
		urls_replaced: dict[str, str] = {}
		for message in input_messages:
			if not isinstance(message, (UserMessage, AssistantMessage)):
				continue
			if isinstance(message.content, str):
				message.content, replaced_urls = self.shorten_text(message.content)
				urls_replaced.update(replaced_urls)
			elif isinstance(message.content, list):
				for part in message.content:
					if isinstance(part, ContentPartTextParam):
						part.text, replaced_urls = self.shorten_text(part.text)
						urls_replaced.update(replaced_urls)
		return urls_replaced

	@classmethod
	def restore_model_urls(cls, model: BaseModel, url_replacements: dict[str, str]) -> None:
		"""Restore shortened URLs in every string nested in a Pydantic model."""
		for field_name, field_value in model.__dict__.items():
			if isinstance(field_value, str):
				setattr(model, field_name, cls._restore_text(field_value, url_replacements))
			elif isinstance(field_value, BaseModel):
				cls.restore_model_urls(field_value, url_replacements)
			elif isinstance(field_value, dict):
				cls._restore_dict(field_value, url_replacements)
			elif isinstance(field_value, (list, tuple)):
				setattr(model, field_name, cls._restore_sequence(field_value, url_replacements))

	@classmethod
	def _restore_dict(cls, dictionary: dict, url_replacements: dict[str, str]) -> None:
		for key, value in dictionary.items():
			if isinstance(value, str):
				dictionary[key] = cls._restore_text(value, url_replacements)
			elif isinstance(value, BaseModel):
				cls.restore_model_urls(value, url_replacements)
			elif isinstance(value, dict):
				cls._restore_dict(value, url_replacements)
			elif isinstance(value, (list, tuple)):
				dictionary[key] = cls._restore_sequence(value, url_replacements)

	@classmethod
	def _restore_sequence(cls, container: list | tuple, url_replacements: dict[str, str]) -> list | tuple:
		if isinstance(container, tuple):
			processed_items = []
			for item in container:
				if isinstance(item, str):
					processed_items.append(cls._restore_text(item, url_replacements))
				elif isinstance(item, BaseModel):
					cls.restore_model_urls(item, url_replacements)
					processed_items.append(item)
				elif isinstance(item, dict):
					cls._restore_dict(item, url_replacements)
					processed_items.append(item)
				elif isinstance(item, (list, tuple)):
					processed_items.append(cls._restore_sequence(item, url_replacements))
				else:
					processed_items.append(item)
			return tuple(processed_items)

		for index, item in enumerate(container):
			if isinstance(item, str):
				container[index] = cls._restore_text(item, url_replacements)
			elif isinstance(item, BaseModel):
				cls.restore_model_urls(item, url_replacements)
			elif isinstance(item, dict):
				cls._restore_dict(item, url_replacements)
			elif isinstance(item, (list, tuple)):
				container[index] = cls._restore_sequence(item, url_replacements)
		return container

	@staticmethod
	def _restore_text(text: str, url_replacements: dict[str, str]) -> str:
		for shortened_url, original_url in url_replacements.items():
			text = text.replace(shortened_url, original_url)
		return text
