from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from cdp_use import CDPClient

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.profile import ProxySettings
from browser_use.browser.session_manager import CDPSession, Target
from browser_use.security import is_url_allowed_by_policy


def _policy_session(*, proxy: ProxySettings | None = None) -> tuple[BrowserSession, MagicMock]:
	profile = BrowserProfile(
		headless=True,
		user_data_dir=None,
		allowed_domains=['https://allowed.example'],
		proxy=proxy,
	)
	browser_session = BrowserSession(browser_profile=profile)
	browser_session.event_bus.dispatch = MagicMock()
	cdp_client = MagicMock(spec=CDPClient)
	cdp_client.send = SimpleNamespace(
		Fetch=SimpleNamespace(
			continueRequest=AsyncMock(),
			failRequest=AsyncMock(),
			enable=AsyncMock(),
		),
		Page=SimpleNamespace(
			enable=AsyncMock(),
			setLifecycleEventsEnabled=AsyncMock(),
			getFrameTree=AsyncMock(return_value={'frameTree': {'frame': {'id': 'main-frame'}}}),
			navigate=AsyncMock(),
		),
		Network=SimpleNamespace(enable=AsyncMock()),
		Target=SimpleNamespace(
			setAutoAttach=AsyncMock(),
			closeTarget=AsyncMock(),
			createTarget=AsyncMock(return_value={'targetId': 'page-target'}),
			getTargets=AsyncMock(return_value={'targetInfos': []}),
		),
		Runtime=SimpleNamespace(runIfWaitingForDebugger=AsyncMock()),
	)
	browser_session._cdp_client_root = cdp_client
	return browser_session, cdp_client


def _track_page(browser_session: BrowserSession, cdp_client: MagicMock) -> None:
	manager = browser_session.session_manager
	manager._targets['page-target'] = Target(target_id='page-target', target_type='page')
	manager._target_sessions['page-target'] = {'page-session'}
	manager._session_to_target['page-session'] = 'page-target'
	cdp_session = CDPSession(
		cdp_client=cdp_client,
		target_id='page-target',
		session_id='page-session',
	)
	cdp_session._lifecycle_events = []
	manager._sessions['page-session'] = cdp_session
	manager.lifecycle._main_frame_ids['page-session'] = 'main-frame'
	manager.navigation_policy.fetch_sessions['page-target'] = 'page-session'


def _paused_request(request_id: str, url: str, *, frame_id: str = 'main-frame') -> dict:
	return {
		'requestId': request_id,
		'resourceType': 'Document',
		'frameId': frame_id,
		'request': {'url': url},
	}


def test_shared_url_policy_evaluator_preserves_allow_prohibit_and_ip_precedence() -> None:
	assert is_url_allowed_by_policy(
		'https://allowed.example/path',
		allowed_domains=['allowed.example'],
		prohibited_domains=['allowed.example'],
		block_ip_addresses=False,
	)


async def test_new_page_without_policy_preserves_direct_target_creation() -> None:
	browser_session, cdp_client = _policy_session()
	browser_session.browser_profile.allowed_domains = None

	page = await browser_session.new_page('https://direct.example/path')

	cdp_client.send.Target.createTarget.assert_awaited_once_with({'url': 'https://direct.example/path'})
	assert page._target_id == 'page-target'
	assert page._session_id is None
	cdp_client.send.Page.navigate.assert_not_awaited()


async def test_new_page_with_policy_uses_ready_manager_session_for_allowed_url() -> None:
	browser_session, cdp_client = _policy_session()
	_track_page(browser_session, cdp_client)

	page = await browser_session.new_page('https://allowed.example/path')

	cdp_client.send.Target.createTarget.assert_awaited_once_with({'url': 'about:blank'})
	assert page._target_id == 'page-target'
	assert page._session_id == 'page-session'
	cdp_client.send.Page.navigate.assert_awaited_once_with(
		{'url': 'https://allowed.example/path'},
		session_id='page-session',
	)


