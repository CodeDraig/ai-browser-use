from unittest.mock import Mock

from browser_use.runtime import SignalHandler


def test_disabled_signal_handler_initializes_state_without_registration():
	loop = Mock()
	handler = SignalHandler(loop=loop, disabled=True)

	handler.register()
	handler.unregister()

	assert loop.ctrl_c_pressed is False
	assert loop.waiting_for_input is False
	loop.add_signal_handler.assert_not_called()
	loop.remove_signal_handler.assert_not_called()


def test_first_sigint_cancels_before_pause_and_reset_clears_state(monkeypatch):
	loop = Mock()
	events: list[str] = []
	handler = SignalHandler(loop=loop, pause_callback=lambda: events.append('pause'))
	monkeypatch.setattr(handler, '_cancel_interruptible_tasks', lambda: events.append('cancel'))

	handler.sigint_handler()

	assert events == ['cancel', 'pause']
	assert loop.ctrl_c_pressed is True
	handler.reset()
	assert loop.ctrl_c_pressed is False
	assert loop.waiting_for_input is False


def test_wait_for_resume_restores_signal_state_and_invokes_callback(monkeypatch):
	loop = Mock()
	resume = Mock()
	handler = SignalHandler(loop=loop, resume_callback=resume)
	monkeypatch.setattr('builtins.input', lambda: '')
	monkeypatch.setattr('browser_use.runtime.signal.getsignal', lambda _signal: 'original')
	restored: list[object] = []
	monkeypatch.setattr('browser_use.runtime.signal.signal', lambda _signal, callback: restored.append(callback))

	handler.wait_for_resume()

	resume.assert_called_once_with()
	assert restored[-1] == 'original'
	assert loop.waiting_for_input is False
