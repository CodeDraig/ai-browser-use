"""Agent runtime and loop-detection state models."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from uuid_extensions import uuid7str

from browser_use.agent.message_manager.views import MessageManagerState
from browser_use.agent.results import ActionResult, AgentOutput, PlanItem
from browser_use.filesystem.file_types import FileSystemState


class PageFingerprint(BaseModel):
	"""Lightweight fingerprint of the browser page state."""

	model_config = ConfigDict(frozen=True)
	url: str
	element_count: int
	text_hash: str

	@staticmethod
	def from_browser_state(url: str, dom_text: str, element_count: int) -> 'PageFingerprint':
		text_hash = hashlib.sha256(dom_text.encode('utf-8', errors='replace')).hexdigest()[:16]
		return PageFingerprint(url=url, element_count=element_count, text_hash=text_hash)


def _normalize_action_for_hash(action_name: str, params: dict[str, Any]) -> str:
	if action_name == 'search':
		query = str(params.get('query', ''))
		tokens = sorted(set(re.sub(r'[^\w\s]', ' ', query.lower()).split()))
		return f'search|{params.get("engine", "google")}|{"|".join(tokens)}'
	if action_name in ('click', 'input'):
		index = params.get('index')
		if action_name == 'input':
			return f'input|{index}|{str(params.get("text", "")).strip().lower()}'
		return f'click|{index}'
	if action_name == 'navigate':
		return f'navigate|{params.get("url", "")}'
	if action_name == 'scroll':
		return f'scroll|{"down" if params.get("down", True) else "up"}|{params.get("index")}'
	filtered = {key: value for key, value in sorted(params.items()) if value is not None}
	return f'{action_name}|{json.dumps(filtered, sort_keys=True, default=str)}'


def compute_action_hash(action_name: str, params: dict[str, Any]) -> str:
	"""Compute a stable hash from an action and its normalized parameters."""
	return hashlib.sha256(_normalize_action_for_hash(action_name, params).encode('utf-8')).hexdigest()[:12]


class ActionLoopDetector(BaseModel):
	"""Track action repetition and page stagnation without blocking actions."""

	model_config = ConfigDict(arbitrary_types_allowed=True)
	window_size: int = 20
	recent_action_hashes: list[str] = Field(default_factory=list)
	recent_page_fingerprints: list[PageFingerprint] = Field(default_factory=list)
	max_repetition_count: int = 0
	most_repeated_hash: str | None = None
	consecutive_stagnant_pages: int = 0

	def record_action(self, action_name: str, params: dict[str, Any]) -> None:
		self.recent_action_hashes.append(compute_action_hash(action_name, params))
		if len(self.recent_action_hashes) > self.window_size:
			self.recent_action_hashes = self.recent_action_hashes[-self.window_size :]
		self._update_repetition_stats()

	def record_page_state(self, url: str, dom_text: str, element_count: int) -> None:
		fingerprint = PageFingerprint.from_browser_state(url, dom_text, element_count)
		if self.recent_page_fingerprints and self.recent_page_fingerprints[-1] == fingerprint:
			self.consecutive_stagnant_pages += 1
		else:
			self.consecutive_stagnant_pages = 0
		self.recent_page_fingerprints.append(fingerprint)
		if len(self.recent_page_fingerprints) > 5:
			self.recent_page_fingerprints = self.recent_page_fingerprints[-5:]

	def _update_repetition_stats(self) -> None:
		if not self.recent_action_hashes:
			self.max_repetition_count = 0
			self.most_repeated_hash = None
			return
		counts: dict[str, int] = {}
		for action_hash in self.recent_action_hashes:
			counts[action_hash] = counts.get(action_hash, 0) + 1
		self.most_repeated_hash = max(counts, key=counts.__getitem__)
		self.max_repetition_count = counts[self.most_repeated_hash]

	def get_nudge_message(self) -> str | None:
		messages: list[str] = []
		if self.max_repetition_count >= 12:
			messages.append(
				f'Heads up: you have repeated a similar action {self.max_repetition_count} times '
				f'in the last {len(self.recent_action_hashes)} actions. '
				'If you are making progress with each repetition, keep going. '
				'If not, a different approach might get you there faster.'
			)
		elif self.max_repetition_count >= 8:
			messages.append(
				f'Heads up: you have repeated a similar action {self.max_repetition_count} times '
				f'in the last {len(self.recent_action_hashes)} actions. '
				'Are you still making progress with each attempt? '
				'If so, carry on. Otherwise, it might be worth trying a different approach.'
			)
		elif self.max_repetition_count >= 5:
			messages.append(
				f'Heads up: you have repeated a similar action {self.max_repetition_count} times '
				f'in the last {len(self.recent_action_hashes)} actions. '
				'If this is intentional and making progress, carry on. '
				'If not, it might be worth reconsidering your approach.'
			)
		if self.consecutive_stagnant_pages >= 5:
			messages.append(
				f'The page content has not changed across {self.consecutive_stagnant_pages} consecutive actions. '
				'Your actions might not be having the intended effect. '
				'It could be worth trying a different element or approach.'
			)
		return '\n\n'.join(messages) if messages else None


class AgentState(BaseModel):
	"""Holds all state information for an Agent."""

	model_config = ConfigDict(arbitrary_types_allowed=True)
	agent_id: str = Field(default_factory=uuid7str)
	n_steps: int = 1
	consecutive_failures: int = 0
	last_result: list[ActionResult] | None = None
	plan: list[PlanItem] | None = None
	current_plan_item_index: int = 0
	plan_generation_step: int | None = None
	last_model_output: AgentOutput | None = None
	paused: bool = False
	stopped: bool = False
	session_initialized: bool = False
	follow_up_task: bool = False
	message_manager_state: MessageManagerState = Field(default_factory=MessageManagerState)
	file_system_state: FileSystemState | None = None
	loop_detector: ActionLoopDetector = Field(default_factory=ActionLoopDetector)


@dataclass
class AgentStepInfo:
	step_number: int
	max_steps: int

	def is_last_step(self) -> bool:
		return self.step_number >= self.max_steps - 1
