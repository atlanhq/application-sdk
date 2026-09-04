# ADR-0021: Quarantined Storage (sensitivity orthogonal to lifecycle)

## Status
**Accepted** (2026-09-04 — Linear [REQ-1635](https://linear.app/atlan-epd/issue/REQ-1635),
[REQ-1636](https://linear.app/atlan-epd/issue/REQ-1636))

## Context

Connectors have started downloading **raw files straight from the source system** into
object storage, so that downstream apps can read the original artifact rather than the
metadata extracted from it. The Tableau and PowerBI connectors do this today (workbook and
datasource definitions); more ECL use-cases are expected to need raw source context.

Raw source files are a different kind of data from anything the SDK previously stored. A
connector's own artifacts are metadata *it produced*; a raw source file is content the
customer authored, and it routinely carries things nobody enumerated in advance —
warehouse hostnames, database and schema references, usernames, authoring filesystem
paths, and literal filter values embedded in the file. Inspecting each file to decide how
sensitive it is does not scale and fails open on the first shape nobody predicted.

The existing `StorageTier` (`TRANSIENT` / `RETAINED` / `PERSISTENT`) was designed for apps
consuming files internally or handing them to each other. Reusing those prefixes for raw
source content puts data with a materially different security posture in the same
locations as ordinary artifacts, which makes "who may read this, and for how long" a
per-connector question rather than a platform one.

Without a standard, each connector picks its own prefix. That is already happening — one
connector writes to a hand-chosen path unrelated to any tier — and every such choice is a
governance decision made by whoever wrote the upload call.

## Decision

Add a **`quarantined` boolean, orthogonal to `StorageTier`**, that routes data under a
dedicated top-level prefix.

### Sensitivity is not a tier

Tier expresses **lifecycle**: how long the data should live and when cleanup reclaims it.
Sensitivity expresses **what kind of data it is**. They are independent — raw source
content can legitimately be transient (needed for one run), retained (kept for
investigation), or persistent (needed across runs). Modelling sensitivity as a fourth tier
would force a wrong coupling, making every quarantined object share one lifecycle.

So `quarantined` is a flag *in addition to* the tier, and quarantined data exists at all
three lifecycles.

### One root, per-tier sub-prefixes beneath it

When `quarantined=True`, the tier's ordinary prefix is rooted under
`QUARANTINE_PREFIX` (default `quarantine`, settable via `ATLAN_QUARANTINE_PREFIX`):

| Tier | Default | Quarantined |
|---|---|---|
| `TRANSIENT` | `file_refs/…` | `quarantine/file_refs/…` |
| `RETAINED` | `artifacts/apps/{app}/workflows/{wf}/{run}/…` | `quarantine/artifacts/apps/{app}/workflows/{wf}/{run}/…` |
| `PERSISTENT` | `persistent-artifacts/apps/{app}/…` | `quarantine/persistent-artifacts/apps/{app}/…` |

Each tier therefore gets its own default location under a single root. Prepending rather
than inventing a flat layout is deliberate: run-scoping, app-scoping, and per-tier cleanup
keep working unchanged, and one access/retention policy on the root covers everything
beneath it.

`StorageTier` remains the single source of truth for path generation
(`upload_prefix`, `_file_ref_base`); the quarantine root is applied there, so every caller
inherits it.

### Opt-in

`quarantined` defaults to `False` on `FileReference` and `UploadInput`. Every existing
storage key is unchanged. Making it default-on would relocate the artifacts of every
connector already in production, which is a breaking change no consumer asked for.

The tradeoff is real and stated plainly under Consequences.

### `quarantined=True` and an explicit `storage_path` are mutually exclusive

`UploadInput.storage_path` is used verbatim and bypasses tier resolution entirely, so the
combination reads as "quarantine this" while writing an ordinary key. That silent
non-quarantined write is precisely the failure the flag exists to prevent, so the contract
rejects the pair. Callers place files within the resolved prefix using `storage_subdir`.

### Layout beneath the root belongs to the app

The SDK owns the root and the per-tier sub-prefix. How an app organises files below that is
its own choice.

One caution learned from a connector that built this by hand: a **connection qualified name
is a poor path segment**. It contains `/` separators, so using it as a single segment nests
the prefix at an unexpected depth and breaks prefix-scoped operations such as
`delete_prefix`. Prefer a flat, separator-free identifier.

### What this does not do

Quarantine is a **location and a routing guarantee**, not redaction. The bytes still exist
at rest. For a source whose raw payload contains live credentials, quarantine is necessary
but not sufficient — such a payload should not be stored at all. The restricted IAM,
encryption, and retention policy that make the root meaningfully secure attach to the
prefix and are provisioned outside the SDK.

## Consequences

### Positive

- **Sensitive-by-default without content inspection.** An app declares that a write is raw
  source content; it does not have to classify what is inside the file, and no allowlist
  has to keep pace with the next unanticipated field.
- **Governance becomes a platform lever.** Access, encryption, and retention are set once on
  one prefix rather than negotiated per connector.
- **Lifecycle still applies.** Quarantined data can be short-lived; a conservative
  deployment can set an aggressive retention policy on the quarantine root without
  affecting ordinary artifacts.
- **No migration.** Opt-in means existing connectors are byte-identical.
- **Small adoption cost.** A connector already using `App.upload` moves from a verbatim key
  to `tier=` + `quarantined=True` + `storage_subdir=`.

### Negative / Tradeoffs

- **Opt-in is fail-open.** A new raw-from-source write lands in an ordinary prefix if its
  author does not set the flag. Opt-in was chosen to avoid breaking existing connectors;
  the residual risk is that quarantine protects only the writes that remember to ask for
  it. A conformance rule flagging un-quarantined source-download writes is the natural
  follow-up once adoption exists.
- **Upstream writes need a proxy allowlist entry.** Atlan's blob-storage proxy enforces an
  allowlist of permitted top-level prefixes and rejects anything outside it with `403`
  (code `1009`). Today that allowlist admits `artifacts/` and `persistent-artifacts/`.
  Until the quarantine root is added, quarantined writes succeed against a deployment-owned
  store but fail against the upstream store. `ATLAN_QUARANTINE_PREFIX` exists so a
  deployment can align the root with its own policy without an SDK release.
- **Quarantine is not redaction.** See above — it shrinks blast radius, it does not make
  storing a secret safe.
- **Two roots to reason about in cleanup.** `cleanup_storage` must sweep both the run
  prefix and its quarantined twin, and `PROTECTED_STORAGE_PREFIXES` needs an explicit entry
  for `quarantine/persistent-artifacts/` because it does not start with
  `persistent-artifacts/`. Both are handled; a future root-relative rewrite of these checks
  would remove the duplication.

## Related

- `application_sdk.constants.QUARANTINE_PREFIX` — the root (env: `ATLAN_QUARANTINE_PREFIX`)
- `application_sdk.constants.PROTECTED_STORAGE_PREFIXES` — includes the quarantined persistent root
- `application_sdk.contracts.types.StorageTier.upload_prefix` — where the root is applied
- `application_sdk.contracts.types.FileReference.quarantined`
- `application_sdk.contracts.storage.UploadInput.quarantined`
- `application_sdk.app.base.App.cleanup_storage` — sweeps both run prefixes
- [ADR-0014: Two-Store Storage Architecture](0014-two-store-storage-architecture.md)
- [docs/concepts/storage.md](../concepts/storage.md)
- [docs/concepts/file-reference.md](../concepts/file-reference.md)
