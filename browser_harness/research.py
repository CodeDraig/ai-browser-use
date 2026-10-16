"""Explicit research evidence and local, resumable text extraction.

These helpers never initiate a site export, upload content, or install tools.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _artifact(path):
    return {'path': Path(path).name, 'size': Path(path).stat().st_size, 'sha256': _hash(path)}


def _url(value):
    parts = urlsplit(value)
    # Signed export links are not durable citations; do not archive their credentials.
    secret = {'iat', 'token', 'access_token', 'apikey', 'api_key', 'signature', 'sig', 'key', 'authorization'}
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in secret and not k.lower().startswith(('x-amz-', 'x-goog-'))]
    host = parts.netloc.rsplit('@', 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, urlencode(pairs), parts.fragment))


def _write(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.research-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _receipt(root, manifest):
    _write(root / 'manifest.json', manifest)
    lines = ['# Research receipt', '', f"Status: {manifest['status']}",
             f"Kind: {manifest['kind']}", f"Recorded: {manifest['created_at']}", '']
    if 'source' in manifest:
        lines += ['## Source', '', json.dumps(manifest['source'], ensure_ascii=False), '']
    if manifest.get('annotations'):
        lines += ['## Caller annotations', '', json.dumps(manifest['annotations'], ensure_ascii=False), '']
    for artifact in manifest.get('artifacts', []):
        lines.append(f"- {artifact['path']}: {artifact['size']} bytes; SHA-256 `{artifact['sha256']}`")
    for warning in manifest.get('warnings', []): lines += ['', warning]
    (root / 'RECEIPT.md').write_text('\n'.join(lines) + '\n')


def _base(kind, metadata=None):
    return {'schema_version': 1, 'kind': kind, 'status': 'incomplete',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'annotations': metadata or {}, 'artifacts': [], 'warnings': []}


def _new_destination(destination):
    root = Path(destination).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    return root


def _document():
    from . import helpers as h
    from .extension_helpers import call, enabled
    if enabled():
        return call('document_token')
    return h.js("({token:performance.timeOrigin,url:location.href})")


def capture_research(destination, *, metadata=None, screenshot=True):
    """Capture selected-page evidence. Returns an explicit incomplete manifest on a race."""
    from . import helpers as h
    root = _new_destination(destination)
    manifest = _base('page_capture', metadata)
    try:
        before = _document()
        page = h.read_page(include_frames=False)
        manifest['source'] = {'url': _url(page['url']), 'title': page['title']}
        page['url'] = _url(page['url'])
        for link in page.get('links', []): link['url'] = _url(link['url'])
        _write(root / 'page.json', page)
        (root / 'visible-text.txt').write_text(page['text'])
        manifest['artifacts'] = [_artifact(root / name) for name in ('page.json', 'visible-text.txt')]
        if screenshot:
            h.capture_screenshot(str(root / 'viewport.png'))
            manifest['artifacts'].append(_artifact(root / 'viewport.png'))
        if before != _document() or (page.get('document_token') and page['document_token'] != before['token']):
            raise RuntimeError('navigation_changed_during_capture')
        manifest['status'] = 'completed'
    except Exception as exc:
        manifest['error'] = str(exc).split(':')[0][:100]
        manifest['warnings'].append('Incomplete capture: artifacts may span a navigation or interrupted operation. Do not treat them as one document.')
    _receipt(root, manifest)
    return {**manifest, 'path': str(root)}


def save_research_download(download_id, destination, *, source, metadata=None):
    """Retrieve an existing completed download; source is a URL or observed citation mapping."""
    from . import helpers as h
    if not isinstance(source, (str, dict)) or not source:
        raise ValueError('source must be a URL or citation mapping')
    citation = {'url': source} if isinstance(source, str) else dict(source)
    if 'url' in citation: citation['url'] = _url(citation['url'])
    root = _new_destination(destination)
    manifest = _base('download', metadata)
    manifest['source'] = citation
    manifest['source_provenance'] = 'caller_supplied'
    try:
        original = root / 'original'
        original.mkdir()
        result = h.save_download(download_id, str(original))
        manifest['download_id'] = download_id
        manifest['artifacts'] = [_artifact(result['path'])]
        manifest['artifacts'][0]['path'] = str(Path(result['path']).relative_to(root))
        if manifest['artifacts'][0]['sha256'] != result['sha256']: raise ValueError('download_verification_failed')
        manifest['status'] = 'completed'
    except Exception:
        manifest['warnings'].append('Download retrieval failed. No export was initiated or retried.')
        _receipt(root, manifest)
        raise
    _receipt(root, manifest)
    return {**manifest, 'path': str(root)}


_TOOL_HELP = {'tesseract': 'Install Tesseract and the requested language data on the harness machine.',
              'pdfinfo': 'Install Poppler utilities on the harness machine.',
              'pdftotext': 'Install Poppler utilities on the harness machine.',
              'pdftoppm': 'Install Poppler utilities on the harness machine.'}


def extract_document(path, destination, *, method='auto', languages='eng', pages=None,
                     max_pages=50, timeout=300):
    """Extract 1-based pages locally; resume only matching source/settings and verified outputs.

    method: auto (PDF text then OCR), text (PDF only), or ocr.
    Limits apply per invocation. Existing successful pages are retained and verified.
    """
    from PIL import Image
    if method not in ('auto', 'text', 'ocr'): raise ValueError('method must be auto, text, or ocr')
    if not isinstance(max_pages, int) or max_pages < 1: raise ValueError('max_pages must be a positive integer')
    if not math.isfinite(timeout) or timeout <= 0: raise ValueError('timeout must be positive finite seconds')
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file(): raise ValueError('source must be a regular file')
    root = Path(destination).expanduser().resolve()
    if source == root or root in source.parents: raise ValueError('output must be separate from the source')
    digest = _hash(source)
    with source.open('rb') as stream: pdf = stream.read(5) == b'%PDF-'
    settings = {'method': method, 'languages': languages}
    manifest_path = root / 'manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('kind') != 'extraction' or manifest.get('source', {}).get('sha256') != digest or manifest.get('settings') != settings:
            raise ValueError('destination belongs to different source or extraction settings')
    else:
        root.mkdir(parents=True, exist_ok=False)
        manifest = _base('extraction')
        manifest.update(source={'path': str(source), 'sha256': digest}, settings=settings, pages={}, tools={})
    manifest['status'] = 'incomplete'
    manifest.pop('error', None)
    deadline = time.monotonic() + timeout

    def run(tool, args):
        binary = shutil.which(tool)
        if not binary: raise RuntimeError('missing_tool: ' + _TOOL_HELP[tool])
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('extraction_timeout')
        if tool not in manifest['tools']:
            v = subprocess.run([binary, '--version' if tool == 'tesseract' else '-v'], capture_output=True, timeout=remaining)
            manifest['tools'][tool] = (v.stdout or v.stderr).decode(errors='replace').splitlines()[0]
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('extraction_timeout')
        result = subprocess.run([binary, *args], capture_output=True, timeout=remaining)
        if result.returncode: raise RuntimeError(f'{tool}_failed: check source and installed language data')
        return result.stdout

    try:
        if pdf:
            info = run('pdfinfo', [str(source)]).decode(errors='replace')
            total = next(int(line.split(':', 1)[1]) for line in info.splitlines() if line.startswith('Pages:'))
        else:
            if method == 'text': raise ValueError('text method requires PDF')
            with Image.open(source) as image: total = getattr(image, 'n_frames', 1)
        selected = list(range(1, total + 1)) if pages is None else list(pages)
        if not selected or any(type(n) is not int or n < 1 or n > total for n in selected) or len(set(selected)) != len(selected):
            raise ValueError('pages must be unique 1-based page numbers within the document')
        manifest['requested_pages'] = selected
        manifest['total_pages'] = total
        attempted = 0
        for number in selected:
            key = str(number)
            previous = manifest['pages'].get(key)
            if previous and previous.get('status') == 'completed':
                old = root / previous['artifact']['path']
                if old.is_file() and _hash(old) == previous['artifact']['sha256']: continue
            if attempted >= max_pages:
                manifest['error'] = 'page_limit: resume this invocation or increase max_pages'
                break
            attempted += 1
            try:
                if time.monotonic() >= deadline: raise TimeoutError('extraction_timeout')
                text = ''
                engine = 'embedded_pdf_text'
                if pdf and method != 'ocr':
                    text = run('pdftotext', ['-f', key, '-l', key, '-layout', str(source), '-']).decode(errors='replace')
                if method == 'ocr' or (method == 'auto' and not text.strip()):
                    engine = 'tesseract_ocr'
                    with tempfile.TemporaryDirectory(prefix='.extract-', dir=root) as tmp:
                        raster = Path(tmp) / 'page.png'
                        if pdf:
                            run('pdftoppm', ['-f', key, '-l', key, '-singlefile', '-scale-to', '5000', '-png', str(source), str(raster.with_suffix(''))])
                        else:
                            with Image.open(source) as image:
                                image.seek(number - 1); image.convert('RGB').save(raster)
                        text = run('tesseract', [str(raster), 'stdout', '-l', languages]).decode(errors='replace')
                output = root / f'page-{number:04d}.txt'
                output.write_text(text)
                manifest['pages'][key] = {'status': 'completed', 'page': number, 'engine': engine,
                                          'uncorrected': engine == 'tesseract_ocr', 'artifact': _artifact(output)}
            except Exception as exc:
                manifest['pages'][key] = {'status': 'failed', 'page': number, 'error': str(exc)}
                manifest['error'] = str(exc)
                _receipt(root, manifest)
                break
            _receipt(root, manifest)
        if all(manifest['pages'].get(str(n), {}).get('status') == 'completed' for n in selected):
            manifest['status'] = 'completed'; manifest.pop('error', None)
        if _hash(source) != digest:
            manifest['status'] = 'incomplete'; manifest['error'] = 'source_changed_during_extraction'
        manifest['artifacts'] = [value['artifact'] for value in manifest['pages'].values() if value.get('artifact')]
        manifest['warnings'] = ['OCR is uncorrected; verify quotations and reading order against the original.'] if any(value.get('uncorrected') for value in manifest['pages'].values()) else []
    except Exception as exc:
        manifest['error'] = str(exc)
    _receipt(root, manifest)
    return {**manifest, 'path': str(root)}
