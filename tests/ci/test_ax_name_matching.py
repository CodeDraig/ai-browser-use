"""Tests for ax_name (accessible name) element matching in history rerun.

This tests Level 4 matching which uses the accessibility tree's name property
to match elements when hash, stable_hash, and xpath all fail.
This is particularly useful for dynamic SPAs where DOM structure changes
but accessible names remain stable.

Also tests dropdown/menu re-opening behavior when menu items can't be found
because the dropdown closed during the wait between steps.
"""

from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from browser_use.agent.history import AgentHistory, AgentHistoryList
from browser_use.agent.results import ActionResult, RerunSummaryAction, StepMetadata
from browser_use.agent.service import Agent
from browser_use.browser.views import BrowserStateHistory
from browser_use.dom.history import DOMInteractedElement, MatchLevel
from browser_use.dom.tree import DOMRect, NodeType
from tests.ci.conftest import create_mock_llm


async def test_ax_name_matching_succeeds_when_hash_fails(httpserver):
	"""Test that ax_name matching finds elements when hash/xpath matching fails.

	This simulates a dynamic SPA where the element hash and xpath change between
	sessions, but the accessible name (ax_name) remains stable.
	"""
	# Set up a test page with a menu item that has an aria-label
	# The aria-label becomes the accessible name (ax_name)
	test_html = """<!DOCTYPE html>
	<html>
	<body>
		<div role="menuitem" aria-label="New Contact" id="menu-1">New Contact</div>
		<div role="menuitem" aria-label="Search" id="menu-2">Search</div>
	</body>
	</html>"""
	httpserver.expect_request('/test').respond_with_data(test_html, content_type='text/html')
	test_url = httpserver.url_for('/test')

	# Create a mock LLM for summary
	summary_action = RerunSummaryAction(
		summary='Rerun completed',
		success=True,
		completion_status='complete',
	)

	async def custom_ainvoke(*args, **kwargs):
		output_format = args[1] if len(args) > 1 else kwargs.get('output_format')
		if output_format is RerunSummaryAction:
			from browser_use.llm.views import ChatInvokeCompletion

			return ChatInvokeCompletion(completion=summary_action, usage=None)
		raise ValueError('Unexpected output_format')

	mock_summary_llm = AsyncMock()
	mock_summary_llm.ainvoke.side_effect = custom_ainvoke

	llm = create_mock_llm(actions=None)
	agent = Agent(task='Test task', llm=llm)
	AgentOutput = agent.AgentOutput

	# Create an element with DIFFERENT hash/xpath but SAME ax_name as the real element
	# This simulates what happens in dynamic SPAs where the DOM changes but
	# accessible names remain stable
	historical_element = DOMInteractedElement(
		node_id=9999,  # Different node_id
		backend_node_id=9999,  # Different backend_node_id
		frame_id=None,
		node_type=NodeType.ELEMENT_NODE,
		node_value='',
		node_name='DIV',  # Same node type
		# Note: aria-label is NOT in attributes - this tests that ax_name matching
		# is used as a fallback when attribute matching fails
		attributes={'role': 'menuitem', 'class': 'dynamic-class-12345'},
		x_path='html/body/div[1]/div[4]/div[4]/div[1]',  # Different xpath
		element_hash=123456789,  # Different hash (won't match)
		stable_hash=987654321,  # Different stable_hash (won't match)
		bounds=DOMRect(x=0, y=0, width=100, height=50),
		ax_name='New Contact',  # SAME ax_name - this should match!
	)

	# Step 1: Navigate to test page
	navigate_step = AgentHistory(
		model_output=AgentOutput(
			evaluation_previous_goal=None,
			memory='Navigate to test page',
			next_goal=None,
			action=[{'navigate': {'url': test_url}}],  # type: ignore[arg-type]
		),
		result=[ActionResult(long_term_memory='Navigated')],
		state=BrowserStateHistory(
			url=test_url,
			title='Test Page',
			tabs=[],
			interacted_element=[None],
		),
		metadata=StepMetadata(
			step_start_time=0,
			step_end_time=1,
			step_number=1,
			step_interval=0.1,
		),
	)

	# Step 2: Click on element that has different hash/xpath but same ax_name
	click_step = AgentHistory(
		model_output=AgentOutput(
			evaluation_previous_goal=None,
			memory='Click New Contact menu',
			next_goal=None,
			action=[{'click': {'index': 100}}],  # type: ignore[arg-type]  # Original index doesn't matter
		),
		result=[ActionResult(long_term_memory='Clicked New Contact')],
		state=BrowserStateHistory(
			url=test_url,
			title='Test Page',
			tabs=[],
			interacted_element=[historical_element],
		),
		metadata=StepMetadata(
			step_start_time=1,
			step_end_time=2,
			step_number=2,
			step_interval=0.1,
		),
	)

	history = AgentHistoryList(history=[navigate_step, click_step])

	try:
		# Run rerun - should succeed because ax_name matching finds the element
		results = await agent.rerun_history(
			history,
			skip_failures=False,
			max_retries=1,
			summary_llm=mock_summary_llm,
		)

		# Should have 3 results: navigate + click + AI summary
		assert len(results) == 3

		# First result should be navigation success
		nav_result = results[0]
		assert nav_result.error is None

		# Second result should be click success (matched via ax_name)
		click_result = results[1]
		assert click_result.error is None, f'Click should succeed via ax_name matching, got error: {click_result.error}'

		# Third result should be AI summary
		summary_result = results[2]
		assert summary_result.is_done is True

	finally:
		await agent.close()


