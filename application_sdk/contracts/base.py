"""Input/output contract definitions for Apps and tasks.

This module provides base classes for defining typed contracts between
Apps, tasks, and their callers. Using these base classes ensures:

1. Type safety - All inputs/outputs are typed Pydantic models
2. Payload safety - Validated against Temporal's 2MB payload limit
3. Serialization - Works seamlessly with Temporal's pydantic_data_converter
4. Backwards compatibility - Add new fields with defaults, never change signatures

All contracts use ``pydantic.BaseModel``. Temporal serialization is handled
by ``temporalio.contrib.pydantic.pydantic_data_converter``, which uses
``pydantic_core.to_json()`` / ``TypeAdapter.validate_json()`` natively.

**Temporal contracts** — ``Input``, ``Output``, ``HeartbeatDetails``, ``Record``:
    Subclass these to define typed payloads for App ``run()`` methods and
    ``@task``-decorated methods. They serialise through Temporal via
    ``pydantic_data_converter``.

**External boundary types** — handler request/response schemas, pub/sub event
    payloads, and any type whose shape is owned by external consumers (HTTP
    clients, pub/sub subscribers, external config): also ``pydantic.BaseModel``.
    Pydantic gives boundary validation on ingress (``model_validate`` /
    ``model_validate_json``), direct JSON serialization on egress
    (``model_dump_json()``), and automatic OpenAPI schema generation.

    Event types (``contracts/events.py``) belong here even though they may
    transit Temporal. The contract is defined by external pub/sub consumers,
    not the Temporal execution engine, so Pydantic is correct. Call
    ``event.model_dump()`` before passing to Temporal — the resulting dict
    serialises cleanly through the JSON data converter.

    See ``application_sdk/handler/manifest.py`` and
    ``application_sdk/contracts/events.py`` for canonical examples.

Usage:
    from application_sdk.contracts import Input, Output

    class MyAppInput(Input):
        source_path: str
        batch_size: int = 100  # Optional with default

    class MyAppOutput(Output):
        records_processed: int
        status: str

Evolution:
    To add new fields, always provide defaults:

    class MyAppInput(Input):
        source_path: str
        batch_size: int = 100
        # New field - MUST have default for backwards compatibility
        retry_count: int = 3

    Never remove fields or change their types - this breaks running workflows.
"""

import hashlib
import posixpath
import re
import warnings
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

import orjson
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_core import PydanticUndefined

from application_sdk.contracts.types import MaxItems
from application_sdk.errors import CONTRACT_VALIDATION, PAYLOAD_SAFETY, ErrorCode
from application_sdk.errors.leaves import InvalidInputError as _InvalidInputError
from application_sdk.observability.logger_adaptor import get_logger

_logger = get_logger(__name__)

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
# Output status enum
# =============================================================================


class OutputStatus(SerializableEnum):
    """Standard run-result status used on :class:`Output`.

    Set on every Output by default to :data:`OutputStatus.SUCCESS` so the
    field is non-breaking for existing connectors — subclasses can leave
    it alone if their run is unambiguously a pass, or set it to
    ``PARTIAL_SUCCESS`` / ``FAILURE`` when they want to surface a
    coarser-grained outcome alongside whatever domain-specific result
    fields they already return.

    Why an enum (vs. a free string): downstream consumers
    (notifications, retries, billing, post-run wiring) all need a
    closed vocabulary to switch on. A free string would let connectors
    invent ad-hoc states (``"ok"``, ``"warn"``, ``"degraded"``) that
    nothing else can interpret reliably. Additive evolution stays
    available — new statuses get added to this enum, the default
    stays ``SUCCESS``, so existing returners stay correct.

    Members:
        SUCCESS:          The run completed successfully.
        PARTIAL_SUCCESS:  The run completed with some non-fatal issues
                          (e.g. some entities were skipped due to
                          permissions, but the rest succeeded).
        FAILURE:          The run failed.
    """

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILURE = "failure"


# =============================================================================
# Base Contract Classes
# =============================================================================


