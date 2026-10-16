"""Chunked operator/browser transfers through CDP; no shared filesystem needed."""
import base64
import hashlib
import json
import math
import mimetypes
import ntpath
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

CHUNK_SIZE = 32 * 1024


def _send(req, **kwargs):
    from .helpers import _send as send
    return send(req, **kwargs)


class _Transfer:
    def __init__(self, timeout, session_id=None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive finite seconds")
        self.end = time.monotonic() + timeout
        self.session_id = session_id

    def call(self, method, **params):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("file transfer deadline exceeded")
        return _send({"method": method, "params": params, "session_id": self.session_id,
                      "pinned": True}, timeout=remaining)["result"]

    def value(self, method, **params):
        from .helpers import _runtime_value
        return _runtime_value(self.call(method, returnByValue=True, **params), method)

    def cleanup(self, method, **params):
        try:
            _send({"method": method, "params": params, "session_id": self.session_id,
                   "pinned": True}, timeout=2)
        except Exception:
            pass


def upload_file(selector, path, *, source="local", session_id=None, timeout=300, progress=None):
    """Fill a file input from operator files, including on a remote browser.

    source='browser' preserves native DOM.setFileInputFiles for paths already
    on the browser host. Local transfers dispatch synthetic input/change events.
    The input is replaced only after every local file has been transferred.
    """
    if source not in {"local", "browser"}:
        raise ValueError("source must be local or browser")
    filenames = [path] if isinstance(path, (str, os.PathLike)) else list(path)
    local = [Path(p).expanduser().resolve() for p in filenames] if source == "local" else []
    sizes = [p.stat().st_size for p in local]
    if any(not p.is_file() for p in local):
        raise ValueError("uploads must be regular files")
    transfer = _Transfer(timeout, session_id or _send({"meta": "session"})["session_id"])
    group = "bh-upload-" + uuid.uuid4().hex
    try:
        response = transfer.call("Runtime.evaluate", expression="""(() => {
            const input = document.querySelector(%s);
            if (!input || input.tagName !== 'INPUT' || input.type !== 'file')
                throw new Error('selector must identify a file input');
            if (%d > 1 && !input.multiple) throw new Error('input does not accept multiple files');
            return {input, data: new DataTransfer(), chunks: []};
        })()""" % (json.dumps(selector), len(filenames)), objectGroup=group)
        from .helpers import _runtime_value
        _runtime_value(response, "prepare file input")
        holder = response["result"]["objectId"]
        if source == "browser":
            input_id = transfer.call("Runtime.callFunctionOn", objectId=holder,
                                     functionDeclaration="function(){return this.input}")["result"]["objectId"]
            transfer.call("DOM.setFileInputFiles", files=[os.fspath(p) for p in filenames], objectId=input_id)
            return {"source": "browser", "files": [os.fspath(p) for p in filenames]}
        transferred = 0
        receipts = []
        for file, size in zip(local, sizes):
            digest = hashlib.sha256()
            count = 0
            with file.open("rb") as stream:
                while chunk := stream.read(CHUNK_SIZE):
                    transfer.value("Runtime.callFunctionOn", objectId=holder,
                                   functionDeclaration="function(b){this.chunks.push(Uint8Array.from(atob(b), c=>c.charCodeAt(0)));}",
                                   arguments=[{"value": base64.b64encode(chunk).decode("ascii")}])
                    digest.update(chunk)
                    count += len(chunk)
                    transferred += len(chunk)
                    if progress:
                        progress({"transferred_bytes": transferred, "total_bytes": sum(sizes), "file": str(file)})
            if count != size:
                raise RuntimeError(f"local file size changed during upload: {file}")
            transfer.value("Runtime.callFunctionOn", objectId=holder,
                           functionDeclaration="""function(name,type,modified){
                               this.data.items.add(new File(this.chunks,name,{type,lastModified:modified}));
                               this.chunks=[];
                           }""", arguments=[{"value": file.name}, {"value": mimetypes.guess_type(file.name)[0] or "application/octet-stream"},
                                            {"value": int(file.stat().st_mtime * 1000)}])
            receipts.append({"name": file.name, "size": count, "sha256": digest.hexdigest()})
        transfer.value("Runtime.callFunctionOn", objectId=holder, functionDeclaration="""function(){
            if(!this.input.isConnected) throw new Error('file input was detached during transfer');
            this.input.files=this.data.files;
            this.input.dispatchEvent(new Event('input',{bubbles:true}));
            this.input.dispatchEvent(new Event('change',{bubbles:true}));
        }""")
        return {"source": "local", "files": receipts, "transferred_bytes": transferred}
    finally:
        transfer.cleanup("Runtime.releaseObjectGroup", objectGroup=group)


@contextmanager
def _browser_file(browser_path, transfer, browser_context_id=None):
    params = {"url": "about:blank", "background": True}
    if browser_context_id:
        params["browserContextId"] = browser_context_id
    target = transfer.call("Target.createTarget", **params)["targetId"]
    try:
        transfer.session_id = transfer.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        response = transfer.call("Runtime.evaluate", expression="""document.body.appendChild(
            Object.assign(document.createElement('input'),{type:'file'}))""")
        input_id = response["result"]["objectId"]
        transfer.call("DOM.setFileInputFiles", files=[os.fspath(browser_path)], objectId=input_id)
        response = transfer.call("Runtime.callFunctionOn", objectId=input_id,
                                 functionDeclaration="function(){if(!this.files.length)throw new Error('browser file unavailable');return this.files[0];}")
        from .helpers import _runtime_value
        _runtime_value(response, "open browser file")
        file_id = response["result"]["objectId"]
        metadata = transfer.value("Runtime.callFunctionOn", objectId=file_id,
                                  functionDeclaration="function(){return {name:this.name,size:this.size,type:this.type};}")
        yield file_id, metadata
    finally:
        transfer.cleanup("Target.closeTarget", targetId=target)


def fetch_browser_file(browser_path, destination, *, browser_context_id=None, timeout=300,
                       overwrite=False, progress=None, expected_size=None):
    """Copy a browser-host file to an operator path, atomically, through CDP.

    Uses a temporary background tab. The browser-host source is retained.
    A failed/cancelled copy never publishes a partial destination.
    """
    browser_path = os.fspath(browser_path)
    if not (browser_path.startswith("/") or ntpath.isabs(browser_path)):
        raise ValueError("browser_path must be absolute on the browser host")
    destination = Path(destination).expanduser().absolute()
    if not overwrite and destination.exists():
        raise FileExistsError(destination)
    transfer = _Transfer(timeout)
    temporary = None
    try:
        with _browser_file(browser_path, transfer, browser_context_id) as (file_id, metadata):
            size = metadata["size"]
            if expected_size is not None and size != expected_size:
                raise RuntimeError(f"browser file size {size} differs from completed download size {expected_size}")
            digest = hashlib.sha256()
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".bh-download-", delete=False) as output:
                temporary = Path(output.name)
                offset = 0
                while True:  # read even an empty file, to detect inaccessible paths
                    encoded = transfer.value("Runtime.callFunctionOn", objectId=file_id, awaitPromise=True,
                                             functionDeclaration="""async function(offset,length){
                                                 const blob=this.size===0 ? this : this.slice(offset,offset+length);
                                                 const bytes=new Uint8Array(await blob.arrayBuffer());
                                                 return btoa(String.fromCharCode(...bytes));
                                             }""", arguments=[{"value": offset}, {"value": CHUNK_SIZE}])
                    chunk = base64.b64decode(encoded, validate=True)
                    if len(chunk) != min(CHUNK_SIZE, size - offset):
                        raise RuntimeError("browser file changed or returned a short read")
                    output.write(chunk)
                    digest.update(chunk)
                    offset += len(chunk)
                    if progress:
                        progress({"transferred_bytes": offset, "total_bytes": size})
                    if offset >= size:
                        break
                output.flush()
                os.fsync(output.fileno())
            if overwrite:
                os.replace(temporary, destination)
            else:
                # Atomic, no-clobber publication even if another writer wins the race.
                os.link(temporary, destination)
            return {"path": str(destination), "size": size, "sha256": digest.hexdigest(), "browser_path": os.fspath(browser_path)}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def enable_downloads(browser_dir=None, *, browser_context_id=None):
    """Enable tracked downloads. Directory is on the browser host, not the operator."""
    return _send({"meta": "downloads_configure", "browser_dir": browser_dir, "browser_context_id": browser_context_id})


