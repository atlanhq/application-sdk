"""Ingress normalisation for the ``agent_json`` wire field.

``agent_json`` names an agent-shape credential *reference* (SDR /
customer-infra runs).  It reaches the SDK in an arbitrary combination of

* three **alias** spellings — ``agent_json``, ``agentJson``, ``agent-json``;
* four **container positions** — top level, ``metadata``,
  ``connection_config``, and ``credentials`` (the last in both the v2 dict
  and the v3 ``list[{key, value}]`` shape);
* three **types** — a JSON string, a dict, or an already-typed
  :class:`~application_sdk.credentials.spec.AgentCredentialSpec`;

and any of those may carry a meaningless **placeholder** rather than a real
reference.  The placeholder is not generated per request: it is a v2 Argo
workflow-template default in ``marketplace-packages`` that submits every
field name as its own value (``{"agent-name": "agent-name", "port": "port",
…}``) because a sibling Argo param indexes straight into the JSON and must
evaluate even in direct mode.  That blob is persisted on the connection
record and replayed verbatim into v3 typed requests, so fixing the templates
stops new poison but cannot clean the connections that already carry it.

This module is the single place that tolerates all of the above.  Two
entry points, one lenient policy — *anything meaningless is absent*:

:func:`normalize_agent_json`
    type layer — one value → :class:`AgentCredentialSpec` or ``None``.
:func:`lift_agent_json`
    wire-body layer — find the freshest binding across every alias and
    position, strip them all, and promote the typed spec to the canonical
    top-level ``agent_json`` key.

Every reader downstream takes the typed field only; none of them re-parses,
re-searches, or re-tolerates.  ``is_populated()`` stays the *consumers'*
call: a spec that validates but carries no fetch anchor is promoted here and
rejected (or not) by whoever resolves it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final, TypeVar, get_args

from pydantic import BaseModel, ValidationError

from application_sdk.credentials.errors import CredentialError
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

AGENT_JSON_ALIASES: Final[tuple[str, ...]] = ("agent_json", "agentJson", "agent-json")
"""Every spelling the field arrives under, in discovery order.

