import logging

from browser_use.agent.results import ActionResult
from browser_use.browser.views import BrowserError

logger = logging.getLogger('browser_use.tools.service')


def handle_browser_error(error: BrowserError) -> ActionResult:
	"""Preserve structured browser error memory at the tools boundary."""
	if error.long_term_memory is not None:
		if error.short_term_memory is not None:
			return ActionResult(
				extracted_content=error.short_term_memory,
				error=error.long_term_memory,
				include_extracted_content_only_once=True,
			)
		return ActionResult(error=error.long_term_memory)

	logger.warning(
		'⚠️ A BrowserError was raised without long_term_memory - always set long_term_memory when raising BrowserError to propagate right messages to LLM.'
	)
	raise error
