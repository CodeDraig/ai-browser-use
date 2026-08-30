from importlib.resources import files

from browser_use.browser.demo_mode import DemoMode


def test_demo_mode_script_is_packaged_as_a_resource() -> None:
	script = files('browser_use.browser').joinpath('demo_mode.js').read_text(encoding='utf-8')
	demo_module = __import__('browser_use.browser.demo_mode', fromlist=['_DEMO_PANEL_SCRIPT'])

	assert '__BROWSER_USE_SESSION_ID_PLACEHOLDER__' in script
	assert "window.addEventListener('browser-use-log', handleLogEvent)" in script
	assert not hasattr(demo_module, '_DEMO_PANEL_SCRIPT')
	assert DemoMode.__module__ == 'browser_use.browser.demo_mode'
