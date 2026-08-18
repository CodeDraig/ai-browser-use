import os
import stat
from pathlib import Path

import pytest

import browser_use.integrations.gmail.service as gmail_service
from browser_use.integrations.gmail.service import GmailService


def _mode(path: Path) -> int:
	return stat.S_IMODE(path.stat().st_mode)


def test_token_file_is_created_atomically_with_owner_only_permissions(tmp_path, monkeypatch):
	token_path = tmp_path / 'gmail_token.json'
	service = GmailService(token_file=str(token_path), config_dir=str(tmp_path))
	real_replace = os.replace
	replacements: list[tuple[Path, Path]] = []

	def record_replace(source, destination):
		replacements.append((Path(source), Path(destination)))
		real_replace(source, destination)

	monkeypatch.setattr(os, 'replace', record_replace)
	service._save_token_file('{"refresh_token":"secret"}')

	assert token_path.read_text() == '{"refresh_token":"secret"}'
	assert _mode(token_path) == 0o600
	assert replacements and replacements[0][0].parent == token_path.parent
	assert replacements[0][1] == token_path


def test_token_file_tightens_preexisting_permissions(tmp_path):
	token_path = tmp_path / 'gmail_token.json'
	token_path.write_text('old')
	token_path.chmod(0o666)

	GmailService(token_file=str(token_path), config_dir=str(tmp_path))._save_token_file('new')

	assert token_path.read_text() == 'new'
	assert _mode(token_path) == 0o600


@pytest.mark.asyncio
async def test_authenticate_rewrites_valid_preexisting_token_with_owner_only_permissions(tmp_path, monkeypatch):
	class ValidCredentials:
		valid = True

		def to_json(self):
			return '{"refresh_token":"preserved"}'

	token_path = tmp_path / 'gmail_token.json'
	token_path.write_text('permissive token')
	token_path.chmod(0o666)
	monkeypatch.setattr(gmail_service.Credentials, 'from_authorized_user_file', lambda *_args: ValidCredentials())
	monkeypatch.setattr(gmail_service, 'build', lambda *_args, **_kwargs: object())

	service = GmailService(token_file=str(token_path), config_dir=str(tmp_path))
	assert await service.authenticate() is True

	assert token_path.read_text() == '{"refresh_token":"preserved"}'
	assert _mode(token_path) == 0o600


@pytest.mark.asyncio
async def test_authenticate_atomically_rewrites_refreshed_token(tmp_path, monkeypatch):
	class ExpiredCredentials:
		valid = False
		expired = True
		refresh_token = 'refresh-token'
		refreshed = False

		def refresh(self, _request):
			self.valid = True
			self.refreshed = True

		def to_json(self):
			return '{"refresh_token":"refreshed"}'

	credentials = ExpiredCredentials()
	token_path = tmp_path / 'gmail_token.json'
	token_path.write_text('expired token')
	token_path.chmod(0o666)
	replacements = []
	real_replace = os.replace

	def record_replace(source, destination):
		replacements.append((Path(source), Path(destination)))
		real_replace(source, destination)

	monkeypatch.setattr(gmail_service.Credentials, 'from_authorized_user_file', lambda *_args: credentials)
	monkeypatch.setattr(gmail_service, 'build', lambda *_args, **_kwargs: object())
	monkeypatch.setattr(os, 'replace', record_replace)

	service = GmailService(token_file=str(token_path), config_dir=str(tmp_path))
	assert await service.authenticate() is True

	assert credentials.refreshed is True
	assert token_path.read_text() == '{"refresh_token":"refreshed"}'
	assert _mode(token_path) == 0o600
	assert replacements and replacements[0][0].parent == token_path.parent


@pytest.mark.skipif(not hasattr(os, 'symlink'), reason='symlinks are unavailable')
def test_token_file_replaces_symlink_without_following_it(tmp_path):
	target = tmp_path / 'unrelated.json'
	target.write_text('do not overwrite')
	token_path = tmp_path / 'gmail_token.json'
	token_path.symlink_to(target)

	GmailService(token_file=str(token_path), config_dir=str(tmp_path))._save_token_file('new token')

	assert not token_path.is_symlink()
	assert token_path.read_text() == 'new token'
	assert target.read_text() == 'do not overwrite'
	assert _mode(token_path) == 0o600


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, 'symlink'), reason='symlinks are unavailable')
async def test_authenticate_never_loads_token_through_symlink(tmp_path, monkeypatch):
	class NewCredentials:
		valid = True

		def to_json(self):
			return '{"refresh_token":"new"}'

	class Flow:
		def run_local_server(self, **_kwargs):
			return NewCredentials()

	target = tmp_path / 'unrelated.json'
	target.write_text('valid credentials belonging to another path')
	token_path = tmp_path / 'gmail_token.json'
	token_path.symlink_to(target)
	credentials_path = tmp_path / 'gmail_credentials.json'
	credentials_path.write_text('{}')

	def reject_symlink_load(*_args):
		raise AssertionError('token symlink was followed')

	monkeypatch.setattr(gmail_service.Credentials, 'from_authorized_user_file', reject_symlink_load)
	monkeypatch.setattr(gmail_service.InstalledAppFlow, 'from_client_secrets_file', lambda *_args: Flow())
	monkeypatch.setattr(gmail_service, 'build', lambda *_args, **_kwargs: object())

	service = GmailService(
		credentials_file=str(credentials_path), token_file=str(token_path), config_dir=str(tmp_path)
	)
	assert await service.authenticate() is True

	assert not token_path.is_symlink()
	assert token_path.read_text() == '{"refresh_token":"new"}'
	assert _mode(token_path) == 0o600
	assert target.read_text() == 'valid credentials belonging to another path'
