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
- Missing `correlation_id` propagation in inter-app calls (`call_by_name`)
- Log statements without structured context (app_name, run_id, correlation_id should come from `self.logger`)
- Custom logger instantiation instead of using `self.logger` (which auto-includes correlation context)
- Hardcoded service names instead of using framework-provided `ATLAN_SERVICE_NAME`

---

## ADR-0004: Build-Time Type Safety

**Rule:** All contracts are `pydantic.BaseModel`. Type checking via Pyright **standard mode** (`typeCheckingMode = "standard"` in pyproject.toml; tests/ excluded from Pyright). `__init_subclass__` hooks + `@task` decorator validation provide build-time safety. No runtime validation overhead. Agents must only flag violations that would fail CI under standard mode — do not apply strict-mode rules.

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

**Rule:** Apps invoke other Apps as Temporal child workflows via `call_by_name()`. Tasks are strictly internal — never callable from outside the App.

**Violations to flag:**
- Direct task method invocation across App boundaries
- Importing another App's implementation class (should only import its contracts)
- Missing `task_queue` parameter in `call_by_name()` calls
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

**Rule:** Async-first. Blocking operations must use `self.task_context.run_in_thread()`. Blocking code must have internal timeouts (framework cannot kill threads).

**Violations to flag:**
- Blocking calls (`requests.get`, `open()` for large files, sync DB drivers) without `run_in_thread()`
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

- **App base.py size**: If changes increase `app/base.py` beyond ~1600 lines, flag for decomposition
- **Registry singleton safety**: `AppRegistry` and `TaskRegistry` mutations must be thread-safe
- **Deprecation shims**: Any removed v2 import path must have a deprecation shim with `warnings.warn()` pointing to the v3 replacement
- **`__init_subclass__` hooks**: New metaclass or `__init_subclass__` must not break existing subclass registration
