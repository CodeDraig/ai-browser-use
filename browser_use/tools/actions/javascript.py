import json
import logging
from typing import TYPE_CHECKING

from browser_use.agent.results import ActionResult
from browser_use.browser import BrowserSession

if TYPE_CHECKING:
	from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.tools.service')


def _validate_and_fix_javascript(code: str) -> str:
	"""Validate and fix common JavaScript issues before execution"""

	import re

	# Pattern 1: Fix double-escaped quotes (\\\" → \")
	fixed_code = re.sub(r'\\"', '"', code)

	# Pattern 2: Fix over-escaped regex patterns (\\\\d → \\d)
	# Common issue: regex gets double-escaped during parsing
	fixed_code = re.sub(r'\\\\([dDsSwWbBnrtfv])', r'\\\1', fixed_code)
	fixed_code = re.sub(r'\\\\([.*+?^${}()|[\]])', r'\\\1', fixed_code)

	# Pattern 3: Fix XPath expressions with mixed quotes
	xpath_pattern = r'document\.evaluate\s*\(\s*"([^"]*)"\s*,'

	def fix_xpath_quotes(match):
		xpath_with_quotes = match.group(1)
		return f'document.evaluate(`{xpath_with_quotes}`,'

	fixed_code = re.sub(xpath_pattern, fix_xpath_quotes, fixed_code)

	# Pattern 4: Fix querySelector/querySelectorAll with mixed quotes
	selector_pattern = r'(querySelector(?:All)?)\s*\(\s*"([^"]*)"\s*\)'

	def fix_selector_quotes(match):
		method_name = match.group(1)
		selector_with_quotes = match.group(2)
		return f'{method_name}(`{selector_with_quotes}`)'

	fixed_code = re.sub(selector_pattern, fix_selector_quotes, fixed_code)

	# Pattern 5: Fix closest() calls with mixed quotes
	closest_pattern = r'\.closest\s*\(\s*"([^"]*)"\s*\)'

	def fix_closest_quotes(match):
		selector_with_quotes = match.group(1)
		return f'.closest(`{selector_with_quotes}`)'

	fixed_code = re.sub(closest_pattern, fix_closest_quotes, fixed_code)

	# Pattern 6: Fix .matches() calls with mixed quotes (similar to closest)
	matches_pattern = r'\.matches\s*\(\s*"([^"]*)"\s*\)'

	def fix_matches_quotes(match):
		selector_with_quotes = match.group(1)
		return f'.matches(`{selector_with_quotes}`)'

	fixed_code = re.sub(matches_pattern, fix_matches_quotes, fixed_code)

	# Note: Removed getAttribute fix - attribute names rarely have mixed quotes
	# getAttribute typically uses simple names like "data-value", not complex selectors

	# Log changes made
	changes_made = []
	if r'\"' in code and r'\"' not in fixed_code:
		changes_made.append('fixed escaped quotes')
	if '`' in fixed_code and '`' not in code:
		changes_made.append('converted mixed quotes to template literals')

	if changes_made:
		logger.debug(f'JavaScript fixes applied: {", ".join(changes_made)}')

	return fixed_code


def register_javascript_action(tools: 'Tools') -> None:
	"""Register browser JavaScript evaluation."""

	@tools.registry.action(
		"""Execute browser JavaScript. Best practice: wrap in IIFE (function(){...})() with try-catch for safety. Use ONLY browser APIs (document, window, DOM). NO Node.js APIs (fs, require, process). Example: (function(){try{const el=document.querySelector('#id');return el?el.value:'not found'}catch(e){return 'Error: '+e.message}})() Avoid comments. Use for hover, drag, zoom, custom selectors, extract/filter links, or analysing page structure. IMPORTANT: Shadow DOM elements with [index] markers can be clicked directly with click(index) — do NOT use evaluate() to click them. Only use evaluate for shadow DOM elements that are NOT indexed. Limit output size.""",
		terminates_sequence=True,
	)
	async def evaluate(code: str, browser_session: BrowserSession):
		# Execute JavaScript with proper error handling and promise support

		cdp_session = await browser_session.get_or_create_cdp_session()

		try:
			# Validate and potentially fix JavaScript code before execution
			validated_code = _validate_and_fix_javascript(code)

			# Always use awaitPromise=True - it's ignored for non-promises
			result = await cdp_session.cdp_client.send.Runtime.evaluate(
				params={'expression': validated_code, 'returnByValue': True, 'awaitPromise': True},
				session_id=cdp_session.session_id,
			)

			# Check for JavaScript execution errors
			if result.get('exceptionDetails'):
				exception = result['exceptionDetails']
				error_msg = f'JavaScript execution error: {exception.get("text", "Unknown error")}'

				# Enhanced error message with debugging info
				enhanced_msg = f"""JavaScript Execution Failed:
{error_msg}

Validated Code (after quote fixing):
{validated_code[:500]}{'...' if len(validated_code) > 500 else ''}
"""

				logger.debug(enhanced_msg)
				return ActionResult(error=enhanced_msg)

			# Get the result data
			result_data = result.get('result', {})

			# Check for wasThrown flag (backup error detection)
			if result_data.get('wasThrown'):
				msg = f'JavaScript code: {code} execution failed (wasThrown=true)'
				logger.debug(msg)
				return ActionResult(error=msg)

			# Get the actual value
			value = result_data.get('value')

			# Handle different value types
			if value is None:
				# Could be legitimate null/undefined result
				result_text = str(value) if 'value' in result_data else 'undefined'
			elif isinstance(value, (dict, list)):
				# Complex objects - should be serialized by returnByValue
				try:
					result_text = json.dumps(value, ensure_ascii=False)
				except (TypeError, ValueError):
					# Fallback for non-serializable objects
					result_text = str(value)
			else:
				# Primitive values (string, number, boolean)
				result_text = str(value)

			import re

			image_pattern = r'(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)'
			found_images = re.findall(image_pattern, result_text)

			metadata = None
			if found_images:
				# Store images in metadata so they can be added as ContentPartImageParam
				metadata = {'images': found_images}

				# Replace image data in result text with shorter placeholder
				modified_text = result_text
				for i, img_data in enumerate(found_images, 1):
					placeholder = '[Image]'
					modified_text = modified_text.replace(img_data, placeholder)
				result_text = modified_text

			# Apply length limit with better truncation (after image extraction)
			if len(result_text) > 20000:
				result_text = result_text[:19950] + '\n... [Truncated after 20000 characters]'

			# Don't log the code - it's already visible in the user's cell
			logger.debug(f'JavaScript executed successfully, result length: {len(result_text)}')

			# Memory handling: keep full result in extracted_content for current step,
			# but use truncated version in long_term_memory if too large
			MAX_MEMORY_LENGTH = 10000
			if len(result_text) < MAX_MEMORY_LENGTH:
				memory = result_text
				include_extracted_content_only_once = False
			else:
				memory = f'JavaScript executed successfully, result length: {len(result_text)} characters.'
				include_extracted_content_only_once = True

			# Return only the result, not the code (code is already in user's cell)
			return ActionResult(
				extracted_content=result_text,
				long_term_memory=memory,
				include_extracted_content_only_once=include_extracted_content_only_once,
				metadata=metadata,
			)

		except Exception as e:
			# CDP communication or other system errors
			error_msg = f'Failed to execute JavaScript: {type(e).__name__}: {e}'
			logger.debug(f'JavaScript code that failed: {code[:200]}...')
			return ActionResult(error=error_msg)
