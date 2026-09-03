import asyncio
from collections.abc import Callable, Iterator

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.events import BrowserErrorEvent


@pytest.fixture
def navigation_server() -> Iterator[HTTPServer]:
	server = HTTPServer(host='127.0.0.1', threaded=True)
	server.start()
	yield server
	server.stop()


async def _wait_until(predicate: Callable[[], object], *, timeout: float = 5.0) -> None:
	loop = asyncio.get_running_loop()
	deadline = loop.time() + timeout
	while loop.time() < deadline:
		if predicate():
			return
		await asyncio.sleep(0.05)
	raise AssertionError('condition did not become true before timeout')


async def _wait_for_url(page, expected_url: str) -> None:
	current_url = ''

	async def poll() -> None:
		nonlocal current_url
		loop = asyncio.get_running_loop()
		deadline = loop.time() + 5.0
		while loop.time() < deadline:
			current_url = await page.get_url()
			if current_url == expected_url:
				return
			await asyncio.sleep(0.05)
		raise AssertionError(f'page URL stayed at {current_url!r}, expected {expected_url!r}')

	await poll()


async def test_top_level_policy_blocks_all_page_driven_navigation_before_destination_request(
	navigation_server: HTTPServer,
) -> None:
	port = navigation_server.port
	allowed_base = f'http://127.0.0.1:{port}'
	blocked_base = f'http://localhost:{port}'
	blocked_paths = {
		'/blocked-click',
		'/blocked-script',
		'/blocked-form',
		'/blocked-page-api',
		'/blocked-popup',
		'/blocked-redirect',
	}
	start_html = f'''
		<html><body>
			<a id="blocked-link" href="{blocked_base}/blocked-click">blocked link</a>
			<form id="blocked-form" action="{blocked_base}/blocked-form" method="get"></form>
		</body></html>
	'''
	navigation_server.expect_request('/start').respond_with_data(start_html, content_type='text/html')
	navigation_server.expect_request('/allowed').respond_with_data('allowed', content_type='text/html')
	navigation_server.expect_request('/allowed-popup').respond_with_data('allowed popup', content_type='text/html')
	navigation_server.expect_request('/redirect').respond_with_data(
		'',
		status=302,
		headers={'Location': f'{blocked_base}/blocked-redirect'},
	)
	for path in blocked_paths:
		navigation_server.expect_request(path).respond_with_data('policy failure', content_type='text/plain')

	browser_session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True,
			user_data_dir=None,
			keep_alive=True,
			enable_default_extensions=False,
			allowed_domains=['http://127.0.0.1'],
		)
	)
	policy_errors: list[BrowserErrorEvent] = []

	async def collect_policy_error(event: BrowserErrorEvent) -> None:
		policy_errors.append(event)

	try:
		await browser_session.start()
		browser_session.event_bus.on(BrowserErrorEvent, collect_policy_error)
		page = await browser_session.must_get_current_page()

		async def load_start() -> None:
			await page.goto(f'{allowed_base}/start')
			await _wait_for_url(page, f'{allowed_base}/start')

		async def expect_blocked(url: str, action: Callable[[], object]) -> None:
			previous_error_count = len(policy_errors)
			result = action()
			if asyncio.iscoroutine(result):
				await result
			await _wait_until(lambda: len(policy_errors) > previous_error_count)
			assert policy_errors[-1].details['url'].removesuffix('?') == url
			await _wait_for_url(page, 'about:blank')

		await load_start()
		await expect_blocked(
			f'{blocked_base}/blocked-click',
			lambda: page.evaluate('() => document.getElementById("blocked-link").click()'),
		)

		await load_start()
		await expect_blocked(
			f'{blocked_base}/blocked-script',
			lambda: page.evaluate(f'() => {{ window.location.href = "{blocked_base}/blocked-script"; }}'),
		)

		await load_start()
		await expect_blocked(
			f'{blocked_base}/blocked-form',
			lambda: page.evaluate('() => document.getElementById("blocked-form").submit()'),
		)

		await load_start()
		await expect_blocked(
			f'{blocked_base}/blocked-page-api',
			lambda: page.goto(f'{blocked_base}/blocked-page-api'),
		)

		await load_start()
		await expect_blocked(
			f'{blocked_base}/blocked-redirect',
			lambda: page.goto(f'{allowed_base}/redirect'),
		)

		await load_start()
		page_count = len(await browser_session.get_pages())
		previous_error_count = len(policy_errors)
		await page.evaluate(f'() => {{ window.open("{blocked_base}/blocked-popup", "_blank"); return null; }}')
		await _wait_until(lambda: len(policy_errors) > previous_error_count)
		assert policy_errors[-1].details['url'] == f'{blocked_base}/blocked-popup'
		await _wait_until(lambda: len(browser_session.session_manager.get_all_page_targets()) == page_count)

		# Allowed direct and page-created navigations still proceed normally.
		await page.goto(f'{allowed_base}/allowed')
		await _wait_for_url(page, f'{allowed_base}/allowed')
		await page.evaluate(f'() => {{ window.open("{allowed_base}/allowed-popup", "_blank"); return null; }}')
		await _wait_until(lambda: any(request.path == '/allowed-popup' for request, _ in navigation_server.log))

		assert not [request.path for request, _ in navigation_server.log if request.path in blocked_paths]
		assert any(request.path == '/redirect' for request, _ in navigation_server.log)
		assert any(request.path == '/allowed' for request, _ in navigation_server.log)
	finally:
		await browser_session.kill()


