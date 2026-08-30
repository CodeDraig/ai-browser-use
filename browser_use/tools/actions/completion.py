import json
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from browser_use.agent.results import ActionResult
from browser_use.browser import BrowserSession
from browser_use.filesystem.file_system import FileSystem
from browser_use.tools.views import DoneAction, StructuredOutputAction

if TYPE_CHECKING:
	from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.tools.service')


def register_done_action(tools: 'Tools', output_model: type[BaseModel] | None, display_files_in_done_text: bool = True):
	if output_model is not None:
		tools.display_files_in_done_text = display_files_in_done_text

		@tools.registry.action(
			'Complete task with structured output.',
			param_model=StructuredOutputAction[output_model],
		)
		async def done(params: StructuredOutputAction, file_system: FileSystem, browser_session: BrowserSession):
			# Exclude success from the output JSON
			# Use mode='json' to properly serialize enums at all nesting levels
			output_dict = params.data.model_dump(mode='json')

			attachments: list[str] = []

			# 1. Resolve any explicitly requested files via files_to_display
			if params.files_to_display:
				for file_name in params.files_to_display:
					file_content = file_system.display_file(file_name)
					if file_content:
						attachments.append(str(file_system.get_dir() / file_name))

			# 2. Auto-attach actual session downloads (CDP-tracked browser downloads)
			#    but NOT user-supplied whitelist paths from available_file_paths
			session_downloads = browser_session.downloaded_files
			if session_downloads:
				existing = set(attachments)
				for file_path in session_downloads:
					if file_path not in existing:
						attachments.append(file_path)

			return ActionResult(
				is_done=True,
				success=params.success,
				extracted_content=json.dumps(output_dict, ensure_ascii=False),
				long_term_memory=f'Task completed. Success Status: {params.success}',
				attachments=attachments,
			)

	else:

		@tools.registry.action(
			'Complete task. Only report actions you performed and data you extracted in this session.',
			param_model=DoneAction,
		)
		async def done(params: DoneAction, file_system: FileSystem):
			user_message = params.text

			len_text = len(params.text)
			len_max_memory = 100
			memory = f'Task completed: {params.success} - {params.text[:len_max_memory]}'
			if len_text > len_max_memory:
				memory += f' - {len_text - len_max_memory} more characters'

			attachments = []
			if params.files_to_display:
				if tools.display_files_in_done_text:
					file_msg = ''
					for file_name in params.files_to_display:
						file_content = file_system.display_file(file_name)
						if file_content:
							file_msg += f'\n\n{file_name}:\n{file_content}'
							attachments.append(file_name)
					if file_msg:
						user_message += '\n\nAttachments:'
						user_message += file_msg
					else:
						logger.warning('Agent wanted to display files but none were found')
				else:
					for file_name in params.files_to_display:
						file_content = file_system.display_file(file_name)
						if file_content:
							attachments.append(file_name)

			attachments = [str(file_system.get_dir() / file_name) for file_name in attachments]

			return ActionResult(
				is_done=True,
				success=params.success,
				extracted_content=user_message,
				long_term_memory=memory,
				attachments=attachments,
			)
