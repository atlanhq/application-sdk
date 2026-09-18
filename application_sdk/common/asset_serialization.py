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

The seam also owns **framework-injected attributes** — values the SDK holds
that the mapper is never handed, so an asset-returning mapper cannot set them
correctly on its own. Today that is ``connectionName`` (FND-2056) and the
three ``lastSync*`` attributes (FND-2097). Both are injected here rather than
copied into every connector, because a copy in every connector is how they
ended up wrong or missing in the first place.

What this module does **not** decide is the *envelope* — the shape of the line
once serialisation has happened, and in particular where relationship
references live. That is
:mod:`application_sdk.common.entity_envelope` (FND-2137), which
:func:`entity_bytes` applies through its ``envelope`` and ``decorations``
arguments. Dispatch here, envelope there: this module answers "how do I
serialise this object at all", that one answers "what does the finished line
look like", and keeping them apart is what stops either becoming the place
every new connector-specific knob gets bolted on.

Public API::

    from application_sdk.common.asset_serialization import entity_bytes
    from application_sdk.common.last_sync import resolve_last_sync_details

    # Resolve ONCE per transform activity, never per record: every asset a run
    # produces should carry the same lastSyncRunAt.
    last_sync = resolve_last_sync_details()

    line = entity_bytes(
        asset,
        connection_name="my-conn",
        last_sync=last_sync,
        entity_type="table",
    )

Non-SQL apps get the same behaviour from the same call — this module is the
shared seam, and nothing in it is SQL-specific. ``SqlApp._transform_entity``
is simply its first caller.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import orjson

from application_sdk.common.entity_envelope import (
    DEFAULT_ENVELOPE,
    EntityDecorations,
    EntityEnvelopePolicy,
    EnvelopeShape,
    apply_envelope,
    to_atlas_format_dict,
)
from application_sdk.common.errors import UnserializableMapperResultError
from application_sdk.common.last_sync import (
    LastSyncDetails,
    LastSyncStampable,
    set_last_sync_details_on_asset,
)

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


#: The dict path's ``(resolved field, Atlas wire key)`` table for the two
#: *conditional* values only. ``run_at_ms`` is deliberately absent: the
#: primitive assigns it unconditionally, so folding it into a uniform
#: truthiness loop here made the dict path drop ``run_at_ms=0`` while the
#: object path kept it. The asymmetry is real, so it is spelled out rather
#: than hidden in a table.
#:
#: The object path needs no table at all — it goes through the primitive,
#: which knows its own field names.
_LAST_SYNC_OPTIONAL_KEYS: tuple[tuple[str, str], ...] = (
    ("run", "lastSyncRun"),
    ("workflow_name", "lastSyncWorkflowName"),
)


def _set_last_sync(asset: object, details: LastSyncDetails) -> None:
    """Stamp ``lastSyncRun`` / ``lastSyncWorkflowName`` / ``lastSyncRunAt``.

    These identify *which* run last touched the asset. They are run identity,
    not asset content: the mapper is handed a record and a connection
    qualified name, so it cannot resolve the AE-dispatched workflow id or the
    end-to-end correlation id — a connector that tries reaches for
    ``input.workflow_id`` and stamps the *child* workflow's Temporal id, which
    is not clickable back to the AE run.

    So, unlike ``connectionName``, a value already on the asset does **not**
    win: whoever resolved *details* had the run context the mapper did not.

    Which of the three that applies to is the primitive's rule, and both
    paths below honour it exactly: an empty resolved ``run`` /
    ``workflow_name`` is never written, so outside Temporal a hand-set value
    survives rather than being blanked — while ``run_at_ms`` is *always*
    written, including a caller's explicit ``0``. ``LastSyncDetails``
    documents ``run_at_ms`` as an accepted override, so 0 is a value a caller
    can mean, not an absence.

    The object path is :func:`set_last_sync_details_on_asset`, unwrapped: the
    SDK has one implementation of "what stamping means" and this is not a
    second one. All this function adds is the aperture ``entity_bytes``
    already has and the primitive does not — it takes ``object``, so the
    shape may be a dict, or may not declare the fields, or may refuse
    assignment.
    """
    if isinstance(asset, dict):
        # The one case the primitive deliberately cannot serve. BLDX-1229
        # removed its dict-shaped helpers to stop *new* code adopting
        # dict transformation, and that stands: nothing public is added
        # back here. But ``entity_bytes`` has always accepted a dict in the
        # Atlas wire shape, and ``SqlApp.map_<entity>`` is annotated to
        # return one — so stamping every shape except that one would leave
        # exactly the silent per-shape gap this change exists to close.
        attributes = asset.setdefault("attributes", {})
        if not isinstance(attributes, dict):
            return
        for field, wire_key in _LAST_SYNC_OPTIONAL_KEYS:
            value = getattr(details, field)
            if value:
                attributes[wire_key] = value
        attributes["lastSyncRunAt"] = details.run_at_ms
        return

    if not isinstance(asset, LastSyncStampable):
        # A shape declaring none of the three. Its own serialiser would
        # ignore an invented field, and the SDK does not add fields to
        # somebody else's model.
        return

    try:
        set_last_sync_details_on_asset(asset, details=details)
    except (AttributeError, TypeError):
        # Frozen or read-only asset. Same trade-off as connectionName:
        # losing a debugging attribute beats failing the whole transform.
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