async def test_ax_name_matching_requires_same_node_type(httpserver):
	"""Test that ax_name matching also requires matching node type.

	Even if ax_name matches, the node type (DIV, BUTTON, etc.) must also match.
	"""
	test_html = """<!DOCTYPE html>
	<html>
	<body>
		<button aria-label="Submit">Submit</button>
		<div aria-label="Submit">Submit Label</div>
	</body>
	</html>"""
	httpserver.expect_request('/test').respond_with_data(test_html, content_type='text/html')
	test_url = httpserver.url_for('/test')

	llm = create_mock_llm(actions=None)
	agent = Agent(task='Test task', llm=llm)
	AgentOutput = agent.AgentOutput

	# Historical element is a SPAN with ax_name "Submit"
	# Page has BUTTON and DIV with same ax_name, but no SPAN
	historical_element = DOMInteractedElement(
		node_id=1,
		backend_node_id=1,
		frame_id=None,
		node_type=NodeType.ELEMENT_NODE,
		node_value='',
		node_name='SPAN',  # SPAN - won't match BUTTON or DIV
		attributes={},
		x_path='html/body/span',
		element_hash=111,
		stable_hash=111,
		bounds=DOMRect(x=0, y=0, width=100, height=50),
		ax_name='Submit',  # Same ax_name, but wrong node type
	)

	navigate_step = AgentHistory(
		model_output=AgentOutput(
			evaluation_previous_goal=None,
			memory='Navigate',
			next_goal=None,
			action=[{'navigate': {'url': test_url}}],  # type: ignore[arg-type]
		),
		result=[ActionResult(long_term_memory='Navigated')],
		state=BrowserStateHistory(
			url=test_url,
			title='Test',
			tabs=[],
			interacted_element=[None],
		),
		metadata=StepMetadata(step_start_time=0, step_end_time=1, step_number=1, step_interval=0.1),
	)

	click_step = AgentHistory(
		model_output=AgentOutput(
			evaluation_previous_goal=None,
			memory='Click Submit',
			next_goal=None,
			action=[{'click': {'index': 1}}],  # type: ignore[arg-type]
		),
		result=[ActionResult(long_term_memory='Clicked')],
		state=BrowserStateHistory(
			url=test_url,
			title='Test',
			tabs=[],
			interacted_element=[historical_element],
		),
		metadata=StepMetadata(step_start_time=1, step_end_time=2, step_number=2, step_interval=0.1),
	)

	history = AgentHistoryList(history=[navigate_step, click_step])

	try:
		# Should fail because no SPAN with ax_name "Submit" exists
		await agent.rerun_history(
			history,
			skip_failures=False,
			max_retries=1,
		)
		assert False, 'Expected RuntimeError - no matching SPAN element'
	except RuntimeError as e:
		# Expected - no SPAN element with ax_name "Submit"
		assert 'failed after 1 attempts' in str(e)
	finally:
		await agent.close()


