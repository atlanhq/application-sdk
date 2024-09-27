import logging
from typing import Any, Dict, List, Callable

from sqlalchemy import Engine
from application_sdk.workflows import WorkflowMetadataInterface

logger = logging.getLogger(__name__)


class SQLWorkflowMetadataInterface(WorkflowMetadataInterface):
    METADATA_SQL = ""
    DATABASE_KEY = "TABLE_CATALOG"
    SCHEMA_KEY = "TABLE_SCHEMA"

    def __init__(self, create_engine_fn: Callable[[Dict[str, Any]], Engine]):
        self.create_engine_fn = create_engine_fn

    # FIXME: duplicate with SQLWorkflowPreflightCheckInterface
    def fetch_metadata(self, credential: Dict[str, Any]) -> List[Dict[str, str]]:
        connection = None
        cursor = None
        try:
            engine = self.create_engine_fn(credential)
            connection = engine.connect()

            connection.execute(self.METADATA_SQL)

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
