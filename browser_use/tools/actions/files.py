import logging
from typing import TYPE_CHECKING

from browser_use.agent.results import ActionResult
from browser_use.filesystem.file_system import FileSystem

if TYPE_CHECKING:
	from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.tools.service')


def register_file_actions(tools: 'Tools') -> None:
	"""Register managed file reads and writes."""
	# File System Actions

	@tools.registry.action(
		'Write content to a file. By default this OVERWRITES the entire file - use append=true to add to an existing file, or use replace_file for targeted edits within a file. '
		'FILENAME RULES: Use only letters, numbers, underscores, hyphens, dots, parentheses. Spaces are auto-converted to hyphens. '
		'SUPPORTED EXTENSIONS: .txt, .md, .json, .jsonl, .csv, .html, .xml, .pdf, .docx. '
		'CANNOT write binary/image files (.png, .jpg, .mp4, etc.) - do not attempt to save screenshots as files. '
		'For PDF files, write content in markdown format and it will be auto-converted to PDF.'
	)
	async def write_file(
		file_name: str,
		content: str,
		file_system: FileSystem,
		append: bool = False,
		trailing_newline: bool = True,
		leading_newline: bool = False,
	):
		if trailing_newline:
			content += '\n'
		if leading_newline:
			content = '\n' + content
		if append:
			result = await file_system.append_file(file_name, content)
		else:
			result = await file_system.write_file(file_name, content)

		# Log the full path where the file is stored (use resolved name)
		resolved_name, _ = file_system._resolve_filename(file_name)
		file_path = file_system.get_dir() / resolved_name
		logger.info(f'💾 {result} File location: {file_path}')

		return ActionResult(extracted_content=result, long_term_memory=result)

	@tools.registry.action(
		'Replace specific text within a file by searching for old_str and replacing with new_str. Use this for targeted edits like updating todo checkboxes or modifying specific lines without rewriting the entire file.'
	)
	async def replace_file(file_name: str, old_str: str, new_str: str, file_system: FileSystem):
		result = await file_system.replace_file_str(file_name, old_str, new_str)
		logger.info(f'💾 {result}')
		return ActionResult(extracted_content=result, long_term_memory=result)

	@tools.registry.action(
		'Read the complete content of a file. Use this to view file contents before editing or to retrieve data from files. Supports text files (txt, md, json, csv, jsonl), documents (pdf, docx), and images (jpg, png).'
	)
	async def read_file(file_name: str, available_file_paths: list[str], file_system: FileSystem):
		if available_file_paths and file_name in available_file_paths:
			structured_result = await file_system.read_file_structured(file_name, external_file=True)
		else:
			structured_result = await file_system.read_file_structured(file_name)

		result = structured_result['message']
		images = structured_result.get('images')

		MAX_MEMORY_SIZE = 1000
		# For images, create a shorter memory message
		if images:
			memory = f'Read image file {file_name}'
		elif len(result) > MAX_MEMORY_SIZE:
			lines = result.splitlines()
			display = ''
			lines_count = 0
			for line in lines:
				if len(display) + len(line) < MAX_MEMORY_SIZE:
					display += line + '\n'
					lines_count += 1
				else:
					break
			remaining_lines = len(lines) - lines_count
			memory = f'{display}{remaining_lines} more lines...' if remaining_lines > 0 else display
		else:
			memory = result
		logger.info(f'💾 {memory}')
		return ActionResult(
			extracted_content=result,
			long_term_memory=memory,
			images=images,
			include_extracted_content_only_once=True,
		)
