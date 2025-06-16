"""Secret store for the application."""

import collections.abc
import copy
import json
import time
from typing import Any, Dict, Optional

from dapr.clients import DaprClient

from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class SecretStoreInput:
    _cache_ttl = 300  # 5 minutes cache TTL
    _discovered_component = None
    _discovery_time = 0

    @classmethod
    async def fetch_secret(cls, component_name: str, secret_key: str) -> Dict[str, Any]:
        """Fetch secret using the Dapr component.

        Args:
            component_name: Name of the Dapr component to fetch from
            secret_key: Key of the secret to fetch

        Returns:
            Dict with processed secret data

        Raises:
            Exception: If secret fetching fails
        """
        try:
            with DaprClient() as client:
                secret = client.get_secret(store_name=component_name, key=secret_key)
                return cls._process_secret_data(secret.secret)
        except Exception as e:
            logger.error(
                f"Failed to fetch secret using component {component_name}: {str(e)}"
            )
            raise

    @classmethod
    def _process_secret_data(cls, secret_data: Any) -> Dict[str, Any]:
        """Process raw secret data into a standardized dictionary format.

        Args:
            secret_data: Raw secret data from various sources.

        Returns:
            Dict[str, Any]: Processed secret data as a dictionary.
        """
        # Convert ScalarMapContainer to dict if needed
        if isinstance(secret_data, collections.abc.Mapping):
            secret_data = dict(secret_data)

        # If the dict has a single key and its value is a JSON string, parse it
        if len(secret_data) == 1 and isinstance(next(iter(secret_data.values())), str):
            try:
                parsed = json.loads(next(iter(secret_data.values())))
                if isinstance(parsed, dict):
                    secret_data = parsed
            except Exception:
                pass

        return secret_data

    @classmethod
    def apply_secret_values(
        cls, source_data: Dict[str, Any], secret_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Apply secret values to source data by substituting references.

        This function replaces values in the source data with values
        from the secret data when the source value exists as a key in the secrets.

        Args:
            source_data: Original data with potential references to secrets
            secret_data: Secret data containing actual values

        Returns:
            Dict[str, Any]: Data with secret values applied
        """
        result_data = copy.deepcopy(source_data)

        # Replace values with secret values
        for key, value in list(result_data.items()):
            if isinstance(value, str) and value in secret_data:
                result_data[key] = secret_data[value]

        # Apply the same substitution to the 'extra' dictionary if it exists
        if "extra" in result_data and isinstance(result_data["extra"], dict):
            for key, value in list(result_data["extra"].items()):
                if isinstance(value, str) and value in secret_data:
                    result_data["extra"][key] = secret_data[value]

        return result_data

    @classmethod
    def extract_credentials(cls, credential_guid: str) -> Dict[str, Any]:
        """Extract credentials from the state store using the credential GUID.

        Args:
            credential_guid: The unique identifier for the credentials.

        Returns:
            Dict[str, Any]: The credentials if found.

        Raises:
            ValueError: If the credential_guid is invalid or credentials are not found.
            Exception: If there's an error with the Dapr client operations.

        Examples:
            >>> SecretStoreInput.extract_credentials("1234567890")
            {"username": "admin", "password": "password"}
        """
        if not credential_guid:
            raise ValueError("Invalid credential GUID provided.")
        return StateStoreInput.get_state(f"credential_{credential_guid}")

    @classmethod
    def discover_secret_component(cls, use_cache: bool = True) -> Optional[str]:
        """Discover which secret store component is available using Dapr metadata API.

        Uses the official Dapr component pattern where all secret stores have
        type starting with 'secretstores.'

        Returns:
            Name of the discovered secret component, or None if none found
        """
        # Check cache first
        if use_cache:
            current_time = time.time()
            if (
                cls._discovered_component
                and current_time - cls._discovery_time < cls._cache_ttl
            ):
                return cls._discovered_component

        logger.info(
            "Discovering available secret store components via Dapr metadata..."
        )

        try:
            with DaprClient() as client:
                metadata_response = client.get_metadata()

                # Filter for secret store components using official Dapr pattern
                for comp in metadata_response.registered_components:
                    if comp.type.startswith("secretstores."):
                        logger.info(
                            f"Discovered secret store component: {comp.name} (type: {comp.type})"
                        )

                        # Cache the result
                        cls._discovered_component = comp.name
                        cls._discovery_time = time.time()

                        return comp.name

                # Log available component types for debugging
                available_types = [
                    comp.type for comp in metadata_response.registered_components
                ]
                logger.warning(
                    f"No secret store components found. Available component types: {available_types}"
                )
                return None

        except Exception as e:
            logger.warning(f"Failed to discover secret store components: {e}")
            return None

    @classmethod
    def clear_discovery_cache(cls) -> None:
        """Clear the component discovery cache."""
        cls._discovered_component = None
        cls._discovery_time = 0
