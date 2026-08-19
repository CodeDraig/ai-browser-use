"""Browser Use CLI backed by Browser Harness"""

from __future__ import annotations

import sys
from contextlib import redirect_stderr, redirect_stdout
from importlib.metadata import PackageNotFoundError, version
from io import StringIO


def _browser_use_version() -> str:
	try:
		return version('browser-use')
	except PackageNotFoundError:
		return 'unknown'


def _set_harness_client_env() -> None:
	import os

	os.environ['BH_CLIENT'] = 'browser-use-cli'
	os.environ['BH_CLIENT_VERSION'] = _browser_use_version()


def _run_mcp_stdio_server(module_name: str) -> None:
	"""Silence all logging"""
	import asyncio
	import importlib
	import logging
	import os

	os.environ['BROWSER_USE_LOGGING_LEVEL'] = 'critical'
	os.environ['BROWSER_USE_SETUP_LOGGING'] = 'false'
	logging.disable(logging.CRITICAL)

	main = importlib.import_module(module_name).main
	asyncio.run(main())


def _run_mcp_server() -> None:
	_run_mcp_stdio_server('browser_use.mcp.server')


def _run_cli_mcp_server() -> None:
	_run_mcp_stdio_server('browser_use.mcp.cli_mcp')


def _run_install_command(argv: list[str]) -> int:
	if any(arg in {'-h', '--help'} for arg in argv):
		print('usage: browser-use install')
		print()
		print('Install Chromium browser and system dependencies.')
		return 0

	import platform
	import subprocess

	print('Installing Chromium browser + system dependencies...')
	print('This may take a few minutes...\n')

	cmd = ['uvx', 'playwright', 'install', 'chromium']
	if platform.system() == 'Linux':
		cmd.append('--with-deps')
	cmd.append('--no-shell')

	result = subprocess.run(cmd)
	if result.returncode == 0:
		print('\nInstallation complete.')
		print('Ready to use. Run: uvx browser-use')
		return 0

	print('\nInstallation failed', file=sys.stderr)
	return result.returncode or 1


def _run_init_command(argv: list[str]) -> int | None:
	from browser_use.init_cmd import main as init_main

	original_argv = sys.argv
	try:
		sys.argv = [original_argv[0], *argv]
		init_main()
	except SystemExit as exc:
		if exc.code is None:
			return 0
		if isinstance(exc.code, int):
			return exc.code
		print(exc.code, file=sys.stderr)
		return 1
	finally:
		sys.argv = original_argv
	return 0


def _as_browser_use_cli_text(text: str) -> str:
	return text.replace('Browser Harness', 'Browser Use').replace('browser-harness', 'browser-use')


def _normalize_captured_cli_output(func, argv: list[str]) -> int:
	stdout = StringIO()
	stderr = StringIO()
	try:
		with redirect_stdout(stdout), redirect_stderr(stderr):
			result = func(argv)
	except SystemExit as exc:
		result = exc.code

	out = stdout.getvalue()
	err = stderr.getvalue()
	if out:
		print(_as_browser_use_cli_text(out), end='')
	if err:
		print(_as_browser_use_cli_text(err), end='', file=sys.stderr)
	if result is None:
		return 0
	if isinstance(result, int):
		return result
	if isinstance(result, str):
		print(_as_browser_use_cli_text(result), file=sys.stderr)
		return 1
	return 1


def _patch_browser_harness_cli_text() -> None:
	from browser_harness import auth, run

	run.HELP = _as_browser_use_cli_text(run.HELP)
	run.USAGE = _as_browser_use_cli_text(run.USAGE)

	original_auth_cli = auth.run_auth_cli

	def run_auth_cli(argv: list[str]) -> int:
		if any(arg in {'-h', '--help'} for arg in argv):
			return _normalize_captured_cli_output(original_auth_cli, argv)
		return original_auth_cli(argv)

	auth.run_auth_cli = run_auth_cli


def _run_browser_harness() -> int | None:
	_set_harness_client_env()
	from browser_harness import run

	_patch_browser_harness_cli_text()
	args = sys.argv[1:]
	if args and args[0] == 'doctor' and args[1:]:
		if args[1:] in (['--help'], ['-h']):
			print('usage: browser-use doctor [--fix-snap]')
			return 0
		if args[1:] != ['--fix-snap']:
			print('usage: browser-use doctor [--fix-snap]', file=sys.stderr)
			sys.exit(2)
	run.main()
	return None


