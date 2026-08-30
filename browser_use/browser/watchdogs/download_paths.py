import os
from pathlib import Path


async def unique_download_filename(directory: str, filename: str) -> str:
	"""Append a numeric suffix until a download filename is unused."""
	base, ext = os.path.splitext(filename)
	counter = 1
	new_filename = filename
	while os.path.exists(os.path.join(directory, new_filename)):
		new_filename = f'{base} ({counter}){ext}'
		counter += 1
	return new_filename


def sanitize_download_filename(name: str | None) -> str:
	"""Reduce a page-controlled filename to a safe basename."""
	if not name:
		return 'download'
	name = name.replace('\x00', '')
	name = name.replace('\\', '/')
	name = os.path.basename(name.rsplit('/', 1)[-1])
	if name in ('', '.', '..'):
		return 'download'
	return name


def is_path_contained(path: str | Path, directory: str | Path) -> bool:
	"""Return whether a path's real path stays within a directory's real path."""
	real_path = os.path.realpath(str(path))
	real_dir = os.path.realpath(str(directory))
	return real_path == real_dir or real_path.startswith(real_dir + os.sep)
