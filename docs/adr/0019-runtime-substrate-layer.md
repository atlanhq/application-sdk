# ADR-0019: A Dependency-Neutral Runtime Substrate (`application_sdk._runtime`)

## Status
**Accepted** — resolves the layering debt left by [ADR-0010](0010-async-first-blocking-code.md)
and [ADR-0018](0018-progress-aware-heartbeat.md) (FND-316).

## Context

Two primitives are needed by every layer of the SDK:

* **the offload seam** — `run_in_thread`, `submit_in_thread`,
  `run_fault_isolated`, `run_best_effort`. ADR-0010 makes `run_in_thread` a
  *mandatory* seam for blocking work, and ADR-0017 adds the isolated variants.
* **the progress seam** — `current_progress_tracker`, `holding_progress`,
  `ProgressTracker`. ADR-0018 has storage writers, transfer loops and app code
  all feeding the same per-attempt tracker.

Both lived under `execution/`, which put them *above* their callers. `storage/`
reached upward for them at eleven call sites; `execution/_temporal/activities.py`
reached back down into `storage/` in the other direction. The cycle was real, and
it was closed by lazy imports at each site.

The mechanism is worth stating precisely, because it is not "storage imports
execution" on its own:

```
storage/ops.py  ──module scope──▶  application_sdk.execution.progress
                                   │
                                   ├─ runs application_sdk/execution/__init__.py
                                   │     └─▶ execution._temporal.* ─▶ app.base
                                   │            └─▶ contracts ─▶ credentials
                                   │                   └─▶ common.utils
                                   └───────────────────────▶ storage.ops  ← half-initialised
```

Importing *any* submodule of a package runs that package's `__init__`, so
`from application_sdk.execution.progress import …` at module scope in
`storage/ops.py` is an `ImportError` — the partially-initialised `storage.ops`
cannot yet supply `download_file_chunked` to `common.utils`.

Each lazy import was individually correct and individually justified. The cost
was structural:

* **The constraint had to be rediscovered.** A new call site learned about it by
  hitting the `ImportError`, then copied the nearest `# noqa: PLC0415 —
  circular:` comment. Four near-identical copies of the explanation had
  accumulated, and no reader could tell which lazy imports were load-bearing and
  which were cargo-culted from a neighbour.
* **It constrained where hot-path code could live.** `storage/chunked.py`'s
  `_fetch_chunk` paid a `sys.modules` lookup per range GET, because the import
  could not be hoisted out of the loop. Trivial against a multi-MiB GET, but a
  real limit on where the seam could be read.
* **Import order decided whether a module was correct.** `storage/transfer.py`
  and `storage/formats/*` imported the same symbols at module scope without
  trouble, purely because they were not in `storage/__init__`'s eager chain. Two
  storage modules doing the same thing, one of which happened to work.

## Decision

**Move the shared primitives into `application_sdk/_runtime/`, a
dependency-neutral bottom layer that every other layer may import at module
scope.**

```
application_sdk/_runtime/
├── __init__.py     # the rule, and no imports
├── offload.py      # run_in_thread, submit_in_thread, run_fault_isolated, run_best_effort
└── progress.py     # ProgressTracker, current_progress_tracker, holding_progress, …
```

**The rule for a `_runtime` module:** it may import the standard library,
third-party packages, `application_sdk.observability.logger_adaptor`, and its
siblings in this package (`offload.py` reads `progress.py` — that is what makes the
offload auto-holds work). Nothing else from `application_sdk`: not `contracts`,
`credentials`, `storage`, `execution` or `app` — each of those reaches
`storage.ops` transitively, which re-creates the cycle. The boundary is *upward*,
not lateral: a sibling import stays inside the bottom layer, so it cannot reach a
layer above and cannot re-create the cycle.

Two consequences follow from that rule.

**`ProgressWatchdogMode` stays in `execution/`, and nothing about `contracts`
changes.** The enum subclasses `SerializableEnum` (it rides on the task's Temporal
payload and is used as a metric attribute value), and `contracts.base` is not
importable from `storage/` — `contracts.types` → `credentials.ref` → … →
`storage.ops`. That reads like a reason to move `SerializableEnum` down to the
substrate, and a first pass did exactly that.

