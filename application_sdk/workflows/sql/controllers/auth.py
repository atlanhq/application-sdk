import logging
from typing import Any, Dict

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
        self.set_sql_resource(sql_resource)

        super().__init__()

    def test_auth(self, credential: Dict[str, Any]) -> bool:
        """
        Test the authentication credentials.

        :param credential: Credentials to test.
        :return: True if the credentials are valid, False otherwise.
        :raises Exception: If the credentials are invalid.
        """
        try:
            self.sql_resource.run_query(self.TEST_AUTHENTICATION_SQL)
            return True
        except Exception as e:
            logger.error(f"Failed to authenticate with the given credentials: {str(e)}")
            raise e

    def set_sql_resource(self, sql_resource):
        self.sql_resource = sql_resource
