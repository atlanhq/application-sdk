"""Opaque credential handle that prevents accidental exposure.

The CredentialHandle wraps credential values and blocks operations
that could expose secrets:
- dict(handle) - Blocked
- print(handle) - Shows redacted placeholder
- json.dumps(handle) - Blocked
- for k in handle - Blocked
- handle.keys() - Blocked
- handle.values() - Blocked
- handle.items() - Blocked

Only handle.get(field) is allowed, and access is audit-logged.
"""

import os
from typing import Any, Dict, Iterator, List, Optional, SupportsIndex

from application_sdk.credentials.exceptions import CredentialHandleError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class CredentialHandle:
    """Opaque wrapper around credential values.

    Prevents accidental exposure by blocking dangerous operations.
    Only provides .get(field) method with audit logging.

    This class is designed to be used with SDK clients that need
    individual credential fields but should not have access to
    dump or iterate over all credentials.

    Example:
        >>> handle = await ctx.credentials.materialize("snowflake")
        >>>
        >>> # Allowed - individual field access
        >>> conn = snowflake.connector.connect(
        ...     account=handle.get("account"),
        ...     user=handle.get("user"),
        ...     password=handle.get("password"),  # audit-logged
        ... )
        >>>
        >>> # Blocked - prevents accidental exposure
        >>> dict(handle)  # CredentialHandleError
        >>> print(handle)  # Shows "<CredentialHandle [REDACTED]>"
        >>> json.dumps(handle)  # CredentialHandleError

    Attributes:
        slot_name: Name of the credential slot.
        workflow_id: ID of the workflow using this handle.
        protocol_type: Type of protocol used for this credential.
    """

    __slots__ = (
        "_slot_name",
        "_values",
        "_workflow_id",
        "_protocol_type",
        "_temp_files",
        "_accessed_fields",
    )

    def __init__(
        self,
        slot_name: str,
        values: Dict[str, Any],
        workflow_id: str,
        protocol_type: str,
        temp_files: Optional[List[str]] = None,
    ):
        """Initialize credential handle.

        Args:
            slot_name: Name of the credential slot.
            values: Actual credential values (stored internally).
            workflow_id: ID of the workflow using this handle.
            protocol_type: Type of protocol used for this credential.
            temp_files: List of temporary files to clean up.
        """
        object.__setattr__(self, "_slot_name", slot_name)
        object.__setattr__(self, "_values", values)
        object.__setattr__(self, "_workflow_id", workflow_id)
        object.__setattr__(self, "_protocol_type", protocol_type)
        object.__setattr__(self, "_temp_files", temp_files or [])
        object.__setattr__(self, "_accessed_fields", set())

    @property
    def slot_name(self) -> str:
        """Get the credential slot name."""
        return self._slot_name

    @property
    def workflow_id(self) -> str:
        """Get the workflow ID."""
        return self._workflow_id

    @property
    def protocol_type(self) -> str:
        """Get the protocol type."""
        return self._protocol_type

    def get(self, field: str, default: Any = None) -> Any:
        """Get a credential field value.

        This is the ONLY way to access credential values.
        Access is audit-logged for security tracking.

        Args:
            field: Name of the field to retrieve.
            default: Default value if field is not found.

        Returns:
            The field value or default.

        Example:
            >>> password = handle.get("password")
            >>> timeout = handle.get("timeout", 30)  # With default
        """
        value = self._values.get(field, default)
        self._audit_log_access(field, found=field in self._values)
        self._accessed_fields.add(field)
        return value

    def has_field(self, field: str) -> bool:
        """Check if a field exists without logging.

        Use this to check field existence before accessing.

        Args:
            field: Name of the field to check.

        Returns:
            True if field exists.
        """
        return field in self._values

    def _audit_log_access(self, field: str, found: bool) -> None:
        """Log credential field access for audit trail.

        Args:
            field: Name of the field accessed.
            found: Whether the field was found.
        """
        logger.debug(
            "Credential field accessed",
            extra={
                "workflow_id": self._workflow_id,
                "slot_name": self._slot_name,
                "field": field,
                "field_found": found,
                "protocol_type": self._protocol_type,
            },
        )

    def cleanup(self) -> None:
        """Clean up any temporary resources.

        Removes temporary certificate files and clears internal state.
        Called automatically when workflow completes.
        """
        for temp_file in self._temp_files:
            try:
                if os.path.exists(temp_file):
                    os.unlink(temp_file)
            except OSError as e:
                logger.warning(f"Failed to clean up temp file {temp_file}: {e}")

        # Clear internal state
        object.__setattr__(self, "_temp_files", [])

    def add_temp_file(self, path: str) -> None:
        """Register a temporary file for cleanup.

        Args:
            path: Path to the temporary file.
        """
        self._temp_files.append(path)

    # ========================================================================
    # BLOCKED OPERATIONS - Prevent accidental credential exposure
    # ========================================================================

    def __repr__(self) -> str:
        """Return redacted representation."""
        return f"<CredentialHandle slot='{self._slot_name}' fields=[REDACTED]>"

    def __str__(self) -> str:
        """Return redacted string."""
        return self.__repr__()

    def __iter__(self) -> Iterator:
        """Block iteration over credentials."""
        raise CredentialHandleError(
            "Cannot iterate over CredentialHandle. "
            "Use handle.get(field) to access individual fields."
        )

    def __len__(self) -> int:
        """Block len() to prevent exposure of field count."""
        raise CredentialHandleError(
            "Cannot get length of CredentialHandle. "
            "Use handle.has_field(field) to check for specific fields."
        )

    def __contains__(self, item: Any) -> bool:
        """Block 'in' operator."""
        raise CredentialHandleError(
            "Cannot use 'in' operator with CredentialHandle. "
            "Use handle.has_field(field) instead."
        )

    def __getitem__(self, key: str) -> Any:
        """Block subscript access."""
        raise CredentialHandleError(
            f"Cannot use subscript access handle['{key}']. "
            f"Use handle.get('{key}') instead."
        )

    def __setitem__(self, key: str, value: Any) -> None:
        """Block setting items."""
        raise CredentialHandleError("CredentialHandle is read-only.")

    def __delitem__(self, key: str) -> None:
        """Block deleting items."""
        raise CredentialHandleError("CredentialHandle is read-only.")

    def __setattr__(self, name: str, value: Any) -> None:
        """Block setting attributes."""
        raise CredentialHandleError("CredentialHandle is read-only.")

    def keys(self) -> Any:
        """Block keys() method."""
        raise CredentialHandleError(
            "Cannot access keys of CredentialHandle. "
            "Use handle.get(field) for specific fields."
        )

    def values(self) -> Any:
        """Block values() method."""
        raise CredentialHandleError(
            "Cannot access values of CredentialHandle. "
            "Use handle.get(field) for specific fields."
        )

    def items(self) -> Any:
        """Block items() method."""
        raise CredentialHandleError(
            "Cannot access items of CredentialHandle. "
            "Use handle.get(field) for specific fields."
        )

    def __reduce__(self) -> Any:
        """Block pickling."""
        raise CredentialHandleError("Cannot pickle CredentialHandle.")

    def __reduce_ex__(self, protocol: SupportsIndex) -> Any:
        """Block pickling with protocol."""
        raise CredentialHandleError("Cannot pickle CredentialHandle.")

    def __getstate__(self) -> Any:
        """Block getting state for pickling."""
        raise CredentialHandleError("Cannot get state of CredentialHandle.")

    def __setstate__(self, state: Any) -> None:
        """Block setting state from pickle."""
        raise CredentialHandleError("Cannot set state of CredentialHandle.")


# ============================================================================
# JSON Serialization Protection
# ============================================================================


def _credential_handle_json_encoder(obj: Any) -> Any:
    """Custom JSON encoder that blocks CredentialHandle serialization.

    This is used as a fallback for json.dumps() calls.
    """
    if isinstance(obj, CredentialHandle):
        raise CredentialHandleError(
            "Cannot JSON serialize CredentialHandle. "
            "Use handle.get(field) to access individual fields, "
            "then serialize those values."
        )
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# Patch json module to block CredentialHandle serialization
# This is done at import time to ensure protection is always active
def _patch_json_encoder() -> None:
    """Patch json.JSONEncoder to block CredentialHandle serialization."""
    import json

    _original_default = json.JSONEncoder.default

    def _patched_default(self: json.JSONEncoder, o: Any) -> Any:
        if isinstance(o, CredentialHandle):
            raise CredentialHandleError(
                "Cannot JSON serialize CredentialHandle. "
                "Use handle.get(field) to access individual fields."
            )
        return _original_default(self, o)

    json.JSONEncoder.default = _patched_default  # type: ignore[method-assign]


# Apply the patch when this module is imported
_patch_json_encoder()
