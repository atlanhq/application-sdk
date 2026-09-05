"""Storage-seam rule definitions (P008–P012, BLDX-1398).

The SDK owns the **storage seam**: apps move bytes only through the SDK's
abstractions, never by constructing cloud clients or object stores themselves.
Keeping the SDK as the single seam lets storage routing evolve (provider swaps,
SDR mode, interceptor changes) without breaking every app, and keeps Temporal's
payload limits and the two-store routing contract enforced in one place.

The storage contract (ADR-0014)
--------------------------------
There are **two object stores** and **two data-flow paths**:

* **task-to-task** data flows through a ``FileReference`` on the contract: a task
  writes a local file and returns ``FileReference.from_local(path, tier=...)``;
  the activity interceptor persists it to the upstream store and materialises it
  back to a local path for the consuming task.  The SDK owns the durability
  fields (``storage_path``, ``is_durable``, ``file_count``) — the app never sets
  them by hand.
* **app-to-app** data flows through ``App.upload()`` / ``App.download()`` called
  from ``run()`` (the deployment store), routed by the SDK's infrastructure
  context (``get_infrastructure().storage`` / ``.upstream_storage``).

In SDR (split-deploy/run) mode the two stores are distinct; routing data through
the wrong one breaks the extract→publish hand-off.  These rules guard that seam:
they catch transfers nested inside activities (P008), apps wiring up their own
storage (P009), hand-built durability metadata (P010), and contracts that carry
raw bytes (P011) or bare filesystem path strings (P012) instead of a durable
``FileReference``.

These are P-series (prescription) rules but live in their own module, modelled
on the orchestration-seam series.  P-ids are a permanent public contract (see
``prescriptions.py``).
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P008",
        canonical_reference=(
            "atlan-mysql-app app/mysql.py — `App.upload()` is called from `run()`, after "
            "the tasks return. A @task hands its output back as a FileReference and lets "
            "the framework move it; the transfer is the App's business, not the task's."
        ),
        scope=RuleScope.APP,
        name="FrameworkTransferInsideTask",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="storage-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.6.0",
        rationale=(
            "App.upload/App.download are themselves @task methods. Calling "
            "self.upload(...)/self.download(...) inside another @task-decorated "
            "method nests one activity inside another, which violates Temporal's "
            "single-activity-per-call contract and bypasses the SDK's store routing "
            "(upstream vs deployment). For task-to-task data, return a FileReference "
            "on the contract instead and let the activity interceptor move the bytes "
            "(BLDX-1398)."
        ),
        short_description="App calls self.upload()/self.download() inside a @task method",
        full_description=(
            "An ``App`` subclass calls ``self.upload(...)`` or ``self.download(...)``\n"
            "from within a ``@task``-decorated method.  ``App.upload`` and\n"
            "``App.download`` are themselves ``@task`` methods, so this nests an\n"
            "activity inside an activity — Temporal expects a single activity per\n"
            "call, and the nested transfer also bypasses the SDK's store routing\n"
            "(upstream store vs deployment store).\n"
            "\n"
            "The SDK has two data-flow paths and this is the wrong one for in-task\n"
            "data.  **Task-to-task** data should travel as a ``FileReference`` on the\n"
            "contract: the task writes a local file, returns\n"
            "``FileReference.from_local(path, tier=...)``, and the activity\n"
            "interceptor persists and materialises it across the boundary.\n"
            "**App-to-app** data is the only sanctioned use of ``App.upload()`` —\n"
            "called from ``run()``, not from inside a task.  Nesting the transfer in\n"
            "a task breaks the activity contract and routes bytes through the wrong\n"
            "store — see BLDX-1398.\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P008]\n"
            "<reason>`` records any unavoidable exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p008",
    ),
    RuleDefinition(
        id="P009",
        canonical_reference=(
            "atlan-openapi-app app/connector.py — object-store access goes through the "
            "SDK's `CloudStore` over `self.context.storage`. No reference app constructs a "
            "boto3/gcs/adls client of its own."
        ),
        scope=RuleScope.APP,
        name="ManualObjectStoreConstruction",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="storage-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.6.0",
        rationale=(
            "Constructing a cloud client or object store directly (boto3, an obstore "
            "S3Store/GCSStore/AzureStore, or a create_store_from_binding* call) bypasses "
            "the SDK's two-store routing contract. In SDR mode the wrong store may "
            "receive data, breaking the extract→publish hand-off. The SDK's "
            "infrastructure context (get_infrastructure().storage / .upstream_storage) "
            "and App.upload() route to the correct store (BLDX-1398)."
        ),
        short_description="App constructs its own cloud client or object store instead of the SDK storage seam",
        full_description=(
            "A consumer app builds its own storage backend directly: importing\n"
            "``boto3`` (``import boto3`` / ``from boto3 ...``), constructing an\n"
            "obstore store (``S3Store``, ``GCSStore``, ``AzureStore``), or calling\n"
            "``create_store_from_binding*``.  Any of these bypasses the SDK's two-store\n"
            "routing contract, so in SDR mode bytes can land in the wrong store and\n"
            "break the extract→publish hand-off.\n"
            "\n"
            "The supported surface is the SDK's infrastructure context —\n"
            "``get_infrastructure().storage`` and ``get_infrastructure().upstream_storage``\n"
            "— and ``App.upload()`` / ``App.download()``, which route to the correct\n"
            "store for the current mode.  Where no SDK equivalent exists, raise it with\n"
            "the SDK team — see BLDX-1398.\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P009]\n"
            "<reason>`` records any unavoidable exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p009",
    ),
    RuleDefinition(
        id="P010",
        canonical_reference=(
            "atlan-hello-world-app app/connector.py — "
            "`FileReference(local_path=str(out_path), tier=StorageTier.RETAINED)`. The app "
            "supplies the local path and the tier; storage_path, is_durable and file_count "
            "are stamped by the SDK when it moves the file."
        ),
        scope=RuleScope.APP,
        name="ManualFileReferenceConstruction",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="storage-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.6.0",
        rationale=(
            "FileReference's durability fields (storage_path, is_durable, file_count) "
            "are owned by the SDK and populated by the activity interceptor's "
            "persist/materialize contract. Hand-setting them at construction bypasses "
            "that contract and can produce stale or wrong references that point at the "
            "wrong store or a non-existent path. Use "
            "FileReference.from_local(path, tier=...) instead (BLDX-1398)."
        ),
        short_description="FileReference constructed with SDK-managed durability fields (storage_path/is_durable/file_count)",
        full_description=(
            "A ``FileReference(...)`` is constructed with one of the SDK-managed\n"
            "durability fields set explicitly: ``storage_path=``, ``is_durable=``, or\n"
            "``file_count=``.  The SDK owns these fields — they are populated by the\n"
            "activity interceptor as it persists a local file to the upstream store\n"
            "and materialises it back across the task boundary.  Hand-setting them\n"
            "bypasses that persist/materialise contract and can yield a stale or wrong\n"
            "reference (pointing at the wrong store, or a path that does not exist on\n"
            "the consuming worker).\n"
            "\n"
            "Construct the reference from a local file instead —\n"
            "``FileReference.from_local(path, tier=...)`` — and let the interceptor\n"
            "fill the durability fields.  See BLDX-1398.\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P010]\n"
            "<reason>`` records any unavoidable exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p010",
    ),
    RuleDefinition(
        id="P011",
        canonical_reference=(
            "atlan-openapi-app app/contracts.py — payloads that could be large travel as "
            "`FileReference` fields, never as bytes. A bytes field puts the whole artefact "
            "inside Temporal's 2MB envelope."
        ),
        scope=RuleScope.APP,
        name="RawBytesInContract",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="storage-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.6.0",
        rationale=(
            "Temporal enforces a hard 2MB payload limit on workflow/activity I/O. A "
            "contract field carrying raw bytes can silently grow past it in production, "
            "causing truncation or a serialization error at runtime instead of a clear "
            "failure. Store the data as a file and pass a FileReference, which crosses "
            "the task boundary as a small durable handle (BLDX-1398)."
        ),
        short_description="Input/Output contract field annotated bytes/bytearray/memoryview — risks the 2MB payload limit",
        full_description=(
            "An ``Input``/``Output`` contract subclass declares a field annotated\n"
            "``bytes``, ``bytearray``, ``memoryview``, or their ``| None`` /\n"
            "``Optional[…]`` variants.  A raw binary blob on a contract crosses the\n"
            "task boundary as a Temporal payload, which has a hard 2MB limit; a blob\n"
            "that grows past it in production fails the workflow with a serialization\n"
            "or size error rather than a clear, early signal.\n"
            "\n"
            "Store the data as a file and pass a ``FileReference`` instead: the bytes\n"
            "live in the object store and only a small durable handle travels on the\n"
            "contract, well under the payload limit.  See BLDX-1398.\n"
            "\n"
            "Land as ``WARN``: a justified inline ``# conformance: ignore[P011]\n"
            "<reason>`` records any unavoidable exception and stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p011",
    ),
    RuleDefinition(
        id="P012",
        canonical_reference=(
            "atlan-hello-world-app app/contracts.py — `greetings_file` and `output_file` "
            "are typed `FileReference | None`, so a hand-off survives being scheduled on "
            "another pod. atlan-metabase-app app/contracts.py shows the legitimate "
            "exception: `output_path` carries an inline ignore[P012] saying it is a "
            "task-local scratch base, not a cross-worker reference."
        ),
        scope=RuleScope.APP,
        name="FilePathStringInContract",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="storage-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.6.0",
        rationale=(
            "A contract field that carries a file or directory path as a bare string is "
            "a local-filesystem hint: the path is valid on the worker that produced it "
            "and meaningless on the worker that consumes it, so the data does not "
            "survive the task boundary. The path must be a durable FileReference so the "
            "interceptor persists and materialises the underlying file (BLDX-1398)."
        ),
        short_description="Input/Output contract field is a `str` path that should be a durable FileReference",
        full_description=(
            "An ``Input``/``Output`` contract subclass declares a ``str`` /\n"
            "``str | None`` field whose name or documentation indicates a file or\n"
            "directory path (e.g. ``output_path``, ``local_dir``, a doc string\n"
            "describing a path on disk).  A bare string path is a local-filesystem\n"
            "hint: it is valid only on the worker that produced it and meaningless on\n"
            "a different worker that consumes the contract, so the referenced data\n"
            "does not survive transfer across the task boundary.\n"
            "\n"
            "Carry the path as a durable ``FileReference`` instead, so the activity\n"
            "interceptor persists the underlying file to the object store and\n"
            "materialises it on the consuming worker.  See BLDX-1398.\n"
            "\n"
            "This rule is a heuristic — it matches on field name and documentation\n"
            "text — so a string field that is genuinely not a transferable path will\n"
            "occasionally be flagged.  Suppress a genuine exception with an inline\n"
            "``# conformance: ignore[P012] <reason>``, which stays visible in SARIF.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p012",
    ),
    RuleDefinition(
        id="P044",
        canonical_reference=(
            "atlan-mysql-app app/mysql.py — the whole-directory hand-off is one "
            "`App.upload()` with an UploadInput naming local_path and storage_path. "
            "storage.upload_prefix / download_prefix move bytes without producing a "
            "FileReference the next task can resolve."
        ),
        scope=RuleScope.APP,
        name="DirectStoragePrefixTransfer",
        tier=EnforcementTier.WARN,
        mechanism=RuleMechanism.STATIC,
        category="storage-seam",
        autofixable=False,
        orthogonal_gate="tests",
        since="0.21.0",
        rationale=(
            "An app moving whole prefixes with storage.upload_prefix/download_prefix is "
            "using a sanctioned SDK function at the wrong level, which is why no existing "
            "rule catches it: P009 fires only on apps constructing their own store, and "
            "P030/P042 are gated on self_deployed_runtime and so never run for a "
            "direct-flow app. What the app gives up is everything layered above the "
            "primitive — App.upload()'s ADR-0014 dual-write routing, the canonical "
            "artifacts/apps/{app}/workflows/{workflow_id}/{run_id} prefix, @task "
            "retry/replay, and FileReference's per-file SHA-256 sidecars, whose whole "
            "purpose is detecting the partial or corrupt file that a per-directory "
            "non-emptiness check cannot see. Apps that hand-roll this reliably re-derive "
            "the missing guards later and get them subtly wrong: one connector's wrapper "
            "skipped the download whenever the target directory was already non-empty, "
            "which for a multi-writer fan-out prefix means a co-located reader silently "
            "sees a subset of the data. "
            "Customer impact: a silent subset. Some of the customer's metadata is "
            "extracted, the run reports success, and the assets that never arrived look "
            "indistinguishable from assets the source does not have — so it is the "
            "customer who finds the gap, if anyone does."
        ),
        short_description=(
            "App transfers whole prefixes via storage.upload_prefix/download_prefix "
            "instead of FileReference + App.upload()/App.download()"
        ),
        full_description=(
            "App source calls ``upload_prefix`` / ``download_prefix`` (or imports them\n"
            "from ``application_sdk.storage``) to move artifacts itself, rather than\n"
            "declaring the data on the contract and letting the SDK move it.\n"
            "\n"
            "These are real SDK functions, so this is not the\n"
            "build-your-own-store shape **P009** describes — it is the sanctioned seam\n"
            "used one level too low. The storage contract in this module's docstring\n"
            "names the two supported paths:\n"
            "\n"
            "* **task-to-task** data → a ``FileReference`` field on the contract."
            " The activity interceptor persists it after the producing task and"
            " materialises it before the consuming one, with a per-file SHA-256"
            " sidecar so a partial or corrupt transfer is detected rather than"
            " reused.\n"
            "\n"
            "* **phase/app hand-off** → ``App.upload()`` / ``App.download()``, which"
            " add dual-write routing, the canonical artifact prefix, and ``@task``"
            " retry/replay.\n"
            "\n"
            "**Fix by hoisting, not by substituting in place.** ``App.upload()`` is\n"
            "itself a framework task, so calling it where the prefix call used to sit\n"
            "— inside a ``@task`` — trades this finding for a **P008** violation. Move\n"
            "the transfer to ``run()`` / the ``@entrypoint``, one call per phase, and\n"
            "let the tasks below it read and write local paths.\n"
            "\n"
            "Not every prefix call is wrong. A genuine bulk transfer with no contract\n"
            "boundary to hang a reference on — a state directory synced wholesale, a\n"
            "one-off migration script — is a legitimate use. Suppress those with an\n"
            "inline ``# conformance: ignore[P044] <reason>``, which stays visible in\n"
            "SARIF rather than disappearing.\n"
            "\n"
            "Tier note: this lands at WARN because the pattern is fleet-wide, not\n"
            "isolated — a sweep of ``atlanhq`` found app-code calls in roughly 34\n"
            "consumer repos, five of them sharing a copied ``app/workflow/io.py``. It\n"
            "graduates to BLOCK once that count is falling rather than flat. It is\n"
            "deliberately a separate id from **P030** for the same reason P030 and P042\n"
            "are separate: absence of ``App.upload()`` in SDR mode is a proven\n"
            "customer-data-loss class and blocks, while absence in the direct flow is\n"
            "legitimate — ``persist_file_refs`` performs that transfer automatically —\n"
            "so the two shapes cannot share a tier.\n"
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p044",
    ),
)
