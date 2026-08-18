import logging
import os
from functools import cache
from pathlib import Path

import httpx
from packaging.version import Version

logger = logging.getLogger(__name__)


@cache
def get_browser_use_version() -> str:
	"""Return the source checkout or installed browser-use version."""
	try:
		package_root = Path(__file__).parent.parent
		pyproject_path = package_root / 'pyproject.toml'
		if pyproject_path.exists():
			import re

			content = pyproject_path.read_text(encoding='utf-8')
			if match := re.search(r'version\s*=\s*["\']([^"\']+)["\']', content):
				version = match.group(1)
				os.environ['LIBRARY_VERSION'] = version
				return version

		from importlib.metadata import version as get_version

		version = str(get_version('browser-use'))
		os.environ['LIBRARY_VERSION'] = version
		return version
	except Exception as error:
		logger.debug(f'Error detecting browser-use version: {type(error).__name__}: {error}')
		return 'unknown'


async def check_latest_browser_use_version() -> str | None:
	"""Return a newer browser-use version from PyPI, when available."""
	try:
		async with httpx.AsyncClient(timeout=3.0) as client:
			response = await client.get('https://pypi.org/pypi/browser-use/json')
			if response.status_code == 200:
				latest_version = response.json()['info']['version']
				if is_newer_browser_use_version(latest_version, get_browser_use_version()):
					return latest_version
	except Exception:
		pass
	return None


def is_newer_browser_use_version(latest_version: str, current_version: str) -> bool:
	"""Return whether the available version is newer than the current version."""
	return Version(latest_version) > Version(current_version)


@cache
def get_git_info() -> dict[str, str] | None:
	"""Return Git provenance when browser-use is running from a checkout."""
	try:
		import subprocess

		package_root = Path(__file__).parent.parent
		if not (package_root / '.git').exists():
			return None

		def git_output(*args: str) -> str:
			return subprocess.check_output(
				['git', *args], cwd=package_root, stderr=subprocess.DEVNULL
			).decode().strip()

		return {
			'commit_hash': git_output('rev-parse', 'HEAD'),
			'branch': git_output('rev-parse', '--abbrev-ref', 'HEAD'),
			'remote_url': git_output('config', '--get', 'remote.origin.url'),
			'commit_timestamp': git_output('show', '-s', '--format=%ci', 'HEAD'),
		}
	except Exception as error:
		logger.debug(f'Error getting git info: {type(error).__name__}: {error}')
		return None
