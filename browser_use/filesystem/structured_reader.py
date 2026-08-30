from __future__ import annotations

import base64
import os
from typing import TYPE_CHECKING, Any

from browser_use.filesystem.file_types import FileSystemError, _build_filename_error_message

if TYPE_CHECKING:
	from browser_use.filesystem.file_system import FileSystem


class StructuredFileReader:
	"""Read workspace and external files into structured text/image results."""

	def __init__(self, file_system: FileSystem) -> None:
		self.file_system = file_system

	async def read_file_structured(self, full_filename: str, external_file: bool = False) -> dict[str, Any]:
		"""Read file and return structured data including images if applicable.

		Returns:
			dict with keys:
				- 'message': str - The message to display
				- 'images': list[dict] | None - Image data if file is an image: [{"name": str, "data": base64_str}]
		"""
		result: dict[str, Any] = {'message': '', 'images': None}

		if external_file:
			try:
				try:
					_, extension = self.file_system._parse_filename(full_filename)
				except Exception:
					result['message'] = (
						f'Error: Invalid filename format {full_filename}. Must be alphanumeric with a supported extension.'
					)
					return result

				# Text-based extensions: derive from _file_types, excluding those with special readers
				_special_extensions = {'docx', 'pdf', 'jpg', 'jpeg', 'png'}
				text_extensions = [ext for ext in self.file_system._file_types if ext not in _special_extensions]

				if extension in text_extensions:
					import anyio

					async with await anyio.open_file(full_filename, 'r') as f:
						content = await f.read()
						result['message'] = f'Read from file {full_filename}.\n<content>\n{content}\n</content>'
						return result

				elif extension == 'docx':
					from docx import Document

					doc = Document(full_filename)
					content = '\n'.join([para.text for para in doc.paragraphs])
					result['message'] = f'Read from file {full_filename}.\n<content>\n{content}\n</content>'
					return result

				elif extension == 'pdf':
					import pypdf

					reader = pypdf.PdfReader(full_filename)
					num_pages = len(reader.pages)
					MAX_CHARS = 60000  # character-based limit

					# Extract text from all pages with page markers
					page_texts: list[tuple[int, str]] = []
					total_chars = 0
					for i, page in enumerate(reader.pages, 1):
						text = page.extract_text() or ''
						page_texts.append((i, text))
						total_chars += len(text)

					# If small enough, return everything
					if total_chars <= MAX_CHARS:
						content_parts = []
						for page_num, text in page_texts:
							if text.strip():
								content_parts.append(f'--- Page {page_num} ---\n{text}')
						extracted_text = '\n\n'.join(content_parts)
						result['message'] = (
							f'Read from file {full_filename} ({num_pages} pages, {total_chars:,} chars).\n'
							f'<content>\n{extracted_text}\n</content>'
						)
						return result

					# Large PDF - use search to prioritize pages with distinctive content
					import math
					import re

					# Extract words from each page and count which pages they appear on
					word_to_pages: dict[str, set[int]] = {}
					page_words: dict[int, set[str]] = {}

					for page_num, text in page_texts:
						# Extract words (lowercase, 4+ chars to filter noise)
						words = set(re.findall(r'\b[a-zA-Z]{4,}\b', text.lower()))
						page_words[page_num] = words
						for word in words:
							if word not in word_to_pages:
								word_to_pages[word] = set()
							word_to_pages[word].add(page_num)

					# Score pages using inverse document frequency (IDF)
					# words appearing on fewer pages get higher weight
					page_scores: dict[int, float] = {}
					for page_num, words in page_words.items():
						score = 0.0
						for word in words:
							pages_with_word = len(word_to_pages[word])
							# IDF: log(total_pages / pages_with_word) - higher for rarer words
							score += math.log(num_pages / pages_with_word)
						page_scores[page_num] = score

					# Sort pages by score (highest first), always include page 1
					sorted_pages = sorted(page_scores.items(), key=lambda x: -x[1])
					priority_pages = [1]
					for page_num, _ in sorted_pages:
						if page_num not in priority_pages:
							priority_pages.append(page_num)

					# Add remaining pages in order (for pages with no distinctive content)
					for page_num, _ in page_texts:
						if page_num not in priority_pages:
							priority_pages.append(page_num)

					# Build content from prioritized pages, respecting char limit
					content_parts = []
					chars_used = 0
					pages_included = []

					# First pass: add pages in priority order
					for page_num in priority_pages:
						text = page_texts[page_num - 1][1]
						if not text.strip():
							continue
						page_header = f'--- Page {page_num} ---\n'
						truncation_suffix = '\n[...truncated]'
						remaining = MAX_CHARS - chars_used
						# Need room for header + suffix + at least some content
						min_useful = len(page_header) + len(truncation_suffix) + 50
						if remaining < min_useful:
							break  # no room left for meaningful content
						page_content = page_header + text
						if len(page_content) > remaining:
							# Truncate page to fit remaining budget exactly
							page_content = page_content[: remaining - len(truncation_suffix)] + truncation_suffix
						content_parts.append((page_num, page_content))
						chars_used += len(page_content)
						pages_included.append(page_num)
						if chars_used >= MAX_CHARS:
							break

					# Sort included pages by page number for readability
					content_parts.sort(key=lambda x: x[0])
					extracted_text = '\n\n'.join(part for _, part in content_parts)

					pages_not_shown = num_pages - len(pages_included)
					if pages_not_shown > 0:
						skipped = [p for p in range(1, num_pages + 1) if p not in pages_included]
						truncation_note = (
							f'\n\n[Showing {len(pages_included)} of {num_pages} pages. '
							f'Skipped pages: {skipped[:10]}{"..." if len(skipped) > 10 else ""}. '
							f'Use extract with start_from_char to read further into the file.]'
						)
					else:
						truncation_note = ''

					result['message'] = (
						f'Read from file {full_filename} ({num_pages} pages, {total_chars:,} chars total).\n'
						f'<content>\n{extracted_text}{truncation_note}\n</content>'
					)
					return result

				elif extension in ['jpg', 'jpeg', 'png']:
					import anyio

					# Read image file and convert to base64
					async with await anyio.open_file(full_filename, 'rb') as f:
						img_data = await f.read()

					base64_str = base64.b64encode(img_data).decode('utf-8')

					result['message'] = f'Read image file {full_filename}.'
					result['images'] = [{'name': os.path.basename(full_filename), 'data': base64_str}]
					return result

				else:
					result['message'] = f'Error: Cannot read file {full_filename} as {extension} extension is not supported.'
					return result

			except FileNotFoundError:
				result['message'] = f"Error: File '{full_filename}' not found."
				return result
			except PermissionError:
				result['message'] = f"Error: Permission denied to read file '{full_filename}'."
				return result
			except Exception as e:
				result['message'] = f"Error: Could not read file '{full_filename}'. {str(e)}"
				return result

		# For internal files, only non-image types are supported
		resolved, was_sanitized = self.file_system._resolve_filename(full_filename)
		if not self.file_system._is_valid_filename(resolved):
			result['message'] = _build_filename_error_message(full_filename, self.file_system.get_allowed_extensions())
			return result

		file_obj = self.file_system.files.get(resolved)
		if not file_obj:
			if was_sanitized:
				result['message'] = f"File '{resolved}' not found. (Filename was auto-corrected from '{full_filename}')"
			else:
				result['message'] = f"File '{full_filename}' not found."
			return result

		try:
			content = file_obj.read()
			sanitize_note = f"Note: filename was auto-corrected from '{full_filename}' to '{resolved}'. " if was_sanitized else ''
			result['message'] = f'{sanitize_note}Read from file {resolved}.\n<content>\n{content}\n</content>'
			return result
		except FileSystemError as e:
			result['message'] = str(e)
			return result
		except Exception as e:
			result['message'] = f"Error: Could not read file '{full_filename}'. {str(e)}"
			return result
