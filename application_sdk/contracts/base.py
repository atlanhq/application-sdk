"""Input/output contract definitions for Apps and tasks.

This module provides base classes for defining typed contracts between
Apps, tasks, and their callers. Using these base classes ensures:

1. Type safety - All inputs/outputs are typed dataclasses
2. Payload safety - Validated against Temporal's 2MB payload limit
3. Serialization - Works seamlessly with Temporal's data converters
4. Backwards compatibility - Add new fields with defaults, never change signatures

Typing zone rules
-----------------
The SDK has two distinct typing zones with different serialization requirements.
Choosing the wrong base type for a zone is a correctness error, not just style.

**Temporal zone** — ``Input``, ``Output``, ``HeartbeatDetails``, ``Record``:
    MUST be plain ``@dataclass``. They serialize natively via
    ``dataclasses.asdict()`` / ``value_to_type()`` through Temporal's data
    converter. Do NOT subclass with ``pydantic.BaseModel`` — that would
    require a custom ``pydantic_data_converter`` and break the existing
    serialization contract.

**External boundary zone** — handler request/response schemas, manifest
    contracts, pub/sub event payloads, and any type whose shape is owned by
    external consumers (HTTP clients, pub/sub subscribers, external config):
    MUST be ``pydantic.BaseModel``. Pydantic gives boundary validation on
    ingress (``model_validate`` / ``model_validate_json``), direct JSON
    serialization on egress (``model_dump_json()``), and automatic OpenAPI
    schema generation. Do NOT use plain dataclasses here — the
    ``dataclasses.asdict`` + ``JSONResponse`` pattern loses all boundary
    validation.

    Event types (``contracts/events.py``) belong in this zone even though they
    may transit Temporal. The contract is defined by external pub/sub consumers,
    not the Temporal execution engine, so Pydantic is correct. Call
    ``event.model_dump()`` before passing to Temporal — the resulting dict
    serialises cleanly through Temporal's JSON data converter.

    See ``application_sdk/handler/manifest.py`` and
    ``application_sdk/contracts/events.py`` for canonical examples.

Usage:
    from dataclasses import dataclass
    from application_sdk.contracts import Input, Output

    @dataclass
    class MyAppInput(Input):
        source_path: str
        batch_size: int = 100  # Optional with default

    @dataclass
    class MyAppOutput(Output):
        records_processed: int
        status: str

Evolution:
    To add new fields, always provide defaults:

    @dataclass
    class MyAppInput(Input):
        source_path: str
        batch_size: int = 100
        # New field - MUST have default for backwards compatibility
        retry_count: int = 3

    Never remove fields or change their types - this breaks running workflows.
"""

from dataclasses import MISSING, dataclass, fields, is_dataclass
from enum import StrEnum
from typing import (
    Annotated,
    Any,
    ClassVar,
    Protocol,
    TypeVar,
    get_args,
    get_origin,
    get_type_hints,
    runtime_checkable,
)

from application_sdk.contracts.types import MaxItems  # noqa: TC001
from application_sdk.errors import CONTRACT_VALIDATION, PAYLOAD_SAFETY, ErrorCode

# =============================================================================
# Serializable Enum Base Class
# =============================================================================


class SerializableEnum(StrEnum):
    """Base class for enums that need to be serialized through Temporal.

    Enums that inherit from this class are automatically JSON serializable
    because they inherit from both ``str`` and ``Enum``. The enum value is used
    as the serialized string representation.

    This solves the "Object of type XEnum is not JSON serializable" error
    that occurs when using regular enums in Temporal activity/workflow payloads.

    Usage:
        class MyStatus(SerializableEnum):
            PENDING = "pending"
            RUNNING = "running"
            COMPLETED = "completed"
            FAILED = "failed"

        @dataclass
        class MyOutput(Output):
            status: MyStatus  # Works with Temporal serialization

    The enum values should be strings that match the desired serialized form.
    When deserialized, Temporal will reconstruct the enum from the string value.
    """

    @staticmethod
    def _generate_next_value_(  # type: ignore[override]
        name: str, start: int, count: int, last_values: list[str]
    ) -> str:
        """Auto-generate value from name in lowercase.

        This allows defining enums without explicit values:

            class Status(SerializableEnum):
                PENDING = auto()  # value will be "pending"
                RUNNING = auto()  # value will be "running"
        """
        return name.lower()


T = TypeVar("T")


# =============================================================================
# Base Contract Classes
# =============================================================================


