"""Secret store for the application."""
import json
import logging
from typing import Any, Dict
import uuid

from dapr.clients import DaprClient

logger = logging.getLogger(__name__)


class SecretStore:

    STATE_STORE_NAME = "statestore"

    @classmethod
    def store_credentials(cls, config: Dict[str, Any]) -> str:
        """
        Store credentials in the state store.

        :param config: The credentials to store.
        :return: The generated credential GUID.
        :raises Exception: If there's an error with the Dapr client operations.
        """
        client = DaprClient()
        try:
            credential_guid = f"credential_{str(uuid.uuid4())}"
            client.save_state(
                store_name=cls.STATE_STORE_NAME,
                key=credential_guid,
                value=json.dumps(config),
            )
            logger.info(f"Credentials stored successfully with GUID: {credential_guid}")
            return credential_guid
        except Exception as e:
            logger.error(f"Failed to store credentials: {str(e)}")
            raise e
        finally:
            client.close()

    @classmethod
    def extract_credentials(cls, credential_guid: str) -> Dict[str, Any]:
        """
        Extract credentials from the state store using the credential GUID.

        :param credential_guid: The unique identifier for the credentials.
        :return: The credentials if found.
        :raises ValueError: If the credential_guid is invalid or credentials are not found.
        :raises Exception: If there's an error with the Dapr client operations.
        """
        if not credential_guid or not credential_guid.startswith("credential_"):
            raise ValueError("Invalid credential GUID provided.")

        client = DaprClient()
        try:
            state = client.get_state(store_name=cls.STATE_STORE_NAME, key=credential_guid)
            if not state.data:
                raise ValueError(f"Credentials not found for GUID: {credential_guid}")
            return json.loads(state.data)
        except ValueError as e:
            logger.error(str(e))
            raise e
        except Exception as e:
            logger.error(f"Failed to extract credentials: {str(e)}")
            raise e
        finally:
            client.close()