class Input(BaseModel):
    """Base class for all input contracts (Apps and tasks).

    All App run() methods and task methods must accept exactly one
    parameter that extends this class. This ensures:

    1. Clear contracts between callers and callees
    2. Backwards-compatible evolution via Pydantic fields with defaults
    3. Proper serialization through Temporal

    Example:
        class ExtractInput(Input):
            source_url: str
            max_records: int = 1000

    Config Hash:
        The config_hash() method computes a stable hash of configuration fields,
        useful for identifying equivalent input configurations across runs
        (e.g., for checkpoint storage keys). Extend _config_hash_exclude in
        subclasses to exclude additional volatile/per-run fields.
    """

    model_config = ConfigDict()

    workflow_id: str = ""
    """Temporal workflow ID for the current run.

    Populated by the framework at dispatch time
    (``application_sdk.handler.service`` sets this before the workflow
    starts).  This is the **canonical way** for apps to access the
    workflow ID inside a task — read it from the input parameter rather
    than importing helpers from ``application_sdk.execution._temporal``::

        @task(timeout_seconds=300)
        async def extract(self, input: ExtractInput) -> ExtractOutput:
            wf_id = input.workflow_id   # ← do this
            # not: from application_sdk.execution._temporal.activity_utils
            #      import get_workflow_id

    See the ``atlan-openapi-app`` reference connector for the canonical
    pattern, including how to compose run-scoped object-store paths from
    ``input.workflow_id``.
    """

    correlation_id: str = ""
    """Caller-supplied correlation ID for tracing across systems."""

    _config_hash_exclude: ClassVar[set[str]] = {"workflow_id", "correlation_id"}
    """Fields to exclude from config_hash(). Extend in subclasses to add
    volatile/per-run fields that shouldn't affect checkpoint identity.
    config_hash() unions this set across the full MRO, so subclass entries
    are merged with (not replaced by) base-class exclusions."""

    _unknown_keys_seen: ClassVar[set[tuple[str, tuple[str, ...]]]] = set()
    """Dedup set for the unknown-key warning: at most one log line per
    (subclass name, sorted unknown keys) pair across the process lifetime."""

    @model_validator(mode="before")
    @classmethod
    def _warn_on_unknown_keys(cls, data: Any) -> Any:
        """Warn when the incoming payload contains keys not declared on the model.

        Pydantic silently drops unknown keys by default. That default masks
        contract drift between the SDK and upstream payload producers (the
        Automation Engine, Heracles, the Kill-Argo native dispatcher, or the
        manifest generator in app-contract-toolkit) — the symptom is an empty
        field at runtime with no trace of why.

        We don't rewrite the payload here. Normalization belongs at the source.
        This validator only emits a one-time ``WARNING`` per (class, extras)
        pair so the drift is visible in logs and the root cause can be fixed
        upstream.

        Kebab-case keys get an extra hint in the log line — they're the
        most common form of drift today (AE payloads built from kebab-case
        UI form field names). See ARUN-527.
        """
        if not isinstance(data, dict):
            return data
        if cls.model_config.get("extra") == "allow":
            return data
        known = {name for name in cls.model_fields}
        known.update(
            field.alias
            for field in cls.model_fields.values()
            if field.alias is not None
        )
        extras = sorted(
            str(k) for k in data if k not in known and not str(k).startswith("_")
        )
        if not extras:
            return data
        seen_key = (cls.__name__, tuple(extras))
        if seen_key in Input._unknown_keys_seen:
            return data
        Input._unknown_keys_seen.add(seen_key)
        kebab = [k for k in extras if "-" in k]
        hint = ""
        if kebab:
            mapped = {k: k.replace("-", "_") for k in kebab}
            hint = (
                f" Kebab-case keys detected — producer likely meant {mapped}."
                " Fix at the source (manifest generator / payload producer),"
                " not in the SDK."
            )
        _logger.warning(
            "Unknown keys in payload for %s, silently dropped by Pydantic: %s.%s",
            cls.__name__,
            extras,
            hint,
        )
        return data

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
            if isinstance(v, BaseModel):
                if hasattr(v, "_log_summary"):
                    return v._log_summary()
                return f"{{{type(v).__name__}}}"
            return v

        result: dict[str, Any] = {}
        for name in type(self).model_fields:
            if name.startswith("_"):
                continue
            if is_sensitive(name):
                continue
            result[name] = safe_value(getattr(self, name))
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

        exclude: set[str] = set()
        for cls in type(self).__mro__:
            exclude |= getattr(cls, "_config_hash_exclude", set())
        if extra_exclude:
            exclude |= extra_exclude

        data: dict[str, Any] = {}
        for name, field_info in type(self).model_fields.items():
            if name in exclude or name.startswith("_"):
                continue
            value = getattr(self, name)
            # Skip fields at their default value (evolution-stable)
            if (
                field_info.default is not PydanticUndefined
                and value == field_info.default
            ):
                continue
            if (
                field_info.default_factory is not None
                and value == field_info.default_factory()  # type: ignore[call-arg]
            ):
                continue
            data[name] = value

        def default_serializer(obj: Any) -> Any:
            if isinstance(obj, BaseModel):
                return obj.model_dump()
            return str(obj)

        content = orjson.dumps(
            data, option=orjson.OPT_SORT_KEYS, default=default_serializer
        )
        return hashlib.sha256(content).hexdigest()[:16]

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