``agent-json`` is the canonical frontend spelling; the other two are what
heracles and AE forward.  All three can appear in one request at once.
"""

_CONTAINERS: Final[tuple[str, ...]] = ("metadata", "connection_config", "credentials")
"""Request containers a binding may be nested in, in discovery order."""

_EMPTY_STRINGS: Final[frozenset[str]] = frozenset({"", "{}"})

SpecT = TypeVar("SpecT", bound=AgentCredentialSpec)


def normalize_agent_json(
    value: Any,
    *,
    spec_type: type[SpecT] = AgentCredentialSpec,
) -> SpecT | None:
    """Canonicalise one ``agent_json`` value to a typed spec, or ``None``.

    Accepts every type the field arrives as — a JSON string, a dict, an
    existing spec — and returns an instance of *spec_type*.  Returns ``None``
    for anything that cannot be a reference: absent, empty (``""``, ``"{}"``,
    ``{}``), unparseable JSON, a non-object JSON value, or a payload that does
    not validate against *spec_type* (the placeholder above — ``port`` is the
    spec's only non-``str`` field, so ``"port": "port"`` coerces to a dict yet
    fails typed validation).

    ``None`` means "no agent reference here", never "the caller should try
    harder".  Callers route on the typed result; nobody catches a parse error.

    Args:
        value: The raw field value, in any accepted wire shape.
        spec_type: The spec class to validate against.  Pass the connector's
            :class:`AgentCredentialSpec` subclass when the reader's field
            declares one, so the reader gets the type it asked for.

    Returns:
        A *spec_type* instance, or ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip() in _EMPTY_STRINGS:
        return None
    if isinstance(value, dict) and not value:
        return None
    if isinstance(value, spec_type):
        return value
    if isinstance(value, AgentCredentialSpec):
        # A base spec where a subclass is declared: round-trip through the
        # wire dict so the reader gets its own type rather than the base.
        value = value.to_raw_dict()

    try:
        return spec_type.model_validate(value)
    except (ValidationError, CredentialError) as exc:
        # Never log exc_info here: pydantic v2 renders the failing field's own
        # input_value into the traceback, and a spec carries connector
        # credential extras as flat dotted keys (``basic.password``,
        # ``api-key``, …) — so a traceback of a rejection lands the raw
        # rejected value in logs. The sanitised error list (no input, no
        # context, no URL) keeps the field path and the reason, which is what
        # a debugger needs.
        # Fully resolved before the call, not composed inside it: a log
        # argument is evaluated eagerly whatever the level, so the sanitising
        # has to be the only thing that ever runs. A CredentialParseError's own
        # text names the parse failure without quoting the payload, so it is
        # safe to pass through.
        details = (
            exc.errors(include_url=False, include_input=False, include_context=False)
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        logger.debug(
            "agent_json is not a valid %s; treating the request as having no "
            "agent reference (typically a marketplace-package placeholder "
            "default replayed from the connection record): %s",
            spec_type.__name__,
            details,
        )
        return None


def declared_agent_spec_type(model_cls: type[BaseModel]) -> type[AgentCredentialSpec]:
    """The spec class *model_cls* declares for its ``agent_json`` field.

    A connector may narrow the field to its own :class:`AgentCredentialSpec`
    subclass (with ``extra="forbid"`` and every dotted key declared).  Passing
    the declared type to :func:`normalize_agent_json` means the reader gets the
    type it asked for, validated by the rules it asked for — rather than a base
    spec its own field would then reject.

    Falls back to :class:`AgentCredentialSpec` when the field is absent or
    declares something else.
    """
    field = model_cls.model_fields.get("agent_json")
    annotation = getattr(field, "annotation", None)
    for candidate in (annotation, *get_args(annotation)):
        if isinstance(candidate, type) and issubclass(candidate, AgentCredentialSpec):
            return candidate
    return AgentCredentialSpec


def lift_agent_json(body: dict[str, Any]) -> dict[str, Any]:
    """Promote the freshest ``agent_json`` binding in *body* to a typed field.

    Handler requests arrive as the frontend form payload forwarded verbatim by
    heracles, so the binding may sit at the top level or nested in any of
    :data:`_CONTAINERS`, under any of :data:`AGENT_JSON_ALIASES`, and several
    copies may arrive at once.  Returns a copy of *body* with

    * every agent-json key removed from every container — a binding left
      inside ``credentials`` would be flattened into a bogus credential pair
      by the v2→v3 credential shim; and
    * ``body["agent_json"]`` set to the typed spec, when one of the copies is
      a real reference.

    When no copy is a real reference the keys are still stripped and no typed
    field is set: the request proceeds as direct mode.  A body carrying no
    binding at all is returned unchanged.

    Trade-off: an SDR user whose spec is genuinely malformed (a non-numeric
    ``port`` they typed themselves) sees "no agent detected" rather than a
    field-level error.  That is deliberate — the alternative is rejecting
    every direct-mode request whose form merely renders the agent widget.
    """
    bindings = list(_iter_bindings(body))
    if not bindings:
        return body

    lifted = _strip_agent_json(body)
    spec = _first_valid(bindings)
    if spec is not None:
        lifted["agent_json"] = spec
    return lifted


def _iter_bindings(body: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    """Every agent-json binding in *body* as ``(alias, raw_value)``.

    Discovery order is top level then each container, alias order within
    each — the order ties are broken in (see :func:`_freshness`).
    """
    for alias in AGENT_JSON_ALIASES:
        if alias in body:
            yield alias, body[alias]

    for container in _CONTAINERS:
        value = body.get(container)
        if isinstance(value, dict):
            for alias in AGENT_JSON_ALIASES:
                if alias in value:
                    yield alias, value[alias]
        elif isinstance(value, list):  # v3 credentials: list[{key, value}]
            for item in value:
                if isinstance(item, dict) and item.get("key") in AGENT_JSON_ALIASES:
                    yield item["key"], item.get("value")


def _freshness(binding: tuple[str, Any]) -> tuple[int, int]:
    """Preference key for competing bindings (higher wins).

    The copies are *not* interchangeable. The frontend normalises its form
    into duplicate keys: the live ``agent-json`` (hyphen) holds the current
    form object, while ``agent_json`` (underscore) is a serialized string
    snapshot that lags behind the user's edits. Picking the wrong one silently
    runs against stale credentials. So prefer a parsed object over a
    serialized string (freshness), then the canonical hyphen spelling
    (tie-break), then discovery order (stable sort). A typed
    :class:`AgentCredentialSpec` counts as parsed — it is the most-processed
    form, so ranking it with the serialized strings would let a stale string
    snapshot beat a current typed spec.
    """
    alias, raw = binding
    return (
        1 if isinstance(raw, (dict, AgentCredentialSpec)) else 0,
        1 if alias == "agent-json" else 0,
    )


def _first_valid(bindings: list[tuple[str, Any]]) -> AgentCredentialSpec | None:
    """The freshest binding that normalises to a real spec, or ``None``.

    Falls through to the next-freshest copy rather than giving up on the
    freshest: a request carrying a placeholder object next to a real
    serialized string is an agent run, not a direct one.
    """
    for _alias, raw in sorted(bindings, key=_freshness, reverse=True):
        spec = normalize_agent_json(raw)
        if spec is not None:
            return spec
    logger.warning(
        "Discarding %d present-but-invalid agent-json binding(s); handling the "
        "request as direct mode (usually a marketplace placeholder, but a "
        "genuinely malformed agent reference downgrades the same way)",
        len(bindings),
    )
    return None


def _strip_agent_json(body: dict[str, Any]) -> dict[str, Any]:
    """Copy of *body* with every agent-json key removed from every container.

    Runs whenever a binding was found, whether or not it gets promoted: a
    placeholder left inside ``credentials`` becomes a bogus credential pair
    just as readily as a real spec would.  Stale top-level aliases go too, so
    the promoted value is the only one Pydantic's ``AliasChoices`` can see.
    """
    stripped: dict[str, Any] = {}
    for key, value in body.items():
        if key in AGENT_JSON_ALIASES:
            continue  # re-added canonically by the caller, if valid
        if key in ("metadata", "connection_config") and isinstance(value, dict):
            stripped[key] = {
                k: v for k, v in value.items() if k not in AGENT_JSON_ALIASES
            }
        elif key == "credentials" and isinstance(value, dict):
            stripped[key] = {
                k: v for k, v in value.items() if k not in AGENT_JSON_ALIASES
            }
        elif key == "credentials" and isinstance(value, list):
            stripped[key] = [
                item
                for item in value
                if not (
                    isinstance(item, dict) and item.get("key") in AGENT_JSON_ALIASES
                )
            ]
        else:
            stripped[key] = value
    return stripped
