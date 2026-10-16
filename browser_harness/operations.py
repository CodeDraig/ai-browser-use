"""Daemon-owned operations; callers can disconnect without losing the result."""
import asyncio
import json
import math
import re
import time

TERMINAL = {"completed", "failed", "cancelled", "timed_out"}
BINDING = "__browser_harness_progress"
REGISTRY = "__browser_harness_operations"


class Operations:
    def __init__(self, daemon, capacity=256):
        self.daemon = daemon
        self.capacity = capacity
        self.records = {}
        self.tasks = {}
        self.specs = {}

    def status(self, operation_id, include_result=False):
        if operation_id not in self.records:
            raise ValueError(f"unknown operation {operation_id}; records last for this daemon's lifetime")
        record = dict(self.records[operation_id])
        if not include_result:
            record.pop("result", None)
        return record

    def submit(self, req):
        oid = req["operation_id"]
        if not isinstance(oid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", oid):
            raise ValueError("operation_id must be 1-128 letters, digits, underscores or hyphens")
        spec = {k: v for k, v in req.items() if k not in {"token", "meta"}}
        if oid in self.records:
            if self.specs[oid] != spec:
                raise ValueError("operation_id already used with different arguments")
            return self.status(oid)
        deadline = req.get("deadline", 300)
        if deadline is not None and (not isinstance(deadline, (float, int)) or not math.isfinite(deadline) or deadline <= 0):
            raise ValueError("deadline must be positive finite seconds, or None")
        if req.get("kind") not in {"js", "cdp"}:
            raise ValueError("operation kind must be js or cdp")
        if len(self.records) >= self.capacity:
            raise RuntimeError("operation capacity reached; forget finished operations before submitting more")
        method = req.get("method", "Runtime.evaluate")
        sid = None if method.startswith(("Browser.", "Target.")) else (req.get("session_id") or self.daemon.session)
        self.records[oid] = {
            "operation_id": oid, "kind": req["kind"], "state": "queued",
            "created_at": time.time(), "deadline": deadline, "session_id": sid,
            "progress": None,
        }
        self.specs[oid] = spec
        self.tasks[oid] = asyncio.create_task(self._run(oid, req, sid))
        return self.status(oid)

    async def _run(self, oid, req, sid):
        record = self.records[oid]
        record.update(state="running", started_at=time.time())
        try:
            async with asyncio.timeout(record["deadline"]):
                if req["kind"] == "js":
                    await self.daemon.cdp.send_raw("Runtime.enable", session_id=sid)
                    await self.daemon.cdp.send_raw("Runtime.addBinding", {"name": BINDING}, session_id=sid)
                    expression = self._expression(oid, req["body"])
                    result = await self.daemon.cdp.send_raw("Runtime.evaluate", {
                        "expression": expression, "returnByValue": True, "awaitPromise": True,
                    }, session_id=sid)
                    if result.get("exceptionDetails"):
                        details = result["exceptionDetails"]
                        raise RuntimeError(details.get("exception", {}).get("description") or details.get("text"))
                else:
                    result = await self.daemon.cdp.send_raw(req["method"], req.get("params", {}), session_id=sid)
                record.update(state="completed", result=result)
        except TimeoutError:
            record.update(state="timed_out", error="operation deadline exceeded")
            record["cancellation"] = await self._abort(oid, req, sid)
        except asyncio.CancelledError:
            record.update(state="cancelled")
            record["cancellation"] = await self._abort(oid, req, sid)
        except Exception as exc:
            record.update(state="failed", error=str(exc))
        finally:
            record["finished_at"] = time.time()

    @staticmethod
    def _expression(oid, body):
        # A function body gives the caller an AbortSignal and progress callback.
        # Abort races the result too, so an uncooperative promise cannot keep our
        # CDP request open forever. Side effects still require cooperative code.
        return """(async () => {
            const registry = globalThis[%s] ??= new Map();
            const id = %s, controller = new AbortController();
            registry.set(id, controller);
            const signal = controller.signal;
            const report_progress = value => {
                if (!signal.aborted) globalThis[%s](JSON.stringify({operation_id:id, value}));
            };
            const aborted = new Promise((_, reject) => signal.addEventListener('abort',
                () => reject(new Error('Operation cancelled')), {once:true}));
            try {
                return await Promise.race([(async (signal, report_progress) => {
                    %s
                })(signal, report_progress), aborted]);
            } finally { registry.delete(id); }
        })()""" % (json.dumps(REGISTRY), json.dumps(oid), json.dumps(BINDING), body)

    async def _abort(self, oid, req, sid):
        if req["kind"] != "js":
            return {"mode": "stop_waiting", "browser_effects_may_continue": True}
        try:
            result = await asyncio.wait_for(self.daemon.cdp.send_raw("Runtime.evaluate", {
                "expression": f"(() => {{const c=globalThis[{json.dumps(REGISTRY)}]?.get({json.dumps(oid)}); if(c)c.abort(); return !!c;}})()",
                "returnByValue": True,
            }, session_id=sid), timeout=2)
            return {"mode": "cooperative", "signal_delivered": result.get("result", {}).get("value") is True}
        except Exception as exc:
            return {"mode": "cooperative", "signal_delivered": False, "error": str(exc)}

    def on_event(self, method, params, session_id):
        if method != "Runtime.bindingCalled" or params.get("name") != BINDING:
            return
        try:
            payload = json.loads(params["payload"])
            record = self.records.get(payload["operation_id"])
            if record and record["state"] == "running" and record["session_id"] == session_id:
                record["progress"] = payload.get("value")
                record["progress_at"] = time.time()
        except (ValueError, KeyError, TypeError):
            pass

    async def cancel(self, oid):
        record = self.records[oid]
        if record["state"] not in TERMINAL:
            task = self.tasks[oid]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:  # cancelled before its first turn
                record.update(state="cancelled", finished_at=time.time(), cancellation={"mode": "not_started"})
        return self.status(oid)

    def forget(self, oid):
        if self.records[oid]["state"] not in TERMINAL or not self.tasks[oid].done():
            raise ValueError("cannot forget a running operation")
        for mapping in (self.records, self.specs, self.tasks):
            del mapping[oid]

    async def close(self):
        await asyncio.gather(*(self.cancel(oid) for oid in list(self.records)))
