# Architecture Decision Records

ADRs capture the architectural decisions behind the Application SDK — what was chosen, what was rejected, and why. Each record is dated and numbered so the context behind a decision is never lost. The Status column mirrors each ADR's own Status section; a superseded ADR names its successor there.

| ADR | Status | Decision |
| --- | --- | --- |
| [ADR-0001](0001-per-app-handlers.md) | Superseded in part by [ADR-0023](0023-consolidated-api-host.md) | Per-App Handlers vs Uber-Handler |
| [ADR-0002](0002-per-app-workers.md) | Accepted | Per-App Workers vs Uber-Worker |
| [ADR-0003](0003-per-app-observability.md) | Accepted | Per-App Observability with Correlation-Based Tracing |
| [ADR-0004](0004-build-time-type-safety.md) | Accepted | Build-Time Type Safety Over Runtime Validation |
| [ADR-0005](0005-infrastructure-abstraction.md) | Accepted | Abstraction of Underlying Infrastructure |
| [ADR-0006](0006-schema-driven-contracts.md) | Accepted | Schema-Driven Contracts with Additive Evolution |
| [ADR-0007](0007-apps-as-coordination-unit.md) | Superseded (no successor ADR) | Apps as the Unit of Inter-App Coordination |
| [ADR-0008](0008-payload-safe-bounded-types.md) | Accepted | Payload-Safe Bounded Types in Contracts |
| [ADR-0009](0009-separate-handler-worker-deployments.md) | Accepted | Separate Handler and Worker Deployments |
| [ADR-0010](0010-async-first-blocking-code.md) | Accepted | Async-First Design and Blocking Code Pitfalls |
| [ADR-0011](0011-logging-level-guidelines.md) | Accepted | Logging Level Guidelines |
| [ADR-0012](0012-observability-consolidation.md) | Accepted | Observability Consolidation |
| [ADR-0013](0013-error-hierarchy-and-failure-taxonomy.md) | Accepted | Error Hierarchy and Failure Taxonomy |
| [ADR-0014](0014-two-store-storage-architecture.md) | Accepted | Two-Store Storage Architecture (task-to-task vs app-to-app) |
| [ADR-0015](0015-directory-listing-race-fix.md) | Accepted | Directory Listing Race Fix (safe_list_directory primitive) |
| [ADR-0016](0016-multi-pool-worker-routing.md) | Accepted | Multi-Pool Worker Routing (N pools per app, @task(pool=...) static affinity) |
| [ADR-0017](0017-native-execution-isolation.md) | Accepted | Native Execution Isolation (best-effort work in an isolated process; run_fault_isolated / run_best_effort, conformance rule P036) |
| [ADR-0018](0018-progress-aware-heartbeat.md) | Accepted | Progress-Aware Stall Watchdog and Duration-Backstop Timeouts (in-process no-progress watchdog; heartbeat_timeout unchanged; start_to_close = backstop) |
| [ADR-0019](0019-runtime-substrate-layer.md) | Accepted | A Dependency-Neutral Runtime Substrate (`application_sdk._runtime`: offload + progress seams importable at module scope from any layer; kills the storage ↔ execution cycle) |
| [ADR-0020](0020-artifact-validation.md) | Accepted | Artifact Validation (one wrapper, per-format validators with their own dependency floors, app-owned declarations; mandatory at the public boundary, optional internally) |
| [ADR-0021](0021-source-validation.md) | Proposed | Source Validation (canonical probe names, network → identity → SQL ordering with skipped probes omitted from rows, app-declared endpoints and dialect facts, warn-only until precision/recall are measured) |
| [ADR-0022](0022-skipped-record-capture.md) | Accepted | Skipped-Record Capture (`skip_record` with a closed `SkipReason` set; one merged `skipped-records` artifact per run, absent when nothing was skipped; `PARTIAL_SUCCESS` derived from the summed count; ADR-0020 two-tier bounding) |
| [ADR-0023](0023-consolidated-api-host.md) | Accepted | Consolidated API Host for Hosted Apps' Handlers (one process serves opted-in hosted apps' handler routes; trusted first-party apps only; per-app thread pools and upload namespaces; one resolved dependency set, checked before a pin moves) |
