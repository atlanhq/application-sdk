"""Type definitions for payload-safe contracts.

Provides types and utilities for contracts that stay within Temporal's 2MB payload limit.

Key types:
- FileReference: Reference to externally-stored data
- GitReference: Reference to a Git repository
- ConnectionRef: Typed replacement for connection: dict[str, Any]
- MaxItems: Constraint marker for bounded collections
- BoundedList/BoundedDict: Type aliases with size bounds
- Lazy: Marker for a FileReference field the interceptor must not auto-download
- AssetArtifact: Marker for a FileReference field whose declaration is a typed model
"""

from __future__ import annotations

import dataclasses
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from application_sdk.common._listing import safe_list_directory
from application_sdk.contracts.types_errors import RunPrefixRequiredError
from application_sdk.credentials.ref import CredentialRef

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


class StoreTarget(StrEnum):
    """Which object store an operation addresses.

    Atlan deployments come in two shapes and the SDK talks to a different
    store in each:

    * ``DEPLOYMENT`` — the store bound to this deployment
      (``context.storage``).  This is where the activity interceptor
      persists and materialises every ``FileReference`` on the
      task-to-task path, in **both** deployment shapes.
    * ``UPSTREAM`` — Atlan's own bucket (``context.upstream_storage``),
      configured only in SDR deployments and the destination
      ``App.upload`` / ``App.download`` route to when it is present.
      Falls back to the deployment store when no upstream binding
      exists, mirroring ``App.upload``'s routing.

    Pick ``DEPLOYMENT`` to assert something about what the interceptor
    wrote; pick ``UPSTREAM`` to assert something about what the publish
    app will read.
    """

    DEPLOYMENT = "deployment"
    UPSTREAM = "upstream"


