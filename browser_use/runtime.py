import asyncio
import logging
import os
import platform
import signal
from collections.abc import Callable, Coroutine
from sys import stderr
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar('T')
_exiting = False


class SignalHandler:
	"""Manage pause, resume, and exit signals for asyncio agent runs."""

	def __init__(
		self,
		loop: asyncio.AbstractEventLoop | None = None,
		pause_callback: Callable[[], None] | None = None,
		resume_callback: Callable[[], None] | None = None,
		custom_exit_callback: Callable[[], None] | None = None,
		exit_on_second_int: bool = True,
		interruptible_task_patterns: list[str] | None = None,
		disabled: bool = False,
	):
		self.loop = loop or asyncio.get_event_loop()
		self.pause_callback = pause_callback
		self.resume_callback = resume_callback
		self.custom_exit_callback = custom_exit_callback
		self.exit_on_second_int = exit_on_second_int
		self.interruptible_task_patterns = interruptible_task_patterns or ['step', 'multi_act', 'get_next_action']
		self.is_windows = platform.system() == 'Windows'
		self.disabled = disabled
		self._initialize_loop_state()
		self.original_sigint_handler = None
		self.original_sigterm_handler = None

	def _initialize_loop_state(self) -> None:
		setattr(self.loop, 'ctrl_c_pressed', False)
		setattr(self.loop, 'waiting_for_input', False)

	def register(self) -> None:
		"""Register SIGINT and SIGTERM handlers unless signal handling is disabled."""
		if self.disabled:
			return

		try:
			if self.is_windows:
				def windows_handler(sig, frame):
					print('\n\n🛑 Got Ctrl+C. Exiting immediately on Windows...\n', file=stderr)
					if self.custom_exit_callback:
						self.custom_exit_callback()
					os._exit(0)

				self.original_sigint_handler = signal.signal(signal.SIGINT, windows_handler)
			else:
				self.original_sigint_handler = self.loop.add_signal_handler(signal.SIGINT, lambda: self.sigint_handler())
				self.original_sigterm_handler = self.loop.add_signal_handler(signal.SIGTERM, lambda: self.sigterm_handler())
		except Exception:
			pass

	def unregister(self) -> None:
		"""Remove registered handlers and restore captured handlers where possible."""
		if self.disabled:
			return

		try:
			if self.is_windows:
				if self.original_sigint_handler:
					signal.signal(signal.SIGINT, self.original_sigint_handler)
			else:
				self.loop.remove_signal_handler(signal.SIGINT)
				self.loop.remove_signal_handler(signal.SIGTERM)
				if self.original_sigint_handler:
					signal.signal(signal.SIGINT, self.original_sigint_handler)
				if self.original_sigterm_handler:
					signal.signal(signal.SIGTERM, self.original_sigterm_handler)
		except Exception as error:
			logger.warning(f'Error while unregistering signal handlers: {error}')

	def _handle_second_ctrl_c(self) -> None:
		global _exiting
		if not _exiting:
			_exiting = True
			if self.custom_exit_callback:
				try:
					self.custom_exit_callback()
				except Exception as error:
					logger.error(f'Error in exit callback: {error}')

		print('\n\n🛑  Got second Ctrl+C. Exiting immediately...\n', file=stderr)
		self._reset_terminal()
		os._exit(0)

	@staticmethod
	def _reset_terminal() -> None:
		print('\033[?25h', end='', flush=True, file=stderr)
		print('\033[?25h', end='', flush=True)
		print('\033[0m', end='', flush=True, file=stderr)
		print('\033[0m', end='', flush=True)
		print('\033[?1l', end='', flush=True, file=stderr)
		print('\033[?1l', end='', flush=True)
		print('\033[?2004l', end='', flush=True, file=stderr)
		print('\033[?2004l', end='', flush=True)
		print('\r', end='', flush=True, file=stderr)
		print('\r', end='', flush=True)
		print('(tip: press [Enter] once to fix escape codes appearing after chrome exit)', file=stderr)

	def sigint_handler(self) -> None:
		"""Pause on the first SIGINT and optionally exit on the second."""
		global _exiting
		if _exiting:
			os._exit(0)

		if getattr(self.loop, 'ctrl_c_pressed', False):
			if getattr(self.loop, 'waiting_for_input', False):
				return
			if self.exit_on_second_int:
				self._handle_second_ctrl_c()

		setattr(self.loop, 'ctrl_c_pressed', True)
		self._cancel_interruptible_tasks()
		if self.pause_callback:
			try:
				self.pause_callback()
			except Exception as error:
				logger.error(f'Error in pause callback: {error}')
		print('----------------------------------------------------------------------', file=stderr)

	def sigterm_handler(self) -> None:
		"""Invoke exit cleanup and terminate immediately on SIGTERM."""
		global _exiting
		if not _exiting:
			_exiting = True
			print('\n\n🛑 SIGTERM received. Exiting immediately...\n\n', file=stderr)
			if self.custom_exit_callback:
				self.custom_exit_callback()
		os._exit(0)

	def _cancel_interruptible_tasks(self) -> None:
		current_task = asyncio.current_task(self.loop)
		for task in asyncio.all_tasks(self.loop):
			if task != current_task and not task.done():
				task_name = task.get_name()
				if any(pattern in task_name for pattern in self.interruptible_task_patterns):
					logger.debug(f'Cancelling task: {task_name}')
					task.cancel()
					task.add_done_callback(lambda completed: completed.exception() if completed.cancelled() else None)

		if current_task and not current_task.done():
			task_name = current_task.get_name()
			if any(pattern in task_name for pattern in self.interruptible_task_patterns):
				logger.debug(f'Cancelling current task: {task_name}')
				current_task.cancel()

	def wait_for_resume(self) -> None:
		"""Block for Enter to resume, treating a second Ctrl+C as an exit."""
		setattr(self.loop, 'waiting_for_input', True)
		original_handler = signal.getsignal(signal.SIGINT)
		try:
			signal.signal(signal.SIGINT, signal.default_int_handler)
		except ValueError:
			pass

		green = '\x1b[32;1m'
		red = '\x1b[31m'
		blink = '\033[33;5m'
		unblink = '\033[0m'
		reset = '\x1b[0m'
		try:
			print(
				f'➡️  Press {green}[Enter]{reset} to resume or {red}[Ctrl+C]{reset} again to exit{blink}...{unblink} ',
				end='',
				flush=True,
				file=stderr,
			)
			input()
			if self.resume_callback:
				self.resume_callback()
		except KeyboardInterrupt:
			self._handle_second_ctrl_c()
		finally:
			try:
				signal.signal(signal.SIGINT, original_handler)
				setattr(self.loop, 'waiting_for_input', False)
			except Exception:
				pass

	def reset(self) -> None:
		"""Clear pause and input state after resuming."""
		if hasattr(self.loop, 'ctrl_c_pressed'):
			setattr(self.loop, 'ctrl_c_pressed', False)
		if hasattr(self.loop, 'waiting_for_input'):
			setattr(self.loop, 'waiting_for_input', False)


def create_task_with_error_handling(
	coro: Coroutine[Any, Any, T],
	*,
	name: str | None = None,
	logger_instance: logging.Logger | None = None,
	suppress_exceptions: bool = False,
) -> asyncio.Task[T]:
	"""Create a task whose completion callback retrieves and logs exceptions."""
	task = asyncio.create_task(coro, name=name)
	log = logger_instance or logger

	def handle_task_exception(completed_task: asyncio.Task[T]) -> None:
		try:
			exception = completed_task.exception()
			if exception is not None:
				task_name = completed_task.get_name()
				message = f'Exception in background task [{task_name}]: {type(exception).__name__}: {exception}'
				if suppress_exceptions:
					log.error(message, exc_info=exception)
				else:
					log.warning(message, exc_info=exception)
		except asyncio.CancelledError:
			pass
		except Exception as error:
			log.error(
				f'Error handling exception in task [{completed_task.get_name()}]: {type(error).__name__}: {error}'
			)

	task.add_done_callback(handle_task_exception)
	return task
