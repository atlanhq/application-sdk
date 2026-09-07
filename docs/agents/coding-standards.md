# Coding Standards

- Primary coding standards live in `docs/standards/coding.md` (formatting, naming, docstrings for all functions/classes/modules).
- Logging, exception, and performance guidance are in `docs/standards/logging.md`, `docs/standards/exceptions.md`, and `docs/standards/performance.md`.
- Metric / Prometheus label cardinality rules are in `docs/standards/metrics.md` — read before adding a `record_metric()` call or a new OTel instrument.
- Tooling enforcement is defined in `.pre-commit-config.yaml` (ruff, isort, pyright).

## Key Rules Enforced by Pre-commit

1. **No bare `except:`** - Always use `except Exception:` or a specific exception type
2. **No useless f-strings** - Don't use `f"string"` if there are no `{placeholders}`
3. **No Unicode in print statements** - Windows CI fails on emojis (`✓`, `❌`, `⚠️`). Use ASCII: `PASS`, `FAIL`, `WARNING`
4. **Import sorting** - Imports must be sorted (isort with black profile)
5. **Type hints** - Pyright enforces type checking
6. **Conventional commits** - Commit messages must follow conventional format (e.g., `fix:`, `feat:`, `chore:`)
7. **No customer names** - Never reference customer names, tenant names, run IDs, or any customer-identifiable information in code, comments, docstrings, commit messages, or PR descriptions. Use generic language: "a production incident", "a prior RCA".

## Serialization & Type Systems

Use the right type system for each zone:

| Zone | Type System | When to Use | Example |
|------|-------------|-------------|---------|
| **Temporal contracts** | `pydantic.BaseModel` | Anything serialized through Temporal wire (workflow/activity I/O) | `Input`, `Output`, `FileReference`, `CredentialRef` |
| **High-volume / low-level** | `msgspec.Struct` or plain dicts | Performance-critical paths: pyatlan_v9 asset types, logging internals | pyatlan_v9 types, log record construction |

**Rules:**
- All contracts (`Input`, `Output`, `HeartbeatDetails`, `Record`, `FileReference`, `CredentialRef`, credential types) **MUST** be frozen `pydantic.BaseModel` subclasses (typically via helpers in `contracts/base.py` or `contracts/types.py`; `CredentialRef` and credential types live in `application_sdk.credentials`). They are serialised through Temporal via `pydantic_data_converter`.
- Define contracts as plain class bodies — no `@dataclass` decorator. Pydantic handles `__init__`, validation, and serialization automatically.
- For frozen (immutable) contracts (e.g., `FileReference`, `CredentialRef`): use `class Foo(BaseModel, frozen=True)` or `model_config = ConfigDict(frozen=True)`.
- Use `Field(default_factory=...)` for mutable defaults (lists, dicts, nested models). Do **not** use `__post_init__` — that is a dataclass pattern.
- Avoid Pydantic on high-volume paths (e.g., every log line). Use plain dicts instead — Pydantic validation overhead accumulates significantly.
- Always use Pydantic v2 `model_config = ConfigDict(...)` style. Do not use the v1 inner `class Config:` pattern.

## Temporal Determinism (CRITICAL)

Code in `run()` and `@entrypoint` methods MUST be deterministic. Temporal replays workflows from history on worker restart — non-deterministic code corrupts the replay.

| DO | DON'T |
|----|-------|
| `self.now()` | `datetime.now()`, `datetime.utcnow()` |
| `self.uuid()` | `uuid.uuid4()`, `uuid.uuid1()` |
| `await self.my_task(input)` | `await http_client.get(url)` |
| Framework-provided random | `random.random()`, `random.choice()` |

All I/O, network calls, and non-deterministic operations go in `@task` methods.

## Preflight Gate (HYP-1883)

Every extraction workflow runs a `{app}:preflight` Temporal activity as its first
step. The activity resolves credentials, calls `handler.preflight_check(PreflightInput)`,
and aborts before extraction when the returned `PreflightOutput.status` is `NOT_READY`
(`READY` and `PARTIAL` proceed; `PARTIAL` is display-only).