async def test_new_page_with_policy_preflights_blocked_url_and_returns_closed_handle() -> None:
	browser_session, cdp_client = _policy_session()
	_track_page(browser_session, cdp_client)

	page = await browser_session.new_page('https://blocked.example/path')

	cdp_client.send.Target.createTarget.assert_awaited_once_with({'url': 'about:blank'})
	cdp_client.send.Page.navigate.assert_not_awaited()
	cdp_client.send.Target.closeTarget.assert_awaited_once_with(params={'targetId': 'page-target'})
	cdp_client.send.Target.getTargets.assert_awaited_once_with()
	assert page._target_id == 'page-target'
	assert page._session_id == 'page-session'
	dispatch_mock = cast(MagicMock, browser_session.event_bus.dispatch)
	assert dispatch_mock.call_count == 1
	policy_error = dispatch_mock.call_args.args[0]
	assert policy_error.error_type == 'TabCreationBlocked'
	assert policy_error.details['url'] == 'https://blocked.example/path'


async def test_new_page_blocked_close_error_with_absent_target_returns_closed_handle() -> None:
	browser_session, cdp_client = _policy_session()
	_track_page(browser_session, cdp_client)
	cdp_client.send.Target.closeTarget.side_effect = RuntimeError('close response lost')

	page = await browser_session.new_page('https://blocked.example/path')

	assert page._target_id == 'page-target'
	cdp_client.send.Target.closeTarget.assert_awaited_once_with(params={'targetId': 'page-target'})
	cdp_client.send.Target.getTargets.assert_awaited_once_with()
	cdp_client.send.Page.navigate.assert_not_awaited()
	assert cast(MagicMock, browser_session.event_bus.dispatch).call_count == 1


async def test_new_page_blocked_close_retries_when_target_remains_live() -> None:
	browser_session, cdp_client = _policy_session()
	_track_page(browser_session, cdp_client)
	browser_session.session_manager.navigation_policy._TARGET_CLOSE_CONFIRMATION_TIMEOUT = 0.0
	cdp_client.send.Target.closeTarget.side_effect = [RuntimeError('first close failed'), {'success': True}]
	cdp_client.send.Target.getTargets.side_effect = [
		{'targetInfos': [{'targetId': 'page-target'}]},
		{'targetInfos': []},
	]

	page = await browser_session.new_page('https://blocked.example/path')

	assert page._target_id == 'page-target'
	assert cdp_client.send.Target.closeTarget.await_count == 2
	assert cdp_client.send.Target.getTargets.await_count == 2
	cdp_client.send.Page.navigate.assert_not_awaited()
	assert cast(MagicMock, browser_session.event_bus.dispatch).call_count == 1


async def test_new_page_blocked_close_raises_when_target_survives_both_attempts() -> None:
	browser_session, cdp_client = _policy_session()
	_track_page(browser_session, cdp_client)
	browser_session.session_manager.navigation_policy._TARGET_CLOSE_CONFIRMATION_TIMEOUT = 0.0
	cdp_client.send.Target.closeTarget.side_effect = [RuntimeError('first close failed'), RuntimeError('second close failed')]
	cdp_client.send.Target.getTargets.return_value = {'targetInfos': [{'targetId': 'page-target'}]}

	with pytest.raises(RuntimeError, match=r'blocked\.example/path.*target page-target.*could not be confirmed closed'):
		await browser_session.new_page('https://blocked.example/path')

	assert cdp_client.send.Target.closeTarget.await_count == 2
	assert cdp_client.send.Target.getTargets.await_count == 2
	cdp_client.send.Page.navigate.assert_not_awaited()
	assert cast(MagicMock, browser_session.event_bus.dispatch).call_count == 1