def test_match_level_enum_ends_at_ax_name():
	"""Current histories do not use the removed legacy attribute fallback."""
	assert hasattr(MatchLevel, 'AX_NAME')
	assert MatchLevel.AX_NAME.value == 4
	assert not hasattr(MatchLevel, 'ATTRIBUTE')


def test_current_history_element_requires_stable_hash():
	payload = {
		'node_id': 1,
		'backend_node_id': 1,
		'frame_id': None,
		'node_type': NodeType.ELEMENT_NODE,
		'node_value': '',
		'node_name': 'button',
		'attributes': {},
		'bounds': None,
		'x_path': '//button',
		'element_hash': 1,
	}
	with pytest.raises(ValidationError, match='stable_hash'):
		TypeAdapter(DOMInteractedElement).validate_python(payload)


def test_is_menu_opener_step_detects_aria_haspopup():
	"""Test that _is_menu_opener_step detects aria-haspopup elements."""
	llm = create_mock_llm(actions=None)
	agent = Agent(task='Test task', llm=llm)
	AgentOutput = agent.AgentOutput

	# Element with aria-haspopup="true" should be detected as menu opener
	opener_element = DOMInteractedElement(
		node_id=1,
		backend_node_id=1,
		frame_id=None,
		node_type=NodeType.ELEMENT_NODE,
		node_value='',
		node_name='DIV',
		attributes={'aria-haspopup': 'true', 'class': 'dropdown-trigger'},
		x_path='html/body/div',
		element_hash=12345,
		stable_hash=12345,
		bounds=DOMRect(x=0, y=0, width=100, height=50),
		ax_name='Contact',
	)

	history_item = AgentHistory(
		model_output=AgentOutput(
			evaluation_previous_goal=None,
			memory='Click dropdown',
			next_goal=None,
			action=[{'click': {'index': 1}}],  # type: ignore[arg-type]
		),
		result=[ActionResult(long_term_memory='Clicked')],
		state=BrowserStateHistory(
			url='http://test.com',
			title='Test',
			tabs=[],
			interacted_element=[opener_element],
		),
		metadata=StepMetadata(step_start_time=0, step_end_time=1, step_number=1, step_interval=0.1),
	)

	assert agent._history_replay.retry_policy.is_menu_opener_step(history_item) is True


def test_is_menu_opener_step_detects_guidewire_toggle():
	"""Test that _is_menu_opener_step detects Guidewire toggleSubMenu pattern."""
	llm = create_mock_llm(actions=None)
	agent = Agent(task='Test task', llm=llm)
	AgentOutput = agent.AgentOutput

	# Element with data-gw-click="toggleSubMenu" should be detected
	opener_element = DOMInteractedElement(
		node_id=1,
		backend_node_id=1,
		frame_id=None,
		node_type=NodeType.ELEMENT_NODE,
		node_value='',
		node_name='DIV',
		attributes={'data-gw-click': 'toggleSubMenu', 'class': 'gw-action--expand-button'},
		x_path='html/body/div',
		element_hash=12345,
		stable_hash=12345,
		bounds=DOMRect(x=0, y=0, width=100, height=50),
		ax_name=None,
	)

	history_item = AgentHistory(
		model_output=AgentOutput(
			evaluation_previous_goal=None,
			memory='Toggle menu',
			next_goal=None,
			action=[{'click': {'index': 1}}],  # type: ignore[arg-type]
		),
		result=[ActionResult(long_term_memory='Toggled')],
		state=BrowserStateHistory(
			url='http://test.com',
			title='Test',
			tabs=[],
			interacted_element=[opener_element],
		),
		metadata=StepMetadata(step_start_time=0, step_end_time=1, step_number=1, step_interval=0.1),
	)

	assert agent._history_replay.retry_policy.is_menu_opener_step(history_item) is True


