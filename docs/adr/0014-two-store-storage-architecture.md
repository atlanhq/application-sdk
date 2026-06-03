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

### App.upload() — routes through upstream_storage when present

`App.upload()` uses `upstream_storage or storage`:

```python
store = self.context.upstream_storage or self.context.storage
```

In SDR deployments `upstream_storage` is the `atlan-objectstore` S3 binding, so
`App.upload()` routes to Atlan's bucket. In local dev and Atlan-hosted
deployments `upstream_storage` is `None` and `App.upload()` falls back to the
deployment store.

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
  `upstream_storage` is `None` and `App.upload()` silently falls back to the
  deployment store. Local dev and integration tests work without any special
  configuration.

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
- `application_sdk.app.base.App.upload()` — routes through `upstream_storage or storage`
- `application_sdk.execution._temporal.activities` — interceptor persist step uses `infra.storage`
- [ADR-0007: Apps as the Unit of Inter-App Coordination](0007-apps-as-coordination-unit.md)
- [docs/concepts/file-reference.md](../concepts/file-reference.md)
- [docs/concepts/storage.md](../concepts/storage.md)
