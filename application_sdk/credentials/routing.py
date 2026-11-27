"""Route a workflow input's credential channels into one ``(ref, inline)`` pair.

Every connector input carries up to four credential channels:

1. a pre-built :class:`~application_sdk.credentials.ref.CredentialRef` — the
   generic ``credential_ref`` field, or the ``<app>_credential`` field the
   contract toolkit generates from ``App.pkl``;
2. ``credential_guid`` — a platform-issued GUID (direct mode);
3. ``agent_json`` — an agent credential spec (self-deployed-runtime mode);
4. ``credentials`` — inline key/value pairs.

Channels 2 and 3 are routed by :meth:`CredentialRef.resolve`. What each app
used to re-implement around that call — the pre-built ref, the inline fallback,
and the shape the inline values travel in — lives here, so every app routes the
same input to the same credential.

The inline channel is **local-dev and test only**. Production never reaches
it: ``/workflows/v1/start`` strips ``credentials`` from every request body
before dispatch, and the platform always sends a ``credential_guid`` or an
``agent_json``. What does reach it is a workflow started in-process — an
``AppExecutor`` integration test, or a unit test that builds the input
directly. Those are the callers the fallback exists for.

Inline credentials have one shape everywhere: a flat dict whose nested
sections travel as dotted keys (``{"extra.client_id": "c"}``). That shape fits
a bounded, scalar-valued contract field (:data:`CredentialMap`), so it can
cross a ``@task`` boundary; :func:`expand_dotted_keys` restores the nested
shape ``CredentialResolver.resolve_raw`` returns, so a task reads both paths
through one parser (see ``AppContext.resolve_credential_raw_or_inline``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, NamedTuple, cast

from pydantic import BaseModel, BeforeValidator

from application_sdk.contracts.types import MaxItems
from application_sdk.credentials.ref import CredentialRef, CredentialResolvable
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.observability.logger_adaptor import get_logger

__all__ = [
    "CredentialMap",
    "CredentialValue",
    "find_prebuilt_credential_ref",
    "flatten_dotted_keys",
    "InlineCredentials",
    "normalize_inline_credentials",
    "ResolvedCredentials",
    "route_credentials",
    "RUN_CREDENTIAL_FIELD_ATTR",
]


logger = get_logger(__name__)

CredentialValue = str | int | float | bool | None
"""One scalar credential value — ``host``, ``port``, ``extra.client_id``, …

Contract fields that carry credentials narrow their values to this: the
payload-safety validator refuses ``Any`` whatever its bound, and a credential
field never legitimately holds anything but a scalar. A structured value (a
list of scopes, say) travels JSON-encoded, as it does on the HTTP path.
"""

_CREDENTIAL_SCALARS = (str, int, float, bool)

_MAX_CREDENTIAL_KEYS = 500
_MAX_CREDENTIAL_PAIRS = 50


def flatten_dotted_keys(nested: Mapping[str, object]) -> dict[str, object]:
    """Flatten nested dicts into dotted keys — the inverse of :func:`expand_dotted_keys`.

    ``{"extra": {"client_id": "c"}, "host": "h"}`` becomes
    ``{"extra.client_id": "c", "host": "h"}``. Keys that are already dotted
    pass through, so flattening a flat dict is a no-op.

    The round trip ``expand_dotted_keys(flatten_dotted_keys(d)) == d`` holds for
    any ``d`` whose keys contain no ``.`` below the top level and which has no
    empty sub-dict (an empty section carries no value, so it has no dotted key
    to travel as).

    Raises:
        ValueError: A key is not a string, two paths land on the same dotted
            key (``{"extra.a": 1, "extra": {"a": 2}}``), or one key is a dotted
            parent of another (``{"host": "h", "host.name": "n"}``) — expanding
            that back would drop one of them. Either value silently winning
            would be a credential nobody asked for. ``ValueError`` so a
            pydantic validator using this reports a validation error.
    """
    flat: dict[str, object] = {}

    def _walk(prefix: str, node: Mapping[str, object]) -> None:
        for key, value in node.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"Credential keys must be strings, got {type(key).__name__}"
                )
            dotted = f"{prefix}{key}"
            if isinstance(value, Mapping):
                _walk(f"{dotted}.", cast("Mapping[str, object]", value))
            elif dotted in flat:
                raise ValueError(f"Credential key {dotted!r} is given twice")
            else:
                flat[dotted] = value

    _walk("", nested)
    for dotted in flat:
        parts = dotted.split(".")
        for depth in range(1, len(parts)):
            parent = ".".join(parts[:depth])
            if parent in flat:
                raise ValueError(
                    f"Credential key {parent!r} is both a value and the parent of {dotted!r}"
                )
    return flat


def _flatten_if_mapping(value: object) -> object:
    """Before-validator: accept the nested shape by flattening it to dotted keys.

    Anything that is not a mapping passes through for normal validation.
    """
    if isinstance(value, Mapping):
        return flatten_dotted_keys(value)
    return value


CredentialMap = Annotated[
    dict[str, CredentialValue],
    MaxItems(_MAX_CREDENTIAL_KEYS),
    BeforeValidator(_flatten_if_mapping),
]
"""Bounded, flat credential dict — the ``inline_credentials`` field of a ``@task`` input.

