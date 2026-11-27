"""The object store as the sink for observability exports (FND-4260).

``application_sdk.observability`` exports logs, metrics, traces and sizing rows
to the object store, and storage logs through ``application_sdk.observability``.
So observability does not import storage: the process bootstrap installs this
sink into it, through :func:`install_observability_store_sink`.

Each call looks up ``upload_file`` and ``create_store_from_binding_with_put_attrs``
on their modules at call time, so a patch of either reaches the export.
"""

from __future__ import annotations

from application_sdk import storage
from application_sdk.observability.observability import install_store_sink
from application_sdk.storage import binding
from application_sdk.storage.errors import (
    StorageBindingBrokenError,
    StorageBindingNotFoundError,
)
from application_sdk.storage.ops import BoundStore

__all__ = ["install_observability_store_sink"]


class _ObjectStoreSink:
    """:class:`~application_sdk.observability.observability.ObservabilityStoreSink`
    over ``application_sdk.storage``."""

    def open_store(self, binding_name: str, components_dir: str) -> BoundStore:
        store, put_attrs = binding.create_store_from_binding_with_put_attrs(
            binding_name, components_dir=components_dir
        )
        return BoundStore(store, put_attrs)

    async def upload_file(self, key: str, local_path: str, store: BoundStore) -> None:
        await storage.upload_file(key, local_path, store=store, write_sidecar=False)

    def is_transient_binding_error(self, exc: Exception) -> bool:
        # A missing component or an unsubstituted placeholder is a boot race
        # that heals once the component is written; anything else will not.
        return isinstance(exc, (StorageBindingNotFoundError, StorageBindingBrokenError))


def install_observability_store_sink() -> None:
    """Install the object store as the sink for observability exports.

    Called by the process bootstrap: ``application_sdk.main`` on import,
    ``create_worker``, ``create_app_handler_service`` and the daprd log
    forwarder. A process that starts some other way and wants its logs,
    metrics and traces in the object store calls it once at startup.
    """
    install_store_sink(_ObjectStoreSink())
