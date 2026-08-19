# ADR-0014: Two-Store Storage Architecture

## Status
**Accepted**

## Context

When a connector runs in Self-Deployed Runtime (SDR) mode it operates inside
the customer's own infrastructure — on their Kubernetes cluster, behind their
firewall, potentially writing to their own object store. Atlan's system apps
(publish, query-intelligence, lineage-app, lineage-publish) run in Atlan's
managed cloud and can only read artifacts from Atlan's own S3-compatible
blobstorage proxy (`/api/blobstorage`).

Before this architecture existed there was a single Dapr component named
`objectstore`. When a connector needed to hand extracted artifacts to the
publish app it had to write to a path that Atlan's infrastructure could reach.
This created a coupling problem: either the customer had to open their internal
object store to Atlan's publish workers, or every SDR deployment had to be
pre-configured to write directly to Atlan's S3. Both options are security and
operational antipatterns, and neither composably supports customers who want to
keep all intermediate pipeline data inside their own perimeter.

Within a single connector run there is also a durability requirement that is
entirely local to the customer's deployment: a `@task` on worker pod A may
produce a large file that another `@task` on pod B needs as input. This
transfer has nothing to do with Atlan's infrastructure — it is intra-deployment
inter-task communication. Forcing it through Atlan's S3 would add latency,
incur unnecessary cross-boundary transfer costs, and unnecessarily expose
customer data to Atlan's storage.

These are two fundamentally different data flows:

1. **Task-to-task**: within a single run, between activities on the same or
   different pods of *the same deployment*. The storage is deployment-owned
   and can be any backend the customer controls.

2. **App-to-app**: from the connector's extract activity to Atlan's system apps
   (publish, QI, lineage). The storage must be Atlan-owned because only Atlan's
   infrastructure can access it.

## Decision

Introduce two distinct Dapr objectstore components and two matching store
references in `InfrastructureContext`:

| Component name | SDK reference | Owner | Purpose |
|----------------|--------------|-------|---------|
| `objectstore` | `infra.storage` (`DEPLOYMENT_OBJECT_STORE_NAME`) | Customer / deployment | Task-to-task `FileReference` durability within a run |
| `atlan-objectstore` | `infra.upstream_storage` (`UPSTREAM_OBJECT_STORE_NAME`) | Atlan | Final artifact hand-off to Atlan system apps |

The `atlan-objectstore` component is provisioned by the atlan-configurator at
SDR deploy time and points to `{tenant}/api/blobstorage` with the deployment's
OAuth client credentials for SigV4 signing. In non-SDR deployments (local dev,
Atlan-hosted) the component is absent and `upstream_storage` is `None`.

### Credential resolution — `auth.secretStore` and env vars (BLDX-1619)

A component may supply its credentials as plain `value` entries, or as
`secretKeyRef` entries backed by an `auth.secretStore`. The Dapr sidecar
resolves both; the SDK builds its own obstore store from the same YAML and must
match. `main.py` runs after `wait_for_dapr_sidecar()`, so it reads the
component's `auth.secretStore`, fetches those secrets, and passes them into the
(synchronous, public) resolver as `secrets=`. Environment variables remain the
fallback — that is what `secretstores.local.env` resolves to in Docker Compose
and SDR-local runs.

Before this, the resolver read env vars only. On the k8s SDR secure path the
component uses `secretKeyRef` **and** the matching env vars are deliberately
absent, so the resolver saw an unresolvable component, treated it as absent, and
left `upstream_storage` as `None` while the sidecar binding worked — a silent
write to the wrong bucket.

### Activity interceptor — always uses `infra.storage`

The activity interceptor's persist step automatically uploads every ephemeral
`FileReference` returned from a `@task` to `infra.storage`. This is intentional
and must not change:

- `infra.storage` is the SDR-controlled deployment store. Using it for intra-run
  transfers keeps all intermediate data inside the customer's perimeter.
- In Atlan-hosted deployments there is no `atlan-objectstore`; the interceptor
  must work with a single store.