_CLI3_GUIDE = """Core helpers: new_tab(url), goto_url(url), page_info(), capture_screenshot(),
  click_at_xy(x, y), type_text(text), fill_input(selector, text), press_key(key),
  scroll(x, y), js(code), cdp(method, ...), wait_for_load(), wait_for_element(selector),
  list_tabs(), switch_tab(target), close_tab(target)

Read the full interface now:   browser-use skill show
Install the CLI 3.0 skill (upgrades the CLI and replaces any pre-existing browser-use skill):
                               browser-use skill install
Then load the freshly installed browser-use skill, and follow it to use the latest CLI.
Skill reference:               https://github.com/browser-use/browser-use/blob/main/browser_use/skills/browser-use/SKILL.md
Health check:                  browser-use --doctor"""


def _unknown_helper_message(name: str) -> str:
	return f"""'{name}' is not defined in the browser-use CLI.

Example:
  browser-use <<'PY'
  new_tab("https://example.com")
  print(page_info())
  PY

{_CLI3_GUIDE}"""


def _unknown_exec_name(exc: NameError) -> str | None:
	import re

	name = getattr(exc, 'name', None)
	if name:
		return name
	m = re.search(r"'([A-Za-z_][A-Za-z0-9_]*)'", str(exc))
	return m.group(1) if m else None


def _raised_from_piped_code(exc: BaseException) -> bool:
	tb = exc.__traceback__
	last = None
	while tb is not None:
		last = tb
		tb = tb.tb_next
	return last is not None and last.tb_frame.f_code.co_filename == '<string>'


_QUICKSTART = """Welcome to the Browser Use CLI. Allow your coding agent to reliably control a web browser.

The CLI allows your agent to control the browser via Python, and it manages the browser in the background.

  browser-use <<'PY'
  new_tab("https://news.ycombinator.com")
  print(page_info())
  PY

Core helpers: new_tab(url), goto_url(url), page_info(),
  capture_screenshot(), click_at_xy(x, y), js(code), cdp(method, ...),
  wait_for_load()

Recommended: install the skill so your coding agent remembers this:

  browser-use skill install

You can also paste this into your agent to get started:

  Install or upgrade browser-use to the latest stable version with uv using
  Python 3.12, register the skill from `browser-use skill`, and connect it to
  my browser. Follow https://github.com/browser-use/browser-use if setup or
  connection fails.

More:
  browser-use --doctor     check install, daemon, and browser health
  browser-use --help       full command list
  docs: https://github.com/browser-use/browser-use/blob/main/browser_use/skills/browser-use/SKILL.md"""

_EMPTY_STDIN_MESSAGE = """browser-use received empty stdin. This CLI executes Python piped on stdin:
  browser-use <<'PY'
  print(page_info())
  PY"""


def _dispatch(args: list[str]) -> tuple[int | None, str]:
	if '--cli-mcp' in args:
		_run_cli_mcp_server()
		return 0, 'cli-mcp'
	if '--mcp' in args:
		_run_mcp_server()
		return 0, 'mcp'
	if args and args[0] == 'install':
		return _run_install_command(args[1:]), 'install'
	if args and args[0] == 'init':
		return _run_init_command(args[1:]), 'init'
	if '--template' in args or '-t' in args:
		return _run_init_command(args), 'init'
	if args and args[0] == 'skill':
		from browser_use.skills.install import handle as handle_skill_command

		return handle_skill_command(args[1:]), 'skill'
	if not args:
		if sys.stdin.isatty():
			print(_QUICKSTART)
			return 0, 'quickstart'
		code = sys.stdin.read()
		if not code.strip():
			print(_EMPTY_STDIN_MESSAGE, file=sys.stderr)
			return 1, 'run'
		sys.stdin = StringIO(code)

	try:
		return _run_browser_harness(), args[0] if args else 'run'
	except NameError as exc:
		name = _unknown_exec_name(exc)
		if name is None or not _raised_from_piped_code(exc):
			raise
		import traceback

		traceback.print_exc()
		print(_unknown_helper_message(name), file=sys.stderr)
		return 2, args[0] if args else 'run'


def main() -> int | None:
	args = sys.argv[1:]
	result, _command = _dispatch(args)
	return result


if __name__ == '__main__':
	result = main()
	if result is not None:
		sys.exit(result)
