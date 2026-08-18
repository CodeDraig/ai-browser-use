import json
import logging
from typing import TYPE_CHECKING

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.tools.views import FindElementsAction, SearchPageAction

if TYPE_CHECKING:
	from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.tools.service')


_SEARCH_PAGE_JS_BODY = """\
try {
	var scope = CSS_SCOPE ? document.querySelector(CSS_SCOPE) : document.body;
	if (!scope) {
		return {error: 'CSS scope selector not found: ' + CSS_SCOPE, matches: [], total: 0};
	}
	var walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
	var fullText = '';
	var nodeOffsets = [];
	while (walker.nextNode()) {
		var node = walker.currentNode;
		var text = node.textContent;
		if (text && text.trim()) {
			nodeOffsets.push({offset: fullText.length, length: text.length, node: node});
			fullText += text;
		}
	}
	var re;
	try {
		var flags = CASE_SENSITIVE ? 'g' : 'gi';
		if (IS_REGEX) {
			re = new RegExp(PATTERN, flags);
		} else {
			re = new RegExp(PATTERN.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'), flags);
		}
	} catch (e) {
		return {error: 'Invalid regex pattern: ' + e.message, matches: [], total: 0};
	}
	var matches = [];
	var match;
	var totalFound = 0;
	while ((match = re.exec(fullText)) !== null) {
		totalFound++;
		if (matches.length < MAX_RESULTS) {
			var start = Math.max(0, match.index - CONTEXT_CHARS);
			var end = Math.min(fullText.length, match.index + match[0].length + CONTEXT_CHARS);
			var context = fullText.slice(start, end);
			var elementPath = '';
			for (var i = 0; i < nodeOffsets.length; i++) {
				var no = nodeOffsets[i];
				if (no.offset <= match.index && no.offset + no.length > match.index) {
					elementPath = _getPath(no.node.parentElement);
					break;
				}
			}
			matches.push({
				match_text: match[0],
				context: (start > 0 ? '...' : '') + context + (end < fullText.length ? '...' : ''),
				element_path: elementPath,
				char_position: match.index
			});
		}
		if (match[0].length === 0) re.lastIndex++;
	}
	return {matches: matches, total: totalFound, has_more: totalFound > MAX_RESULTS};
} catch (e) {
	return {error: 'search_page error: ' + e.message, matches: [], total: 0};
}
function _getPath(el) {
	var parts = [];
	var current = el;
	while (current && current !== document.body && current !== document) {
		var desc = current.tagName ? current.tagName.toLowerCase() : '';
		if (!desc) break;
		if (current.id) desc += '#' + current.id;
		else if (current.className && typeof current.className === 'string') {
			var classes = current.className.trim().split(/\\s+/).slice(0, 2).join('.');
			if (classes) desc += '.' + classes;
		}
		parts.unshift(desc);
		current = current.parentElement;
	}
	return parts.join(' > ');
}
"""

_FIND_ELEMENTS_JS_BODY = """\
try {
	var elements;
	try {
		elements = document.querySelectorAll(SELECTOR);
	} catch (e) {
		return {error: 'Invalid CSS selector: ' + e.message, elements: [], total: 0};
	}
	var total = elements.length;
	var limit = Math.min(total, MAX_RESULTS);
	var results = [];
	for (var i = 0; i < limit; i++) {
		var el = elements[i];
		var item = {index: i, tag: el.tagName.toLowerCase()};
		if (INCLUDE_TEXT) {
			var text = (el.textContent || '').trim();
			item.text = text.length > 300 ? text.slice(0, 300) + '...' : text;
		}
		if (ATTRIBUTES && ATTRIBUTES.length > 0) {
			item.attrs = {};
			for (var j = 0; j < ATTRIBUTES.length; j++) {
				var attrName = ATTRIBUTES[j];
				var val;
				// Use resolved DOM property for src/href to get absolute URLs
				if ((attrName === 'src' || attrName === 'href') && typeof el[attrName] === 'string' && el[attrName] !== '') {
					val = el[attrName];
				} else {
					val = el.getAttribute(attrName);
				}
				if (val !== null) {
					item.attrs[attrName] = val.length > 500 ? val.slice(0, 500) + '...' : val;
				}
			}
		}
		item.children_count = el.children.length;
		results.push(item);
	}
	return {elements: results, total: total, showing: limit};
} catch (e) {
	return {error: 'find_elements error: ' + e.message, elements: [], total: 0};
}
"""


def _build_search_page_js(
	pattern: str,
	regex: bool,
	case_sensitive: bool,
	context_chars: int,
	css_scope: str | None,
	max_results: int,
) -> str:
	"""Build JS IIFE for search_page with safe parameter injection."""
	params_js = (
		f'var PATTERN = {json.dumps(pattern)};\n'
		f'var IS_REGEX = {json.dumps(regex)};\n'
		f'var CASE_SENSITIVE = {json.dumps(case_sensitive)};\n'
		f'var CONTEXT_CHARS = {json.dumps(context_chars)};\n'
		f'var CSS_SCOPE = {json.dumps(css_scope)};\n'
		f'var MAX_RESULTS = {json.dumps(max_results)};\n'
	)
	return '(function() {\n' + params_js + _SEARCH_PAGE_JS_BODY + '\n})()'


def _build_find_elements_js(
	selector: str,
	attributes: list[str] | None,
	max_results: int,
	include_text: bool,
) -> str:
	"""Build JS IIFE for find_elements with safe parameter injection."""
	params_js = (
		f'var SELECTOR = {json.dumps(selector)};\n'
		f'var ATTRIBUTES = {json.dumps(attributes)};\n'
		f'var MAX_RESULTS = {json.dumps(max_results)};\n'
		f'var INCLUDE_TEXT = {json.dumps(include_text)};\n'
	)
	return '(function() {\n' + params_js + _FIND_ELEMENTS_JS_BODY + '\n})()'


