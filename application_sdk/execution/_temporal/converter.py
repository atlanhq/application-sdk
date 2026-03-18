"""Temporal data converter configuration.

Standard chain order (first match wins):
1. App-specific converters (optional)
2. Binary converters (null, plain, proto)
3. Msgspec-aware JSON converter (always-on default)

msgspec.Struct serialization is built-in — no per-app opt-in required.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio.converter import (
    BinaryNullPayloadConverter,
    BinaryPlainPayloadConverter,
    BinaryProtoPayloadConverter,
    CompositePayloadConverter,
    DataConverter,
    EncodingPayloadConverter,
    JSONPlainPayloadConverter,
    JSONProtoPayloadConverter,
)

from application_sdk.execution._temporal.msgspec_converter import (
    MsgspecJSONEncoder,
    MsgspecTypeConverter,
)

if TYPE_CHECKING:
    from application_sdk.app.base import App


def get_msgspec_payload_converter() -> EncodingPayloadConverter:
    """Get the msgspec-aware JSON payload converter.

    Returns:
        Msgspec-aware JSON payload converter.
    """
    return JSONPlainPayloadConverter(
        encoder=MsgspecJSONEncoder,
        custom_type_converters=[MsgspecTypeConverter()],
    )


def create_composite_payload_converter(
    additional_converters: list[EncodingPayloadConverter] | None = None,
) -> CompositePayloadConverter:
    """Create a composite payload converter with the standard chain.

    Args:
        additional_converters: Optional app-specific converters to check first.

    Returns:
        Configured CompositePayloadConverter.
    """
    converters: list[EncodingPayloadConverter] = []
    has_json_plain = False

    if additional_converters:
        converters.extend(additional_converters)
        for conv in additional_converters:
            if isinstance(conv, JSONPlainPayloadConverter):
                has_json_plain = True
                break

    converters.append(BinaryNullPayloadConverter())
    converters.append(BinaryPlainPayloadConverter())
    converters.append(JSONProtoPayloadConverter())
    converters.append(BinaryProtoPayloadConverter())

    if not has_json_plain:
        converters.append(
            JSONPlainPayloadConverter(
                encoder=MsgspecJSONEncoder,
                custom_type_converters=[MsgspecTypeConverter()],
            )
        )

    return CompositePayloadConverter(*converters)


def create_data_converter(
    additional_converters: list[EncodingPayloadConverter] | None = None,
) -> DataConverter:
    """Create a data converter with the standard chain.

    msgspec.Struct serialization is built-in. No additional_converters needed
    for pyatlan types (Connection, Table, AtlanGroup, etc.).

    Args:
        additional_converters: Optional app-specific converters to check first.

    Returns:
        Configured DataConverter.

    Example:
        converter = create_data_converter()
        client = await Client.connect("localhost:7233", data_converter=converter)
    """
    payload_converter = create_composite_payload_converter(additional_converters)
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
