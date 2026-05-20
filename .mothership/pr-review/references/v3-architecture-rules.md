# v3 Architecture Review Rules

Derived from the 11 Architecture Decision Records (ADRs) governing application-sdk v3.

---

## ADR-0001: Per-App Handlers

**Rule:** Each app runs its own handler service. Handlers are stateless HTTP endpoints (auth, preflight, metadata) separate from Temporal workers.

**Violations to flag:**
- Handler sharing state between requests (instance variables set in one call, read in another)
- Handler holding persistent connections (should be created per-request or via context injection)
- Missing `Handler` ABC subclass for new apps that expose HTTP endpoints
- Handler methods using `*args, **kwargs` instead of typed `AuthInput`/`PreflightInput`/`MetadataInput` contracts

---

## ADR-0002: Per-App Workers

**Rule:** Each app has its own Temporal worker with dedicated task queue. Workers scale to zero via KEDA.

**Violations to flag:**
- Multiple App subclasses sharing a task queue name
- Worker configuration that prevents scale-to-zero (hardcoded minReplicas > 0 for workers)
- Missing KEDA ScaledObject in Helm changes for new apps

---

## ADR-0003: Per-App Observability with Correlation-Based Tracing

**Rule:** Each app exports telemetry under its own `OTEL_SERVICE_NAME`. Distributed traces linked via `correlation_id`.

**Violations to flag:**
- Missing `correlation_id` propagation across app boundaries
- Log statements without structured context (app_name, run_id, correlation_id should come from `self.logger`)
- Custom logger instantiation instead of using `self.logger` (which auto-includes correlation context)
- Hardcoded service names instead of reading from the `OTEL_SERVICE_NAME` environment variable (see `application_sdk/constants.py`)

---

## ADR-0004: Build-Time Type Safety

**Rule:** All contracts are `pydantic.BaseModel`. Type checking via Pyright strict mode + `__init_subclass__` hooks + `@task` decorator validation. No runtime validation overhead.

**Violations to flag:**
- `Dict[str, Any]` as task input/output (must be typed `Input`/`Output` subclass)
- Missing type annotations on public methods
- `# type: ignore` without justification comment
- Raw dict access for data that should be a Pydantic model
- Contract fields that would fail Pyright (mismatched types, missing Optional for nullable)

---

## ADR-0005: Infrastructure Abstraction

**Rule:** Developers never import from `temporalio` or `dapr` directly. Framework applies decorators automatically. Implementation details live in `_`-prefixed directories.

**Violations to flag:**
- `from temporalio import workflow, activity` in app code (use `@task` decorator)
- `from dapr.clients import DaprClient` in app code (use infrastructure protocols)
- Importing from `application_sdk/execution/_temporal/` directly (private module)
- Importing from `application_sdk/infrastructure/_dapr/` directly (private module)
- Missing `self.now()` usage (using `datetime.now()` instead — breaks Temporal replay determinism)
- Missing `self.uuid()` usage (using `uuid.uuid4()` instead — breaks determinism)
- Any non-deterministic operation in `run()` method body (I/O, random, time) that isn't inside a `@task`

---

## ADR-0006: Schema-Driven Contracts with Additive Evolution

**Rule:** Every `run()` and `@task` method accepts exactly one `Input` subclass and returns one `Output` subclass. Evolution: add fields with defaults only. Never remove or rename fields. Never change field types.

**Violations to flag:**
- Task method with multiple parameters (must be single Input)
- Task method returning a raw type (must be Output subclass)
- New fields on existing contracts without default values (breaks replay of in-flight workflows)
- Removed or renamed fields on existing contracts
- Changed field types on existing contracts
- `run()` method not following the single-Input, single-Output pattern

---

## ADR-0007: Apps as Unit of Inter-App Coordination

> **⚠️ Under Review — BLDX-878**: `call()` / `call_by_name()` are **deactivated** in the SDK. Do not recommend or flag missing usage of these methods. Multi-app coordination goes through Automation Engine DAG orchestration.

