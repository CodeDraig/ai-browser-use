from typing import Any

from browser_use.tools.service import Tools

_SPECIAL_ARGUMENTS = {
	'browser_session',
	'page_extraction_llm',
	'file_system',
	'available_file_paths',
	'sensitive_data',
	'extraction_schema',
	'action_timeout',
}


async def execute_registered_action(tools: Tools, action_name: str, **kwargs: Any):
	"""Exercise an action through the same explicit model and execution path as Agent."""
	registered_action = tools.registry.registry.actions[action_name]
	action_parameters = {key: value for key, value in kwargs.items() if key not in _SPECIAL_ARGUMENTS}
	injected_arguments = {key: value for key, value in kwargs.items() if key in _SPECIAL_ARGUMENTS}
	browser_session = injected_arguments.pop('browser_session', None)
	parameter_model = registered_action.param_model(**action_parameters)
	action_model_type = tools.registry.create_action_model()
	action_model = action_model_type.model_validate({action_name: parameter_model})
	return await tools.act(action_model, browser_session=browser_session, **injected_arguments)