A nested dict (``{"extra": {"client_id": "c"}}``) is accepted and flattened to
dotted keys on the way in, so a payload written before the field was typed
still validates.
"""

InlineCredentials = (
    Annotated[
        list[Annotated[dict[str, CredentialValue], MaxItems(_MAX_CREDENTIAL_KEYS)]],
        MaxItems(_MAX_CREDENTIAL_PAIRS),
    ]
    | CredentialMap
)
"""The ``credentials`` field of an entry-point input: v3 ``[{key, value}]`` pairs or a dict.

Pass it to :func:`route_credentials`, which normalizes either form to one
:data:`CredentialMap`.
"""


class ResolvedCredentials(NamedTuple):
    """Where a run's credential comes from. At most one field is populated.

    A tuple, so ``ref, inline = route_credentials(input)`` reads as the
    ``(credential_ref, inline_credentials)`` pair apps already thread into
    their ``@task`` inputs. Both empty means the input carries no credential
    at all — a source-less app, or a test that needs none.
    """

    ref: CredentialRef | None
    """Pre-built, GUID, or agent reference, for ``resolve_credential_raw``."""

    inline: dict[str, CredentialValue]
    """Flat, dotted-key inline credentials (local dev and tests only)."""


def normalize_inline_credentials(
    raw: Sequence[Mapping[str, object]] | Mapping[str, object] | None,
) -> dict[str, CredentialValue]:
    """Normalize inline credentials to one flat, dotted-key dict.

    Accepts the v3 ``[{key, value}]`` pairs the HTTP layer produces, a nested
    dict (``{"extra": {...}}``), or a flat dotted dict. A JSON-string ``extra``
    — the other legal ``extra`` shape — is decoded to dotted keys too, so every
    input lands on the same keys.

    Pairs without a string ``key`` are skipped: they cannot name a credential
    field. A pair without ``value`` takes ``""``, as the HTTP layer does.

    Raises:
        CredentialParseError: ``extra`` is a string that does not decode to a
            JSON object, a value is not a :data:`CredentialValue` scalar, two
            pairs or paths name the same key, or a key is the dotted parent of
            another.
    """
    from application_sdk.credentials.errors import (  # noqa: PLC0415 — circular: credentials/__init__ loads sibling modules
        CredentialParseError,
    )
    from application_sdk.credentials.utils import (  # noqa: PLC0415 — circular: credentials/__init__ loads sibling modules
        parse_credentials_extra,
    )

    if not raw:
        return {}

    merged: dict[str, object] = {}
    if isinstance(raw, Mapping):
        merged = dict(raw)
    else:
        for item in raw:
            key = item.get("key") if isinstance(item, Mapping) else None
            if not isinstance(key, str) or not key:
                logger.debug("Skipping an inline credential pair with no string key")
                continue
            if key in merged:
                raise CredentialParseError(
                    message=f"Credential key {key!r} is given twice",
                    credential_name=key,
                )
            merged[key] = item.get("value", "")

    if isinstance(merged.get("extra"), str):
        merged["extra"] = parse_credentials_extra(merged, strict=True)

    try:
        flat = flatten_dotted_keys(merged)
    except ValueError as e:
        raise CredentialParseError(
            message=str(e), credential_name="credentials", cause=e
        ) from e

    for key, value in flat.items():
        if value is not None and not isinstance(value, _CREDENTIAL_SCALARS):
            raise CredentialParseError(
                message=(
                    f"Inline credential {key!r} holds a {type(value).__name__}; "
                    "credential values must be scalars (JSON-encode structured values)"
                ),
                credential_name=key,
            )
    # Every leaf was just checked against the CredentialValue scalars.
    return flat  # pyright: ignore[reportReturnType]


RUN_CREDENTIAL_FIELD_ATTR = "run_credential_field"
"""Input-class attribute naming the field that holds the run's pre-built ref.

