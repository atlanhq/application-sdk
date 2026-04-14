"""Contracts module - typed Input/Output base classes for Apps and tasks.

Provides the foundation for schema-driven contracts between Apps, tasks,
and their callers. Using these base classes ensures:
1. Type safety - All inputs/outputs are typed Pydantic models
2. Payload safety - Validated against Temporal's 2MB payload limit
3. Serialization - Works seamlessly with Temporal's pydantic_data_converter
4. Backwards compatibility - Add new fields with defaults
"""

from application_sdk.contracts.base import (
    validate_is_dataclass,  # backward-compat alias
)
from application_sdk.contracts.base import (
    ContractMetadata,
    ContractValidationError,
    HeartbeatDetails,
    Input,
    InputContract,
    Output,
    OutputContract,
    PayloadSafetyError,
    PublishInputMixin,
    Record,
    SerializableEnum,
    get_contract_fields,
    has_default,
    is_backwards_compatible,
    validate_is_contract,
    validate_payload_safety,
)
from application_sdk.contracts.config import ResolveConfigInput, ResolveConfigOutput
from application_sdk.contracts.storage import (
    DownloadInput,
    DownloadOutput,
    UploadInput,
    UploadOutput,
)
from application_sdk.contracts.types import (
    BoundedDict,
    BoundedList,
    ConnectionRef,
    FileReference,
    GitReference,
    MaxItems,
    StorageTier,
)

__all__ = [
    "ContractMetadata",
    "ContractValidationError",
    "HeartbeatDetails",
    "Input",
    "InputContract",
    "Output",
    "OutputContract",
    "PayloadSafetyError",
    "PublishInputMixin",
    "Record",
    "SerializableEnum",
    "get_contract_fields",
    "has_default",
    "is_backwards_compatible",
    "validate_is_contract",
    "validate_is_dataclass",  # backward-compat alias
    "validate_payload_safety",
    "BoundedDict",
    "BoundedList",
    "ConnectionRef",
    "FileReference",
    "GitReference",
    "MaxItems",
    "StorageTier",
    "ResolveConfigInput",
    "ResolveConfigOutput",
    "UploadInput",
    "UploadOutput",
    "DownloadInput",
    "DownloadOutput",
]
