"""Regression tests for ENG-5280: V2 worker crash on warm-Lambda resume.

A reused keep_alive session bus is stopped and nulled by Agent.close(); on resume the worker
can step() it before a dispatch() restarts it, which makes stock bubus assert. ResilientEventBus
no-ops step()/wait_until_idle() in that state while still restarting on the next dispatch().
"""

import asyncio

import pytest
from bubus import BaseEvent, EventBus

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.event_bus import ResilientEventBus
from browser_use.browser.events import (
	AgentFocusChangedEvent,
	BrowserStartEvent,
	BrowserStopEvent,
	CloseTabEvent,
	FileDownloadedEvent,
	NavigateToUrlEvent,
	SwitchTabEvent,
	TabClosedEvent,
	TabCreatedEvent,
)


class ResiliencePingEvent(BaseEvent):
	pass


CORE_EVENT_TYPES = (
	BrowserStartEvent,
	BrowserStopEvent,
	NavigateToUrlEvent,
	SwitchTabEvent,
	TabCreatedEvent,
	TabClosedEvent,
	AgentFocusChangedEvent,
	FileDownloadedEvent,
	CloseTabEvent,
)


def _tear_down_like_agent_close(bus: EventBus) -> None:
	"""Mimic the keep_alive teardown in Agent.close() that triggers ENG-5280."""
	bus.event_queue = None
	bus._on_idle = None


def test_browser_session_uses_resilient_event_bus():
	"""The session's default event bus must be the resilient subclass."""
	session = BrowserSession(browser_profile=BrowserProfile(keep_alive=True))
	assert isinstance(session.event_bus, ResilientEventBus)


@pytest.mark.parametrize('lifecycle_method', ['stop', 'kill'])
async def test_lifecycle_reset_renews_bus_with_one_core_handler_set(lifecycle_method: str):
	"""A stopped session must remain startable through its fresh event bus."""
	session = BrowserSession(browser_profile=BrowserProfile(user_data_dir=None, keep_alive=False))
	original_bus = session.event_bus

	await getattr(session, lifecycle_method)()

	assert session.event_bus is not original_bus
	assert session.watchdogs._attached is False
	for event_type in CORE_EVENT_TYPES:
		assert len(session.event_bus.handlers.get(event_type.__name__, [])) == 1


def test_resilient_event_bus_keeps_event_bus_name_prefix():
	"""The subclass must keep the ``EventBus_`` default name (not ``ResilientEventBus_``)."""
	assert ResilientEventBus().name.startswith('EventBus_')
	# Explicit names are still honored.
	assert ResilientEventBus(name='EventBus_custom').name == 'EventBus_custom'


async def test_step_on_torn_down_bus_is_noop_not_assertion():
	"""Stepping a stopped+nulled bus returns None instead of raising AssertionError."""
	bus = ResilientEventBus(name='ResilientWarmResumeStep')
	bus.dispatch(ResiliencePingEvent())
	await asyncio.sleep(0.1)
	await bus.stop(clear=False, timeout=1.0)
	_tear_down_like_agent_close(bus)

	# Warm-Lambda resume: worker steps the torn-down bus. Must not raise.
	assert await bus.step() is None
	assert await bus.wait_until_idle(timeout=0.1) is None


async def test_torn_down_bus_still_restarts_on_dispatch():
	"""A later dispatch() must recreate the queue and process events normally."""
	processed: list[BaseEvent] = []
	bus = ResilientEventBus(name='ResilientWarmResumeRestart')
	bus.on('ResiliencePingEvent', lambda event: processed.append(event))

	bus.dispatch(ResiliencePingEvent())
	await asyncio.sleep(0.1)
	await bus.stop(clear=False, timeout=1.0)
	_tear_down_like_agent_close(bus)

	# No-op step on the torn-down bus, then dispatch again -> bus restarts.
	assert await bus.step() is None
	processed.clear()
	event = bus.dispatch(ResiliencePingEvent())
	await asyncio.wait_for(event, timeout=2.0)
	assert len(processed) == 1
	await bus.stop(timeout=0.5)


async def test_stock_event_bus_reproduces_the_crash():
	"""Document the upstream bug the subclass guards against (stock bus still asserts)."""
	bus = EventBus(name='StockWarmResume')
	bus.dispatch(ResiliencePingEvent())
	await asyncio.sleep(0.1)
	await bus.stop(clear=False, timeout=1.0)
	_tear_down_like_agent_close(bus)

	try:
		await bus.step()
		raised = False
	except AssertionError:
		raised = True
	assert raised, 'stock EventBus.step() should assert on a torn-down bus'
