from typing import Any, Optional, Type
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from sqlalchemy.engine import Engine

from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler


class MockBaseSQLClient(BaseSQLClient):
    def __init__(self):
        super().__init__()
        self.run_query = AsyncMock()
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.engine: Engine = MagicMock(spec=Engine)
        self.engine.connect = MagicMock()
        self.engine.connect.return_value.__enter__ = MagicMock()
        self.engine.connect.return_value.__exit__ = MagicMock()
        self.engine.connect.return_value.connection = MagicMock()

    async def __aenter__(self) -> "MockBaseSQLClient":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Any,
    ) -> None:
        pass


@pytest.fixture
def sql_handler() -> BaseSQLHandler:
    sql_client = MockBaseSQLClient()
    handler = BaseSQLHandler(sql_client)
    handler.tables_check_sql = "SELECT COUNT(*) as count FROM tables"
    handler.extract_temp_table_regex_table_sql = "WHERE table_name NOT LIKE 'temp%'"
    return handler


async def test_tables_check_success(sql_handler: BaseSQLHandler) -> None:
    """Test tables check with successful response."""
    # Create a mock DataFrame with table count
    if not sql_handler.sql_client.engine:
        raise ValueError("Engine is not initialized")
    mock_df = pd.DataFrame([{"count": 5}])
    sql_handler.sql_client.engine.connect.return_value.__enter__.return_value = (
        MagicMock()
    )  # type: ignore

    with patch("pandas.read_sql_query") as mock_read_sql:
        mock_read_sql.return_value = mock_df

        result = await sql_handler.tables_check(payload={})
        assert result["success"] is True
        assert "Table count: 5" in result["successMessage"]


async def test_tables_check_empty(sql_handler: BaseSQLHandler) -> None:
    """Test tables check with empty response."""
    # Create a mock DataFrame with zero count
    if not sql_handler.sql_client.engine:
        raise ValueError("Engine is not initialized")
    mock_df = pd.DataFrame([{"count": 0}])
    sql_handler.sql_client.engine.connect.return_value.__enter__.return_value = (
        MagicMock()
    )  # type: ignore

    with patch("pandas.read_sql_query") as mock_read_sql:
        mock_read_sql.return_value = mock_df

        result = await sql_handler.tables_check(payload={})
        assert result["success"] is True
        assert "Table count: 0" in result["successMessage"]


async def test_tables_check_failure(sql_handler: BaseSQLHandler) -> None:
    """Test tables check with failure response."""
    # Create a DataFrame with invalid data that will cause an error
    if not sql_handler.sql_client.engine:
        raise ValueError("Engine is not initialized")
    mock_df = pd.DataFrame([{"wrong_column": "invalid"}])  # Missing 'count' column
    sql_handler.sql_client.engine.connect.return_value.__enter__.return_value = (
        MagicMock()
    )  # type: ignore

    with patch("pandas.read_sql_query") as mock_read_sql:
        mock_read_sql.return_value = mock_df

        result = await sql_handler.tables_check(payload={})
        assert result["success"] is False
        assert "Tables check failed" in result["failureMessage"]
        assert (
            "'count'" in result["error"]
        )  # KeyError's string representation includes quotes
