"""Agent-bundle credential ref-keys, keyed off the spec's own ``auth_type``.

The emit side of :func:`~application_sdk.common.transforms.transform_agent_credentials`.
That transform collapses ``{authType}.<field>`` to a root-level ``<field>`` and
leaves every other dotted key untouched, so a bundle whose credential keys sit
under a prefix other than the block's own ``auth-type`` reaches the connector's
client with **no credential fields at all** — and the client then fails with its
own source-authentication error, which reads as a provisioning problem rather
than a payload defect (FND-923).

Every builder that emits an agent bundle must take its credential keys from
here so the prefix and the ``auth-type`` in the same block cannot drift apart.
"""

from __future__ import annotations

# Fallback prefix when a spec leaves ``auth_type`` blank. ``transform_agent_credentials``
# builds no prefix at all from an empty ``authType`` (nothing would collapse), so
# blank is treated as the historical default rather than passed through.
DEFAULT_AUTH_TYPE = "basic"


def agent_credential_ref_keys(
    *,
    auth_type: str,
    connector_short_name: str,
) -> dict[str, str]:
    """Dotted ``{auth_type}.username`` / ``.password`` → SDR secret-store ref-keys.

    In agent mode the values are *secret-store keys*, not literal credentials —
    the agent's local Dapr secret store resolves them at workflow time, so the
    caller is responsible for pre-populating the store with these key names.

    Args:
        auth_type: The same value the bundle carries as ``auth-type``
            (``DatabaseSpec.auth_type``). Blank falls back to
            :data:`DEFAULT_AUTH_TYPE`.
        connector_short_name: Connector short name; upper-cased into the ref-key.

    Returns:
        Two dotted keys ready to splat into an agent bundle.
    """
    prefix = auth_type or DEFAULT_AUTH_TYPE
    upper = connector_short_name.upper()
    return {
        f"{prefix}.username": f"SDR_{upper}_USERNAME",
        f"{prefix}.password": f"SDR_{upper}_PASSWORD",
    }