@dataclass
class Input:
    """Base class for all input contracts (Apps and tasks).

    All App run() methods and task methods must accept exactly one
    parameter that extends this class. This ensures:

    1. Clear contracts between callers and callees
    2. Backwards-compatible evolution via dataclass fields with defaults
    3. Proper serialization through Temporal

    Example:
        @dataclass
        class ExtractInput(Input):
            source_url: str
            max_records: int = 1000

    Config Hash:
        The config_hash() method computes a stable hash of configuration fields,
        useful for identifying equivalent input configurations across runs
        (e.g., for checkpoint storage keys). Override _config_hash_exclude in
        subclasses to exclude volatile/per-run fields.
    """

    _config_hash_exclude: ClassVar[set[str]] = set()
    """Fields to exclude from config_hash(). Override in subclasses to list
    volatile/per-run fields that shouldn't affect checkpoint identity."""

    def _log_summary(self) -> dict[str, Any]:
        """Return a dict of field values safe for logging.

        Excludes:
        - Underscore-prefixed fields
        - Fields with sensitive names (credential, secret, password, token, key, auth)
        - Truncates long strings (>200 chars) and large lists (show count only)
        """
        SENSITIVE = {"credential", "secret", "password", "token", "key", "auth"}

        def is_sensitive(name: str) -> bool:
            n = name.lower()
            return any(p in n for p in SENSITIVE)

        def safe_value(v: Any) -> Any:
            if isinstance(v, str):
                if len(v) > 200:
                    return f"{v[:100]}...({len(v)} chars)"
                return v
            if isinstance(v, list):
                if len(v) > 5:
                    return [*v[:2], f"({len(v)} total)"]
                return v
            if isinstance(v, dict):
                return f"{{{len(v)} keys}}"
            if is_dataclass(v) and not isinstance(v, type):
                if hasattr(v, "_log_summary"):
                    return v._log_summary()
                return f"{{{type(v).__name__}}}"
            return v

        result: dict[str, Any] = {}
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            if is_sensitive(f.name):
                continue
            result[f.name] = safe_value(getattr(self, f.name))
        return result

    def summary(self) -> str | None:
        """Return a human-readable summary for Temporal UI.

        Override in subclasses to provide contextual summaries that appear
        next to activity names in the Temporal event history timeline.

        Returns:
            A short summary string, or None for no summary.
        """
        return None

    def config_hash(self, extra_exclude: set[str] | None = None) -> str:
        """Compute a stable hash of this input's configuration fields.

        Only includes fields whose values DIFFER from their defaults. This
        ensures the hash is stable as the Input class evolves: adding new
        fields with defaults won't change existing hashes.

        Excludes:
        - Underscore-prefixed fields
        - Fields listed in _config_hash_exclude (class variable)
        - Fields in extra_exclude parameter
        - Fields at their default value (key for evolution stability)

        Returns:
            16-character hex string (64 bits of SHA-256).
        """
        import hashlib
        import json
        from dataclasses import asdict as dc_asdict

        exclude = set(self.__class__._config_hash_exclude)
        if extra_exclude:
            exclude |= extra_exclude

        data: dict[str, Any] = {}
        for f in fields(self):
            if f.name in exclude or f.name.startswith("_"):
                continue
            value = getattr(self, f.name)
            # Skip fields at their default value (evolution-stable)
            if f.default is not MISSING and value == f.default:
                continue
            if f.default_factory is not MISSING and value == f.default_factory():  # type: ignore[misc]
                continue
            data[f.name] = value

        def default_serializer(obj: Any) -> Any:
            if is_dataclass(obj) and not isinstance(obj, type):
                return dc_asdict(obj)
            return str(obj)

        content = json.dumps(data, sort_keys=True, default=default_serializer)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def __init_subclass__(
        cls, allow_unbounded_fields: bool = False, **kwargs: Any
    ) -> None:
        """Validate payload safety when Input subclasses are defined.

        This hook runs at class definition time (import time) and validates
        that the subclass doesn't use types that could exceed Temporal's
        payload limits.

        Args:
            allow_unbounded_fields: Set to True to opt out of payload safety
                validation. Use with caution - large payloads can fail at runtime.
                Example: class MyInput(Input, allow_unbounded_fields=True): ...
            **kwargs: Additional class creation keyword arguments.
        """
        super().__init_subclass__(**kwargs)

        # Store the flag for potential later checks
        if allow_unbounded_fields:
            cls._allow_unbounded_fields = True  # type: ignore[attr-defined]

        # Validate payload safety, skipping internal framework fields
        validate_payload_safety(cls, skip_fields=set())


