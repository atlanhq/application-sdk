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

`message` and `suggested_action` are **redacted where the envelope is built**, by
a `field_validator` on the model: URL userinfo of any shape
(`scheme://user:pass@host` or a bare token as the username → `scheme://***@host`;
only the Azure blob schemes' `container@account` addressing is left alone, and
only while no password is present) and secret-named query or DSN parameters
(`password=`, `api_key=`, `pwd=`, … → `***`). A consumer must not expect raw credential text in either
field, and must not rely on either as a stable identifier — the same handler
line can arrive redacted differently if the redaction rules change. The pass is
idempotent, so an envelope replayed off the wire compares equal. `evidence` is
handled by key name instead: a secret-named key is rejected, not masked.

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
| **Written under it** | `marker.txt` (the incremental watermark, via `persist_marker`), `current-state/`, and per-app siblings such as a miner's own marker file |
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

## The S3 current-state layout, its manifest, and the incremental diff

| | |
|---|---|
| **Produced by** | `CurrentStateStore.commit()` in `application_sdk/common/incremental/state/store.py`, called by `create_current_state_snapshot()` in `state/state_writer.py`; the diff by `create_incremental_diff()` / `_write_metadata()` in `state/incremental_diff.py`, uploaded by `create_current_state_snapshot()` before the commit |
| **Layout** | Under the connection-scoped prefix (previous entry), `persistent-artifacts/apps/{app}/connection/{id}/`: **current state** at `current-state/{entity}/{stamp}--{file}.json` plus `current-state/.sdk-manifest`; **incremental diff** at `runs/{run_id}/incremental-diff/` (`INCREMENTAL_DIFF_SUBPATH_TEMPLATE`) with `table/`, `column/`, `schema/`, `database/`, `delete/table/`, `delete/column/` and `metadata.json` |
| **Shape** | `.sdk-manifest` is JSON: `version`, `run_id`, `committed_at`, `keys` → size. `{stamp}` is 12 hex characters derived from the committing run ID. After the manifest is written, the commit prunes every key the manifest does not name — the snapshot it replaced, a failed run's uploads, and unstamped legacy keys — except keys with its own run's stamp, since two attempts of one run can overlap. An earlier attempt's leftovers go with the next run's commit. It then re-uploads any key the manifest names that the store no longer holds. Between a failed run and the next commit, a listing can hold stamped keys the manifest does not name — the manifest, not the listing, is the snapshot. This relies on one run per connection at a time, which scheduling guarantees. `metadata.json` is JSON with `is_incremental`, `tables_created`, `tables_updated`, `tables_backfill`, `tables_deleted`, `columns_total`, `columns_deleted`, `schemas_total`, `databases_total`, `total_changed_entities`, `total_files` |
| **Read by** | **Argo publish templates** in marketplace-packages — the incremental connectors pass `current-state/` as `transformed-input-path` (marketplace-scripts' `convert_transformer_file_structure` globs it with `**/*.json` and takes the parent directory as the asset type), and `metadata.json` routes the publish (diff with entities → stream publish; no diff → batch publish; diff with zero entities → skip). **atlan-snowflake-app**, which duplicates the layout: it builds the `persistent-artifacts/apps/{app}/connection/{id}` prefix and the `current-state` subpath from its own constants rather than from this SDK. **atlan-oracle-app**, which rebuilds the object keys under that prefix itself. The SDK's own `probe()` on the next run |
| **Pinned by** | `tests/unit/common/incremental/test_current_state_store.py` (`test_manifest_name_is_invisible_to_the_publish_glob`, the commit/prune tests, the two-run end-to-end test), the FND-3061 regressions in `test_state_lifecycle_characterization.py`, and `TestWriteMetadata` in `tests/unit/common/incremental/test_incremental_diff.py` (`test_metadata_key_set_is_the_argo_routing_contract` pins the exact `metadata.json` key set) |

Constraints that come from the readers:

- **The manifest must never match `**/*.json`.** A root `_manifest.json` would
  be parsed by the publish converter as asset records under an asset type
  named `current-state`. Hence the dot-prefixed, suffix-less `.sdk-manifest`.
- **Entity files must stay directly under `{entity}/`.** The converter reads
  the asset type from the file's parent directory, so the run stamp goes in
  the file name, never in a subdirectory. Readers must glob `{entity}/*.json`:
  file names change every commit.
- **The prefix, `current-state`, and `runs/{run_id}/incremental-diff` are
  spelled out in other repos.** atlan-snowflake-app and atlan-oracle-app do
  not import them from here, so renaming or relocating any segment strands
  those apps' state exactly as the previous entry describes for the marker —
  silently, with a full re-extraction as the only symptom. Coordinate the
  change with both apps, and with the Argo templates, before landing it.
- **A key a reader rebuilds must still exist after the prune.** An app that
  constructs a current-state key by name rather than listing the prefix will
  miss run-stamped files; that app must list `{entity}/`, not guess a name.
- **The snapshot is the manifest, not the listing.** A reader that lists or
  globs `current-state/` — the Argo publish converter, atlan-oracle-app's
  direct `download_prefix` reads — also sees stamped keys no manifest names: a
  failed commit's upload, until the next commit prunes it (see **Shape**).
  New readers go through `CurrentStateStore.probe()` / `materialize()`, which
  read only the manifest's keys. Glob readers that run after a commit, such as
  Argo publish, see only its snapshot. A glob reader that runs before the next
  commit, such as atlan-oracle-app's carry-forward reads, still sees a failed
  run's copy, and the fix for it is to read through the manifest. The old
  layout was worse on both counts: it never pruned at all.
- **A damaged manifest resets the connection to a full extraction.** If
  `.sdk-manifest` is unreadable or names a key the store does not hold, the
  template's probes treat the snapshot as absent (`DamagedManifestPolicy.TREAT_AS_ABSENT`):
  the run extracts in full, writes no diff, and its commit replaces the
  manifest and prunes every key it does not name. For readers this means a
  damaged manifest is followed by one full snapshot — and a batch publish, not
  a stream one — rather than a connection that fails every run. A failed
  listing or manifest read still fails the task, so a transient outage never
  becomes a full extraction.
- **`metadata.json` keys are routing inputs.** Adding a key is safe; renaming
  or dropping one — or writing it non-atomically — changes which publish mode
  Argo picks. It is written with `atomic_write` because a truncated counts
  block reads as zero entities and turns a stream publish into a skipped one.
- **Diff before commit.** The diff is uploaded before `CurrentStateStore.commit`
  moves the snapshot, so any committed snapshot has a durable diff behind it;
  a publish step reading both never sees a committed snapshot without its diff.

## The preflight gate's Temporal failure payload

| | |
|---|---|
| **Produced by** | `_gate_error()` and `_plumbing_error()` in `application_sdk/execution/_temporal/preflight_gate.py`, on every error that leaves the `{app}:preflight` activity, and on the block the workflow raises for a dead gate frame (`build_workflow_block()`, which routes through `_gate_error()`) |
| **Shape** | An `ApplicationError` whose `details[0]` is one `FailureDetails` (category, code, audience, retryable, message, suggested_action, evidence) and whose `details[1]` is `{"status": ..., "checks": [...], "attempt": N}`, every check in wire form. `status` is `not_ready` on every exit the gate attributes to the source, and `null` on a gate-plumbing failure, where no verdict was reached and the run proceeds. `attempt` is the activity attempt that raised. The wire `type` is `PreflightFailed` for the block, `PreflightNoVerdict` for a non-final attempt's retry marker, and the raising class name (e.g. `DependencyUnavailableError`) for a gate-plumbing failure. `details[0]` is present even when the raising leaf's own evidence cannot be serialised; the gate synthesises one rather than leave the position empty. For a verdict with no typed error, `details[0].message` is the handler's `result.message`, else every failed check's line joined with `; `, else a fixed line — the same string the raised error's message and the log rows carry |
| **Read by** | The Automation Engine, which attributes a failed run from `details[0]` of the terminal failure and of the gate activity's failure; the Temporal UI's activity pane, which renders `details[1]`; the workflow itself, which reads `attempt` and the marker's evidence off a killed frame's chain |
| **Pinned by** | `TestEveryExitCarriesFailureDetails`, `TestPlumbingPayloadNeverLosesItsPrimary` and `TestEveryGateErrorCarriesTheAttempt` in `tests/unit/execution/test_preflight_gate_classification.py` |

Every exit shares one builder on purpose. A consumer must be able to read a killed attempt's
chain and find the previous attempt's typed evidence, so the retry marker cannot carry less
than the block does; and a plumbing failure that left the activity as a bare class name gave
the reader nothing to attribute at all. The workflow parses this payload inside its own
`except`, so it reads tolerantly: a `details[0]` it cannot parse is `gate_broken` and fails
open rather than failing the workflow task, and a single check it cannot parse is dropped
from `checks` while the block stands on `details[0]`. `checks` may therefore be shorter
than the handler's list, and it is empty on the `frame_lost` block the workflow raises for
a killed attempt that left no evidence. A consumer must attribute from `details[0]` and
treat `checks` as supplementary, never index `checks[0]`.

## The preflight-results write route

The one entry here that runs the other way: the SDK is the **caller**, not the
producer. It holds another repo's address, route path and request shape.

| | |
|---|---|
| **Produced by** | `PREFLIGHT_RESULTS_ENDPOINT` in `application_sdk/constants.py`; the row is built by `build_check_result()` and sent by `post_check_result()` in `application_sdk/execution/_temporal/preflight_persist.py`, from the injected preflight gate |
| **Sent to** | `POST http://system-workflows.system-workflows-app.svc.cluster.local:8000/continuous-preflight/check-results` — the **whole URL is one constant**, never composed from a base plus a path |
| **Body** | `PreflightCheckResult`: `workflow_slug`, `origin`, `payload`, `extraction_method`, `connection_qualified_name`, `app_id`, `app_version`. Field names must match the receiver's request model exactly; `origin` and `extraction_method` are validated server-side against closed enums |
| **Read by** | The `system-workflows` app (`atlanhq/atlan-system-workflows-app`, `app/continuous_preflight/api.py`), which holds the only writer principal for the tenant's `apps.system-workflows` Iceberg namespace and derives the table's own columns from `payload` |
| **Pinned by** | `TestThePreflightResultsRouteContract` in `tests/unit/execution/test_preflight_persist.py` |

The SDK ships the **whole URL, path included**, because the route belongs to the
app that serves it: holding a base address and appending a path here would
hardcode another repo's route layout and ship it stale the day that entrypoint's
prefix changes. The host is safe to pin — the receiving app cannot be renamed
without moving the Iceberg namespace its table lives in, so its name, and
therefore its Service DNS, cannot change. The **path** carries no such
protection, which is exactly why it is written down here.

Three consequences, and the first two are silent by construction — the write is
scheduled and abandoned, its response is never recorded beyond a status code, and
nothing retries. A break costs rows, not runs, and nothing goes red:

- **Renaming the route path is a breaking change** for every already-deployed SDK
  version, not just the next release. That app's own docs describe the prefix as
  "the entrypoint's own name", a locally-chosen convention it has renamed once
  before; from this constant's first release it is frozen. Coordinate it, and
  serve both paths until the old SDK versions retire.
- **The route is unauthenticated, and this caller depends on that.**
  `post_check_result` deliberately sends no `Authorization` header — forwarding
  the run's token to a service that does not check it would widen that token's
  reach for nothing. Putting auth in front of that route therefore drops every
  row the fleet writes, with no error anywhere. It needs the header added here,
  released, and the fleet bumped **first**.
- **Nothing authenticates this write, and that is accepted for the first ship.**
  Any workload that can reach
  `system-workflows.system-workflows-app.svc.cluster.local:8000` can POST a row
  for any `workflow_slug`, `app_id` and verdict — every field that attributes a
  row is asserted by the caller — and because the write is abandoned, neither side
  can tell a forged row from a real one afterwards.

  What bounds it: the address is a cluster-internal Service DNS name, so the trust
  boundary is "any workload already running inside the tenant's cluster", not the
  internet; the Iceberg writer principal stays in the receiving app; and the
  `payload` narrowing above means no row carries handler-authored text either way.
  What does **not** bound it, despite being the obvious candidate: this repo
  neither owns nor asserts a `NetworkPolicy` in front of that Service. If one is
  wanted it belongs to the receiving app's chart, and this SDK would not notice
  its absence — so do not read this entry as a record that one exists.

  What that makes the rows worth: unauthenticated, self-asserted observability
  data, good for counting and trending preflight outcomes across the fleet, which
  is what they exist for. Nothing that has to be *trustworthy* should be built on
  them — billing, access decisions, or anything a customer sees as authoritative.

  What would change the answer: the table starts feeding a decision that must be
  trustworthy, the receiver becomes reachable beyond the cluster, or a tenant
  begins hosting workloads that are not first-party. The fix is then the one the
  SDK cannot make alone — workload identity, or a short-lived service token bound
  to `app_id` — agreed with the `system-workflows` owners and rolled out in the
  order the bullet above demands: header added here, released, fleet bumped,
  *then* enforced at the receiver.
- **A warmup app writes two rows per run, told apart by per-check `tier`.**
  An app that declares `App.preflight_warmup_ceiling_seconds` dispatches the
  gate once per tier, and each dispatch persists its own verdict over only that
  tier's checks. `payload.preflight.checks[].tier` (`fast` / `warmup`) is
  therefore on the wire — but only for a check whose handler set a tier, so an
  untiered app's payload is byte-for-byte what it was. A reader that counts runs
  must group on `workflow_slug` and the run, not count rows. The warmup phase's
  own outcome, duration and observed transitions are deliberately **not** sent:
  they describe the gate's wait, not the verdict, and live on the
  `Preflight gate outcome` log row (`warmup_outcome`, `warmup_duration_ms`,
  `warmup_transitions`). Adding `tier` relies on the receiver tolerating keys
  it does not derive a column from inside `payload` — the relay contract this
  entry already assumes for `payload`, but not separately verified against the
  receiver here.
- **The two enum vocabularies must stay in step.** `PreflightResultOrigin` and
  `ExtractionMethod` are validated against the receiver's own enums; a value it
  does not accept is a 422 and a dropped row, visible only as one WARNING
  carrying a status code. Adding a member on either side is additive; renaming
  one is not.

## The streaming event-trigger contract (`trigger_config` keys ↔ `$.event.*` args)

This one runs **both ways**, which is why it is here rather than only in the
Automation Engine: the toolkit produces the trigger config AE reads, and AE
produces the event shape an app's DAG reads through paths the toolkit renders.

| | |
|---|---|
| **Produced by (toolkit → AE)** | The `triggers.events[].trigger_config` block rendered by `contract-toolkit/src/App.pkl` and `NativeApp.pkl` from `EventTriggerConfig` |
| **Produced by (AE → app)** | `event_context` in `automation_engine/workflows/streaming_batch.py`, constructed there and nowhere else; `workflows/executor.py` forwards it unchanged into `$.event.*` |
| **Key (toolkit → AE)** | `streaming_enabled`, `ack_paths`; `max_retries` is deliberately **not** rendered under streaming |
| **Key (AE → app)** | Exactly four: `batch_key` (always set), `batch` (the events inline, or `null` above AE's inline cap), `event_count`, `topic` |
| **Read by** | The Automation Engine, which registers the trigger and picks a dispatch shell from `streaming_enabled`; every streaming consumer app, whose extract-node args resolve `$.event.batch` / `$.event.batch_key` |
| **Pinned by** | `contract-toolkit/tests/streaming_trigger_config_test.pkl` (render shape, both schemas, and every refusal) and the streaming section of `contract-toolkit/scripts/check-invariants.sh` (the eval-failure cases facts cannot express) |
| **Owner (AE side)** | Anurag Badoni — change `event_context` or the `TriggerConfig` keys through this entry |
| **Design record** | DISTR-973 |

Both directions fail **silently** by default, which is what makes this worth an
entry rather than a comment:

- **AE ignores unknown `trigger_config` keys.** Pydantic drops what it does not
  model, so a toolkit-side key AE has not implemented registers as a trigger
  with that key absent — for `streaming_enabled` that means a contract reading
  as streaming and running as batch, with nothing logged. Ship the AE side
  first, always.
- **A `$.event.*` path AE does not send fails the node** with `did not match
  any value` at run time, not at render time. Adding a key to `event_context`
  is safe; renaming or removing one breaks every DAG wired to it, and the
  toolkit cannot catch it because the path is a string it renders faithfully.
- **`batch` is permanent, but it is not the contract.** `batch_key` is always
  set — AE writes the object before deciding whether the batch fits inline.
  `batch` is present-but-`null` above the cap, deliberately: an *absent* key
  raises `did not match any value`, while a null one lets the consumer fall
  through to the key. A DAG that reads `batch` alone applies nothing on the
  first over-cap batch and reports success, and Kafka was acked when the run
  started. Handle `batch_key`; treat `batch` as an optimisation.
- **An SDK app receives only `batch_key`.** The generated input model declares
  `batch_key` and not `batch`, and the SDK's `Input` drops undeclared keys. The
  `{id, topic, data}` envelope has no typed contract, and an untyped list of
  dicts fails the SDK's payload-safety check, so `batch` stays undeclared until
  the envelope gets a real type definition. Giving it one, on either side, is
  a change to this entry.
- **`ack_paths` renders under streaming even though it is inert there**, because
  AE's `_validate_event_ack_paths` rejects an event trigger with a falsy value.
  `[""]` is AE's fire-and-forget form. Suppressing it needs the AE change
  shipped first — the same ordering as the first bullet.
