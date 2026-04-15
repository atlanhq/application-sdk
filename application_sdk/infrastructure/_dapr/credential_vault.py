"""Dapr-backed credential vault for GUID-based runtime credential resolution.

Fetches credential config from S3 via Dapr binding, then resolves secret
references via the Dapr secret store.
"""

import copy
import json
import re
from enum import Enum
from typing import Any

from application_sdk.infrastructure._dapr.http import AsyncDaprClient
from application_sdk.infrastructure._secret_utils import process_secret_data
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Allowlist: UUIDs, hex strings, and similar safe identifiers.
_SAFE_GUID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


class _CredentialSource(str, Enum):
    DIRECT = "direct"
    AGENT = "agent"


class _SecretMode(Enum):
    MULTI_KEY = "multi-key"
    SINGLE_KEY = "single-key"


def _handle_single_key_secret(key: str, value: Any) -> dict[str, Any]:
    """Handle single-key secret — attempt JSON parse, fall back to {key: value}."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {key: value}


def _resolve_credentials(
    credential_config: dict[str, Any], secret_data: dict[str, Any]
) -> dict[str, Any]:
    """Substitute secret references in *credential_config* with values from *secret_data*."""
    credentials = copy.deepcopy(credential_config)
    for key, value in list(credentials.items()):
        if isinstance(value, str) and value in secret_data:
            credentials[key] = secret_data[value]
    if "extra" in credentials and isinstance(credentials["extra"], dict):
        for key, value in list(credentials["extra"].items()):
            if isinstance(value, str) and value in secret_data:
                credentials["extra"][key] = secret_data[value]
    return credentials


# ---------------------------------------------------------------------------
# DaprCredentialVault
# ---------------------------------------------------------------------------


class DaprCredentialVault:
    """Dapr-backed credential vault for GUID-based runtime credential resolution.

    Fetches a credential config record from the upstream object store (S3 via
    Dapr binding), then resolves secret references within it via the Dapr
    secret store.
    """

    def __init__(
        self,
        client: AsyncDaprClient,
        *,
        upstream_binding_name: str | None = None,
        secret_store_name: str | None = None,
    ) -> None:
        # Deferred import: circular dependency with constants module
        from application_sdk.constants import (
            SECRET_STORE_NAME,
            UPSTREAM_OBJECT_STORE_NAME,
        )
        from application_sdk.infrastructure._dapr.client import DaprBinding

        self._client = client
        self._upstream = DaprBinding(
            client, upstream_binding_name or UPSTREAM_OBJECT_STORE_NAME
        )
        self._secret_store_name: str = secret_store_name or SECRET_STORE_NAME

    async def get_credentials(self, credential_guid: str) -> dict[str, Any]:
        """Resolve the full credential dict for *credential_guid*.

        1. Fetches the credential config JSON from the upstream S3 store
           (path: ``persistent-artifacts/apps/{app}/credentials/{guid}/config.json``).
        2. Determines the secret resolution mode (multi-key or single-key).
        3. Fetches secrets from the Dapr secret store.
        4. Returns the merged credential dict.

        Raises:
            CredentialVaultError: If any step fails.
        """
        # Deferred import: circular dependency
        from application_sdk.infrastructure.credential_vault import CredentialVaultError

        try:
            credential_config = await self._fetch_credential_config(credential_guid)

            credential_source_str = credential_config.get(
                "credentialSource", _CredentialSource.DIRECT.value
            )
            try:
                credential_source = _CredentialSource(credential_source_str)
            except ValueError:
                credential_source = _CredentialSource.DIRECT

            secret_path = credential_config.get("secret-path")

            if credential_source == _CredentialSource.DIRECT or secret_path:
                mode = _SecretMode.MULTI_KEY
            else:
                mode = _SecretMode.SINGLE_KEY

            secret_data: dict[str, Any] = {}

            if mode == _SecretMode.MULTI_KEY:
                key_to_fetch = (
                    secret_path
                    if credential_source == _CredentialSource.AGENT
                    else credential_guid
                )
                try:
                    logger.debug("Fetching multi-key secret: %s", key_to_fetch)
                    secret_data = await self._get_secret(key_to_fetch)
                except Exception:
                    logger.warning(
                        "Failed to fetch secret bundle: %s",
                        key_to_fetch,
                        exc_info=True,
                    )
            else:
                secret_data = await self._fetch_single_key_secrets(credential_config)

            if credential_source == _CredentialSource.DIRECT:
                credential_config.update(secret_data)
                return credential_config
            else:
                return _resolve_credentials(credential_config, secret_data)

        except CredentialVaultError:
            raise
        except Exception as e:
            raise CredentialVaultError(
                f"Failed to resolve credentials for {credential_guid}: {e}",
                credential_guid=credential_guid,
                cause=e,
            ) from e

    async def _fetch_credential_config(self, credential_guid: str) -> dict[str, Any]:
        """Fetch the credential config JSON from the upstream object store.

        Raises:
            CredentialVaultError: If the GUID contains unsafe characters or no
                config is found in the upstream store.
        """
        import os

        # Deferred imports: circular dependency with constants and storage modules
        from application_sdk.constants import (
            APPLICATION_NAME,
            STATE_STORE_PATH_TEMPLATE,
            TEMPORARY_PATH,
        )
        from application_sdk.infrastructure.credential_vault import CredentialVaultError
        from application_sdk.storage.ops import normalize_key

        # Validate before interpolation to prevent path traversal.
        if not _SAFE_GUID_RE.match(credential_guid):
            raise CredentialVaultError(
                "Invalid credential GUID — must match [a-zA-Z0-9_-]+: %r"
                % credential_guid,
                credential_guid=credential_guid,
            )

        raw_path = os.path.join(
            TEMPORARY_PATH,
            STATE_STORE_PATH_TEMPLATE.format(
                application_name=APPLICATION_NAME,
                state_type="credentials",
                id=credential_guid,
            ),
        )
        normalized_key = normalize_key(raw_path)

        data = json.dumps({"key": normalized_key}).encode("utf-8")
        metadata = {
            "key": normalized_key,
            "fileName": normalized_key,
            "blobName": normalized_key,
        }

        response = await self._upstream.invoke("get", data=data, metadata=metadata)

        if response.data is None:
            raise CredentialVaultError(
                "No credential config found for GUID %s in upstream store"
                % credential_guid,
                credential_guid=credential_guid,
            )

        return json.loads(response.data)

    async def _get_secret(
        self, secret_key: str, component_name: str | None = None
    ) -> dict[str, Any]:
        """Fetch and process a secret from the Dapr secret store.

        Returns ``{}`` in local-environment deployments to avoid secret store
        dependency during development.
        """
        # Deferred import: circular dependency
        from application_sdk.common.exc_utils import rewrap
        from application_sdk.constants import DEPLOYMENT_NAME, LOCAL_ENVIRONMENT

        if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
            return {}

        store = component_name or self._secret_store_name
        try:
            result = await self._client.get_secret(store_name=store, key=secret_key)
            return process_secret_data(result)
        except Exception as e:
            raise rewrap(e, "Failed to fetch secret (component=%s)" % store) from e

    async def _fetch_single_key_secrets(
        self, credential_config: dict[str, Any]
    ) -> dict[str, Any]:
        """Fetch secrets in single-key mode — one lookup per string field value."""
        logger.debug("Single-key mode: fetching secrets per field")
        collected: dict[str, Any] = {}
        failed_lookups: list[str] = []

        async def _try_fetch(label: str, value: str) -> None:
            if not value.strip():
                return
            try:
                single_secret = await self._get_secret(value)
                if single_secret:
                    for k, v in single_secret.items():
                        if v is None or v == "":
                            continue
                        collected[k] = v
            except Exception as e:
                failed_lookups.append("  '%s' → '%s': %s" % (label, value, e))

        for field, value in credential_config.items():
            if isinstance(value, str):
                await _try_fetch(field, value)
            elif field == "extra" and isinstance(value, dict):
                for extra_key, extra_value in value.items():
                    if isinstance(extra_value, str):
                        await _try_fetch(f"extra.{extra_key}", extra_value)

        if not collected and failed_lookups:
            logger.error(
                "Single-key secret resolution failed: no secrets resolved. "
                "%d attempted, all failed:\n%s",
                len(failed_lookups),
                "\n".join(failed_lookups),
            )
        elif failed_lookups:
            logger.debug(
                "Single-key mode: resolved %d secrets, skipped %d non-secret fields",
                len(collected),
                len(failed_lookups),
            )

        return collected


def create_dapr_credential_vault(
    client: AsyncDaprClient,
    *,
    upstream_binding_name: str | None = None,
    secret_store_name: str | None = None,
) -> "DaprCredentialVault":
    """Create a Dapr-backed credential vault.

    Args:
        client: Dapr client instance.
        upstream_binding_name: Dapr binding component for the upstream S3 store.
            Defaults to the ``UPSTREAM_OBJECT_STORE_NAME`` constant.
        secret_store_name: Dapr secret store component name.
            Defaults to the ``SECRET_STORE_NAME`` constant.

    Returns:
        Configured DaprCredentialVault.
    """
    return DaprCredentialVault(
        client,
        upstream_binding_name=upstream_binding_name,
        secret_store_name=secret_store_name,
    )