async def test_new_page_blocked_close_raises_when_target_inventory_is_unavailable() -> None:
	browser_session, cdp_client = _policy_session()
	_track_page(browser_session, cdp_client)
	browser_session.session_manager.navigation_policy._TARGET_CLOSE_CONFIRMATION_TIMEOUT = 0.0
	cdp_client.send.Target.getTargets.side_effect = RuntimeError('target inventory unavailable')

	with pytest.raises(RuntimeError, match=r'target page-target.*could not be confirmed closed.*inventory unavailable'):
		await browser_session.new_page('https://blocked.example/path')

	assert cdp_client.send.Target.closeTarget.await_count == 2
	assert cdp_client.send.Target.getTargets.await_count == 2
	cdp_client.send.Page.navigate.assert_not_awaited()
	assert cast(MagicMock, browser_session.event_bus.dispatch).call_count == 1


async def test_strict_remediation_rechecks_closure_without_emitting_duplicate_error() -> None:
	browser_session, cdp_client = _policy_session()
	_track_page(browser_session, cdp_client)
	manager = browser_session.session_manager
	manager.navigation_policy.new_page_targets.add('page-target')

	await manager.navigation_policy.remediate_blocked_navigation('page-target', 'https://blocked.example/path')
	await manager.navigation_policy.remediate_blocked_navigation(
		'page-target',
		'https://blocked.example/path',
		require_target_closed=True,
	)

	assert cdp_client.send.Target.closeTarget.await_count == 2
	cdp_client.send.Target.getTargets.assert_awaited_once_with()
	assert cast(MagicMock, browser_session.event_bus.dispatch).call_count == 1


async def test_new_page_policy_setup_failure_closes_target_and_raises() -> None:
	browser_session, cdp_client = _policy_session()
	browser_session.session_manager.navigation_policy.setup_failures['page-target'] = 'Fetch unavailable'

	with pytest.raises(RuntimeError, match='Fetch unavailable'):
		await browser_session.new_page('https://allowed.example/path')

	cdp_client.send.Target.createTarget.assert_awaited_once_with({'url': 'about:blank'})
	cdp_client.send.Page.navigate.assert_not_awaited()
	cdp_client.send.Target.closeTarget.assert_awaited_once_with(params={'targetId': 'page-target'})
	dispatch_mock = cast(MagicMock, browser_session.event_bus.dispatch)
	assert dispatch_mock.call_count == 1
	policy_error = dispatch_mock.call_args.args[0]
	assert policy_error.details['reason'] == 'policy_setup_failed'
	assert not is_url_allowed_by_policy(
		'https://blocked.example/path',
		allowed_domains=None,
		prohibited_domains=['blocked.example'],
		block_ip_addresses=False,
	)
	assert not is_url_allowed_by_policy(
		'http://127.0.0.1/path',
		allowed_domains=['127.0.0.1'],
		prohibited_domains=None,
		block_ip_addresses=True,
	)


async def test_fetch_router_resolves_allowed_blocked_and_subframe_requests_once() -> None:
	browser_session, cdp_client = _policy_session()
	_track_page(browser_session, cdp_client)
	manager = browser_session.session_manager

	await manager.navigation_policy.handle_request_paused(
		_paused_request('allowed', 'https://allowed.example/path'),
		'page-session',
	)
	cdp_client.send.Fetch.continueRequest.assert_awaited_once_with(
		params={'requestId': 'allowed'},
		session_id='page-session',
	)
	cdp_client.send.Fetch.failRequest.assert_not_awaited()

	cdp_client.send.Fetch.continueRequest.reset_mock()
	await manager.navigation_policy.handle_request_paused(
		_paused_request('subframe', 'https://blocked.example/frame', frame_id='child-frame'),
		'page-session',
	)
	cdp_client.send.Fetch.continueRequest.assert_awaited_once_with(
		params={'requestId': 'subframe'},
		session_id='page-session',
	)
	cdp_client.send.Fetch.failRequest.assert_not_awaited()

	cdp_client.send.Fetch.continueRequest.reset_mock()
	await manager.navigation_policy.handle_request_paused(
		_paused_request('blocked', 'https://blocked.example/path'),
		'page-session',
	)
	cdp_client.send.Fetch.continueRequest.assert_not_awaited()
	cdp_client.send.Fetch.failRequest.assert_awaited_once_with(
		params={'requestId': 'blocked', 'errorReason': 'BlockedByClient'},
		session_id='page-session',
	)
	cdp_client.send.Page.navigate.assert_awaited_once_with(
		params={'url': 'about:blank'},
		session_id='page-session',
	)