@dataclass
class Output:
    """Base class for all output contracts (Apps and tasks).

    All App run() methods and task methods must return exactly one
    instance of a class that extends this class. This ensures:

    1. Clear contracts between callers and callees
    2. Backwards-compatible evolution via dataclass fields with defaults
    3. Proper serialization through Temporal

    Example:
        @dataclass
        class ExtractOutput(Output):
            records_extracted: int
            checkpoint_path: str
            status: str = "completed"
    """

    def __init_subclass__(
        cls, allow_unbounded_fields: bool = False, **kwargs: Any
    ) -> None:
        """Validate payload safety when Output subclasses are defined.

        This hook runs at class definition time (import time) and validates
        that the subclass doesn't use types that could exceed Temporal's
        payload limits.

        Args:
            allow_unbounded_fields: Set to True to opt out of payload safety
                validation. Use with caution - large payloads can fail at runtime.
                Example: class MyOutput(Output, allow_unbounded_fields=True): ...
            **kwargs: Additional class creation keyword arguments.
        """
        super().__init_subclass__(**kwargs)

        # Store the flag for potential later checks
        if allow_unbounded_fields:
            cls._allow_unbounded_fields = True  # type: ignore[attr-defined]

        validate_payload_safety(cls)


@dataclass
class HeartbeatDetails:
    """Base class for heartbeat progress contracts.

    Defines the progress state that a long-running task persists
    periodically so Temporal can resume from the last checkpoint on retry.

    Unlike Input/Output (which describe what enters/leaves a task),
    HeartbeatDetails captures internal mid-task progress: position markers
    for resuming and counters for observability.

    Serialization: Temporal serializes these to JSON. On retry, Temporal
    returns them as plain dicts — use self.get_heartbeat_details(cls) to
    reconstruct the typed dataclass automatically.

    Evolution Rules (same as Input/Output):
        ADD new fields with default values
        NEVER remove fields
        NEVER change field types or names

    Example:
        @dataclass
        class LoadTypeHeartbeat(HeartbeatDetails):
            chunk_idx: int         # Resume position (required, no default)
            loaded_count: int = 0  # Progress tracking
    """

    pass


@dataclass
class Record:
    """Base class for domain records passed between Apps.

    Records represent domain data (e.g., products, users, events) that flow
    through pipelines and between Apps. Unlike Input/Output which define
    method contracts, Record defines the structure of domain data.

    All records must have an 'id' field for identification and tracking.

    Example:
        @dataclass
        class ProductRecord(Record):
            name: str
            price: float
            category: str
            in_stock: bool = True
    """

    id: str
    """Unique identifier for this record."""


# =============================================================================
# Validation and Utilities
# =============================================================================


class ContractValidationError(Exception):
    """Raised when a contract validation fails."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CONTRACT_VALIDATION

    def __init__(
        self,
        message: str,
        *,
        contract_type: str | None = None,
        field_name: str | None = None,
        expected_type: str | None = None,
        actual_type: str | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.contract_type = contract_type
        self.field_name = field_name
        self.expected_type = expected_type
        self.actual_type = actual_type
        self._error_code = error_code

    @property
    def error_code(self) -> ErrorCode:
        """Structured error code for monitoring and alerting."""
        return (
            self._error_code
            if self._error_code is not None
            else self.DEFAULT_ERROR_CODE
        )

    def __str__(self) -> str:
        parts = [f"[{self.error_code.code}] {self.message}"]
        if self.contract_type:
            parts.append(f"contract_type={self.contract_type}")
        if self.field_name:
            parts.append(f"field={self.field_name}")
        if self.expected_type:
            parts.append(f"expected={self.expected_type}")
        if self.actual_type:
            parts.append(f"actual={self.actual_type}")
        return " | ".join(parts)


class PayloadSafetyError(ContractValidationError):
    """Raised when a contract field uses a type that risks exceeding payload limits.

    Temporal has a 2MB payload limit for workflow/activity inputs and outputs.
    This error is raised at class definition time when a field uses a type that
    could grow unbounded and potentially exceed this limit.

    Forbidden types:
    - bytes/bytearray: Binary data should use FileReference
    - list[T] without MaxItems: Unbounded lists can grow arbitrarily
    - dict[K, V] without MaxItems: Unbounded dicts can grow arbitrarily
    - Any: Cannot validate size constraints

    To fix:
    - Use FileReference for large/binary data
    - Use Annotated[list[T], MaxItems(N)] for bounded lists
    - Use Annotated[dict[K, V], MaxItems(N)] for bounded dicts
    - Use allow_unbounded_fields=True class keyword to opt out (use with caution)
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = PAYLOAD_SAFETY

    def __init__(
        self, cls_name: str, field_name: str, field_type: type, reason: str
    ) -> None:
        message = (
            f"Field '{field_name}' in {cls_name} uses unsafe type {field_type}. "
            f"{reason}\n\n"
            f"To fix:\n"
            f"  - Use FileReference for large/binary data\n"
            f"  - Use Annotated[list[T], MaxItems(N)] for bounded lists\n"
            f"  - Use Annotated[dict[K,V], MaxItems(N)] for bounded dicts\n"
            f"  - Use allow_unbounded_fields=True class keyword to opt out (use with caution)"
        )
        super().__init__(message, contract_type=cls_name, field_name=field_name)
        self.field_type = field_type
        self.reason = reason