**Rule (still enforced):** Tasks are strictly internal — never callable from outside the App.

**Violations to flag:**
- Direct task method invocation across App boundaries
- Importing another App's implementation class (should only import its contracts)
- Tight coupling: one App class importing internals of another

---

## ADR-0008: Payload-Safe Bounded Types

**Rule:** Temporal has a 2MB payload limit. Contracts validated at import time. Forbidden types: `Any`, `bytes`, `bytearray`, unbounded `list[T]`, unbounded `dict[K, V]`.

**Violations to flag:**
- `Any` type in contract fields
- `bytes` or `bytearray` in contract fields (use `FileReference`)
- Unbounded `list[T]` without `MaxItems` annotation
- Unbounded `dict[K, V]` without `MaxItems` annotation
- Large data passed directly in contracts instead of via `FileReference`
- `allow_unbounded_fields=True` without documented justification

---

## ADR-0009: Separate Handler and Worker Deployments

**Rule:** Handlers always-on (minReplicas: 1), workers scale to zero. All framework env vars use `ATLAN_` prefix.

**Violations to flag:**
- Framework env vars without `ATLAN_` prefix (except standard `OTEL_` vars)
- Helm templates that couple handler and worker deployments
- Missing `--mode handler` / `--mode worker` support in new entry points

---

## ADR-0010: Async-First Design

**Rule:** Async-first. Blocking operations must use `self.task_context.run_in_thread()`. Blocking code must have internal timeouts (framework cannot kill threads). Never use `run_in_thread()` to wrap `AtlanClient` calls — the Atlan client is async-only; use its native async API.

**Violations to flag:**
- Blocking calls (`requests.get`, `open()` for large files, sync DB drivers) without `run_in_thread()`
- `run_in_thread()` wrapping `AtlanClient` or any other async-native Atlan SDK call
- `run_in_thread()` calls where the blocking function has no internal timeout parameter
- `time.sleep()` in async code (use `asyncio.sleep()`)
- Sync HTTP libraries (`requests`, `urllib3`) when `httpx` async is available
- `await` on CPU-bound computation that should be in a thread

---

## ADR-0011: Logging Level Guidelines

**Rule:** Four levels only — DEBUG, INFO, WARNING, ERROR. Never CRITICAL.

**Violations to flag:**
- `logger.critical()` — not used in this project
- `logger.info()` for per-item progress (should be DEBUG with sampling)
- `logger.error()` for recoverable situations (should be WARNING)
- `logger.warning()` or `logger.error()` without `exc_info=True` when swallowing exceptions
- Expensive computations inside log calls at any level (use lazy evaluation / %-style)
- Missing structured context fields (prefer keyword args over string interpolation for Loki/Grafana indexing)
- f-string or `.format()` in log calls (must use %-style: `logger.info("Found %d records", count)`)

---

## General Architecture Checks

- **App base.py size**: `app/base.py` is currently ~1739 lines (decomposition tracked). Flag any PR that increases it further — do not allow unbounded growth
- **Registry singleton safety**: `AppRegistry` and `TaskRegistry` mutations must be thread-safe
- **Deprecation shims**: Only for v2 paths where connectors may already import the old symbol (i.e. `application_sdk.test_utils.integration` → `application_sdk.testing.integration`). Do NOT add shims for symbols that were removed before v3 shipped — those never existed from a public-API perspective
- **`__init_subclass__` hooks**: New metaclass or `__init_subclass__` must not break existing subclass registration

---

## Retro Learnings — 2026-05-20

> Each subsection is a generalized rule. Concrete incidents that
> surfaced the rule are recorded in `retro-2026-05-20.md`.

### Contract discipline — Pydantic v2 only, no `@dataclass`, default_factory for mutables (Critical)

This rule consolidates several CLAUDE.md mandates that PRs keep
slipping past. All four sub-rules apply to any class that is — or
inherits from — `pydantic.BaseModel`, `Input`, `Output`,
`HeartbeatDetails`, a credential type, or anything under
`application_sdk/contracts/`. Public connector developers depend on
these classes; getting any of the four wrong is a Critical finding.