def _format_search_results(data: dict, pattern: str) -> str:
	"""Format search_page CDP result into human-readable text for the agent."""
	if not isinstance(data, dict):
		return f'search_page returned unexpected result: {data}'

	matches = data.get('matches', [])
	total = data.get('total', 0)
	has_more = data.get('has_more', False)

	if total == 0:
		return f'No matches found for "{pattern}" on page.'

	lines = [f'Found {total} match{"es" if total != 1 else ""} for "{pattern}" on page:']
	lines.append('')
	for i, m in enumerate(matches):
		context = m.get('context', '')
		path = m.get('element_path', '')
		loc = f' (in {path})' if path else ''
		lines.append(f'[{i + 1}] {context}{loc}')

	if has_more:
		lines.append(f'\n... showing {len(matches)} of {total} total matches. Increase max_results to see more.')

	return '\n'.join(lines)


def _format_find_results(data: dict, selector: str) -> str:
	"""Format find_elements CDP result into human-readable text for the agent."""
	if not isinstance(data, dict):
		return f'find_elements returned unexpected result: {data}'

	elements = data.get('elements', [])
	total = data.get('total', 0)
	showing = data.get('showing', 0)

	if total == 0:
		return f'No elements found matching "{selector}".'

	lines = [f'Found {total} element{"s" if total != 1 else ""} matching "{selector}":']
	lines.append('')
	for el in elements:
		idx = el.get('index', 0)
		tag = el.get('tag', '?')
		text = el.get('text', '')
		attrs = el.get('attrs', {})
		children = el.get('children_count', 0)

		# Build element description
		parts = [f'[{idx}] <{tag}>']
		if text:
			# Collapse whitespace for readability
			display_text = ' '.join(text.split())
			if len(display_text) > 120:
				display_text = display_text[:120] + '...'
			parts.append(f'"{display_text}"')
		if attrs:
			attr_strs = [f'{k}="{v}"' for k, v in attrs.items()]
			parts.append('{' + ', '.join(attr_strs) + '}')
		parts.append(f'({children} children)')
		lines.append(' '.join(parts))

	if showing < total:
		lines.append(f'\nShowing {showing} of {total} total elements. Increase max_results to see more.')

	return '\n'.join(lines)


def register_page_query_actions(tools: 'Tools') -> None:
	"""Register zero-cost page text and DOM queries."""

	@tools.registry.action(
		"""Search page text for a pattern (like grep). Zero LLM cost, instant. Returns matches with surrounding context. Use to find specific text, verify content exists, or locate data on the page. Set regex=True for regex patterns. Use css_scope to search within a specific section.""",
		param_model=SearchPageAction,
	)
	async def search_page(params: SearchPageAction, browser_session: BrowserSession):
		js_code = _build_search_page_js(
			pattern=params.pattern,
			regex=params.regex,
			case_sensitive=params.case_sensitive,
			context_chars=params.context_chars,
			css_scope=params.css_scope,
			max_results=params.max_results,
		)

		cdp_session = await browser_session.get_or_create_cdp_session()
		result = await cdp_session.cdp_client.send.Runtime.evaluate(
			params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
			session_id=cdp_session.session_id,
		)

		if result.get('exceptionDetails'):
			error_text = result['exceptionDetails'].get('text', 'Unknown JS error')
			return ActionResult(error=f'search_page failed: {error_text}')

		data = result.get('result', {}).get('value')
		if data is None:
			return ActionResult(error='search_page returned no result')

		if isinstance(data, dict) and data.get('error'):
			return ActionResult(error=f'search_page: {data["error"]}')

		formatted = _format_search_results(data, params.pattern)
		total = data.get('total', 0)
		memory = f'Searched page for "{params.pattern}": {total} match{"es" if total != 1 else ""} found.'
		logger.info(f'🔎 {memory}')
		return ActionResult(extracted_content=formatted, long_term_memory=memory)

	@tools.registry.action(
		"""Query DOM elements by CSS selector (like find). Zero LLM cost, instant. Returns matching elements with tag, text, and attributes. Use to explore page structure, count items, get links/attributes. Use attributes=["href","src"] to extract specific attributes.""",
		param_model=FindElementsAction,
	)
	async def find_elements(params: FindElementsAction, browser_session: BrowserSession):
		js_code = _build_find_elements_js(
			selector=params.selector,
			attributes=params.attributes,
			max_results=params.max_results,
			include_text=params.include_text,
		)

		cdp_session = await browser_session.get_or_create_cdp_session()
		result = await cdp_session.cdp_client.send.Runtime.evaluate(
			params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
			session_id=cdp_session.session_id,
		)

		if result.get('exceptionDetails'):
			error_text = result['exceptionDetails'].get('text', 'Unknown JS error')
			return ActionResult(error=f'find_elements failed: {error_text}')

		data = result.get('result', {}).get('value')
		if data is None:
			return ActionResult(error='find_elements returned no result')

		if isinstance(data, dict) and data.get('error'):
			return ActionResult(error=f'find_elements: {data["error"]}')

		formatted = _format_find_results(data, params.selector)
		total = data.get('total', 0)
		memory = f'Found {total} element{"s" if total != 1 else ""} matching "{params.selector}".'
		logger.info(f'🔍 {memory}')
		return ActionResult(extracted_content=formatted, long_term_memory=memory)