async def test_new_page_url_is_policy_gated_before_any_destination_request(navigation_server: HTTPServer) -> None:
	port = navigation_server.port
	allowed_base = f'http://127.0.0.1:{port}'
	blocked_base = f'http://localhost:{port}'
	blocked_paths = {'/blocked-new-page', '/blocked-new-page-redirect', '/blocked-immediate-goto'}

	navigation_server.expect_request('/allowed-new-page').respond_with_data('allowed', content_type='text/html')
	navigation_server.expect_request('/redirect-new-page').respond_with_data(
		'',
		status=302,
		headers={'Location': f'{blocked_base}/blocked-new-page-redirect'},
	)
	for path in blocked_paths:
		navigation_server.expect_request(path).respond_with_data('policy failure', content_type='text/plain')

	browser_session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True,
			user_data_dir=None,
			keep_alive=True,
			enable_default_extensions=False,
			allowed_domains=['http://127.0.0.1'],
		)
	)
	policy_errors: list[BrowserErrorEvent] = []

	async def collect_policy_error(event: BrowserErrorEvent) -> None:
		policy_errors.append(event)

	try:
		await browser_session.start()
		browser_session.event_bus.on(BrowserErrorEvent, collect_policy_error)

		blocked_page = await browser_session.new_page(f'{blocked_base}/blocked-new-page')
		await _wait_until(lambda: any(error.details.get('url', '').endswith('/blocked-new-page') for error in policy_errors))
		await _wait_until(
			lambda: all(
				target.target_id != blocked_page._target_id for target in browser_session.session_manager.get_all_page_targets()
			)
		)

		allowed_page = await browser_session.new_page(f'{allowed_base}/allowed-new-page')
		await _wait_until(lambda: any(request.path == '/allowed-new-page' for request, _ in navigation_server.log))
		assert any(
			target.target_id == allowed_page._target_id for target in browser_session.session_manager.get_all_page_targets()
		)

		redirect_page = await browser_session.new_page(f'{allowed_base}/redirect-new-page')
		await _wait_until(lambda: any(request.path == '/redirect-new-page' for request, _ in navigation_server.log))
		await _wait_until(
			lambda: any(error.details.get('url', '').endswith('/blocked-new-page-redirect') for error in policy_errors)
		)
		await _wait_until(
			lambda: all(
				target.target_id != redirect_page._target_id for target in browser_session.session_manager.get_all_page_targets()
			)
		)

		blank_page = await browser_session.new_page()
		await asyncio.wait_for(blank_page.goto(f'{blocked_base}/blocked-immediate-goto'), timeout=5.0)
		await _wait_until(
			lambda: any(error.details.get('url', '').endswith('/blocked-immediate-goto') for error in policy_errors)
		)

		assert not [request.path for request, _ in navigation_server.log if request.path in blocked_paths]
	finally:
		await browser_session.kill()
