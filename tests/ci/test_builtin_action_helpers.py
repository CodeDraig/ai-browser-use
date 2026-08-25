from types import SimpleNamespace
from typing import cast

from browser_use.browser import BrowserSession
from browser_use.dom.service import EnhancedDOMTreeNode
from browser_use.tools.actions.clicks import _convert_llm_coordinates_to_viewport
from browser_use.tools.actions.inputs import _detect_sensitive_key_name, _is_autocomplete_field
from browser_use.tools.actions.javascript import _validate_and_fix_javascript
from browser_use.tools.actions.page_queries import (
	_build_find_elements_js,
	_build_search_page_js,
	_format_find_results,
	_format_search_results,
)


def test_coordinate_conversion_scales_to_original_viewport():
	browser_session = cast(
		BrowserSession,
		SimpleNamespace(llm_screenshot_size=(1000, 500), dom_state=SimpleNamespace(original_viewport_size=(2000, 1000))),
	)
	assert _convert_llm_coordinates_to_viewport(250, 125, browser_session) == (500, 250)


def test_coordinate_conversion_preserves_coordinates_without_resize():
	browser_session = cast(
		BrowserSession,
		SimpleNamespace(llm_screenshot_size=None, dom_state=SimpleNamespace(original_viewport_size=None)),
	)
	assert _convert_llm_coordinates_to_viewport(250, 125, browser_session) == (250, 125)


def test_sensitive_key_detection_searches_domain_scoped_values():
	sensitive_data = {
		'https://example.com': {'username': 'alice'},
		'https://accounts.example.com': {'password': 'secret'},
	}
	assert _detect_sensitive_key_name('secret', sensitive_data) == 'password'
	assert _detect_sensitive_key_name('missing', sensitive_data) is None


def test_autocomplete_detection_recognizes_current_attribute_shapes():
	assert _is_autocomplete_field(cast(EnhancedDOMTreeNode, SimpleNamespace(attributes={'role': 'combobox'})))
	assert _is_autocomplete_field(cast(EnhancedDOMTreeNode, SimpleNamespace(attributes={'list': 'cities'})))
	assert _is_autocomplete_field(
		cast(
			EnhancedDOMTreeNode,
			SimpleNamespace(attributes={'aria-haspopup': 'listbox', 'aria-controls': 'options'}),
		)
	)
	assert not _is_autocomplete_field(cast(EnhancedDOMTreeNode, SimpleNamespace(attributes={'type': 'text'})))


def test_page_query_builders_json_escape_untrusted_parameters():
	search_js = _build_search_page_js('line\n"quoted"', False, True, 25, 'main[data-x="1"]', 4)
	find_js = _build_find_elements_js('a[href="/x"]', ['href', 'src'], 5, False)

	assert 'var PATTERN = "line\\n\\"quoted\\"";' in search_js
	assert 'var CSS_SCOPE = "main[data-x=\\"1\\"]";' in search_js
	assert 'var SELECTOR = "a[href=\\"/x\\"]";' in find_js
	assert 'var ATTRIBUTES = ["href", "src"]' in find_js


def test_page_query_formatters_preserve_agent_messages():
	search_result = _format_search_results(
		{'matches': [{'context': '...needle...', 'element_path': 'main > p'}], 'total': 1, 'has_more': False},
		'needle',
	)
	find_result = _format_find_results(
		{
			'elements': [{'index': 0, 'tag': 'a', 'text': ' Example ', 'attrs': {'href': '/x'}, 'children_count': 0}],
			'total': 1,
			'showing': 1,
		},
		'a',
	)

	assert search_result == 'Found 1 match for "needle" on page:\n\n[1] ...needle... (in main > p)'
	assert find_result == 'Found 1 element matching "a":\n\n[0] <a> "Example" {href="/x"} (0 children)'


def test_javascript_cleanup_preserves_current_quote_repairs():
	assert _validate_and_fix_javascript(r'return \"value\"') == 'return "value"'
