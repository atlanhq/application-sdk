import logging
from typing import Any, Dict

import pandas as pd

from application_sdk import activity_pd
from application_sdk.workflows.controllers import WorkflowAuthControllerInterface
from application_sdk.workflows.sql.resources.sql_resource import SQLResource

logger = logging.getLogger(__name__)


class SQLWorkflowAuthController(WorkflowAuthControllerInterface):
    """
    SQL Workflow Auth Interface

    This interface is used to authenticate the SQL workflow. For example, if the SQL workflow
    is used to connect to a database, the `test_auth` method is used to test the authentication
    credentials.

    Attributes:
        TEST_AUTHENTICATION_SQL(str): The SQL query to test the authentication credentials.

    Usage:
        Subclass this interface and implement the required attributes and any methods
        that need custom behavior (ex. test_auth).

        >>> class MySQLWorkflowAuthInterface(SQLWorkflowAuthInterface):
        >>>     TEST_AUTHENTICATION_SQL = "SELECT 1;"
        >>>     def __init__(self, create_engine_fn: Callable[[Dict[str, Any]], Engine]):
        >>>         super().__init__(create_engine_fn)
    """

    TEST_AUTHENTICATION_SQL: str = "SELECT 1;"

    sql_resource: SQLResource

    def __init__(self, sql_resource: SQLResource):
        self.sql_resource = sql_resource

        super().__init__()

    async def prepare(self, credentials: Dict[str, Any]) -> None:
        self.sql_resource.set_credentials(credentials)
        await self.sql_resource.load()

    @activity_pd(
        batch_input=lambda self, workflow_args=None: self.sql_resource.sql_input(
            self.sql_resource.engine, self.TEST_AUTHENTICATION_SQL, chunk_size=None
        )
    )
    async def test_auth(self, batch_input: pd.DataFrame) -> bool:
        """
        Test the authentication credentials.

        :return: True if the credentials are valid, False otherwise.
        :raises Exception: If the credentials are invalid.
        """
        try:
            batch_input.to_dict(orient="records")
            return True
        except Exception as e:
            logger.error(f"Failed to authenticate with the given credentials: {str(e)}")
            raise e