async def test_fetch_router_closes_new_blocked_page_and_deduplicates_containment() -> None:
	browser_session, cdp_client = _policy_session()
	_track_page(browser_session, cdp_client)
	manager = browser_session.session_manager
	manager.navigation_policy.new_page_targets.add('page-target')

	for request_id in ('blocked-1', 'blocked-2'):
		await manager.navigation_policy.handle_request_paused(
			_paused_request(request_id, 'https://blocked.example/popup'),
			'page-session',
		)

	assert cdp_client.send.Fetch.failRequest.await_count == 2
	assert cdp_client.send.Fetch.continueRequest.await_count == 0
	cdp_client.send.Target.closeTarget.assert_awaited_once_with(params={'targetId': 'page-target'})
	cdp_client.send.Page.navigate.assert_not_awaited()


async def test_fetch_router_keeps_new_page_containment_through_allowed_redirect_source() -> None:
	browser_session, cdp_client = _policy_session()
	_track_page(browser_session, cdp_client)
	manager = browser_session.session_manager
	manager.navigation_policy.new_page_targets.add('page-target')

	await manager.navigation_policy.handle_request_paused(
		_paused_request('redirect-source', 'https://allowed.example/redirect'),
		'page-session',
	)
	assert 'page-target' in manager.navigation_policy.new_page_targets

	await manager.navigation_policy.handle_request_paused(
		_paused_request('redirect-destination', 'https://blocked.example/destination'),
		'page-session',
	)

	cdp_client.send.Fetch.continueRequest.assert_awaited_once_with(
		params={'requestId': 'redirect-source'},
		session_id='page-session',
	)
	cdp_client.send.Fetch.failRequest.assert_awaited_once_with(
		params={'requestId': 'redirect-destination', 'errorReason': 'BlockedByClient'},
		session_id='page-session',
	)
	cdp_client.send.Target.closeTarget.assert_awaited_once_with(params={'targetId': 'page-target'})
	cdp_client.send.Page.navigate.assert_not_awaited()


async def test_allowed_committed_page_clears_new_page_containment_marker() -> None:
	browser_session, cdp_client = _policy_session()
	_track_page(browser_session, cdp_client)
	manager = browser_session.session_manager
	manager.navigation_policy.new_page_targets.add('page-target')

	await manager._handle_target_info_changed(
		{
			'targetInfo': {
				'targetId': 'page-target',
				'type': 'page',
				'url': 'https://allowed.example/committed',
				'title': 'Allowed',
			}
		}
	)

	assert 'page-target' not in manager.navigation_policy.new_page_targets


async def test_fetch_configuration_combines_policy_and_proxy_auth() -> None:
	proxy = ProxySettings(username='proxy-user', password='proxy-pass')
	browser_session, cdp_client = _policy_session(proxy=proxy)
	cdp_session = CDPSession(cdp_client=cdp_client, target_id='page-target', session_id='page-session')
	duplicate_session = CDPSession(cdp_client=cdp_client, target_id='page-target', session_id='duplicate-session')
	browser_session.session_manager._sessions['page-session'] = cdp_session
	browser_session.session_manager._sessions['duplicate-session'] = duplicate_session

	await browser_session.session_manager.navigation_policy.enable_fetch_for_session(cdp_session, target_type='page')
	await browser_session.session_manager.navigation_policy.enable_fetch_for_session(duplicate_session, target_type='page')

	cdp_client.send.Fetch.enable.assert_awaited_once_with(
		params={
			'handleAuthRequests': True,
			'patterns': [{'urlPattern': '*', 'resourceType': 'Document', 'requestStage': 'Request'}],
		},
		session_id='page-session',
	)
	assert browser_session.session_manager.navigation_policy.fetch_sessions == {'page-target': 'page-session'}


