"""Temporal data converter configuration.

Uses the official ``pydantic_data_converter`` from ``temporalio.contrib.pydantic``,
which handles Pydantic ``BaseModel`` and dataclasses natively via
``pydantic_core.to_json()`` / ``TypeAdapter.validate_json()``.

This replaces the previous custom msgspec-based converter chain. All contracts
are now Pydantic ``BaseModel`` subclasses, so no custom encoder or type converter
is required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter, EncodingPayloadConverter

if TYPE_CHECKING:
    from application_sdk.app.base import App


def create_data_converter(
    additional_converters: list[EncodingPayloadConverter] | None = None,
) -> DataConverter:
    """Create a data converter with Pydantic support.

    When no additional converters are provided, returns the official
    ``pydantic_data_converter`` directly.

    When app-specific converters are provided, they are prepended to the
    standard Pydantic converter chain.

    Args:
        additional_converters: Optional app-specific converters to check first.

    Returns:
        Configured DataConverter.

    Example:
        converter = create_data_converter()
        client = await Client.connect("localhost:7233", data_converter=converter)
    """
    if not additional_converters:
        return pydantic_data_converter

    from temporalio.converter import (
        BinaryNullPayloadConverter,
        BinaryPlainPayloadConverter,
        BinaryProtoPayloadConverter,
        CompositePayloadConverter,
        JSONProtoPayloadConverter,
    )

    # Build a chain that puts app converters first, then the standard pydantic chain
    converters: list[EncodingPayloadConverter] = list(additional_converters)
    converters.extend(
        [
            BinaryNullPayloadConverter(),
            BinaryPlainPayloadConverter(),
            JSONProtoPayloadConverter(),
            BinaryProtoPayloadConverter(),
        ]
    )
    # Append the pydantic JSON converter from the official chain
    for conv in pydantic_data_converter.payload_converter.converters:
        from temporalio.converter import JSONPlainPayloadConverter

        if isinstance(conv, JSONPlainPayloadConverter):
            converters.append(conv)
            break

    payload_converter = CompositePayloadConverter(*converters)
    return DataConverter(
        payload_converter_class=lambda: payload_converter,  # type: ignore[arg-type]
    )


def create_data_converter_for_app(app_class: type[App]) -> DataConverter:
    """Create a data converter for a specific app, including any app-specific converters.

    If the app class declares ``payload_converters``, they are instantiated and
    placed first in the converter chain.

    Args:
        app_class: The App class to create a converter for.

    Returns:
        Configured DataConverter with app-specific converters (if any).
    """
    app_converters: list[EncodingPayloadConverter] | None = None

    converter_classes = getattr(app_class, "payload_converters", None)
    if converter_classes:
        app_converters = [cls() for cls in converter_classes]

    return create_data_converter(additional_converters=app_converters)
