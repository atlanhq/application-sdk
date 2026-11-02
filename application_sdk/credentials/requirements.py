"""Required credential fields, read from an app's generated forms (FND-502).

An app's contract already declares which credential fields are required, per
auth type: ``required = true`` on each ``FieldSpec`` in ``credentials.pkl``,
generated into two schemas the app ships in ``generated/``:

* the **credential form**, ``<credential-type>.json`` (e.g.
  ``atlan-connectors-bigquery.json``): an ``auth-type`` selector, one object
  per auth type whose ``properties`` are that type's fields (an ``extra``
  object holds the ``extra.*`` ones), shared top-level fields, and ``anyOf``
  implications such as "``extra.connect_type`` is ``private`` ⇒ ``host`` is
  required";
* the **agent schema**, ``agent-json.ui.agentConfigs`` in the workflow form:
  the same per-auth-type shape, where each value is the secret-store key the
  field is read from.

Nothing enforced either before a run, so a workflow could be created with a
required credential field missing and fail only at engine time. This module
reads the requirement from those schemas and reports the gaps; it never
returns, logs or echoes a credential value -- only field paths.

The check is conservative where a value can legitimately be supplied later:
a required field with a non-empty declared default is satisfied by that
default, and ``conditional`` fields are left to the form, which alone knows
when their condition holds.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import orjson

from application_sdk._logging import get_logger

logger = get_logger(__name__)

AUTH_TYPE_KEY = "auth-type"
"""The credential form's auth-type selector property."""

_AUTH_TYPE_VALUE_KEYS: tuple[str, ...] = ("authType", "auth-type", "auth_type")
"""Where a credential carries its selected auth type, in precedence order."""

CREDENTIAL_WIDGET = "credential"
"""``ui.widget`` of a workflow-form property that holds a credential reference."""


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, dict)) and not value)


def _has_non_empty_default(spec: Mapping[str, Any]) -> bool:
    return "default" in spec and not _is_blank(spec["default"])


