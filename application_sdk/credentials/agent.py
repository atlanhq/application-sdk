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

import hashlib
import re
import traceback
from typing import TYPE_CHECKING, Any

import orjson

from application_sdk.common.transforms import transform_agent_credentials
from application_sdk.credentials.errors import (
    CredentialError,
    CredentialNotFoundError,
    CredentialParseError,
)
from application_sdk.errors import redact_secrets
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
    spec: AgentCredentialSpec,
    secret_store: SecretStore,
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
        CredentialParseError: If the fetched bundle isn't valid JSON.
        CredentialNotFoundError: If the secret store does not have
            a bundle at ``secret_path``.
        CredentialError: For any other secret-store failure.

    Note:
        Resolution mode is selected by the spec:

        * ``key-type: single-key`` — each ref-key is fetched as a
          separate secret store entry (one entry per credential field).
          Useful for ``secretstores.local.env``-backed deployments
          where each env var holds one credential value, avoiding the
          all-in-one-JSON-bundle workaround. Non-secret fields
          (host/port) silently fall through unchanged.
        * ``secret-path`` set (and ``key-type`` not single-key): the
          bundle is fetched once from ``secret_path`` and ref-keys are
          substituted from it (the original v2 multi-key behavior).
        * Both empty: raw spec values are used as-is (no store
          lookup). Intended for dev/testing where ``agent_json``
          carries literal credentials inline.
    """
    raw = spec.to_raw_dict()

    if spec.key_type == "single-key":
        bundle = await _fetch_per_key_bundle(secret_store, raw)
        resolved_flat = _substitute(raw, bundle)
    elif spec.secret_path:
        bundle = await _fetch_bundle(secret_store, spec.secret_path)
        resolved_flat = _substitute(raw, bundle)
    else:
        resolved_flat = raw

    return transform_agent_credentials(resolved_flat)


# Keep backward-compatible alias for existing callers and tests
async def resolve_agent_json(
    agent_json: str,
    secret_store: SecretStore,
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
    from application_sdk.credentials.spec import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
        AgentCredentialSpec,
    )

    spec = AgentCredentialSpec.model_validate(agent_json)
    return await resolve_agent_credential(spec, secret_store)


async def _fetch_bundle(secret_store: SecretStore, secret_path: str) -> dict[str, Any]:
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


async def _fetch_per_key_bundle(
    secret_store: SecretStore, raw: dict[str, Any]
) -> dict[str, Any]:
    """Build a synthetic bundle by per-key lookups against the secret store.

    For ``key-type: single-key`` agent specs, each non-literal string field
    value is treated as its own secret store key. The returned bundle maps
    each successfully fetched ref-key to its real value, so that the
    existing :func:`_substitute` step can finish substitution unchanged.

    Missing keys are silently skipped — single-key mode probes every
    non-literal field value, so a non-secret field (like ``host`` carrying
    a hostname) won't fail the resolution. Unmatched ref-keys then take
    the v2-parity fallthrough in :func:`_substitute` (left as-is, surfaced
    by downstream connect errors).
    """
    bundle: dict[str, Any] = {}
    seen: set[str] = set()

    async def _try_fetch(value: str) -> None:
        if not value or value in seen:
            return
        seen.add(value)
        try:
            secret = await secret_store.get_optional(value)
        except Exception as exc:
            # Store-side error — distinct from "key not in store" (silent
            # below). A transient outage here on a real secret field
            # would otherwise auth-fail with the ref-key as the literal
            # username, so surface at WARNING with the stack trace.
            # Log a hash, not the ref-key itself: ref-key names encode secret
            # store topology (purpose, environment) and enable enumeration if
            # logs leak.
            value_hash = hashlib.sha256(value.encode()).hexdigest()[:8]
            # NOT exc_info=True: SecretStoreError.__str__ renders `secret=<ref-key>`
            # and its message embeds the backend cause, which can echo the raw
            # ref-key — that would undo the hashing above in the same log record.
            # Format the traceback ourselves, redact known secret patterns, and
            # additionally scrub the literal ref-key (which redact_secrets can't
            # know) so the topology stays hidden while diagnosis survives.
            # Bound the ref-key match to standalone tokens: a literal replace of
            # a short key like "DB" would corrupt "DB_CONNECTION"; the
            # lookarounds treat word chars and hyphens as identifier-continuation
            # so only whole-token occurrences are scrubbed.
            safe_traceback = re.sub(
                rf"(?<![\w-]){re.escape(value)}(?![\w-])",
                f"sha256:{value_hash}",
                redact_secrets("".join(traceback.format_exception(exc))),
            )
            logger.warning(  # conformance: ignore[E005] exc_info would bypass the secret-redacted traceback built above; safe_traceback included inline
                "single-key probe failed for ref-key sha256:%s — store error, "
                "treating as non-secret. If this was a real credential "
                "key, the auth attempt will fail with the ref-key as the "
                "literal value.\n%s",
                value_hash,
                safe_traceback,
            )
            return
        if secret in (None, ""):
            # Key not in store — expected for non-secret fields probed
            # in single-key mode (host, port, region literals).
            logger.debug(
                "single-key probe: sha256:%s not found in store (non-secret field)",
                hashlib.sha256(value.encode()).hexdigest()[:8],
            )
            return
        bundle[value] = secret

    for key, value in raw.items():
        if key in _LITERAL_KEYS:
            continue
        if isinstance(value, str):
            await _try_fetch(value)

    extra = raw.get("extra")
    if isinstance(extra, dict):
        for value in extra.values():
            if isinstance(value, str):
                await _try_fetch(value)

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
