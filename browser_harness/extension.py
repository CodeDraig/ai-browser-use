"""Authenticated extension broker and explicit operator setup commands."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import uuid
from collections import deque
from pathlib import Path

from . import paths

HOST = 'org.browser_harness.bridge'
FIREFOX_ID = 'browser-harness@local'


def config_path():
    return paths.config_dir() / 'extension.json'


def read_config():
    try:
        return json.loads(config_path().read_text())
    except FileNotFoundError:
        raise RuntimeError('extension_not_configured: run browser-harness extension configure') from None


def write_config(config):
    path = config_path()
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(config, f, indent=2)
    os.replace(temp, path)


def handshake_proof(token, role, challenge, hello):
    payload = json.dumps([role, challenge, hello.get('nonce'), hello.get('connection_id'),
                          hello.get('browser'), hello.get('version')], separators=(',', ':'))
    return hmac.new(token.encode(), payload.encode(), hashlib.sha256).hexdigest()


class Broker:
    def __init__(self, config):
        self.config = config
        self.connections = {}
        self.selected = config.get('selected')
        self.pending = {}
        self.tabs = {}
        self.transfers = {}
        self.server = None
        self.recent = deque(maxlen=100)

    async def start(self):
        from websockets.asyncio.server import serve
        self.server = await serve(self.accept, '127.0.0.1', self.config['port'],
                                  max_size=32 * 1024 * 1024, origins=[None])

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def accept(self, ws):
        cid = None
        try:
            challenge = secrets.token_hex(32)
            await ws.send(json.dumps({'challenge': challenge, 'version': 1}))
            hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
            expected = handshake_proof(self.config['token'], 'client', challenge, hello)
            if not hmac.compare_digest(str(hello.get('proof', '')), expected):
                await ws.close(1008, 'unauthorized')
                return
            cid = hello.get('connection_id')
            if hello.get('version') != 1 or not isinstance(cid, str) or not re.fullmatch(r'[\w-]{1,80}', cid):
                await ws.close(1008, 'invalid_handshake')
                return
            if cid in self.connections:
                await ws.close(1008, 'connection_already_present')
                return
            features = sorted(set(hello.get('features', [])) & set(hello.get('native_features', [])) & {'transfer_192k', 'download_deltas', 'document_token'})
            self.connections[cid] = {'ws': ws, 'browser': hello.get('browser'), 'label': hello.get('label', cid), 'features': features}
            await ws.send(json.dumps({'connected': True, 'features': features, 'proof': handshake_proof(self.config['token'], 'server', challenge, hello)}))
            async for raw in ws:
                msg = json.loads(raw)
                future = self.pending.get((cid, msg.get('id')))
                if future and not future.done():
                    future.set_result(msg)
        finally:
            if cid and self.connections.get(cid, {}).get('ws') is ws:
                del self.connections[cid]
                self.tabs.pop(cid, None)
                for (owner, _), future in list(self.pending.items()):
                    if owner == cid and not future.done():
                        future.set_exception(RuntimeError('bridge_disconnected: outcome may be unknown; command was not replayed'))

    def connection(self):
        if self.selected:
            if self.selected not in self.connections:
                raise RuntimeError('selected_connection_disconnected')
            return self.selected
        if len(self.connections) != 1:
            raise RuntimeError('select_connection: use extension list and extension select ID')
        # Pin the first use. Never silently migrate if another browser arrives.
        self.selected = next(iter(self.connections))
        return self.selected

    async def call(self, operation, args=None, *, connection=None, tab=True):
        cid = connection or self.connection()
        entry = self.connections.get(cid)
        if not entry:
            raise RuntimeError('selected_connection_disconnected')
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[cid, request_id] = future
        started = time.monotonic()
        error_code = None
        try:
            await entry['ws'].send(json.dumps({'id': request_id, 'operation': operation,
                'args': args or {}, 'tab_id': self.tabs.get(cid) if tab else None}))
            result = await asyncio.wait_for(future, 25)
            if 'error' in result:
                error_code = re.sub(r'[^a-zA-Z_]', '', str(result['error']).split(':')[0])[:60]
                raise RuntimeError(result['error'])
            return result.get('result')
        except RuntimeError as exc:
            error_code = re.sub(r'[^a-zA-Z_]', '', str(exc).split(':')[0])[:60]
            raise
        except asyncio.TimeoutError:
            error_code = 'command_timeout'
            raise RuntimeError('command_timeout: outcome may be unknown; command was not replayed') from None
        finally:
            self.pending.pop((cid, request_id), None)
            self.recent.append({"connection_id": cid, "operation": re.sub(r"[^a-zA-Z_]", "", operation)[:40], "seconds": round(time.monotonic()-started, 6), "error": error_code})

    async def handle(self, req):
        meta = req.get('meta', '')
        if meta == 'extension_diagnostics':
            entries = []
            for cid, connection in list(self.connections.items()):
                item = {'connection_id': cid, 'browser': connection['browser'], 'selected': cid == self.selected,
                        'tab_id': self.tabs.get(cid), 'features': connection.get('features', [])}
                try:
                    item['capabilities'] = await self.call('capabilities', connection=cid, tab=False)
                    item['healthy'] = True
                except Exception:
                    item['healthy'] = False
                entries.append(item)
            return {'connections': entries, 'recent_operations': list(self.recent)}
        if meta == 'extension_connections':
            return {'connections': [dict(connection_id=k, browser=v['browser'], label=v['label'], selected=k == self.selected, features=v.get('features', []))
                                    for k, v in self.connections.items()]}
        if meta == 'extension_select':
            cid = req['connection_id']
            if cid not in self.connections:
                raise RuntimeError('unknown_connection')
            self.selected = cid
            self.config['selected'] = cid
            write_config(self.config)
            return {'selected': cid}
        if meta == 'extension_call':
            op, args = req['operation'], req.get('args', {})
            cid = self.connection()
            if op in ('switch_tab', 'current_tab', 'page_info', 'new_tab', 'list_tabs', 'capabilities'):
                pass
            elif self.tabs.get(cid) is None:
                raise RuntimeError('tab_not_selected: use list_tabs() and switch_tab(targetId)')
            if op == 'switch_tab':
                result = await self.call(op, args, connection=cid, tab=False)
                self.tabs[cid] = result['targetId']
            else:
                result = await self.call(op, args, connection=cid)
                if op == 'new_tab': self.tabs[cid] = result['targetId']
                if op == 'close_tab': self.tabs.pop(cid, None)
            if op == 'document_token':
                result = {**result, 'tab': self.tabs.get(cid), 'connection_id': cid}
            return {'result': result}
        if meta == 'connection_status':
            cid = self.connection()
            page = await self.call('current_tab', connection=cid)
            return {'backend': 'extension', 'connection_id': cid, 'page': page,
                    'target_id': self.tabs.get(cid), 'session_id': None}
        if meta == 'current_tab':
            return await self.call('current_tab')
        if meta.startswith('download'):
            args = {k:v for k,v in req.items() if k != 'meta'}
            cid = self.connection()
            if 'download_id' in args:
                owner, separator, local_id = args['download_id'].partition(':')
                if not separator or owner != cid: raise RuntimeError('download_connection_mismatch')
                args['download_id'] = local_id
            result = await self.call(meta, args, connection=cid, tab=False)
            def qualified(record):
                return {**record, 'download_id': cid + ':' + str(record['download_id'])}
            if 'download' in result: result['download'] = qualified(result['download'])
            if 'downloads' in result: result['downloads'] = [qualified(r) for r in result['downloads']]
            return result
        if meta == 'extension_file':
            args = dict(req['args'])
            action = req['action']
            if action == 'open':
                owner, separator, local_id = args['download_id'].partition(':')
                if not separator or owner != self.connection(): raise RuntimeError('download_connection_mismatch')
                args['download_id'] = local_id
                result = await self.call('file_open', args, connection=owner, tab=False)
                self.transfers[result['transfer_id']] = owner
                return result
            owner = self.transfers.get(args.get('transfer_id'))
            if owner is None: raise RuntimeError('unknown_transfer')
            try: return await self.call('file_' + action, args, connection=owner, tab=False)
            finally:
                if action == 'close': self.transfers.pop(args['transfer_id'], None)
        if 'method' in req:
            if req.get('session_id') is not None: raise RuntimeError('unsupported_capability: explicit CDP session_id in extension mode')
            return {'result': await self.call('cdp', {'method': req['method'], 'params': req.get('params', {})})}
        raise RuntimeError('unsupported_capability: ' + (meta or 'request'))


def manifest_path(browser, platform=None):
    platform = platform or sys.platform
    if platform == 'win32':
        return paths.config_dir() / f'{HOST}-{browser}.json'
    if platform == 'darwin':
        base = Path.home() / 'Library/Application Support'
        base /= 'Google/Chrome/NativeMessagingHosts' if browser == 'chrome' else 'Mozilla/NativeMessagingHosts'
    else:
        base = (Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config'))) / 'google-chrome/NativeMessagingHosts' if browser == 'chrome' else Path.home()/'.mozilla/native-messaging-hosts')
    return base / (HOST + '.json')


def package(browser, destination):
    from importlib.resources import files
    root = files('browser_harness').joinpath('extension_assets')
    dest = Path(destination).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for name in ('background.js', 'page.js', 'popup.html', 'popup.js'):
        (dest / name).write_bytes(root.joinpath(name).read_bytes())
    (dest / 'manifest.json').write_bytes(root.joinpath(browser + '.json').read_bytes())
    return str(dest)


def cli(argv):
    parser = argparse.ArgumentParser(prog='browser-harness extension')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('configure')
    p.add_argument('--port', type=int, default=8766)
    p.add_argument('--token-file', type=Path)
    p.add_argument('--export-token', type=Path)
    p = sub.add_parser('package'); p.add_argument('browser', choices=['chrome', 'firefox']); p.add_argument('destination')
    for command in ('install-host', 'remove-host'):
        p = sub.add_parser(command); p.add_argument('browser', choices=['chrome', 'firefox'])
        p.add_argument('--manifest-dir', type=Path, help='override native manifest directory for a custom browser user-data directory')
        if command == 'install-host': p.add_argument('--extension-id')
    sub.add_parser('list'); sub.add_parser('status')
    p = sub.add_parser('select'); p.add_argument('connection_id')
    args = parser.parse_args(argv)
    if args.command == 'configure':
        if not 1 <= args.port <= 65535: parser.error('port must be between 1 and 65535')
        config = json.loads(config_path().read_text()) if config_path().exists() else {}
        token = args.token_file.read_text().strip() if args.token_file else config.get('token', secrets.token_hex(32))
        if not re.fullmatch(r'[a-f0-9]{64}', token): parser.error('token must be 64 lowercase hexadecimal characters')
        config.update(port=args.port, token=token)
        write_config(config)
        if args.export_token:
            fd = os.open(args.export_token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f: f.write(token + '\n')
        print('Configured loopback bridge. Use BH_BACKEND=extension and a dedicated BU_NAME; restart an existing daemon after changes.')
    elif args.command == 'package':
        print(package(args.browser, args.destination))
    elif args.command in ('install-host', 'remove-host'):
        target = args.manifest_dir.resolve() / (HOST + '.json') if args.manifest_dir else manifest_path(args.browser)
        if args.command == 'remove-host':
            target.unlink(missing_ok=True)
            if sys.platform == 'win32':
                import winreg
                key = ('Software\\Google\\Chrome' if args.browser == 'chrome' else 'Software\\Mozilla') + '\\NativeMessagingHosts\\' + HOST
                try: winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
                except FileNotFoundError: pass
            print('Removed host registration: ' + str(target)); return 0
        if args.browser == 'chrome' and not re.fullmatch('[a-p]{32}', args.extension_id or ''):
            parser.error('Chrome requires --extension-id from chrome://extensions')
        config = read_config()
        identity = args.extension_id if args.browser == 'chrome' else FIREFOX_ID
        identities = config.setdefault('identities', {})
        identities[args.browser] = identity
        write_config(config)
        directory = paths.config_dir()
        runner = directory / 'native-host.py'
        runner.write_text('import sys\nsys.path.insert(0, ' + repr(str(Path(__file__).resolve().parent.parent)) + ')\nfrom browser_harness.native_host import main\nmain()\n')
        launcher = directory / ('native-host.cmd' if sys.platform == 'win32' else 'native-host')
        # Fixed local launcher; all runtime requests remain data, never shell input.
        if sys.platform == 'win32':
            launcher.write_text(f'@echo off\r\n"{sys.executable}" "{runner}" --config "{config_path()}" %*\r\n')
        else:
            import shlex
            launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(runner)) + ' --config ' + shlex.quote(str(config_path())) + ' "$@"\n')
            launcher.chmod(0o700)
        manifest = dict(name=HOST, description='Browser Harness local bridge', path=str(launcher), type='stdio')
        manifest['allowed_origins' if args.browser == 'chrome' else 'allowed_extensions'] = [f'chrome-extension://{identity}/' if args.browser == 'chrome' else identity]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest, indent=2))
        if sys.platform == 'win32':
            import winreg
            key = ('Software\\Google\\Chrome' if args.browser == 'chrome' else 'Software\\Mozilla') + '\\NativeMessagingHosts\\' + HOST
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as reg: winreg.SetValueEx(reg, '', 0, winreg.REG_SZ, str(target))
        print(target)
    else:
        os.environ['BH_BACKEND'] = 'extension'
        from .admin import ensure_daemon
        from .helpers import _send
        ensure_daemon()
        request = {'meta': 'extension_select', 'connection_id': args.connection_id} if args.command == 'select' else {'meta': 'extension_connections'}
        result = _send(request)
        if args.command == 'status':
            result.update(_send({'meta': 'extension_diagnostics'}))
            config = read_config()
            result.update(backend='extension', endpoint=f"127.0.0.1:{config['port']}", config_path=str(config_path()),
                          registered_hosts={browser: str(manifest_path(browser)) for browser in ('chrome','firefox') if manifest_path(browser).exists()})
        print(json.dumps(result, indent=2))
    return 0
