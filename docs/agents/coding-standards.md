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
and aborts before extraction when a check the app marked `blocking` fails — the SDK
derives `PreflightOutput.should_block` from the per-check flags — or, fail-closed,
when the gate cannot produce a verdict at all.

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

**(ii) Declare per-check severity; the SDK derives the verdict.** Each check
carries a `blocking` flag. The SDK folds them into the verdict: the run blocks iff
some check the app marked `blocking` did not pass (`should_block`). The app never
returns a single boolean — it declares which checks gate and lets the SDK
arbitrate. Advisory checks (`blocking=False`, the default) surface in the UI but
never stop the run. Order gating probes first: on a block, the first blocking-failed
check carrying `error` supplies the typed `FailureDetails`.

Build failing checks with `PreflightCheck.from_error`, the one blessed bridge
from the typed-error system, and catch probe exceptions rather than letting them
escape:

```python
checks: list[PreflightCheck] = []
try:
    await probe_auth()
    checks.append(PreflightCheck(name="auth", passed=True, blocking=True))
except AppError as e:
    checks.append(PreflightCheck.from_error("auth", e))  # from_error defaults blocking=True
except Exception as e:
    logger.error("auth probe failed", exc_info=True)
    checks.append(PreflightCheck.from_error("auth", e))
try:
    await probe_connectivity()
    checks.append(PreflightCheck(name="connectivity", passed=True))
except AppError as e:
    # advisory: surface it, don't gate
    checks.append(PreflightCheck.from_error("connectivity", e, blocking=False))
return PreflightOutput(checks=checks)  # should_block / status derived from the flags
```

Here auth gates the run but connectivity is advisory — a failed connectivity check
surfaces in the UI without blocking, and there is no bookkeeping boolean to keep in
sync. The `blocking` flag is **data the app computes at return time**, so any
formula is expressible: combination gating ("checks 1 and 2 gate only together") is
just app logic that sets the flags before returning. `from_error` defaults
`blocking=True` — the blessed failure path gates by default, so opting out
(`blocking=False`) is the explicit act. A typed `AppError` keeps its full
classification: `from_error` stores its `to_failure_details()` on
`PreflightCheck.error`, and on a block the gate forwards that verbatim — the
app-specific `code` reaches the Automation Engine with the same fidelity a runtime
activity raise has. An untyped exception yields an unclassified check
(`category=None` → the gate defaults to `PRECONDITION`) with a sanitized message —
the exception text never enters the check; log it with `exc_info=True` at the catch
site instead. `PreflightOutput.status` and `should_block` derive from the flags;
never pass `passed=` or `status=`.

### Fail-closed semantics

Any failure to *produce* a verdict blocks the run — "couldn't verify" is not a
pass. A `preflight_check` that **raises**, a gate **timeout** (after the retry
budget), or a **secret-store outage** all abort extraction. This is separate from
the derived verdict: once the handler returns, the run blocks only if a `blocking`
check failed.

Attribution on a block: every blocking-failed check's message folds into the abort
reason (an explicit `PreflightOutput.message` overrides the fold; advisory failures
stay out of the reason), and the first blocking-failed check carrying `error`
supplies the typed `FailureDetails` — so order gating probes first. When the gate
itself could not evaluate and no typed detail is recoverable from the failure's
cause chain, the block is attributed to `DEPENDENCY_UNAVAILABLE` (a dead dependency,
never misattributed as bad auth).

### Logging contract

Both outcomes hard-block; they differ only in log level:

- **`PreflightFailed`** (a blocking check failed) — `warning`, no
  stack trace. An expected, typed outcome, not a crash.
- **`PreflightUnavailable`** (the gate could not evaluate) — `error` with
  `exc_info=True`. A real failure of the gate's plumbing, logged like any crash.

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

## Before Every Commit

```bash
uv run pre-commit run --files <changed-files>
```

Or install hooks to run automatically:
```bash
uv run pre-commit install
```
