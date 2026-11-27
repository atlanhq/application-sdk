"""Object storage module — direct obstore-backed I/O, no Dapr sidecar needed.

Public API:
    create_local_store(root_path)                      → LocalStore (for local dev / testing)
    create_memory_store()                              → MemoryStore (for unit tests)
    create_store_from_binding(...)                     → ObjectStore parsed from Dapr component YAML
    create_store_from_binding_optional(...)            → ObjectStore | None (None if component absent)
    create_store_from_binding_with_put_attrs(...)      → (ObjectStore, put_attrs | None)
    read_binding_secret_refs(name)                     → BindingSecretRefs  (secretKeyRef + auth.secretStore)
    set_fetched_binding_secrets(name, secrets)         → publish startup-fetched secrets for sync callers
    SecretMap / BindingSecretRefs                      → public shapes for the above
    normalize_key(key)                                 → str  (path normalisation)
    upload_file(key, local_path)      → str  (streaming upload, returns sha256)
    upload_bytes(key, data)           → str  (the byte-put primitive, returns sha256)
    download_file(key, local_path)    → str | None  (streaming download)
    download_file_chunked(key, local_path) → str | None  (parallel range GETs for
        large files; resumable + version-pinned — prefer for GB-class objects)
    get_file_meta(key, store=None)    → (size, e_tag) | None  (single HEAD)
    delete(key, store=None)           → bool
    exists(key, store=None)           → bool
    delete_prefix(prefix, store=None) → int  (returns count deleted)
    list_keys(prefix, suffix=...)     → list[str]
    list_keys_with_meta(prefix, ...)  → list[(key, size, e_tag)]
    list_data_keys(prefix, ...)       → list[str]  (sidecars excluded)
    list_data_keys_with_meta(prefix)  → list[(key, size, e_tag)]  (sidecars excluded)
    list_data_objects(prefix, ...)    → list[DataObject]  (adds per-object has_sidecar)
    is_sidecar_key(key)               → bool  (single source of truth; SIDECAR_SUFFIX)

Transfer integrity (FND-306): every upload validates what landed in the store
and records a ``{key}.sha256`` sidecar; every download validates the bytes it
wrote and, when a sidecar exists, that they hash to what the producer recorded.
A mismatch raises ``StorageIntegrityError`` (non-retryable) instead of letting a
truncated artifact reach a parser. See ``application_sdk.storage.integrity``.

Which entry point to use
------------------------
Pick by what you hold and where it goes. Every row except ``put_json`` ends
in the same primitives — ``upload_file`` / ``upload_bytes`` for writes,
``download_file`` / ``download_file_chunked`` for reads — so integrity
(FND-306) is the same whichever of those you call. ``put_json`` is the
exception: a plain PUT for small configs, with no post-write size check and
no ``.sha256`` sidecar.

=====================================  ============================================
You have / want                        Use
=====================================  ============================================
Bytes in memory → one key              ``upload_bytes(key, data)``
A JSON value → one key                 ``put_json(key, obj)`` (no readback,
                                       no sidecar)
A local file → one key                 ``upload_file(key, local_path)``
One key → a local file                 ``download_file(key, local_path)``
One GB-class key → a local file        ``download_file_chunked(key, local_path)``
A directory, from a ``@task``          ``App.upload`` / ``App.download`` (adds
                                       validation and ``FileReference`` tagging)
A ``FileReference``, outside ``App``   ``transfer.upload`` / ``transfer.download``
A state directory mirrored as-is       ``upload_prefix`` / ``download_prefix``
(RocksDB, DuckDB, a parquet dataset)   (no local markers, no manifest)
A customer's own bucket                ``CloudStore`` (its ``upload`` /
                                       ``upload_bytes`` / ``upload_dir`` /
                                       ``download`` run on the primitives and
                                       engines above, with sidecars off)
=====================================  ============================================

``upload_file_from_bytes`` is a deprecated alias of ``upload_bytes``.

When ``store`` is omitted all I/O functions resolve the store from the current
infrastructure context (set via ``set_infrastructure()`` in ``main.py``).

Pass ``store=my_store`` to target a specific store.
All I/O functions normalise keys by default (see normalize_key).  Pass
``normalize=False`` to use a key exactly as supplied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from application_sdk._install import lazy_getattr

# Eager: the read and write path. These ship in the api distribution's
# ``obstore`` extra, so the API host's config and file routes call the worker's
# own primitives. Everything else here (Dapr binding parsing, ``CloudStore``,
# the SDR preflight, the batch helpers) is worker code the host does not ship:
# it loads on first use, through ``_LAZY`` below.
from application_sdk.storage.errors import (
    ObjectStorePreflightError,
    StorageAuthError,
    StorageBindingBrokenError,
    StorageBindingNotFoundError,
    StorageBucketRelocationError,
    StorageConfigError,
    StorageDiskFullError,
    StorageError,
    StorageGatewayAuthUnavailableError,
    StorageGatewayNoRouteError,
    StorageIntegrityError,
    StorageNotFoundError,
    StoragePermissionError,
)
from application_sdk.storage.factory import create_local_store, create_memory_store
from application_sdk.storage.ops import (
    BoundStore,
    delete,
    download_file,
    download_file_chunked,
    exists,
    get_file_meta,
    normalize_key,
    put_json,
    upload_bytes,
    upload_file,
)

if TYPE_CHECKING:
    from application_sdk.storage.batch import (
        SIDECAR_SUFFIX,
        DataObject,
        delete_prefix,
        download_prefix,
        is_sidecar_key,
        list_data_keys,
        list_data_keys_with_meta,
        list_data_objects,
        list_keys,
        list_keys_with_meta,
        upload_file_from_bytes,
        upload_prefix,
    )
    from application_sdk.storage.binding import (
        BindingSecretRefs,
        SecretMap,
        create_store_from_binding,
        create_store_from_binding_optional,
        create_store_from_binding_with_put_attrs,
        read_binding_secret_refs,
        set_fetched_binding_secrets,
    )
    from application_sdk.storage.cloud import CloudStore
    from application_sdk.storage.preflight import (
        ObjectStoreCheckResult,
        check_object_store_access,
        check_run_storage_access,
        verify_object_store_access,
    )

#: Names served on first access: ``name -> (module, attribute)``.
_LAZY: dict[str, tuple[str, str]] = {
    "SIDECAR_SUFFIX": ("application_sdk.storage.batch", "SIDECAR_SUFFIX"),
    "DataObject": ("application_sdk.storage.batch", "DataObject"),
    "delete_prefix": ("application_sdk.storage.batch", "delete_prefix"),
    "download_prefix": ("application_sdk.storage.batch", "download_prefix"),
    "is_sidecar_key": ("application_sdk.storage.batch", "is_sidecar_key"),
    "list_data_keys": ("application_sdk.storage.batch", "list_data_keys"),
    "list_data_keys_with_meta": (
        "application_sdk.storage.batch",
        "list_data_keys_with_meta",
    ),
    "list_data_objects": ("application_sdk.storage.batch", "list_data_objects"),
    "list_keys": ("application_sdk.storage.batch", "list_keys"),
    "list_keys_with_meta": ("application_sdk.storage.batch", "list_keys_with_meta"),
    "upload_file_from_bytes": (
        "application_sdk.storage.batch",
        "upload_file_from_bytes",
    ),
    "upload_prefix": ("application_sdk.storage.batch", "upload_prefix"),
    "BindingSecretRefs": ("application_sdk.storage.binding", "BindingSecretRefs"),
    "SecretMap": ("application_sdk.storage.binding", "SecretMap"),
    "create_store_from_binding": (
        "application_sdk.storage.binding",
        "create_store_from_binding",
    ),
    "create_store_from_binding_optional": (
        "application_sdk.storage.binding",
        "create_store_from_binding_optional",
    ),
    "create_store_from_binding_with_put_attrs": (
        "application_sdk.storage.binding",
        "create_store_from_binding_with_put_attrs",
    ),
    "read_binding_secret_refs": (
        "application_sdk.storage.binding",
        "read_binding_secret_refs",
    ),
    "set_fetched_binding_secrets": (
        "application_sdk.storage.binding",
        "set_fetched_binding_secrets",
    ),
    "CloudStore": ("application_sdk.storage.cloud", "CloudStore"),
    "ObjectStoreCheckResult": (
        "application_sdk.storage.preflight",
        "ObjectStoreCheckResult",
    ),
    "check_object_store_access": (
        "application_sdk.storage.preflight",
        "check_object_store_access",
    ),
    "check_run_storage_access": (
        "application_sdk.storage.preflight",
        "check_run_storage_access",
    ),
    "verify_object_store_access": (
        "application_sdk.storage.preflight",
        "verify_object_store_access",
    ),
}

#: Submodules this package imported eagerly before it went lazy: still
#: attributes of the package on first access (``storage.binding``).
_EAGER_SUBMODULES = frozenset({"batch", "binding", "cloud", "preflight"})

__all__ = [
    # Cloud store (external customer buckets)
    "CloudStore",
    # Store factories
    "create_store_from_binding",
    "create_store_from_binding_optional",
    "create_local_store",
    "create_memory_store",
    # Store wrapper
    "BoundStore",
    # Core ops
    "upload_file",
    "upload_bytes",
    "upload_file_from_bytes",
    "upload_prefix",
    "download_file",
    "download_file_chunked",
    "download_prefix",
    "delete",
    "delete_prefix",
    "exists",
    "get_file_meta",
    "list_keys",
    "list_keys_with_meta",
    "list_data_keys",
    "list_data_keys_with_meta",
    "list_data_objects",
    "DataObject",
    "is_sidecar_key",
    "SIDECAR_SUFFIX",
    "normalize_key",
    "put_json",
    # Errors
    "StorageError",
    "StorageDiskFullError",
    "StorageIntegrityError",
    "StorageNotFoundError",
    "StoragePermissionError",
    "StorageAuthError",
    "StorageBucketRelocationError",
    "StorageGatewayAuthUnavailableError",
    "StorageGatewayNoRouteError",
    "StorageConfigError",
    "StorageBindingNotFoundError",
    "StorageBindingBrokenError",
    "ObjectStorePreflightError",
    "create_store_from_binding_with_put_attrs",
    # Binding secret refs (SDR startup wiring)
    "BindingSecretRefs",
    "SecretMap",
    "read_binding_secret_refs",
    "set_fetched_binding_secrets",
    # SDR preflight
    "verify_object_store_access",
    "check_object_store_access",
    "check_run_storage_access",
    "ObjectStoreCheckResult",
]


def __getattr__(name: str) -> object:
    return lazy_getattr(__name__, name, _LAZY, globals(), _EAGER_SUBMODULES)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY) | _EAGER_SUBMODULES)