### What the gate does

- The **workflow** builds a `PreflightGateInput` — a secret-free envelope containing
  credential references and a raw `model_dump()` snapshot of the extraction input.
- The **activity** resolves credentials, converts the snapshot into `PreflightInput`
  metadata, and calls the handler.
- Field reads from the extraction input therefore happen **inside the activity** (not
  in the deterministic workflow context), which is required for Temporal replay safety.

### `PreflightInput.metadata` / `connection_config` on the gate path

The gate derives `metadata` and `connection_config` from `extraction_snapshot` via
`_config_from_snapshot()` (runs in the activity frame). That helper:

- Excludes credential-routing fields (`extraction_method`, `credential_guid`, etc.).
- Produces both the original field name and its hyphenated variant so handlers that
  use either naming convention work on the gate path (e.g. `include_filter` and
  `include-filter`).
- Drops credential-routing fields and *genuinely* empty values (None, empty string,
  empty container), but preserves `False` and `0` — a handler reading a bool/int
  config field off the gate sees the real value, not a silent default.

If no snapshot is present (gate inputs built without a `model_dump`-capable extraction
input, e.g. manually constructed in tests), the activity falls back to `input.metadata`.

### Adopting the gate — two clauses

**(i) The input must be gate-eligible.** The gate only fires when the entrypoint's
input is `CredentialResolvable` — it declares `extraction_method`, `credential_guid`,
and `agent_json` as **top-level** fields. Declare them as real fields, not Pydantic
extras or nested config: extras are not a portable way to satisfy the protocol (the
`isinstance` check ignores them on Python 3.12+). A toolkit-generated `AppInputContract(ExtractionInput)`
satisfies this automatically — `ExtractionInput` declares the fields and its
`_normalize_ae_payload` lifts them out of the nested AE `metadata`. A hand-written input
that omits them (or keeps them nested) is **silently skipped** — so the SDK warns at
worker startup for any registered entrypoint whose input is not gate-eligible. The fix
is to put the input on `ExtractionInput` (via the toolkit) rather than hand-rolling the
lift.

**(ii) Express the verdict by returning `NOT_READY`, not by raising.** The handler
reports readiness by **returning** `PreflightOutput(status=NOT_READY, ...)`. Attach a typed
`error` to each failed check — `PreflightCheck(passed=False, error=AuthError(message=...,
suggested_action=..., cause=exc))` — and the gate carries the primary failure's
`FailureDetails` on the abort. Catch probe exceptions and turn them into checks; the
returned status is what both surfaces render.

Raising is not a no-op, though, and the error's type decides what happens — see below.

### Fail-open semantics

Enforcement is scoped by **who caused the failure**, not by whether a verdict was reached.

- **Source-attributable** (`gate_classification="source_unverifiable"`) — a `NOT_READY`
  verdict, a probe overrunning `App.preflight_gate_timeout_seconds`, a handler crash, or a
  provably absent credential. Subject to `preflight_gate_mode`: hard aborts the run, soft
  reports `would_block` and proceeds.
- **Gate plumbing** (`gate_classification="gate_broken"`) — a typed
  `DependencyUnavailableError` / `RateLimitedError` / `ResourceExhaustedError`, a
  secret-store outage, a collapsed not-found wrapping a transport error, an unavailable
  worker, or `schedule_to_close`. **Always** fails open, in both postures: a platform blip
  must never fail a healthy run.

So a handler signals "ask me later" by raising a typed plumbing error, and never by
returning `NOT_READY` for a transient — that would make a hard gate fail closed on a blip.

Every gated run emits a structured `Preflight gate outcome` event
(`outcome ∈ {proceeded, blocked, would_block, no_verdict, skipped}`), plus a
`Preflight gate posture` event per app at worker boot.

### Logging contract

One record per gate event: the outcome row itself carries the level (FND-901 — the
customer-facing log view filters at ERROR, so a block must be the ERROR record, not a WARN
beside one). Each row stamps `failure.audience` (who must act) except `proceeded`/`skipped`.

