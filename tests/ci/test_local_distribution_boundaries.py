import ast
import json
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

from click.testing import CliRunner

from browser_use.init_cmd import INIT_TEMPLATES, _get_template_content
from browser_use.init_cmd import main as init_main

ROOT = Path(__file__).resolve().parents[2]


def test_wheel_and_sdist_bundle_the_cleaned_harness(tmp_path):
	result = subprocess.run(
		['uv', 'build', '--wheel', '--sdist', '--out-dir', str(tmp_path)],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=120,
	)
	assert result.returncode == 0, result.stderr

	wheel_path = next(tmp_path.glob('browser_use-*.whl'))
	with zipfile.ZipFile(wheel_path) as wheel:
		names = set(wheel.namelist())
		metadata_name = next(name for name in names if name.endswith('.dist-info/METADATA'))
		metadata = BytesParser().parsebytes(wheel.read(metadata_name))
		harness_source = wheel.read('browser_harness/admin.py') + wheel.read('browser_harness/run.py')

	assert 'browser_harness/run.py' in names
	assert 'browser_harness/SKILL.md' in names
	assert 'browser_use/cli_templates/default.py' in names
	assert not any(
		requirement.startswith(('browser-harness', 'browser-use')) for requirement in metadata.get_all('Requires-Dist', [])
	)
	assert b'pypi.org/pypi/browser-harness' not in harness_source
	assert b'uv tool upgrade browser-harness' not in harness_source
	assert b'browser-harness --update' not in harness_source

	sdist_path = next(tmp_path.glob('browser_use-*.tar.gz'))
	with tarfile.open(sdist_path, 'r:gz') as sdist:
		names = set(sdist.getnames())
	assert any(name.endswith('/vendor/browser-harness/src/browser_harness/run.py') for name in names)


def test_project_metadata_has_no_external_harness_or_public_package_selector():
	project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
	dependencies = project['project']['dependencies']

	assert not any(dependency.startswith('browser-harness') for dependency in dependencies)
	assert 'websockets==15.0.1' in dependencies
	assert not any(
		dependency.startswith('browser-use')
		for extra in project['project']['optional-dependencies'].values()
		for dependency in extra
	)
	assert project['project']['urls']['Repository'] == 'https://github.com/CodeDraig/ai-browser-use'
	assert 'browser-harness' not in project.get('tool', {}).get('uv', {}).get('sources', {})


def test_mcp_descriptors_only_launch_the_installed_fork():
	assert not (ROOT / 'server.json').exists()
	manifest = json.loads((ROOT / 'browser_use' / 'mcp' / 'manifest.json').read_text(encoding='utf-8'))
	mcp_config = manifest['server']['mcp_config']

	assert mcp_config['command'] == 'browser-use'
	assert mcp_config['args'] == ['--mcp']
	assert 'uvx' not in json.dumps(mcp_config)


def test_init_generates_only_bundled_importable_templates(monkeypatch, tmp_path):
	monkeypatch.chdir(tmp_path)
	runner = CliRunner()

	for template_name, metadata in INIT_TEMPLATES.items():
		content = _get_template_content(metadata['file'])
		ast.parse(content, filename=metadata['file'])
		assert 'ChatBrowserUse' not in content
		assert 'use_cloud' not in content
		assert 'browser-use.com' not in content

		result = runner.invoke(init_main, ['--template', template_name, '--force'])
		assert result.exit_code == 0, result.output
		generated = tmp_path / template_name / 'main.py'
		assert generated.read_text(encoding='utf-8') == content
		subprocess.run(
			[sys.executable, '-c', f'import runpy; runpy.run_path({str(generated)!r})'],
			cwd=tmp_path,
			check=True,
			capture_output=True,
			text=True,
			timeout=20,
		)


def test_init_source_has_no_runtime_template_network_path():
	source = (ROOT / 'browser_use' / 'init_cmd.py').read_text(encoding='utf-8')

	assert 'urlopen' not in source
	assert 'raw.githubusercontent.com' not in source
	assert 'template-library' not in source
	assert 'uv add browser-use' not in source
