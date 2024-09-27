import logging
import uuid

from dapr.clients import DaprClient

from application_sdk.paas.const import STATE_STORE_NAME
from application_sdk.workflows.models.credentials import BasicCredential

logger = logging.getLogger(__name__)


class SecretStore:
    @staticmethod
    def store_credentials(config: BasicCredential) -> str:
        """
        Store credentials in the state store using the BasicCredential format.

        :param config: The BasicCredential object containing the credentials.
        :return: The generated credential GUID.
        :raises Exception: If there's an error with the Dapr client operations.
        """
        client = DaprClient()
        try:
            credential_guid = f"credential_{str(uuid.uuid4())}"
            client.save_state(
                store_name=STATE_STORE_NAME,
                key=credential_guid,
                value=config.model_dump_json(),
            )
            logger.info(f"Credentials stored successfully with GUID: {credential_guid}")
            return credential_guid
        except Exception as e:
            logger.error(f"Failed to store credentials: {str(e)}")
            raise e
        finally:
            client.close()

    @staticmethod
    def extract_credentials(credential_guid: str) -> BasicCredential:
        """
        Extract credentials from the state store using the credential GUID.

        :param credential_guid: The unique identifier for the credentials.
        :return: BasicCredential object if found.
        :raises ValueError: If the credential_guid is invalid or credentials are not found.
        :raises Exception: If there's an error with the Dapr client operations.
        """
        if not credential_guid or not credential_guid.startswith("credential_"):
            raise ValueError("Invalid credential GUID provided.")

        client = DaprClient()
        try:
            state = client.get_state(store_name=STATE_STORE_NAME, key=credential_guid)
            if not state.data:
                raise ValueError(f"Credentials not found for GUID: {credential_guid}")
            return BasicCredential.model_validate_json(state.data)
        except ValueError as e:
            logger.error(str(e))
            raise e
        except Exception as e:
            logger.error(f"Failed to extract credentials: {str(e)}")
            raise e
        finally:
            client.close()