- **Verdict blocks** (`PreflightFailed` from a handler `NOT_READY`) — the outcome row at
  `error`, no stack trace, audience from the primary check's typed error (typically `USER`).
  The block is an expected typed outcome, not a crash — but it aborted the customer's run.
- **No-verdict outcomes** (budget overrun, handler crash — `source_unverifiable`) — the
  outcome row at `error` with `exc_info`, in both modes: the failure is real even when soft
  mode proceeds. There is a real exception behind these and it is the only diagnostic.
- **Gate plumbing failures** (exception during dispatch — `gate_broken`) — the workflow's
  `no_verdict` row at `error` with `exc_info=True`, audience `APP_OWNER`.
- **Advisory failures** (`proceeded` with any failed check — PARTIAL, or READY with a failed
  advisory row) — the outcome row at `warning`. P047 bans the handler from logging the
  warning itself, so the gate owns the one level that case is semantically for.
- **Clean `proceeded` / `skipped` / verdict `would_block`** — `info`.
- The interceptor's `workflow.ended` / `activity.ended … BLOCKED (preflight gate)` lifecycle
  records stay `warning`, terse, no stack.
- **Interactive surfaces** (the HTTP `/workflows/v1/check` endpoint and the SDR
  `sdr:preflight_check` activity) emit the sibling `Preflight check outcome` row via
  `emit_preflight_check_outcome`, with `preflight_surface` naming the surface. The level
  follows whether the log is the delivery channel: HTTP rows stay `info` (the verdict IS
  the response body rendered in the setup form); SDR failures surface through a workflow
  run log read at the default ERROR filter, so SDR rows mirror the gate — `not_ready` at
  `error`, advisory failure at `warning`, clean at `info`. Handler crashes stay `error`
  at each surface's boundary handler and additionally emit one
  `outcome="crashed"` row at `error` on every interactive surface
  (`emit_preflight_crash_outcome`), so the funnel sees the worst case; a typed
  client-input error that raises (wrong password, 4xx-class category) is counted
  as `outcome="client_fault"` instead — at `info` on HTTP where the response is
  the channel, at `error` on SDR where the row is — so it never pollutes the
  crash series and never drops from the denominator. The surface is
  the `PreflightSurface` enum, not a
  string, and its level policy is the `_LOG_ROW_IS_ONLY_CHANNEL` table — a new surface
  fails `test_every_surface_has_a_level_policy` until someone routes it.

## Contract Evolution

- NEVER remove or rename fields on Input/Output classes
- NEVER change field types
- Add new fields with defaults only (`field: str = ""`)
- Use `Field(default_factory=list)` for mutable defaults, never `field: list = []`

Breaking a contract silently corrupts in-flight Temporal workflows.

## Blocking Operations

In `@task` methods, wrap blocking calls with `self.run_in_thread()`:

```python
# Wrong — blocks event loop, kills heartbeats
result = requests.get(url)

# Right
result = await self.run_in_thread(requests.get, url)
```

## Writing Files

Never write an artifact directly to its final name. Use `application_sdk.common.atomic`:

```python
from application_sdk.common.atomic import atomic_write

# Wrong — a write that dies part-way leaves a truncated file at the real name.
# Nothing downstream can tell it from a correct one, so it gets carried forward
# and uploaded, and the failure surfaces in a consuming app's parser instead.
Path(out).write_text(payload)

# Right — the final path either does not exist or holds a complete file.
with atomic_write(out, operation="entity export") as handle:
    handle.write(payload.encode())
```

`operation` is a short phrase naming the step; it goes into the failure message and
its evidence. Two companions:

- `atomic_path(path, operation=...)` for writers that take a filename rather than a
  handle (`pq.write_table`, `shutil.copy2`), and `atomic_copy(src, dst)` for copies.
- `disk_full_guard(path, operation=...)` for a write that genuinely cannot be atomic
  — an append across calls. It types the failure without the atomicity.

