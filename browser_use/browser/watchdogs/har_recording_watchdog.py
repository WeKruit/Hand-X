"""HAR Recording Watchdog for Browser-Use sessions.

Captures HTTPS network activity via CDP Network domain and writes a HAR 1.2
file on browser shutdown. Respects `record_har_content` (omit/embed/attach)
and `record_har_mode` (full/minimal).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import ClassVar, Dict, Optional

from bubus import BaseEvent

from browser_use.browser.events import BrowserConnectedEvent, BrowserStopEvent
from browser_use.browser.watchdog_base import BaseWatchdog


@dataclass
class _HarContent:
	mime_type: str | None = None
	text_b64: str | None = None  # for embed
	file_rel: str | None = None  # for attach
	size: int | None = None


@dataclass
class _HarEntryBuilder:
	request_id: str = ''
	frame_id: str | None = None
	document_url: str | None = None
	url: str | None = None
	method: str | None = None
	request_headers: dict = field(default_factory=dict)
	request_body: bytes | None = None
	status: int | None = None
	status_text: str | None = None
	response_headers: dict = field(default_factory=dict)
	mime_type: str | None = None
	encoded_data: bytearray = field(default_factory=bytearray)
	failed: bool = False
	# timing info (CDP timestamps are monotonic seconds); wallTime is epoch seconds
	ts_request: float | None = None
	wall_time_request: float | None = None
	ts_response: float | None = None
	ts_finished: float | None = None
	encoded_data_length: int | None = None


def _is_https(url: str | None) -> bool:
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


class HarRecordingWatchdog(BaseWatchdog):
	"""Collects HTTPS requests/responses and writes a HAR 1.2 file on stop."""

	LISTENS_TO: ClassVar[list[type[BaseEvent]]] = [BrowserConnectedEvent, BrowserStopEvent]
	EMITS: ClassVar[list[type[BaseEvent]]] = []

	def __init__(self, *args, **kwargs) -> None:
		super().__init__(*args, **kwargs)
		self._enabled: bool = False
		self._entries: dict[str, _HarEntryBuilder] = {}
		self._top_level_pages: dict[str, str] = {}  # frameId -> document URL

	async def on_BrowserConnectedEvent(self, event: BrowserConnectedEvent) -> None:
		profile = self.browser_session.browser_profile
		if not profile.record_har_path:
			return

		# Normalize config
		self._content_mode = (profile.record_har_content or 'embed').lower()
		self._mode = (profile.record_har_mode or 'full').lower()
		self._har_path = Path(str(profile.record_har_path)).expanduser().resolve()
		self._har_dir = self._har_path.parent
		self._har_dir.mkdir(parents=True, exist_ok=True)

		try:
			# Enable Network domain and subscribe to events
			cdp_session = await self.browser_session.get_or_create_cdp_session()
			await cdp_session.cdp_client.send.Network.enable(session_id=cdp_session.session_id)

			# Query browser version for HAR log.browser
			try:
				version_info = await self.browser_session.cdp_client.send.Browser.getVersion()
				self._browser_name = version_info.get('product') or 'Chromium'
				self._browser_version = version_info.get('jsVersion') or ''
			except Exception:
				self._browser_name = 'Chromium'
				self._browser_version = ''

			cdp = self.browser_session.cdp_client.register
			cdp.Network.requestWillBeSent(self._on_request_will_be_sent)
			cdp.Network.responseReceived(self._on_response_received)
			cdp.Network.dataReceived(self._on_data_received)
			cdp.Network.loadingFinished(self._on_loading_finished)
			cdp.Network.loadingFailed(self._on_loading_failed)

			self._enabled = True
			self.logger.info(f'📊 Starting HAR recording to {self._har_path}')
		except Exception as e:
			self.logger.warning(f'Failed to enable HAR recording: {e}')
			self._enabled = False

	async def on_BrowserStopEvent(self, event: BrowserStopEvent) -> None:
		if not self._enabled:
			return
		try:
			await self._write_har()
			self.logger.info(f'📊 HAR file saved: {self._har_path}')
		except Exception as e:
			self.logger.warning(f'Failed to write HAR: {e}')

	# =============== CDP Event Handlers (sync) ==================
	def _on_request_will_be_sent(self, params: dict, session_id: str | None) -> None:
		try:
			req = params.get('request', {})
			url = req.get('url')
			if not _is_https(url):
				return  # HTTPS-only requirement

			request_id = params.get('requestId')
			if not request_id:
				return

			entry = self._entries.setdefault(request_id, _HarEntryBuilder(request_id=request_id))
			entry.url = url
			entry.method = req.get('method')
			entry.request_headers = (
				{h['name'].lower(): h.get('value', '') for h in req.get('headers', [])}
				if isinstance(req.get('headers'), list)
				else req.get('headers', {}) or {}
			)
			entry.frame_id = params.get('frameId')
			entry.document_url = params.get('documentURL') or entry.document_url

			# Timing anchors
			entry.ts_request = params.get('timestamp')
			entry.wall_time_request = params.get('wallTime')

			# Track top-level navigations for page context
			if params.get('type') == 'Document' and params.get('isSameDocument', False) is False:
				# best-effort: consider as navigation
				if entry.frame_id:
					self._top_level_pages[entry.frame_id] = url
		except Exception as e:
			self.logger.debug(f'requestWillBeSent handling error: {e}')

	def _on_response_received(self, params: dict, session_id: str | None) -> None:
		try:
			request_id = params.get('requestId')
			if not request_id or request_id not in self._entries:
				return
			response = params.get('response', {})
			entry = self._entries[request_id]
			entry.status = response.get('status')
			entry.status_text = response.get('statusText')
			entry.response_headers = response.get('headers', {}) or {}
			entry.mime_type = response.get('mimeType')
			entry.ts_response = params.get('timestamp')
		except Exception as e:
			self.logger.debug(f'responseReceived handling error: {e}')

	def _on_data_received(self, params: dict, session_id: str | None) -> None:
		try:
			request_id = params.get('requestId')
			if not request_id or request_id not in self._entries:
				return
			data = params.get('data')
			if isinstance(data, str):
				try:
					self._entries[request_id].encoded_data.extend(data.encode('latin1'))
				except Exception:
					pass
		except Exception as e:
			self.logger.debug(f'dataReceived handling error: {e}')

	def _on_loading_finished(self, params: dict, session_id: str | None) -> None:
		try:
			request_id = params.get('requestId')
			if not request_id or request_id not in self._entries:
				return
			entry = self._entries[request_id]
			entry.ts_finished = params.get('timestamp')
			if 'encodedDataLength' in params:
				try:
					entry.encoded_data_length = int(params.get('encodedDataLength') or 0)
				except Exception:
					entry.encoded_data_length = None
		except Exception as e:
			self.logger.debug(f'loadingFinished handling error: {e}')

	def _on_loading_failed(self, params: dict, session_id: str | None) -> None:
		try:
			request_id = params.get('requestId')
			if request_id and request_id in self._entries:
				self._entries[request_id].failed = True
		except Exception as e:
			self.logger.debug(f'loadingFailed handling error: {e}')

	# ===================== HAR Writing ==========================
	async def _write_har(self) -> None:
		# Filter by mode and HTTPS already respected at collection time
		entries = [e for e in self._entries.values() if self._include_entry(e)]

		har_entries = []
		sidecar_dir: Path | None = None
		if self._content_mode == 'attach':
			sidecar_dir = self._har_dir / f'{self._har_path.stem}_har_parts'
			sidecar_dir.mkdir(parents=True, exist_ok=True)

		for e in entries:
			content_obj: dict = {'mimeType': e.mime_type or ''}

			body_bytes: bytes = bytes(e.encoded_data)
			content_size = len(body_bytes)

			if self._content_mode == 'embed' and content_size > 0:
				content_obj['text'] = base64.b64encode(body_bytes).decode('ascii')
				content_obj['encoding'] = 'base64'
				content_obj['size'] = content_size
			elif self._content_mode == 'attach' and content_size > 0 and sidecar_dir is not None:
				# Use incremental index-based filenames for determinism
				rel_name = f'{len(har_entries):06d}.bin'
				(sidecar_dir / rel_name).write_bytes(body_bytes)
				content_obj['_file'] = str(Path(sidecar_dir.name) / rel_name)
				content_obj['size'] = content_size
			else:
				# omit or empty
				content_obj['size'] = content_size

			started_date_time, total_time_ms, timings = self._compute_timings(e)
			req_headers_list = [{'name': k, 'value': str(v)} for k, v in (e.request_headers or {}).items()]
			resp_headers_list = [{'name': k, 'value': str(v)} for k, v in (e.response_headers or {}).items()]
			request_headers_size = self._calc_headers_size(e.method or 'GET', e.url or '', req_headers_list)
			response_headers_size = self._calc_headers_size(None, None, resp_headers_list)
			request_body_size = self._calc_request_body_size(e)

			har_entries.append(
				{
					'startedDateTime': started_date_time,
					'time': total_time_ms,
					'request': {
						'method': e.method or 'GET',
						'url': e.url or '',
						'httpVersion': 'HTTP/1.1',
						'headers': req_headers_list,
						'queryString': [],
						'cookies': [],
						'headersSize': request_headers_size,
						'bodySize': request_body_size,
					},
					'response': {
						'status': e.status or 0,
						'statusText': e.status_text or '',
						'httpVersion': 'HTTP/1.1',
						'headers': resp_headers_list,
						'cookies': [],
						'content': content_obj,
						'redirectURL': '',
						'headersSize': response_headers_size,
						'bodySize': content_size if content_size > 0 else -1,
					},
					'cache': {},
					'timings': timings,
					'pageref': self._page_ref_for_entry(e),
				}
			)

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
				'browser': {'name': self._browser_name, 'version': self._browser_version},
				'pages': [
					{'id': pid, 'title': url, 'startedDateTime': '', 'pageTimings': {}}
					for pid, url in self._top_level_pages.items()
				],
				'entries': har_entries,
			}
		}

		tmp_path = self._har_path.with_suffix(self._har_path.suffix + '.tmp')
		tmp_path.write_text(json.dumps(har_obj, indent=2))
		tmp_path.replace(self._har_path)

	def _page_ref_for_entry(self, e: _HarEntryBuilder) -> str | None:
		# Use frame_id as stable page id if known
		if e.frame_id and e.frame_id in self._top_level_pages:
			return e.frame_id
		return None

	def _include_entry(self, e: _HarEntryBuilder) -> bool:
		if not _is_https(e.url):
			return False
		if getattr(self, '_mode', 'full') == 'full':
			return True
		# minimal: include main document and same-origin subresources
		if e.frame_id and e.frame_id in self._top_level_pages:
			page_url = self._top_level_pages[e.frame_id]
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

		send_ms = 0
		wait_ms = 0
		receive_ms = 0
		if e.ts_request is not None and e.ts_response is not None:
			wait_ms = max(0, int(round((e.ts_response - e.ts_request) * 1000)))
		if e.ts_response is not None and e.ts_finished is not None:
			receive_ms = max(0, int(round((e.ts_finished - e.ts_response) * 1000)))
		total = send_ms + wait_ms + receive_ms
		return started, total, {'send': send_ms, 'wait': wait_ms, 'receive': receive_ms}

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
		# Try Content-Length header first; else, if request_body set, use its length; else -1
		try:
			cl = None
			if e.request_headers:
				cl = e.request_headers.get('content-length') or e.request_headers.get('Content-Length')
			if cl is not None:
				return int(cl)
			if e.request_body is not None:
				return len(e.request_body)
		except Exception:
			pass
		return -1