#### Sub-rule 1: No `@dataclass` on Pydantic contracts (Critical)

Pydantic handles `__init__`, validation, and serialization. Stacking
`@dataclass` on top of `BaseModel` produces a class that looks
correct but bypasses Pydantic's validation pipeline and breaks
serialization through Temporal's data converter.

**Flag any new code that:**
- Adds `@dataclass` (any form: `@dataclass`, `@dataclass(frozen=True)`,
  `@dataclasses.dataclass`, `@dc.dataclass`) on a class that is or
  inherits from `BaseModel`, `Input`, `Output`, or anything under
  `application_sdk/contracts/`.
- Adds `@dataclass` on a class meant to flow through a Temporal
  payload (return type of a `@task`, parameter of `run()`, anything
  serialized by `pydantic_data_converter`).
- Defines `@dataclass`-style helper classes in `contracts/` even
  for "internal" use — those tend to escape into the public surface
  through `__init__.py` re-exports.

**Diff-grep heuristic:** `grep -E '^\+.*@dataclass' diff` — every
hit needs to be checked against the class's base. If the base is
(or inherits from) `BaseModel` → Critical finding.

**Replacement:** delete the `@dataclass` decorator; rely on
`BaseModel.__init__`. If the class needs to be frozen, use
`model_config = ConfigDict(frozen=True)` or
`class Foo(BaseModel, frozen=True): ...`.

#### Sub-rule 2: Pydantic v2 config style only (Critical)

The SDK is on Pydantic v2. The v1 inner-`class Config:` style is
silently ignored in v2 — `class Config:` declared on a v2 BaseModel
does nothing. PRs that drop a v1 `class Config:` block ship a model
whose config is whatever the defaults are, not what the author
thought they were configuring.

**Flag any new code that:**
- Adds an inner `class Config:` block inside a `BaseModel` subclass.
- Uses `Config = ...` class-level assignment for v1-style config.
- Mixes v1 `Config` and v2 `model_config` in the same class.

**Replacement:** `model_config = ConfigDict(frozen=True, extra='forbid', ...)`.

#### Sub-rule 3: `Field(default_factory=...)` for mutable defaults (Critical)

`field: list = []` shares the SAME list instance across every
instance of the class — classic Python gotcha that produces silent
state corruption. The Pydantic-v2-safe replacement is
`field: list = Field(default_factory=list)`.

**Flag any new code that:**
- Declares a field on a `BaseModel` (or any class) with a mutable
  default literal: `field: list = []`, `field: dict = {}`,
  `field: set = set()`, `field: list[X] = []`.
- Uses `field: Optional[list] = []` (still shared, just lets `None`
  through).

**Diff-grep heuristic:**
`grep -E '^\+.*: (list|List|dict|Dict|set|Set)(\[[^]]+\])?\s*=\s*(\[\]|\{\}|set\(\))' diff` —
every hit is a candidate.

**Replacement:** `field: list[X] = Field(default_factory=list)` (or
`list`, `dict`, etc. as appropriate).

#### Sub-rule 4: Frozen contracts where identity matters (Important)

Contracts used as dict keys, hash keys, message-bus payloads, or
shared across activity boundaries should be immutable. `FileReference`,
`CredentialRef`, and credential subclasses are already frozen.
New similar-purpose contracts MUST also be frozen.

**Flag any new contract that:**
- Resembles a value object (no methods beyond accessors, models a
  fact rather than an entity) and is NOT declared frozen.
- Inherits from `FileReference` or `CredentialRef` without
  preserving `frozen=True`.

**Replacement:** `class Foo(BaseModel, frozen=True): ...` or
`model_config = ConfigDict(frozen=True)`.

### Typed contracts everywhere — no `dict`/`Any` escape hatches on public surface (Critical)

