import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_SKILL_INSTALL_PATHS = (
	Path('.agents') / 'skills' / 'browser-use' / 'SKILL.md',
	Path('.claude') / 'skills' / 'browser-use' / 'SKILL.md',
	Path('.codex') / 'skills' / 'browser-use' / 'SKILL.md',
	Path('.copilot') / 'skills' / 'browser-use' / 'SKILL.md',
	Path('.cursor') / 'skills' / 'browser-use' / 'SKILL.md',
	Path('.gemini') / 'skills' / 'browser-use' / 'SKILL.md',
	Path('.openclaw') / 'skills' / 'browser-use' / 'SKILL.md',
	Path('.config') / 'opencode' / 'skills' / 'browser-use' / 'SKILL.md',
)


def _fake_external_tools(tmp_path: Path) -> tuple[Path, Path]:
	"""Create failing uv/harness commands so tests prove installation is copy-only."""
	bin_dir = tmp_path / 'bin'
	bin_dir.mkdir()
	sentinel = tmp_path / 'external-tool-ran.txt'

	for name in ('uv', 'browser-harness'):
		tool = bin_dir / name
		tool.write_text(
			'#!/usr/bin/env python3\n'
			'import os, pathlib, sys\n'
			f'pathlib.Path(os.environ["EXTERNAL_TOOL_SENTINEL"]).write_text({name!r}, encoding="utf-8")\n'
			'sys.exit(99)\n',
			encoding='utf-8',
		)
		tool.chmod(0o755)
	return bin_dir, sentinel


def _skill_subprocess_env(tmp_path: Path, home: Path) -> tuple[dict[str, str], Path]:
	bin_dir, sentinel = _fake_external_tools(tmp_path)
	env = os.environ.copy()
	env['HOME'] = str(home)
	env['XDG_CONFIG_HOME'] = str(home / '.config')
	env['PATH'] = os.pathsep.join(part for part in (str(bin_dir), env.get('PATH', '')) if part)
	env['PYTHONPATH'] = os.pathsep.join(part for part in (str(ROOT), env.get('PYTHONPATH', '')) if part)
	env['EXTERNAL_TOOL_SENTINEL'] = str(sentinel)
	return env, sentinel


def test_docs_install_this_fork_and_copy_the_bundled_skill():
	readme = (ROOT / 'README.md').read_text(encoding='utf-8')

	assert 'browser-use skill install' in readme
	assert 'git+https://github.com/CodeDraig/ai-browser-use.git' in readme
	assert 'uv add browser-use' not in readme
	assert 'uvx browser-use' not in readme
	assert 'raw.githubusercontent.com/browser-use' not in readme


def test_browser_use_cli_installs_bundled_skill_without_external_commands(tmp_path):
	home = tmp_path / 'home'
	for stale in (home / path for path in EXPECTED_SKILL_INSTALL_PATHS):
		stale.parent.mkdir(parents=True)
		stale.write_text('stale browser-use skill', encoding='utf-8')

	env, sentinel = _skill_subprocess_env(tmp_path, home)
	result = subprocess.run(
		[sys.executable, '-m', 'browser_use.cli', 'skill', 'install'],
		cwd=ROOT,
		env=env,
		capture_output=True,
		text=True,
		timeout=10,
	)

	assert result.returncode == 0, result.stderr
	assert not sentinel.exists()
	expected = (ROOT / 'browser_use' / 'skills' / 'browser-use' / 'SKILL.md').read_text(encoding='utf-8')
	assert '"package": "browser-use"' not in expected
	for installed in (home / path for path in EXPECTED_SKILL_INSTALL_PATHS):
		assert installed.read_text(encoding='utf-8') == expected


def test_browser_use_cli_validates_destination_before_writing(tmp_path):
	blocking_file = tmp_path / 'not-a-directory'
	blocking_file.write_text('blocks skill directory creation', encoding='utf-8')

	env, sentinel = _skill_subprocess_env(tmp_path, tmp_path / 'home')
	result = subprocess.run(
		[sys.executable, '-m', 'browser_use.cli', 'skill', 'install', '--path', str(blocking_file / 'nested')],
		cwd=ROOT,
		env=env,
		capture_output=True,
		text=True,
		timeout=10,
	)

	assert result.returncode == 1
	assert 'is not a directory' in result.stderr
	assert not sentinel.exists()


def test_skill_help_has_no_package_install_or_upgrade_option():
	result = subprocess.run(
		[sys.executable, '-m', 'browser_use.cli', 'skill', 'install', '--help'],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=10,
	)

	assert result.returncode == 0
	assert '--no-install' not in result.stdout
	assert 'upgrade' not in result.stdout.lower()
