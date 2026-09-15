"""One serialisation seam for asset-mapper return values (FND-2056).

Every connector on the v3 asset-mapper pattern hands the SDK whatever its
``map_<entity>()`` returned and expects a JSONL line in the Atlas wire shape
back. Before this module each template did that inline, by probing method
names and falling back to writing the *raw source record* when none matched —
which published unmapped rows as entities and still reported SUCCESS. A
``pyatlan_v9`` asset hit that fallback, because its serialiser is
``to_nested_bytes()`` and it has none of the probed names.

The dispatch here is ordered and closed: the cheapest native encoder wins, and
an unrecognised type raises :class:`~application_sdk.common.errors.UnserializableMapperResultError`
rather than silently degrading. Each accepted shape is declared as a
``runtime_checkable`` Protocol so the contract is a named type rather than a
string method name spelled out at the call site.

Public API::

    from application_sdk.common.asset_serialization import entity_bytes

    line = entity_bytes(asset, connection_name="my-conn", entity_type="table")
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import orjson

from application_sdk.common.errors import UnserializableMapperResultError

__all__ = [
    "NestedBytesAsset",
    "NestedDictAsset",
    "ModelDumpAsset",
    "UnserializableValue",
    "entity_bytes",
    "orjson_default",
]


@runtime_checkable
class NestedBytesAsset(Protocol):
    """An asset that encodes itself straight to Atlas nested-format JSON bytes.

    ``pyatlan_v9`` assets implement this (msgspec-backed, no dict
    intermediate). It is first in the dispatch because it is the only shape
    that needs no JSON pass at all on the SDK side.
    """

    def to_nested_bytes(self) -> bytes: ...


@runtime_checkable
class NestedDictAsset(Protocol):
    """An asset that renders itself as an Atlas nested-format ``dict``."""

    def to_nested_dict(self) -> dict[str, Any]: ...


@runtime_checkable
class ModelDumpAsset(Protocol):
    """A pydantic-style model exposing ``model_dump()``.

    Kept for the pyatlan v1 assets some connectors still build. It is last of
    the object shapes deliberately: ``model_dump()`` yields the model's own
    field names, which for a snake_case pydantic asset is *not* the Atlas wire
    shape. An asset that also exposes one of the nested encoders above is
    therefore serialised through that instead.
    """

    def model_dump(self) -> dict[str, Any]: ...


class UnserializableValue(TypeError):
    """A nested value ``orjson_default`` cannot render, naming its type.

    A ``TypeError`` because that is what orjson's ``default=`` protocol
    contractually requires to signal "not serialisable" — anything else is
    swallowed or re-raised as something less useful. The subclass exists so
    :func:`entity_bytes` can re-raise it as the module's own typed error with
    the *offending value's* type rather than the mapper result's, which for a
    dict is the uninformative ``dict``.
    """

    def __init__(self, type_name: str) -> None:
        super().__init__(f"Object of type {type_name} is not JSON-serializable")
        self.type_name = type_name


def orjson_default(obj: Any) -> Any:
    """Fallback serialiser for orjson — covers types it doesn't handle natively.

    orjson natively serialises ``str``, ``int``, ``float``, ``bool``, ``None``,
    ``list``, ``dict``, ``datetime``, ``date``, ``time``, ``UUID`` and
    ``dataclass`` instances. SQL drivers commonly return ``Decimal`` for
    numeric columns and occasionally ``bytes`` for blob columns; both fall
    back to a JSON-safe representation here.

    Raises:
        UnserializableValue: *obj* is none of those — a ``TypeError`` subclass,
            so the orjson protocol is honoured and the type name survives for
            :func:`entity_bytes` to attribute.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    # conformance: ignore[E012] orjson default= protocol contractually requires TypeError to signal non-serialisable; replacing with AppError would break serialisation
    raise UnserializableValue(  # orjson default= protocol requires TypeError to signal non-serializable
        type(obj).__name__
    )


def _set_connection_name(asset: object, connection_name: str) -> None:
    """Stamp ``connectionName`` onto *asset* when the mapper left it unset.

    The SDK holds the connection name; the mapper is handed only the connection
    *qualified name*, so without this an asset-returning mapper loses the
    attribute entirely. Done before the dispatch rather than by patching a
    serialised dict afterwards — the value belongs on the asset, and one of the
    supported shapes (``to_nested_bytes``) never produces a dict to patch.

    An existing value always wins: a mapper that set it deliberately is the
    authority.
    """
    if isinstance(asset, dict):
        attributes = asset.setdefault("attributes", {})
        if isinstance(attributes, dict):
            attributes.setdefault("connectionName", connection_name)
        return

    # Anything else: only touch an attribute the object actually declares, and
    # only when it is empty. ``pyatlan_v9`` leaves it as msgspec ``UNSET``,
    # pydantic models as ``None`` — both falsy, so one truthiness test covers
    # every shape without importing either library.
    try:
        current = getattr(asset, "connection_name")
    except Exception:  # noqa: BLE001 - a property that raises is not ours to fix
        return
    if current:
        return
    try:
        asset.connection_name = connection_name  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        # Frozen or slot-less asset: the attribute is simply not settable.
        # Losing connectionName is worse than failing the run only if the
        # asset genuinely needs it, and every asset type that does exposes a
        # settable field. Swallow rather than fail the whole transform.
        return


