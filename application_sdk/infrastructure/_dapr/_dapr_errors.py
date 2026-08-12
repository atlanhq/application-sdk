"""Typed error leaves for the Dapr infrastructure client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InvalidInputError, UnimplementedError


@dataclass(kw_only=True)
class DaprBinaryPayloadError(InvalidInputError):
    """Binding payload is not UTF-8 text, which Dapr's HTTP API cannot carry.

    The payload travels inside a JSON request body, so there is no raw-bytes
    channel.  Decoding it lossily would write silently corrupted data, so the
    call fails instead.  The payload itself is never included in the message.
    """

    code: ClassVar[str] = "INVALID_INPUT_DAPR_BINARY_PAYLOAD"
    message: str = (
        "Binding payload is not valid UTF-8. Dapr's HTTP binding API carries "
        "the payload inside a JSON body, so binary data cannot be sent "
        "unchanged. Base64-encode it and set decodeBase64 on the component, "
        "or use App.upload, which writes binary through obstore."
    )
    field: str | None = "data"
    binding_name: str | None = None


@dataclass(kw_only=True)
class DaprListKeysUnsupportedError(UnimplementedError):
    """Key listing is not supported by this Dapr state-store backend."""

    code: ClassVar[str] = "UNIMPLEMENTED_DAPR_LIST_KEYS"
    message: str = "Key listing is not supported by all Dapr state stores"
    operation: str | None = "list_keys"
    reason: str | None = "dapr_state_store_no_list"