- The interceptor upload is fire-and-forget plumbing, not a semantic hand-off.
  Routing it through Atlan's S3 would couple every task output to Atlan's
  availability and access model, even for data that never needs to leave the
  deployment.

### App.upload() — dual-write when both stores are configured (BLDX-1464)

In SDR deployments, `App.upload()` writes artifacts to **both** stores at the
identical key/prefix — deployment (customer) store first, then upstream (Atlan) —
matching the documented flow "extracted metadata is first written to configured
storage … then transferred to Atlan SaaS":

```
deployment store (objectstore)   ← written first; retained audit copy
upstream store (atlan-objectstore) ← written second; authoritative handoff to publish app
```

The destination key is **identical** in both stores because it is derived purely
from the tier, run prefix, and app name — not from the store itself (see
`StorageTier.upload_prefix`, `storage/transfer._derive_target_key`). The
returned `UploadOutput` reflects the upstream write; because keys are identical,
the `FileReference` it carries is valid for reading from either store.

One tri-state flag controls the behaviour (see `docs/configuration.md`, `## Storage`):

| `ATLAN_DEPLOYMENT_ARTIFACT_DUAL_WRITE` | Meaning |
|---|---|
| `best_effort` (default) | Dual-write enabled; deployment failure logs `WARNING`, run succeeds. |
| `required` | Dual-write enabled; deployment failure fails the run after upstream completes. |
| `disabled` | Upstream-only write (pre-BLDX-1464 behaviour). |

When `ATLAN_DEPLOYMENT_ARTIFACT_DUAL_WRITE=disabled`, or in non-SDR deployments where
`upstream_storage` is `None`, `App.upload()` writes to a single store as before.

The deployment-write failure is **never** allowed to suppress the upstream
write — the upstream write (Atlan handoff) always runs even if the customer-bucket
mirror failed, so a copy lands somewhere regardless.

#### Ref-only uploads: the deployment leg copies within its own store (FND-536)

When the artifacts are **already in the deployment store** and no local copy
exists on the uploading pod — a cross-pod / KEDA-scaled hand-off, or a caller
passing only `UploadInput.ref` — both legs take `transfer.upload`'s
deployment-store fallback branch. Every leg is therefore handed
`_source_store=self.context.storage`, including the deployment leg itself, so
the deployment write is a copy *within* the deployment store from the ref's
prefix to the destination key. Two shapes follow:

- **destination pinned to the ref's own prefix** (key-preserving, as the
  hand-rolled `upload_to_atlan` bridges do): source and target key are the same
  object, so `_upload_from_store` returns immediately with
  `reason="skipped:same_object"` — no bytes, and not even the two sidecar GETs
  the cross-store SHA-256 dedup would cost. "Already satisfied" is conditional on
  the object being there: keys that came from a listing of the source store are
  proven present, and a caller-supplied `ref` costs one HEAD, so a stale `ref`
  pinned to a key that was never written still fails with
  `StorageNotFoundError` rather than buying a durable-looking success;
- **destination not pinned**: a real copy runs, which is what keeps the
  identical-key invariant above true for the ref-only case.

Withholding the source store from the deployment leg (pre-FND-536) made a
deployment→deployment copy inexpressible: the leg fell through to `StorageError
("local_path does not exist …")`, which is a spurious `WARNING` under
`best_effort` and a **failed run** under `required`, even though the upstream
leg had done its job.

### Connector responsibility

Connectors that hand artifacts to Atlan system apps **must** call `App.upload()`
explicitly. Relying on the activity interceptor is not sufficient — the
interceptor writes to `infra.storage` (deployment-owned), which the publish app
cannot access in SDR deployments.

The typical pattern in a SQL connector's `run()`:

```python
async def run(self, input: ExtractionInput) -> ExtractionOutput:
    base = await super().run(input)  # extract + transform → local files

    # Explicit hand-off to Atlan: upload transformed/ to atlan-objectstore (S3).
    # The activity interceptor already persisted FileReferences to infra.storage
    # for task-to-task durability; this separate upload routes through
    # upstream_storage so the publish app can read the artifacts.
    await self.upload(
        UploadInput(
            local_path=os.path.join(base.output_path, "transformed"),
            storage_path=base.transformed_data_prefix,
            raise_on_empty=True,
        )
    )
    return ExtractionOutput(transformed_data_prefix=base.transformed_data_prefix, ...)
```

