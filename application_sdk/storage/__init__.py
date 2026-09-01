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

For directory upload/download, use App.upload / App.download (framework tasks)
or call application_sdk.storage.transfer.upload / .download directly.

When ``store`` is omitted all I/O functions resolve the store from the current
infrastructure context (set via ``set_infrastructure()`` in ``main.py``).

Pass ``store=my_store`` to target a specific store.
All I/O functions normalise keys by default (see normalize_key).  Pass
``normalize=False`` to use a key exactly as supplied.
"""

from __future__ import annotations

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
from application_sdk.storage.errors import (
    ObjectStorePreflightError,
    StorageBindingBrokenError,
    StorageBindingNotFoundError,
    StorageBucketRelocationError,
    StorageConfigError,
    StorageError,
    StorageIntegrityError,
    StorageNotFoundError,
    StoragePermissionError,
    StoragePreconditionError,
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
    upload_file,
)
from application_sdk.storage.preflight import (
    ObjectStoreCheckResult,
    check_object_store_access,
    check_run_storage_access,
    verify_object_store_access,
)

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
    "StorageIntegrityError",
    "StorageNotFoundError",
    "StoragePermissionError",
    "StorageBucketRelocationError",
    "StoragePreconditionError",
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
