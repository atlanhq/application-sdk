# ADR-0017: Native Execution Isolation (best-effort work in an isolated process)

## Status
**Accepted** — extends [ADR-0010](0010-async-first-blocking-code.md).

## Context

The SDK runs on Temporal's asyncio event loop. [ADR-0010](0010-async-first-blocking-code.md)
established the async-first model: activities are `async def`, and genuinely
blocking work is offloaded with `run_in_thread()` onto a dedicated
`sdk-blocking-*` pool so it never starves the heartbeat loop.

That model handles *blocking*, but not *crashing*. A **native fault** — a
`SIGSEGV`/`SIGABRT` in a C extension (`msgspec`, `pyatlan`, `rocksdict`,
`pandas`, …) — is **not** a Python exception. It bypasses every `try/except` and
kills the OS process. In a worker thread that means the entire Temporal worker
dies mid-poll, taking every concurrent activity with it. Worse, the failure can
be invisible: the process can die in a way k8s and Temporal only notice via
timeout, not a clean signal (see the worker-zombie failure mode).

This bit us in production (CNCT-85): a **warn-only**, best-effort pre-upload
asset-validation scan decoded assets via `msgspec`, hit a native
concurrent-decode segfault on py3.13, and crashed the worker. A feature with
near-zero upside took down an essential worker — a wholly disproportionate blast
radius.

The SDK aggressively adopts new Python minors (the CI matrix already includes
3.14), so "a native library that isn't yet safe on this interpreter" is a
*standing* exposure, not a one-off. We need a durable, uniform policy — not a
per-feature patch — for how native code runs relative to the worker.

Two properties are often conflated and must be separated:

- **Crash containment** — a native fault does not kill the worker. This requires
  a separate **process**. Nothing else (not a thread, not a Temporal activity —
  activities run *in* the worker process) provides it.
- **Failure recovery** — a dead worker is detected and its work rescheduled.
  Temporal + k8s already provide this for *any* worker death.

## Decision

**1. Classify every native/blocking code path on one axis: essential vs
best-effort.**

- **Essential** work *is* the job (SQL drivers, `obstore`, `pyatlan` on the
  extract/transform/upload path). A crash here means the job genuinely cannot
  proceed on this worker, so a worker crash is *proportionate*. It runs
  **in-process** (async, or `run_in_thread()` for blocking), and recovery is
  Temporal activity-retry + k8s pod-restart. Per-call process isolation is
  infeasible here (pickling large payloads, N× interpreter memory) and buys
  little.
- **Best-effort** work is auxiliary — its result is used when present and safely
  skipped when absent, and it must **never** be able to crash or fail the worker
  (warn-only validation, optional telemetry/decode). It runs through the
  sanctioned isolation seam below.

**2. Provide three sanctioned offload primitives in
`application_sdk.execution.heartbeat`** (siblings, async-first):

| Primitive | Isolation | On failure | Use for |
|---|---|---|---|
| `run_in_thread(fn, …)` | thread (no crash containment) | propagates | essential blocking work |
| `run_fault_isolated(fn, …)` | **child process** (spawn) | **raises** `BrokenProcessPool` / `TimeoutError` | crash-prone work whose failure the caller handles |
| `run_best_effort(fn, *, label, logger, …)` | child process (over `run_fault_isolated`) | **swallows**: logs a WARNING, returns `None` | best-effort native work |

`run_fault_isolated` is the mechanism (isolate a native fault into a catchable
exception); `run_best_effort` is the policy layer (isolate **and** warn-and-
continue). Both accept an optional `max_workers` — default `min(4, cpu_count)`
so concurrent callers decode in parallel; pass `1` to opt into sequential
execution, or another integer to bound concurrency. Pools are keyed by width so
one width's crash never disturbs another's.

Spawn (not fork) is mandatory: forking a multi-threaded worker copies locks in
arbitrary states — a deadlock/corruption factory.

**3. Best-effort native work MUST route through `run_best_effort`** — it must not
hand-roll a `ProcessPoolExecutor` or `multiprocessing` child. This is enforced
statically by conformance rule **P036** (`HandRolledProcessIsolation`, WARN),
which flags bare `ProcessPoolExecutor` / `multiprocessing.Process`/`Pool`
construction outside the seam. The rule makes the policy un-bypassable: any
future native path is classified once and routed consistently, rather than
rediscovered case-by-case.

**4. We deliberately do NOT adopt Temporal's `ProcessPoolExecutor`
`activity_executor` for this.** That mechanism runs *whole sync activities* in a
process pool at worker-config granularity, requires all activity args/returns to
be picklable and a `SharedStateManager`, and Temporal itself recommends threaded
activities over it (multiprocess activities cannot raise on cancellation — the
exact stuck-child problem `run_fault_isolated` hand-handles). Crucially it would
invert this SDK's async-first model (we set no `activity_executor`). It is the
wrong granularity (whole activity vs one risky call) and the wrong altitude for
"contain one crash-prone call inside an otherwise-in-process activity."

## Consequences

**Positive**

- A best-effort feature can never take down an essential worker via a native
  fault — the CNCT-85 class of bug is structurally prevented.
- One shared, well-tested primitive instead of per-feature process code; the
  investment amortizes across all current and future best-effort native work.
- Enforceable and uniform (P036), so the policy doesn't erode.

**Negative / costs**

- A spawn child re-imports the callable's module (e.g. `pyatlan`/`msgspec`) on
  first use — real memory (tens–hundreds of MB per child) against
  `K8S_POD_MEMORY_LIMIT`, and a cold-start latency after idle/crash recycle. The
  width cap keeps this bounded; validate spawn viability + child RSS in the
  runtime container (hardened images may restrict `/dev/shm`).
- `run_fault_isolated`/`run_best_effort` inherit process-boundary constraints:
  the callable must be a module-level, picklable function; args/return must
  pickle; ContextVars do **not** propagate (log from the parent).
- **Essential** work is explicitly *not* contained: a native fault there still
  crashes the worker. That is an accepted tradeoff — recovery (Temporal + k8s),
  not containment, is the contract for essential paths, because per-call
  isolation of the hot path is infeasible.

## Alternatives considered

- **Thread-local / per-call decoder (no process).** Cheaper, and removes *this*
  msgspec bug's trigger, but provides no containment against arbitrary future
  native faults — insufficient for the standing exposure.
- **`run_best_effort` as a plain (threaded) Temporal activity.** Gives failure
  *recovery* and visibility, but not *containment* — an activity runs in the
  worker process. Composes with, but does not replace, the process seam.

## References
- [ADR-0010: Async-First Design and Blocking Code Pitfalls](0010-async-first-blocking-code.md)
- Conformance rule **P036** — `packages/conformance/conformance/docs/rules/prescriptions.md#p036`
- CNCT-85 (warn-only upload validation crashed the worker via a native msgspec fault)