This rule restates and **strengthens** ADR-0004 / ADR-0008 because
review history shows the framework rule is being approved past. Every
parameter, return type, and contract field on the public API surface
of `application_sdk/` MUST be typed concretely. `dict[str, Any]` /
`Dict[str, Any]` / `Any` / `object` / `dict` (bare) / `list` (bare)
are escape hatches — they accept arbitrary shapes, break Pyright,
and force every connector developer to read the source to figure out
what the field actually contains.

**Treat every occurrence in a public-surface location as a Critical
finding. Public surface means:**

- Any class field on a `pydantic.BaseModel` subclass that is — or
  inherits from — `Input`, `Output`, `HeartbeatDetails`, a credential
  type, an event payload, or any model under `application_sdk/contracts/`.
- The signature of any function, method, classmethod, or constructor
  exported from `application_sdk/<module>/__init__.py` (i.e. anything
  importable as `application_sdk.<module>.<symbol>`).
- The signature of any `@task`-decorated method, `@entrypoint`-
  decorated method, or `run()` override.
- The return type of any factory / builder / accessor function whose
  return value crosses an `application_sdk/` module boundary.
- The signature of any `Handler` subclass method (`auth`, `preflight`,
  `metadata`, custom handler endpoints).

**Flag the following forbidden type forms anywhere in those locations:**

| Forbidden form | Why |
|---|---|
| `dict[str, Any]`, `Dict[str, Any]`, `Mapping[str, Any]` | Accepts arbitrary keys and values; no validation; no IDE support. Use a Pydantic model. |
| `dict`, `Dict`, `list`, `List`, `tuple`, `Tuple`, `set`, `Set` (bare, no type parameters) | Equivalent to `dict[Any, Any]`. Specify the value type, or use a model. |
| `Any`, `object` | Accepts anything; no contract. |
| `Union[A, B, ...]` / `A \| B` where one of the variants is `dict` / `Dict` / `Any` | The escape hatch is now inside a Union; just as bad. |
| `**kwargs: Any` on a public method | Hides the contract. Either accept a typed config model, or enumerate the keyword arguments with concrete types. |
| `# type: ignore` on a public signature line | Almost always means a typed alternative exists or the public surface needs to be reshaped. Require an inline justification or fix. |

**Detection heuristic (deterministic; run on the diff):**

```bash
git diff origin/main...HEAD -- 'application_sdk/**/*.py' | \
  grep -E '^\+' | \
  grep -E ':\s*(dict|Dict|list|List|tuple|Tuple|set|Set)\b[^[]|\b(dict|Dict|Mapping)\[\s*str\s*,\s*Any\s*\]|:\s*(Any|object)\b|\*\*kwargs:\s*Any'
```

Every hit is a candidate finding. Walk each one back to its
declaration site and confirm whether it is on a public surface per
the list above. If yes → Critical.

**The narrow legitimate exceptions** (must be explicitly justified in
the PR description or a same-line comment, otherwise flag):

- **Dapr/Temporal interop boundary** — code inside
  `application_sdk/infrastructure/_dapr/` and
  `application_sdk/execution/_temporal/` may use `Dict[str, Any]`
  where it directly hands payloads to/from the upstream library
  that itself uses untyped dicts. The boundary must be in a private
  (`_`-prefixed) module and the typed model must take over within
  one function call.
- **Wire-format pass-through** — fields that carry an opaque blob to
  be deserialised by the caller (rare; should be `bytes` or a
  `FileReference` instead).
- **Generic helper utilities** in `application_sdk/common/` that are
  intentionally type-agnostic (`merge_dicts`, `deep_get`, etc.) —
  but these should be `dict[K, V]` with TypeVar `K`, `V`, not
  `dict[str, Any]`.

**Migration paths for the most common findings:**

| Anti-pattern | Replacement |
|---|---|
| `credentials: dict[str, Any] \| None` | A typed credential model (`CredentialDict` or a connector-specific credential subclass). If truly generic, accept `CredentialRef` and let the resolver produce the typed object. |
| `metadata: dict[str, Any] = {}` on an Input | A dedicated `Metadata` Pydantic model with the actual fields named, or an explicit `extra="allow"` ConfigDict with a documented schema. |
| `def task(...) -> dict[str, Any]` | Define a `TaskOutput(BaseModel)` and return it. |
| `properties: dict[str, Any] = Field(...)` | A typed `Properties` submodel, or `list[Property]` where `Property` is a small Pydantic model. |
| `**kwargs: Any` on a public method | A typed config model passed as a single argument. |