A full disk raises `DiskFullError` naming the path and the shortfall, which is the
signal an operator reads to raise a deployment's ephemeral storage. Pass
`required_bytes=` when the size is known up front and the check happens before the
first byte moves. See `docs/concepts/storage.md` → *Atomic artifact writes*.

Every SDK writer already goes through this, so a `Writer`, `FileReference`, or the
incremental helpers need nothing from you. This rule is for code that opens a file
itself.

## Long-Running Tasks: Progress and Stalls

A task that goes quiet for longer than `max_no_progress_seconds` (900s) is reported as a
stall — and failed, in an app that enforces. The SDK covers its own write, transfer and
page loops, and auto-holds every `run_in_thread` offload; what it cannot see is a custom
async loop or an opaque single `await` against the connector's own source client. Those
need one line: `self.heartbeat(...)` in the loop, or
`async with self.holding_progress(label, timeout=...)` around the opaque call.

`timeout` is *how long you would let this one call run before you would rather it
failed* — not a prediction of its duration. Err generous; too tight false-kills a
healthy run, and stall kills retry.

Read [Progress and Stalls](../concepts/progress-and-stalls.md) before writing a
long-running task, and [ADR-0018](../adr/0018-progress-aware-heartbeat.md) if you need
the design rationale.

## Large Payloads and FileReference

Use `FileReference` for any data that cannot fit in Temporal's 2 MB payload limit.
See `docs/concepts/file-reference.md` for the full guide: decision matrix, lifecycle,
the `Lazy()` marker for selective materialization, dedup behaviour, and observability events.

### App-to-app hand-off (required for SDR deployments)

`FileReference` auto-durability writes to the **customer-owned `objectstore`**
(`infra.storage`) — it is designed for task-to-task data passing within a single run.

**Silent-failure rule:** if a connector returns a `FileReference` from a `@task` but
never calls `App.upload()` from `run()`, the DAG completes successfully but the publish
app finds nothing in Atlan's bucket. This produces no error — the failure is invisible
in the Temporal UI.

To hand off artifacts to Atlan system apps (publish, lineage, quality), call
`App.upload()` explicitly from `run()` — it routes to the Atlan-owned
`atlan-objectstore` (`infra.upstream_storage`) in SDR deployments:

```python
from application_sdk.contracts import UploadInput

async def run(self, input: MyInput) -> MyOutput:
    fetch_out = await self.fetch_data(input)
    # Required: push artifact to Atlan's upstream store
    await self.upload(UploadInput(local_path=fetch_out.output_path))  # RETAINED is the default tier
    return fetch_out
```

**Anti-pattern: calling `App.upload()` for task-to-task data.** `App.upload()` routes
to `atlan-objectstore` in SDR — using it for intermediate pipeline data (instead of
`FileReference`) has three harms: pollutes Atlan's bucket with internal artifacts;
bypasses SHA-256 dedup (every call is a full re-upload, even for identical files);
and does not wire into cross-worker auto-materialization. Declare `FileReference` on
task `Input`/`Output` contracts instead — the interceptor handles persistence and
re-download automatically.

See [file-reference.md § App-to-app hand-off](../concepts/file-reference.md) and
[ADR-0014](../adr/0014-two-store-storage-architecture.md) for the full rationale.

## Replacing `ParquetFileWriter` / `JsonFileWriter` (v4.0 removal path)

`ParquetFileWriter`, `JsonFileWriter`, `ParquetFileReader`, and `JsonFileReader`
emit `DeprecationWarning` on construction and **will be removed in v4.0**.
Migrate now — the replacement is fully supported and fully optimised
(SHA-256 dedup + sidecars + parallel transfers via `_gather_with_semaphore`).

### Recommended: `RollingFileWriter`

`application_sdk/storage/rolling.py` provides a small format-agnostic helper
that encapsulates the common pattern:

- Owns a scoped output sub-directory (so the resulting `FileReference`
  covers only your chunks — never sibling content in `base_path`).