def _where(entity_type: str | None) -> str:
    """The `` while transforming <entity>`` clause, or nothing when unknown."""
    return f" while transforming {entity_type}" if entity_type else ""


def _dumps(payload: Any, asset: object, entity_type: str | None) -> bytes:
    """``orjson.dumps`` with this module's typed error on an unrenderable value.

    A dict the mapper produced can still hold a value orjson cannot render —
    a driver type that is neither ``Decimal`` nor ``bytes``. Left unwrapped
    that surfaces as a bare ``TypeError``, which fails the activity loudly but
    lands outside the ``DataIntegrityError`` / ``APP_OWNER`` attribution the
    rest of this seam guarantees, so the Automation Engine cannot read who owns
    it from the typed fields.
    """
    try:
        return orjson.dumps(payload, default=orjson_default)
    except TypeError as exc:
        # orjson does not let an exception from ``default=`` through: it raises
        # its own JSONEncodeError (itself a TypeError) with ours as __cause__.
        # So the offending type has to be read off the cause, not the raised
        # error. Failures orjson raises on its own — recursion, an unsupported
        # key type — have no cause and no inner type to name.
        cause = exc.__cause__
        if isinstance(cause, UnserializableValue):
            raise UnserializableMapperResultError(
                message=(
                    f"Asset mapper result{_where(entity_type)} holds a "
                    f"{cause.type_name} value, which the SDK cannot serialise "
                    f"to the Atlas wire shape"
                ),
                observed=cause.type_name,
                location=entity_type,
            ) from exc
        # The encoder's own text is deliberately not interpolated into
        # ``message`` (E015: it breaks dashboard grouping and can carry
        # unsanitised payload text). ``from exc`` keeps it on the traceback.
        raise UnserializableMapperResultError(
            message=(
                f"Asset mapper result{_where(entity_type)} could not be "
                f"encoded as JSON"
            ),
            observed=type(asset).__name__,
            location=entity_type,
        ) from exc


def _checked_line(line: bytes, asset: object, entity_type: str | None) -> bytes:
    """Reject a ``to_nested_bytes()`` result that is not a single JSON line.

    The caller writes this verbatim and appends one ``b"\\n"``, so bytes that
    already span lines would split one entity across several JSONL records —
    well-formed lines, wrong count, no error. That is the same silent,
    count-passing damage this module exists to remove, so it is refused here
    rather than trusted. ``pyatlan_v9``'s encoder is compact, but
    ``NestedBytesAsset`` is public: any implementer can reach this branch.
    """
    if b"\n" in line or b"\r" in line:
        observed = type(asset).__name__
        raise UnserializableMapperResultError(
            message=(
                f"{observed}.to_nested_bytes(){_where(entity_type)} returned "
                f"JSON spanning more than one line; the Atlas wire format is "
                f"one entity per JSONL record"
            ),
            observed=observed,
            location=entity_type,
        )
    return line


def entity_bytes(
    asset: object,
    *,
    connection_name: str = "",
    entity_type: str | None = None,
) -> bytes:
    """Serialise a mapper's return value to one Atlas wire-shape JSON line.

    Args:
        asset: Whatever ``map_<entity>()`` returned — a ``pyatlan_v9`` asset, a
            pyatlan v1 asset, any object matching one of the protocols in this
            module, or a plain ``dict`` already in the Atlas wire shape.
        connection_name: Connection display name to stamp on the asset when the
            mapper left it unset. Empty string skips the injection.
        entity_type: Entity being transformed (``"table"``, ``"column"``, …).
            Carried into the error so a failure names where it happened.

    Returns:
        Compact JSON bytes with no trailing newline. JSON string escaping means
        the result never contains a raw newline, so the caller can write it as
        a JSONL record directly.

    Raises:
        UnserializableMapperResultError: *asset* is none of the supported
            shapes — the branch that used to write the unmapped raw source
            record and report success; a supported shape holds a nested value
            that cannot be rendered; or ``to_nested_bytes()`` returned bytes
            spanning more than one line.
    """
    if connection_name:
        _set_connection_name(asset, connection_name)

    if isinstance(asset, NestedBytesAsset):
        return _checked_line(asset.to_nested_bytes(), asset, entity_type)
    if isinstance(asset, NestedDictAsset):
        return _dumps(asset.to_nested_dict(), asset, entity_type)
    if isinstance(asset, ModelDumpAsset):
        return _dumps(asset.model_dump(), asset, entity_type)
    if isinstance(asset, dict):
        return _dumps(asset, asset, entity_type)

    observed = type(asset).__name__
    raise UnserializableMapperResultError(
        message=(
            f"Asset mapper returned {observed}{_where(entity_type)}, which the "
            f"SDK cannot serialise to the Atlas wire shape"
        ),
        observed=observed,
        location=entity_type,
    )
