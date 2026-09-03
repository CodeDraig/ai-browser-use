from browser_use.agent.url_detection import (
	is_placeholder_url,
	iter_url_candidate_matches,
	sanitize_url_candidate,
	substitute_url_candidates,
)


def test_placeholder_url_detection_uses_complete_hostname_labels():
	assert is_placeholder_url('https://XXX.XX') is True
	assert is_placeholder_url('www.xxx.xx/path') is True
	assert is_placeholder_url('https://xxx.example') is False
	assert is_placeholder_url('https://example.com/XXX.XX') is False


def test_url_candidate_sanitization_removes_task_prose_suffixes():
	assert sanitize_url_candidate(' https://example.com/search.\\n2. Continue ') == 'https://example.com/search'
	assert sanitize_url_candidate('https://example.com/path),') == 'https://example.com/path'


def test_url_candidate_iteration_and_substitution_share_one_pattern():
	text = 'Visit example.com and then https://example.org/docs.'
	matches = [match.group(0) for match in iter_url_candidate_matches(text)]
	assert matches == ['example.com', 'https://example.org/docs.']
	assert substitute_url_candidates(text, lambda match: f'<{match.group(0)}>') == (
		'Visit <example.com> and then <https://example.org/docs.>'
	)