class StorageTier(StrEnum):
    """Storage lifecycle tier for a ``FileReference``.

    Controls where the file is stored and whether it is cleaned up automatically
    at the end of a workflow run.

    * ``TRANSIENT``: stored under ``file_refs/`` and deleted by
      ``App.cleanup_storage()`` at the end of every run.  This is the default
      and is appropriate for intermediary files that are only needed between
      tasks.
    * ``RETAINED``: stored under the run-scoped artifacts prefix
      (``artifacts/apps/{app}/workflows/{wf_id}/{run_id}/file_refs/``) and
      **not** deleted by default cleanup.  Use this when you want the file to
      survive the run for post-run investigation.  It can still be removed with
      ``StorageCleanupInput(include_prefix_cleanup=True)``.
    * ``PERSISTENT``: stored under ``persistent-artifacts/`` and never deleted
      by cleanup.  Use this for files that must survive across multiple runs.
    """

    TRANSIENT = "transient"
    RETAINED = "retained"
    PERSISTENT = "persistent"

    # ------------------------------------------------------------------
    # Canonical tier → object-store path helpers
    #
    # These are the *single source of truth* for tier-based path generation.
    # All other modules (storage/reference.py, app/base.py, etc.) delegate
    # here instead of duplicating conditional logic.
    #
    # Adding methods to a StrEnum does NOT affect Temporal serde — the
    # payload converter only uses the string value ("transient" etc.).
    # ------------------------------------------------------------------

    def upload_prefix(self, *, run_prefix: str = "", app_name: str = "") -> str:
        """Return the base object-store prefix for ``App.upload`` at this tier.

        This prefix is used as the destination root when no explicit
        ``storage_path`` is given to :meth:`~application_sdk.app.base.App.upload`.

        * ``TRANSIENT``  → ``file_refs`` (cleaned at end of run)
        * ``RETAINED``   → *run_prefix* — requires *run_prefix*
        * ``PERSISTENT`` → ``persistent-artifacts/apps/{app_name}``
        """
        if self is StorageTier.TRANSIENT:
            return "file_refs"
        if self is StorageTier.RETAINED:
            if not run_prefix:
                raise RunPrefixRequiredError()
            return run_prefix
        # PERSISTENT
        return (
            f"persistent-artifacts/apps/{app_name}"
            if app_name
            else "persistent-artifacts"
        )

    def _make_file_ref_path(
        self, *, suffix: str = "", run_prefix: str = "", app_name: str = ""
    ) -> str:
        """Return a unique single-file object-store key for auto-persisted ``FileReference`` objects.

        * ``TRANSIENT``  → ``file_refs/{uuid}{suffix}``
        * ``RETAINED``   → ``{run_prefix}/file_refs/{uuid}{suffix}``
        * ``PERSISTENT`` → ``persistent-artifacts/apps/{app_name}/file_refs/{uuid}{suffix}``

        .. warning::
            Uses ``uuid.uuid4()`` internally, which is **not** deterministic.
            This method must only be called from within a Temporal **activity**
            (i.e. a ``@task``-decorated function or a utility called from one).
            Calling it from ``@workflow.defn`` code will violate Temporal's
            sandbox non-determinism restrictions.

        Args:
            suffix: File extension including the leading dot (e.g. ``".parquet"``).
            run_prefix: Run-scoped base prefix.  Required for ``RETAINED``.
            app_name: Application name.  Used by ``PERSISTENT``.
        """
        base = self._file_ref_base(run_prefix=run_prefix, app_name=app_name)
        return f"{base}/{uuid.uuid4().hex}{suffix}"

    def _make_file_ref_prefix(self, *, run_prefix: str = "", app_name: str = "") -> str:
        """Return a unique directory prefix for auto-persisted ``FileReference`` directories.

        Identical to :meth:`_make_file_ref_path` with a trailing slash and no
        suffix — used for directory uploads.

        .. warning::
            Uses ``uuid.uuid4()`` internally — **activity-context only**.
            See :meth:`_make_file_ref_path` for details.
        """
        base = self._file_ref_base(run_prefix=run_prefix, app_name=app_name)
        return f"{base}/{uuid.uuid4().hex}/"

    def _file_ref_base(self, *, run_prefix: str = "", app_name: str = "") -> str:
        """Return the base prefix under which ``file_refs/{uid}`` paths are stored.

        ``TRANSIENT`` uses *run_prefix* when one is available so that the
        resulting storage key is tenant-scoped — production deployments
        (Atlan blob-storage gateway) only permit writes under
        ``artifacts/`` and ``persistent-artifacts/``. The activity
        interceptor always supplies *run_prefix* via
        ``persist_file_refs(..., output_path=build_output_path())``, so
        in any real Temporal-driven workflow TRANSIENT refs land at
        ``{run_prefix}/file_refs/{uid}``. The bare-prefix fallback is
        kept for callers that legitimately have no run context (local
        scripts, unit tests, ad-hoc utilities) — those run against
        local stores with no path policy, so the bare prefix is
        harmless there.

        ``RETAINED`` continues to require *run_prefix*: it's a
        contract-level invariant that RETAINED refs must be
        run-scoped because they survive cleanup-at-end-of-run.
        """
        if self is StorageTier.TRANSIENT:
            # Use run-scoped prefix when available; fall back to bare
            # ``file_refs`` for ad-hoc callers without a run context.
            return f"{run_prefix}/file_refs" if run_prefix else "file_refs"
        if self is StorageTier.RETAINED:
            if not run_prefix:
                raise RunPrefixRequiredError()
            return f"{run_prefix}/file_refs"
        # PERSISTENT
        return (
            f"persistent-artifacts/apps/{app_name}/file_refs"
            if app_name
            else "persistent-artifacts/file_refs"
        )


@dataclasses.dataclass(frozen=True)
class MaxItems:
    """Constraint marker indicating maximum collection size.

    Use with Annotated to declare bounded collections in contracts:

        class MyInput(Input):
            settings: Annotated[dict[str, str], MaxItems(100)]
            items: Annotated[list[Record], MaxItems(1000)]
    """

    limit: int
    """Maximum number of items allowed in the collection."""


class Lazy:
    """Marker: this FileReference field is NOT auto-materialized before the activity runs.

    Use with ``Annotated`` on any ``FileReference | None`` field whose data is
    too large to download unconditionally, or that the activity may not always
    need:

        class MyInput(Input):
            heavy_artifact: Annotated[FileReference | None, Lazy()] = None
            light_manifest: FileReference | None = None  # eager (default)

    Lazy fields are left as durable ``FileReference`` objects in the activity
    input.  Call ``await fetch(ref, store)`` from ``storage.reference`` inside
    the activity to download on demand — the sidecar fast-path means repeated
    calls are cheap if the file is already on disk.
    """

    __slots__ = ()