# =============================================================================
# Payload Safety Validation
# =============================================================================


def _is_unbounded_collection(field_type: type, collection_type: type) -> bool:
    """Check if a type is an unbounded collection (no MaxItems annotation).

    Args:
        field_type: The type to check.
        collection_type: The collection type to check for (e.g., dict, list).

    Returns:
        True if the type is an unbounded collection of the specified type.
    """
    origin = get_origin(field_type)

    # Plain collection without bounds
    if origin is collection_type:
        return True

    # Check Annotated types for MaxItems constraint
    if origin is Annotated:
        args = get_args(field_type)
        # Check if any annotation arg is MaxItems
        for arg in args[1:]:
            if isinstance(arg, MaxItems):
                return False
        # Annotated but no MaxItems - check the inner type
        if args:
            return _is_unbounded_collection(args[0], collection_type)

    return False


def _is_unbounded_dict(field_type: type) -> bool:
    """Check if a type is an unbounded dict (no MaxItems annotation)."""
    return _is_unbounded_collection(field_type, dict)


def _is_unbounded_list(field_type: type) -> bool:
    """Check if a type is an unbounded list (no MaxItems annotation)."""
    return _is_unbounded_collection(field_type, list)


def _is_forbidden_type(field_type: type) -> tuple[bool, str]:
    """Check if a type is forbidden in contracts.

    Args:
        field_type: The type to check.

    Returns:
        Tuple of (is_forbidden, reason string).
    """
    origin = get_origin(field_type)

    # Check for Any
    if field_type is Any:
        return True, "Any type cannot be validated and may contain unbounded data"

    # Check for bytes/bytearray
    if field_type in (bytes, bytearray):
        return True, "Binary data should use FileReference instead"
    if origin in (bytes, bytearray):
        return True, "Binary data should use FileReference instead"

    # Check for unbounded collections (handles both plain and Annotated types)
    if _is_unbounded_dict(field_type):
        return True, "Unbounded dict may exceed payload limits"
    if _is_unbounded_list(field_type):
        return True, "Unbounded list may exceed payload limits"

    # For Annotated types, we already handled the collection bounds check above.
    # If we get here, the Annotated type has MaxItems so the collection is bounded.
    # We need to check the TYPE ARGS of the inner collection for forbidden types,
    # but NOT re-check if the inner collection itself is unbounded.
    if origin is Annotated:
        args = get_args(field_type)
        if args:
            # First arg is the actual type (e.g., list[dict[str, Any]])
            inner_type = args[0]
            inner_origin = get_origin(inner_type)
            # For bounded collections (list/dict with MaxItems), check their type args
            if inner_origin in (list, dict):
                inner_args = get_args(inner_type)
                for arg in inner_args:
                    if isinstance(arg, type) or get_origin(arg) is not None:
                        is_forbidden, reason = _is_forbidden_type(arg)
                        if is_forbidden:
                            return True, reason
            else:
                # For other Annotated types, recurse normally
                return _is_forbidden_type(inner_type)
        return False, ""

    # Recursively check generic args (e.g., list[dict[str, Any]])
    if origin is not None:
        args = get_args(field_type)
        for arg in args:
            # Skip non-type args (like constraint instances)
            if isinstance(arg, type) or get_origin(arg) is not None:
                is_forbidden, reason = _is_forbidden_type(arg)
                if is_forbidden:
                    return True, reason

    return False, ""