**Severity:** Critical (ARCH + DX). Counts as a violation of
ADR-0004 (build-time type safety) and ADR-0008 (payload-safe bounded
types). Every untyped-dict field on a public contract is a future
refactor blocker — once a connector ships using it, the SDK can no
longer tighten the type without a major-version bump.

### Input/Output direction must match the producer/consumer relationship (Critical)

Every typed contract belongs to a specific activity in a specific
direction. A field on `*Input` is consumed by the task that takes it
as parameter — the task body reads it before doing work. A field on
`*Output` is produced by that task and returned. Placing a produced
value on `Input` (or a consumed value on `Output`) silently corrupts
inter-activity hand-off: a downstream task will read defaults
instead of the value the upstream task wrote.

**For every new or moved field on a public contract, verify direction
by reading the task body that uses it:**

- If the task method reads it via `input.<field>` *before* doing any
  work → the field belongs on the task's `*Input`.
- If the task method assigns to it and returns it via
  `return MyOutput(<field>=…)` → the field belongs on that task's
  `*Output`.
- A value produced by task A and consumed by task B is on A's Output
  AND on B's Input (different contracts; same value flows through).

**Flag any change that:**
- Adds a field on `*Input` that the owning task never reads in its
  body (only writes to or only passes through).
- Adds a field on `*Output` that the owning task never produces in
  its body.
- Adds a field whose producer and consumer cannot be identified in
  this repo or a named consumer repo — i.e. a speculative or
  pre-emptive field with no current caller (these accumulate as dead
  surface area and break later refactors).

**Detection heuristic:** for each new field `foo` on a contract,
grep the same module for `input.foo` (Input reads) and
`OutputClass(foo=` / `return OutputClass(... foo=...)` (Output
writes). If neither pattern appears → the field is speculative; flag
it. If the pattern is opposite to the contract direction → flag it.

**Severity:** Critical. Wrong-side placement is a wire-shape break
that the type checker cannot catch.

### Field removal cascades across consumer repositories (Critical)

Removing a field from a public `Input`/`Output` contract is in scope
of G2 (Contract Safety), but the check is incomplete if it only
looks within this repo. Connector apps in sibling repositories
import and read these contracts at runtime; Pydantic raises
`AttributeError` when a removed attribute is accessed. The SDK PR
that removes a field MUST land *after* or *with* consumer-side
removals — never alone.

**For any deletion of a field on a public `Input`/`Output` contract:**

1. Identify every consumer repository likely to read the field.
   Default consumer set: any `atlanhq/atlan-*-app` repository that
   imports from `application_sdk`. The `audit-consumers` skill
   automates this; grep
   `github.com/atlanhq atlan-*-app input.<field>` /
   `<ContractClass>(... <field>=...)` is the manual path.
2. For each consumer, confirm one of: (a) it doesn't read the
   field, (b) an open PR removes the read, (c) it will hit
   `AttributeError` at runtime.
3. If (c) is non-empty → flag G2 violation with the consumer list.
   Either keep the field as a deprecated optional (`Field(default=None)`
   plus a docstring noting the removal version), or block the SDK PR
   until consumer-side PRs are queued.

**Severity:** Critical (G2). Consumer-blast-radius check is mandatory
for any contract field deletion.

### Temporal workflow sandbox restrictions (Important)

Temporal's workflow sandbox restricts non-deterministic stdlib calls
(`random.*`, `time.*`, signal handlers, file I/O, etc.) inside
workflow validation. Any module imported on the worker-startup path
whose initialization touches these stdlib functions can crash worker
boot with a `RestrictedWorkflowAccessError` — sometimes deterministically,
sometimes only on slow runners where signal handlers race with
validation.