class AssetArtifact:
    """Marker: this ``FileReference`` field's declaration **is** a typed model.

    Use with ``Annotated`` on a ``FileReference``-bearing field whose artifact is
    Atlas assets written by the SDK itself:

        class ExtractionOutput(Output):
            transformed_files: Annotated[
                list[FileReference], MaxItems(1000), AssetArtifact()
            ] = Field(default_factory=list)

    An artifact declaration normally comes from the app's generated
    ``artifact_schemas.json`` — a hand-authored field map, keyed by field name
    (ADR-0020).  For an Atlas asset artifact that is the wrong instrument, and
    :mod:`application_sdk.validation.sources` already says why: the asset case is
    500+ types and 4000+ properties with diamond inheritance, so *the model
    already is the declaration* and nothing should be authored at all.  The
    generated envelope carries field maps only, so an app asked to declare one of
    these fields can produce nothing better than a partial restatement of
    ``Asset`` — strictly weaker than the check the SDK runs on the same bytes
    elsewhere.

    This marker is what tells the two readers of that declaration apart:

    * :func:`~application_sdk.validation.interceptor.validate_artifacts` builds a
      :class:`~application_sdk.validation.sources.ModelSource` for a marked field
      instead of a
      :class:`~application_sdk.validation.sources.ContractSource`, so the
      hand-off is checked against the full ``pyatlan_v9`` ``Asset`` backbone.
    * :func:`~application_sdk.app._artifact_schema_guard.warn_undeclared_artifact_schemas`
      treats a marked field as declared, so the field it cannot usefully describe
      is not one the app is asked to hand-write — including at v4.0, when a
      missing boundary declaration becomes an error.

    A marked field is therefore *more* strongly checked than a hand-declared one,
    never less: the marker swaps a field map for an executable model, it never
    turns a check off.  A ``ContractSource`` envelope that also exists for a
    marked field is ignored at the boundary — the model wins, because it is the
    stronger of the two and a field cannot have two declarations.

    Marking is not restricted to SDK contracts: a connector whose own contract
    field carries SDK-transformed Atlas assets can mark it and get the same
    check.  What the marker asserts is a fact about the *bytes* — one Atlas
    entity per line, in the nested format ``Asset.validate()`` reads.
    """

    __slots__ = ()

    @staticmethod
    def model() -> type:
        """The typed model this field's records are validated against.

        ``pyatlan_v9``'s ``Asset``, imported here rather than at module scope:
        this module is on the import path of every contract in the SDK, and
        ``pyatlan_v9`` must stay off the import path of an app that never
        touches transformed assets (the same rule
        :func:`application_sdk.app.base._warn_on_invalid_transformed_assets`
        follows at the upload boundary).

        Raises:
            ImportError: ``pyatlan_v9`` is not installed.  Both readers treat
                that as the SDK's own failure — a ``validator_broken`` outcome
                that never blocks a hand-off — rather than as a finding against
                the app.
        """
        from pyatlan_v9.model.assets import (  # noqa: PLC0415 — deferred: pyatlan_v9 stays off every contract's import path
            Asset,
        )

        return Asset


def asset_artifact_marker(contract: type | None, field: str) -> AssetArtifact | None:
    """The :class:`AssetArtifact` marker on ``contract``'s *field*, or ``None``.

    **One reader, two callers, on purpose.**  The activity interceptor and the
    registration-time guard both act on this answer, and they have to act on the
    *same* answer: a field the guard exempts from hand-declaration but the
    interceptor does not model-validate is a boundary nobody checks at all.  Read
    the same way ``Lazy`` is read in
    :mod:`application_sdk.storage.file_ref_sync` — off the field's
    ``Annotated`` metadata, which is where a contract records facts about a field
    that Pydantic itself has no opinion about.

    Args:
        contract: The model class declaring *field*, or ``None`` for a bare
            reference reached with no owning contract — which carries no
            annotation and therefore no marker.
        field: The field name.

    Returns:
        The marker instance, or ``None`` when the field is absent, unmarked, or
        not a Pydantic field at all.  Never raises: this sits under two advisory
        paths, neither of which may break a hand-off or a registration.
    """
    if contract is None:
        return None
    model_fields = getattr(contract, "model_fields", None)
    if not isinstance(model_fields, dict):
        return None
    field_info = model_fields.get(field)
    metadata = getattr(field_info, "metadata", None) or ()
    for entry in metadata:
        if isinstance(entry, AssetArtifact):
            return entry
    return None


