from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from browser_use.agent.settings import MessageCompactionSettings
from browser_use.agent.state import AgentStepInfo
from browser_use.llm.base import BaseChatModel
from browser_use.llm.messages import SystemMessage, UserMessage

if TYPE_CHECKING:
	from browser_use.agent.message_manager.service import MessageManager

logger = logging.getLogger(__name__)


class MessageCompactor:
	"""Summarize older message history according to explicit compaction policy."""

	def __init__(self, manager: MessageManager) -> None:
		self.manager = manager

	async def maybe_compact_messages(
		self,
		llm: BaseChatModel | None,
		settings: MessageCompactionSettings | None,
		step_info: AgentStepInfo | None = None,
	) -> bool:
		"""Summarize older history into a compact memory block.

		Step interval is the primary trigger; char count is a minimum floor.
		"""
		if not settings or not settings.enabled:
			return False
		if llm is None:
			return False
		if step_info is None:
			return False

		# Step cadence gate
		steps_since = step_info.step_number - (self.manager.state.last_compaction_step or 0)
		if steps_since < settings.compact_every_n_steps:
			return False

		# Char floor gate
		history_items = self.manager.state.agent_history_items
		full_history_text = '\n'.join(item.to_string() for item in history_items).strip()
		trigger_char_count = settings.trigger_char_count if settings.trigger_char_count is not None else 40000
		if len(full_history_text) < trigger_char_count:
			return False

		logger.debug(f'Compacting message history (items={len(history_items)}, chars={len(full_history_text)})')

		# Build compaction input
		compaction_sections = []
		if self.manager.state.compacted_memory:
			compaction_sections.append(
				f'<previous_compacted_memory>\n{self.manager.state.compacted_memory}\n</previous_compacted_memory>'
			)
		compaction_sections.append(f'<agent_history>\n{full_history_text}\n</agent_history>')
		if settings.include_read_state and self.manager.state.read_state_description:
			compaction_sections.append(f'<read_state>\n{self.manager.state.read_state_description}\n</read_state>')
		compaction_input = '\n\n'.join(compaction_sections)

		if self.manager.sensitive_data:
			filtered = self.manager._filter_sensitive_data(UserMessage(content=compaction_input))
			compaction_input = filtered.text

		system_prompt = (
			'You are summarizing an agent run for prompt compaction.\n'
			'Capture task requirements, key facts, decisions, partial progress, errors, and next steps.\n'
			'Preserve important entities, values, URLs, and file paths.\n'
			'CRITICAL: Only mark a step as completed if you see explicit success confirmation in the history. '
			'If a step was started but not explicitly confirmed complete, mark it as "IN-PROGRESS". '
			'Never infer completion from context — only report what was confirmed.\n'
			'Return plain text only. Do not include tool calls or JSON.'
		)
		if settings.summary_max_chars:
			system_prompt += f' Keep under {settings.summary_max_chars} characters if possible.'

		messages = [SystemMessage(content=system_prompt), UserMessage(content=compaction_input)]
		try:
			response = await llm.ainvoke(messages)
			summary = (response.completion or '').strip()
		except Exception as e:
			logger.warning(f'Failed to compact messages: {e}')
			return False

		if not summary:
			return False

		if settings.summary_max_chars and len(summary) > settings.summary_max_chars:
			summary = summary[: settings.summary_max_chars].rstrip() + '…'

		self.manager.state.compacted_memory = summary
		self.manager.state.compaction_count += 1
		self.manager.state.last_compaction_step = step_info.step_number

		# Keep first item + most recent items
		keep_last = max(0, settings.keep_last_items)
		if len(history_items) > keep_last + 1:
			if keep_last == 0:
				self.manager.state.agent_history_items = [history_items[0]]
			else:
				self.manager.state.agent_history_items = [history_items[0]] + history_items[-keep_last:]

		logger.debug(
			f'Compaction complete (summary_chars={len(summary)}, history_items={len(self.manager.state.agent_history_items)})'
		)

		return True