## Consequences

### Positive

- **Perimeter isolation.** Intermediate pipeline data (raw SQL results, partial
  transform outputs) never leaves the customer's deployment unless the connector
  explicitly decides to hand it off.
- **Backend flexibility.** Customers can configure `objectstore` to any backend
  they already operate (their own S3 bucket, Azure Blob, GCS, even local disk in
  dev). Only the final artifact upload cares about Atlan's specific S3 endpoint.
- **Clear semantic boundary.** The two-store split makes the task-to-task vs
  app-to-app distinction visible in the code: `FileReference` + interceptor =
  intra-run, `App.upload()` = cross-system hand-off.
- **Graceful local-dev fallback.** When `atlan-objectstore` is absent
  `upstream_storage` is `None` and `App.upload()` falls back to the deployment
  store. Local dev and integration tests work without any special
  configuration. The fallback is gated on `ENABLE_ATLAN_UPLOAD` (BLDX-1619): a
  deployment that set it asked for Atlan's bucket, so `App.upload()` raises
  `UpstreamObjectStoreNotConfiguredError` instead of writing elsewhere and
  reporting a positive file count.
- **Retained audit copy (BLDX-1464).** In SDR deployments the customer's bucket
  receives a mirror copy of every metadata artifact at the identical run-scoped
  key (`artifacts/apps/{app}/workflows/{run_id}/…`). Customers can apply their
  own lifecycle/rollover policies on this prefix to manage retention.

### Negative / Tradeoffs

- **Connector authors must know the rule.** Forgetting the explicit `App.upload()`
  call produces a silent failure: all DAG nodes succeed (the interceptor
  uploaded to localstorage), but the publish app finds 0 artifacts in Atlan's
  S3 and publishes nothing. SQL connectors built on `SqlApp` should include the
  explicit upload in their `run()` override; the `SqlApp` base class will add
  this call in a future SDK release.
- **`App.upload()` misuse.** Calling `App.upload()` for task-to-task data —
  instead of `FileReference` — has three distinct harms: (1) in SDR it routes to
  Atlan's `atlan-objectstore`, polluting it with intermediate pipeline artifacts
  that the publish app treats as connector output; (2) it bypasses SHA-256 dedup —
  every call uploads the full file even if an identical file already exists in the
  store; (3) it does not wire into the SDK's cross-worker auto-materialization —
  the resulting `FileReference` will not be automatically re-downloaded if a
  downstream task lands on a different worker. The correct tool for task-to-task
  data is `FileReference` on the contract; the interceptor handles persistence and
  materialization automatically.
- **Two stores to configure.** SDR deployments need both components provisioned.
  The atlan-configurator handles this automatically; custom deployments must
  ensure `atlan-objectstore` is present if the connector hands off to Atlan
  system apps.

## Related

- `application_sdk.constants.DEPLOYMENT_OBJECT_STORE_NAME` — `"objectstore"`
- `application_sdk.constants.UPSTREAM_OBJECT_STORE_NAME` — `"atlan-objectstore"`
- `application_sdk.constants.DEPLOYMENT_ARTIFACT_DUAL_WRITE_ENABLED` — `True` when dual-write is active
- `application_sdk.constants.DEPLOYMENT_ARTIFACT_DUAL_WRITE_REQUIRED` — `True` when a deployment failure is fatal
- `application_sdk.app.base.App.upload()` — dual-write fan-out (deployment first, upstream second)
- `application_sdk.execution._temporal.activities` — interceptor persist step uses `infra.storage`
- [ADR-0007: Apps as the Unit of Inter-App Coordination](0007-apps-as-coordination-unit.md)
- [docs/concepts/file-reference.md](../concepts/file-reference.md)
- [docs/concepts/storage.md](../concepts/storage.md)
- [docs/configuration.md](../configuration.md) — `## Storage` table for env-var reference
