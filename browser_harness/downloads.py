"""Browser download lifecycle, independent of the lossy general event buffer."""
import ntpath
import posixpath
import re
import time
import uuid


class Downloads:
    def __init__(self, daemon):
        self.daemon = daemon
        self.directory = None
        self.context_id = None
        self.enabled = False
        self.sequence = 0
        self.records = {}

    async def configure(self, directory=None, context_id=None):
        if self.enabled and (directory is None or directory == self.directory) and context_id == self.context_id:
            return self.configuration()
        if any(r["state"] == "inProgress" for r in self.records.values()):
            raise ValueError("cannot reconfigure downloads while a download is in progress")
        if self.enabled and context_id != self.context_id:
            raise ValueError("disable downloads before changing browser context")
        if directory is None:
            version = await self.daemon.cdp.send_raw("Browser.getVersion")
            if "Windows" in version.get("userAgent", ""):
                raise ValueError("supply browser_dir: an absolute writable directory on the Windows browser host")
            directory = f"/tmp/browser-harness-downloads-{uuid.uuid4().hex}"
        if not isinstance(directory, str) or not (posixpath.isabs(directory) or ntpath.isabs(directory)):
            raise ValueError("browser_dir must be an absolute path on the browser host")
        params = {"behavior": "allowAndName", "downloadPath": directory, "eventsEnabled": True}
        if context_id:
            params["browserContextId"] = context_id
        await self.daemon.cdp.send_raw("Browser.setDownloadBehavior", params)
        self.directory, self.context_id, self.enabled = directory, context_id, True
        return self.configuration()

    def configuration(self):
        return {"browser_dir": self.directory, "browser_context_id": self.context_id,
                "enabled": self.enabled, "cursor": self.sequence}

    async def disable(self):
        if any(r["state"] == "inProgress" for r in self.records.values()):
            raise ValueError("wait for or cancel active downloads before disabling tracking")
        params = {"behavior": "default", "eventsEnabled": False}
        if self.context_id:
            params["browserContextId"] = self.context_id
        await self.daemon.cdp.send_raw("Browser.setDownloadBehavior", params)
        self.enabled = False
        return self.configuration()

    def on_event(self, method, params):
        if not self.enabled:
            return
        if method == "Browser.downloadWillBegin":
            guid = params["guid"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", guid):
                return
            self.sequence += 1
            join = ntpath.join if re.match(r"^[A-Za-z]:|^\\\\", self.directory) else posixpath.join
            self.records[guid] = {
                "download_id": guid, "sequence": self.sequence, "url": params.get("url"),
                "suggested_filename": params.get("suggestedFilename"), "frame_id": params.get("frameId"),
                "state": "inProgress", "received_bytes": 0, "total_bytes": 0,
                "browser_path": join(self.directory, guid), "browser_context_id": self.context_id,
                "started_at": time.time(),
            }
        elif method == "Browser.downloadProgress" and params.get("guid") in self.records:
            record = self.records[params["guid"]]
            record.update(state=params["state"], received_bytes=params.get("receivedBytes", 0),
                          total_bytes=params.get("totalBytes", 0), updated_at=time.time())
            if params["state"] in {"completed", "canceled"}:
                record["finished_at"] = time.time()

    def status(self, guid):
        if guid not in self.records:
            raise ValueError(f"unknown download {guid}; enable tracking before triggering the download")
        return dict(self.records[guid])

    async def cancel(self, guid):
        record = self.status(guid)
        if record["state"] == "inProgress":
            params = {"guid": guid}
            if record["browser_context_id"]:
                params["browserContextId"] = record["browser_context_id"]
            await self.daemon.cdp.send_raw("Browser.cancelDownload", params)
            self.records[guid]["cancel_requested"] = True
        return self.status(guid)

    def forget(self, guid):
        if self.status(guid)["state"] == "inProgress":
            raise ValueError("cannot forget an active download")
        del self.records[guid]
