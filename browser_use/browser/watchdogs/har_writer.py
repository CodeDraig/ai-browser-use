from __future__ import annotations

import base64
import hashlib
import json
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from browser_use.browser.watchdogs.har_recording_watchdog import HarRecordingWatchdog, _HarEntryBuilder


def is_https(url: str | None) -> bool:
	return bool(url and url.lower().startswith('https://'))


def _origin(url: str) -> str:
	# Very small origin extractor, assumes https URLs
	# https://host[:port]/...
	if not url:
		return ''
	try:
		without_scheme = url.split('://', 1)[1]
		host_port = without_scheme.split('/', 1)[0]
		return f'https://{host_port}'
	except Exception:
		return ''


def _mime_to_extension(mime_type: str | None) -> str:
	"""Map MIME type to file extension, matching Playwright's behavior."""
	if not mime_type:
		return 'bin'

	mime_lower = mime_type.lower().split(';')[0].strip()

	# Common MIME type to extension mapping
	mime_map = {
		'text/html': 'html',
		'text/css': 'css',
		'text/javascript': 'js',
		'application/javascript': 'js',
		'application/x-javascript': 'js',
		'application/json': 'json',
		'application/xml': 'xml',
		'text/xml': 'xml',
		'text/plain': 'txt',
		'image/png': 'png',
		'image/jpeg': 'jpg',
		'image/jpg': 'jpg',
		'image/gif': 'gif',
		'image/webp': 'webp',
		'image/svg+xml': 'svg',
		'image/x-icon': 'ico',
		'font/woff': 'woff',
		'font/woff2': 'woff2',
		'application/font-woff': 'woff',
		'application/font-woff2': 'woff2',
		'application/x-font-woff': 'woff',
		'application/x-font-woff2': 'woff2',
		'font/ttf': 'ttf',
		'application/x-font-ttf': 'ttf',
		'font/otf': 'otf',
		'application/x-font-opentype': 'otf',
		'application/pdf': 'pdf',
		'application/zip': 'zip',
		'application/x-zip-compressed': 'zip',
		'video/mp4': 'mp4',
		'video/webm': 'webm',
		'audio/mpeg': 'mp3',
		'audio/mp3': 'mp3',
		'audio/wav': 'wav',
		'audio/ogg': 'ogg',
	}

	return mime_map.get(mime_lower, 'bin')


def _generate_har_filename(content: bytes, mime_type: str | None) -> str:
	"""Generate a hash-based filename for HAR attach mode, matching Playwright's format."""
	content_hash = hashlib.sha1(content).hexdigest()
	extension = _mime_to_extension(mime_type)
	return f'{content_hash}.{extension}'


