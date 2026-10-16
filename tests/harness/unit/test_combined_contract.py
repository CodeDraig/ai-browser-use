import asyncio
import inspect

from browser_harness import daemon, helpers


def test_combined_helpers_preserve_both_feature_sets():
    for name in ('read_page', 'click_element', 'capture_research', 'extract_document',
                 'save_research_download', 'start_js', 'wait_for_download', 'save_download',
                 'start_recording', 'stop_recording', 'activate_tab'):
        assert callable(getattr(helpers, name))
    assert inspect.signature(helpers.switch_tab).parameters['activate'].default is False
    assert inspect.signature(helpers.cdp).parameters['ipc_timeout'].default is None


def test_browser_domain_commands_are_not_sent_to_a_page_session():
    calls = []

    class CDP:
        async def send_raw(self, method, params, session_id=None):
            calls.append((method, session_id))
            return {}

    instance = daemon.Daemon()
    instance.cdp = CDP()
    instance.session = 'page-session'
    result = asyncio.run(instance.handle({'method': 'Browser.setDownloadBehavior', 'params': {'behavior': 'default'}}))
    assert result == {'result': {}}
    assert calls == [('Browser.setDownloadBehavior', None)]


def test_pinned_operation_never_recovers_to_another_tab():
    class CDP:
        async def send_raw(self, *args, **kwargs):
            raise RuntimeError('Session with given id not found')

    instance = daemon.Daemon()
    instance.cdp = CDP()
    instance.session = 'stale'
    async def forbidden(*args, **kwargs):
        raise AssertionError('pinned requests must not retarget')
    instance.attach_first_page = forbidden
    result = asyncio.run(instance.handle({'method': 'Runtime.evaluate', 'pinned': True}))
    assert 'Session with given id not found' in result['error']
