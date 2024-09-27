import logging
from typing import Any, Dict, List

from sqlalchemy import create_engine

from application_sdk.dto.credentials import BasicCredential
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
            engine = create_engine(
                self.get_sql_alchemy_string_fn(credential),
                connect_args=self.get_sql_alchemy_connect_args_fn(credential),
                pool_pre_ping=True,
            )
            connection = engine.connect()

            connection = self.get_connection(credential)
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
