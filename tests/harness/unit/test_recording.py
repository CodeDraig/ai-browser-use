import base64
import io
import json
import subprocess

from PIL import Image

from browser_harness import helpers, recorder, video_render


def test_firefox_recording_has_frames_metadata_and_password_masking(tmp_path, monkeypatch):
    """The fixed extension operation records Firefox without arbitrary JS or CDP."""
    from browser_harness import extension_helpers

    monkeypatch.setenv('BH_BACKEND', 'extension')
    image = io.BytesIO()
    Image.new('RGB', (320, 240), 'white').save(image, format='PNG')
    operations = []

    def call(operation, **args):
        operations.append(operation)
        if operation == 'recording_context':
            return dict(url='https://example.test/?token=secret', title='Fixture', w=320, h=240, input='password')
        if operation == 'capture_screenshot':
            return {'data': base64.b64encode(image.getvalue()).decode()}
        raise AssertionError(operation)

    def forbidden(*args, **kwargs):
        raise AssertionError('Firefox recording must use its fixed extension operations')

    monkeypatch.setattr(extension_helpers, 'call', call)
    monkeypatch.setattr(helpers, 'js', forbidden)
    monkeypatch.setattr(helpers, 'cdp', forbidden)
    recorder._capture(tmp_path, 'fill_input', ('#password', 'secret-value'))
    event = json.loads((tmp_path / 'events.jsonl').read_text())
    assert operations == ['recording_context', 'capture_screenshot']
    assert event['w'] == 320 and event['h'] == 240
    assert event['text'] == '•' * len('secret-value')
    assert 'secret' not in event['url']
    with Image.open(tmp_path / event['frame']) as frame:
        assert frame.format == 'JPEG'
        assert frame.size == (320, 240)


def test_nested_actions_record_once_and_failed_actions_do_not_record(monkeypatch):
    """A selector click may call a coordinate click; it is still one public action."""
    import pytest

    observed = []
    namespace = {'click_at_xy': lambda x, y: 'clicked'}
    namespace['click_element'] = lambda selector: namespace['click_at_xy'](10, 20)
    recorder.install(namespace)
    recorder.install(namespace)
    monkeypatch.setattr(recorder, 'observe', lambda name, *args: observed.append(name))
    assert namespace['click_element']('#button') == 'clicked'
    assert observed == ['click_element']
    with pytest.raises(TypeError):
        namespace['click_at_xy']()
    assert observed == ['click_element']


def test_video_renderer_uses_separate_cdp_runtime_without_changing_task(monkeypatch, tmp_path):
    monkeypatch.setenv('BH_BACKEND', 'extension')
    monkeypatch.setenv('BH_RUNTIME_DIR', str(tmp_path / 'task-runtime'))
    monkeypatch.setenv('BH_TMP_DIR', str(tmp_path / 'task-tmp'))
    monkeypatch.setenv('BU_CDP_WS', 'ws://task-endpoint')
    monkeypatch.setenv('BH_VIDEO_CDP_URL', 'http://127.0.0.1:9333')
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs['env'])
        return subprocess.CompletedProcess(command, 0, video_render.MARKER + '{"ok": true}\n', '')

    monkeypatch.setattr(video_render.subprocess, 'run', run)
    assert video_render.run_harness('pass') == {'ok': True}
    env = calls[0]
    assert env['BH_BACKEND'] == 'cdp'
    assert env['BU_CDP_URL'] == 'http://127.0.0.1:9333'
    assert env['BH_RECORD'] == '0'
    assert env['BH_RUNTIME_DIR'] != str(tmp_path / 'task-runtime')
    assert 'BU_CDP_WS' not in env
    assert video_render.os.environ['BH_BACKEND'] == 'extension'
    assert video_render.os.environ['BU_CDP_WS'] == 'ws://task-endpoint'


def test_cli_mcp_uses_the_shared_action_observer(monkeypatch):
    from browser_use.mcp.cli_mcp import CLIMCPServer

    observed = []
    monkeypatch.setattr(recorder, 'observe', lambda name, *args: observed.append(name))
    server = CLIMCPServer()
    assert not server._execute('wait(0)', connect=False)
    assert observed == ['wait']


def test_recording_is_opt_in_and_unknown_focus_masks_text(tmp_path, monkeypatch):
    monkeypatch.setenv('BH_RECORD', '0')
    monkeypatch.setenv('BH_AGENT_WORKSPACE', str(tmp_path))
    recorder.observe('fill_input', ('#field', 'private'), {})
    assert not list(tmp_path.iterdir())
    assert recorder._mask('private', {}) == '•' * len('private')


def test_named_recording_never_overwrites_existing_sources(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setenv('BH_RECORD', '1')
    monkeypatch.setattr(recorder.paths, 'workspace_dir', lambda: tmp_path)
    existing = tmp_path / 'recordings' / 'existing'
    existing.mkdir(parents=True)
    original = existing / 'meta.json'
    original.write_text('{"title":"Original recording"}')
    with pytest.raises(FileExistsError):
        recorder.start_recording('existing', title='Replacement')
    assert original.read_text() == '{"title":"Original recording"}'
