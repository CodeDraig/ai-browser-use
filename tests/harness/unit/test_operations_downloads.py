import asyncio
from types import SimpleNamespace

import pytest

from browser_harness import managed
from browser_harness.downloads import Downloads
from browser_harness.operations import BINDING, Operations


class CDP:
    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()

    async def send_raw(self, method, params=None, session_id=None):
        self.calls.append((method, params, session_id))
        if method == "slow":
            await self.release.wait()
            return {"value": 42}
        return {}


def test_operations_pin_session_deduplicate_and_keep_result():
    async def run():
        cdp = CDP()
        daemon = SimpleNamespace(cdp=cdp, session="old")
        ops = Operations(daemon)
        req = {"operation_id": "one", "kind": "cdp", "method": "slow", "deadline": 10}
        ops.submit(req)
        daemon.session = "new"
        assert ops.submit(req)["operation_id"] == "one"
        await asyncio.sleep(0)
        assert cdp.calls == [("slow", {}, "old")]
        with pytest.raises(ValueError, match="running"):
            ops.forget("one")
        cdp.release.set()
        await ops.tasks["one"]
        assert "result" not in ops.status("one")
        assert ops.status("one", True)["result"] == {"value": 42}
        ops.forget("one")
        with pytest.raises(ValueError, match="unknown"):
            ops.status("one")
    asyncio.run(run())


def test_queued_cancel_deadline_and_bounded_capacity():
    async def run():
        cdp = CDP()
        ops = Operations(SimpleNamespace(cdp=cdp, session="s"), capacity=1)
        ops.submit({"operation_id": "one", "kind": "cdp", "method": "slow"})
        assert (await ops.cancel("one"))["state"] == "cancelled"
        assert cdp.calls == []  # cancellation before execution is not an action
        with pytest.raises(RuntimeError, match="capacity"):
            ops.submit({"operation_id": "two", "kind": "cdp", "method": "slow"})
        ops.forget("one")
        ops.submit({"operation_id": "two", "kind": "cdp", "method": "slow", "deadline": 0.01})
        await ops.tasks["two"]
        record = ops.status("two")
        assert record["state"] == "timed_out"
        assert record["cancellation"]["browser_effects_may_continue"] is True
    asyncio.run(run())


def test_progress_rejects_wrong_session_and_malformed_payloads():
    ops = Operations(SimpleNamespace())
    ops.records["one"] = {"state": "running", "session_id": "s", "progress": None}
    for payload in ["{", "[]", '"hello"', '{"operation_id":"absent"}']:
        ops.on_event("Runtime.bindingCalled", {"name": BINDING, "payload": payload}, "s")
    event = {"name": BINDING, "payload": '{"operation_id":"one", "value":{"bytes":42}}'}
    ops.on_event("Runtime.bindingCalled", event, "other-session")
    assert ops.records["one"]["progress"] is None
    ops.on_event("Runtime.bindingCalled", event, "s")
    assert ops.records["one"]["progress"] == {"bytes": 42}


def test_download_records_survive_reconfigure_and_refuse_active_changes():
    async def run():
        cdp = CDP()
        downloads = Downloads(SimpleNamespace(cdp=cdp))
        await downloads.configure("/browser/downloads", "context")
        downloads.on_event("Browser.downloadWillBegin", {"guid": "one", "url": "https://example.test/report",
                                                       "suggestedFilename": "../report.bin"})
        assert downloads.status("one")["browser_path"] == "/browser/downloads/one"
        with pytest.raises(ValueError, match="progress"):
            await downloads.configure("/other", "context")
        await downloads.cancel("one")
        assert cdp.calls[-1][:2] == ("Browser.cancelDownload", {"guid": "one", "browserContextId": "context"})
        downloads.on_event("Browser.downloadProgress", {"guid": "one", "state": "completed", "receivedBytes": 8, "totalBytes": 8})
        await downloads.configure("/other", "context")
        assert downloads.status("one")["browser_path"] == "/browser/downloads/one"
        assert downloads.status("one")["state"] == "completed"
        await downloads.disable()
        downloads.forget("one")
    asyncio.run(run())


@pytest.mark.parametrize("reply", [{}, {"operation": {"operation_id": "wrong"}}])
def test_submission_missing_ack_retains_id(monkeypatch, reply):
    monkeypatch.setattr(managed, "_send", lambda req: reply)
    with pytest.raises(managed.OperationSubmissionError) as exc:
        managed.start_js("return 1", operation_id="recoverable")
    assert exc.value.operation_id == "recoverable"


def test_submission_transport_failure_retains_id(monkeypatch):
    def timeout(req):
        raise TimeoutError("disconnected")
    monkeypatch.setattr(managed, "_send", timeout)
    with pytest.raises(managed.OperationSubmissionError) as exc:
        managed.start_js("return 1", operation_id="recoverable")
    assert exc.value.operation_id == "recoverable"