def disable_downloads():
    """Restore Chrome's default behavior; keeps records and downloaded files."""
    return _send({"meta": "downloads_disable"})


def list_downloads(*, after=0):
    return [r for r in _send({"meta": "download_list"})["downloads"] if r["sequence"] > after]


def download_status(download_id):
    return _send({"meta": "download_status", "download_id": download_id})["download"]


def cancel_download(download_id):
    return _send({"meta": "download_cancel", "download_id": download_id})["download"]


def forget_download(download_id):
    """Forget a finished record. Does not delete the browser-host file."""
    return _send({"meta": "download_forget", "download_id": download_id})


def wait_for_download(download_id=None, *, after=0, url=None, timeout=60, progress=None):
    """Wait for completion, optionally discovering one new download after a cursor.

    If several downloads match, require an explicit ID rather than guessing.
    Timeout leaves the browser download running and its status available.
    """
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be nonnegative finite seconds")
    end = time.monotonic() + timeout
    last = None
    while True:
        if download_id:
            record = download_status(download_id)
        else:
            matches = [r for r in list_downloads(after=after) if url is None or r["url"] == url]
            if len(matches) > 1:
                raise RuntimeError("multiple downloads match; use list_downloads() and an explicit download_id")
            record = matches[0] if matches else None
        if record:
            download_id = record["download_id"]
            if progress and record != last:
                progress(dict(record))
                last = record
            if record["state"] == "completed":
                return record
            if record["state"] in ("canceled", "interrupted"):
                raise RuntimeError(f"download {download_id} was canceled or failed in the browser")
        if time.monotonic() >= end:
            raise TimeoutError(f"download {download_id or '(not yet detected)'} did not complete; inspect list_downloads() before retrying")
        time.sleep(min(0.1, max(0, end - time.monotonic())))


def save_download(download_id, destination, *, timeout=300, overwrite=False, progress=None):
    """Retrieve a completed download; destination may be a file or existing directory."""
    record = download_status(download_id)
    if record["state"] != "completed":
        raise ValueError(f"download {download_id} is {record['state']}; wait for completion first")
    destination = Path(destination).expanduser()
    if destination.is_dir():
        name = (record.get("suggested_filename") or download_id).replace("\\", "/").split("/")[-1]
        destination = destination / (name if name not in {"", ".", ".."} else download_id)
    receipt = fetch_browser_file(record["browser_path"], destination,
                                 browser_context_id=record["browser_context_id"], timeout=timeout,
                                 overwrite=overwrite, progress=progress, expected_size=record["received_bytes"])
    return {**receipt, "download_id": download_id}
