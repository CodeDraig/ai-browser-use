"""Public clients for daemon-owned long-running operations."""
import math
import time
import uuid

from .operations import TERMINAL


def _send(req):
    from .helpers import _send as send
    return send(req)


class OperationPending(TimeoutError):
    def __init__(self, operation_id):
        self.operation_id = operation_id
        super().__init__(f"operation {operation_id} is still running; retrieve it later or cancel it")


class OperationFailed(RuntimeError):
    def __init__(self, operation):
        self.operation = operation
        self.operation_id = operation["operation_id"]
        super().__init__(f"operation {self.operation_id}: {operation['state']}: {operation.get('error', '')}")


class OperationSubmissionError(RuntimeError):
    def __init__(self, operation_id, error):
        self.operation_id = operation_id
        super().__init__(f"submission acknowledgement unavailable for {operation_id}: {error}; check operation_status before retrying")


def _submit(kind, deadline, operation_id, session_id, **kwargs):
    oid = operation_id or uuid.uuid4().hex
    try:
        response = _send({"meta": "operation_submit", "operation_id": oid, "kind": kind,
                          "deadline": deadline, "session_id": session_id, **kwargs})
        if response.get("operation", {}).get("operation_id") != oid:
            raise ValueError("missing submission acknowledgement")
    except (OSError, ValueError) as exc:
        raise OperationSubmissionError(oid, exc) from exc
    return oid


def start_js(body, *, deadline=300, operation_id=None, session_id=None):
    """Submit an async JS function body. Use signal and report_progress(value).

    Returns an operation ID immediately. A deadline stops waiting and signals
    cancellation; browser side effects require the body to honor `signal`.
    """
    return _submit("js", deadline, operation_id, session_id, body=body)


def start_cdp(method, *, params=None, session_id=None, deadline=300, operation_id=None):
    """Submit raw CDP. Cancelling stops waiting; CDP itself has no generic undo."""
    return _submit("cdp", deadline, operation_id, session_id, method=method, params=params or {})


def operation_status(operation_id):
    return _send({"meta": "operation_status", "operation_id": operation_id})["operation"]


def list_operations():
    return _send({"meta": "operation_list"})["operations"]


def cancel_operation(operation_id):
    return _send({"meta": "operation_cancel", "operation_id": operation_id})["operation"]


def forget_operation(operation_id):
    """Release a finished operation's record and result; no browser effects."""
    return _send({"meta": "operation_forget", "operation_id": operation_id})


def operation_result(operation_id, *, timeout=0, progress=None):
    """Wait up to timeout seconds (None: unlimited); a wait timeout leaves work running."""
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        raise ValueError("timeout must be nonnegative finite seconds, or None")
    end = None if timeout is None else time.monotonic() + timeout
    last_progress = object()
    while True:
        record = operation_status(operation_id)
        if progress and record["progress"] != last_progress:
            last_progress = record["progress"]
            progress(last_progress)
        if record["state"] in TERMINAL and "finished_at" in record:
            if record["state"] != "completed":
                raise OperationFailed(record)
            record = _send({"meta": "operation_status", "operation_id": operation_id, "include_result": True})["operation"]
            if record["kind"] == "js":
                from .helpers import _runtime_value
                return _runtime_value(record["result"], f"operation {operation_id}")
            return record["result"]
        if end is not None and time.monotonic() >= end:
            raise OperationPending(operation_id)
        time.sleep(0.1 if end is None else min(0.1, max(0, end - time.monotonic())))
