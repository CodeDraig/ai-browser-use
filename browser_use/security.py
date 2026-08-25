import ipaddress
import logging
import re
import socket
import unicodedata
from fnmatch import fnmatch
from urllib.parse import unquote, urlparse

SensitiveData = dict[str, dict[str, str]]

logger = logging.getLogger(__name__)


def collect_sensitive_data_values(sensitive_data: SensitiveData | None) -> dict[str, str]:
	"""Flatten domain-scoped sensitive data into placeholder-to-value mappings."""
	if not sensitive_data:
		return {}

	return {
		placeholder: value for domain_values in sensitive_data.values() for placeholder, value in domain_values.items() if value
	}


def redact_sensitive_string(value: str, sensitive_values: dict[str, str]) -> str:
	"""Replace sensitive values with placeholders, longest matches first."""
	if not sensitive_values:
		return value

	sorted_items = sorted(sensitive_values.items(), key=lambda item: len(item[1]), reverse=True)
	secret_to_key = {secret: key for key, secret in sorted_items}
	pattern = re.compile('|'.join(re.escape(secret) for secret in secret_to_key))
	return pattern.sub(lambda match: f'<secret>{secret_to_key[match.group(0)]}</secret>', value)


def is_new_tab_page(url: str) -> bool:
	"""Return whether a URL is one of Chromium's supported new-tab pages."""
	return url in ('about:blank', 'chrome://new-tab-page/', 'chrome://new-tab-page', 'chrome://newtab/', 'chrome://newtab')


def is_ip_address(host: str) -> bool:
	"""Return whether ``host`` is an IPv4 or IPv6 spelling Chromium can resolve."""
	bare = host.strip('[]')
	try:
		bare = unquote(bare)
	except Exception:
		pass
	try:
		bare = unicodedata.normalize('NFKC', bare)
	except Exception:
		pass
	# IDNA label separators NFKC misses (U+3002, U+FF61 -> U+3002).
	bare = bare.replace('。', '.').replace('｡', '.')

	try:
		ipaddress.ip_address(bare)
		return True
	except Exception:
		pass

	# Chromium and the kernel resolver also accept decimal, hex, octal, and
	# short-form IPv4 spellings. inet_aton mirrors those legacy forms.
	try:
		socket.inet_aton(bare)
		return True
	except Exception:
		return False


def url_policy_configured(
	*,
	allowed_domains: list[str] | set[str] | None,
	prohibited_domains: list[str] | set[str] | None,
	block_ip_addresses: bool,
) -> bool:
	"""Return whether browser navigation needs active URL-policy enforcement."""
	return bool(allowed_domains or prohibited_domains or block_ip_addresses)


def is_url_allowed_by_policy(
	url: str,
	*,
	allowed_domains: list[str] | set[str] | None,
	prohibited_domains: list[str] | set[str] | None,
	block_ip_addresses: bool,
	log_warnings: bool = False,
) -> bool:
	"""Evaluate one URL using the browser profile's navigation policy."""
	if is_new_tab_page(url):
		return True

	try:
		parsed = urlparse(url)
	except Exception:
		return False

	# These URLs do not address a network host and are intentionally supported
	# by the existing browser policy contract.
	if parsed.scheme in ('data', 'blob'):
		return True

	host = parsed.hostname
	if not host:
		return False

	if block_ip_addresses and is_ip_address(host):
		return False

	if not allowed_domains and not prohibited_domains:
		return True

	if allowed_domains:
		return any(match_url_with_domain_pattern(url, pattern, log_warnings) for pattern in allowed_domains)

	if prohibited_domains:
		return not any(match_url_with_domain_pattern(url, pattern, log_warnings) for pattern in prohibited_domains)

	return True


def _split_domain_pattern(domain_pattern: str) -> tuple[str, str, bool]:
	normalized_pattern = domain_pattern.lower()
	has_explicit_scheme = '://' in normalized_pattern
	if has_explicit_scheme:
		pattern_scheme, pattern_domain = normalized_pattern.split('://', 1)
	else:
		pattern_scheme = 'https'
		pattern_domain = normalized_pattern

	if ':' in pattern_domain and not pattern_domain.startswith(':'):
		pattern_domain = pattern_domain.split(':', 1)[0]

	return pattern_scheme, pattern_domain, has_explicit_scheme


def _is_supported_wildcard_pattern(domain_pattern: str, pattern_domain: str, log_warnings: bool) -> bool:
	if pattern_domain.count('*.') > 1 or pattern_domain.count('.*') > 1:
		if log_warnings:
			logger.error(f'⛔️ Multiple wildcards in pattern=[{domain_pattern}] are not supported')
		return False

	if pattern_domain.endswith('.*'):
		if log_warnings:
			logger.error(f'⛔️ Wildcard TLDs like in pattern=[{domain_pattern}] are not supported for security')
		return False

	if '*' in pattern_domain.replace('*.', ''):
		if log_warnings:
			logger.error(f'⛔️ Only *.domain style patterns are supported, ignoring pattern=[{domain_pattern}]')
		return False

	return True