def _nested_dict(asset: object, entity_type: str | None) -> dict[str, Any]:
    """The nested-format dict for *asset*, by the same ordered dispatch.

    Split out of :func:`entity_bytes` because the envelope path needs a dict
    where the identity path can stay on raw bytes. The order is the dispatch
    order and must not drift from it: a shape that serialises one way as bytes
    and another way as a dict is the per-shape gap this module exists to close.

    Raises:
        UnserializableMapperResultError: *asset* is none of the supported
            shapes.
    """
    if isinstance(asset, NestedBytesAsset):
        line = _checked_line(asset.to_nested_bytes(), asset, entity_type)
        return orjson.loads(line)  # type: ignore[no-any-return]
    if isinstance(asset, NestedDictAsset):
        return asset.to_nested_dict()
    if isinstance(asset, ModelDumpAsset):
        return asset.model_dump()
    if isinstance(asset, dict):
        return asset

    observed = type(asset).__name__
    raise UnserializableMapperResultError(
        message=(
            f"Asset mapper returned {observed}{_where(entity_type)}, which the "
            f"SDK cannot serialise to the Atlas wire shape"
        ),
        observed=observed,
        location=entity_type,
    )


def entity_bytes(
    asset: object,
    *,
    connection_name: str = "",
    last_sync: LastSyncDetails | None = None,
    entity_type: str | None = None,
    envelope: EntityEnvelopePolicy | None = None,
    decorations: EntityDecorations | None = None,
) -> bytes:
    """Serialise a mapper's return value to one Atlas wire-shape JSON line.

    Args:
        asset: Whatever ``map_<entity>()`` returned — a ``pyatlan_v9`` asset, a
            pyatlan v1 asset, any object matching one of the protocols in this
            module, or a plain ``dict`` already in the Atlas wire shape.
        connection_name: Connection display name to stamp on the asset when the
            mapper left it unset. Empty string skips the injection.
        last_sync: Run-identity values to stamp, from
            :func:`application_sdk.common.last_sync.resolve_last_sync_details`.
            Resolve it **once per transform activity** and pass the same
            object for every record, so one crawl produces one
            ``lastSyncRunAt``. ``None`` (the default) skips the injection —
            for a caller that has already stamped the asset itself, or one
            running outside any run context.
        entity_type: Entity being transformed (``"table"``, ``"column"``, …).
            Carried into the error so a failure names where it happened.
        envelope: The connector's declared
            :class:`~application_sdk.common.entity_envelope.EntityEnvelopePolicy`.
            ``None`` (the default) means
            :data:`~application_sdk.common.entity_envelope.DEFAULT_ENVELOPE` —
            **flattened**, which is a change from the pre-FND-2137 output. A
            caller that must keep the pyatlan-native shape pins
            ``EnvelopeShape.PYATLAN`` rather than relying on the default.
        decorations: Top-level contract fields to add to the entity root, as an
            :class:`~application_sdk.common.entity_envelope.EntityDecorations`.
            ``None`` adds none.

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
    if last_sync is not None:
        _set_last_sync(asset, last_sync)

    policy = envelope if envelope is not None else DEFAULT_ENVELOPE

    if policy.is_identity and decorations is None:
        # The pre-FND-2137 path, byte for byte. Reached only by a caller that
        # pinned PYATLAN: the migration lever is worth nothing if it produces
        # output merely equivalent to what it is standing in for.
        if isinstance(asset, NestedBytesAsset):
            return _checked_line(asset.to_nested_bytes(), asset, entity_type)
        return _dumps(_nested_dict(asset, entity_type), asset, entity_type)

    entity = None
    already_flattened = False
    if policy.shape is EnvelopeShape.FLATTENED:
        # pyatlan owns its own flattening, and its encoder is cheaper than the
        # nested one plus our pass. ``None`` means "not a pyatlan_v9 asset" —
        # a dict or a v1 model — which the generic path below handles.
        entity = to_atlas_format_dict(asset)
        already_flattened = entity is not None
    if entity is None:
        entity = _nested_dict(asset, entity_type)

    entity = apply_envelope(
        entity,
        policy=policy,
        decorations=decorations,
        already_flattened=already_flattened,
    )
    return _dumps(entity, asset, entity_type)