- Buffers appended batches and rolls over to a new chunk file when **any**
  of the configured policies fire:
  - `chunk_interval_seconds` — wall clock (default **30s**)
  - `max_buffer_bytes` — buffer ceiling in bytes (default **50 MB**)
  - `max_buffer_records` — buffer ceiling in records (default `None`,
    opt-in)
- Optional `on_chunk_complete(chunk_index, chunk_path)` callback wires
  cleanly to `activity.heartbeat(...)` for within-heartbeat checkpointing.
- `writer.file_reference` returns an ephemeral `FileReference` for your
  typed Output; the activity interceptor uploads on task return.

**Why the default policy bundle:** the SDK runs in two extreme streaming
regimes — slow JDBC streams (~200 rows/min) and fast msgspec transforms
(~10 000 records/ms). A pure record-count threshold like `chunk_size=10_000`
either never trips (slow stream → one huge final file, blocks heartbeat)
or trips every millisecond (fast stream → thousands of tiny files). A
pure wall-clock interval handles the slow case but leaves no ceiling on
memory when the stream is fast. The default bundle (time + bytes, plus
opt-in records) gives:

- Predictable checkpoint cadence regardless of upstream throughput.
- Bounded peak memory: a runaway fast upstream hits the 50 MB ceiling
  long before it can OOM a typical pod.
- An optional records-based escape hatch for callers who think in rows.

For advanced cases (e.g. fixed-size exports where only size matters), pass
a custom `rollover_policy=` — see :class:`TimePolicy`, :class:`SizePolicy`,
:class:`CountPolicy`, and :class:`AnyOfPolicy` exposed from
`application_sdk.storage.rolling`.

#### Parquet (pandas)

```python
import pandas as pd

from application_sdk.contracts.types import FileReference
from application_sdk.storage.rolling import RollingFileWriter


def _flush_parquet(batches: list[pd.DataFrame], path: str) -> None:
    pd.concat(batches, ignore_index=True).to_parquet(path)


async def extract_users(self, inp: ExtractInput) -> ExtractOutput:
    async with RollingFileWriter[pd.DataFrame](
        base_path=inp.output_path,
        extension=".parquet",
        flush_fn=_flush_parquet,
        chunk_interval_seconds=60.0,
        on_chunk_complete=self._heartbeat_chunk,   # optional — see below
    ) as writer:
        async for df in self._stream_users():
            await writer.append(df)
    return ExtractOutput(data=writer.file_reference)


async def _heartbeat_chunk(self, chunk_index: int, chunk_path: str) -> None:
    from temporalio import activity  # noqa: PLC0415
    activity.heartbeat(f"wrote {chunk_path}")
```

#### JSON (line-delimited, `orjson`)

```python
import orjson

from application_sdk.contracts.types import FileReference
from application_sdk.storage.rolling import RollingFileWriter


def _flush_jsonl(batches: list[list[dict]], path: str) -> None:
    with open(path, "wb") as f:
        for batch in batches:
            for record in batch:
                f.write(orjson.dumps(record))
                f.write(b"\n")


async def extract_events(self, inp: ExtractInput) -> ExtractOutput:
    async with RollingFileWriter[list[dict]](
        base_path=inp.output_path,
        extension=".json",
        flush_fn=_flush_jsonl,
        chunk_interval_seconds=60.0,
    ) as writer:
        async for records in self._stream_events():
            await writer.append(records)
    return ExtractOutput(data=writer.file_reference)
```

To support a new format, write a 3-line `flush_fn` and plug it in. CSV via
`pandas.to_csv`, Arrow IPC via `pyarrow.ipc`, msgpack via `msgpack.pack` —
all the same shape.

### Alternative: bare copy-paste (no helper class)

If you want zero new dependencies, the loop the helper wraps is small enough
to inline directly.

**Parquet:**