class Output(BaseModel):
    """Base class for all output contracts (Apps and tasks).

    All App run() methods and task methods must return exactly one
    instance of a class that extends this class. This ensures:

    1. Clear contracts between callers and callees
    2. Backwards-compatible evolution via Pydantic fields with defaults
    3. Proper serialization through Temporal

    Example:
        class ExtractOutput(Output):
            records_extracted: int
            checkpoint_path: str
            # ``status`` is inherited from Output and defaults to SUCCESS;
            # set it explicitly only to signal partial-success / failure.

    Structured outputs:
        The SDK's OutputInterceptor automatically populates ``metrics`` and
        ``artifacts`` from any ``get_outputs().add_metric()`` /
        ``add_artifact()`` calls made during the workflow or its activities.
        Connector code never needs to set these fields directly.
    """

    model_config = ConfigDict()

    status: OutputStatus = OutputStatus.SUCCESS
    """Coarse-grained run outcome — see :class:`OutputStatus`. Defaults to
    ``SUCCESS`` so the field is additive-only for connectors that don't
    override it. Set to ``PARTIAL_SUCCESS`` when some entities were
    skipped or degraded but the run produced usable output; set to
    ``FAILURE`` when the run did not produce usable output."""

    metrics: dict[str, Any] | None = None
    """Metrics collected by the OutputInterceptor (e.g. assets-extracted).
    Populated automatically — do not set manually."""

    artifacts: dict[str, Any] | None = None
    """Artifact references collected by the OutputInterceptor.
    Populated automatically — do not set manually."""

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

        # Skip framework-managed fields — metrics and artifacts are populated
        # by the OutputInterceptor, not by user code, and are bounded in
        # practice (a handful of metric key-value pairs per workflow).
        validate_payload_safety(cls, skip_fields={"metrics", "artifacts"})


class HeartbeatDetails(BaseModel):
    """Base class for heartbeat progress contracts.

    Defines the progress state that a long-running task persists
    periodically so Temporal can resume from the last checkpoint on retry.

    Unlike Input/Output (which describe what enters/leaves a task),
    HeartbeatDetails captures internal mid-task progress: position markers
    for resuming and counters for observability.

    Serialization: Temporal serializes these to JSON. On retry, Temporal
    returns them as plain dicts — use self.get_heartbeat_details(cls) to
    reconstruct the typed model automatically.

    Evolution Rules (same as Input/Output):
        ADD new fields with default values
        NEVER remove fields
        NEVER change field types or names

    Example:
        class LoadTypeHeartbeat(HeartbeatDetails):
            chunk_idx: int         # Resume position (required, no default)
            loaded_count: int = 0  # Progress tracking
    """

    model_config = ConfigDict()


class Record(BaseModel):
    """Base class for domain records passed between Apps.

    Records represent domain data (e.g., products, users, events) that flow
    through pipelines and between Apps. Unlike Input/Output which define
    method contracts, Record defines the structure of domain data.

    All records must have an 'id' field for identification and tracking.

    Example:
        class ProductRecord(Record):
            name: str
            price: float
            category: str
            in_stock: bool = True
    """

    model_config = ConfigDict()

    id: str
    """Unique identifier for this record."""


# =============================================================================
# Validation and Utilities
# =============================================================================


