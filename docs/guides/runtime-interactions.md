# Runtime Interactions — Cookbook

`@signal`, `@query`, and `@update` let external callers reach into a running `App` without killing or restarting it. The SDK lifts these decorators from your `App` subclass onto the generated run class, so the client API works transparently.

This page collects the patterns app authors actually need. Each example is paste-into-an-App-subclass and works against the SDK as of [BLDX-1283](https://linear.app/atlan-epd/issue/BLDX-1283).

> **TL;DR — when to reach for each decorator**
>
> | Need | Decorator |
> |---|---|
> | Mutate state and return a value, with validation, exactly-once delivery | `@update` |
> | Pure trigger — fire-and-forget, no payload, no return value | `@signal` |
> | Read state without mutation, fast response | `@query` |

## Contracts enforced by the SDK

The SDK validates interaction signatures at class-definition time (when `generate_workflow_class` is called). Violations raise `InvalidInputError` immediately — not at runtime.

| Decorator | Parameter rule | Return-type rule |
|---|---|---|
| `@signal` | No params besides `self` | None (`-> None`) |
| `@query` | No params besides `self` | Must be a subclass of `Output` |
| `@update` | Exactly one param besides `self`, must be a subclass of `Input` | Must be a subclass of `Output` |

Dynamic interactions (`@update(dynamic=True)` etc.) are exempt.

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


class PauseInput(Input):
    reason: str = ""


class StateMessageOutput(Output):
    message: str = ""


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
    async def pause(self, input: PauseInput) -> StateMessageOutput:
        self.state = "paused"
        return StateMessageOutput(message=f"paused: {input.reason}")

    @update
    async def resume(self, input: PauseInput) -> StateMessageOutput:
        self.state = "running"
        return StateMessageOutput(message="resumed")

    @update
    async def graceful_cancel(self, input: PauseInput) -> StateMessageOutput:
        self.state = "cancelled"
        return StateMessageOutput(message=f"cancelling: {input.reason}")
```

**Client side**

```python
handle = await client.start_workflow(
    ExtractApp.run, ExtractInput(source_url="..."),
    id="extract-1", task_queue="default",
)
await handle.execute_update("pause", PauseInput(reason="customer-maintenance-window"))
# ... later ...
await handle.execute_update("resume", PauseInput())
```

---

## 2. Live Progress / ETA Query

Operators see throughput without waiting for the run to finish. Useful for any long extractor.

```python
from datetime import datetime

from application_sdk.app import now, query


class ProgressOutput(Output):
    tables_done: int = 0
    tables_total: int = 0
    elapsed_seconds: float = 0.0
    eta_seconds: float | None = None


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
    def progress(self) -> ProgressOutput:
        elapsed = (
            (now() - self.started_at).total_seconds()
            if self.started_at else 0.0
        )
        rate = self.tables_done / elapsed if elapsed > 0 else 0
        remaining = self.tables_total - self.tables_done
        return ProgressOutput(
            tables_done=self.tables_done,
            tables_total=self.tables_total,
            elapsed_seconds=elapsed,
            eta_seconds=remaining / rate if rate > 0 else None,
        )
```

**Client side (status endpoint, oncall CLI, dashboard)**

```python
p = await handle.query("progress")
print(f"{p.tables_done}/{p.tables_total} — ETA {p.eta_seconds:.0f}s")
```

---

## 3. In-Flight Rate-Limit Adjustment

Source is hitting a quota. Back off without killing the run.

```python
from datetime import timedelta

from application_sdk.app import update, wait_condition


class RateLimitInput(Input):
    rps: int = 100


class RateLimitOutput(Output):
    rps: int = 0


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
    async def set_rate_limit(self, input: RateLimitInput) -> RateLimitOutput:
        self.rps_limit = input.rps
        return RateLimitOutput(rps=self.rps_limit)

    @set_rate_limit.validator
    def _validate_rps(self, input: RateLimitInput) -> None:
        if input.rps < 1 or input.rps > 10_000:
            raise ValueError("rps must be in [1, 10_000]")
```

**Client side (oncall during a source incident)**

```python
await handle.execute_update("set_rate_limit", RateLimitInput(rps=10))   # back off
# ... source recovers ...
await handle.execute_update("set_rate_limit", RateLimitInput(rps=200))  # ramp back up
```

The validator runs **before** the interaction body. If it raises, the client gets a `WorkflowUpdateFailedError` and no state mutation happens.

---

## 4. External Update Advances a Watermark

A webhook, cron, or sibling app notifies a long-running extractor that new data is ready.

```python
from application_sdk.app import update, wait_condition


class WatermarkInput(Input):
    new_watermark: str = ""


class WatermarkOutput(Output):
    watermark: str = ""


class IncrementalApp(App):
    def __init__(self) -> None:
        self.watermark: str = ""
        self.advance: bool = False

    async def run(self, input: ExtractInput) -> ExtractOutput:
        while True:
            await self._extract_since(self.watermark)
            # Block until an external system sends a new watermark.
            await wait_condition(lambda: self.advance)
            self.advance = False

    @update
    async def new_data_available(self, input: WatermarkInput) -> WatermarkOutput:
        self.watermark = input.new_watermark
        self.advance = True
        return WatermarkOutput(watermark=self.watermark)
```

**Client side (a webhook handler in a different service)**

```python
await client.get_workflow_handle("incremental-extract-1").execute_update(
    "new_data_available", WatermarkInput(new_watermark="2026-05-21T08:00:00Z")
)
```

Use `@update` (not `@signal`) when the caller needs confirmation that the watermark was received, or when the new watermark value must be validated before the run acts on it.

---

## 5. Operator Probe for Incident Response

"Where did it stop?" during oncall, without reading logs.

```python
from application_sdk.app import query


class CheckpointOutput(Output):
    partition: str | None = None
    offset: int = 0


class CheckpointedApp(App):
    def __init__(self) -> None:
        self.checkpoint: CheckpointOutput = CheckpointOutput()

    async def run(self, input: ExtractInput) -> ExtractOutput:
        for partition in self._partitions():
            for batch in self._read(partition, offset=self.checkpoint.offset):
                await self._process(batch)
                self.checkpoint = CheckpointOutput(
                    partition=partition,
                    offset=batch.end_offset,
                )
        return ExtractOutput(rows_extracted=...)

    @query
    def get_checkpoint(self) -> CheckpointOutput:
        return self.checkpoint
```

**Client side**

```bash
$ temporal workflow query --workflow-id=extract-1 --type=get_checkpoint
{"partition": "shard-7", "offset": 1842394}
```

---

## Gotchas

The same determinism rules that apply to `run()` apply to every runtime interaction — they all execute inside the run sandbox.

- **No I/O, no random, no `time.time()`.** Use `now()` / `uuid4()` (from `application_sdk.app`). Network calls and DB queries belong in `@task` activities an interaction may invoke if needed.
- **Don't `await` long-running work in an interaction.** Updates have a deadline (default 10s). Flip a flag in the interaction; let `run()` do the actual work.
- **Prefer `wait_condition` over polling.** `while not flag: await asyncio.sleep(0.1)` works but is wasteful. `wait_condition` is the deterministic primitive and runs only when something changes.
- **Validators raise → update is rejected with `WorkflowUpdateFailedError` on the client.** Use `@<update_name>.validator` for argument shape checks (non-empty strings, integer ranges). The interaction body should assume validated input.
- **Interaction state is per-run.** The `App` instance lives for one execution; instance fields reset across runs. For cross-run state, use `self.state.persistent` / `self.state.scratch` (SDK state accessors).
- **Dynamic interactions** (`@update(dynamic=True)` etc.) are supported — useful for catch-all routers. The interaction signature must be `(self, name: str, args: Sequence[temporalio.common.RawValue])`. Dynamic interactions are exempt from the Input/Output contract check.
- **Signals carry no payload.** They are pure triggers. To pass data into a running workflow, use `@update`.