Declare it as a ``ClassVar[str]`` on the entry-point input when the model has
more than one ``CredentialRef`` field (or to pin discovery to one field)::

    class MyInput(AppInputContract):
        run_credential_field: ClassVar[str] = "my_credential"

It is a class declaration, not a call argument, so every reader of the input —
:func:`route_credentials` in the entry point and the preflight gate — makes the
same choice. The same convention as ``preflight_credential_refs``.
"""


def _declared_run_credential_field(source: object) -> str | None:
    """The ``run_credential_field`` the input's class declares, if any."""
    from application_sdk.credentials.errors import (  # noqa: PLC0415 — circular: credentials/__init__ loads sibling modules
        CredentialRoutingError,
    )

    source_type = type(source)
    if RUN_CREDENTIAL_FIELD_ATTR in getattr(source_type, "model_fields", {}):
        # A pydantic field, not a ClassVar: it would travel with every payload,
        # and a caller could then choose which credential the run uses.
        raise CredentialRoutingError(
            message=(
                f"{source_type.__name__}.{RUN_CREDENTIAL_FIELD_ATTR} is a model "
                "field; declare it as ClassVar[str]"
            ),
            field=RUN_CREDENTIAL_FIELD_ATTR,
        )
    declared = getattr(source_type, RUN_CREDENTIAL_FIELD_ATTR, None)
    return declared if isinstance(declared, str) and declared else None


_CONNECTION_CREDENTIAL_GUID_KEYS = ("defaultCredentialGuid", "default_credential_guid")
"""Where a Connection carries its default credential GUID: the AE wire name
first, then the snake_case form a Python-built input may use."""


def _connection_default_credential_guid(source: object) -> str:
    """The input's ``connection.attributes.defaultCredentialGuid``, or ``""``.

    Query-extraction (miner) inputs reuse the crawler's connection: AE puts the
    whole Connection on ``connection``, and the credential GUID arrives only on
    its attributes, never on the top-level ``credential_guid``. The preflight
    gate runs before the entrypoint body, so it cannot wait for app code to
    copy the GUID across; it and :meth:`SqlApp.resolve_credential_ref` both
    fall back to this when the input names no credential of its own.

    ``ConnectionAttributes`` does not declare the field; it survives as an
    ``extra="allow"`` value under its wire name. A dict-shaped connection (a
    custom input that never typed it) is read the same way. Never raises: a
    connection this cannot read means no fallback.
    """
    connection = getattr(source, "connection", None)
    if connection is None:
        return ""
    attributes = (
        connection.get("attributes")
        if isinstance(connection, dict)
        else getattr(connection, "attributes", None)
    )
    if attributes is None:
        return ""
    for key in _CONNECTION_CREDENTIAL_GUID_KEYS:
        value = (
            attributes.get(key)
            if isinstance(attributes, dict)
            else getattr(attributes, key, None)
        )
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


