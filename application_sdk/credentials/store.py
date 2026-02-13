"""Workflow-scoped credential storage in worker process memory.

The WorkerCredentialStore provides:
- Workflow-isolated credential storage
- Thread-safe access
- Automatic cleanup on workflow completion
- No persistence (credentials live only during workflow execution)
"""

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.credentials.handle import CredentialHandle
    from application_sdk.credentials.http_client import AuthenticatedHTTPClient

logger = get_logger(__name__)


@dataclass
class WorkflowCredentialEntry:
    """Credential data for a single workflow.

    Attributes:
        credential_mapping: Mapping from slot name to credential GUID.
        handles: Mapping from slot name to CredentialHandle.
        http_clients: Mapping from slot name to AuthenticatedHTTPClient.
        refresh_token: Optional refresh token for long-running workflows.
        initialized: Whether credentials have been bootstrapped.
    """

    credential_mapping: Dict[str, str] = field(default_factory=dict)
    handles: Dict[str, "CredentialHandle"] = field(default_factory=dict)
    http_clients: Dict[str, "AuthenticatedHTTPClient"] = field(default_factory=dict)
    refresh_token: Optional[str] = None
    initialized: bool = False
    raw_credentials: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class WorkerCredentialStore:
    """Workflow-scoped credential storage in worker process memory.

    Thread-safe, singleton storage for credential handles during workflow execution.
    Credentials are automatically cleaned up when workflows complete.

    Key properties:
    - Isolated per workflow (keyed by workflow_id)
    - Never serialized out of worker
    - Shared across activities in same workflow
    - Automatic cleanup on completion/failure

    Usage:
        >>> store = WorkerCredentialStore.get_instance()
        >>> store.set_handle(workflow_id, "database", handle)
        >>> handle = store.get_handle(workflow_id, "database")
        >>> store.cleanup(workflow_id)  # On workflow completion

    This class is designed to be used by the CredentialInterceptor
    and CredentialContext. Application code should not interact
    with it directly.
    """

    _instance: Optional["WorkerCredentialStore"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        """Initialize the credential store.

        Should not be called directly - use get_instance() instead.
        """
        self._store: Dict[str, WorkflowCredentialEntry] = {}
        self._store_lock = threading.RLock()

    @classmethod
    def get_instance(cls) -> "WorkerCredentialStore":
        """Get the singleton instance of the credential store.

        Thread-safe singleton access.

        Returns:
            The WorkerCredentialStore instance.
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (for testing only)."""
        with cls._lock:
            if cls._instance is not None:
                # Clean up all workflows
                for workflow_id in list(cls._instance._store.keys()):
                    cls._instance.cleanup(workflow_id)
                cls._instance = None

    # ========================================================================
    # Entry Management
    # ========================================================================

    def get_or_create_entry(self, workflow_id: str) -> WorkflowCredentialEntry:
        """Get or create an entry for a workflow.

        Args:
            workflow_id: The workflow ID.

        Returns:
            The WorkflowCredentialEntry for the workflow.
        """
        with self._store_lock:
            if workflow_id not in self._store:
                self._store[workflow_id] = WorkflowCredentialEntry()
            return self._store[workflow_id]

    def get_entry(self, workflow_id: str) -> Optional[WorkflowCredentialEntry]:
        """Get an entry for a workflow if it exists.

        Args:
            workflow_id: The workflow ID.

        Returns:
            The WorkflowCredentialEntry or None if not found.
        """
        with self._store_lock:
            return self._store.get(workflow_id)

    def has_entry(self, workflow_id: str) -> bool:
        """Check if an entry exists for a workflow.

        Args:
            workflow_id: The workflow ID.

        Returns:
            True if entry exists.
        """
        with self._store_lock:
            return workflow_id in self._store

    # ========================================================================
    # Credential Mapping
    # ========================================================================

    def set_credential_mapping(self, workflow_id: str, mapping: Dict[str, str]) -> None:
        """Set the credential mapping for a workflow.

        Args:
            workflow_id: The workflow ID.
            mapping: Mapping from slot name to credential GUID.
        """
        with self._store_lock:
            entry = self.get_or_create_entry(workflow_id)
            entry.credential_mapping = mapping

    def get_credential_mapping(self, workflow_id: str) -> Dict[str, str]:
        """Get the credential mapping for a workflow.

        Args:
            workflow_id: The workflow ID.

        Returns:
            Mapping from slot name to credential GUID.
        """
        with self._store_lock:
            entry = self.get_entry(workflow_id)
            return entry.credential_mapping if entry else {}

    # ========================================================================
    # Handle Management
    # ========================================================================

    def set_handle(
        self, workflow_id: str, slot_name: str, handle: "CredentialHandle"
    ) -> None:
        """Store a credential handle for a workflow slot.

        Args:
            workflow_id: The workflow ID.
            slot_name: Name of the credential slot.
            handle: The CredentialHandle to store.
        """
        with self._store_lock:
            entry = self.get_or_create_entry(workflow_id)
            entry.handles[slot_name] = handle

    def get_handle(
        self, workflow_id: str, slot_name: str
    ) -> Optional["CredentialHandle"]:
        """Get a credential handle for a workflow slot.

        Args:
            workflow_id: The workflow ID.
            slot_name: Name of the credential slot.

        Returns:
            The CredentialHandle or None if not found.
        """
        with self._store_lock:
            entry = self.get_entry(workflow_id)
            if entry and entry.initialized:
                return entry.handles.get(slot_name)
            return None

    def get_all_handles(self, workflow_id: str) -> Dict[str, "CredentialHandle"]:
        """Get all credential handles for a workflow.

        Args:
            workflow_id: The workflow ID.

        Returns:
            Mapping from slot name to CredentialHandle.
        """
        with self._store_lock:
            entry = self.get_entry(workflow_id)
            return dict(entry.handles) if entry else {}

    # ========================================================================
    # Raw Credentials (for protocol operations)
    # ========================================================================

    def set_raw_credentials(
        self, workflow_id: str, slot_name: str, credentials: Dict[str, Any]
    ) -> None:
        """Store raw credentials for a workflow slot.

        Used by protocol engine for operations like refresh.

        Args:
            workflow_id: The workflow ID.
            slot_name: Name of the credential slot.
            credentials: Raw credential values.
        """
        with self._store_lock:
            entry = self.get_or_create_entry(workflow_id)
            entry.raw_credentials[slot_name] = credentials

    def get_raw_credentials(
        self, workflow_id: str, slot_name: str
    ) -> Optional[Dict[str, Any]]:
        """Get raw credentials for a workflow slot.

        Args:
            workflow_id: The workflow ID.
            slot_name: Name of the credential slot.

        Returns:
            Raw credential values or None if not found.
        """
        with self._store_lock:
            entry = self.get_entry(workflow_id)
            if entry:
                return entry.raw_credentials.get(slot_name)
            return None

    # ========================================================================
    # HTTP Client Management
    # ========================================================================

    def set_http_client(
        self, workflow_id: str, slot_name: str, client: "AuthenticatedHTTPClient"
    ) -> None:
        """Store an HTTP client for a workflow slot.

        Args:
            workflow_id: The workflow ID.
            slot_name: Name of the credential slot.
            client: The AuthenticatedHTTPClient to store.
        """
        with self._store_lock:
            entry = self.get_or_create_entry(workflow_id)
            entry.http_clients[slot_name] = client

    def get_http_client(
        self, workflow_id: str, slot_name: str
    ) -> Optional["AuthenticatedHTTPClient"]:
        """Get an HTTP client for a workflow slot.

        Args:
            workflow_id: The workflow ID.
            slot_name: Name of the credential slot.

        Returns:
            The AuthenticatedHTTPClient or None if not found.
        """
        with self._store_lock:
            entry = self.get_entry(workflow_id)
            if entry:
                return entry.http_clients.get(slot_name)
            return None

    # ========================================================================
    # Initialization State
    # ========================================================================

    def mark_initialized(self, workflow_id: str) -> None:
        """Mark credentials as initialized for a workflow.

        Args:
            workflow_id: The workflow ID.
        """
        with self._store_lock:
            entry = self.get_or_create_entry(workflow_id)
            entry.initialized = True

    def is_initialized(self, workflow_id: str) -> bool:
        """Check if credentials are initialized for a workflow.

        Args:
            workflow_id: The workflow ID.

        Returns:
            True if credentials have been bootstrapped.
        """
        with self._store_lock:
            entry = self.get_entry(workflow_id)
            return entry.initialized if entry else False

    # ========================================================================
    # Refresh Token Management
    # ========================================================================

    def set_refresh_token(self, workflow_id: str, token: str) -> None:
        """Store a refresh token for long-running workflows.

        Args:
            workflow_id: The workflow ID.
            token: The refresh token.
        """
        with self._store_lock:
            entry = self.get_or_create_entry(workflow_id)
            entry.refresh_token = token

    def get_refresh_token(self, workflow_id: str) -> Optional[str]:
        """Get the refresh token for a workflow.

        Args:
            workflow_id: The workflow ID.

        Returns:
            The refresh token or None if not set.
        """
        with self._store_lock:
            entry = self.get_entry(workflow_id)
            return entry.refresh_token if entry else None

    # ========================================================================
    # Cleanup
    # ========================================================================

    def cleanup(self, workflow_id: str) -> None:
        """Clean up all credentials for a workflow.

        This method:
        - Closes all HTTP clients
        - Cleans up all credential handles (including temp files)
        - Removes the workflow entry from the store

        Called automatically on workflow completion/failure.

        Args:
            workflow_id: The workflow ID.
        """
        with self._store_lock:
            entry = self._store.pop(workflow_id, None)

            if entry:
                # Close all HTTP clients
                for slot_name, client in entry.http_clients.items():
                    try:
                        # Note: close() might be async - we call it sync here
                        # The client should handle this gracefully
                        if hasattr(client, "close_sync"):
                            client.close_sync()
                    except Exception as e:
                        logger.warning(
                            f"Error closing HTTP client for slot '{slot_name}': {e}"
                        )

                # Clean up all handles (including temp files)
                for slot_name, handle in entry.handles.items():
                    try:
                        handle.cleanup()
                    except Exception as e:
                        logger.warning(
                            f"Error cleaning up handle for slot '{slot_name}': {e}"
                        )

                logger.debug(
                    f"Cleaned up credentials for workflow {workflow_id}",
                    extra={
                        "workflow_id": workflow_id,
                        "slots_cleaned": list(entry.handles.keys()),
                    },
                )

    def get_all_workflow_ids(self) -> List[str]:
        """Get all workflow IDs currently in the store.

        For debugging/monitoring only.

        Returns:
            List of workflow IDs.
        """
        with self._store_lock:
            return list(self._store.keys())
