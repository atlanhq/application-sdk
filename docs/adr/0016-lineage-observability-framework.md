# ADR-0016: Lineage Observability Framework — SDK Placement and Argo Engine Provenance

## Status
**Proposed** (2026-06-18 — Linear project *Tableau Lineage Observability*, Milestone 2 "Create a Lineage observability integration framework for BI connectors"; introduced by application-sdk#2204). Becomes **Accepted** on merge.

## Context

In Atlan, lineage is **Process** / **ColumnProcess** entities (plus relationship `inputs`/`outputs`). For BI connectors, an edge is created either at transform time (BI→BI) or, for BI→warehouse edges, emitted as an unresolved `arsIdentity` that the **publish-app** resolves later (ARS). Today a "lineage capable" asset that gets no lineage fails **silently** — `return None` / `continue` at transform, or a `noMatchAction='drop'` absence at publish.

The argo-world Tableau connector solved this in M1 with a `LineageCoverageTracker` embedded in `marketplace_scripts/lineage/tableau_main.py`: it registers every lineage-capable asset, marks success, and records a **structured reason** on every skip, emitting an aggregate coverage summary (→ Mixpanel) and a per-asset reason artifact (→ reactive RCA). It is proven at scale (LendingClub ~250k assets, VMO2).

As all BI connectors migrate to AE (App Framework v3 on this SDK), each would otherwise re-implement that logic. We need a **reusable, extensible** framework. Two decisions are conflated in casual framing ("embed the argo logic in the SDK") and must be separated:

- **Decision A — where the framework lives.**
- **Decision B — how the in-activity tracker *engine* is built** (copy the argo logic vs build new).

A third concern — **distributed aggregation across Temporal activities/pods, the connector↔publish ARS stitch, the artifact schemas, and the metric/Mixpanel emission** — is **net-new regardless of A/B**: the argo tracker is single-process and has no equivalent. So "direct copy from argo" describes only the engine (Decision B), not the framework as a whole.

**Forces:**
- Connectors are **separate repos** that depend on this SDK → the public surface must be stable and importable; SDK-internal coupling needs justification.
- AE transforms run across **many activities on different pods** (e.g. Looker = 31 transform activities); process-local memory cannot be the aggregation substrate.
- The engine runs in **per-row hot loops** at 200k+ assets → a NoOp path must be ~free; the merge must be bounded-memory.
- **Observability must never break lineage generation** (fail-open).
- The argo engine's per-asset bookkeeping (idempotent miss-cancellation, coverage math, the reason taxonomy) is **battle-tested**; re-deriving it is pure risk with no product upside.

## Decision

1. **(A) The framework lives centrally in the SDK**, at `application_sdk/observability/lineage/`, as the single stable import surface for every connector repo and the publish-app.
2. **(B) The in-activity tracker engine is a verbatim port of the argo `LineageObservabilityTracker` / `NoOp` / reason taxonomy** — `build_output` is byte-stable, locked by the argo test suite ported as a regression gate. The only changes are severing the `marketplace_scripts` import roots and two additive methods (`emit_intent`, `success_keys`) that touch new state only.
3. **(C) Everything above the engine is net-new** and AE-native: an object-store partial+reduce, the connector↔publish ARS two-phase stitch on a shared identity hash, the v2 artifact schemas, a per-activity ContextVar interceptor, and the metrics/Mixpanel emission. These are *not* copied — argo had none.

## Options Considered — Decision A: Where the framework lives

### Option A1: Central in `application_sdk` (Chosen)

The tracker, taxonomy, identity hash, schemas, writers, ContextVar accessor, and interceptor live in the SDK; connectors import them and own only their reason registry + skip annotations.

**Pros:**
- **One implementation, all connectors.** Tableau/Sigma/Looker/MicroStrategy/PowerBI get coverage by importing, not re-implementing — directly satisfies the M2 "plug-and-play for any connector" goal.
- **The SDK already owns the four pillars** the framework builds on: the ContextVar-interceptor pattern (`outputs`/`interceptors`), the OTel→Prometheus + Segment/Mixpanel stack (`observability/`), the object-store API (`storage/ops`), and the SIGTERM-flush path. No new infra.
- **Shared identity hash by construction.** The publish-app already depends on the SDK, so it imports the *same* `components_hash` — eliminating connector↔publish key drift at the source (the single hardest correctness risk).
- **Versioned, governed taxonomy** in one place; new connectors extend, not fork.
- Consistent with existing SDK ADRs (per-app handlers/workers/observability) — cross-cutting capabilities live in the SDK, connector-specific policy lives in the connector.

**Cons:**
- **SDK release cadence couples to framework changes.** Mitigated: per-connector reason registries live in the connector repo, so taxonomy churn doesn't force an SDK release; only core-engine changes do.
- Adds surface area to the SDK. Mitigated: additive, isolated package; zero impact on existing modules; NoOp default.
- Connectors must bump the SDK version to adopt — acceptable; they bump regularly.

### Option A2: Per-connector (each connector copies/owns its tracker)

**Pros:**
- Maximum connector autonomy; no SDK coupling; each can diverge freely.
- Fastest to land for the *first* connector.

**Cons:**
- **N-way duplication** of the tracker, schemas, taxonomy, and the hard distributed/ARS logic — exactly the M1→M2 problem we are chartered to eliminate.
- **Guaranteed drift**: coverage math, artifact schema, and especially the ARS identity hash would diverge across repos → the connector↔publish stitch and the cross-connector Mixpanel dashboard break.
- Every bug fix and every new invariant must be applied N times. Rejected.

### Option A3: Standalone shared package (separate internal/PyPI lib)

A dedicated `atlan-lineage-observability` package imported by the SDK, connectors, and publish-app.

**Pros:**
- Single implementation (like A1) with an **independent release cadence**, decoupled from SDK releases.
- Clear ownership boundary.

**Cons:**
- **New repo + CI + publishing + version-matrix** overhead for a library that depends on SDK primitives anyway (object store, metrics, interceptors) — it would either duplicate or re-import them, re-introducing coupling without the convenience.
- **Three-way version skew** (SDK × lib × connector) is harder to reason about than two-way (SDK × connector).
- The framework is intrinsically an SDK concern (it hooks SDK activities and SDK observability). Splitting it out is ceremony without payoff. Deferred — can be extracted later if SDK-release coupling ever becomes the bottleneck.

### Option A4: Leave it in `marketplace-scripts` (argo) and import from there

**Pros:**
- Zero move cost; the code already exists and is tested there.

**Cons:**
- `marketplace-scripts` is the **argo/script world** being deprecated for AE; AE connectors should not take a runtime dependency on it.
- Carries argo-only utilities and a single-process execution model; no path to the distributed/ARS layer AE needs. Rejected.

### Option A5: In the publish-app

Because ARS resolution (and the biggest silent losses) happen in publish.

**Pros:**
- Publish is where ARS drops occur; co-locating the resolution-side instrumentation is natural.

**Cons:**
- **Misses the transform side entirely.** Half of all lineage skips die in the *connector*, before publish ever sees them. The denominator (lineage-capable assets) is only knowable at the connector.
- Publish would become a dependency of every connector's coverage story — wrong direction.
- The publish-side instrumentation is one *consumer* of the shared framework, not its home. Rejected (publish imports the SDK framework instead — the chosen design).

### Option A6: Dedicated observability microservice / sidecar

**Pros:**
- Fully decoupled; could aggregate across runs centrally.

**Cons:**
- **Massive over-engineering** for what is fundamentally per-run bookkeeping + an object-store artifact + one metric emit.
- New service to run, secure, scale, and pay for; adds a network hop and a failure mode into the lineage path (violates "never break lineage"). Rejected.

## Options Considered — Decision B: How the in-activity engine is built

### Option B1: Verbatim port of the argo tracker (Chosen)

Lift `tracker.py` / `noop_tracker.py` / the reason taxonomy into the SDK unchanged (imports re-rooted), keep `build_output` byte-identical, and run the **argo test suite as a regression lock**. New AE behavior wraps the engine; it does not modify it.

**Pros:**
- **De-risks the load-bearing math.** The miss-cancellation, coverage formulas, and per-asset state machine are subtle and already correct at production scale; re-deriving them invites regressions for zero product gain.
- **Byte-identical output** means the existing Tableau Mixpanel dashboard, debugging guide, and reason taxonomy keep working — the M1 investment carries forward.
- **Behavior is provably preserved** — the ported argo suite passes unchanged (a true regression gate, not a re-spec).
- Smallest, most reviewable first PR; the novel risk is concentrated in the *new* layers (merge, stitch) where review attention belongs.

**Cons:**
- The argo API is **imperative** (`register_asset` / `mark_*` / `record_missing_reason`) and does not natively fit **daft/columnar** transforms (Looker) — handled by a thin columnar adapter that produces the same artifacts (a wrapper, not an engine change).
- Carries **single-threaded assumptions** — explicitly *not* used for cross-activity aggregation (each activity owns a tracker; a separate reduce merges partials).
- Inherits minor argo legacy (naming, an imperative call contract). Accepted as the cost of a proven core; the wrapper layer modernizes the ergonomics.

### Option B2: Clean-room rewrite / AE-native redesign

A new tracker designed for AE from scratch (e.g. immutable/event-sourced, daft-first).

**Pros:**
- Could be cleaner and DataFrame-native end to end.
- Sheds argo legacy naming and the imperative contract.

**Cons:**
- **Re-litigates solved problems** (coverage math, idempotent cancellation, taxonomy) → high regression risk, no product upside.
- **Throws away the M1 regression suite** as a safety net and the byte-compatible dashboard.
- Larger, riskier first PR that mixes "new engine" risk with "new distributed layer" risk. Rejected — we want exactly one of those novelties per review.

### Option B3: DataFrame-native only (daft)

Compute coverage purely set-wise over daft DataFrames; no per-asset object tracker.

**Pros:**
- Best fit for daft transforms (Looker, SQL connectors); naturally vectorized.

**Cons:**
- **Poor fit for the imperative connectors** (Tableau/MStr/Sigma processors) whose lineage logic is row-wise Python with many decision branches — forcing them columnar is a rewrite.
- Some skip reasons are only knowable at an imperative decision point, not as a column. Rejected as the *sole* model; adopted as an **opt-in adapter** alongside the engine for daft connectors.

### Option B4: Auto-derivation only (infer coverage from emitted entities)

Scan the Process/ColumnProcess the connector emits and diff against capable assets; no explicit instrumentation.

**Pros:**
- Near-zero connector effort; coverage "for free".
- A useful safety net against forgotten annotations.

**Cons:**
- **Cannot explain *why*** lineage is missing — the core reactive value. It sees the gap, not the reason.
- **Fails for ARS**: the connector doesn't emit a resolved Process for warehouse edges (publish does), so there is nothing to scan at transform time.
- Requires the SDK to know each connector's emitted-entity layout and reverse-parse qualifiedNames — fragile cross-repo coupling. Rejected as the sole model; retained as an **opt-in supplement** (off the critical path) to catch uninstrumented paths.

### Option B5: Adopt an OSS lineage standard (OpenLineage / Marquez / OTel)

**Pros:**
- Industry-standard schema; external tooling/ecosystem.

**Cons:**
- OpenLineage models **lineage events**, not **coverage-of-expected-lineage with skip reasons** — a different problem; we'd bolt our reason taxonomy on anyway.
- New runtime dependency and an emit pipeline for a need already met by the SDK's metrics + object-store stack.
- Discards the proven argo engine and byte-compatible M1 dashboard. Rejected for this internal coverage use case (orthogonal to whether Atlan ingests OpenLineage from customers).

## Consequences

- **Additive, low-risk first step.** PR-1 adds the package with no changes to existing modules; the framework is inert until a connector wires it up. Default-ON is a low-cost (misses-only) mode; NoOp is the zero-cost kill switch; all observability work is fail-open.
- **The hard novelty is isolated** to the new layers (distributed merge, ARS stitch, telemetry), shipped in subsequent PRs (PR-4/6/2/8) where review should focus — not in the engine.
- **Connector onboarding is small:** declare a reason registry + should-have-lineage predicates, annotate skip sites, wire a finalize step. The engine, schemas, identity hash, interceptor, and emission come from the SDK.
- **Connector↔publish stitch is safe by construction** because both sides import the SDK's single `components_hash` (versioned via `IDENTITY_SCHEMA_VERSION`, case-/order-normalized to the resolver's JOIN).
- **SDK release coupling** for core-engine changes is accepted; connector-specific taxonomy lives in connector repos to limit that coupling.
- **Migration:** the argo Tableau tracker and the ported-but-disabled AE Tableau tracker both keep working (byte-stable). Tableau's `disable_lineage_observability` flag is flipped via a contract flag in a later PR.