def validate_payload_safety(cls: type, *, skip_fields: set[str] | None = None) -> None:
    """Validate that all fields in a contract use payload-safe types.

    This function checks each field in a dataclass for types that could
    potentially exceed Temporal's 2MB payload limit.

    Args:
        cls: The dataclass to validate.
        skip_fields: Field names to skip (for internal framework fields).

    Raises:
        PayloadSafetyError: If any field uses an unsafe type.
    """
    # Check for opt-out decorator
    if getattr(cls, "_allow_unbounded_fields", False):
        return

    skip = skip_fields or set()

    # Get type hints, handling forward references
    # include_extras=True preserves Annotated metadata (like MaxItems)
    try:
        hints = get_type_hints(cls, include_extras=True)
    except NameError:
        # Forward reference couldn't be resolved - skip validation
        # This can happen during module initialization
        return

    for field_name, field_type in hints.items():
        # Skip internal fields and explicitly skipped fields
        if field_name in skip or field_name.startswith("_"):
            continue

        is_forbidden, reason = _is_forbidden_type(field_type)
        if is_forbidden:
            raise PayloadSafetyError(cls.__name__, field_name, field_type, reason)


@runtime_checkable
class InputContract(Protocol):
    """Protocol marker for App input contracts.

    All App inputs should be dataclasses. This protocol ensures
    they can be validated and serialized.
    """

    pass


@runtime_checkable
class OutputContract(Protocol):
    """Protocol marker for App output contracts.

    All App outputs should be dataclasses. This protocol ensures
    they can be validated and serialized.
    """

    pass


def validate_is_dataclass(cls: type, context: str = "contract") -> None:
    """Validate that a class is a dataclass.

    Args:
        cls: The class to validate.
        context: Description for error messages.

    Raises:
        ContractValidationError: If cls is not a dataclass.
    """
    if not is_dataclass(cls):
        raise ContractValidationError(
            f"{context} must be a dataclass, got {cls.__name__}",
            contract_type=context,
        )


def get_contract_fields(cls: type) -> dict[str, type]:
    """Get the fields and their types from a dataclass contract.

    Args:
        cls: A dataclass type.

    Returns:
        Dictionary mapping field names to their types.

    Raises:
        ContractValidationError: If cls is not a dataclass.
    """
    validate_is_dataclass(cls)
    hints = get_type_hints(cls)
    return {f.name: hints.get(f.name, Any) for f in fields(cls)}


def has_default(cls: type, field_name: str) -> bool:
    """Check if a dataclass field has a default value.

    Args:
        cls: A dataclass type.
        field_name: Name of the field to check.

    Returns:
        True if the field has a default value.
    """
    validate_is_dataclass(cls)
    from dataclasses import MISSING
    from dataclasses import fields as dc_fields

    for f in dc_fields(cls):
        if f.name == field_name:
            return f.default is not MISSING or f.default_factory is not MISSING  # type: ignore[misc]
    return False


def is_backwards_compatible(old_cls: type, new_cls: type) -> tuple[bool, list[str]]:
    """Check if a new contract version is backwards compatible with the old.

    Backwards compatibility rules:
    - All fields from old must exist in new with same types
    - New fields must have default values

    Args:
        old_cls: The previous contract version.
        new_cls: The new contract version.

    Returns:
        Tuple of (is_compatible, list of incompatibility reasons).
    """
    validate_is_dataclass(old_cls, "old contract")
    validate_is_dataclass(new_cls, "new contract")

    old_fields = get_contract_fields(old_cls)
    new_fields = get_contract_fields(new_cls)
    issues: list[str] = []

    # Check all old fields exist in new with compatible types
    for name, old_type in old_fields.items():
        if name not in new_fields:
            issues.append(f"Field '{name}' was removed")
        elif new_fields[name] != old_type:
            issues.append(
                f"Field '{name}' type changed from {old_type} to {new_fields[name]}"
            )

    # Check new fields have defaults
    for name in new_fields:
        if name not in old_fields and not has_default(new_cls, name):
            issues.append(f"New field '{name}' does not have a default value")

    return len(issues) == 0, issues


@dataclass(frozen=True)
class ContractMetadata:
    """Metadata about a contract for registration and discovery."""

    name: str
    version: str
    cls: type
    is_input: bool

    # Reserved for future schema evolution tracking
    schema_hash: str | None = None
    deprecated: bool = False
    deprecation_message: str | None = None
