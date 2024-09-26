import logging
from typing import Any, Dict, List

import psycopg2

from application_sdk.dto.credentials import CredentialPayload
from application_sdk.workflows import WorkflowMetadataInterface

logger = logging.getLogger(__name__)


class SQLWorkflowMetadataInterface(WorkflowMetadataInterface):
    METADATA_SQL = ""
    DATABASE_KEY = "TABLE_CATALOG"
    SCHEMA_KEY = "TABLE_SCHEMA"

    # FIXME: duplicate with SQLWorkflowPreflightCheckInterface
    def fetch_metadata(self, credential: Dict[str, Any]) -> List[Dict[str, str]]:
        connection = None
        cursor = None
        try:
            basic_credential = CredentialPayload(**credential).get_credential_config()
            connection = psycopg2.connect(**basic_credential.model_dump())
            cursor = connection.cursor()
            cursor.execute(self.METADATA_SQL)

            result: List[Dict[str, str]] = []
            while True:
                rows = cursor.fetchmany(1000)  # Fetch 1000 rows at a time
                if not rows:
                    break
                for schema_name, catalog_name in rows:
                    result.append(
                        {
                            self.DATABASE_KEY: catalog_name,
                            self.SCHEMA_KEY: schema_name,
                        }
                    )
        except Exception as e:
            logger.error(f"Failed to fetch metadata: {str(e)}")
            raise e
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

        return result
