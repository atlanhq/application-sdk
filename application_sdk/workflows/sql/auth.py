import logging
from typing import Any, Dict

from sqlalchemy import create_engine, text

from application_sdk.workflows import WorkflowAuthInterface

logger = logging.getLogger(__name__)


class SQLWorkflowAuthInterface(WorkflowAuthInterface):
    TEST_AUTHENTICATION_SQL = "SELECT 1;"

    def test_auth(self, credential: Dict[str, Any]) -> bool:
        try:
            engine = create_engine(
                self.get_sql_alchemy_string_fn(credential),
                connect_args=self.get_sql_alchemy_connect_args_fn(credential),
                pool_pre_ping=True,
            )
            with engine.connect() as connection:
                connection.execute(text(self.TEST_AUTHENTICATION_SQL))
            return True
        except Exception as e:
            logger.error(f"Failed to authenticate with the given credentials: {str(e)}")
            raise e