async def test_fetch_owner_rebinds_when_one_of_several_target_sessions_detaches() -> None:
	browser_session, cdp_client = _policy_session()
	manager = browser_session.session_manager
	primary_session = CDPSession(cdp_client=cdp_client, target_id='page-target', session_id='primary-session')
	replacement_session = CDPSession(cdp_client=cdp_client, target_id='page-target', session_id='replacement-session')
	manager._targets['page-target'] = Target(target_id='page-target', target_type='page')
	manager._sessions.update(
		{
			'primary-session': primary_session,
			'replacement-session': replacement_session,
		}
	)
	manager._target_sessions['page-target'] = {'primary-session', 'replacement-session'}
	manager._session_to_target.update(
		{
			'primary-session': 'page-target',
			'replacement-session': 'page-target',
		}
	)
	manager.lifecycle._main_frame_ids.update(
		{
			'primary-session': 'main-frame',
			'replacement-session': 'main-frame',
		}
	)
	manager.navigation_policy.fetch_sessions['page-target'] = 'primary-session'

	await manager._handle_target_detached({'sessionId': 'primary-session', 'targetId': 'page-target'})

	assert manager.navigation_policy.fetch_sessions == {'page-target': 'replacement-session'}
	cdp_client.send.Fetch.enable.assert_awaited_once_with(
		params={
			'handleAuthRequests': False,
			'patterns': [{'urlPattern': '*', 'resourceType': 'Document', 'requestStage': 'Request'}],
		},
		session_id='replacement-session',
	)


@pytest.mark.parametrize('waiting_for_debugger', [False, True])
async def test_page_attachment_fails_closed_when_policy_interception_cannot_be_installed(
	waiting_for_debugger: bool,
) -> None:
	browser_session, cdp_client = _policy_session()
	cdp_client.send.Page.getFrameTree.return_value = {'frameTree': {'frame': {}}}

	await browser_session.session_manager._handle_target_attached(
		{
			'sessionId': 'page-session',
			'targetInfo': {
				'targetId': 'page-target',
				'type': 'page',
				'url': 'about:blank',
				'title': '',
			},
			'waitingForDebugger': waiting_for_debugger,
		}
	)

	assert 'page-target' in browser_session.session_manager.navigation_policy.setup_failures
	cdp_client.send.Target.closeTarget.assert_awaited_once_with(params={'targetId': 'page-target'})
	cdp_client.send.Runtime.runIfWaitingForDebugger.assert_not_awaited()


async def test_non_page_attachment_is_resumed_when_optional_fetch_setup_fails() -> None:
	proxy = ProxySettings(username='proxy-user', password='proxy-pass')
	browser_session, cdp_client = _policy_session(proxy=proxy)
	cdp_client.send.Fetch.enable.side_effect = RuntimeError('Fetch unavailable')

	with pytest.raises(RuntimeError, match='Fetch unavailable'):
		await browser_session.session_manager._handle_target_attached(
			{
				'sessionId': 'worker-session',
				'targetInfo': {
					'targetId': 'worker-target',
					'type': 'worker',
					'url': '',
					'title': '',
				},
				'waitingForDebugger': True,
			}
		)

	cdp_client.send.Runtime.runIfWaitingForDebugger.assert_awaited_once_with(session_id='worker-session')
	assert 'worker-target' not in browser_session.session_manager.navigation_policy.setup_failures
