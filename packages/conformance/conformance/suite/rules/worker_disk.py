"""Worker-disk rule definitions (P060–P062).

The disk model that lets a connector run on the fleet's smallest worker volume —
8 GiB that grows on demand — has three mechanical parts, and these rules enforce
them: every file the worker uploads is removed once the upload returns (P060),
scratch is created where the worker volume and the SDK's cleanup can see it
(P061), and scratch is removed on every exit path, not just the happy one
(P062).  The judgment-heavy companions stay recommendations in the O-series:
O009 (fixed-size worker volumes) and O010 (a whole prefix staged on disk).

Why the P-series
----------------
These rules are mandatory, and the O-series holds recommendations only: "a rule
that earns mandatory status graduates into a category series or the P-series".
No category series covers storage or the worker's disk — the closest
neighbours, the storage-seam rules (P008–P012, P044), are P-series too — so
these are P-series rules in their own module, modelled on the storage-seam
series.  P060 and P061 were drafted as O007 and O008 and graduated before any
release; those O-ids stay vacant, the way P032–P035 and P047 did when the
preflight rules moved to the F-series.

Why BLOCK, and how it is staged
-------------------------------
Each fix is a local, mechanical edit that ``/remediate`` applies in default
mode (the prescriptions area runs this group through the full gated loop; see
``programs/areas/prescriptions.prose.md``), and each failure mode is a disk
that fills mid-run.  A BLOCK finding fails CI only in a repo that runs
conformance as a hard gate: a consumer's ``conformance.yaml`` sets
``exit-zero`` (bootstrap ``--enforce false``) to observe BLOCK findings without
failing on them while the fleet converges, and a site whose file must outlive
the function takes a justified inline directive (see each rule's
``terminal_state``).

P-ids are a permanent public contract (see ``prescriptions.py``).
"""

from __future__ import annotations

from conformance.suite.schema.catalog import (
    RemediationKind,
    RemediationReference,
    RuleDefinition,
)
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