_AGENT_NAME_HINT_KEYS = ("agent_name", "agent-name")
"""Top-level input keys that name a Secure Agent without an ``agent_json`` spec.

The preflight outcome row reads these as an agent signal too. ``agent_json``
itself is judged by :func:`normalize_agent_json` instead of by presence: AE
replays a marketplace placeholder blob onto direct runs, and treating that as
an agent would switch the fallback off for exactly the runs that need it."""


def _connection_credential_fallback_guid(
    source: object, snapshot: Mapping[str, object] | None = None
) -> str:
    """The connection's default credential GUID, when *source* may fall back to it.

    The one eligibility rule the preflight gate and
    :meth:`SqlApp.resolve_credential_ref` share, so both resolve the same
    credential. Returns ``""`` unless the input names no credential at all:

    * no ``credential_guid`` and no pre-built ``CredentialRef`` field;
    * no named refs declared in a class-level ``preflight_credential_refs``;
    * no sign of agent mode — ``extraction_method="agent"``, an ``agent_json``
      that parses to a spec, or an ``agent_name`` / ``agent-name`` key.

    Only then is ``connection.attributes.defaultCredentialGuid`` returned (see
    :func:`_connection_default_credential_guid`). Never raises: an input this
    cannot read gets no fallback.

    Args:
        source: The extraction input.
        snapshot: Its ``model_dump(mode="json")``, when the caller already has
            one; computed here otherwise.
    """
    from application_sdk.credentials.errors import (  # noqa: PLC0415
        CredentialRoutingError,
    )
    from application_sdk.credentials.ingress import (  # noqa: PLC0415
        normalize_agent_json,
    )

    guid = getattr(source, "credential_guid", "")
    if guid.strip() if isinstance(guid, str) else guid:
        return ""
    try:
        if find_prebuilt_credential_ref(source) is not None:
            return ""
    except CredentialRoutingError:
        return ""  # several pre-built refs: the input names its credentials
    if getattr(type(source), "preflight_credential_refs", None):
        return ""
    method = getattr(source, "extraction_method", "")
    if isinstance(method, str) and method.strip().lower() == "agent":
        return ""
    if normalize_agent_json(getattr(source, "agent_json", None)) is not None:
        return ""
    if snapshot is None:
        snapshot = _safe_snapshot(source)
    if any(snapshot.get(key) for key in _AGENT_NAME_HINT_KEYS):
        return ""
    return _connection_default_credential_guid(source)


def _safe_snapshot(source: object) -> Mapping[str, object]:
    dump = getattr(source, "model_dump", None)
    if dump is None:
        return {}
    try:
        result = dump(mode="json")
    except Exception:  # an unreadable input gets no agent hints, not a crash
        logger.warning(
            "Could not snapshot the input; reading no agent hints from it",
            exc_info=True,
        )
        return {}
    return result if isinstance(result, Mapping) else {}


