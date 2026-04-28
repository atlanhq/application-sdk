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
            logger.info(
                "Resolving credentials for guid=%s, secret_store=%s",
                credential_guid,
                self._secret_store_name,
            )

            credential_config = await self._fetch_credential_config(credential_guid)
            logger.info(
                "Fetched credential config for guid=%s — "
                "config keys: %s, credentialSource: %s",
                credential_guid,
                sorted(credential_config.keys()),
                credential_config.get("credentialSource", "(not set)"),
            )

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

            logger.info(
                "Credential resolution: guid=%s, source=%s, mode=%s, "
                "secret_path=%s, secret_store=%s",
                credential_guid,
                credential_source.value,
                mode.value,
                secret_path or "(none)",
                self._secret_store_name,
            )

            secret_data: dict[str, Any] = {}

            if mode == _SecretMode.MULTI_KEY:
                key_to_fetch = (
                    secret_path
                    if credential_source == _CredentialSource.AGENT
                    else credential_guid
                )
                try:
                    logger.info(
                        "Fetching multi-key secret: store=%s, key=%s",
                        self._secret_store_name,
                        key_to_fetch,
                    )
                    secret_data = await self._get_secret(key_to_fetch)
                    logger.info(
                        "Secret fetched successfully for guid=%s — " "secret keys: %s",
                        credential_guid,
                        sorted(secret_data.keys()) if secret_data else "(empty)",
                    )
                except Exception as exc:
                    # Vault direct access failed (e.g. 403 permission denied).
                    # Check if the S3 config already contains secrets
                    # (pre-provisioned by Heracles via /config endpoint).
                    _secret_keys = {"password", "token", "extra"}
                    if _secret_keys & set(credential_config.keys()):
                        logger.warning(
                            "Vault secret fetch failed for guid=%s, but "
                            "credential config from S3 already contains "
                            "secret fields %s — using pre-provisioned "
                            "credentials (Heracles flow). Vault error: %s",
                            credential_guid,
                            sorted(_secret_keys & set(credential_config.keys())),
                            exc,
                        )
                        return credential_config
                    # Walk the exception chain to find the httpx response
                    # with the actual Vault error. Chain is typically:
                    # RuntimeError (rewrap) → httpx.HTTPStatusError
                    import httpx as _httpx

                    detail = str(exc)
                    cause: BaseException | None = exc
                    while cause is not None:
                        if isinstance(cause, _httpx.HTTPStatusError):
                            resp = cause.response
                            status = resp.status_code
                            body = resp.text[:500] if resp.text else ""
                            if "permission denied" in body.lower():
                                detail = (
                                    "Vault returned 403 — permission denied. "
                                    "The Dapr secret store token does not "
                                    "have read access to the secret path. "
                                    f"Vault response: {body}"
                                )
                            elif "not found" in body.lower():
                                detail = (
                                    "Secret not found in Vault. The credential "
                                    "may not have been written to the secret "
                                    f"store. Vault response: {body}"
                                )
                            else:
                                detail = (
                                    f"Vault returned HTTP {status}. "
                                    f"Response: {body}"
                                )
                            break
                        cause = getattr(cause, "__cause__", None)

                    raise CredentialVaultError(
                        f"Failed to fetch secrets for credential "
                        f"{credential_guid} from secret store "
                        f"'{self._secret_store_name}' "
                        f"(key={key_to_fetch}): {detail}"
                    ) from exc
            else:
                logger.info(
                    "Fetching single-key secrets for guid=%s",
                    credential_guid,
                )
                secret_data = await self._fetch_single_key_secrets(credential_config)
                logger.info(
                    "Single-key secrets fetched for guid=%s — " "secret keys: %s",
                    credential_guid,
                    sorted(secret_data.keys()) if secret_data else "(empty)",
                )

            if credential_source == _CredentialSource.DIRECT:
                credential_config.update(secret_data)
                merged_keys = sorted(credential_config.keys())
                has_password = "password" in credential_config
                has_token = "token" in credential_config
                logger.info(
                    "Credential resolution complete for guid=%s — "
                    "merged keys: %s, has_password: %s, has_token: %s",
                    credential_guid,
                    merged_keys,
                    has_password,
                    has_token,
                )
                return credential_config
            else:
                result = _resolve_credentials(credential_config, secret_data)
                logger.info(
                    "Credential resolution complete for guid=%s — "
                    "result keys: %s, has_password: %s, has_token: %s",
                    credential_guid,
                    sorted(result.keys()),
                    "password" in result,
                    "token" in result,
                )
                return result

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
            return self._get_local_secret(secret_key)

        store = component_name or self._secret_store_name
        try:
            result = await self._client.get_secret(store_name=store, key=secret_key)
            return process_secret_data(result)
        except Exception as e:
            raise rewrap(e, "Failed to fetch secret (component=%s)" % store) from e

    def _get_local_secret(self, secret_key: str) -> dict[str, Any]:
        """Read secret from the local secrets file for development.

        All secrets are stored in a single ``./local/dapr/secrets/secrets.json``
        file keyed by guid. No user input in filenames.
        """
        from pathlib import Path  # deferred import: only needed in local dev path

        secrets_file = Path(".", "local", "dapr", "secrets", "secrets.json")
        if not secrets_file.exists():
            logger.debug("No local secrets file found")
            return {}
        try:
            all_secrets = json.loads(secrets_file.read_text())
            secret = all_secrets.get(secret_key, {})
            if not secret:
                logger.debug("No local secret for key %s", secret_key)
            return secret
        except Exception:
            logger.debug(
                "Failed to read local secret file for key %s",
                secret_key,
                exc_info=True,
            )
            return {}

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