It was the wrong call, for a simple reason: **nothing in `_runtime` reads
`ProgressWatchdogMode`.** `progress.py` merely *defined* it; every consumer — the
stall check, `auto_heartbeat_loop`, the telemetry that labels a gap with it — is in
`execution/`. So the enum belongs in `execution/progress.py` next to the watchdog
whose vocabulary it is, and the substrate needs no `contracts` dependency at all.

That matters beyond tidiness. `SerializableEnum` is a **base class app code
subclasses** to make its own enums Temporal-serialisable, so its definition site is
part of the contract rather than an implementation detail: `__module__` feeds
pickling and schema naming for every downstream subclass. Relocating a public base
class to a private package to satisfy an import constraint that turned out not to
exist would have been a real cost for no benefit. It stays exactly where it was, and
a test asserts both its identity and its `__module__`.

**`execution/heartbeat.py` split along the seam it already had.** The heartbeat
concern (`HeartbeatController` and its two implementations, `auto_heartbeat_loop`,
the stall watchdog) stays in `execution/` — it is genuinely execution-layer, and
`TemporalHeartbeatController` imports `temporalio`. The offload primitives moved
out. `execution/progress.py` re-exports the whole seam from `_runtime/progress.py`
and owns `ProgressWatchdogMode`.

**The app-facing paths did not change.** `application_sdk.execution.heartbeat`
and `application_sdk.execution.progress` remain the documented imports for app
code — the docs and every shipped connector point there — and re-export the same
objects, so a patch applied at either path affects both. `_runtime` is
underscore-private on purpose: it is substrate, not a new public surface. SDK
code imports `_runtime` directly; apps should not need to know it exists.

## Consequences

**Every lazy import on the storage ↔ execution edge is gone**, along with the
constraint that produced them:

| Site | Was |
| --- | --- |
| `storage/ops.py` ×2 | lazy `execution.progress` |
| `storage/chunked.py` | lazy `execution.progress`, inside the per-chunk path |
| `storage/cloud.py`, `storage/batch.py`, `storage/rolling.py`, `storage/integrity.py` | lazy `execution.heartbeat` |
| `app/context.py` ×2, `app/base.py` ×2 | lazy `execution.heartbeat` / `execution.progress` |
| `execution/_temporal/activities.py` ×2 | lazy sibling imports |

Two lazy imports on this edge deliberately remain, and neither is about
`storage/`:

* `observability/observability.py` defers `_runtime.offload` because
  `_runtime.offload` imports `observability.logger_adaptor`, which imports
  `observability.observability` — an *observability-internal* cycle, now named as
  such in the comment rather than misattributed to storage.
* `execution/_temporal/activities.py` still defers `app.context`, because
  `app ↔ execution` is a separate cycle that this ADR does not claim to fix.

**What a consumer can and cannot still rely on.** Every module path and every symbol
that resolved before still resolves — verified by snapshotting `dir()` for all 294
`application_sdk` modules before and after and diffing. Two things needed explicit
work to keep that true, because a module split silently narrows the namespace of the
file it splits:

* `execution/heartbeat.py` re-exports names it no longer uses itself but which used to
  resolve through it — `current_progress_tracker`, `declared_hold_active`,
  `AtlanLoggerAdapter`, `BrokenProcessPool`. Nothing in the SDK imports them from
  there, so no other test would have noticed them disappearing.
* `execution/__init__.py` imports `execution.progress` explicitly. That attribute used
  to be bound as a side effect of `heartbeat` importing the submodule, so
  `import application_sdk.execution as ex; ex.progress.…` worked; `heartbeat` now
  reaches `_runtime.progress` instead.

Both are pinned per-name in `tests/unit/runtime/test_layering.py`. What is *not*
preserved is incidental import leakage — stdlib module aliases (`threading`,
`functools`), typing helpers (`Iterator`, `TypeVar`), and module privates
(`_BLOCKING_EXECUTOR`, `_auto_hold`, `_progress_tracker`) that were reachable only
because the pre-split file happened to import them. An org-wide code search found no
consumer of any of them.

