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

from application_sdk import _deprecation

if TYPE_CHECKING:
    from application_sdk.app.base import App


def create_data_converter(
    additional_converters: list[EncodingPayloadConverter] | None = None,
) -> DataConverter:
    """Return the SDK's Temporal data converter: the official ``pydantic_data_converter``.

    Args:
        additional_converters: Deprecated and ignored. It never worked: on
            temporalio 1.35 (the SDK's floor) building the chain raised
            ``AttributeError``, and on earlier releases the chain had no JSON
            converter, so a dict or ``BaseModel`` payload failed with "no known
            converter". The Pydantic converter already
            handles ``BaseModel``, dataclasses and plain JSON values. Passing
            any value, including an empty list, emits a ``DeprecationWarning``.

    Returns:
        The shared ``pydantic_data_converter``.

    Example:
        converter = create_data_converter()
        client = await Client.connect("localhost:7233", data_converter=converter)

    .. deprecated:: 3.59
        ``additional_converters`` is ignored. Will be removed in v4.0.0.
    """
    if additional_converters is not None:
        _deprecation.warn_deprecated(
            "create_data_converter(additional_converters=...)",
            "create_data_converter() with no arguments",
            "4.0.0",
            note=(
                "the parameter never produced a working converter and is now "
                "ignored; the Pydantic converter already serialises BaseModel, "
                "dataclass and JSON payloads"
            ),
        )
    return pydantic_data_converter


def create_data_converter_for_app(app_class: type[App]) -> DataConverter:
    """Return the data converter for *app_class*.

    Every app gets the shared ``pydantic_data_converter``. An app class that
    still declares a ``payload_converters`` attribute gets a
    ``DeprecationWarning``: the attribute is ignored, because the chain it fed
    never worked (see :func:`create_data_converter`).

    Args:
        app_class: The App class to create a converter for.

    Returns:
        The shared ``pydantic_data_converter``.
    """
    if getattr(app_class, "payload_converters", None) is not None:
        _deprecation.warn_deprecated(
            "App.payload_converters",
            "no attribute; remove it from the App class",
            "4.0.0",
            note=(
                "custom payload converters never produced a working converter "
                "and are now ignored; the Pydantic converter already "
                "serialises BaseModel, dataclass and JSON payloads"
            ),
        )
    return create_data_converter()