def _matches_conventional_www(domain: str, pattern_domain: str, has_explicit_scheme: bool) -> bool:
	if has_explicit_scheme or '*' in pattern_domain:
		return False
	if pattern_domain.count('.') == 1 and domain == f'www.{pattern_domain}':
		return True
	return pattern_domain.startswith('www.') and pattern_domain[4:].count('.') == 1 and domain == pattern_domain[4:]


def match_url_with_domain_pattern(url: str, domain_pattern: str, log_warnings: bool = False) -> bool:
	"""Return whether a URL matches a supported scheme and hostname pattern."""
	try:
		if is_new_tab_page(url):
			return False

		parsed_url = urlparse(url)
		scheme = parsed_url.scheme.lower() if parsed_url.scheme else ''
		domain = parsed_url.hostname.lower() if parsed_url.hostname else ''
		if not scheme or not domain:
			return False

		pattern_scheme, pattern_domain, has_explicit_scheme = _split_domain_pattern(domain_pattern)
		normalized_pattern = domain_pattern.lower()
		if not fnmatch(scheme, pattern_scheme):
			return False

		if pattern_domain == '*' or domain == pattern_domain:
			return True
		if _matches_conventional_www(domain, pattern_domain, has_explicit_scheme):
			return True

		if '*' not in pattern_domain:
			return False
		if not _is_supported_wildcard_pattern(normalized_pattern, pattern_domain, log_warnings):
			return False

		if pattern_domain.startswith('*.'):
			parent_domain = pattern_domain[2:]
			if domain == parent_domain or fnmatch(domain, parent_domain):
				return True

		return fnmatch(domain, pattern_domain)
	except Exception as error:
		logger.error(f'⛔️ Error matching URL {url} with pattern {domain_pattern}: {type(error).__name__}: {error}')
		return False


def matching_sensitive_values(
	sensitive_data: SensitiveData | None,
	current_url: str | None,
	*,
	log_warnings: bool = False,
) -> dict[str, str]:
	"""Return non-empty sensitive values permitted for the current URL."""
	if not sensitive_data or not current_url or is_new_tab_page(current_url):
		return {}

	values: dict[str, str] = {}
	for domain_pattern, domain_values in sensitive_data.items():
		if match_url_with_domain_pattern(current_url, domain_pattern, log_warnings):
			values.update({key: value for key, value in domain_values.items() if value})
	return values


def sensitive_data_placeholders(sensitive_data: SensitiveData | None) -> dict[str, list[str]]:
	"""Return non-empty placeholder names grouped by domain pattern."""
	if not sensitive_data:
		return {}
	return {
		domain_pattern: placeholders
		for domain_pattern, values in sensitive_data.items()
		if (placeholders := sorted(key for key, value in values.items() if key and value))
	}


def sensitive_domain_is_allowed(domain_pattern: str, allowed_domain: str) -> bool:
	"""Return whether an allowed-domain pattern covers a sensitive-data domain."""
	if domain_pattern == allowed_domain or allowed_domain == '*':
		return True
	pattern_domain = domain_pattern.split('://')[-1] if '://' in domain_pattern else domain_pattern
	allowed_domain_part = allowed_domain.split('://')[-1] if '://' in allowed_domain else allowed_domain
	return pattern_domain == allowed_domain_part or (
		allowed_domain_part.startswith('*.')
		and (pattern_domain == allowed_domain_part[2:] or pattern_domain.endswith('.' + allowed_domain_part[2:]))
	)


def warn_sensitive_data_domain_constraints(
	log: logging.Logger,
	sensitive_data: SensitiveData | None,
	allowed_domains: list[str] | set[str] | None,
) -> None:
	"""Warn when sensitive-data domains are not constrained by browser policy."""
	if not sensitive_data:
		return
	if not allowed_domains:
		log.warning(
			'⚠️ Agent(sensitive_data=••••••••) was provided but Browser(allowed_domains=[...]) is not locked down! ⚠️\n'
			'          ☠️ If the agent visits a malicious website and encounters a prompt-injection attack, your sensitive_data may be exposed!\n\n'
			'   \n'
		)
		return

	for domain_pattern in sensitive_data:
		if not any(sensitive_domain_is_allowed(domain_pattern, allowed_domain) for allowed_domain in allowed_domains):
			log.warning(
				f'⚠️ Domain pattern "{domain_pattern}" in sensitive_data is not covered by any pattern in allowed_domains={allowed_domains}\n'
				f'   This may be a security risk as credentials could be used on unintended domains.'
			)
