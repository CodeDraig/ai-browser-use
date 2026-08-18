from typing import Any, cast
from unittest.mock import Mock

from browser_use.logging_utils import log_pretty_path, log_pretty_url, time_execution_async, time_execution_sync


def test_sync_timing_preserves_result_metadata_and_uses_instance_logger(monkeypatch):
	logger = Mock()
	owner = type('Owner', (), {'logger': logger})()
	monkeypatch.setattr('browser_use.logging_utils.time.time', Mock(side_effect=[10.0, 10.3]))

	@time_execution_sync('--work')
	def work(self, value: int) -> int:
		return value + 1

	assert work(owner, 4) == 5
	assert work.__name__ == 'work'
	logger.debug.assert_called_once_with('⏳ work() took 0.30s')


async def test_async_timing_preserves_result_and_threshold(monkeypatch):
	logger = Mock()
	owner = type('Owner', (), {'logger': logger})()
	monkeypatch.setattr('browser_use.logging_utils.time.time', Mock(side_effect=[20.0, 20.2]))

	@time_execution_async('--work')
	async def work(self, value: int) -> int:
		return value + 1

	assert await work(owner, 4) == 5
	logger.debug.assert_not_called()


def test_log_formatting_helpers_preserve_current_display_contract(tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	assert log_pretty_path(tmp_path / 'folder with spaces') == '"./folder with spaces"'
	assert log_pretty_path(cast(Any, {})) == ''
	assert log_pretty_path(cast(Any, {'secret': 'value'})) == '<dict>'
	assert log_pretty_url('https://www.example.com/long/path', max_len=11) == 'example.com…'
	assert log_pretty_url('http://example.com', max_len=None) == 'example.com'