**Flag any change that:**
- Imports a profiler (`scalene`, `pyinstrument`, `viztracer`,
  `py-spy`, similar) at module top level in code loaded during
  worker startup, without an accompanying entry in the Temporal
  sandbox passthrough list
  (`application_sdk/execution/_temporal/worker.py`).
- Adds a new background thread, signal handler, or `atexit` hook in
  startup code (combined-mode entry point, app `__init__`,
  decorators that fire at import time) that touches `random.*`,
  `time.time()`, or other Temporal-restricted modules during
  workflow validation.
- Modifies the Temporal sandbox passthrough list (adds a new
  passthrough module) without justifying in the PR body: what
  module, what it imports during validation, why the passthrough is
  safe (i.e. why the module's behaviour is deterministic from
  Temporal's POV).

**Why generalize:** failures in this class look different (profiler
crashes, signal-handler races, "works locally, breaks on CI") but
share the same root cause — module-init-time access to non-
deterministic stdlib functions under the workflow sandbox. The rule
is "anything new on the worker-startup path must be sandbox-safe."

**Severity:** Important. Catches a class of CI-only failures that
consumers struggle to diagnose.

### Async shutdown paths must have bounded waits (Important)

`flush()`, `close()`, `shutdown()`, SIGTERM handlers, and other
cleanup paths run during pod termination on a Kubernetes timeout.
An unbounded `await` (no `asyncio.wait_for(..., timeout=…)`) inside
any of these can extend pod termination indefinitely, causing
rolling restarts to stall, deploys to time out, and metrics/spans to
be dropped silently.

**Flag any code in a cleanup-flagged context that:**
- `await`s on an external operation (HTTP flush, queue drain, DB
  commit, gRPC close) without a `timeout=` parameter or an
  enclosing `asyncio.wait_for(..., timeout=…)`.
- Adds a new `async def flush() / close() / shutdown() /
  on_terminate()` method that calls another `await` without
  documenting an upper bound.
- Adds a SIGTERM/SIGINT handler that schedules an `await` task
  without a deadline.

**Detection heuristic:** in functions whose name matches
`(flush|close|shutdown|stop|terminate|teardown|drain)`, every
`await` must have a `timeout=` argument or sit inside
`asyncio.wait_for(...)`.

**Severity:** Important (DETERMINISM). Failure mode is silent in
dev but corrosive in production rolling restarts.

### Single source of truth for cross-cutting versions and tunables (Important)

A version string, image tag, dependency pin, or tunable constant
that drives behaviour in more than one place MUST live in exactly
one location and be referenced from everywhere else. When the same
literal value appears in multiple files, the duplicates drift, and
the symptom (mismatched behaviour, surprise version bumps,
incompatible CI vs runtime) is hard to attribute.

**Flag any change that:**
- Hard-codes a version string (semver, image tag, dependency pin,
  protocol revision) in a file when the same string already lives
  elsewhere in the repo. Grep for the literal value before flagging
  — if it appears in `>1` location, the new occurrence should
  import from a single source-of-truth (typically `version.py`,
  `constants.py`, `pyproject.toml`, or a documented `.env` template).
- Hard-codes a tunable (timeout, sample interval, batch size,
  retry backoff, metric prefix) inline in source code instead of in
  `application_sdk/constants.py` with an `ATLAN_`-prefixed env-var
  override. The constants module is the documented place for
  framework-level tunables; inline values can't be overridden by
  operators.
- Defines a default value in two places (one in `constants.py`, one
  inline in a call site). They will drift; only `constants.py`
  should hold the default.

**Detection heuristic:** for each new literal that looks like a
version (`r"\d+\.\d+\.\d+"`), image tag (`ghcr.io/...:tag`),
timeout/interval (numeric followed by `_seconds`, `_ms`, `_secs`),
or a string used as a metric/log attribute prefix, run
`git grep -F "<literal>"` across the repo. More than one hit → flag.

**Severity:** Important (STRUCT + DX). Catches drift before it
becomes a production incident.
