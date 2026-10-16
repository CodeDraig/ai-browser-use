"""Browser-launched native messaging host. Only registered downloads are readable."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import struct
import sys
import uuid
from pathlib import Path

MAX_MESSAGE = 1024 * 1024
CHUNK = 48 * 1024
MAX_CHUNK = 192 * 1024
FEATURES = {"download_deltas", "transfer_192k", "document_token"}


def read_exact(stream, length):
    data = bytearray()
    while len(data) < length:
        chunk = stream.read(length - len(data))
        if not chunk: raise EOFError()
        data.extend(chunk)
    return bytes(data)


def read_message(stream):
    size = struct.unpack('=I', read_exact(stream, 4))[0]
    if size > MAX_MESSAGE: raise ValueError('native_message_too_large')
    return json.loads(read_exact(stream, size))


def write_message(stream, message):
    raw = json.dumps(message).encode()
    if len(raw) > MAX_MESSAGE: raise ValueError('native_message_too_large')
    stream.write(struct.pack('=I', len(raw)) + raw)
    stream.flush()


class DownloadFiles:
    def __init__(self):
        self.records = {}
        self.chunk_size = CHUNK
        self.transfers = {}

    def register(self, records):
        # Called only from extension-originated messages, never a broker request.
        self.records = {str(r['download_id']): r for r in records
                        if r.get('state') == 'completed' and r.get('browser_path')}

    def update(self, records, removed=()):
        for record in records:
            key = str(record['download_id'])
            if record.get('state') == 'completed' and record.get('browser_path'):
                self.records[key] = record
            else:
                self.records.pop(key, None)
        for key in removed:
            self.records.pop(str(key), None)

    def call(self, operation, args):
        if operation == 'file_open':
            if len(self.transfers) >= 16: raise ValueError('too_many_open_transfers')
            record = self.records.get(str(args['download_id']))
            if not record: raise ValueError('download_not_registered_or_incomplete')
            path = Path(record['browser_path'])
            if not path.is_absolute() or path.is_symlink(): raise ValueError('invalid_download_file')
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            stream = os.fdopen(fd, 'rb')
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                stream.close(); raise ValueError('download_not_regular_file')
            # Network progress can lag completion (Firefox) or count compressed bytes.
            expected = record.get('file_size', record.get('received_bytes'))
            if expected is not None and expected != info.st_size:
                stream.close(); raise ValueError('download_size_changed')
            transfer_id = uuid.uuid4().hex
            self.transfers[transfer_id] = (stream, info, hashlib.sha256(), 0, min(self.chunk_size, max(CHUNK, int(args.get('chunk_size', CHUNK)))))
            return {'transfer_id': transfer_id, 'size': info.st_size, 'chunk_size': self.transfers[transfer_id][4]}
        tid = args['transfer_id']
        if tid not in self.transfers: raise ValueError('unknown_transfer')
        stream, info, digest, count, chunk_size = self.transfers[tid]
        if operation == 'file_close':
            stream.close(); del self.transfers[tid]; return {'closed': True}
        if operation != 'file_chunk': raise ValueError('unsupported_file_operation')
        if args.get('offset') != count: raise ValueError('invalid_transfer_offset')
        data = stream.read(chunk_size)
        digest.update(data); count += len(data)
        current = os.fstat(stream.fileno())
        if (current.st_size, current.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
            stream.close(); del self.transfers[tid]; raise ValueError('download_changed_during_transfer')
        self.transfers[tid] = (stream, info, digest, count, chunk_size)
        return {'data': base64.b64encode(data).decode(), 'offset': count,
                'eof': count == info.st_size, 'sha256': digest.hexdigest() if count == info.st_size else None}

    def close(self):
        for stream, *_ in self.transfers.values(): stream.close()
        self.transfers.clear()


async def run(config):
    from websockets.asyncio.client import connect
    downloads = DownloadFiles()
    incoming = asyncio.Queue()
    loop = asyncio.get_running_loop()
    # A daemon thread permits prompt process exit when the browser closes stdin.
    import threading
    def reader():
        chunks = {}
        try:
            while True:
                message = read_message(sys.stdin.buffer)
                if message.get('type') == 'chunk':
                    key = message['transfer']
                    value = chunks.get(key, '') + message['data']
                    if len(value) > 16 * MAX_MESSAGE or len(chunks) > 4:
                        raise ValueError('native_message_too_large')
                    if not message.get('last'):
                        chunks[key] = value
                        continue
                    chunks.pop(key, None)
                    message = json.loads(value)
                loop.call_soon_threadsafe(incoming.put_nowait, message)
        except (EOFError, ValueError, OSError):
            loop.call_soon_threadsafe(incoming.put_nowait, None)
    threading.Thread(target=reader, daemon=True).start()
    first = await incoming.get()
    if not first or first.get('type') != 'hello': return
    from .extension import handshake_proof
    delay = 1
    try:
        while True:
            try:
                async with connect(f"ws://127.0.0.1:{config['port']}", max_size=32 * MAX_MESSAGE) as ws:
                    greeting = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    if greeting.get('version', 1) != 1: raise ValueError('protocol_version_mismatch: upgrade the extension, native host and harness together')
                    challenge = greeting.get('challenge')
                    if not isinstance(challenge, str) or len(challenge) != 64: raise ValueError('invalid_challenge')
                    hello = {**first, 'version': 1, 'native_features': sorted(FEATURES), 'features': sorted(FEATURES & (set(first.get('features', [])) | {'transfer_192k'})), 'nonce': secrets.token_hex(32)}
                    hello['proof'] = handshake_proof(config['token'], 'client', challenge, hello)
                    await ws.send(json.dumps(hello))
                    acknowledgement = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    expected = handshake_proof(config['token'], 'server', challenge, hello)
                    if not acknowledgement.get('connected') or not hmac.compare_digest(str(acknowledgement.get('proof', '')), expected):
                        raise ValueError('bridge_authentication_failed')
                    negotiated = set(acknowledgement.get('features', [])) & FEATURES
                    downloads.chunk_size = MAX_CHUNK if 'transfer_192k' in negotiated else CHUNK
                    write_message(sys.stdout.buffer, {'type': 'status', 'connected': True, 'features': sorted(negotiated), 'harness': f'127.0.0.1:{config["port"]}'})
                    delay = 1
                    async def from_browser():
                        while True:
                            msg = await incoming.get()
                            if msg is None: return
                            if msg.get('type') == 'downloads': downloads.register(msg['records'])
                            elif msg.get('type') == 'download_delta': downloads.update(msg['records'], msg.get('removed', []))
                            elif 'id' in msg: await ws.send(json.dumps(msg))
                    async def from_broker():
                        async for raw in ws:
                            msg = json.loads(raw)
                            if msg.get('operation', '').startswith('file_'):
                                try: reply = {'id': msg['id'], 'result': downloads.call(msg['operation'], msg.get('args', {}))}
                                except (ValueError, KeyError, OSError) as exc: reply = {'id': msg['id'], 'error': str(exc)}
                                await ws.send(json.dumps(reply))
                            else: write_message(sys.stdout.buffer, msg)
                    browser_task = asyncio.create_task(from_browser())
                    broker_task = asyncio.create_task(from_broker())
                    done, pending = await asyncio.wait([browser_task, broker_task], return_when=asyncio.FIRST_COMPLETED)
                    for task in pending: task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    if browser_task in done and browser_task.exception() is None: return
                    for task in done: task.result()
            except Exception as exc:
                downloads.close()
                detail = str(exc) if str(exc).startswith('protocol_version_mismatch:') else f'Bridge unavailable ({type(exc).__name__}); reconnecting without replay.'
                write_message(sys.stdout.buffer, {'type': 'status', 'connected': False, 'error': detail})
            # Consume browser-side registry updates while waiting, never replay responses.
            try:
                msg = await asyncio.wait_for(incoming.get(), delay)
                if msg is None: return
                if msg.get('type') == 'downloads': downloads.register(msg['records'])
                elif msg.get('type') == 'download_delta': downloads.update(msg['records'], msg.get('removed', []))
            except asyncio.TimeoutError: pass
            delay = min(delay * 2, 10)
    finally:
        downloads.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args, caller = parser.parse_known_args()
    config = json.loads(args.config.read_text())
    identities = config.get('identities', {})
    allowed = {f'chrome-extension://{identities["chrome"]}/'} if 'chrome' in identities else set()
    if 'firefox' in identities: allowed.add(identities['firefox'])
    # Firefox supplies manifest path then extension ID; Chrome supplies origin.
    if not any(value in allowed for value in caller):
        raise SystemExit('unregistered native messaging caller')
    asyncio.run(run(config))

if __name__ == '__main__': main()