```python
import os
import uuid
import pandas as pd
from application_sdk.contracts.types import FileReference


async def extract_users(self, inp: ExtractInput) -> ExtractOutput:
    output_dir = os.path.join(inp.output_path, f"users_{uuid.uuid4().hex[:8]}")
    os.makedirs(output_dir, exist_ok=True)
    chunk_index = 0
    async for df in self._stream_users():
        df.to_parquet(os.path.join(output_dir, f"chunk-{chunk_index}.parquet"))
        chunk_index += 1
    return ExtractOutput(data=FileReference.from_local(output_dir))
```

**JSON (line-delimited):**

```python
import os
import uuid
import orjson
from application_sdk.contracts.types import FileReference


async def extract_events(self, inp: ExtractInput) -> ExtractOutput:
    output_dir = os.path.join(inp.output_path, f"events_{uuid.uuid4().hex[:8]}")
    os.makedirs(output_dir, exist_ok=True)
    chunk_index = 0
    async for records in self._stream_events():
        with open(os.path.join(output_dir, f"chunk-{chunk_index}.json"), "wb") as f:
            for record in records:
                f.write(orjson.dumps(record))
                f.write(b"\n")
        chunk_index += 1
    return ExtractOutput(data=FileReference.from_local(output_dir))
```

The bare pattern has no rollover or heartbeat awareness — each iteration of
your stream loop produces one chunk file. Prefer `RollingFileWriter` when
you want predictable checkpoint cadence on long-running activities.

### Reading a `FileReference` (replaces `ParquetFileReader` / `JsonFileReader`)

Declare the upstream artifact as a `FileReference` field on the consuming
task's typed Input. The activity interceptor auto-materialises it to a local
path before the task runs (with sidecar verification + parallel transfers).
Read it directly with the library of your choice:

```python
class TransformInput(Input):
    data: FileReference                              # auto-materialised

async def transform_users(self, inp: TransformInput) -> TransformOutput:
    df = pd.read_parquet(inp.data.local_path)
    ...
```

For JSON inputs, swap `pd.read_parquet` for an `orjson.loads` loop over the
file lines. No `ParquetFileReader` / `JsonFileReader` construction required —
they exist only to bridge the legacy inline-upload contract and will be
deleted in v4.0.

### Transitional opt-in on the legacy `ParquetFileWriter`

If you cannot migrate to the direct pattern immediately but want the SHA-256
+ parallel-transfer benefits today, `ParquetFileWriter` accepts a
`defer_uploads=True` flag. Default (`False`) preserves the pre-3.8 inline-
upload behaviour so existing call sites are unaffected; `True` switches to
the `FileReference` boundary. `close()` always returns a `WriterResult` that
subclasses `TaskStatistics` (so `result.total_record_count` etc. continue to
work via inheritance) and gains a `result.files: FileReference | None`
field — `None` in default mode (no double-upload risk), ephemeral in opt-in
mode.

```python
async with ParquetFileWriter(
    path=base, typename="users", defer_uploads=True,
) as writer:
    await writer.write(df)
result = writer.last_result
return MyOutput(statistics=result, data=result.files)
```

The opt-in flag is a bridge for in-flight migrations only. New code should
go straight to the direct copy-paste pattern above.

**A writer's own files appear only after `close()`.** Both legacy writers stage
their chunks in a private directory (a hidden `.sdk-writer-staging/` sibling of
the output path) and move them into the output directory in one step at
`close()`. Chunk filenames, object-store keys, and the `statistics/` layout are
exactly what they always were — only the moment the files land changes. Do not
read a writer's output directory before its `close()` returns. Publishing only
adds: anything already in a reused output directory stays there, and a deferred
writer's `FileReference` covers it too.

The reason is cancellation. A cancelled activity leaves an orphaned worker
thread that cannot be killed and is still writing to a path it resolved before
the cancel; the chunk name it holds (`chunk-<n>-part<m>`) is identical to the
one the retry will resolve. Staging means only a writer that reaches `close()`
ever touches the output directory, so a retry can neither have its file
overwritten by that orphan nor sweep the orphan's other files into its own
`FileReference` (FND-315, FND-317).

## Before Every Commit

```bash
uv run pre-commit run --files <changed-files>
```

Or install hooks to run automatically:
```bash
uv run pre-commit install
```
