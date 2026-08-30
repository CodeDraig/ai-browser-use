from __future__ import annotations

import logging
import tempfile
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from browser_use.browser.profile import BrowserProfile, ViewportSize

logger = logging.getLogger(__name__)

CHROME_PROFILE_TRANSIENT_FILE_PATTERNS = (
	'Singleton*',
	'*.lock',
	'*-journal',
	'LOCK',
	'LOCKFILE',
)


def _ignore_chrome_profile_transient_files(_src: str, names: list[str]) -> set[str]:
	"""Skip Chrome lock/journal files that should not be copied into a temp profile."""
	return {name for name in names if any(fnmatch(name, pattern) for pattern in CHROME_PROFILE_TRANSIENT_FILE_PATTERNS)}


def _is_chrome_profile_lock_error(error: BaseException) -> bool:
	"""Detect Windows sharing violations or permission errors raised while copying a Chrome profile."""
	if isinstance(error, PermissionError):
		return True

	if getattr(error, 'winerror', None) == 32:
		return True

	# shutil.Error stores copy failures as (src, dst, message/exception) triples.
	for arg in getattr(error, 'args', ()):
		if isinstance(arg, (list, tuple)):
			for item in arg:
				if isinstance(item, (list, tuple)) and item:
					detail = item[-1]
					if isinstance(detail, BaseException) and _is_chrome_profile_lock_error(detail):
						return True
					if 'WinError 32' in str(detail) or 'being used by another process' in str(detail):
						return True

	return False


def copy_browser_profile(profile: BrowserProfile) -> None:
	"""Copy profile to temp directory if user_data_dir is not None and not already a temp dir."""
	if profile.user_data_dir is None:
		return

	user_data_str = str(profile.user_data_dir)
	if 'browser-use-user-data-dir-' in user_data_str.lower():
		# Already using a temp directory, no need to copy
		return

	is_chrome = (
		'chrome' in user_data_str.lower()
		or ('chrome' in str(profile.executable_path).lower())
		or getattr(profile.channel, 'value', None) in {'chrome', 'chrome-beta', 'chrome-dev', 'chrome-canary'}
	)

	if not is_chrome:
		return

	temp_dir = tempfile.mkdtemp(prefix='browser-use-user-data-dir-')
	path_original_user_data = Path(profile.user_data_dir)
	path_original_profile = path_original_user_data / profile.profile_directory
	path_temp_profile = Path(temp_dir) / profile.profile_directory

	if path_original_profile.exists():
		import shutil

		try:
			shutil.copytree(
				path_original_profile,
				path_temp_profile,
				ignore=_ignore_chrome_profile_transient_files,
			)
		except (OSError, shutil.Error) as error:
			if not _is_chrome_profile_lock_error(error):
				raise

			shutil.rmtree(temp_dir, ignore_errors=True)
			raise RuntimeError(
				f'Unable to copy Chrome profile "{profile.profile_directory}" because one or more files are locked. '
				'Close any Chrome windows using this profile, or start browser-use with --cdp-url to connect to '
				'an already-running browser instead of copying the profile.'
			) from error
		local_state_src = path_original_user_data / 'Local State'
		local_state_dst = Path(temp_dir) / 'Local State'
		if local_state_src.exists():
			shutil.copy(local_state_src, local_state_dst)
		logger.info(f'Copied profile ({profile.profile_directory}) and Local State to temp directory: {temp_dir}')

	else:
		Path(temp_dir).mkdir(parents=True, exist_ok=True)
		path_temp_profile.mkdir(parents=True, exist_ok=True)
		logger.info(f'Created new profile ({profile.profile_directory}) in temp directory: {temp_dir}')

	profile.user_data_dir = temp_dir


def configure_display(profile: BrowserProfile, viewport_type: type[ViewportSize], display_size: ViewportSize | None) -> None:
	"""
	Detect the system display size and initialize the display-related config defaults:
	        screen, window_size, window_position, viewport, no_viewport, device_scale_factor
	"""

	has_screen_available = bool(display_size)
	profile.screen = profile.screen or display_size or viewport_type(width=1920, height=1080)

	# if no headless preference specified, prefer headful if there is a display available
	if profile.headless is None:
		profile.headless = not has_screen_available

	# Determine viewport behavior based on mode and user preferences
	user_provided_viewport = profile.viewport is not None

	if profile.headless:
		# Headless mode: always use viewport for content size control
		profile.viewport = profile.viewport or profile.window_size or profile.screen
		profile.window_position = None
		profile.window_size = None
		profile.no_viewport = False
	else:
		# Headful mode: respect user's viewport preference
		profile.window_size = profile.window_size or profile.screen

		if user_provided_viewport:
			# User explicitly set viewport - enable viewport mode
			profile.no_viewport = False
		else:
			# Default headful: content fits to window (no viewport)
			profile.no_viewport = True if profile.no_viewport is None else profile.no_viewport

	# Handle special requirements (device_scale_factor forces viewport mode)
	if profile.device_scale_factor and profile.no_viewport is None:
		profile.no_viewport = False

	# Finalize configuration
	if profile.no_viewport:
		# No viewport mode: content adapts to window
		profile.viewport = None
		profile.device_scale_factor = None
		profile.screen = None
		assert profile.viewport is None
		assert profile.no_viewport is True
	else:
		# Viewport mode: ensure viewport is set
		profile.viewport = profile.viewport or profile.screen
		profile.device_scale_factor = profile.device_scale_factor or 1.0
		assert profile.viewport is not None
		assert profile.no_viewport is False

	assert not (profile.headless and profile.no_viewport), (
		'headless=True and no_viewport=True cannot both be set at the same time'
	)