def find_prebuilt_credential_ref(source: object) -> CredentialRef | None:
    """Return the :class:`CredentialRef` an input already carries, if any.

    When the input's class declares :data:`RUN_CREDENTIAL_FIELD_ATTR`, only that
    field is read. Otherwise the generic ``credential_ref`` field wins, then the
    one other populated ``CredentialRef`` field on the model — normally the
    ``<app>_credential`` field the contract toolkit generates.

    Raises:
        CredentialRoutingError: No field is declared and more than one other
            ``CredentialRef`` field is populated, so which credential the run
            means is ambiguous; or ``run_credential_field`` is declared as a
            model field instead of a ``ClassVar``.
    """
    declared = _declared_run_credential_field(source)
    if declared is not None:
        value = getattr(source, declared, None)
        return value if isinstance(value, CredentialRef) else None

    generic = getattr(source, "credential_ref", None)
    if isinstance(generic, CredentialRef):
        return generic

    if not isinstance(source, BaseModel):
        return None
    populated = [
        name
        for name in type(source).model_fields
        if isinstance(getattr(source, name, None), CredentialRef)
    ]
    if len(populated) > 1:
        from application_sdk.credentials.errors import (  # noqa: PLC0415 — circular: credentials/__init__ loads sibling modules
            CredentialRoutingError,
        )

        raise CredentialRoutingError(
            message=(
                f"Input carries several credential refs ({', '.join(sorted(populated))}); "
                f"declare {RUN_CREDENTIAL_FIELD_ATTR}: ClassVar[str] on the input "
                "class to say which one the run uses"
            ),
            field=RUN_CREDENTIAL_FIELD_ATTR,
        )
    return getattr(source, populated[0]) if populated else None


def _has_routing_fields(source: object) -> bool:
    """Whether the input names a credential: a GUID, agent mode, or an agent spec.

    Agent mode counts on its own: an ``extraction_method="agent"`` run with an
    empty spec is a misroute, and must reach the strict resolver to be refused
    rather than fall through to inline credentials.
    """
    if getattr(source, "credential_guid", ""):
        return True
    method = getattr(source, "extraction_method", "")
    if isinstance(method, str) and method.strip().lower() == "agent":
        return True
    agent = getattr(source, "agent_json", None)
    return isinstance(agent, AgentCredentialSpec) and agent.is_populated()


def route_credentials(
    source: object,
    *,
    inline_field: str = "credentials",
) -> ResolvedCredentials:
    """Route an input's credential channels into one :class:`ResolvedCredentials`.

    In order:

    1. A pre-built ref (:func:`find_prebuilt_credential_ref`) wins outright.
    2. An input that names a credential — a ``credential_guid``, agent mode, or
       a populated ``agent_json`` — routes through the strict :meth:`CredentialRef.resolve`,
       the same routing the preflight gate uses, so the gate and the tasks
       agree on the credential. A misrouted input (``extraction_method="agent"``
       with an empty spec) raises here and names the cause, rather than falling
       back to a GUID or to inline credentials and failing later with an
       unrelated error. An input that is not
       :class:`~application_sdk.credentials.ref.CredentialResolvable` (no
       ``agent_json`` field) but has a ``credential_guid`` gets a GUID ref.
    3. Otherwise the ``inline_field`` value (``credentials`` by default) is
       normalized by :func:`normalize_inline_credentials` — the local-dev and
       test channel; see the module docstring.

    Args:
        source: The entry-point input. Its class may declare
            :data:`RUN_CREDENTIAL_FIELD_ATTR` to name its pre-built ref field.
        inline_field: Field holding the inline credentials.

    Raises:
        CredentialRoutingError: The input names a credential that cannot be
            routed, or carries several pre-built refs and declares no
            ``run_credential_field``.
        CredentialParseError: The inline credentials are malformed.
    """
    prebuilt = find_prebuilt_credential_ref(source)
    if prebuilt is not None:
        return ResolvedCredentials(prebuilt, {})

    if _has_routing_fields(source):
        guid: str = getattr(source, "credential_guid", "") or ""
        if isinstance(source, CredentialResolvable) or not guid:
            # A non-resolvable input with only an agent spec raises
            # CredentialResolvableTypeError here, naming the missing fields.
            return ResolvedCredentials(CredentialRef.resolve(source), {})  # pyright: ignore[reportArgumentType]
        return ResolvedCredentials(
            CredentialRef(name=guid, credential_type="unknown", credential_guid=guid),
            {},
        )

    inline = normalize_inline_credentials(getattr(source, inline_field, None))
    if inline:
        logger.debug(
            "No credential ref, GUID, or agent spec on the input; using inline credentials",
            inline_keys=sorted(inline),
        )
    return ResolvedCredentials(None, inline)