class ContractValidationError(_InvalidInputError):
    """Deprecated: use ``application_sdk.errors.InvalidInputError`` — removed in v4.0."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = CONTRACT_VALIDATION
    code: ClassVar[str] = "CONTRACT_VALIDATION"

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
        warnings.warn(
            "ContractValidationError is deprecated; use application_sdk.errors.InvalidInputError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        _InvalidInputError.__init__(self, message=message)
        self.contract_type = contract_type
        self.field_name = field_name
        self.expected_type = expected_type
        self.actual_type = actual_type
        self._error_code = error_code

    @property
    def error_code(self) -> ErrorCode:
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
    code: ClassVar[str] = "PAYLOAD_SAFETY"

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
        # Skip ContractValidationError's __init__ to avoid double DeprecationWarning;
        # call the new leaf directly.
        _InvalidInputError.__init__(self, message=message)
        self.contract_type = cls_name
        self.field_name = field_name
        self.expected_type: str | None = None
        self.actual_type: str | None = None
        self.field_type = field_type
        self.reason = reason
        self._error_code = PAYLOAD_SAFETY


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

    This function checks each field in a Pydantic model (or dataclass) for types
    that could potentially exceed Temporal's 2MB payload limit.

    Args:
        cls: The contract class to validate.
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
        # Skip internal fields, explicitly skipped fields, and Pydantic internals
        if (
            field_name in skip
            or field_name.startswith("_")
            or field_name.startswith("model_")
        ):
            continue

        is_forbidden, reason = _is_forbidden_type(field_type)
        if is_forbidden:
            raise PayloadSafetyError(cls.__name__, field_name, field_type, reason)


class PublishInputMixin(BaseModel):
    """Mixin for apps whose workflow output feeds the Publish App.

    The Automation Engine reads these fields via JSONPath
    (``$.extract.outputs.*``) to pass to the Publish App. Apps that
    include a ``publish`` step in their AE manifest should use this
    as a mixin alongside their own output fields.

    ``publish_state_prefix``, ``staging_data_prefix``, and ``current_state_prefix``
    are auto-derived from ``connection_qualified_name`` via a model validator.
    Apps only need to set ``connection_qualified_name`` and ``transformed_data_prefix``.

    Example::

        class MyWorkflowOutput(PublishInputMixin, allow_unbounded_fields=True):
            custom_field: str = ""

        return MyWorkflowOutput(
            connection_qualified_name="default/snowflake/123",
            transformed_data_prefix="artifacts/.../transformed",
        )
    """

    PUBLISH_STATE_PREFIX_TEMPLATE: ClassVar[str] = (
        "persistent-artifacts/apps/atlan-publish-app/state"
        "/{connection_qn}/publish-state"
    )
    STAGING_DATA_PREFIX_TEMPLATE: ClassVar[str] = (
        "persistent-artifacts/apps/atlan-publish-app/state/{connection_qn}"
    )
    CURRENT_STATE_PREFIX_TEMPLATE: ClassVar[str] = (
        "argo-artifacts/{connection_qn}/current-state"
    )
    _SAFE_CONNECTION_QN_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"\A(?!.*(?:^|/)\.\.)(?!.*\.\./)[a-zA-Z0-9/_\-\.]+\Z"
    )

    # ── Input fields (used to derive output fields) ────────────────
    output_path: str = ""
    """SDK output path. Used to derive ``transformed_data_prefix``."""

    output_prefix: str = ""
    """Prefix to strip from ``output_path`` before deriving transformed prefix."""

    # ── Output fields (read by AE via JSONPath) ──────────────────
    transformed_data_prefix: str = ""
    """Object-store-relative path to transformed data files."""

    connection_qualified_name: str = ""
    """Qualified name of the Atlan connection."""

    publish_state_prefix: str = ""
    """Auto-derived from ``connection_qualified_name`` if not set."""

    staging_data_prefix: str = ""
    """Auto-derived from ``connection_qualified_name`` if not set.
    Same as ``publish_state_prefix`` but only up to the connection QN
    (without the ``/publish-state`` suffix)."""

    current_state_prefix: str = ""
    """Auto-derived from ``connection_qualified_name`` if not set."""

    @model_validator(mode="after")
    def _derive_publish_paths(self) -> "PublishInputMixin":
        """Auto-derive all publish-related paths."""
        # Auto-resolve output_path from Temporal context if not set
        if not self.output_path:
            try:
                from temporalio import (  # noqa: PLC0415 — defensive: try/except wraps "not in Temporal context"
                    workflow as _wf,
                )

                from application_sdk.constants import (  # noqa: PLC0415 — co-located with temporalio import in same try block
                    APPLICATION_NAME,
                    WORKFLOW_OUTPUT_PATH_TEMPLATE,
                )

                self.output_path = WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
                    application_name=APPLICATION_NAME,
                    workflow_id=_wf.info().workflow_id,
                    run_id=_wf.info().run_id,
                )
            except Exception:  # noqa: S110 — not in a Temporal workflow context; output_path stays empty by design
                pass

        # Derive transformed_data_prefix from output_path
        if not self.transformed_data_prefix and self.output_path:
            relative = self.output_path
            if self.output_prefix and relative.startswith(self.output_prefix):
                relative = relative[len(self.output_prefix) :].lstrip("/")
            result = posixpath.normpath(f"{relative}/transformed")
            if not result.startswith("..") and not result.startswith("/"):
                self.transformed_data_prefix = result

        # Derive state prefixes from connection_qualified_name
        cqn = self.connection_qualified_name
        if not cqn or not self._SAFE_CONNECTION_QN_RE.match(cqn):
            return self
        if not self.publish_state_prefix:
            self.publish_state_prefix = self.PUBLISH_STATE_PREFIX_TEMPLATE.format(
                connection_qn=cqn
            )
        if not self.staging_data_prefix:
            self.staging_data_prefix = self.STAGING_DATA_PREFIX_TEMPLATE.format(
                connection_qn=cqn
            )
        if not self.current_state_prefix:
            self.current_state_prefix = self.CURRENT_STATE_PREFIX_TEMPLATE.format(
                connection_qn=cqn
            )
        return self


@runtime_checkable
class InputContract(Protocol):
    """Protocol marker for App input contracts.

    All App inputs should be BaseModel subclasses. This protocol ensures
    they can be validated and serialized.
    """


@runtime_checkable
class OutputContract(Protocol):
    """Protocol marker for App output contracts.

    All App outputs should be BaseModel subclasses. This protocol ensures
    they can be validated and serialized.
    """


def validate_is_contract(cls: type, context: str = "contract") -> None:
    """Validate that a class is an Input, Output, HeartbeatDetails, or Record subclass.

    Args:
        cls: The class to validate.
        context: Description for error messages.

    Raises:
        ContractValidationError: If cls is not a contract base class subclass.
    """
    if not (
        isinstance(cls, type)
        and issubclass(cls, (Input, Output, HeartbeatDetails, Record))
    ):
        raise ContractValidationError(
            f"{context} must be a contract class (Input/Output/HeartbeatDetails/Record), "
            f"got {cls.__name__}",
            contract_type=context,
        )


def get_contract_fields(cls: type) -> dict[str, type]:
    """Get the fields and their types from a contract class.

    Args:
        cls: A contract type (Input/Output/HeartbeatDetails/Record subclass).

    Returns:
        Dictionary mapping field names to their types.

    Raises:
        ContractValidationError: If cls is not a contract class.
    """
    validate_is_contract(cls)
    hints = get_type_hints(cls)
    return {name: hints.get(name, Any) for name in cls.model_fields}


def has_default(cls: type, field_name: str) -> bool:
    """Check if a contract field has a default value.

    Args:
        cls: A contract type.
        field_name: Name of the field to check.

    Returns:
        True if the field has a default value.
    """
    validate_is_contract(cls)
    field_info = cls.model_fields.get(field_name)
    if field_info is None:
        return False
    return not field_info.is_required()


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
    validate_is_contract(old_cls, "old contract")
    validate_is_contract(new_cls, "new contract")

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


class ContractMetadata(BaseModel, frozen=True):
    """Metadata about a contract for registration and discovery."""

    name: str
    version: str
    cls: type
    is_input: bool

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # Reserved for future schema evolution tracking
    schema_hash: str | None = None
    deprecated: bool = False
    deprecation_message: str | None = None