def test_is_menu_opener_step_returns_false_for_regular_element():
	"""Test that _is_menu_opener_step returns False for non-menu elements."""
	llm = create_mock_llm(actions=None)
	agent = Agent(task='Test task', llm=llm)
	AgentOutput = agent.AgentOutput

	# Regular button without menu attributes
	regular_element = DOMInteractedElement(
		node_id=1,
		backend_node_id=1,
		frame_id=None,
		node_type=NodeType.ELEMENT_NODE,
		node_value='',
		node_name='BUTTON',
		attributes={'class': 'submit-btn', 'type': 'submit'},
		x_path='html/body/button',
		element_hash=12345,
		stable_hash=12345,
		bounds=DOMRect(x=0, y=0, width=100, height=50),
		ax_name='Submit',
	)

	history_item = AgentHistory(
		model_output=AgentOutput(
			evaluation_previous_goal=None,
			memory='Click submit',
			next_goal=None,
			action=[{'click': {'index': 1}}],  # type: ignore[arg-type]
		),
		result=[ActionResult(long_term_memory='Clicked')],
		state=BrowserStateHistory(
			url='http://test.com',
			title='Test',
			tabs=[],
			interacted_element=[regular_element],
		),
		metadata=StepMetadata(step_start_time=0, step_end_time=1, step_number=1, step_interval=0.1),
	)

	assert agent._history_replay.retry_policy.is_menu_opener_step(history_item) is False


def test_is_menu_item_element_detects_role_menuitem():
	"""Test that _is_menu_item_element detects role=menuitem."""
	llm = create_mock_llm(actions=None)
	agent = Agent(task='Test task', llm=llm)

	menu_item = DOMInteractedElement(
		node_id=1,
		backend_node_id=1,
		frame_id=None,
		node_type=NodeType.ELEMENT_NODE,
		node_value='',
		node_name='DIV',
		attributes={'role': 'menuitem', 'class': 'menu-option'},
		x_path='html/body/div/div',
		element_hash=12345,
		stable_hash=12345,
		bounds=DOMRect(x=0, y=0, width=100, height=50),
		ax_name='New Contact',
	)

	assert agent._history_replay.retry_policy.is_menu_item_element(menu_item) is True


def test_is_menu_item_element_detects_guidewire_class():
	"""Test that _is_menu_item_element detects Guidewire gw-action--inner class."""
	llm = create_mock_llm(actions=None)
	agent = Agent(task='Test task', llm=llm)

	menu_item = DOMInteractedElement(
		node_id=1,
		backend_node_id=1,
		frame_id=None,
		node_type=NodeType.ELEMENT_NODE,
		node_value='',
		node_name='DIV',
		attributes={'class': 'gw-action--inner gw-hasDivider', 'aria-haspopup': 'true'},
		x_path='html/body/div/div',
		element_hash=12345,
		stable_hash=12345,
		bounds=DOMRect(x=0, y=0, width=100, height=50),
		ax_name='New Contact',
	)

	assert agent._history_replay.retry_policy.is_menu_item_element(menu_item) is True


def test_is_menu_item_element_returns_false_for_regular_element():
	"""Test that _is_menu_item_element returns False for non-menu elements."""
	llm = create_mock_llm(actions=None)
	agent = Agent(task='Test task', llm=llm)

	regular_element = DOMInteractedElement(
		node_id=1,
		backend_node_id=1,
		frame_id=None,
		node_type=NodeType.ELEMENT_NODE,
		node_value='',
		node_name='BUTTON',
		attributes={'class': 'submit-btn', 'type': 'submit'},
		x_path='html/body/button',
		element_hash=12345,
		stable_hash=12345,
		bounds=DOMRect(x=0, y=0, width=100, height=50),
		ax_name='Submit',
	)

	assert agent._history_replay.retry_policy.is_menu_item_element(regular_element) is False
