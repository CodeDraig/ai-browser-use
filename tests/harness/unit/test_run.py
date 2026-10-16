import sys
from io import StringIO

import pytest

from browser_harness import run


def test_extension_command_uses_the_common_cli(monkeypatch):
    from browser_harness import extension
    calls = []
    monkeypatch.setattr(sys, 'argv', ['browser-harness', 'extension', 'status'])
    monkeypatch.setattr(extension, 'cli', lambda args: calls.append(args) or 0)
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 0
    assert calls == [['status']]


def test_direct_cli_retains_python_execution_without_cloud_bootstrap(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['browser-harness'])
    monkeypatch.setattr(sys, 'stdin', StringIO('assert callable(capture_research) and callable(start_recording)'))
    monkeypatch.setattr(run, 'ensure_daemon', lambda: None)
    run.main()
    assert not hasattr(run, 'start_remote_daemon')
