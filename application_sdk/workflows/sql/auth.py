import logging
from typing import Any, Dict, Callable

from sqlalchemy import create_engine, text, Engine

from application_sdk.workflows import WorkflowAuthInterface

logger = logging.getLogger(__name__)


class SQLWorkflowAuthInterface(WorkflowAuthInterface):
    TEST_AUTHENTICATION_SQL = "SELECT 1;"

    def __init__(self, create_engine_fn: Callable[[Dict[str, Any]], Engine]):
        self.create_engine_fn = create_engine_fn

    def test_auth(self, credential: Dict[str, Any]) -> bool:
        try:
            engine = self.create_engine_fn(credential)
            with engine.connect() as connection:
                connection.execute(text(self.TEST_AUTHENTICATION_SQL))
            return True
        except Exception as e:
            logger.error(f"Failed to authenticate with the given credentials: {str(e)}")
            raise e
