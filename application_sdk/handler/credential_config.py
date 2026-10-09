"""Non-destructive guard for the materialised credential ``config.json``.

``POST /workflows/v1/config/{guid}?type=credentials`` is a blind, whole-object
PUT: the body is never schema-checked (only the path params are) and
:func:`~application_sdk.storage.ops.put_json` overwrites unconditionally. A
request that carries a GUID but an empty body therefore replaces a complete
credential record with ``{"credentialSource": "direct"}``.

That loses exactly the fields with no redundant copy. A credential is assembled
at run time from two stores:

* **Vault** — ``username`` / ``password`` / ``extra``. Survives, because
  :meth:`DaprCredentialVault.get_credentials` merges the secret half *over* the
  object-store half with ``dict.update`` (add-only).
* **Object store** (this file) — ``authType`` / ``host`` / ``port`` /
  ``connectorType`` / ``name`` / ``connectorConfigName``. Gone for good.

So the fields that *select* the auth branch and the API endpoint are the ones
with no backup, while everything the branch then consumes is stored twice. This
module closes that inversion at the write, using the schema the app already
ships.

Where the schema comes from
---------------------------
Each connector generates a credential configmap from its ``credentials.pkl``
contract into ``CONTRACT_GENERATED_DIR`` — e.g.
``app/generated/crawler/atlan-connectors-bigquery.json``. The handler service
already reads these files to serve ``/workflows/v1/configmap/{id}``, so the
schema is on the same disk in the same process as the write endpoint. This
module reads the ``config`` block of the file named by the record's
``connectorConfigName``.

The dialect is **not** JSON Schema
----------------------------------
It is the Atlan form-toolkit dialect, and three differences matter:

* ``"type": "conditional"`` is not a JSON Schema type. BigQuery declares
  ``host``/``port`` conditional on ``extra.connect_type == "private"``;
  Snowflake and Databricks declare them unconditionally required.
* ``"required": true`` sits *inside* a property, not as an array on the parent.
* ``anyOf`` addresses **dotted paths** — ``{"properties": {"extra.connect_type":
  {"const": "private"}}, "required": ["host"]}``. Under real JSON Schema
  semantics that branch never binds (the sibling ``auth-type`` branch already
  passes, and no property is literally named ``extra.connect_type``), so a
  stock ``jsonschema.validate`` would report PASS on precisely the body this
  module exists to catch.

Hence a small dialect-aware evaluator rather than a JSON Schema library.

What is deliberately *not* checked
---------------------------------
Secret-bearing fields, because Heracles strips them before this endpoint ever
sees them — demanding them would reject every legitimate write:

* the per-auth-type branch objects named by ``anyOf`` (``basic``, ``gcp-wif``,
  ``keypair``, ``aws_service``, …). These *are* the secret containers:
  BigQuery's ``basic`` holds ``username`` + ``password``; its ``gcp-wif`` holds
  ``atlan_oauth_secret``.
* top-level ``username`` / ``password``.
* any ``extra`` key whose name matches :data:`_SECRET_NAME_RE` — the same
  ``secret`` / ``private_key`` / ``passphrase`` / ``password`` family Heracles
  redacts from its ``extra`` copy.

Enum values are not policed either. The schema enumerates form values
(``basic``, ``gcp-wif``) while stored records legitimately carry the runtime
alias (``service_account``), so an enum check would reject working credentials.
Presence is the only thing asserted.

The check is a *transition* check
--------------------------------
Nothing here decides whether a credential is valid in the abstract — only
whether this write **drops a required field the stored record already had**.
That keeps three legitimate flows untouched:

* a first-ever create (no stored object ⇒ nothing can be dropped),
* a value change (``authType`` basic → gcp-wif),
* dropping a field that is no longer required — switching BigQuery from
  Private Network Link back to public sends ``extra.connect_type: "public"``
  and omits ``host``, and because the requirement is evaluated *conditionally*
  against the incoming body, ``host`` is correctly not demanded.

See ``ATLAN_CREDENTIAL_CONFIG_GUARD`` in :mod:`application_sdk.constants` for
the enforcement modes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

__all__ = [
    "GUARD_MODE_OFF",
    "GUARD_MODE_REJECT",
    "GUARD_MODE_REPAIR",
    "dropped_required_fields",
    "load_credential_schema",
    "repair_dropped_fields",
]

GUARD_MODE_REPAIR = "repair"
GUARD_MODE_REJECT = "reject"
GUARD_MODE_OFF = "off"

#: ``extra`` keys Heracles redacts from the object-store copy. Mirrors
#: ``stripSensitiveCredentialFields`` — a field matching this never reaches the
#: endpoint, so it must never be required.
_SECRET_NAME_RE = re.compile(
    r"secret|password|passphrase|private[_-]?key|token", re.IGNORECASE
)

#: Top-level credential fields that live only in Vault.
_VAULT_ONLY_FIELDS = frozenset({"username", "password"})


def load_credential_schema(
    record: dict[str, Any],
    existing: dict[str, Any] | None,
    generated_dir: Path,
) -> dict[str, Any] | None:
    """Return the ``config`` block of the record's credential configmap.

    The schema is located by ``connectorConfigName``, read from the incoming
    body first and the stored record second — the stub body that motivates this
    module carries no ``connectorConfigName``, so the stored copy is what
    identifies it.

    Returns ``None`` whenever the schema cannot be identified or read, which
    makes the guard a no-op. An app that ships no credential configmap, or a
    record predating the contract, must not be blocked from saving.
    """
    name = (record.get("connectorConfigName") or "") or (
        (existing or {}).get("connectorConfigName") or ""
    )
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()

    if not generated_dir.exists():
        return None

    for json_file in generated_dir.rglob("*.json"):
        if json_file.stem != name:
            continue
        try:
            import orjson  # noqa: PLC0415 — cold path: only on a credential write

            payload = orjson.loads(json_file.read_bytes())
        except Exception:
            logger.warning(
                "Credential configmap %s is unreadable; skipping the config guard",
                name,
                exc_info=True,
            )
            return None
        config = payload.get("config")
        return config if isinstance(config, dict) else None
    return None


def _auth_branch_names(schema: dict[str, Any]) -> set[str]:
    """Object names that hold per-auth-type secrets, taken from ``anyOf``.

    A branch is ``{"properties": {"auth-type": {"const": X}}, "required": [Y]}``
    — ``Y`` is the object carrying that auth type's fields. Those objects never
    survive Heracles' strip, so they are excluded from the required set. Derived
    from the schema rather than hardcoded, so a connector adding an auth type
    needs no change here.
    """
    names: set[str] = set()
    for branch in schema.get("anyOf") or []:
        if not isinstance(branch, dict):
            continue
        props = branch.get("properties")
        if not isinstance(props, dict):
            continue
        if not any(_property_aliases(key) & {"auth-type"} for key in props):
            continue
        for required in branch.get("required") or []:
            if isinstance(required, str):
                names.add(required)
    return names


def _property_aliases(name: str) -> set[str]:
    """Spellings of a schema property name that a stored record may use.

    The contract declares kebab-case (``auth-type``) while stored records carry
    camelCase (``authType``); some producers send snake_case. All three are the
    same field.
    """
    kebab = name.replace("_", "-")
    snake = name.replace("-", "_")
    head, *rest = snake.split("_")
    camel = head + "".join(part[:1].upper() + part[1:] for part in rest)
    return {name, kebab, snake, camel}


def _resolve_alias(segment: str, mapping: Any) -> str | None:
    """The spelling of ``segment`` that ``mapping`` actually uses, if any."""
    if not isinstance(mapping, dict):
        return None
    for alias in _property_aliases(segment):
        if alias in mapping:
            return alias
    return None


def _lookup(record: dict[str, Any], dotted: str) -> Any:
    """Read a possibly-dotted path, trying every alias at each segment."""
    current: Any = record
    for segment in dotted.split("."):
        alias = _resolve_alias(segment, current)
        if alias is None:
            return None
        current = current[alias]
    return current


def _is_present(record: dict[str, Any], dotted: str) -> bool:
    """True when the path resolves to a non-blank value.

    A key present with ``None`` or ``""`` counts as absent: an empty
    ``authType`` selects no auth branch and an empty ``host`` resolves to the
    public endpoint, so both are losses in every way that matters.
    """
    value = _lookup(record, dotted)
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _required_fields(schema: dict[str, Any], record: dict[str, Any]) -> list[str]:
    """Non-secret fields the schema requires *for this record*.

    ``record`` decides the conditional requirements, so it should be the
    effective post-write shape (stored merged with incoming).
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []

    excluded = _auth_branch_names(schema) | _VAULT_ONLY_FIELDS
    required: list[str] = []

    for name, spec in properties.items():
        if name in excluded or not isinstance(spec, dict):
            continue
        if _SECRET_NAME_RE.search(name):
            continue

        if spec.get("required") is True:
            required.append(name)

        # A conditional field is required only when its condition holds — this
        # is what lets a private → public switch legitimately drop ``host``.
        if spec.get("type") == "conditional":
            for condition in spec.get("conditions") or []:
                if not isinstance(condition, dict) or not condition.get("required"):
                    continue
                target = condition.get("property")
                if not isinstance(target, str):
                    continue
                if _lookup(record, target) == condition.get("value"):
                    required.append(name)
                    break

        # One level of nesting covers `extra`, the only nested object in the
        # credential contracts. Secret-named children are skipped: Heracles
        # redacts them from this copy.
        if spec.get("type") == "object":
            children = spec.get("properties")
            if not isinstance(children, dict):
                continue
            for child, child_spec in children.items():
                if not isinstance(child_spec, dict):
                    continue
                if child_spec.get("required") is not True:
                    continue
                if _SECRET_NAME_RE.search(child) or child in _VAULT_ONLY_FIELDS:
                    continue
                required.append(f"{name}.{child}")

    return required