## Alternatives considered (summary)

| Axis | Option | Verdict |
|---|---|---|
| Placement | Central in application-sdk | **Chosen** |
| Placement | Per-connector copies | Rejected — N-way duplication + drift |
| Placement | Standalone shared package | Deferred — ceremony without payoff now |
| Placement | Stay in marketplace-scripts (argo) | Rejected — deprecated world |
| Placement | In publish-app | Rejected — misses the transform side; publish is a consumer |
| Placement | Dedicated service/sidecar | Rejected — over-engineering; adds failure mode |
| Engine | Verbatim argo port | **Chosen** |
| Engine | Clean-room rewrite | Rejected — re-litigates solved math |
| Engine | DataFrame-only | Adopted as opt-in adapter, not sole model |
| Engine | Auto-derivation only | Adopted as opt-in supplement, not sole model |
| Engine | OpenLineage/OTel standard | Rejected for this coverage use case |

## References
- Design: `research/01-understanding.md` (instrumentation map), `research/02-architecture.md` (full architecture + 10-PR plan) in the lineage-observability workspace.
- Related: [ADR-0012](0012-observability-consolidation.md) (observability stack), [ADR-0013](0013-error-hierarchy-and-failure-taxonomy.md) (failure taxonomy — distinct from the lineage `ReasonCategory`), [ADR-0014](0014-two-store-storage-architecture.md) (object-store substrate for partials).
