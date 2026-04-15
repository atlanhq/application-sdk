"""CredentialRef — secret-free frozen Pydantic model for identifying credentials."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict


class CredentialRef(BaseModel, frozen=True):
    """A reference to a credential in the secret store.

    This is a secret-free identifier — it contains no sensitive data itself.
    Use a :class:`~application_sdk.credentials.resolver.CredentialResolver`
    to turn a :class:`CredentialRef` into a typed
    :class:`~application_sdk.credentials.types.Credential` (or a raw dict
    via ``resolver.resolve_raw``).

    ``CredentialRef`` is the single typed identifier for every credential
    resolution flow.  Its populated field tells the resolver which path
    to take:

    * ``agent_json`` non-empty → v3 agent-shape inline resolution.  The
      JSON describes *how* to look up the credential from an external
      secret manager (e.g. AWS Secrets Manager).  The resolver fetches
      the bundle at ``secret-path`` via the injected
      :class:`~application_sdk.infrastructure.secrets.SecretStore`,
      substitutes ref-keys for real values, and returns the result.
    * ``credential_guid`` non-empty → legacy GUID resolution.  The
      resolver checks the local secret store first (for in-process
      inline credentials stored by the handler layer in combined-mode /
      local dev), then falls back to ``DaprCredentialVault`` for
      platform-issued GUIDs.
    * neither → named-path resolution via ``secret_store.get(name)``
      with registry-based typed parsing.
    """

    model_config = ConfigDict(frozen=True)

    name: str = ""
    """Secret store key or human-readable name for this credential.

    Empty when resolution goes through ``agent_json`` — in that case
    the secret-store key lives inside the parsed JSON (``secret-path``).
    """

    credential_type: str = ""
    """Type identifier used to look up the parser in the registry
    (e.g. ``"basic"``, ``"api_key"``).

    Optional for the ``resolve_raw`` path (agent + legacy GUID both
    return raw dicts regardless of type).  Required for the typed
    ``resolve`` path — for agent refs the resolver falls back to the
    ``auth-type`` field inside ``agent_json`` when ``credential_type``
    is empty.
    """

    store_name: str = "default"
    """Which secret store to use (for multi-store setups)."""

    credential_guid: str = ""
    """Platform-issued credential GUID — non-empty triggers GUID resolution path."""

    agent_json: str = ""
    """Inline agent-shape JSON payload — non-empty triggers v3 agent resolution.

    Always stored as a JSON string.  Factories accept either a string
    (wire format) or a ``dict`` (already-parsed Input field) and
    normalise to a string at construction time so the resolver has a
    single consistent shape.
    """

    def __repr__(self) -> str:
        # Intentionally terse — agent_json and credential_guid are
        # logged separately when useful; we don't want the whole JSON
        # blob in every repr.
        agent_flag = "<agent>" if self.agent_json else ""
        guid_flag = f"guid={self.credential_guid[:8]}…" if self.credential_guid else ""
        extras = " ".join(x for x in (agent_flag, guid_flag) if x)
        return (
            f"CredentialRef("
            f"name={self.name!r}, "
            f"credential_type={self.credential_type!r}, "
            f"store_name={self.store_name!r}"
            f"{' ' + extras if extras else ''}"
            f")"
        )

    # ------------------------------------------------------------------
    # Factory — build the right ref from a Temporal workflow payload
    # ------------------------------------------------------------------

    @classmethod
    def from_workflow_args(cls, workflow_args: dict[str, Any]) -> "CredentialRef":
        """Build a :class:`CredentialRef` from a Temporal workflow payload.

        This is the canonical adapter for the Atlan Argo marketplace
        template contract.  It inspects ``extraction_method``,
        ``agent_json``, and ``credential_guid`` on the payload and
        returns a ref with exactly one routing field populated.

        Routing rules:

        +-----------------------+-------------+------------------+-------------+
        | extraction_method     | agent_json  | credential_guid  | Route       |
        +=======================+=============+==================+=============+
        | ``"agent"``           | populated   | any              | agent       |
        +-----------------------+-------------+------------------+-------------+
        | ``"agent"``           | empty/``{}``| set              | legacy GUID |
        +-----------------------+-------------+------------------+-------------+
        | anything else         | any         | set              | legacy GUID |
        +-----------------------+-------------+------------------+-------------+
        | (none applies)        |             |                  | ValueError  |
        +-----------------------+-------------+------------------+-------------+

        ``extraction_method`` is case-insensitive and trimmed.
        ``agent_json`` may arrive as either a JSON string (raw
        wire format from Argo) or an already-parsed ``dict`` (when the
        typed Pydantic Input has coerced it). Both are normalised to a
        JSON string on the returned ref.

        Args:
            workflow_args: The Temporal workflow input dict — typically
                obtained via ``input.model_dump()`` in a task or
                orchestrator.

        Returns:
            A :class:`CredentialRef` with exactly one routing field
            populated (``agent_json`` or ``credential_guid``).

        Raises:
            ValueError: If no routable credential source is present.
        """
        method = (workflow_args.get("extraction_method") or "").strip().lower()
        raw_agent = workflow_args.get("agent_json")

        # Normalise agent_json: accept str (wire format) or dict (typed Input).
        if isinstance(raw_agent, dict):
            agent_json_str = json.dumps(raw_agent) if raw_agent else ""
        else:
            agent_json_str = str(raw_agent or "")

        agent_json_populated = bool(agent_json_str) and agent_json_str.strip() not in (
            "",
            "{}",
        )

        if method == "agent" and agent_json_populated:
            return cls(agent_json=agent_json_str)

        guid = workflow_args.get("credential_guid") or ""
        if guid:
            return cls(
                name=guid,
                credential_type="unknown",
                credential_guid=guid,
            )

        raise ValueError(
            "workflow_args has no routable credential source: need either "
            "extraction_method='agent' with a non-empty agent_json, "
            "or a non-empty credential_guid"
        )


# ---------------------------------------------------------------------------
# Factory functions — produce CredentialRef with the correct credential_type
# ---------------------------------------------------------------------------


def api_key_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for an API key credential."""
    return CredentialRef(name=name, credential_type="api_key", store_name=store_name)


