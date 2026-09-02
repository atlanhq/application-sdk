# Cross-Repo Contracts

Some values this SDK produces are read by code that does not live here — the
Automation Engine, the runtime scenario suite, Heracles. For those, "the tests
pass" is not the whole bar: a rename or a relocation can be locally invisible
and still red another repo's suite, or worse, silently mis-route production
work.

Each entry below names one such value, what reads it, and the test in this repo
that pins it. **Before changing anything on this list, read the entry.** If you
are adding a value that another repo will read, add an entry and a pinning test
with it.

## The typed failure envelope (`FailureDetails`)

| | |
|---|---|
| **Produced by** | `AppError.to_failure_details()` in `application_sdk/errors/base.py`, wrapped into `ApplicationError(details=…)` by `_to_application_error()` in `application_sdk/execution/_temporal/activities.py` |
| **Served at** | `ApplicationError.details[0]` on every failed activity, plus `non_retryable` on the error itself |
| **Key** | `category`, `code`, `retryable`, `audience` at the top level; per-error context under `evidence` |
| **Read by** | The Automation Engine, which attributes a failed run from these fields instead of parsing exception strings; connector-pulse, which buckets runs by `failure_category` / `failure_code` for the failure boards |
| **Pinned by** | `TestFailureEnvelopeWireContract` in `tests/unit/errors/test_wire_contract.py` |

The envelope is the whole point of the typed-error hierarchy: a consumer is
supposed to branch on a field rather than regex a message. That makes the field
*names*, the enum *spellings*, and the category-to-code relationship a contract,
not an implementation detail.

What this means in practice:

- **`category` is coarse; `code` is the specific cause.** A consumer that keys a
  customer-facing attribution on `category` alone will mis-attribute the moment
  any app adds a leaf in that category — and every category already has several.
  This is not hypothetical; it has happened. If you need a `code` that does not
  exist yet, add one here rather than inferring it from the category downstream.
- **Renaming a field, or changing an enum's spelling, is a breaking change** for
  a consumer that cannot vote in this repo's CI. `FailureCategory` and
  `Audience` serialise by member *name*, so renaming a member is a wire change
  even though it looks like a local refactor.
- **`evidence` is per-`service`, not comparable across producers.** Two raise
  sites may report the same `code` and still populate `evidence` differently —
  the object-store write path sets `target` to a `scheme://bucket/key` URI,
  while the boot-time preflight gate sets it to the Dapr binding name for the
  same condition. Both are "what we were talking to" for their own producer.
  A consumer may group by `code` and read `evidence` for context, but must not
  assume a key holds the same *kind* of value across every producer of that
  code. If you need one that does, say so here and make it so at both sites.
- **Adding an `evidence` key is safe; repurposing one is not.** Evidence is the
  producing dataclass's fields, so a new field appears automatically — but a
  field that changes meaning silently changes what a consumer reads. Note that
  `evidence` keys are also gated by the secret-name denylist in
  `errors/wire.py`, which rejects the envelope outright rather than dropping the
  key.
- **`retryable` is not advisory.** It becomes `non_retryable=not
  effective_retryable` on the `ApplicationError`, so it *is* Temporal's retry
  decision. Flipping it for an existing failure class changes production retry
  behaviour, not just a dashboard label — coordinate it before landing.

## The served manifest's resolved `task_queue`

| | |
|---|---|
| **Produced by** | `resolve_manifest_tokens()` in `application_sdk/common/task_queue.py`, applied by the manifest route in `application_sdk/handler/service.py` |
| **Served at** | `GET`/`POST /workflows/v1/manifest` (and the deprecated unversioned `/manifest` alias) |
| **Key** | `task_queue`, at `dag.<node>.task_queue`, `dag.<node>.inputs.task_queue`, and top level on single-node manifests |
| **Read by** | The Automation Engine, which writes it into the DAG and submits work to it; from FND-224, the runtime scenario suite's contract tier, which diffs it against the TWD trigger, the KEDA metadata and the rerouter's formula |
| **Pinned by** | `TestServedManifestTaskQueueContract` in `tests/unit/handler/test_service.py` |

