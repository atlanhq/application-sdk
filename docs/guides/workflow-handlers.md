# Workflow Handlers — Cookbook

`@signal`, `@query`, and `@update` let external callers reach into a running `App` without killing or restarting it. The SDK lifts these decorators from your `App` subclass onto the generated workflow class, so the Temporal client API works transparently.

This page collects the patterns app authors actually need. Each example is paste-into-an-App-subclass and works against the SDK as of [BLDX-1283](https://linear.app/atlan-epd/issue/BLDX-1283).

> **TL;DR — when to reach for each decorator**
>
> | Need | Decorator |
> |---|---|
> | Mutate state and return a value, with validation, exactly-once delivery | `@update` |
> | Mutate state, fire-and-forget, no return value | `@signal` |
> | Read state without mutation, fast response | `@query` |

---

## 1. Pause / Resume / Graceful Cancel

A long extract that customers can pause mid-flight (maintenance window, source incident) without losing progress.

```python
from application_sdk.app import App, update, wait_condition
from application_sdk.contracts.base import Input, Output


class ExtractInput(Input):
    source_url: str = ""


class ExtractOutput(Output):
    rows_extracted: int = 0
    final_state: str = ""


class ExtractApp(App):
    def __init__(self) -> None:
        self.state: str = "running"
        self.rows_extracted: int = 0

    async def run(self, input: ExtractInput) -> ExtractOutput:
        for batch in self._batches(input.source_url):
            # Block here while paused — wait_condition is deterministic
            # and free when nothing is happening.
            await wait_condition(lambda: self.state != "paused")

            if self.state == "cancelled":
                break

            self.rows_extracted += await self._process(batch)

        return ExtractOutput(
            rows_extracted=self.rows_extracted, final_state=self.state
        )

    @update
    async def pause(self, reason: str) -> str:
        self.state = "paused"
        return f"paused: {reason}"

    @update
    async def resume(self) -> str:
        self.state = "running"
        return "resumed"

    @update
    async def graceful_cancel(self, reason: str) -> str:
        self.state = "cancelled"
        return f"cancelling: {reason}"
```

**Client side**

```python
handle = await client.start_workflow(
    ExtractApp.run, ExtractInput(source_url="..."),
    id="extract-1", task_queue="default",
)
await handle.execute_update("pause", "customer-maintenance-window")
# ... later ...
await handle.execute_update("resume")
```

---

## 2. Live Progress / ETA Query

Operators see throughput without waiting for the run to finish. Useful for any long extractor.

```python
from datetime import datetime

from application_sdk.app import now, query


class MetricsApp(App):
    def __init__(self) -> None:
        self.tables_done: int = 0
        self.tables_total: int = 0
        self.started_at: datetime | None = None

    async def run(self, input: ExtractInput) -> ExtractOutput:
        self.started_at = now()
        tables = await self._list_tables(input.source_url)
        self.tables_total = len(tables)

        for t in tables:
            await self._extract_table(t)
            self.tables_done += 1

        return ExtractOutput(rows_extracted=self.tables_done)

    @query
    def progress(self) -> dict:
        elapsed = (
            (now() - self.started_at).total_seconds()
            if self.started_at else 0
        )
        rate = self.tables_done / elapsed if elapsed > 0 else 0
        remaining = self.tables_total - self.tables_done
        return {
            "tables_done": self.tables_done,
            "tables_total": self.tables_total,
            "elapsed_seconds": elapsed,
            "rate_per_second": rate,
            "eta_seconds": remaining / rate if rate > 0 else None,
        }
```

**Client side (status endpoint, oncall CLI, dashboard)**

```python
progress = await handle.query("progress")
print(f"{progress['tables_done']}/{progress['tables_total']} — ETA {progress['eta_seconds']:.0f}s")
```

---

## 3. In-Flight Rate-Limit Adjustment

Source is hitting a quota. Back off without killing the run.

```python
from datetime import timedelta

from application_sdk.app import update, wait_condition


class RateLimitedApp(App):
    def __init__(self) -> None:
        self.rps_limit: int = 100  # default

    async def run(self, input: ExtractInput) -> ExtractOutput:
        rows = 0
        async for batch in self._batches(input.source_url):
            # Re-read the live limit each batch; updates flip it mid-flight.
            await wait_condition(
                lambda: True, timeout=timedelta(seconds=1 / self.rps_limit)
            )
            rows += await self._process(batch)
        return ExtractOutput(rows_extracted=rows)

    @update
    async def set_rate_limit(self, rps: int) -> int:
        self.rps_limit = rps
        return self.rps_limit

    @set_rate_limit.validator
    def _validate_rps(self, rps: int) -> None:
        if rps < 1 or rps > 10_000:
            raise ValueError("rps must be in [1, 10_000]")
```

**Client side (oncall during a source incident)**

```python
await handle.execute_update("set_rate_limit", 10)   # back off
# ... source recovers ...
await handle.execute_update("set_rate_limit", 200)  # ramp back up
```

The validator runs **before** the handler body. If it raises, the client gets a `WorkflowUpdateFailedError` and no state mutation happens.

---

## 4. External Signal from Another System

Webhook / cron / sibling app pokes a long-running extractor.

```python
from application_sdk.app import signal, wait_condition


class IncrementalApp(App):
    def __init__(self) -> None:
        self.watermark: str = ""
        self.advance_signal: bool = False

    async def run(self, input: ExtractInput) -> ExtractOutput:
        while True:
            await self._extract_since(self.watermark)
            # Block until an external system signals more data is ready.
            await wait_condition(lambda: self.advance_signal)
            self.advance_signal = False

    @signal
    async def new_data_available(self, new_watermark: str) -> None:
        self.watermark = new_watermark
        self.advance_signal = True
```

**Client side (a webhook handler in a different service)**

```python
await client.get_workflow_handle("incremental-extract-1").signal(
    "new_data_available", "2026-05-21T08:00:00Z"
)
```

Signals are fire-and-forget. Use them when the caller doesn't need a response or validation.

---

## 5. Operator Probe for Incident Response

"Where did it stop?" during oncall, without reading logs.

```python
from application_sdk.app import query


class CheckpointedApp(App):
    def __init__(self) -> None:
        self.last_checkpoint: dict = {"partition": None, "offset": 0}

    async def run(self, input: ExtractInput) -> ExtractOutput:
        for partition in self._partitions():
            for batch in self._read(partition, offset=self.last_checkpoint["offset"]):
                await self._process(batch)
                self.last_checkpoint = {
                    "partition": partition,
                    "offset": batch.end_offset,
                }
        return ExtractOutput(rows_extracted=...)

    @query
    def get_checkpoint(self) -> dict:
        return self.last_checkpoint
```

**Client side**

```bash
$ temporal workflow query --workflow-id=extract-1 --type=get_checkpoint
{"partition": "shard-7", "offset": 1842394}
```

---

## Gotchas

The same determinism rules that apply to `run()` apply to every handler — they all execute inside the workflow sandbox.

- **No I/O, no random, no `time.time()`.** Use `now()` / `uuid4()` (from `application_sdk.app`). Network calls and DB queries belong in `@task` activities the handler may invoke if needed.
- **Don't `await` long-running work in a handler.** Updates have a deadline (default 10s). Flip a flag in the handler; let `run()` do the actual work.
- **Prefer `wait_condition` over polling.** `while not flag: await asyncio.sleep(0.1)` works but is wasteful. `wait_condition` is the deterministic primitive and runs only when something changes.
- **Validators raise → update is rejected with `WorkflowUpdateFailedError` on the client.** Use `@<update_name>.validator` for argument shape checks (non-empty strings, integer ranges). The handler body should assume validated input.
- **Handler state is per-workflow-run.** The `App` instance lives for one workflow execution; instance fields reset across runs. For cross-run state, use `self.state.persistent` / `self.state.scratch` (SDK state accessors).
- **Dynamic handlers** (`@update(dynamic=True)` etc.) are supported — useful for catch-all routers. The handler signature must be `(self, name: str, args: Sequence[temporalio.common.RawValue])`.