class HarWriter:
	"""Serialize captured network state into HAR 1.2 and optional sidecar bodies."""

	def __init__(self, watchdog: HarRecordingWatchdog) -> None:
		self.watchdog = watchdog

	async def write(self) -> None:
		# Filter by mode and HTTPS already respected at collection time
		entries = [e for e in self.watchdog._entries.values() if self._include_entry(e)]

		har_entries = []
		sidecar_dir: Path | None = None
		if self.watchdog._content_mode == 'attach':
			sidecar_dir = self.watchdog._har_dir / f'{self.watchdog._har_path.stem}_har_parts'
			sidecar_dir.mkdir(parents=True, exist_ok=True)

		for e in entries:
			content_obj: dict = {'mimeType': e.mime_type or ''}

			# Get body data, preferring response_body over encoded_data
			if e.response_body is not None:
				body_data = e.response_body
			else:
				body_data = e.encoded_data

			# Defensive conversion: ensure body_data is always bytes
			if isinstance(body_data, str):
				body_bytes = body_data.encode('utf-8', errors='replace')
			elif isinstance(body_data, bytearray):
				body_bytes = bytes(body_data)
			elif isinstance(body_data, bytes):
				body_bytes = body_data
			else:
				# Fallback: try to convert to bytes
				try:
					body_bytes = bytes(body_data) if body_data else b''
				except (TypeError, ValueError):
					body_bytes = b''

			content_size = len(body_bytes)

			# Calculate compression (bytes saved by compression)
			compression = 0
			if e.content_length is not None and e.encoded_data_length is not None:
				compression = max(0, e.content_length - e.encoded_data_length)

			if self.watchdog._content_mode == 'embed' and content_size > 0:
				# Prefer plain text; fallback to base64 only if decoding fails
				try:
					text_decoded = body_bytes.decode('utf-8')
					content_obj['text'] = text_decoded
					content_obj['size'] = content_size
					content_obj['compression'] = compression
				except UnicodeDecodeError:
					content_obj['text'] = base64.b64encode(body_bytes).decode('ascii')
					content_obj['encoding'] = 'base64'
					content_obj['size'] = content_size
					content_obj['compression'] = compression
			elif self.watchdog._content_mode == 'attach' and content_size > 0 and sidecar_dir is not None:
				filename = _generate_har_filename(body_bytes, e.mime_type)
				(sidecar_dir / filename).write_bytes(body_bytes)
				content_obj['_file'] = filename
				content_obj['size'] = content_size
				content_obj['compression'] = compression
			else:
				# omit or empty
				content_obj['size'] = content_size
				if content_size > 0:
					content_obj['compression'] = compression

			started_date_time, total_time_ms, timings = self._compute_timings(e)
			req_headers_list = [{'name': k, 'value': str(v)} for k, v in (e.request_headers or {}).items()]
			resp_headers_list = [{'name': k, 'value': str(v)} for k, v in (e.response_headers or {}).items()]
			request_headers_size = self._calc_headers_size(e.method or 'GET', e.url or '', req_headers_list)
			response_headers_size = self._calc_headers_size(None, None, resp_headers_list)
			request_body_size = self._calc_request_body_size(e)
			request_post_data = None
			if e.post_data and self.watchdog._content_mode != 'omit':
				if self.watchdog._content_mode == 'embed':
					request_post_data = {'mimeType': e.request_headers.get('content-type', ''), 'text': e.post_data}
				elif self.watchdog._content_mode == 'attach' and sidecar_dir is not None:
					post_data_bytes = e.post_data.encode('utf-8')
					req_mime_type = e.request_headers.get('content-type', 'text/plain')
					req_filename = _generate_har_filename(post_data_bytes, req_mime_type)
					(sidecar_dir / req_filename).write_bytes(post_data_bytes)
					request_post_data = {
						'mimeType': req_mime_type,
						'_file': req_filename,
					}

			http_version = e.protocol if e.protocol else 'HTTP/1.1'

			response_body_size = e.transfer_size
			if response_body_size is None:
				response_body_size = e.encoded_data_length
			if response_body_size is None:
				response_body_size = content_size if content_size > 0 else -1

			entry_dict = {
				'startedDateTime': started_date_time,
				'time': total_time_ms,
				'request': {
					'method': e.method or 'GET',
					'url': e.url or '',
					'httpVersion': http_version,
					'headers': req_headers_list,
					'queryString': [],
					'cookies': [],
					'headersSize': request_headers_size,
					'bodySize': request_body_size,
					'postData': request_post_data,
				},
				'response': {
					'status': e.status or 0,
					'statusText': e.status_text or '',
					'httpVersion': http_version,
					'headers': resp_headers_list,
					'cookies': [],
					'content': content_obj,
					'redirectURL': '',
					'headersSize': response_headers_size,
					'bodySize': response_body_size,
				},
				'cache': {},
				'timings': timings,
				'pageref': self._page_ref_for_entry(e),
			}

			# Add security/TLS details if available
			if e.server_ip_address:
				entry_dict['serverIPAddress'] = e.server_ip_address
			if e.server_port is not None:
				entry_dict['_serverPort'] = e.server_port
			if e.security_details:
				# Filter to match Playwright's minimal security details set
				security_filtered = {}
				if 'protocol' in e.security_details:
					security_filtered['protocol'] = e.security_details['protocol']
				if 'subjectName' in e.security_details:
					security_filtered['subjectName'] = e.security_details['subjectName']
				if 'issuer' in e.security_details:
					security_filtered['issuer'] = e.security_details['issuer']
				if 'validFrom' in e.security_details:
					security_filtered['validFrom'] = e.security_details['validFrom']
				if 'validTo' in e.security_details:
					security_filtered['validTo'] = e.security_details['validTo']
				if security_filtered:
					entry_dict['_securityDetails'] = security_filtered
			if e.transfer_size is not None:
				entry_dict['response']['_transferSize'] = e.transfer_size

			har_entries.append(entry_dict)

		# Try to include our library version in creator
		try:
			bu_version = importlib_metadata.version('browser-use')
		except Exception:
			# Fallback when running from source without installed package metadata
			bu_version = 'dev'

		har_obj = {
			'log': {
				'version': '1.2',
				'creator': {'name': 'browser-use', 'version': bu_version},
				'browser': {'name': self.watchdog._browser_name, 'version': self.watchdog._browser_version},
				'pages': [
					{
						'id': f'page@{pid}',  # Use Playwright format: "page@{frame_id}"
						'title': page_info.get('title', page_info.get('url', '')),
						'startedDateTime': self._format_page_started_datetime(page_info.get('startedDateTime')),
						'pageTimings': (
							(lambda _ocl, _ol: ({k: v for k, v in (('onContentLoad', _ocl), ('onLoad', _ol)) if v is not None}))(
								(page_info.get('onContentLoad') if page_info.get('onContentLoad', -1) >= 0 else None),
								(page_info.get('onLoad') if page_info.get('onLoad', -1) >= 0 else None),
							)
						),
					}
					for pid, page_info in self.watchdog._top_level_pages.items()
				],
				'entries': har_entries,
			}
		}

		tmp_path = self.watchdog._har_path.with_suffix(self.watchdog._har_path.suffix + '.tmp')
		# Write as bytes explicitly to avoid any text/binary mode confusion in different environments
		tmp_path.write_bytes(json.dumps(har_obj, indent=2, ensure_ascii=False).encode('utf-8'))
		tmp_path.replace(self.watchdog._har_path)

	def _format_page_started_datetime(self, timestamp: float | None) -> str:
		"""Format page startedDateTime from timestamp."""
		if timestamp is None:
			return ''
		try:
			from datetime import datetime, timezone

			return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
		except Exception:
			return ''

	def _page_ref_for_entry(self, e: _HarEntryBuilder) -> str | None:
		# Use Playwright format: "page@{frame_id}" if frame_id is known
		if e.frame_id and e.frame_id in self.watchdog._top_level_pages:
			return f'page@{e.frame_id}'
		return None

	def _include_entry(self, e: _HarEntryBuilder) -> bool:
		if not is_https(e.url):
			return False
		# Filter out favicon requests (matching Playwright behavior)
		if e.url and '/favicon.ico' in e.url.lower():
			return False
		if getattr(self, '_mode', 'full') == 'full':
			return True
		# minimal: include main document and same-origin subresources
		if e.frame_id and e.frame_id in self.watchdog._top_level_pages:
			page_info = self.watchdog._top_level_pages[e.frame_id]
			page_url = page_info.get('url') if isinstance(page_info, dict) else page_info
			return _origin(e.url or '') == _origin(page_url or '')
		return False

	# ===================== Helpers ==============================
	def _compute_timings(self, e: _HarEntryBuilder) -> tuple[str, int, dict]:
		# startedDateTime from wall_time_request in ISO8601 Z
		started = ''
		try:
			if e.wall_time_request is not None:
				from datetime import datetime, timezone

				started = datetime.fromtimestamp(e.wall_time_request, tz=timezone.utc).isoformat().replace('+00:00', 'Z')
		except Exception:
			started = ''

		# Calculate timings - CDP doesn't always provide DNS/connect/SSL breakdown
		# Default to 0 for unavailable timings, calculate what we can from timestamps
		dns_ms = 0
		connect_ms = 0
		ssl_ms = 0
		send_ms = 0
		wait_ms = 0
		receive_ms = 0

		if e.ts_request is not None and e.ts_response is not None:
			wait_ms = max(0, int(round((e.ts_response - e.ts_request) * 1000)))

		if e.ts_response is not None and e.ts_finished is not None:
			receive_ms = max(0, int(round((e.ts_finished - e.ts_response) * 1000)))

		# Note: DNS, connect, and SSL timings would require additional CDP events or ResourceTiming API
		# For now, we structure the timings dict to match Playwright format
		# but leave DNS/connect/SSL as 0 since CDP doesn't provide this breakdown directly

		total = dns_ms + connect_ms + ssl_ms + send_ms + wait_ms + receive_ms
		return (
			started,
			total,
			{
				'dns': dns_ms,
				'connect': connect_ms,
				'ssl': ssl_ms,
				'send': send_ms,
				'wait': wait_ms,
				'receive': receive_ms,
			},
		)

	def _calc_headers_size(self, method: str | None, url: str | None, headers_list: list[dict]) -> int:
		try:
			# Approximate per RFC: sum of header lines + CRLF; include request/status line only for request
			size = 0
			if method and url:
				# Use HTTP/1.1 request line approximation
				size += len(f'{method} {url} HTTP/1.1\r\n'.encode('latin1'))
			for h in headers_list:
				size += len(f'{h.get("name", "")}: {h.get("value", "")}\r\n'.encode('latin1'))
			size += len(b'\r\n')
			return size
		except Exception:
			return -1

	def _calc_request_body_size(self, e: _HarEntryBuilder) -> int:
		# Try Content-Length header first; else post_data; else request_body; else 0 for GET/HEAD, -1 if unknown
		try:
			cl = None
			if e.request_headers:
				cl = e.request_headers.get('content-length') or e.request_headers.get('Content-Length')
			if cl is not None:
				return int(cl)
			if e.post_data:
				return len(e.post_data.encode('utf-8'))
			if e.request_body is not None:
				return len(e.request_body)
			# GET/HEAD requests typically have no body
			if e.method and e.method.upper() in ('GET', 'HEAD'):
				return 0
		except Exception:
			pass
		return -1