def asset_artifact_fields(contract: type) -> frozenset[str]:
    """Every field on *contract* carrying the :class:`AssetArtifact` marker.

    ``model_fields`` resolves the full MRO, so a field marked on an SDK base is
    reported for every subclass that inherits it — which is what makes a
    connector's ``MyExtractionOutput(ExtractionOutput)`` exempt without the
    connector restating anything.

    The set form exists for callers that need the whole picture rather than one
    field: the conformance suite's static mirror of this fact is drift-tested
    against it, so the rule that suppresses a review finding and the marker that
    suppresses the runtime warning cannot come to disagree.
    """
    model_fields = getattr(contract, "model_fields", None)
    if not isinstance(model_fields, dict):
        return frozenset()
    return frozenset(
        name
        for name in model_fields
        if asset_artifact_marker(contract, name) is not None
    )


BoundedList = Annotated[list[T], MaxItems]
"""Bounded list type. Use: Annotated[list[T], MaxItems(N)]"""

BoundedDict = Annotated[dict[K, V], MaxItems]
"""Bounded dict type. Use: Annotated[dict[K, V], MaxItems(N)]"""


class FileReference(BaseModel, frozen=True):
    """Reference to externally-stored data (for large payloads).

    Use this instead of embedding large data directly in Input/Output.
    Store the actual data in a file/blob storage and pass only this reference.

    Temporal has a 2MB payload limit. Large data (files, blobs, large datasets)
    should be stored externally and referenced via FileReference.

    Attributes:
        local_path: Local filesystem path to the file or directory.
        storage_path: Object-store key (single file) or prefix (directory).
        is_durable: ``True`` when the data has been uploaded to the object
            store and ``storage_path`` is set.
        file_count: Number of files this reference covers.  Defaults to 1
            for single-file references; set to the total number of files for
            directory uploads/downloads.
        tier: Storage lifecycle tier.  Controls where the file is stored and
            whether it is automatically cleaned up at the end of a run.
            Defaults to ``StorageTier.TRANSIENT`` (cleaned up automatically).
            Set to ``StorageTier.RETAINED`` to keep the file under the
            run-scoped prefix for post-run investigation, or
            ``StorageTier.PERSISTENT`` to keep it indefinitely under
            ``persistent-artifacts/``.
        auto_materialize: When ``True`` (default), the activity interceptor
            will transparently upload (persist) ephemeral refs after a task
            completes and download (materialize) durable refs before the
            next task runs.  Set to ``False`` to opt out — the app then
            owns the upload/download lifecycle.  Useful when an app needs
            custom retry/timeout/streaming behavior the interceptor cannot
            provide (e.g. multi-GB files, lazy-streaming reads, or
            deferred materialization).
    """

    local_path: str | None = None
    storage_path: str | None = None
    is_durable: bool = False
    file_count: int = 1
    tier: StorageTier = StorageTier.TRANSIENT
    auto_materialize: bool = True

    @staticmethod
    def from_local(
        path: str | Path,
        *,
        tier: StorageTier = StorageTier.TRANSIENT,
    ) -> FileReference:
        """Create an ephemeral FileReference from a local filesystem path.

        For a directory, ``file_count`` is computed as the number of regular
        files under the tree (recursively); for a single file it is ``1``.
        Non-existent paths fall back to the default ``file_count=1`` so this
        helper is safe to call before the file has been written.

        Args:
            path: Local file or directory path.
            tier: Storage lifecycle tier. Defaults to
                :attr:`StorageTier.TRANSIENT` for one-off intermediary
                files. Pass :attr:`StorageTier.RETAINED` when the ref
                belongs to a workflow run and must land under the
                run-scoped ``artifacts/`` prefix (this is what the
                ``UploadInput`` / ``App.upload`` path uses by default
                and what the Atlan blob-storage gateway permits in
                production deployments).

        Returns:
            An ephemeral ``FileReference`` (``is_durable=False``) with
            ``local_path`` and ``tier`` set.
        """
        p = Path(path) if not isinstance(path, Path) else path
        # Best-effort file_count. We swallow OSError so the constructor
        # stays usable from inside Temporal sandbox where filesystem
        # inspection may not be permitted.
        file_count = 1
        try:
            if p.is_dir():
                file_count = len(safe_list_directory(p))
        except OSError:  # conformance: ignore[E009] sandbox-safe fallback to 1
            file_count = 1
        return FileReference(
            local_path=str(p),
            file_count=file_count,
            tier=tier,
        )