_PRESCRIPTION = RemediationReference(
    kind=RemediationKind.PRESCRIPTION,
    target="programs/areas/prescriptions.prose.md",
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="P060",
        canonical_reference=(
            "application_sdk/storage/formats/__init__.py — the writers' "
            "`_upload_file` passes `retain_local_copy=` explicitly, and the JSON and "
            "Parquet writers default it to False, so a chunk uploaded inline leaves "
            "the worker's disk once it is in the store; `upload_prefix` takes the "
            "same keyword. No reference app calls upload_file or upload_prefix "
            "directly; they hand files off through App.upload and App.upload_refs."
        ),
        scope=RuleScope.APP,
        name="UploadedFileNotDeleted",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="worker-disk",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.51.0",
        rationale=(
            "upload_file keeps the local file after a successful upload unless the "
            "caller passes retain_local_copy=False (the keyword defaults to True), "
            "and even then it deletes only a file under TEMPORARY_PATH; "
            "upload_prefix does the same for every file it uploads. A connector "
            "that uploads as it goes therefore holds every file it has already "
            "shipped until the run ends, so its worker disk must be sized for the "
            "whole run's output rather than one step's. The connector that runs on "
            "the fleet's smallest worker disk removes each file by path as soon as "
            "its upload returns (atlan-snowflake-app, RollingParquetWriter.close in "
            "app/pipeline/rolling_writers.py). Customer impact: on a large tenant "
            "the kept files fill the worker volume partway through the run, which "
            "then fails with 'No space left on device' or is evicted; sizing every "
            "worker for that peak instead is what drives fixed worker volumes to "
            "hundreds of GiB."
        ),
        short_description=(
            "upload_file()/upload_prefix() leaves its local source on disk: no "
            "retain_local_copy=False and no removal after the upload"
        ),
        full_description=(
            "Flags a call to the SDK's ``upload_file`` or ``upload_prefix`` whose\n"
            "local source stays on disk: ``retain_local_copy`` is omitted or the\n"
            "literal ``True``, and nothing after the upload in the same function\n"
            "removes the uploaded path, a path it was built from, or an alias of\n"
            "it.  A removal is ``os.remove`` / ``os.unlink`` / ``shutil.rmtree`` /\n"
            "``Path.unlink``, a helper whose name says it removes, or a removal\n"
            "handed to ``asyncio.to_thread`` or a cleanup callback, ``finally``\n"
            "included, so removing ``out_dir`` covers each file uploaded from it.\n"
            "\n"
            "Calls resolve through import bindings (``application_sdk.storage``,\n"
            "``application_sdk.storage.ops`` / ``.batch`` and every module that\n"
            "re-exports the helpers), so an app's own ``upload_file`` and a client\n"
            "method of that name never match.  ``retain_local_copy=False`` is\n"
            "compliant; any other expression and a ``**kwargs`` splat are left\n"
            "alone.  A path the function returns, yields or stores on an attribute\n"
            "— a ``FileReference`` handed to the next task, say — belongs to its\n"
            "receiver and is not graded.\n"
            "\n"
            "Fix: add the removal right after the upload — ``os.remove(path)`` or\n"
            "``Path(path).unlink(missing_ok=True)`` for ``upload_file``,\n"
            "``shutil.rmtree(local_dir, ignore_errors=True)`` for\n"
            "``upload_prefix`` — or pass ``retain_local_copy=False`` when the path\n"
            "is built under ``TEMPORARY_PATH`` (outside it the keyword deletes\n"
            "nothing).  When the function reads the path again after the upload,\n"
            "the message names the line; put the removal after that read.  When the\n"
            "next reader is outside this function, the file is not this function's\n"
            "to delete (see *Already correct when*).\n"
        ),
        rule_interactions=(
            "P061: retain_local_copy=False deletes only under TEMPORARY_PATH, so a "
            "file staged by a tempfile call with no dir= is not deleted by it; "
            "remove it explicitly. O010: removing a re-uploaded download_prefix "
            "directory clears P060, but the whole prefix was still staged at once, "
            "which O010 reports until the move is batched."
        ),
        terminal_state=(
            "A justified inline `# conformance: ignore[P060] <reason>` IS the "
            "correct end state when the file must outlive this function and the "
            "checker cannot see it leave: a later step reads it (an App.upload in "
            "run(), a second store's upload elsewhere, a retry that re-uploads it) "
            "and the reason names that reader and what removes the file afterwards. "
            "A directive that names neither is unremediated: remove the file after "
            "the upload."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p060",
        remediation_reference=_PRESCRIPTION,
    ),
    RuleDefinition(
        id="P061",
        canonical_reference=(
            "application_sdk/storage/ops.py — `download_file` stages its partial "
            "file with `tempfile.mkstemp(dir=str(staging_dir), ...)` in a directory "
            "beside the destination, so the scratch sits on the same volume as the "
            "file it becomes. No reference app passes dir=: atlan-openapi-app's "
            "connector and atlan-metabase-app's `task_scratch_dir` call "
            "tempfile.mkdtemp with only a prefix, the shape this rule flags."
        ),
        scope=RuleScope.APP,
        name="TempFileOutsideWorkerVolume",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="worker-disk",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.51.0",
        rationale=(
            "Worker disk is provisioned, measured and cleaned at TEMPORARY_PATH: "
            "apps mount their worker volume where it lives, upload_file deletes "
            "only under it, and the end-of-run cleanup_files task removes the run's "
            "directory beneath it. The stdlib tempfile constructors default to the "
            "system temp dir instead ($TMPDIR, else /tmp), and the SDK base image "
            "sets no TMPDIR, so unless an app mounts its volume at /tmp that scratch "
            "lands on the node's root disk. A review of connector worker disks found "
            "scratch there that volume metrics never saw, that disk sizing missed "
            "and that no cleanup removed. Customer impact: a large run fills the "
            "node's root disk instead of the worker volume the platform can grow, "
            "so the pod is evicted mid-run, and the volume's metrics never showed "
            "the pressure that caused it."
        ),
        short_description=(
            "tempfile scratch with no dir= lands in the system temp dir, outside "
            "TEMPORARY_PATH and the worker volume — pass dir= under TEMPORARY_PATH"
        ),
        full_description=(
            "Flags a stdlib ``tempfile`` call — ``mkdtemp``, ``mkstemp``,\n"
            "``NamedTemporaryFile``, ``TemporaryDirectory``, ``TemporaryFile`` or\n"
            "``SpooledTemporaryFile`` — that is given no directory: ``dir=``\n"
            "omitted, ``dir=None``, ``dir=tempfile.gettempdir()`` (the same location\n"
            'spelled out) or the literal ``"/tmp"`` / ``"/var/tmp"`` itself.  A\n'
            "``dir`` passed positionally is read at each constructor's real\n"
            "position.  Only stdlib bindings count (``import tempfile [as t]``,\n"
            "``from tempfile import mkdtemp [as x]``), so an app helper of the same\n"
            "name is never matched; any other ``dir=`` expression — a sub-directory\n"
            "literal included, since some apps mount their volume under ``/tmp`` —\n"
            "is compliant, and a ``*args`` / ``**kwargs`` splat that may supply it\n"
            "is left alone.\n"
            "\n"
            "Such scratch lands in the system temp dir (``$TMPDIR``, else\n"
            "``/tmp``).  The SDK base image sets no ``TMPDIR``, so unless the app\n"
            "mounts its worker volume at ``/tmp`` it lands on the node's root disk:\n"
            "worker-volume metrics and disk sizing never see it, and filling it\n"
            "starves the node rather than the app's volume.  Either way it sits\n"
            "outside ``TEMPORARY_PATH``, the tree the SDK's local cleanup works in\n"
            "(``upload_file`` deletes only under it; ``cleanup_files`` removes the\n"
            "run's directory beneath it).\n"
            "\n"
            "Fix: pass ``dir=`` a directory under ``TEMPORARY_PATH``\n"
            "(``application_sdk.constants``), creating it first with\n"
            "``os.makedirs(..., exist_ok=True)`` because ``tempfile`` does not create\n"
            "a missing ``dir``; reuse the app's own scratch-root helper when it\n"
            "already resolves under ``TEMPORARY_PATH``, and keep the call's other\n"
            "arguments as they are.  In an app that declares no worker volume,\n"
            "``TEMPORARY_PATH`` itself sits on the container's disk, so there the\n"
            "fix buys cleanup reach and puts the scratch where a volume lands once\n"
            "the app declares one.\n"
            "\n"
            "Out of scope: ``tempfile.mktemp`` (deprecated) and paths assembled by\n"
            "hand from ``tempfile.gettempdir()``.  A process-wide\n"
            "``tempfile.tempdir`` or ``TMPDIR`` override is invisible to a per-file\n"
            "scan, so an app that points one under ``TEMPORARY_PATH`` still sees\n"
            "findings: pass ``dir=`` anyway.\n"
        ),
        rule_interactions=(
            "P060: upload_file(retain_local_copy=False) deletes only under "
            "TEMPORARY_PATH, so moving this scratch there is also what lets an "
            "upload clean up after itself. P062 grades whether mkdtemp/mkstemp "
            "scratch is removed; this rule grades where any tempfile scratch lands."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p061",
        remediation_reference=_PRESCRIPTION,
    ),
    RuleDefinition(
        id="P062",
        canonical_reference=(
            "application_sdk/storage/transfer.py — `_upload_from_store` stages each "
            "object in a `tempfile.mkstemp()` file and unlinks it in a `finally` "
            "(under `contextlib.suppress(OSError)`), so a failed copy leaves nothing "
            "behind. No reference app removes mkdtemp scratch in a finally: "
            "atlan-openapi-app's extract step returns FileReferences to files in its "
            "mkdtemp directory, which makes the framework their owner, so it is not "
            "graded."
        ),
        scope=RuleScope.APP,
        name="TempScratchNotCleanedUp",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="worker-disk",
        autofixable=True,
        orthogonal_gate="tests",
        since="0.51.0",
        rationale=(
            "tempfile.mkdtemp and tempfile.mkstemp hand back a path only the caller "
            "removes, and a removal at the end of the happy path is skipped by "
            "every exception before it. Activities fail and retry by design, so "
            "each failed attempt leaves its scratch behind on the worker that ran "
            "it. The connector that runs on the fleet's smallest worker disk pairs "
            "every write path with a discard path: RollingParquetWriter.discard() "
            "deletes a failed chunk's partial files without uploading them "
            "(atlan-snowflake-app app/pipeline/rolling_writers.py). Customer "
            "impact: a run that retries, or a worker that keeps picking up failing "
            "activities, accumulates one scratch tree per attempt until the volume "
            "fills, and then unrelated runs on that worker fail with 'No space "
            "left on device'."
        ),
        short_description=(
            "tempfile.mkdtemp()/mkstemp() scratch is not removed in a finally, so an "
            "exception leaves it on the worker's disk"
        ),
        full_description=(
            "Flags ``tempfile.mkdtemp(...)`` / ``tempfile.mkstemp(...)`` whose\n"
            "result is bound to a local name (``d = tempfile.mkdtemp()``,\n"
            "``d = Path(tempfile.mkdtemp())``, ``fd, path = tempfile.mkstemp()``)\n"
            "when that scratch is not removed on every exit path: no removal of it\n"
            "— ``shutil.rmtree`` / ``os.remove`` / ``os.unlink`` /\n"
            "``Path.unlink``, a removal-named helper, or a removal handed to\n"
            "``asyncio.to_thread`` — inside a ``finally``, and no cleanup callback\n"
            "registered for it (``stack.callback(shutil.rmtree, d)``,\n"
            "``atexit.register``, ``weakref.finalize``).  A removal at the end of\n"
            "the happy path is not enough: the message names its line.\n"
            "\n"
            "Scratch the function passes out — returned, yielded, stored on an\n"
            "attribute or subscript, or built into something that is (a returned\n"
            "``FileReference`` to a file inside it) — belongs to the receiver and\n"
            "is not graded, so a helper that returns a fresh scratch directory is\n"
            "clean.  The context-manager forms (``TemporaryDirectory``,\n"
            "``NamedTemporaryFile``) clean up after themselves and are not graded\n"
            "here; P061 grades where they land.\n"
            "\n"
            "Fix: wrap the scratch's use in ``try`` / ``finally`` and remove it in\n"
            "the ``finally`` — ``shutil.rmtree(d, ignore_errors=True)`` for a\n"
            "directory, ``os.remove(path)`` (after ``os.close(fd)``) for a\n"
            "``mkstemp`` file — moving an existing happy-path removal there rather\n"
            "than adding a second one; or switch a directory to\n"
            "``with tempfile.TemporaryDirectory(dir=...) as d:``.\n"
            "\n"
            "A removal in an ``except`` that catches everything (bare,\n"
            "``Exception``, ``BaseException``) together with one on the success\n"
            "path covers every exit too.  Known limits: a result not bound to a\n"
            "name is not graded, and a removal only in narrower handlers\n"
            "(``except OSError``) plus the success path still reads as\n"
            "happy-path-only, since other exceptions skip both.\n"
        ),
        rule_interactions=(
            "P061: the same call usually also lacks dir=; fix both in one edit "
            "(TemporaryDirectory(dir=...) clears both for a directory)."
        ),
        terminal_state=(
            "A justified inline `# conformance: ignore[P062] <reason>` IS the "
            "correct end state when the scratch must outlive this function and the "
            "checker cannot see it leave — it is registered with an owner through a "
            "call (a writer, a cache, a cleanup registry) that removes it later — "
            "and the reason names that owner. A directive that names no owner is "
            "unremediated: remove the scratch in a finally."
        ),
        help_uri="https://github.com/atlanhq/application-sdk/blob/main/packages/conformance/conformance/docs/rules/prescriptions.md#p062",
        remediation_reference=_PRESCRIPTION,
    ),
)