The value is **stamped, not derived**: the route hands `resolve_manifest_tokens`
the queue this process was configured with — the same value `create_worker`
receives — and that value is copied into the manifest verbatim. It is
deliberately not re-derived from the environment at serve time, because an
explicit `ATLAN_TASK_QUEUE` / `--task-queue` override is not reproducible by
re-derivation. Two paths deriving the same answer is a convention that holds
until someone's inputs differ; one path copying the other's answer is
structural.

That is the FND-195 fix, and the failure it removed is silent by construction:
when the served queue and the polled queue disagree, nothing errors. AE submits
to one queue, the worker polls another, and the run sits unclaimed until its 24h
heartbeat backstop (CONNECT-183; the same gap stripped failure attribution in
HYP-1954).

What this means in practice for a change to `task_queue.py` or the manifest
route:

- **Renaming the key, or the path it sits at, is a breaking change** for a
  consumer that cannot vote in this repo's CI. It needs coordinating, not just
  a passing test run.
- **Reverting to token substitution** — filling `{app_name}` /
  `{deployment_name}` in place rather than replacing the whole template —
  reintroduces the original divergence. The module docstring in
  `task_queue.py` explains why at length; read it before proposing it again.
- **The unset case must stay loud.** A missing app name resolves to `None` and
  leaves the literal `{app_name}` token visible, rather than manufacturing
  `atlan-default-<deployment>`, which reads as a legitimate queue and hangs the
  run silently.

## The connection-scoped `persistent-artifacts` layout

| | |
|---|---|
| **Produced by** | `get_persistent_s3_prefix()` in `application_sdk/common/incremental/helpers.py`, from `PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE` in `application_sdk/constants.py`; local counterpart `get_persistent_artifacts_path()` |
| **Layout** | `persistent-artifacts/apps/{application_name}/connection/{connection_id}/`, where `connection_id` is the **last** segment of `connection_qualified_name` |
| **Written under it** | `marker.txt` (the incremental watermark, via `persist_marker_to_storage`), `current-state/`, and per-app siblings such as a miner's own marker file |
| **Read by** | Every connector app doing incremental extraction — the crawler and the miner of the same connection both key off this prefix, in separate repos, and must agree; the object store retains it across runs, so past runs read what past SDK versions wrote |
| **Pinned by** | `TestExtractEpochId` and `TestGetPersistentS3Prefix` in `tests/unit/common/incremental/test_helpers.py`; conformance `P048`/`P049` enforce that apps derive it from here rather than re-deriving it |

This prefix is **state, not just a path**. It is the address of a watermark that
outlives the run that wrote it, so a change to how it is derived does not fail —
it silently relocates every existing connection's marker. The next run finds no
marker at the new address, treats itself as a first run, and re-extracts from
the beginning; the old marker is orphaned where nothing will look for it again.
Nothing errors, and the only visible symptom is a full extraction where an
incremental one was expected.

Two consequences for changes here:

- **Changing the segment choice, the template, or the `application_name`
  fallback is a data migration**, not a refactor. Existing markers live at the
  old address. `ATLAN_APPLICATION_NAME` is not set in every app's `atlan.yaml`,
  so apps that pass `application_name` explicitly and apps that rely on the
  fallback resolve different directories for the same connection — aligning
  those two is exactly such a migration and needs its own plan.
- **A non-epoch last segment must keep warning rather than raising.**
  Connections named after a workflow (`default/oracle/some-name`) are produced
  by tenants that provision programmatically; they crawl normally, so failing
  them here would fail one leg of a connection whose other leg works. An app
  that re-derives the segment and raises reintroduces CONNECT-1136, where a
  miner rejected names its own crawler accepted and one tenant's query lineage
  went missing with every test green. The one rejected case is an *empty* last
  segment, which is not a name and would collapse every such connection onto a
  single shared directory.
