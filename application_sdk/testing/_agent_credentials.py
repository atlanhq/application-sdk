"""Agent-bundle credential ref-keys, keyed off the spec's own ``auth_type``.

The emit side of :func:`~application_sdk.common.transforms.transform_agent_credentials`.
That transform collapses ``{authType}.<field>`` to a root-level ``<field>`` and
leaves every other dotted key untouched, so a bundle whose credential keys sit
under a prefix other than the block's own ``auth-type`` reaches the connector's
client with **no credential fields at all** — and the client then fails with its
own source-authentication error, which reads as a provisioning problem rather
than a payload defect (FND-923).

Every builder that emits an agent bundle must take **both** its credential keys
and the ``auth-type`` it writes alongside them from here — via
:func:`agent_credential_ref_keys` and :func:`resolve_auth_type` — so the prefix
and the declared ``auth-type`` in the same block cannot drift apart.

.. note::

   Only the dotted *prefix* is derived here; the two field names are not. The
   platform convention is that every auth option's ``fields`` are named
   ``username`` / ``password`` (snowflake ``keypair``, okta and entra_id all
   carry client id/secret under those names, differing only in
   ``displayName``), which holds for 10 of the 12 ``contract/app.pkl`` files
   surveyed for FND-923. The two known exceptions are
   ``atlan-snowflake-app``'s ``custom_oauth`` (``accessTokenSecret`` / ``role``
   / ``warehouse``) and ``atlan-trino-app``'s ``jwt`` (``__jwt_token`` and
   friends) — for those, a caller must override the harness's ``agent_json()``
   hook rather than rely on this helper. Generating the field names from
   ``credentialAuthOptions[*].fields`` (already the codegen source for the
   DIRECT ``<Connector>CredentialBody``) is tracked as FND-945.
"""

from __future__ import annotations

# Fallback when a spec leaves ``auth_type`` blank. ``transform_agent_credentials``
# builds no prefix at all from an empty ``authType`` (nothing would collapse), so
# blank is treated as the historical default rather than passed through.
DEFAULT_AUTH_TYPE = "basic"


def resolve_auth_type(auth_type: str) -> str:
    """The ``auth-type`` a bundle must declare for its ref-keys to collapse.

    Emitting a blank ``auth-type`` is never correct: ``transform_agent_credentials``
    derives its prefix from that field and an empty prefix is falsy, so *no*
    dotted key collapses and the connector's client sees no credentials — the
    exact FND-923 failure. A blank therefore takes :data:`DEFAULT_AUTH_TYPE`,
    the same fallback :func:`agent_credential_ref_keys` uses for the prefix.

    Args:
        auth_type: The spec's ``auth_type`` (``DatabaseSpec.auth_type``).

    Returns:
        ``auth_type`` unchanged, or :data:`DEFAULT_AUTH_TYPE` when blank.
    """
    return auth_type or DEFAULT_AUTH_TYPE


def agent_credential_ref_keys(
    *,
    auth_type: str,
    connector_short_name: str,
) -> dict[str, str]:
    """Dotted ``{auth_type}.username`` / ``.password`` → SDR secret-store ref-keys.

    In agent mode the values are *secret-store keys*, not literal credentials —
    the agent's local Dapr secret store resolves them at workflow time, so the
    caller is responsible for pre-populating the store with these key names.

    The prefix comes from :func:`resolve_auth_type`, so a caller that writes its
    ``auth-type`` from the same function is guaranteed to agree with these keys.

    Args:
        auth_type: The same value the bundle carries as ``auth-type``
            (``DatabaseSpec.auth_type``). Blank falls back to
            :data:`DEFAULT_AUTH_TYPE`.
        connector_short_name: Connector short name; upper-cased into the ref-key.

    Returns:
        Two dotted keys ready to splat into an agent bundle.
    """
    prefix = resolve_auth_type(auth_type)
    upper = connector_short_name.upper()
    return {
        f"{prefix}.username": f"SDR_{upper}_USERNAME",
        f"{prefix}.password": f"SDR_{upper}_PASSWORD",
    }
