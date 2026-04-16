"""Object storage module — direct obstore-backed I/O, no Dapr sidecar needed.

Public API:
    create_local_store(root_path)    → LocalStore (for local dev / testing)
    create_memory_store()            → MemoryStore (for unit tests)
    create_store_from_binding(...)   → ObjectStore parsed from Dapr component YAML
    normalize_key(key)               → str  (v2-compatible path normalisation)
    upload_file(key, local_path)     → str  (streaming upload, returns sha256)
    download_file(key, local_path)   → str | None  (streaming download)
    delete(key, store=None)          → bool           (alias: delete_file)
    exists(key, store=None)          → bool
    delete_prefix(prefix, store=None) → int  (returns count deleted)
    list_keys(prefix, suffix=...)    → list[str]      (alias: list_files)

For directory upload/download, use App.upload / App.download (framework tasks)
or call application_sdk.storage.transfer.upload / .download directly.

When ``store`` is omitted all I/O functions resolve the store from the current
infrastructure context (set via ``set_infrastructure()`` in ``main.py``),
mirroring the v2 behaviour where the store was transparent to callers.

Pass ``store=my_store`` to target a specific store.
All I/O functions normalise keys by default (see normalize_key).  Pass
``normalize=False`` to use a key exactly as supplied.

Migration from v2:
    objectstore.get_content(key)            →  download_file(key, local_path)
    objectstore.upload_bytes(key, data)     →  upload_file(key, local_path)
    objectstore.delete_file(key)            →  delete(key)  or  delete_file(key)
    objectstore.exists(key)                 →  exists(key)
    objectstore.list_files(prefix)          →  list_keys(prefix)  or  list_files(prefix)
    objectstore.delete_prefix(prefix)       →  delete_prefix(prefix)
    objectstore.upload_prefix(src, prefix)  →  App.upload(UploadInput(local_path=src, storage_path=prefix))
    objectstore.download_prefix(prefix, dst) →  App.download(DownloadInput(storage_path=prefix, local_path=dst))
"""

from __future__ import annotations

from application_sdk.storage.batch import (
    delete_prefix,
    download_prefix,
    list_keys,
    upload_file_from_bytes,
    upload_prefix,
)
from application_sdk.storage.binding import create_store_from_binding
from application_sdk.storage.errors import (
    StorageConfigError,
    StorageError,
    StorageNotFoundError,
    StoragePermissionError,
)
from application_sdk.storage.factory import create_local_store, create_memory_store
from application_sdk.storage.ops import (
    delete,
    delete_file,
    download_file,
    exists,
    normalize_key,
    upload_file,
)

#: v2-compatible alias
list_files = list_keys

__all__ = [
    # Store factories
    "create_store_from_binding",
    "create_local_store",
    "create_memory_store",
    # Core ops
    "upload_file",
    "upload_file_from_bytes",
    "upload_prefix",
    "download_file",
    "download_prefix",
    "delete",
    "delete_prefix",
    "exists",
    "list_keys",
    "normalize_key",
    # v2-compatible aliases
    "delete_file",
    "list_files",
    # Errors
    "StorageError",
    "StorageNotFoundError",
    "StoragePermissionError",
    "StorageConfigError",
]
