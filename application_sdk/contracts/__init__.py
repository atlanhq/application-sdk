"""Contracts module - typed Input/Output base classes for Apps and tasks.

Provides the foundation for schema-driven contracts between Apps, tasks,
and their callers. Using these base classes ensures:
1. Type safety - All inputs/outputs are typed Pydantic models
2. Payload safety - Validated against Temporal's 2MB payload limit
3. Serialization - Works seamlessly with Temporal's pydantic_data_converter
4. Backwards compatibility - Add new fields with defaults
"""

import importlib
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from application_sdk.contracts.storage import (
        DeclaredFile,
        DownloadInput,
        DownloadOutput,
        UploadInput,
        UploadOutput,
        UploadRefsInput,
        UploadRefsOutput,
        VerifyRefsInput,
        VerifyRefsOutput,
    )
    from application_sdk.contracts.types import (
        AssetArtifact,
        BoundedDict,
        BoundedList,
        ConnectionRef,
        FileReference,
        GitReference,
        Lazy,
        MaxItems,
        StorageTier,
        StoreTarget,
        asset_artifact_fields,
        asset_artifact_marker,
    )

#: Worker-side names: imported on first access, so the api distribution
#: (which ships this ``__init__`` without them) imports cleanly.
_LAZY: dict[str, tuple[str, str]] = {
    "AssetArtifact": ("application_sdk.contracts.types", "AssetArtifact"),
    "BoundedDict": ("application_sdk.contracts.types", "BoundedDict"),
    "BoundedList": ("application_sdk.contracts.types", "BoundedList"),
    "ConnectionRef": ("application_sdk.contracts.types", "ConnectionRef"),
    "DeclaredFile": ("application_sdk.contracts.storage", "DeclaredFile"),
    "DownloadInput": ("application_sdk.contracts.storage", "DownloadInput"),
    "DownloadOutput": ("application_sdk.contracts.storage", "DownloadOutput"),
    "FileReference": ("application_sdk.contracts.types", "FileReference"),
    "GitReference": ("application_sdk.contracts.types", "GitReference"),
    "Lazy": ("application_sdk.contracts.types", "Lazy"),
    "MaxItems": ("application_sdk.contracts.types", "MaxItems"),
    "StorageTier": ("application_sdk.contracts.types", "StorageTier"),
    "StoreTarget": ("application_sdk.contracts.types", "StoreTarget"),
    "UploadInput": ("application_sdk.contracts.storage", "UploadInput"),
    "UploadOutput": ("application_sdk.contracts.storage", "UploadOutput"),
    "UploadRefsInput": ("application_sdk.contracts.storage", "UploadRefsInput"),
    "UploadRefsOutput": ("application_sdk.contracts.storage", "UploadRefsOutput"),
    "VerifyRefsInput": ("application_sdk.contracts.storage", "VerifyRefsInput"),
    "VerifyRefsOutput": ("application_sdk.contracts.storage", "VerifyRefsOutput"),
    "asset_artifact_fields": (
        "application_sdk.contracts.types",
        "asset_artifact_fields",
    ),
    "asset_artifact_marker": (
        "application_sdk.contracts.types",
        "asset_artifact_marker",
    ),
}

__all__ = [
    "AssetArtifact",
    "BoundedDict",
    "BoundedList",
    "ConnectionRef",
    "ContractMetadata",
    "DeclaredFile",
    "ContractValidationError",
    "DownloadInput",
    "DownloadOutput",
    "FileReference",
    "GitReference",
    "HeartbeatDetails",
    "Input",
    "InputContract",
    "Lazy",
    "MaxItems",
    "Output",
    "OutputContract",
    "PayloadSafetyError",
    "PublishInputMixin",
    "Record",
    "SerializableEnum",
    "StorageTier",
    "StoreTarget",
    "UploadInput",
    "UploadOutput",
    "UploadRefsInput",
    "UploadRefsOutput",
    "VerifyRefsInput",
    "VerifyRefsOutput",
    "asset_artifact_fields",
    "asset_artifact_marker",
    "get_contract_fields",
    "has_default",
    "is_backwards_compatible",
    "validate_is_contract",
    "validate_payload_safety",
]


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
