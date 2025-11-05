"""Unified secret store service for the application.

Logic summary:
1. Fetch credential config from state store.
2. Determine mode:
     - Multi-key if (credentialSource == 'direct' OR secret-path is defined)
     - Single-key otherwise.
3. Fetch secrets accordingly:
     - Multi-key: use secret_path or credential_guid
     - Single-key: fetch each field individually
4. Merge & resolve secrets.
"""

import collections.abc
import copy
import json
import uuid
from typing import Any, Dict

from dapr.clients import DaprClient

from application_sdk.common.dapr_utils import is_component_registered
from application_sdk.common.error_codes import CommonError
from application_sdk.constants import (
    DEPLOYMENT_NAME,
    DEPLOYMENT_SECRET_PATH,
    DEPLOYMENT_SECRET_STORE_NAME,
    LOCAL_ENVIRONMENT,
    SECRET_STORE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.statestore import StateStore, StateType

logger = get_logger(__name__)


class SecretStore:
    """Unified secret store service for handling secret management across providers."""

    @classmethod
    async def get_credentials(cls, credential_guid: str) -> Dict[str, Any]:
        """Resolve credentials from state store and secret store.

        Supports:
        - Multi-key mode (direct / has secret-path)
        - Single-key mode (no secret-path, non-direct)
        """
        async def _get_credentials_async(credential_guid: str) -> Dict[str, Any]:
            credential_config = await StateStore.get_state(
                credential_guid, StateType.CREDENTIALS
            )

            credential_source = credential_config.get("credentialSource", "direct")
            secret_path = credential_config.get("secret-path")

            secret_data: Dict[str, Any] = {}

            # ---------------------------------------------------------------------
            # Decide mode
            # ---------------------------------------------------------------------
            if credential_source == "direct" or secret_path:
                mode = "multi-key"
            else:
                mode = "single-key"

            # ---------------------------------------------------------------------
            # 1️⃣ Multi-key secret fetch (direct or has secret-path)
            # ---------------------------------------------------------------------
            if mode == "multi-key":
                key_to_fetch = secret_path or credential_guid
                try:
                    logger.debug(f"[SecretStore] Fetching multi-key secret from '{key_to_fetch}'")
                    secret_data = cls.get_secret(secret_key=key_to_fetch)
                except Exception as e:
                    logger.warning(f"Failed to fetch secret bundle '{key_to_fetch}': {e}")
                    secret_data = {}

            # ---------------------------------------------------------------------
            # 2️⃣ Single-key mode → per-field secret lookup
            # ---------------------------------------------------------------------
            else:
                logger.debug(f"[SecretStore] Single-key mode for credential {credential_guid}")
                collected = {}
                for field, value in credential_config.items():
                    if not isinstance(value, str):
                        continue
                    try:
                        single_secret = cls.get_secret(value)
                        if single_secret:
                            for k, v in single_secret.items():
                                collected[k] = v
                    except Exception as e:
                        logger.debug(f"Skipping '{field}' → '{value}' ({e})")
                secret_data = collected

            # ---------------------------------------------------------------------
            # 3️⃣ Merge or resolve references
            # ---------------------------------------------------------------------
            if credential_source == "direct":
                credential_config.update(secret_data)
                return credential_config
            else:
                return cls.resolve_credentials(credential_config, secret_data)

        try:
            return await _get_credentials_async(credential_guid)
        except Exception as e:
            logger.error(f"Error resolving credentials for {credential_guid}: {str(e)}")
            raise CommonError(
                CommonError.CREDENTIALS_RESOLUTION_ERROR,
                f"Failed to resolve credentials: {str(e)}",
            )

    # -------------------------------------------------------------------------
    # Secret resolution helpers
    # -------------------------------------------------------------------------

    @classmethod
    def resolve_credentials(
        cls, credential_config: Dict[str, Any], secret_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Resolve credentials by substituting secret references with actual values."""
        credentials = copy.deepcopy(credential_config)

        for key, value in list(credentials.items()):
            if isinstance(value, str) and value in secret_data:
                credentials[key] = secret_data[value]

        if "extra" in credentials and isinstance(credentials["extra"], dict):
            for key, value in list(credentials["extra"].items()):
                if isinstance(value, str):
                    if value in secret_data:
                        credentials["extra"][key] = secret_data[value]
                    elif value in secret_data.get("extra", {}):
                        credentials["extra"][key] = secret_data["extra"][value]

        return credentials

    # -------------------------------------------------------------------------
    # Dapr secret interactions
    # -------------------------------------------------------------------------

    @classmethod
    def get_deployment_secret(cls) -> Dict[str, Any]:
        """Fetch deployment configuration secrets from deployment secret store."""
        if not is_component_registered(DEPLOYMENT_SECRET_STORE_NAME):
            logger.warning(
                f"Deployment secret store '{DEPLOYMENT_SECRET_STORE_NAME}' not registered."
            )
            return {}

        try:
            return cls.get_secret(DEPLOYMENT_SECRET_PATH, DEPLOYMENT_SECRET_STORE_NAME)
        except Exception as e:
            logger.error(f"Failed to fetch deployment config: {e}")
            return {}

    @classmethod
    def get_secret(
        cls, secret_key: str, component_name: str = SECRET_STORE_NAME
    ) -> Dict[str, Any]:
        """Fetch secret from Dapr secret store component."""
        if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
            return {}

        try:
            with DaprClient() as client:
                dapr_secret_object = client.get_secret(
                    store_name=component_name, key=secret_key
                )
                return cls._process_secret_data(dapr_secret_object.secret)
        except Exception as e:
            logger.error(
                f"Failed to fetch secret using component '{component_name}': {str(e)}"
            )
            raise

    @classmethod
    def _process_secret_data(cls, secret_data: Any) -> Dict[str, Any]:
        """Normalize Dapr secret response into a dictionary."""
        if isinstance(secret_data, collections.abc.Mapping):
            secret_data = dict(secret_data)

        # Handle single-key secrets gracefully
        if len(secret_data) == 1:
            k, v = next(iter(secret_data.items()))
            if isinstance(v, str):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass
                return {k: v}

        return secret_data

    # -------------------------------------------------------------------------
    # Utility helpers
    # -------------------------------------------------------------------------

    @classmethod
    def apply_secret_values(
        cls, source_data: Dict[str, Any], secret_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Replace referenced keys in a dict with resolved secret values."""
        result_data = copy.deepcopy(source_data)

        for key, value in list(result_data.items()):
            if isinstance(value, str) and value in secret_data:
                result_data[key] = secret_data[value]

        if "extra" in result_data and isinstance(result_data["extra"], dict):
            for key, value in list(result_data["extra"].items()):
                if isinstance(value, str) and value in secret_data:
                    result_data["extra"][key] = secret_data[value]

        return result_data

    # -------------------------------------------------------------------------
    # Development utility
    # -------------------------------------------------------------------------

    @classmethod
    async def save_secret(cls, config: Dict[str, Any]) -> str:
        """Store credentials in state store (dev-only)."""
        if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
            credential_guid = str(uuid.uuid4())
            await StateStore.save_state_object(
                id=credential_guid, value=config, type=StateType.CREDENTIALS
            )
            return credential_guid
        else:
            raise ValueError("Storing credentials is not supported in production.")