def _lookup(credential: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    cursor: Any = credential
    for part in path:
        if not isinstance(cursor, Mapping):
            return None
        cursor = cursor.get(part)
    return cursor


def _gaps_in(
    properties: Mapping[str, Any],
    credential: Mapping[str, Any],
    prefix: tuple[str, ...],
    skip: frozenset[str] = frozenset(),
) -> list[tuple[str, ...]]:
    """Required leaves under *properties* that *credential* leaves blank."""
    gaps: list[tuple[str, ...]] = []
    for name, spec in properties.items():
        if name in skip or not isinstance(spec, Mapping):
            continue
        path = (*prefix, name)
        nested = spec.get("properties")
        # The agent schema omits ``type`` on its nested objects; ``properties``
        # alone marks one.
        if spec.get("type", "object") == "object" and isinstance(nested, Mapping):
            gaps.extend(_gaps_in(nested, credential, path))
            continue
        if spec.get("type") == "conditional" or spec.get("required") is not True:
            continue
        if _is_blank(_lookup(credential, path)) and not _has_non_empty_default(spec):
            gaps.append(path)
    return gaps


def selected_auth_type(
    credential: Mapping[str, Any], properties: Mapping[str, Any]
) -> str:
    """The credential's auth type, else the form's declared default, else ``""``."""
    for key in _AUTH_TYPE_VALUE_KEYS:
        value = credential.get(key)
        if isinstance(value, str) and value:
            return value
    selector = properties.get(AUTH_TYPE_KEY)
    if isinstance(selector, Mapping) and isinstance(selector.get("default"), str):
        return selector["default"]
    return ""


def missing_required_fields(
    schema: Mapping[str, Any], credential: Mapping[str, Any]
) -> list[str]:
    """Dotted paths of the required fields *credential* leaves blank.

    Args:
        schema: A credential form's ``config`` or an agent schema -- a mapping
            with ``properties`` (and, for a credential form, ``anyOf``).
        credential: The credential in its canonical nested shape --
            ``{"authType": ..., "username": ..., "extra": {...}}`` -- as
            :func:`application_sdk.common.transforms.transform_agent_credentials`
            produces for an agent payload. Values are only tested for
            presence.

    Returns:
        The missing fields as dotted paths relative to the credential
        (``"extra.atlan_oauth_id"``, ``"authType"``), deduplicated, in schema
        order. Empty when nothing required is missing.
    """
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return []
    auth_type = selected_auth_type(credential, properties)

    selector = properties.get(AUTH_TYPE_KEY)
    variant_names: frozenset[str] = frozenset()
    gaps: list[tuple[str, ...]] = []
    if isinstance(selector, Mapping):
        variant_names = frozenset(
            v for v in selector.get("enum", []) if isinstance(v, str)
        )
        if selector.get("required") is True and not auth_type:
            gaps.append(("authType",))
    else:
        # An agent schema has no selector: every top-level object is a variant.
        variant_names = frozenset(
            name
            for name, spec in properties.items()
            if isinstance(spec, Mapping) and spec.get("type", "object") == "object"
        )

    gaps.extend(
        _gaps_in(
            properties,
            credential,
            (),
            skip=variant_names | {AUTH_TYPE_KEY},
        )
    )
    variant = properties.get(auth_type)
    if auth_type in variant_names and isinstance(variant, Mapping):
        gaps.extend(_gaps_in(variant.get("properties") or {}, credential, ()))

    gaps.extend(_implied_gaps(schema.get("anyOf"), properties, credential))

    seen: set[str] = set()
    ordered: list[str] = []
    for gap in gaps:
        dotted = ".".join(gap)
        if dotted not in seen:
            seen.add(dotted)
            ordered.append(dotted)
    return ordered


def _implied_gaps(
    rules: Any, properties: Mapping[str, Any], credential: Mapping[str, Any]
) -> list[tuple[str, ...]]:
    """Fields an ``anyOf`` implication requires when its condition holds.

    Each rule is ``{"properties": {<dotted>: {"const": v}}, "required": [...]}``
    and is read as an implication: when every ``const`` matches the
    credential (a field it omits takes the form's declared default), every
    named field must be present. A name that is an auth-type variant is the
    selector's own branch, already checked field by field.
    """
    if not isinstance(rules, list):
        return []
    selector = properties.get(AUTH_TYPE_KEY)
    variants = (
        frozenset(selector.get("enum", []))
        if isinstance(selector, Mapping)
        else frozenset()
    )
    gaps: list[tuple[str, ...]] = []
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        conditions = rule.get("properties")
        required = rule.get("required")
        if not isinstance(conditions, Mapping) or not isinstance(required, list):
            continue
        if not conditions or not all(
            isinstance(c, Mapping) and "const" in c for c in conditions.values()
        ):
            continue
        if not all(
            _value_or_default(credential, properties, key) == cond["const"]
            for key, cond in conditions.items()
        ):
            continue
        for name in required:
            if not isinstance(name, str) or name in variants:
                continue
            path = tuple(name.split("."))
            spec = _spec_at(properties, path)
            if _is_blank(_lookup(credential, path)) and not (
                spec is not None and _has_non_empty_default(spec)
            ):
                gaps.append(path)
    return gaps


def _spec_at(
    properties: Mapping[str, Any], path: tuple[str, ...]
) -> Mapping[str, Any] | None:
    cursor: Any = {"properties": properties}
    for part in path:
        nested = cursor.get("properties") if isinstance(cursor, Mapping) else None
        if not isinstance(nested, Mapping):
            return None
        cursor = nested.get(part)
    return cursor if isinstance(cursor, Mapping) else None


def _value_or_default(
    credential: Mapping[str, Any], properties: Mapping[str, Any], dotted: str
) -> Any:
    if dotted == AUTH_TYPE_KEY:
        return selected_auth_type(credential, properties)
    path = tuple(dotted.split("."))
    value = _lookup(credential, path)
    if value is None:
        spec = _spec_at(properties, path)
        if spec is not None:
            return spec.get("default")
    return value


def canonical_credential(flat: Mapping[str, Any]) -> dict[str, Any]:
    """Nest a flat credential the way the runtime reads it.

    The same expansion as
    :func:`application_sdk.common.transforms.transform_agent_credentials`
    (pinned against it by a parity test; that module is not in the api
    distribution): ``auth-type`` → ``authType``; ``extra.<f>`` and
    ``<authType>.extra.<f>`` → ``extra[<f>]`` (the auth-type one wins);
    ``<authType>.<f>`` → root ``<f>``. A nested ``extra`` dict is merged as
    sent. Accepts a v2 dict, a v3 ``[{key, value}]`` list already folded into
    a dict, or an agent spec's raw dict.
    """
    creds: dict[str, Any] = {k: v for k, v in flat.items() if "." not in k}
    if "auth-type" in creds:
        creds["authType"] = creds.pop("auth-type")
    auth_type = creds.get("authType") or ""
    auth_prefix = f"{auth_type}." if auth_type else None
    extra_prefix = f"{auth_type}.extra." if auth_type else None
    extra: dict[str, Any] = (
        dict(creds["extra"]) if isinstance(creds.get("extra"), Mapping) else {}
    )
    for key, value in flat.items():
        if key.startswith("extra."):
            extra[key[len("extra.") :]] = value
    for key, value in flat.items():
        if extra_prefix and key.startswith(extra_prefix):
            extra[key[len(extra_prefix) :]] = value
        elif auth_prefix and key.startswith(auth_prefix):
            creds[key[len(auth_prefix) :]] = value
    if extra:
        creds["extra"] = extra
    return creds


def credential_from_pairs(pairs: list[Any]) -> dict[str, Any]:
    """Fold v3 ``[{key, value}]`` credential pairs into a canonical credential."""
    flat: dict[str, Any] = {}
    for pair in pairs:
        if isinstance(pair, Mapping) and isinstance(pair.get("key"), str):
            flat[pair["key"]] = pair.get("value")
    return canonical_credential(flat)


# ---------------------------------------------------------------------------
# Generated-form lookups
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Mapping[str, Any] | None:
    try:
        loaded = orjson.loads(path.read_bytes())
    except (OSError, orjson.JSONDecodeError):
        logger.warning("Unreadable generated form %s; skipping its checks", path.name)
        return None
    return loaded if isinstance(loaded, Mapping) else None


def load_form(path: Path | None) -> Mapping[str, Any] | None:
    """A generated form's ``config`` object, or ``None`` when absent/unreadable."""
    if path is None or not path.is_file():
        return None
    loaded = _load_json(path)
    config = loaded.get("config") if loaded is not None else None
    return config if isinstance(config, Mapping) else None


def credential_form(
    generated_dir: Path, credential_type: str
) -> Mapping[str, Any] | None:
    """The credential form ``<credential_type>.json`` from *generated_dir*, if shipped."""
    if not credential_type or "/" in credential_type or credential_type.startswith("."):
        return None
    for candidate in sorted(generated_dir.rglob(f"{credential_type}.json")):
        return load_form(candidate)
    return None


class CredentialSlot:
    """One credential the workflow form asks for.

    Attributes:
        field: The form property, e.g. ``credential-guid``.
        credential_type: The credential form it is created from, e.g.
            ``atlan-connectors-bigquery``.
        required: Whether the form marks it required.
    """

    __slots__ = ("credential_type", "field", "required")

    def __init__(self, field: str, credential_type: str, required: bool) -> None:
        self.field = field
        self.credential_type = credential_type
        self.required = required


def credential_slots(workflow_form: Mapping[str, Any]) -> list[CredentialSlot]:
    """The credential properties a workflow form declares, in form order."""
    properties = workflow_form.get("properties")
    if not isinstance(properties, Mapping):
        return []
    slots: list[CredentialSlot] = []
    for name, spec in properties.items():
        if not isinstance(spec, Mapping):
            continue
        ui = spec.get("ui")
        if not isinstance(ui, Mapping) or ui.get("widget") != CREDENTIAL_WIDGET:
            continue
        credential_type = ui.get("credentialType")
        if isinstance(credential_type, str) and credential_type:
            slots.append(
                CredentialSlot(name, credential_type, spec.get("required") is True)
            )
    return slots


def agent_schema(
    workflow_form: Mapping[str, Any], credential_type: str
) -> Mapping[str, Any] | None:
    """The agent schema for *credential_type* from the workflow form's ``agent-json``."""
    properties = workflow_form.get("properties")
    agent = properties.get("agent-json") if isinstance(properties, Mapping) else None
    ui = agent.get("ui") if isinstance(agent, Mapping) else None
    configs = ui.get("agentConfigs") if isinstance(ui, Mapping) else None
    if not isinstance(configs, list):
        return None
    for entry in configs:
        if (
            isinstance(entry, list)
            and len(entry) == 2
            and entry[0] == credential_type
            and isinstance(entry[1], Mapping)
        ):
            return entry[1]
    return None
