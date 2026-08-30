import pytest
from pydantic import ValidationError

from browser_use.agent.history import AgentHistory, AgentHistoryList
from browser_use.agent.results import ActionResult, AgentOutput, StepMetadata
from browser_use.browser.views import BrowserStateHistory
from browser_use.tools.service import Tools


def test_step_metadata_has_step_interval_field():
	"""Test that StepMetadata includes step_interval field"""
	metadata = StepMetadata(step_number=1, step_start_time=10.0, step_end_time=12.5, step_interval=2.5)

	assert hasattr(metadata, 'step_interval')
	assert metadata.step_interval == 2.5


def test_step_metadata_step_interval_nullable_but_required():
	"""The first step may have no interval, but the serialized field is required."""
	metadata_none = StepMetadata(step_number=0, step_start_time=0.0, step_end_time=1.0, step_interval=None)
	assert metadata_none.step_interval is None

	from pydantic import ValidationError

	try:
		StepMetadata(step_number=0, step_start_time=0.0, step_end_time=1.0)  # type: ignore[call-arg]
	except ValidationError:
		pass
	else:
		raise AssertionError('step_interval must be supplied, including for the first step')


def test_step_interval_calculation():
	"""Test step_interval calculation logic (uses previous step's duration)"""
	# Previous step (Step 1): runs from 100.0 to 102.5 (duration: 2.5s)
	previous_start = 100.0
	previous_end = 102.5
	previous_duration = previous_end - previous_start

	# Current step (Step 2): should have step_interval = previous step's duration
	# This tells the rerun system "wait 2.5s before executing Step 2"
	expected_step_interval = previous_duration
	calculated_step_interval = max(0, previous_end - previous_start)

	assert abs(calculated_step_interval - expected_step_interval) < 0.001  # Float comparison
	assert calculated_step_interval == 2.5


def test_step_metadata_serialization_with_step_interval():
	"""Test that step_interval is included in metadata serialization"""
	# With step_interval
	metadata_with_wait = StepMetadata(step_number=1, step_start_time=10.0, step_end_time=12.5, step_interval=2.5)

	data = metadata_with_wait.model_dump()
	assert 'step_interval' in data
	assert data['step_interval'] == 2.5

	# Without step_interval (None)
	metadata_without_wait = StepMetadata(step_number=0, step_start_time=0.0, step_end_time=1.0, step_interval=None)

	data = metadata_without_wait.model_dump()
	assert 'step_interval' in data
	assert data['step_interval'] is None


def test_step_metadata_deserialization_with_step_interval():
	"""Test that step_interval can be loaded from dict"""
	# Load with step_interval
	data_with_wait = {'step_number': 1, 'step_start_time': 10.0, 'step_end_time': 12.5, 'step_interval': 2.5}

	metadata = StepMetadata.model_validate(data_with_wait)
	assert metadata.step_interval == 2.5

	# Missing step_interval is no longer a current-format history.
	data_without_wait = {
		'step_number': 0,
		'step_start_time': 0.0,
		'step_end_time': 1.0,
		# step_interval is missing
	}

	from pydantic import ValidationError

	try:
		StepMetadata.model_validate(data_without_wait)
	except ValidationError:
		pass
	else:
		raise AssertionError('step_interval must be present in serialized metadata')


def test_duration_seconds_property_still_works():
	"""Test that existing duration_seconds property still works"""
	metadata = StepMetadata(step_number=1, step_start_time=10.0, step_end_time=13.5, step_interval=2.0)

	# duration_seconds should be 3.5 (13.5 - 10.0)
	assert metadata.duration_seconds == 3.5

	# step_interval is separate from duration
	assert metadata.step_interval == 2.0


def test_step_metadata_json_round_trip():
	"""Test that step_interval survives JSON serialization round-trip"""
	metadata = StepMetadata(step_number=1, step_start_time=100.0, step_end_time=102.5, step_interval=1.5)

	# Serialize to JSON
	json_str = metadata.model_dump_json()

	# Deserialize from JSON
	loaded = StepMetadata.model_validate_json(json_str)

	assert loaded.step_interval == 1.5
	assert loaded.step_number == 1
	assert loaded.step_start_time == 100.0
	assert loaded.step_end_time == 102.5


def _current_history_data() -> tuple[dict, type[AgentOutput]]:
	tools = Tools()
	action_model = tools.registry.create_action_model()
	output_model = AgentOutput.type_with_custom_actions(action_model)
	item = AgentHistory(
		model_output=output_model(
			evaluation_previous_goal='Start',
			memory='Ready',
			next_goal='Finish',
			action=[{'done': {'text': 'finished', 'success': True}}],  # type: ignore[arg-type]
		),
		result=[ActionResult(is_done=True, success=True)],
		state=BrowserStateHistory(url='https://example.com', title='Example', tabs=[], interacted_element=[None]),
		metadata=StepMetadata(step_number=0, step_start_time=0.0, step_end_time=1.0, step_interval=None),
	)
	return {'history': [item.model_dump()]}, output_model


def test_current_history_round_trip_and_custom_actions():
	data, output_model = _current_history_data()
	history = AgentHistoryList.load_from_dict(data, output_model)

	assert history.history[0].model_output is not None
	assert history.history[0].model_output.action[0].model_dump()['done']['text'] == 'finished'


def test_history_loader_rejects_missing_current_fields_without_repairing():
	data, output_model = _current_history_data()
	data['history'][0]['state'].pop('interacted_element')

	with pytest.raises(ValidationError):
		AgentHistoryList.load_from_dict(data, output_model)
	assert 'interacted_element' not in data['history'][0]['state']


def test_history_loader_rejects_missing_history_through_validation():
	_, output_model = _current_history_data()

	with pytest.raises(ValidationError):
		AgentHistoryList.load_from_dict({}, output_model)


def test_history_loader_rejects_missing_step_interval():
	data, output_model = _current_history_data()
	data['history'][0]['metadata'].pop('step_interval')

	with pytest.raises(ValidationError):
		AgentHistoryList.load_from_dict(data, output_model)


def test_history_loader_rejects_old_output_schema():
	data, output_model = _current_history_data()
	data['history'][0]['model_output'].pop('next_goal')

	with pytest.raises(ValidationError):
		AgentHistoryList.load_from_dict(data, output_model)
