import inspect
from unittest.mock import AsyncMock

from browser_use import Agent, BrowserProfile, BrowserSession
from browser_use.agent.construction import AgentConstruction
from browser_use.agent.execution import AgentExecution
from browser_use.agent.history import AgentHistoryList
from browser_use.agent.history_replay import AgentHistoryReplay
from browser_use.agent.model_interaction import AgentModelInteraction
from browser_use.agent.model_settings import AgentModelSettings
from browser_use.agent.state import AgentStepInfo
from browser_use.agent.state_restoration import AgentStateRestoration


def test_agent_components_share_the_agent_as_their_only_state_owner(browser_session, mock_llm):
	agent = Agent(task='Test task', llm=mock_llm, browser=browser_session)

	assert isinstance(agent._construction, AgentConstruction)
	assert isinstance(agent._model_settings, AgentModelSettings)
	assert isinstance(agent._state_restoration, AgentStateRestoration)
	assert isinstance(agent._model_interaction, AgentModelInteraction)
	assert isinstance(agent._execution, AgentExecution)
	assert isinstance(agent._history_replay, AgentHistoryReplay)
	assert all(
		component.agent is agent
		for component in (
			agent._construction,
			agent._model_settings,
			agent._state_restoration,
			agent._model_interaction,
			agent._execution,
			agent._history_replay,
		)
	)


def test_agent_facade_contract_excludes_moved_and_skills_surfaces():
	parameters = inspect.signature(Agent).parameters
	assert 'skills' not in parameters
	assert 'skill_service' not in parameters
	for name in ('step', 'take_step', 'multi_act', 'save_file_system_state', 'log_completion'):
		assert not hasattr(Agent, name)


async def test_rerun_history_omits_failures_by_default(browser_session, mock_llm, monkeypatch):
	"""The facade must pass the public stop-on-first-failure default to replay."""
	agent = Agent(task='Test task', llm=mock_llm, browser=browser_session)
	history = AgentHistoryList(history=[])
	rerun = AsyncMock(return_value=[])
	monkeypatch.setattr(agent._history_replay, 'rerun_history', rerun)

	await agent.rerun_history(history)

	assert inspect.signature(Agent.rerun_history).parameters['skip_failures'].default is False
	call_args = rerun.await_args
	assert call_args is not None
	assert call_args.args[2] is False


def test_existing_browser_wins_over_profile_and_demo_mode_still_overrides(mock_llm):
	browser_session = BrowserSession(browser_profile=BrowserProfile(headless=True, demo_mode=False))
	original_profile = browser_session.browser_profile
	agent = Agent(
		task='Test task',
		llm=mock_llm,
		browser=browser_session,
		browser_profile=BrowserProfile(headless=not bool(original_profile.headless)),
		demo_mode=not original_profile.demo_mode,
	)

	assert agent.browser_session is browser_session
	assert agent.browser_profile.demo_mode is (not original_profile.demo_mode)


def test_direct_url_extraction_builds_initial_navigation(browser_session, mock_llm):
	agent = Agent(task='Open https://example.com/path and inspect it', llm=mock_llm, browser=browser_session)

	assert agent.initial_url == 'https://example.com/path'
	assert agent.initial_actions is not None
	assert agent.initial_actions[0].model_dump(exclude_none=True) == {
		'navigate': {'url': 'https://example.com/path', 'new_tab': False}
	}


async def test_execution_hooks_receive_the_owning_agent(browser_session, mock_llm, monkeypatch):
	agent = Agent(task='Test task', llm=mock_llm, browser=browser_session)
	seen: list[tuple[str, Agent]] = []

	async def on_start(owner: Agent) -> None:
		seen.append(('start', owner))

	async def on_end(owner: Agent) -> None:
		seen.append(('end', owner))

	monkeypatch.setattr(agent._execution, 'step', AsyncMock())
	done = await agent._execution._execute_step(
		0,
		1,
		AgentStepInfo(step_number=0, max_steps=1),
		on_start,
		on_end,
	)

	assert done is False
	assert seen == [('start', agent), ('end', agent)]