class GitReference(BaseModel, frozen=True):
    """Reference to a Git repository for workflow inputs.

    Temporal-safe data carrier for specifying a git repo to clone.
    Checkout precedence: commit > tag > branch.
    """

    repo_url: str
    branch: str = "main"
    path: str = ""
    tag: str = ""
    commit: str = ""
    credential: CredentialRef | None = None


class ConnectionAttributes(BaseModel, frozen=True):
    """Minimal normalized attributes from an AE Connection object.

    Python-side uses snake_case (qualified_name, admin_users, etc.).
    Pydantic auto-converts to/from camelCase (qualifiedName, adminUsers) on
    serialization/deserialization via alias_generator + populate_by_name.

    ``extra="allow"`` ensures unknown AE fields (connector-specific attributes)
    survive round-trips without requiring SDK changes.
    """

    qualified_name: str = ""
    name: str = ""
    connector_name: str | None = None
    category: str | None = None
    admin_users: list[str] = Field(default_factory=list)
    admin_roles: list[str] = Field(default_factory=list)
    admin_groups: list[str] = Field(default_factory=list)

    model_config = ConfigDict(
        frozen=True,
        extra="allow",
        alias_generator=to_camel,
        populate_by_name=True,
    )


class ConnectionRef(BaseModel, frozen=True):
    """Typed replacement for ``connection: dict[str, Any]`` in Temporal contracts.

    Mirrors the AE wire shape:
        ``{"typeName": "Connection", "attributes": {"qualifiedName": ..., "name": ...}}``

    Python-side uses snake_case (``type_name``, ``attributes.qualified_name``).
    Pydantic auto-serializes to camelCase (``typeName``, ``qualifiedName``) via
    ``alias_generator=to_camel`` + ``serialize_by_alias=True``.

    ``extra="allow"`` on both layers ensures unknown AE fields survive
    round-trips without requiring SDK changes.

    Example::

        # From AE wire payload (camelCase):
        ref = ConnectionRef.model_validate({
            "typeName": "Connection",
            "attributes": {
                "qualifiedName": "default/snowflake/1234567890",
                "name": "My Snowflake",
                "adminUsers": ["user-1"],
            },
        })

        # Python-side access (snake_case):
        print(ref.type_name)                    # "Connection"
        print(ref.attributes.qualified_name)    # "default/snowflake/1234567890"
        print(ref.attributes.admin_users)       # ["user-1"]

        # Temporal payload (camelCase, via serialize_by_alias):
        ref.model_dump(by_alias=True)
        # {"typeName": "Connection", "attributes": {"qualifiedName": ..., ...}}
    """

    type_name: str = Field(default="Connection")
    attributes: ConnectionAttributes = Field(default_factory=ConnectionAttributes)

    model_config = ConfigDict(
        frozen=True,
        extra="allow",
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    @staticmethod
    def from_connection(conn: Any) -> ConnectionRef:
        """Convert a pyatlan_v9 Connection (msgspec.Struct) to ConnectionRef.

        The pyatlan_v9 struct is flat (all attributes at top level with camelCase
        keys); ``to_atlas_format`` converts it to the nested Atlas API shape
        ``{"typeName": ..., "attributes": {...}}`` that ConnectionRef expects.

        Args:
            conn: A pyatlan_v9 Connection msgspec.Struct instance.

        Returns:
            A ConnectionRef with normalized snake_case fields.
        """
        from pyatlan_v9.model.transform import (  # type: ignore[import]  # noqa: PLC0415 — optional dep: pyatlan_v9 (vendored module not always available)
            to_atlas_format,
        )

        return ConnectionRef.model_validate(to_atlas_format(conn))

    def to_connection(self) -> Any:
        """Convert back to a pyatlan_v9 Connection (msgspec.Struct).

        ``model_dump(by_alias=True)`` produces the nested Atlas API shape
        ``{"typeName": ..., "attributes": {...}}``; ``from_atlas_format``
        flattens that back into the pyatlan_v9 struct.

        Returns:
            A pyatlan_v9 Connection msgspec.Struct instance.
        """
        from pyatlan_v9.model.transform import (  # type: ignore[import]  # noqa: PLC0415 — optional dep: pyatlan_v9 (vendored module not always available)
            from_atlas_format,
        )

        return from_atlas_format(self.model_dump(by_alias=True))