def basic_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for a basic (username/password) credential."""
    return CredentialRef(name=name, credential_type="basic", store_name=store_name)


def bearer_token_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for a bearer token credential."""
    return CredentialRef(
        name=name, credential_type="bearer_token", store_name=store_name
    )


def oauth_client_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for an OAuth client credential."""
    return CredentialRef(
        name=name, credential_type="oauth_client", store_name=store_name
    )


def certificate_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for a certificate credential."""
    return CredentialRef(
        name=name, credential_type="certificate", store_name=store_name
    )


def git_ssh_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for a Git SSH credential."""
    return CredentialRef(name=name, credential_type="git_ssh", store_name=store_name)


def git_token_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for a Git token (PAT/deploy token) credential."""
    return CredentialRef(name=name, credential_type="git_token", store_name=store_name)


def atlan_api_token_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for an Atlan API token credential."""
    return CredentialRef(
        name=name, credential_type="atlan_api_token", store_name=store_name
    )


def atlan_oauth_client_ref(name: str, *, store_name: str = "default") -> CredentialRef:
    """Create a CredentialRef for an Atlan OAuth client credential."""
    return CredentialRef(
        name=name, credential_type="atlan_oauth_client", store_name=store_name
    )


def legacy_credential_ref(guid: str, credential_type: str = "unknown") -> CredentialRef:
    """Create a CredentialRef from a platform-issued credential GUID.

    Wraps a ``credential_guid`` string (issued by the Atlan platform or
    generated in-process by the handler layer) so that templates and
    connectors can resolve it through the standard ``CredentialResolver``
    path.

    The resolver checks the local secret store first (in-process inline
    credentials), then falls back to ``DaprCredentialVault`` for GUIDs
    that only exist in the upstream platform secret store.

    Args:
        guid: The credential GUID.
        credential_type: The credential type hint; defaults to ``"unknown"``
            which causes the resolver to return a ``RawCredential``.
    """
    return CredentialRef(
        name=guid, credential_type=credential_type, credential_guid=guid
    )
