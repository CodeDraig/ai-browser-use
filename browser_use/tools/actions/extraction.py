import asyncio
import json
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.dom.utils import sanitize_surrogates
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm.base import BaseChatModel
from browser_use.llm.messages import SystemMessage, UserMessage
from browser_use.tools.views import ExtractAction

if TYPE_CHECKING:
	from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.tools.service')


def register_extraction_action(tools: 'Tools') -> None:
	"""Register LLM-backed page extraction."""

	@tools.registry.action(
		"""LLM extracts structured data from page markdown. Use when: on right page, know what to extract, haven't called before on same page+query. Can't get interactive elements. Set extract_links=True for URLs. Set extract_images=True for image src URLs. Use start_from_char if previous extraction was truncated to extract data further down the page. When paginating across pages, pass already_collected with item identifiers (names/URLs) from prior pages to avoid duplicates.""",
		param_model=ExtractAction,
	)
	async def extract(
		params: ExtractAction,
		browser_session: BrowserSession,
		page_extraction_llm: BaseChatModel,
		file_system: FileSystem,
		extraction_schema: dict | None = None,
	):
		# Constants
		MAX_CHAR_LIMIT = 100000
		query = params.query
		extract_links = params.extract_links
		extract_images = params.extract_images
		start_from_char = params.start_from_char
		output_schema = params.output_schema
		already_collected = params.already_collected

		# Auto-enable extract_images if query contains image-related keywords
		_IMAGE_KEYWORDS = ['image', 'photo', 'picture', 'thumbnail', 'img url', 'image url', 'photo url', 'product image']
		if not extract_images and any(kw in query.lower() for kw in _IMAGE_KEYWORDS):
			extract_images = True

		# If the LLM didn't provide an output_schema, use the agent-injected extraction_schema
		if output_schema is None and extraction_schema is not None:
			output_schema = extraction_schema

		# Attempt to convert output_schema to a pydantic model upfront; fall back to free-text on failure
		structured_model: type[BaseModel] | None = None
		if output_schema is not None:
			try:
				from browser_use.tools.extraction.schema_utils import schema_dict_to_pydantic_model

				structured_model = schema_dict_to_pydantic_model(output_schema)
			except (ValueError, TypeError) as exc:
				logger.warning(f'Invalid output_schema, falling back to free-text extraction: {exc}')
				output_schema = None

		# Extract clean markdown using the unified method
		try:
			from browser_use.dom.markdown_extractor import extract_clean_markdown

			content, content_stats = await extract_clean_markdown(
				browser_session=browser_session, extract_links=extract_links, extract_images=extract_images
			)
		except Exception as e:
			raise RuntimeError(f'Could not extract clean markdown: {type(e).__name__}')

		# Original content length for processing
		final_filtered_length = content_stats['final_filtered_chars']

		# Structure-aware chunking replaces naive char-based truncation
		from browser_use.dom.markdown_extractor import chunk_markdown_by_structure

		chunks = chunk_markdown_by_structure(content, max_chunk_chars=MAX_CHAR_LIMIT, start_from_char=start_from_char)
		if not chunks:
			return ActionResult(
				error=f'start_from_char ({start_from_char}) exceeds content length {final_filtered_length} characters.'
			)
		chunk = chunks[0]
		content = chunk.content
		truncated = chunk.has_more

		# Prepend overlap context for continuation chunks (e.g. table headers)
		if chunk.overlap_prefix:
			content = chunk.overlap_prefix + '\n' + content

		if start_from_char > 0:
			content_stats['started_from_char'] = start_from_char
		if truncated:
			content_stats['truncated_at_char'] = chunk.char_offset_end
			content_stats['next_start_char'] = chunk.char_offset_end
			content_stats['chunk_index'] = chunk.chunk_index
			content_stats['total_chunks'] = chunk.total_chunks

		# Add content statistics to the result
		original_html_length = content_stats['original_html_chars']
		initial_markdown_length = content_stats['initial_markdown_chars']
		chars_filtered = content_stats['filtered_chars_removed']

		stats_summary = f"""Content processed: {original_html_length:,} HTML chars → {initial_markdown_length:,} initial markdown → {final_filtered_length:,} filtered markdown"""
		if start_from_char > 0:
			stats_summary += f' (started from char {start_from_char:,})'
		if truncated:
			chunk_info = f'chunk {chunk.chunk_index + 1} of {chunk.total_chunks}, '
			stats_summary += f' → {len(content):,} final chars ({chunk_info}use start_from_char={content_stats["next_start_char"]} to continue)'
		elif chars_filtered > 0:
			stats_summary += f' (filtered {chars_filtered:,} chars of noise)'

		# Sanitize surrogates from content to prevent UTF-8 encoding errors
		content = sanitize_surrogates(content)
		query = sanitize_surrogates(query)

		# --- Structured extraction path ---
		if structured_model is not None:
			assert output_schema is not None
			system_prompt = """
You are an expert at extracting structured data from the markdown of a webpage.

<input>
You will be given a query, a JSON Schema, and the markdown of a webpage that has been filtered to remove noise and advertising content.
</input>

<instructions>
- Extract ONLY information present in the webpage. Do not guess or fabricate values.
- Your response MUST conform to the provided JSON Schema exactly.
- If a required field's value cannot be found on the page, use null (if the schema allows it) or an empty string / empty array as appropriate.
- If the content was truncated, extract what is available from the visible portion.
- If <already_collected> items are provided, skip any items whose name/title/URL matches those listed — do not include duplicates.
</instructions>
""".strip()

			schema_json = json.dumps(output_schema, indent=2)
			already_collected_section = ''
			if already_collected:
				items_str = '\n'.join(f'- {item}' for item in already_collected[:100])
				already_collected_section = f'\n\n<already_collected>\nSkip items whose name/title/URL matches any of these already-collected identifiers:\n{items_str}\n</already_collected>'
			prompt = (
				f'<query>\n{query}\n</query>\n\n'
				f'<output_schema>\n{schema_json}\n</output_schema>\n\n'
				f'<content_stats>\n{stats_summary}\n</content_stats>\n\n'
				f'<webpage_content>\n{content}\n</webpage_content>' + already_collected_section
			)

			try:
				response = await asyncio.wait_for(
					page_extraction_llm.ainvoke(
						[SystemMessage(content=system_prompt), UserMessage(content=prompt)],
						output_format=structured_model,
					),
					timeout=120.0,
				)

				# response.completion is a pydantic model instance
				result_data: dict = response.completion.model_dump(mode='json')  # type: ignore[union-attr]
				result_json = json.dumps(result_data)

				current_url = await browser_session.get_current_page_url()
				extracted_content = f'<url>\n{current_url}\n</url>\n<query>\n{query}\n</query>\n<structured_result>\n{result_json}\n</structured_result>'

				from browser_use.tools.extraction.views import ExtractionResult

				extraction_meta = ExtractionResult(
					data=result_data,
					schema_used=output_schema,
					is_partial=truncated,
					source_url=current_url,
					content_stats=content_stats,
				)

				# Simple memory handling
				MAX_MEMORY_LENGTH = 10000
				if len(extracted_content) < MAX_MEMORY_LENGTH:
					memory = extracted_content
					include_extracted_content_only_once = False
				else:
					file_name = await file_system.save_extracted_content(extracted_content)
					memory = f'Query: {query}\nContent in {file_name} and once in <read_state>.'
					include_extracted_content_only_once = True

				logger.info(f'📄 {memory}')
				return ActionResult(
					extracted_content=extracted_content,
					include_extracted_content_only_once=include_extracted_content_only_once,
					long_term_memory=memory,
					metadata={'structured_extraction': True, 'extraction_result': extraction_meta.model_dump(mode='json')},
				)
			except Exception as e:
				logger.debug(f'Error in structured extraction: {e}')
				raise RuntimeError(str(e))

		# --- Free-text extraction path (default) ---
		system_prompt = """
You are an expert at extracting data from the markdown of a webpage.

<input>
You will be given a query and the markdown of a webpage that has been filtered to remove noise and advertising content.
</input>

<instructions>
- You are tasked to extract information from the webpage that is relevant to the query.
- You should ONLY use the information available in the webpage to answer the query. Do not make up information or provide guess from your own knowledge.
- If the information relevant to the query is not available in the page, your response should mention that.
- If the query asks for all items, products, etc., make sure to directly list all of them.
- If the content was truncated and you need more information, note that the user can use start_from_char parameter to continue from where truncation occurred.
- If <already_collected> items are provided, exclude any results whose name/title/URL matches those already collected — do not include duplicates.
</instructions>

<output>
- Your output should present ALL the information relevant to the query in a concise way.
- Do not answer in conversational format - directly output the relevant information or that the information is unavailable.
</output>
""".strip()

		already_collected_section = ''
		if already_collected:
			items_str = '\n'.join(f'- {item}' for item in already_collected[:100])
			already_collected_section = f'\n\n<already_collected>\nSkip items whose name/title/URL matches any of these already-collected identifiers:\n{items_str}\n</already_collected>'
		prompt = (
			f'<query>\n{query}\n</query>\n\n<content_stats>\n{stats_summary}\n</content_stats>\n\n<webpage_content>\n{content}\n</webpage_content>'
			+ already_collected_section
		)

		try:
			response = await asyncio.wait_for(
				page_extraction_llm.ainvoke([SystemMessage(content=system_prompt), UserMessage(content=prompt)]),
				timeout=120.0,
			)

			current_url = await browser_session.get_current_page_url()
			extracted_content = (
				f'<url>\n{current_url}\n</url>\n<query>\n{query}\n</query>\n<result>\n{response.completion}\n</result>'
			)

			# Simple memory handling
			MAX_MEMORY_LENGTH = 10000
			if len(extracted_content) < MAX_MEMORY_LENGTH:
				memory = extracted_content
				include_extracted_content_only_once = False
			else:
				file_name = await file_system.save_extracted_content(extracted_content)
				memory = f'Query: {query}\nContent in {file_name} and once in <read_state>.'
				include_extracted_content_only_once = True

			logger.info(f'📄 {memory}')
			return ActionResult(
				extracted_content=extracted_content,
				include_extracted_content_only_once=include_extracted_content_only_once,
				long_term_memory=memory,
			)
		except Exception as e:
			logger.debug(f'Error extracting content: {e}')
			raise RuntimeError(str(e))
