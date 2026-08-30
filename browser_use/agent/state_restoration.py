from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from browser_use.filesystem.file_system import FileSystem
from browser_use.version import get_browser_use_version

if TYPE_CHECKING:
	from browser_use.agent.service import Agent


class AgentStateRestoration:
	"""Restore filesystem state and construct runtime artifact services."""

	def __init__(self, agent: Agent) -> None:
		self.agent = agent

	def _set_file_system(self, file_system_path: str | None = None) -> None:
		# Check for conflicting parameters
		if self.agent.state.file_system_state and file_system_path:
			raise ValueError(
				'Cannot provide both file_system_state (from agent state) and file_system_path. '
				'Either restore from existing state or create new file system at specified path, not both.'
			)

		# Check if we should restore from existing state first
		if self.agent.state.file_system_state:
			try:
				# Restore file system from state at the exact same location
				self.agent.file_system = FileSystem.from_state(self.agent.state.file_system_state)
				# The parent directory of base_dir is the original file_system_path
				self.agent.file_system_path = str(self.agent.file_system.base_dir)
				self.agent.logger.debug(f'💾 File system restored from state to: {self.agent.file_system_path}')
				return
			except Exception as e:
				self.agent.logger.error(f'💾 Failed to restore file system from state: {e}')
				raise e

		# Initialize new file system
		try:
			if file_system_path:
				self.agent.file_system = FileSystem(file_system_path)
				self.agent.file_system_path = file_system_path
			else:
				# Use the agent directory for file system
				self.agent.file_system = FileSystem(self.agent.agent_directory)
				self.agent.file_system_path = str(self.agent.agent_directory)
		except Exception as e:
			self.agent.logger.error(f'💾 Failed to initialize file system: {e}.')
			raise e

		# Save file system state to agent state
		self.agent.state.file_system_state = self.agent.file_system.get_state()

		self.agent.logger.debug(f'💾 File system path: {self.agent.file_system_path}')

	def _set_screenshot_service(self) -> None:
		"""Initialize screenshot service using agent directory"""
		try:
			from browser_use.screenshots.service import ScreenshotService

			self.agent.screenshot_service = ScreenshotService(self.agent.agent_directory)
			self.agent.logger.debug(f'📸 Screenshot service initialized in: {self.agent.agent_directory}/screenshots')
		except Exception as e:
			self.agent.logger.error(f'📸 Failed to initialize screenshot service: {e}.')
			raise e

	def _set_browser_use_version_and_source(self, source_override: str | None = None) -> None:
		"""Get the version from pyproject.toml and determine the source of the browser-use package"""
		# Use the helper function for version detection
		version = get_browser_use_version()

		# Determine source
		try:
			package_root = Path(__file__).parent.parent.parent
			repo_files = ['.git', 'README.md', 'docs', 'examples']
			if all(Path(package_root / file).exists() for file in repo_files):
				source = 'git'
			else:
				source = 'pip'
		except Exception as e:
			self.agent.logger.debug(f'Error determining source: {e}')
			source = 'unknown'

		if source_override is not None:
			source = source_override
		# self.agent.logger.debug(f'Version: {version}, Source: {source}')  # moved later to _log_agent_run so that people are more likely to include it in copy-pasted support ticket logs
		self.agent.version = version
		self.agent.source = source
