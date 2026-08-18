import re
from collections.abc import Callable, Iterator
from re import Match
from urllib.parse import urlparse

_URL_PATTERN = re.compile(
	r'https?://[^\s<>"\']+|www\.[^\s<>"\']+|[^\s<>"\']+\.[a-z]{2,}(?:/[^\s<>"\']*)?', re.IGNORECASE
)


def iter_url_candidate_matches(text: str) -> Iterator[Match[str]]:
	"""Yield URL-like matches from agent-provided text."""
	return _URL_PATTERN.finditer(text)


def substitute_url_candidates(text: str, replacement: Callable[[Match[str]], str]) -> str:
	"""Replace URL-like matches in text with values produced by a callback."""
	return _URL_PATTERN.sub(replacement, text)


def is_placeholder_url(url: str) -> bool:
	"""Return whether a URL uses a mock placeholder hostname such as XXX.XX."""
	parsed_url = urlparse(url if '://' in url else f'https://{url}')
	hostname = (parsed_url.hostname or '').strip('.').lower()
	if not hostname:
		return False

	labels = [label for label in hostname.split('.') if label]
	if labels and labels[0] == 'www':
		labels = labels[1:]
	return len(labels) >= 2 and all(re.fullmatch(r'x+', label) for label in labels)


def sanitize_url_candidate(url: str) -> str:
	"""Normalize a URL candidate captured from prose before auto-navigation."""
	candidate = url.strip()
	candidate = re.split(r'\\[nrt]', candidate, maxsplit=1)[0]
	return re.sub(r'[.,;:!?()\[\]]+$', '', candidate)
