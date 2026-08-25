"""Browser event-bus lifecycle behavior."""

from typing import Any

from bubus import BaseEvent, EventBus
from uuid_extensions import uuid7str


class ResilientEventBus(EventBus):
	"""Event bus that tolerates stepping after its async primitives were torn down."""

	def __init__(self, name: str | None = None, **kwargs: Any) -> None:
		super().__init__(name=name or f'EventBus_{uuid7str()[-8:]}', **kwargs)

	async def step(
		self,
		event: BaseEvent[Any] | None = None,
		timeout: float | None = None,
		wait_for_timeout: float = 0.1,
	) -> BaseEvent[Any] | None:
		if self._on_idle is None or self.event_queue is None:
			return None
		return await super().step(event, timeout, wait_for_timeout)

	async def wait_until_idle(self, timeout: float | None = None) -> None:
		if self._on_idle is None or self.event_queue is None:
			return None
		return await super().wait_until_idle(timeout)
