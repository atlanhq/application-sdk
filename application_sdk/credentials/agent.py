"""Agent-shape credential resolution for v3 inline agent payloads.

This module mirrors the behaviour of the v2
``application_sdk.services.secretstore.SecretStore.get_credentials()``
flow, but operates on an :class:`AgentCredentialSpec` (the typed
representation of the ``agent_json`` workflow field) rather than a
GUID-indexed blob in the state store.

The ``agent_json`` field on the workflow input describes *how to
resolve* a credential against an external secret manager (AWS Secrets
Manager, Azure Key Vault, K8s secrets) — **not** the secret values
themselves.  Its fields mix literal configuration values (``host``,
``port``, ``aws-region``) with **ref-keys**: string values that name
a field inside the secret bundle stored at ``secret-path``.

Resolution is a three-step process:

1. **Fetch the bundle** at ``secret-path`` via the injected
   :class:`~application_sdk.infrastructure.secrets.SecretStore`.  The
   concrete backend (Dapr + AWS Secrets Manager in production,
   :class:`~application_sdk.testing.mocks.MockSecretStore` under test)
   is opaque to this module.
2. **Substitute** each string value in the agent spec that matches a
   key in the bundle with the real value.  Mirrors v2's
   ``resolve_credentials`` — walks the root dict plus one level of
   ``extra`` when ``extra`` is a nested dict.  Literal keys
   (``host``, ``port``, ``aws-region`` and friends) are never treated
   as ref-keys.
3. **Expand dotted keys** into nested dicts — e.g.
   ``{"extra.database": "db"}`` becomes ``{"extra": {"database": "db"}}``.
   This undoes the flattening the Argo template does when serialising
   YAML ``--parameter`` values. The output is a flat ``dict[str, Any]``
   with any dotted roots (``extra``, ``basic``, …) nested into
   sub-dicts.

The returned shape is **client-agnostic**: it is the same flat-dict
convention v2's resolved credentials produced at the client boundary,
consumable by any SQL, REST, NoSQL, or cloud-storage client whose
``load()`` entry point takes ``dict[str, Any]``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import orjson

from application_sdk.credentials.errors import (
    CredentialError,
    CredentialNotFoundError,
    CredentialParseError,
)
from application_sdk.infrastructure.secrets import SecretNotFoundError
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.credentials.spec import AgentCredentialSpec
    from application_sdk.infrastructure.secrets import SecretStore

logger = get_logger(__name__)

#: Root-level keys whose values are always literals, never ref-keys
#: into the secret bundle. Mirrors the contract the Atlan platform
#: emits for the agent-shape JSON payload.
_LITERAL_KEYS: frozenset[str] = frozenset(
    {
        "connectBy",
        "agent-name",
        "secret-manager",
        "secret-path",
        "aws-region",
        "aws-auth-method",
        "azure-auth-method",
        "host",
        "port",
        "auth-type",
        "agent-type",
        "key-type",
    }
)


async def resolve_agent_credential(
    spec: "AgentCredentialSpec",
    secret_store: "SecretStore",
) -> dict[str, Any]:
    """Resolve a typed agent credential spec to a flat dict.

    Args:
        spec: The :class:`AgentCredentialSpec` parsed from the workflow
            input's ``agent_json`` field.
        secret_store: The :class:`SecretStore` injected into the app's
            :class:`~application_sdk.app.context.AppContext` at worker
            startup.

    Returns:
        A flat ``dict[str, Any]`` with all ref-keys substituted for
        their real values and dotted root keys (``extra.database``,
        ``basic.username``) collapsed into nested dicts.

    Raises:
        CredentialParseError: If ``secret_path`` is empty or the
            fetched bundle isn't valid JSON.
        CredentialNotFoundError: If the secret store does not have
            a bundle at ``secret_path``.
        CredentialError: For any other secret-store failure.
    """
    if not spec.secret_path:
        raise CredentialParseError(
            "agent_json is missing required field 'secret-path'",
        )

    raw = spec.to_raw_dict()
    bundle = await _fetch_bundle(secret_store, spec.secret_path)
    resolved_flat = _substitute(raw, bundle)
    expanded = _expand_dotted(resolved_flat)
    return _flatten_auth_section(expanded)


# Keep backward-compatible alias for existing callers and tests
async def resolve_agent_json(
    agent_json: str,
    secret_store: "SecretStore",
) -> dict[str, Any]:
    """Resolve an agent-shape JSON string to a flat dict.

    Backward-compatible wrapper around :func:`resolve_agent_credential`
    that accepts a raw JSON string.

    Args:
        agent_json: JSON string carried on ``workflow_args["agent_json"]``.
        secret_store: The secret store instance.

    Returns:
        A flat ``dict[str, Any]`` with substituted and expanded credentials.
    """
    from application_sdk.credentials.spec import AgentCredentialSpec

    spec = AgentCredentialSpec.model_validate(agent_json)
    return await resolve_agent_credential(spec, secret_store)


async def _fetch_bundle(
    secret_store: "SecretStore", secret_path: str
) -> dict[str, Any]:
    """Fetch and JSON-parse the secret bundle at ``secret-path``."""
    try:
        raw = await secret_store.get(secret_path)
    except SecretNotFoundError as exc:
        raise CredentialNotFoundError(secret_path) from exc
    except Exception as exc:
        raise CredentialError(
            f"Failed to fetch agent secret bundle at '{secret_path}': {exc}",
            credential_name=secret_path,
            cause=exc,
        ) from exc

    if isinstance(raw, dict):
        # Some SecretStore backends may return a dict directly; accept it.
        return raw
    try:
        bundle = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        raise CredentialParseError(
            f"Agent secret bundle at '{secret_path}' is not valid JSON: {exc}",
            credential_name=secret_path,
            cause=exc,
        ) from exc
    if not isinstance(bundle, dict):
        raise CredentialParseError(
            f"Agent secret bundle at '{secret_path}' must be a JSON object, "
            f"got {type(bundle).__name__}",
            credential_name=secret_path,
        )
    return bundle


def _substitute(agent: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    """Replace ref-key string values in ``agent`` with values from ``bundle``.

    Mirrors v2's
    :meth:`application_sdk.services.secretstore.SecretStore.resolve_credentials`:

    * Walks every root-level key. If the key is in ``_LITERAL_KEYS`` or
      the value is not a string, it is left alone. Otherwise, if the
      string value is a key in ``bundle``, it is replaced with the
      bundle value.
    * If the root dict contains an ``extra`` key whose value is a
      nested dict (the v2-era shape), the same substitution is applied
      one level deep inside it. Mostly a no-op for v3 payloads which
      use dotted-flat keys instead.

    Missing ref-keys are left as-is (same as v2). Downstream code is
    expected to error cleanly if a required field is still a placeholder.
    """
    out: dict[str, Any] = dict(agent)
    for key, value in list(out.items()):
        if key in _LITERAL_KEYS:
            continue
        if isinstance(value, str) and value in bundle:
            out[key] = bundle[value]

    # v2-compat: descend into a nested ``extra`` dict if present.
    extra = out.get("extra")
    if isinstance(extra, dict):
        new_extra = dict(extra)
        for key, value in list(new_extra.items()):
            if isinstance(value, str) and value in bundle:
                new_extra[key] = bundle[value]
        out["extra"] = new_extra

    return out


def _expand_dotted(flat: dict[str, Any]) -> dict[str, Any]:
    """Collapse dotted root keys into nested dicts.

    ``{"basic.username": "u", "extra.database": "d", "host": "h"}``
    becomes
    ``{"basic": {"username": "u"}, "extra": {"database": "d"}, "host": "h"}``.

    Keys without dots are copied as-is. If a dotted key's parent path
    conflicts with a non-dict value already at that root (pathological —
    would only happen if someone mixed ``"extra": {...}`` and
    ``"extra.foo": "bar"`` in the same payload), the existing non-dict
    root wins and the dotted key is dropped with a debug log.
    """
    out: dict[str, Any] = {}

    # Pass 1: copy non-dotted keys first so dicts placed by dotted keys
    # can co-exist with pre-existing dicts at the same root.
    for key, value in flat.items():
        if "." not in key:
            out[key] = value

    # Pass 2: insert dotted keys, merging into existing dicts where possible.
    for key, value in flat.items():
        if "." not in key:
            continue
        parts = key.split(".")
        cursor: Any = out
        ok = True
        for part in parts[:-1]:
            existing = cursor.get(part) if isinstance(cursor, dict) else None
            if existing is None:
                cursor[part] = {}
                cursor = cursor[part]
            elif isinstance(existing, dict):
                cursor = existing
            else:
                logger.debug(
                    "Dotted key %r conflicts with non-dict root %r=%r; skipping",
                    key,
                    part,
                    existing,
                )
                ok = False
                break
        if ok:
            cursor[parts[-1]] = value

    return out


def _flatten_auth_section(creds: dict[str, Any]) -> dict[str, Any]:
    """Promote the auth-type section to root level for client compatibility.

    The agent JSON uses dotted keys like ``basic.username`` which
    ``_expand_dotted`` nests as ``{"basic": {"username": "u"}}``.
    SQL/REST/NoSQL clients expect ``username`` and ``password`` at the
    root level.  This function reads ``auth-type`` (e.g. ``"basic"``),
    finds the matching nested dict, and **deep-merges** its contents
    to root — so existing root-level dicts (e.g. ``extra``) are merged
    rather than overwritten.

    Example::

        {"auth-type": "gcp-wif",
         "extra": {"connect_type": "public"},
         "gcp-wif": {"extra": {"project_id": "p"}}}
        →
        {"auth-type": "gcp-wif",
         "extra": {"connect_type": "public", "project_id": "p"},
         "gcp-wif": {"extra": {"project_id": "p"}}}
    """
    auth_type = creds.get("auth-type", "")
    if not auth_type:
        return creds
    auth_section = creds.get(auth_type)
    if not isinstance(auth_section, dict):
        return creds
    for key, value in auth_section.items():
        existing = creds.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            existing.update(value)
        else:
            creds[key] = value
    return creds
