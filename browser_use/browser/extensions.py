from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from browser_use.config import get_environment_config

if TYPE_CHECKING:
	from browser_use.browser.profile import BrowserProfile

logger = logging.getLogger(__name__)


class ExtensionManager:
	"""Download, validate, patch, and expose the browser's bundled extensions."""

	def __init__(self, profile: BrowserProfile) -> None:
		self.profile = profile

	def get_args(self) -> list[str]:
		"""Get Chrome args for enabling default extensions (ad blocker and cookie handler)."""
		extension_paths = self._ensure_default_extensions_downloaded()

		args = [
			'--enable-extensions',
			'--disable-extensions-file-access-check',
			'--disable-extensions-http-throttling',
			'--enable-extension-activity-logging',
		]

		if extension_paths:
			args.append(f'--load-extension={",".join(extension_paths)}')

		return args

	@staticmethod
	def _check_extension_manifest_version(ext_dir: Path, ext_name: str) -> bool:
		"""Check that an extension uses Manifest V3. Returns False for MV2 extensions (unsupported by Chrome 145+)."""
		import json

		manifest_path = ext_dir / 'manifest.json'
		if not manifest_path.exists():
			return False
		try:
			with open(manifest_path, encoding='utf-8') as f:
				manifest = json.load(f)
			mv = manifest.get('manifest_version', 2)
			if mv < 3:
				logger.warning(f'Skipping {ext_name} extension: Manifest V{mv} is no longer supported by Chrome')
				return False
			return True
		except Exception:
			return False

	def _ensure_default_extensions_downloaded(self) -> list[str]:
		"""
		Ensure default extensions are downloaded and cached locally.
		Returns list of paths to extension directories.
		"""

		# Extension definitions - optimized for automation and content extraction
		# uBlock Origin Lite (ad blocking, MV3) + "I still don't care about cookies" (cookie banner handling)
		extensions = [
			{
				'name': 'uBlock Origin Lite',
				'id': 'ddkjiahejlhfcafbddmgiahcphecmpfh',
				'url': 'https://clients2.google.com/service/update2/crx?response=redirect&prodversion=133&acceptformat=crx3&x=id%3Dddkjiahejlhfcafbddmgiahcphecmpfh%26uc',
			},
			{
				'name': "I still don't care about cookies",
				'id': 'edibdbjcniadpccecjdfdjjppcpchdlm',
				'url': 'https://clients2.google.com/service/update2/crx?response=redirect&prodversion=133&acceptformat=crx3&x=id%3Dedibdbjcniadpccecjdfdjjppcpchdlm%26uc',
			},
			{
				'name': 'Force Background Tab',
				'id': 'gidlfommnbibbmegmgajdbikelkdcmcl',
				'url': 'https://clients2.google.com/service/update2/crx?response=redirect&prodversion=133&acceptformat=crx3&x=id%3Dgidlfommnbibbmegmgajdbikelkdcmcl%26uc',
			},
			# {
			# 	'name': 'Captcha Solver: Auto captcha solving service',
			# 	'id': 'pgojnojmmhpofjgdmaebadhbocahppod',
			# 	'url': 'https://clients2.google.com/service/update2/crx?response=redirect&prodversion=130&acceptformat=crx3&x=id%3Dpgojnojmmhpofjgdmaebadhbocahppod%26uc',
			# },
			# Consent-O-Matic disabled - using uBlock Origin's cookie lists instead for simplicity
			# {
			# 	'name': 'Consent-O-Matic',
			# 	'id': 'mdjildafknihdffpkfmmpnpoiajfjnjd',
			# 	'url': 'https://clients2.google.com/service/update2/crx?response=redirect&prodversion=130&acceptformat=crx3&x=id%3Dmdjildafknihdffpkfmmpnpoiajfjnjd%26uc',
			# },
			# {
			# 	'name': 'Privacy | Protect Your Payments',
			# 	'id': 'hmgpakheknboplhmlicfkkgjipfabmhp',
			# 	'url': 'https://clients2.google.com/service/update2/crx?response=redirect&prodversion=130&acceptformat=crx3&x=id%3Dhmgpakheknboplhmlicfkkgjipfabmhp%26uc',
			# },
		]

		# Create extensions cache directory
		cache_dir = get_environment_config().extensions_dir
		cache_dir.mkdir(parents=True, exist_ok=True)

		extension_paths = []
		loaded_extension_names = []

		for ext in extensions:
			ext_dir = cache_dir / ext['id']
			crx_file = cache_dir / f'{ext["id"]}.crx'

			# Check if extension is already extracted
			if ext_dir.exists() and (ext_dir / 'manifest.json').exists():
				if not self._check_extension_manifest_version(ext_dir, ext['name']):
					continue
				extension_paths.append(str(ext_dir))
				loaded_extension_names.append(ext['name'])
				continue

			try:
				# Download extension if not cached
				if not crx_file.exists():
					logger.info(f'📦 Downloading {ext["name"]} extension...')
					self._download_extension(ext['url'], crx_file)
				else:
					logger.debug(f'📦 Found cached {ext["name"]} .crx file')

				# Extract extension
				logger.info(f'📂 Extracting {ext["name"]} extension...')
				self._extract_extension(crx_file, ext_dir)

				if not self._check_extension_manifest_version(ext_dir, ext['name']):
					continue

				extension_paths.append(str(ext_dir))
				loaded_extension_names.append(ext['name'])

			except Exception as e:
				logger.warning(f'⚠️ Failed to setup {ext["name"]} extension: {e}')
				continue

		# Apply minimal patch to cookie extension with configurable whitelist
		for i, path in enumerate(extension_paths):
			if loaded_extension_names[i] == "I still don't care about cookies":
				self._apply_minimal_extension_patch(Path(path), self.profile.cookie_whitelist_domains)

		if extension_paths:
			logger.debug(f'[BrowserProfile] 🧩 Extensions loaded ({len(extension_paths)}): [{", ".join(loaded_extension_names)}]')
		else:
			logger.warning('[BrowserProfile] ⚠️ No default extensions could be loaded')

		return extension_paths

	def _apply_minimal_extension_patch(self, ext_dir: Path, whitelist_domains: list[str]) -> None:
		"""Minimal patch: pre-populate chrome.storage.local with configurable domain whitelist."""
		try:
			bg_path = ext_dir / 'data' / 'background.js'
			if not bg_path.exists():
				return

			with open(bg_path, encoding='utf-8') as f:
				content = f.read()

			# Create the whitelisted domains object for JavaScript with proper indentation
			whitelist_entries = [f'        "{domain}": true' for domain in whitelist_domains]
			whitelist_js = '{\n' + ',\n'.join(whitelist_entries) + '\n      }'

			# Find the initialize() function and inject storage setup before updateSettings()
			# The actual function uses 2-space indentation, not tabs
			old_init = """async function initialize(checkInitialized, magic) {
  if (checkInitialized && initialized) {
    return;
  }
  loadCachedRules();
  await updateSettings();
  await recreateTabList(magic);
  initialized = true;
}"""

			# New function with configurable whitelist initialization
			new_init = f"""// Pre-populate storage with configurable domain whitelist if empty
async function ensureWhitelistStorage() {{
  const result = await chrome.storage.local.get({{ settings: null }});
  if (!result.settings) {{
    const defaultSettings = {{
      statusIndicators: true,
      whitelistedDomains: {whitelist_js}
    }};
    await chrome.storage.local.set({{ settings: defaultSettings }});
  }}
}}

async function initialize(checkInitialized, magic) {{
  if (checkInitialized && initialized) {{
    return;
  }}
  loadCachedRules();
  await ensureWhitelistStorage(); // Add storage initialization
  await updateSettings();
  await recreateTabList(magic);
  initialized = true;
}}"""

			if old_init in content:
				content = content.replace(old_init, new_init)

				with open(bg_path, 'w', encoding='utf-8') as f:
					f.write(content)

				domain_list = ', '.join(whitelist_domains)
				logger.info(f'[BrowserProfile] ✅ Cookie extension: {domain_list} pre-populated in storage')
			else:
				logger.debug('[BrowserProfile] Initialize function not found for patching')

		except Exception as e:
			logger.debug(f'[BrowserProfile] Could not patch extension storage: {e}')

	def _download_extension(self, url: str, output_path: Path) -> None:
		"""Download extension .crx file."""
		import urllib.request

		try:
			with urllib.request.urlopen(url) as response:
				with open(output_path, 'wb') as f:
					f.write(response.read())
		except Exception as e:
			raise Exception(f'Failed to download extension: {e}')

	def _extract_extension(self, crx_path: Path, extract_dir: Path) -> None:
		"""Extract .crx file to directory."""
		import os
		import zipfile

		# Remove existing directory
		if extract_dir.exists():
			import shutil

			shutil.rmtree(extract_dir)

		extract_dir.mkdir(parents=True, exist_ok=True)

		try:
			# CRX files are ZIP files with a header, try to extract as ZIP
			with zipfile.ZipFile(crx_path, 'r') as zip_ref:
				zip_ref.extractall(extract_dir)

			# Verify manifest exists
			if not (extract_dir / 'manifest.json').exists():
				raise Exception('No manifest.json found in extension')

		except zipfile.BadZipFile:
			# CRX files have a header before the ZIP data
			# Skip the CRX header and extract the ZIP part
			with open(crx_path, 'rb') as f:
				# Read CRX header to find ZIP start
				magic = f.read(4)
				if magic != b'Cr24':
					raise Exception('Invalid CRX file format')

				version = int.from_bytes(f.read(4), 'little')
				if version == 2:
					pubkey_len = int.from_bytes(f.read(4), 'little')
					sig_len = int.from_bytes(f.read(4), 'little')
					f.seek(16 + pubkey_len + sig_len)  # Skip to ZIP data
				elif version == 3:
					header_len = int.from_bytes(f.read(4), 'little')
					f.seek(12 + header_len)  # Skip to ZIP data

				# Extract ZIP data
				zip_data = f.read()

			# Write ZIP data to temp file and extract

			with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as temp_zip:
				temp_zip.write(zip_data)
				temp_zip.flush()

				with zipfile.ZipFile(temp_zip.name, 'r') as zip_ref:
					zip_ref.extractall(extract_dir)

				os.unlink(temp_zip.name)