**Monkeypatch seams move when a lazy import is hoisted, and that is the one real
behavioural change here.** `application_sdk.execution.heartbeat.run_in_thread` only
intercepts an SDK-internal offload if the SDK resolves that name through the
`heartbeat` module at call time. Hoisting `storage/`, `app/base.py` and
`app/context.py` to module-scope `_runtime.offload` imports — the point of this ADR —
means a `patch()` on the old path no longer reaches them. The failure mode is silent:
the patch applies and the call is simply not intercepted. Consumers intercepting SDK
offloads must patch `application_sdk._runtime.offload.run_in_thread`, or better, the
consuming module (`patch.object(storage.integrity, "run_in_thread")`), which is the
correct target either way.

That cost is worth paying where the cycle required it, and **not** worth paying
anywhere else. `execution/_temporal/activities.py` therefore keeps its call-time
import of `auto_heartbeat_loop`: at least one connector's `conftest.py` patches
`application_sdk.execution.heartbeat.auto_heartbeat_loop` to neutralise the beat in
its tests, and that import is intra-`execution` — not a `storage/` edge — so hoisting
it would have broken a real consumer for no benefit to the goal. A test asserts
`activities.py` does not bind the name at module scope.

**The rule is enforced, not remembered.**
`tests/unit/runtime/test_layering.py` imports each `_runtime` module in a
subprocess and fails if any layer above it was pulled in, asserts
`storage.ops` still loads standalone without `execution`, and asserts the façades
re-export the same objects. A subprocess is required: in-process, pytest has
already imported most of the SDK, so `sys.modules` would show every layer loaded
regardless of what the module under test actually needs.

**A new call site can answer the question without experimenting.** Anything in
`application_sdk._runtime` is importable at module scope from anywhere. Anything
under `execution/` is not importable at module scope from `storage/` — and no
longer needs to be.

**The blocking pool is constructed earlier in the process.** `_runtime/offload.py`
creates `_BLOCKING_EXECUTOR` at import, and it is now imported by
`storage/integrity.py`, so any process that touches `contracts` builds the
executor object. `ThreadPoolExecutor` spawns threads lazily on first `submit`, so
an unused pool costs one object and no threads.

## Alternatives considered

**Write the constraint down as an ADR note and have each `# noqa` point at it.**
Cheap, and it would have made the pattern deliberate rather than rediscovered.
Rejected as the endpoint because the cycle stays: the next call site still has to
be lazy, the hot-path constraint in `_fetch_chunk` remains, and "may I import
this at module scope?" still depends on whether the importing module happens to
sit in `storage/__init__`'s eager chain. Worth doing immediately had this move
not been scheduled.

**Make `execution/__init__.py` lazy (PEP 562 `__getattr__`).** This removes the
`ImportError` with a much smaller diff — nothing under `execution/` would need to
move. Rejected because it fixes the symptom and not the layering: `storage/`
would still depend upward on `execution/`, `execution/` would still depend
downward on `storage/`, and the bidirectional relationship the debt is *about*
would survive in a form that no longer announces itself.

**Put the primitives in `application_sdk/common/`.** No new package, and
`common/concurrency.py` (thread-pool sizing) is thematically adjacent. Rejected
because `common/` is neutral only by accident: `common/utils.py` imports
`storage.ops` and `common/incremental/` reaches much further, so the property
holds today solely because `common/__init__.py` happens not to import those.
One line added to that `__init__` would silently re-break every storage module —
the same rediscover-by-`ImportError` trap, one layer down.

## References

* [ADR-0010](0010-async-first-blocking-code.md) — async-first design; `run_in_thread` as a mandatory seam.
* [ADR-0017](0017-native-execution-isolation.md) — `run_fault_isolated` / `run_best_effort`.
* [ADR-0018](0018-progress-aware-heartbeat.md) — the progress tracker, holds, and the stall watchdog.
* `application_sdk/_runtime/__init__.py` — the rule, next to the code it governs.
* `tests/unit/runtime/test_layering.py` — the enforcement.
