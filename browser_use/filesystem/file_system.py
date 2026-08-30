import os
import re
import shutil
from pathlib import Path
from typing import Any

from browser_use.filesystem.file_types import (
	DEFAULT_FILE_SYSTEM_PATH,
	BaseFile,
	CsvFile,
	DocxFile,
	FileSystemError,
	FileSystemState,
	HtmlFile,
	JsonFile,
	JsonlFile,
	MarkdownFile,
	PdfFile,
	TxtFile,
	XmlFile,
	_build_filename_error_message,
)
from browser_use.filesystem.structured_reader import StructuredFileReader


class FileSystem:
	"""Enhanced file system with in-memory storage and multiple file type support"""

	def __init__(self, base_dir: str | Path, create_default_files: bool = True):
		# Handle the Path conversion before calling super().__init__
		self.base_dir = Path(base_dir) if isinstance(base_dir, str) else base_dir
		self.base_dir.mkdir(parents=True, exist_ok=True)

		# Create and use a dedicated subfolder for all operations
		self.data_dir = self.base_dir / DEFAULT_FILE_SYSTEM_PATH
		self.data_dir.mkdir(exist_ok=True)
		self.structured_reader = StructuredFileReader(self)

		self._file_types: dict[str, type[BaseFile]] = {
			'md': MarkdownFile,
			'txt': TxtFile,
			'json': JsonFile,
			'jsonl': JsonlFile,
			'csv': CsvFile,
			'pdf': PdfFile,
			'docx': DocxFile,
			'html': HtmlFile,
			'xml': XmlFile,
		}

		self.files = {}
		if create_default_files:
			self.default_files = ['todo.md']
			self._create_default_files()

		self.extracted_content_count = 0

	def get_allowed_extensions(self) -> list[str]:
		"""Get allowed extensions"""
		return list(self._file_types.keys())

	def _get_file_type_class(self, extension: str) -> type[BaseFile] | None:
		"""Get the appropriate file class for an extension."""
		return self._file_types.get(extension.lower(), None)

	def _create_default_files(self) -> None:
		"""Create default results and todo files"""
		for full_filename in self.default_files:
			# Reusing a filesystem path must not overwrite or import prior output.
			# Existing disk files remain deliberately outside this instance's index.
			if (self.data_dir / full_filename).exists():
				continue
			name_without_ext, extension = self._parse_filename(full_filename)
			file_class = self._get_file_type_class(extension)
			if not file_class:
				raise ValueError(f"Error: Invalid file extension '{extension}' for file '{full_filename}'.")

			file_obj = file_class(name=name_without_ext)
			self.files[full_filename] = file_obj  # Use full filename as key
			file_obj.sync_to_disk_sync(self.data_dir)

	def _is_valid_filename(self, file_name: str) -> bool:
		"""Check if filename matches the required pattern: name.extension

		Allows letters, numbers, underscores, hyphens, dots, parentheses, spaces, and Chinese characters
		in the name part, followed by a dot and a supported extension.
		"""
		extensions = '|'.join(self._file_types.keys())
		# Allow dots, spaces, parens in the name part - match everything up to the last dot
		pattern = rf'^[a-zA-Z0-9_\-\.\(\) \u4e00-\u9fff]+\.({extensions})$'
		file_name_base = os.path.basename(file_name)
		if not re.match(pattern, file_name_base):
			return False
		# Ensure the name part (before last dot) is non-empty
		name_part = file_name_base.rsplit('.', 1)[0]
		return len(name_part.strip()) > 0

	@staticmethod
	def sanitize_filename(file_name: str) -> str:
		"""Sanitize a filename by replacing/removing invalid characters.

		- Replaces spaces with hyphens
		- Removes characters that are not alphanumeric, underscore, hyphen, dot, parentheses, or Chinese
		- Preserves the extension
		- Collapses multiple consecutive hyphens
		"""
		base = os.path.basename(file_name)
		if '.' not in base:
			return base

		name_part, ext = base.rsplit('.', 1)
		# Replace spaces with hyphens
		name_part = name_part.replace(' ', '-')
		# Remove invalid characters (keep alphanumeric, underscore, hyphen, dot, parens, Chinese)
		name_part = re.sub(r'[^a-zA-Z0-9_\-\.\(\)\u4e00-\u9fff]', '', name_part)
		# Collapse multiple hyphens
		name_part = re.sub(r'-{2,}', '-', name_part)
		# Strip leading/trailing hyphens and dots
		name_part = name_part.strip('-.')

		if not name_part:
			name_part = 'file'

		return f'{name_part}.{ext.lower()}'

	def _resolve_filename(self, file_name: str) -> tuple[str, bool]:
		"""Resolve a filename, attempting sanitization if the original is invalid.

		Normalizes to basename first to prevent directory traversal (e.g. ../secret.md).

		Returns:
			(resolved_name, was_changed): The resolved filename and whether it differs from the input.
			If resolution fails, returns (basename, was_changed).
		"""
		base_name = os.path.basename(file_name)
		was_changed = base_name != file_name

		if self._is_valid_filename(base_name):
			return base_name, was_changed

		sanitized = self.sanitize_filename(base_name)
		if sanitized != base_name and self._is_valid_filename(sanitized):
			return sanitized, True

		return base_name, was_changed

	def _parse_filename(self, filename: str) -> tuple[str, str]:
		"""Parse filename into name and extension. Always check _is_valid_filename first."""
		name, extension = filename.rsplit('.', 1)
		return name, extension.lower()

	def get_dir(self) -> Path:
		"""Get the file system directory"""
		return self.data_dir

	def get_file(self, full_filename: str) -> BaseFile | None:
		"""Get a file object by full filename, trying sanitization if the name is invalid."""
		resolved, _ = self._resolve_filename(full_filename)
		if not self._is_valid_filename(resolved):
			return None

		# Use resolved filename as key
		return self.files.get(resolved)

	def list_files(self) -> list[str]:
		"""List all files in the system"""
		return [file_obj.full_name for file_obj in self.files.values()]

	def display_file(self, full_filename: str) -> str | None:
		"""Display file content using file-specific display method"""
		resolved, _ = self._resolve_filename(full_filename)
		if not self._is_valid_filename(resolved):
			return None

		file_obj = self.files.get(resolved)
		if not file_obj:
			return None

		return file_obj.read()

	async def read_file_structured(self, full_filename: str, external_file: bool = False) -> dict[str, Any]:
		return await self.structured_reader.read_file_structured(full_filename, external_file)

	async def read_file(self, full_filename: str, external_file: bool = False) -> str:
		"""Read file content using file-specific read method and return appropriate message to LLM.

		Note: For image files, use read_file_structured() to get image data.
		"""
		result = await self.read_file_structured(full_filename, external_file)
		return result['message']

	async def write_file(self, full_filename: str, content: str) -> str:
		"""Write content to file using file-specific write method"""
		original_filename = full_filename
		resolved, was_sanitized = self._resolve_filename(full_filename)
		if not self._is_valid_filename(resolved):
			return _build_filename_error_message(full_filename, self.get_allowed_extensions())
		full_filename = resolved

		try:
			name_without_ext, extension = self._parse_filename(full_filename)
			file_class = self._get_file_type_class(extension)
			if not file_class:
				raise ValueError(f"Error: Invalid file extension '{extension}' for file '{full_filename}'.")

			# Create or get existing file using full filename as key
			if full_filename in self.files:
				file_obj = self.files[full_filename]
			else:
				file_obj = file_class(name=name_without_ext)
				self.files[full_filename] = file_obj  # Use full filename as key

			# Use file-specific write method
			await file_obj.write(content, self.data_dir)
			sanitize_note = f" (auto-corrected from '{original_filename}')" if was_sanitized else ''
			return f'Data written to file {full_filename} successfully.{sanitize_note}'
		except FileSystemError as e:
			return str(e)
		except Exception as e:
			return f"Error: Could not write to file '{full_filename}'. {str(e)}"

	async def append_file(self, full_filename: str, content: str) -> str:
		"""Append content to file using file-specific append method"""
		original_filename = full_filename
		resolved, was_sanitized = self._resolve_filename(full_filename)
		if not self._is_valid_filename(resolved):
			return _build_filename_error_message(full_filename, self.get_allowed_extensions())
		full_filename = resolved

		file_obj = self.files.get(full_filename)
		if not file_obj:
			if was_sanitized:
				return f"File '{full_filename}' not found. (Filename was auto-corrected from '{original_filename}')"
			return f"File '{full_filename}' not found."

		try:
			await file_obj.append(content, self.data_dir)
			sanitize_note = f" (auto-corrected from '{original_filename}')" if was_sanitized else ''
			return f'Data appended to file {full_filename} successfully.{sanitize_note}'
		except FileSystemError as e:
			return str(e)
		except Exception as e:
			return f"Error: Could not append to file '{full_filename}'. {str(e)}"

	async def replace_file_str(self, full_filename: str, old_str: str, new_str: str) -> str:
		"""Replace old_str with new_str in file_name"""
		original_filename = full_filename
		resolved, was_sanitized = self._resolve_filename(full_filename)
		if not self._is_valid_filename(resolved):
			return _build_filename_error_message(full_filename, self.get_allowed_extensions())
		full_filename = resolved

		if not old_str:
			return 'Error: Cannot replace empty string. Please provide a non-empty string to replace.'

		file_obj = self.files.get(full_filename)
		if not file_obj:
			if was_sanitized:
				return f"File '{full_filename}' not found. (Filename was auto-corrected from '{original_filename}')"
			return f"File '{full_filename}' not found."

		try:
			content = file_obj.read()
			content = content.replace(old_str, new_str)
			await file_obj.write(content, self.data_dir)
			sanitize_note = f" (auto-corrected from '{original_filename}')" if was_sanitized else ''
			return f'Successfully replaced all occurrences of "{old_str}" with "{new_str}" in file {full_filename}{sanitize_note}'
		except FileSystemError as e:
			return str(e)
		except Exception as e:
			return f"Error: Could not replace string in file '{full_filename}'. {str(e)}"

	async def save_extracted_content(self, content: str) -> str:
		"""Save extracted content to a numbered file"""
		while True:
			initial_filename = f'extracted_content_{self.extracted_content_count}'
			extracted_filename = f'{initial_filename}.md'
			extracted_path = self.data_dir / extracted_filename
			if extracted_filename not in self.files and not os.path.lexists(extracted_path):
				break
			self.extracted_content_count += 1

		file_obj = MarkdownFile(name=initial_filename)
		await file_obj.write(content, self.data_dir)
		self.files[extracted_filename] = file_obj
		self.extracted_content_count += 1
		return extracted_filename

	def describe(self) -> str:
		"""List all files with their content information using file-specific display methods"""
		DISPLAY_CHARS = 400
		description = ''

		for file_obj in self.files.values():
			# Skip todo.md from description
			if file_obj.full_name == 'todo.md':
				continue

			content = file_obj.read()

			# Handle empty files
			if not content:
				description += f'<file>\n{file_obj.full_name} - [empty file]\n</file>\n'
				continue

			lines = content.splitlines()
			line_count = len(lines)

			# For small files, display the entire content
			whole_file_description = (
				f'<file>\n{file_obj.full_name} - {line_count} lines\n<content>\n{content}\n</content>\n</file>\n'
			)
			if len(content) < int(1.5 * DISPLAY_CHARS):
				description += whole_file_description
				continue

			# For larger files, display start and end previews
			half_display_chars = DISPLAY_CHARS // 2

			# Get start preview
			start_preview = ''
			start_line_count = 0
			chars_count = 0
			for line in lines:
				if chars_count + len(line) + 1 > half_display_chars:
					break
				start_preview += line + '\n'
				chars_count += len(line) + 1
				start_line_count += 1

			# Get end preview
			end_preview = ''
			end_line_count = 0
			chars_count = 0
			for line in reversed(lines):
				if chars_count + len(line) + 1 > half_display_chars:
					break
				end_preview = line + '\n' + end_preview
				chars_count += len(line) + 1
				end_line_count += 1

			# Calculate lines in between
			middle_line_count = line_count - start_line_count - end_line_count
			if middle_line_count <= 0:
				description += whole_file_description
				continue

			start_preview = start_preview.strip('\n').rstrip()
			end_preview = end_preview.strip('\n').rstrip()

			# Format output
			if not (start_preview or end_preview):
				description += f'<file>\n{file_obj.full_name} - {line_count} lines\n<content>\n{middle_line_count} lines...\n</content>\n</file>\n'
			else:
				description += f'<file>\n{file_obj.full_name} - {line_count} lines\n<content>\n{start_preview}\n'
				description += f'... {middle_line_count} more lines ...\n'
				description += f'{end_preview}\n'
				description += '</content>\n</file>\n'

		return description.strip('\n')

	def get_todo_contents(self) -> str:
		"""Get todo file contents"""
		todo_file = self.get_file('todo.md')
		return todo_file.read() if todo_file else ''

	def get_state(self) -> FileSystemState:
		"""Get serializable state of the file system"""
		files_data = {}
		for full_filename, file_obj in self.files.items():
			files_data[full_filename] = {'type': file_obj.__class__.__name__, 'data': file_obj.model_dump()}

		return FileSystemState(
			files=files_data, base_dir=str(self.base_dir), extracted_content_count=self.extracted_content_count
		)

	def nuke(self) -> None:
		"""Delete the file system directory"""
		shutil.rmtree(self.data_dir)

	@classmethod
	def from_state(cls, state: FileSystemState) -> 'FileSystem':
		"""Restore file system from serializable state at the exact same location"""
		# Create file system without default files
		fs = cls(base_dir=Path(state.base_dir), create_default_files=False)
		fs.extracted_content_count = state.extracted_content_count

		# Restore all files
		for full_filename, file_data in state.files.items():
			file_type = file_data['type']
			file_info = file_data['data']

			# Create the appropriate file object based on type
			file_type_map: dict[str, type[BaseFile]] = {
				'MarkdownFile': MarkdownFile,
				'TxtFile': TxtFile,
				'JsonFile': JsonFile,
				'JsonlFile': JsonlFile,
				'CsvFile': CsvFile,
				'PdfFile': PdfFile,
				'DocxFile': DocxFile,
				'HtmlFile': HtmlFile,
				'XmlFile': XmlFile,
			}

			file_class = file_type_map.get(file_type)
			if not file_class:
				# Skip unknown file types
				continue
			file_obj = file_class(**file_info)

			# Add to files dict and sync to disk
			fs.files[full_filename] = file_obj
			file_obj.sync_to_disk_sync(fs.data_dir)

		return fs