def dropped_required_fields(
    incoming: dict[str, Any],
    existing: dict[str, Any] | None,
    schema: dict[str, Any] | None,
) -> list[str]:
    """Required, non-secret fields the stored record has and the write loses.

    Empty when there is no schema, no stored record, or the write preserves
    everything required — the guard only ever fires on a genuine regression of
    the stored object.
    """
    if not schema or not existing:
        return []

    effective = {**existing, **incoming}
    return [
        field
        for field in _required_fields(schema, effective)
        if _is_present(existing, field) and not _is_present(incoming, field)
    ]


def _assign(
    record: dict[str, Any],
    dotted: str,
    value: Any,
    reference: dict[str, Any] | None = None,
) -> None:
    """Set a possibly-dotted path using the spelling already in use.

    Alias preference is: whatever ``record`` already uses, else whatever
    ``reference`` (the stored record) uses, else the schema's own spelling.

    The middle case carries the weight. A stub body has no spelling of its own,
    and the contract declares kebab-case (``auth-type``) while stored records and
    every runtime reader use camelCase (``authType``) — so defaulting to the
    schema name would "restore" a field nothing reads, leaving the credential
    just as broken but now silently.
    """
    segments = dotted.split(".")
    current = record
    ref: Any = reference or {}
    for segment in segments[:-1]:
        key = (
            _resolve_alias(segment, current) or _resolve_alias(segment, ref) or segment
        )
        nested = current.get(key)
        if not isinstance(nested, dict):
            nested = {}
            current[key] = nested
        current = nested
        ref_alias = _resolve_alias(segment, ref)
        ref = ref[ref_alias] if ref_alias is not None else {}
    leaf = segments[-1]
    key = _resolve_alias(leaf, current) or _resolve_alias(leaf, ref) or leaf
    current[key] = value


def repair_dropped_fields(
    incoming: dict[str, Any],
    existing: dict[str, Any],
    fields: list[str],
) -> dict[str, Any]:
    """Backfill only ``fields`` from ``existing`` onto a copy of ``incoming``.

    Deliberately not a full merge: a blind ``{**existing, **incoming}`` would
    also resurrect fields the caller meant to remove — a stale private-link
    ``host`` outliving a switch back to the public endpoint, for instance.
    Restoring exactly the required fields that went missing keeps every
    intentional edit intact.
    """
    import copy  # noqa: PLC0415 — cold path: only on a detected drop

    repaired = copy.deepcopy(incoming)
    for field in fields:
        _assign(repaired, field, _lookup(existing, field), reference=existing)
    return repaired
