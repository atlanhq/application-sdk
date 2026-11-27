"""``SqlApp``'s per-entity I/O: SQL stream to raw JSONL, raw JSONL to entities.

:meth:`extract_entity` and :meth:`transform_entity` own the streaming, the
event-loop offload, the progress marks and the canonical ``FileReference``
keys; the client and SQL helpers under them live here too. Split out of
``templates/sql_app.py`` (FND-4267):
:class:`~application_sdk.templates.sql_app.SqlApp` inherits it, so every name
here is reached as ``SqlApp.<name>``, and a connector overrides them on its
``SqlApp`` subclass as before.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Union

import orjson

from application_sdk._runtime.offload import raise_if_offload_abandoned, run_in_thread
from application_sdk._runtime.progress import current_progress_tracker
from application_sdk.app.base import App
from application_sdk.common.asset_serialization import entity_bytes
from application_sdk.common.asset_serialization import orjson_default as _orjson_default
from application_sdk.common.entity_envelope import (
    EntityDecorations,
    EntityEnvelopePolicy,
)
from application_sdk.common.last_sync import resolve_last_sync_details
from application_sdk.common.sql_filters import (
    normalize_filters,
    safe_substitute_placeholders,
    strip_sql_block_comments,
)
from application_sdk.constants import TEMPORARY_PATH as _TEMPORARY_PATH
from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.credentials import CredentialResolver
from application_sdk.credentials.ref import CredentialRef, _guid_credential_ref

# ``AppTimeoutError`` is re-exported: importable from here before FND-4687 moved
# the wire-envelope rebuild into ``errors.wire`` (Symbol Removal Check).
from application_sdk.errors.leaves import AppTimeoutError as AppTimeoutError
from application_sdk.execution import build_output_path, get_object_store_prefix
from application_sdk.infrastructure.context import get_infrastructure
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage._concurrency import _run_drained
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionTaskInput,
    ExtractionTaskOutput,
    TransformInput,
    TransformOutput,
)

if TYPE_CHECKING:
    from pyatlan_v9.model.assets import Asset

    from application_sdk.clients.sql import BaseSQLClient


if TYPE_CHECKING:
    # A type checker sees the mixin as an ``App``, so ``self`` has the App
    # API; at runtime it is a plain mixin, and only ``SqlApp`` is an ``App``.
    _AppBase = App
else:
    _AppBase = object

logger = get_logger(__name__)

#: Mapper-function signature: takes a raw record dict + connection
#: qualified name, returns a pyatlan_v9 Asset (preferred) or a plain dict
#: (legacy / connector-specific shapes the v3 publisher accepts).
_MapperFn = Callable[[dict[str, Any], str], Union["Asset", dict[str, Any]]]

#: SQL row batch size used by ``extract_entity`` when streaming results
#: through ``client.run_query()``. Caps the in-flight set at
#: ``_EXTRACT_BATCH_SIZE * row_size`` regardless of total result size.
_EXTRACT_BATCH_SIZE: int = 10_000


class _SqlEntityIOMixin(_AppBase):
    """Per-entity extract and transform I/O, mixed into ``SqlApp``."""

    if TYPE_CHECKING:
        # Declared on ``SqlApp``; restated so this mixin type-checks alone.
        sql_client_class: ClassVar[type[BaseSQLClient] | None]
        fetch_column_sql: ClassVar[str]
        extract_temp_table_regex_table_sql: ClassVar[str]
        extract_temp_table_regex_column_sql: ClassVar[str]
        entity_envelope: ClassVar[EntityEnvelopePolicy]

        def decorate_entity(
            self, *, entity_type: str, record: dict[str, Any]
        ) -> EntityDecorations | None: ...

    @staticmethod
    def resolve_output_path(input: ExtractionTaskInput) -> str:
        """Return the local directory this task writes under.

        ``input.output_path`` when the caller set one; otherwise the run's
        path under ``TEMPORARY_PATH``, from ``build_output_path()``, which
        reads the activity context — so call this inside a task, not in
        ``run()``.

        Public because connectors that write their own files beside the
        template's (a custom extract, a miner) need the same directory
        :meth:`extract_entity` and :meth:`transform_entity` use, or their
        output lands outside the tree the upload walks.
        """
        if not input.output_path:
            resolved = build_output_path()
            logger.info("Auto-resolved output_path: %s", resolved)
            return os.path.join(_TEMPORARY_PATH, resolved)
        return input.output_path

    async def _init_sql_client(self, input: ExtractionTaskInput) -> BaseSQLClient:
        """Initialize and return a SQL client from credentials.

        ``ExtractionTaskInput`` only carries ``credential_ref`` / ``credential_guid``
        (no ``agent_json`` / ``extraction_method``). The SDR/direct routing
        decision happens upstream in ``_resolve_credential_ref(ExtractionInput)``
        which populates ``credential_ref`` on the task input via
        ``build_task_input``. So here we just consume that ref.
        """
        if self.sql_client_class is None:
            from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
                SqlClientClassNotSetError,
            )

            raise SqlClientClassNotSetError()

        client = self.sql_client_class()
        creds: dict[str, Any] = {}

        ref: CredentialRef | None = input.credential_ref
        if ref is None and input.credential_guid:
            ref = _guid_credential_ref(input.credential_guid)

        if ref is not None:
            infra = get_infrastructure()
            secret_store = infra.secret_store if infra else None
            if secret_store:
                resolver = CredentialResolver(secret_store)
                creds = await resolver.resolve_raw(ref) or {}

        await client.load(credentials=creds)
        return client

    def _temp_table_regex_fragment(self, sql: str) -> str:
        """Pick the temp-table fragment for *sql*.

        The column query gets ``extract_temp_table_regex_column_sql`` when the
        app declares one: it filters on the column table's own name
        (``C.TABLE_NAME``) rather than a joined table alias the column query
        may not have. Every other query, and apps without a column fragment,
        get ``extract_temp_table_regex_table_sql``. The query is recognised by
        comparing against ``fetch_column_sql`` so ``_prepare_sql``'s signature,
        which connector overrides mirror, is unchanged.
        """
        column_fragment = self.extract_temp_table_regex_column_sql
        if (
            column_fragment
            and self.fetch_column_sql
            and sql.strip() == self.fetch_column_sql.strip()
        ):
            return column_fragment
        return self.extract_temp_table_regex_table_sql

    def _prepare_sql(self, sql: str, input: ExtractionTaskInput) -> str:
        """Substitute filter placeholders in SQL template."""
        exclude_filter = input.exclude_filter or ""
        include_filter = input.include_filter or ""

        # Handle dict filters (from AE) — normalize to regex
        if isinstance(exclude_filter, dict):
            normalized = normalize_filters(exclude_filter, False)
            exclude_regex = "|".join(normalized) if normalized else "^$"
        else:
            exclude_regex = exclude_filter or "^$"

        if isinstance(include_filter, dict):
            normalized = normalize_filters(include_filter, True)
            include_regex = "|".join(normalized) if normalized else ".*"
        else:
            include_regex = include_filter or ".*"

        # Temp table regex. The fragment's own ``/* ... */`` header is stripped
        # first: full templates often mention ``{temp_table_regex_sql}`` inside
        # their header comment, and a nested block comment closes it early
        # (FND-2733).
        temp_table_sql = ""
        if hasattr(input, "temp_table_regex") and input.temp_table_regex:
            fragment = self._temp_table_regex_fragment(sql)
            if fragment:
                temp_table_sql = strip_sql_block_comments(fragment).replace(
                    "{exclude_table_regex}", input.temp_table_regex
                )

        # Single-pass substitution via safe_substitute_placeholders prevents
        # cascading: three chained str.replace() calls processed the mutating
        # string, so a replacement value containing another placeholder's text
        # would be re-substituted by the next call.  safe_substitute_placeholders
        # scans the original positions exactly once — replacement values are
        # never re-scanned (APP-2291).
        return safe_substitute_placeholders(
            sql,
            {
                "{normalized_exclude_regex}": exclude_regex,
                "{normalized_include_regex}": include_regex,
                "{temp_table_regex_sql}": temp_table_sql,
            },
        )

    async def extract_entity(
        self,
        *,
        entity_type: str,
        sql_template: str,
        input: ExtractionTaskInput,
    ) -> ExtractionTaskOutput:
        """Stream SQL rows verbatim into ``raw/<entity>/records.json`` JSONL.

        The body of every ``extract_*`` task. Call it from your own override
        to extract an entity with different SQL, or one the template does not
        know about::

            @task()
            async def extract_functions(
                self, input: ExtractionTaskInput
            ) -> ExtractionTaskOutput:
                return await self.extract_entity(
                    entity_type="function",
                    sql_template=self.fetch_function_sql,
                    input=input,
                )

        1. Open SQL client, render the template with the input filters.
        2. Stream rows in batches of ``_EXTRACT_BATCH_SIZE`` via the
           server-side cursor that ``client.run_query`` exposes — each
           batch is a ``list[dict[str, Any]]`` keyed by lower-cased column
           names. Memory stays bounded regardless of total result size.
        3. Write each row to JSONL. Mapping is deliberately *not* done
           here — it runs in the matching ``transform_*`` activity so the
           activity boundary stays a durable retry point and so future
           change-detection can compare raw streams between runs.
        """
        if not sql_template:
            logger.warning("No SQL configured for %s — skipping", entity_type)
            return ExtractionTaskOutput(typename=entity_type, total_record_count=0)

        output_path = self.resolve_output_path(input)
        if not output_path:
            return ExtractionTaskOutput(typename=entity_type, total_record_count=0)

        output_dir = Path(output_path) / "raw" / entity_type
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "records.json"

        client = await self._init_sql_client(input)
        count = 0
        try:
            sql = self._prepare_sql(sql_template.strip(), input)

            def _serialize_batch(batch: list[dict[str, Any]]) -> bytes:
                return b"".join(
                    orjson.dumps(
                        record,
                        default=_orjson_default,
                        option=orjson.OPT_APPEND_NEWLINE,
                    )
                    for record in batch
                )

            with open(output_file, "wb") as f:

                def _write_batch(batch: list[dict[str, Any]]) -> None:
                    f.write(_serialize_batch(batch))

                async for batch in client.run_query(
                    sql, batch_size=_EXTRACT_BATCH_SIZE
                ):
                    # Serialise and write off the loop (ADR-0010): 10k
                    # orjson.dumps calls per batch outlast the heartbeat
                    # interval on wide rows, and a batch has no byte bound, so
                    # its write to a slow or contended volume can too.
                    #
                    # Drained, because the handle crosses into the thread: on
                    # a cancellation at this await, ``_run_drained`` waits for
                    # the thread before the cancellation propagates, so the
                    # ``with`` closes ``f`` only after the write has returned —
                    # never under a live writer, and never into a
                    # retry-reopened file.
                    await _run_drained(run_in_thread(_write_batch, batch))
                    count += len(batch)
                    # One fetched page serialised and written is one observable
                    # unit (ADR-0018). Marked here rather than per record: one
                    # page is already the unit, and a mark per record would be a
                    # hot-path cost for no extra signal.
                    #
                    # This is the whole reason the read side needs no hold — the
                    # fetch → serialise → write cycle marks on every page, so
                    # only a *single* cycle slower than max_no_progress_seconds
                    # needs an explicit hold from the app. (FND-290's auto-hold
                    # around the `run_in_thread` above already covers the
                    # serialise-and-write leg of that cycle.)
                    current_progress_tracker().mark_progress("extract.page")
        finally:
            await client.close()

        logger.info("Extracted %s: %d raw records", entity_type, count)
        # Emit a ``FileReference`` so the activity interceptor uploads the
        # raw JSONL to the object store after the extract activity
        # completes and marks the ref durable. ``run()`` then threads
        # this durable ref into the matching transform's
        # ``TransformInput`` — the interceptor re-downloads it onto
        # whichever worker pod picks up the transform (with SHA-256
        # sidecar verification, so a transform that lands on a
        # different pod than the extract still sees a verified-fresh
        # local copy). This is how cross-worker fault tolerance for
        # the raw → transform handoff is provided without any explicit
        # ``download_file`` plumbing in the template (BLDX-1281).
        #
        # ``storage_path`` is pinned to the canonical run-scoped key
        # ``<run_prefix>/raw/<entity>/records.json`` rather than the
        # default UUID-named ``<run_prefix>/file_refs/<uuid>.json``.
        # ``get_object_store_prefix`` strips ``TEMPORARY_PATH`` from the
        # local path to derive the matching object-store key — same
        # shape ``upload_to_atlan``'s legacy directory walk would have
        # produced. Pinning the key on the ref itself makes the upload
        # work even when ``upload_to_atlan`` runs on a different pod
        # than this extract and finds the local FS empty (the
        # cross-pod failure mode that caused silent asset archival in
        # production — publish reads from canonical paths and would
        # miss any entity whose transformed-side upload only landed
        # under ``file_refs/<uuid>.json``).
        #
        # TRANSIENT tier — the raw file is purely intermediate
        # (extract → transform within the same run); ``cleanup_storage``
        # auto-deletes tracked TRANSIENT refs at run end.
        raw_ref = (
            FileReference(
                local_path=str(output_file),
                storage_path=get_object_store_prefix(str(output_file)),
                tier=StorageTier.TRANSIENT,
            )
            if count > 0
            else None
        )
        return ExtractionTaskOutput(
            typename=entity_type,
            total_record_count=count,
            raw_file=raw_ref,
        )

    async def transform_entity(
        self,
        *,
        entity_type: str,
        mapper_fn: _MapperFn,
        input: TransformInput,
    ) -> TransformOutput:
        """Read raw JSONL → mapper → transformed JSONL.

        The body of every ``transform_*`` task. Call it from your own override
        to run an entity through a different mapper::

            @task()
            async def transform_functions(
                self, input: TransformInput
            ) -> TransformOutput:
                return await self.transform_entity(
                    entity_type="function",
                    mapper_fn=self.map_function,
                    input=input,
                )

        It also applies ``entity_envelope``, :meth:`decorate_entity`, and the
        ``connectionName`` / ``lastSync*`` attributes to every line, so going
        through it is how a custom entity's output matches the rest.

        Reads the raw records.json via the ``FileReference`` threaded
        through from the matching ``extract_*`` activity, runs each
        record through ``mapper_fn``, and writes the resulting Atlan
        asset to ``transformed/<entity>/entities.json``.

        Cross-worker contract (BLDX-1281 / PR #1787):
            ``run()`` populates ``input.raw_file`` with the durable
            ``FileReference`` returned by the matching extract activity.
            The activity interceptor materialises that ref onto this
            worker's local FS BEFORE this method runs (SHA-256 sidecar
            verification → fresh local copy even if extract and
            transform landed on different pods). The method below just
            opens ``input.raw_file.local_path`` directly.

            When ``input.raw_file is None`` (extract returned zero rows
            or the caller didn't thread a ref), this is a clean no-op
            returning ``total_record_count=0`` — matches the historical
            contract that the publish step relies on.
        """
        output_path = self.resolve_output_path(input)
        if not output_path:
            return TransformOutput(typename=entity_type, total_record_count=0)

        # Resolve the raw file via the threaded FileReference. The
        # interceptor has already materialised it; we just consume the
        # local_path. Fall back to the legacy local path lookup so
        # callers that don't thread a ref (e.g. unit tests that seed
        # raw files directly under tmp_path) still work.
        if input.raw_file is not None and input.raw_file.local_path:
            raw_file = Path(input.raw_file.local_path)
        else:
            raw_file = Path(output_path) / "raw" / entity_type / "records.json"
        if not raw_file.exists() or raw_file.stat().st_size == 0:
            return TransformOutput(typename=entity_type, total_record_count=0)

        connection_qn = ""
        connection_name = ""
        if hasattr(input, "connection") and input.connection:
            attrs = getattr(input.connection, "attributes", None)
            if attrs:
                connection_qn = getattr(attrs, "qualified_name", "") or ""
                connection_name = getattr(attrs, "name", "") or ""

        # Resolved once, here, for two reasons (FND-2097).
        #
        # Once, not per record: ``lastSyncRunAt`` is a property of the *run*,
        # so every asset this activity writes must carry the same value. A
        # per-record ``time.time()`` — what connectors hand-rolling this did —
        # gives every row in one crawl a different "last synced at".
        #
        # Here, not inside ``_map_records``: the resolver reads the execution
        # and correlation contextvars the Temporal interceptor populates.
        # ``run_in_thread`` does propagate them (it runs the callable under
        # ``contextvars.copy_context()``), so resolving in the thread would
        # also work today — but that correctness would rest on an offload
        # implementation detail rather than on where the call sits.
        last_sync = resolve_last_sync_details()

        output_dir = Path(output_path) / "transformed" / entity_type
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "entities.json"

        def _map_records() -> int:
            written = 0
            with open(raw_file, "rb") as r, open(output_file, "wb") as w:
                for line in r:
                    line = line.strip()
                    if not line:
                        continue
                    record = orjson.loads(line)
                    asset = mapper_fn(record, connection_qn)
                    # One seam owns the framework-injected attributes —
                    # connectionName (FND-2056) and lastSync* (FND-2097) —
                    # the asset → wire-shape dispatch, and the envelope the
                    # finished line takes (FND-2137), for every template that
                    # runs the mapper pattern. A return value it does not
                    # recognise raises rather than falling back to writing
                    # ``record`` — that fallback published unmapped source rows
                    # as entities under a SUCCESS status.
                    #
                    # ``decorate_entity`` is called per record because that is
                    # its aperture: its inputs are the row and the stream, and
                    # a connector that owes no decorations returns None from
                    # the base implementation at the cost of one call.
                    w.write(
                        entity_bytes(
                            asset,
                            connection_name=connection_name,
                            last_sync=last_sync,
                            entity_type=entity_type,
                            envelope=self.entity_envelope,
                            decorations=self.decorate_entity(
                                entity_type=entity_type, record=record
                            ),
                        )
                    )
                    w.write(b"\n")
                    written += 1
                    # Every 1,024 records: a cancelled transform stops here
                    # instead of writing on into entities.json after the
                    # activity has gone — where a retry on this pod reopens
                    # and truncates the same file under it.
                    if not written % 1024:
                        raise_if_offload_abandoned()
            return written

        # Offloaded as one hop: this maps every raw record for the entity —
        # JSON parse, the connector's mapper_fn (which builds pyatlan asset
        # objects), serialise, write — with no await anywhere in the loop.
        # A large table's records.json therefore holds the event loop for the
        # whole transform, which stops the auto-heartbeat and gets a healthy
        # activity killed on heartbeat_timeout. Every SQL connector inherits
        # this method, so the fix applies fleet-wide (ADR-0010).
        #
        # No framework progress hook inside `_map_records`, deliberately
        # (ADR-0018). The loop is line-by-line with no batch boundary to mark
        # at, and the never-per-record rule forbids the only boundary it has.
        # It does not need one: the whole map is a single `run_in_thread` hop,
        # which FND-290 wraps in an unbounded auto-hold — so the stall watchdog
        # is vouched for across the entire transform by mechanism 2 rather than
        # mechanism 1. That leaves the 24h backstop as its only bound, which is
        # the ADR's accepted residual for opaque offloaded work and is exactly
        # what warn mode is meant to surface with an observed duration.
        count = await run_in_thread(_map_records)

        logger.info("Transformed %s: %d records", entity_type, count)
        # Emit a FileReference to entities.json so the activity
        # interceptor uploads it after the transform activity finishes.
        # Downstream publish / upload tasks then consume the durable ref
        # via the same materialise contract — no local-FS coupling.
        #
        # ``storage_path`` is pinned to the canonical
        # ``<run_prefix>/transformed/<entity>/entities.json`` so the
        # downstream publish step finds the file at the entity-typed
        # path it expects. Without this pin the interceptor would
        # auto-generate a ``file_refs/<uuid>.json`` key — publish
        # discovers transformed assets by walking
        # ``transformed/<entity>/`` prefixes, so anything that only
        # lands under ``file_refs/`` is invisible to it and the
        # entity gets archived as "removed from source" on the next
        # publish run. ``get_object_store_prefix`` strips
        # ``TEMPORARY_PATH`` from the local path to derive the
        # matching object-store key; see ``extract_entity`` for the
        # corresponding canonical key on the raw side.
        #
        # Tier = RETAINED (vs. TRANSIENT on raw_file): the
        # transform → publish handoff can span an SDR → in-tenant
        # deployment boundary, so the ref must survive the SDR-side
        # workflow's auto-cleanup at run end. RETAINED keeps the
        # object-store key under the run-scoped ``artifacts/`` prefix
        # but skips ``cleanup_storage``'s tracked-TRANSIENT sweep.
        transformed_ref = (
            FileReference(
                local_path=str(output_file),
                storage_path=get_object_store_prefix(str(output_file)),
                tier=StorageTier.RETAINED,
            )
            if count > 0
            else None
        )
        return TransformOutput(
            typename=entity_type,
            total_record_count=count,
            transformed_file=transformed_ref,
        )
