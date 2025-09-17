"""Unit tests for SQL metadata extraction activities (context-free)."""

from typing import Any, Dict
from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.activities.metadata_extraction.sql import (
    ActivityStatistics,
    BaseSQLMetadataExtractionActivities,
    BaseSQLMetadataExtractionActivitiesState,
)
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.outputs.parquet import ParquetOutput
from application_sdk.transformers import TransformerInterface


class MockSQLClient(BaseSQLClient):
    def __init__(self, *args, **kwargs):
        self.engine = True  # Dummy engine attribute for tests

    async def load(self, credentials: Dict[str, Any]) -> None:
        pass

    async def close(self) -> None:
        pass


class MockSQLHandler(BaseSQLHandler):
    async def preflight_check(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": "success"}

    async def fetch_metadata(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return {"metadata": "test"}

    async def load(self, config: Dict[str, Any]) -> None:
        pass

    async def test_auth(self, config: Dict[str, Any]) -> bool:
        return True


class MockTransformer(TransformerInterface):
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def transform(self, data: Any) -> Any:
        return {"transformed": data}

    async def transform_metadata(self, *args, **kwargs):
        return {"metadata": "dummy"}


class MockActivities(BaseSQLMetadataExtractionActivities):
    def __init__(self):
        super().__init__(
            sql_client_class=MockSQLClient,
            handler_class=MockSQLHandler,
            transformer_class=MockTransformer,
        )
        self.test_workflow_id = "test-workflow-123"

    def _get_test_workflow_id(self) -> str:
        return self.test_workflow_id

    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        workflow_id = self._get_test_workflow_id()
        if not self._state.get(workflow_id):
            self._state[workflow_id] = BaseSQLMetadataExtractionActivitiesState()
        self._state[workflow_id].workflow_args = workflow_args
        self._state[workflow_id].sql_client = MockSQLClient()
        self._state[workflow_id].handler = MockSQLHandler(MockSQLClient())
        self._state[workflow_id].transformer = MockTransformer()

    async def _get_state(self, workflow_args: Dict[str, Any]):
        workflow_id = self._get_test_workflow_id()
        if workflow_id not in self._state:
            await self._set_state(workflow_args)
        return self._state[workflow_id]

    async def _clean_state(self):
        workflow_id = self._get_test_workflow_id()
        if workflow_id in self._state:
            self._state.pop(workflow_id)


@pytest.fixture
def mock_activities():
    return MockActivities()


@pytest.fixture
def sample_workflow_args():
    return {
        "workflow_id": "test-workflow-123",
        "workflow_run_id": "test-run-456",
        "output_prefix": "test_prefix",
        "output_path": "/test/path",
        "typename": "DATABASE",
        "credential_guid": "test-credential-guid",
        "metadata": {"key": "value"},
    }


class TestBaseSQLMetadataExtractionActivitiesState:
    def test_state_initialization(self):
        state = BaseSQLMetadataExtractionActivitiesState()
        assert state.sql_client is None
        assert state.handler is None
        assert state.transformer is None
        assert state.workflow_args is None

    def test_state_with_values(self):
        sql_client = MockSQLClient()
        handler = MockSQLHandler(sql_client)
        transformer = MockTransformer()
        workflow_args = {"test": "data"}
        state = BaseSQLMetadataExtractionActivitiesState(
            sql_client=sql_client,
            handler=handler,
            transformer=transformer,
            workflow_args=workflow_args,
        )
        assert state.sql_client == sql_client
        assert state.handler == handler
        assert state.transformer == transformer
        assert state.workflow_args == workflow_args


class TestBaseSQLMetadataExtractionActivities:
    def test_initialization_custom_classes(self):
        activities = MockActivities()
        assert activities.sql_client_class == MockSQLClient
        assert activities.handler_class == MockSQLHandler
        assert activities.transformer_class == MockTransformer

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch(
        "application_sdk.outputs.parquet.ParquetOutput.get_statistics",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe")
    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_batched_dataframe")
    @patch("application_sdk.outputs.json.JsonOutput.write_dataframe")
    async def test_query_executor_success(
        self,
        mock_write_dataframe,
        mock_get_batched_dataframe,
        mock_get_dataframe,
        mock_get_statistics,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        await mock_activities._set_state(sample_workflow_args)
        mock_dataframe = Mock()
        mock_dataframe.__len__ = Mock(return_value=10)
        mock_get_dataframe.return_value = mock_dataframe
        mock_get_batched_dataframe.return_value = (df for df in [mock_dataframe])
        mock_write_dataframe.return_value = None
        mock_get_statistics.return_value = ActivityStatistics(total_record_count=10)
        sql_engine = Mock()
        sql_query = "SELECT * FROM test_table"
        output_suffix = "test_suffix"
        typename = "DATABASE"
        result = await mock_activities.query_executor(
            sql_engine, sql_query, sample_workflow_args, output_suffix, typename
        )
        assert result is not None
        assert isinstance(result, ActivityStatistics)
        assert result.total_record_count == 10

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch(
        "application_sdk.outputs.parquet.ParquetOutput.get_statistics",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_dataframe")
    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_batched_dataframe")
    async def test_query_executor_empty_dataframe(
        self,
        mock_get_batched_dataframe,
        mock_get_dataframe,
        mock_get_statistics,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        await mock_activities._set_state(sample_workflow_args)
        mock_dataframe = Mock()
        mock_dataframe.__len__ = Mock(return_value=0)
        mock_get_dataframe.return_value = mock_dataframe
        mock_get_batched_dataframe.return_value = (df for df in [mock_dataframe])
        mock_get_statistics.return_value = ActivityStatistics(total_record_count=0)
        sql_engine = Mock()
        sql_query = "SELECT * FROM empty_table"
        output_suffix = "test_suffix"
        typename = "DATABASE"
        result = await mock_activities.query_executor(
            sql_engine, sql_query, sample_workflow_args, output_suffix, typename
        )
        assert isinstance(result, ActivityStatistics)
        assert result.total_record_count == 0

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch.object(MockActivities, "query_executor")
    async def test_fetch_databases_success(
        self,
        mock_query_executor,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        mock_query_executor.return_value = ActivityStatistics(total_record_count=5)
        await mock_activities._set_state(sample_workflow_args)
        result = await mock_activities.fetch_databases(sample_workflow_args)
        assert result is not None
        assert result.total_record_count == 5
        mock_query_executor.assert_called_once()

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch.object(MockActivities, "query_executor")
    async def test_fetch_schemas_success(
        self,
        mock_query_executor,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        mock_query_executor.return_value = ActivityStatistics(total_record_count=10)
        await mock_activities._set_state(sample_workflow_args)
        result = await mock_activities.fetch_schemas(sample_workflow_args)
        assert result is not None
        assert result.total_record_count == 10
        mock_query_executor.assert_called_once()

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch.object(MockActivities, "query_executor")
    async def test_fetch_tables_success(
        self,
        mock_query_executor,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        mock_query_executor.return_value = ActivityStatistics(total_record_count=15)
        await mock_activities._set_state(sample_workflow_args)
        result = await mock_activities.fetch_tables(sample_workflow_args)
        assert result is not None
        assert result.total_record_count == 15
        mock_query_executor.assert_called_once()

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch.object(MockActivities, "query_executor")
    async def test_fetch_columns_success(
        self,
        mock_query_executor,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        mock_query_executor.return_value = ActivityStatistics(total_record_count=50)
        await mock_activities._set_state(sample_workflow_args)
        result = await mock_activities.fetch_columns(sample_workflow_args)
        assert result is not None
        assert result.total_record_count == 50
        mock_query_executor.assert_called_once()

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch.object(MockActivities, "query_executor")
    async def test_fetch_procedures_success(
        self,
        mock_query_executor,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        mock_query_executor.return_value = ActivityStatistics(total_record_count=8)
        await mock_activities._set_state(sample_workflow_args)
        result = await mock_activities.fetch_procedures(sample_workflow_args)
        assert result is not None
        assert result.total_record_count == 8
        mock_query_executor.assert_called_once()

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch(
        "application_sdk.outputs.parquet.ParquetOutput.get_statistics",
        new_callable=AsyncMock,
    )
    @patch(
        "application_sdk.outputs.json.JsonOutput.get_statistics", new_callable=AsyncMock
    )
    @patch("application_sdk.inputs.parquet.ParquetInput.get_dataframe")
    @patch(
        "application_sdk.inputs.Input.download_files",
        new_callable=AsyncMock,
    )
    @patch("daft.read_parquet")
    @patch(
        "application_sdk.activities.metadata_extraction.sql.is_empty_dataframe",
        return_value=False,
    )
    @patch.object(MockTransformer, "transform_metadata")
    @patch(
        "application_sdk.outputs.json.JsonOutput.write_daft_dataframe",
        new_callable=AsyncMock,
    )
    async def test_transform_data_success(
        self,
        mock_write_daft_dataframe,
        mock_transform_metadata,
        mock_is_empty,
        mock_read_parquet,
        mock_download_files,
        mock_get_dataframe,
        mock_get_statistics_json,
        mock_get_statistics_parquet,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        await mock_activities._set_state(sample_workflow_args)
        mock_dataframe = Mock()
        mock_dataframe.__len__ = Mock(return_value=20)
        mock_dataframe.empty = False
        mock_dataframe.shape = (20, 1)
        # Patch get_dataframe to return a list with one mock dataframe
        mock_get_dataframe.return_value = [mock_dataframe]
        mock_download_files.return_value = ["/test/path/raw/file1.parquet"]

        # Create a proper mock for daft DataFrame with chunked behavior
        mock_daft_df = Mock()
        mock_daft_df.count_rows.return_value = 20  # Return integer for range()
        mock_daft_df.offset.return_value = mock_daft_df  # Return self for chaining
        mock_daft_df.limit.return_value = mock_daft_df  # Return self for chaining
        mock_read_parquet.return_value = mock_daft_df
        mock_transform_metadata.return_value = {"transformed": "data"}
        mock_write_daft_dataframe.return_value = None
        mock_get_statistics_parquet.return_value = ActivityStatistics(
            total_record_count=20
        )
        mock_get_statistics_json.return_value = ActivityStatistics(
            total_record_count=20
        )
        result = await mock_activities.transform_data(sample_workflow_args)
        assert result is not None
        assert isinstance(result, ActivityStatistics)
        assert result.total_record_count == 20
        mock_transform_metadata.assert_called_once()
        mock_write_daft_dataframe.assert_called_once()

    # Tests for query_executor method
    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch(
        "application_sdk.outputs.parquet.ParquetOutput.get_statistics",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_batched_dataframe")
    @patch("application_sdk.outputs.parquet.ParquetOutput.write_batched_dataframe")
    async def test_query_executor_single_db_success(
        self,
        mock_write_batched_dataframe,
        mock_get_batched_dataframe,
        mock_get_statistics,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        """Test query_executor with single database mode (multidb=False)."""
        await mock_activities._set_state(sample_workflow_args)

        # Mock the batched dataframe iterator
        mock_dataframe = Mock()
        mock_dataframe.__len__ = Mock(return_value=10)
        mock_batched_iter = [mock_dataframe]
        mock_get_batched_dataframe.return_value = mock_batched_iter
        mock_write_batched_dataframe.return_value = None
        mock_get_statistics.return_value = ActivityStatistics(total_record_count=10)

        sql_engine = Mock()
        sql_query = "SELECT * FROM test_table"
        output_suffix = "test_suffix"
        typename = "DATABASE"

        result = await mock_activities.query_executor(
            sql_engine, sql_query, sample_workflow_args, output_suffix, typename
        )

        assert result is not None
        assert isinstance(result, ActivityStatistics)
        assert result.total_record_count == 10
        mock_get_batched_dataframe.assert_called_once()
        mock_write_batched_dataframe.assert_called_once()

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch(
        "application_sdk.outputs.parquet.ParquetOutput.get_statistics",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_batched_dataframe")
    async def test_query_executor_single_db_async_iterator(
        self,
        mock_get_batched_dataframe,
        mock_get_statistics,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        """Test query_executor with async iterator."""
        await mock_activities._set_state(sample_workflow_args)

        # Mock async iterator
        async def mock_async_iter():
            mock_df = Mock()
            mock_df.__len__ = Mock(return_value=5)
            yield mock_df

        mock_get_batched_dataframe.return_value = mock_async_iter()
        mock_get_statistics.return_value = ActivityStatistics(total_record_count=5)

        sql_engine = Mock()
        sql_query = "SELECT * FROM test_table"
        output_suffix = "test_suffix"
        typename = "DATABASE"

        result = await mock_activities.query_executor(
            sql_engine, sql_query, sample_workflow_args, output_suffix, typename
        )

        assert result is not None
        assert isinstance(result, ActivityStatistics)
        assert result.total_record_count == 5

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_batched_dataframe")
    async def test_query_executor_single_db_no_write_to_file(
        self,
        mock_get_batched_dataframe,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        """Test query_executor with write_to_file=False."""
        await mock_activities._set_state(sample_workflow_args)

        mock_dataframe = Mock()
        mock_dataframe.__len__ = Mock(return_value=10)
        mock_get_batched_dataframe.return_value = [mock_dataframe]

        sql_engine = Mock()
        sql_query = "SELECT * FROM test_table"
        output_suffix = "test_suffix"
        typename = "DATABASE"

        result = await mock_activities.query_executor(
            sql_engine,
            sql_query,
            sample_workflow_args,
            output_suffix,
            typename,
            write_to_file=False,
        )

        assert result is None

    async def test_query_executor_empty_query(
        self, mock_activities, sample_workflow_args
    ):
        """Test query_executor with empty query."""
        sql_engine = Mock()
        sql_query = ""
        output_suffix = "test_suffix"
        typename = "DATABASE"

        result = await mock_activities.query_executor(
            sql_engine, sql_query, sample_workflow_args, output_suffix, typename
        )

        assert result is None

    async def test_query_executor_missing_output_args(self, mock_activities):
        """Test query_executor with missing output arguments."""
        sql_engine = Mock()
        sql_query = "SELECT * FROM test_table"
        output_suffix = "test_suffix"
        typename = "DATABASE"
        workflow_args = {"workflow_id": "test"}  # Missing output_prefix and output_path

        with pytest.raises(
            ValueError, match="Output prefix and path must be specified"
        ):
            await mock_activities.query_executor(
                sql_engine, sql_query, workflow_args, output_suffix, typename
            )

    @patch("os.makedirs")
    async def test_query_executor_no_sql_engine(
        self, mock_makedirs, mock_activities, sample_workflow_args
    ):
        """Test query_executor with no SQL engine."""
        sql_engine = None
        sql_query = "SELECT * FROM test_table"
        output_suffix = "test_suffix"
        typename = "DATABASE"

        with pytest.raises(ValueError, match="SQL engine must be provided"):
            await mock_activities.query_executor(
                sql_engine, sql_query, sample_workflow_args, output_suffix, typename
            )

    # Tests for multidb mode
    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch(
        "application_sdk.outputs.parquet.ParquetOutput.get_statistics",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_batched_dataframe")
    @patch("application_sdk.outputs.parquet.ParquetOutput.write_batched_dataframe")
    @patch("application_sdk.activities.metadata_extraction.sql.get_database_names")
    @patch("application_sdk.activities.metadata_extraction.sql.prepare_query")
    @patch("application_sdk.activities.metadata_extraction.sql.parse_credentials_extra")
    async def test_query_executor_multidb_success(
        self,
        mock_parse_credentials_extra,
        mock_prepare_query,
        mock_get_database_names,
        mock_write_batched_dataframe,
        mock_get_batched_dataframe,
        mock_get_statistics,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        """Test query_executor with multidb mode enabled."""
        # Enable multidb mode
        mock_activities.multidb = True
        await mock_activities._set_state(sample_workflow_args)

        # Mock the SQL client's engine properly
        state = await mock_activities._get_state(sample_workflow_args)
        mock_engine = Mock()
        mock_connection = Mock()
        mock_engine.connect.return_value = mock_connection
        mock_connection.__enter__ = Mock(return_value=mock_connection)
        mock_connection.__exit__ = Mock(return_value=None)
        state.sql_client.engine = mock_engine
        state.sql_client.credentials = {"extra": "{}"}

        # Mock database names
        mock_get_database_names.return_value = ["db1", "db2"]
        mock_prepare_query.return_value = "SELECT * FROM test_table"
        mock_parse_credentials_extra.return_value = {}

        # Mock dataframe
        mock_dataframe = Mock()
        mock_dataframe.__len__ = Mock(return_value=5)
        mock_get_batched_dataframe.return_value = [mock_dataframe]
        mock_write_batched_dataframe.return_value = None
        mock_get_statistics.return_value = ActivityStatistics(total_record_count=10)

        sql_engine = Mock()
        sql_query = "SELECT * FROM {database_name}.test_table"
        output_suffix = "test_suffix"
        typename = "DATABASE"

        result = await mock_activities.query_executor(
            sql_engine, sql_query, sample_workflow_args, output_suffix, typename
        )

        assert result is not None
        assert isinstance(result, ActivityStatistics)
        assert result.total_record_count == 10
        assert mock_get_database_names.call_count == 1
        assert mock_prepare_query.call_count == 2  # Called for each database

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch("application_sdk.activities.metadata_extraction.sql.get_database_names")
    async def test_query_executor_multidb_no_databases(
        self,
        mock_get_database_names,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        """Test query_executor with multidb mode but no databases found."""
        # Enable multidb mode
        mock_activities.multidb = True
        await mock_activities._set_state(sample_workflow_args)

        # Mock the SQL client's engine properly
        state = await mock_activities._get_state(sample_workflow_args)
        mock_engine = Mock()
        mock_connection = Mock()
        mock_engine.connect.return_value = mock_connection
        mock_connection.__enter__ = Mock(return_value=mock_connection)
        mock_connection.__exit__ = Mock(return_value=None)
        state.sql_client.engine = mock_engine
        state.sql_client.credentials = {"extra": "{}"}

        # Mock no databases found
        mock_get_database_names.return_value = []

        sql_engine = Mock()
        sql_query = "SELECT * FROM {database_name}.test_table"
        output_suffix = "test_suffix"
        typename = "DATABASE"

        result = await mock_activities.query_executor(
            sql_engine, sql_query, sample_workflow_args, output_suffix, typename
        )

        assert result is None

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch("application_sdk.activities.metadata_extraction.sql.get_database_names")
    async def test_query_executor_multidb_no_sql_client(
        self,
        mock_get_database_names,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        """Test query_executor with multidb mode but no SQL client."""
        # Enable multidb mode
        mock_activities.multidb = True
        await mock_activities._set_state(sample_workflow_args)

        # Remove SQL client from state
        state = await mock_activities._get_state(sample_workflow_args)
        state.sql_client = None

        mock_get_database_names.return_value = ["db1"]

        sql_engine = Mock()
        sql_query = "SELECT * FROM {database_name}.test_table"
        output_suffix = "test_suffix"
        typename = "DATABASE"

        with pytest.raises(ValueError, match="SQL client not initialized"):
            await mock_activities.query_executor(
                sql_engine, sql_query, sample_workflow_args, output_suffix, typename
            )

    # Tests for helper functions
    @patch("os.makedirs")
    def test_setup_parquet_output_success(
        self, mock_makedirs, mock_activities, sample_workflow_args
    ):
        """Test _setup_parquet_output with valid arguments."""
        output_suffix = "test_suffix"

        result = mock_activities._setup_parquet_output(
            sample_workflow_args, output_suffix, write_to_file=True
        )

        assert result is not None
        assert isinstance(result, ParquetOutput)

    def test_setup_parquet_output_no_write_to_file(
        self, mock_activities, sample_workflow_args
    ):
        """Test _setup_parquet_output with write_to_file=False."""
        output_suffix = "test_suffix"

        result = mock_activities._setup_parquet_output(
            sample_workflow_args, output_suffix, write_to_file=False
        )

        assert result is None

    def test_setup_parquet_output_missing_args(self, mock_activities):
        """Test _setup_parquet_output with missing workflow arguments."""
        workflow_args = {"workflow_id": "test"}  # Missing output_prefix and output_path
        output_suffix = "test_suffix"

        with pytest.raises(
            ValueError, match="Output prefix and path must be specified"
        ):
            mock_activities._setup_parquet_output(
                workflow_args, output_suffix, write_to_file=True
            )

    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_batched_dataframe")
    @patch(
        "application_sdk.outputs.parquet.ParquetOutput.write_batched_dataframe",
        new_callable=AsyncMock,
    )
    async def test_execute_single_db_success_with_write(
        self,
        mock_write_batched_dataframe,
        mock_get_batched_dataframe,
        mock_activities,
    ):
        """Test _execute_single_db with write_to_file=True."""
        sql_engine = Mock()
        prepared_query = "SELECT * FROM test_table"
        parquet_output = Mock()
        parquet_output.write_batched_dataframe = AsyncMock(return_value=None)

        # Mock dataframe
        mock_dataframe = Mock()
        mock_dataframe.__len__ = Mock(return_value=5)
        mock_get_batched_dataframe.return_value = [mock_dataframe]
        mock_write_batched_dataframe.return_value = None

        success, result = await mock_activities._execute_single_db(
            sql_engine, prepared_query, parquet_output, write_to_file=True
        )

        assert success is True
        assert result is None
        mock_get_batched_dataframe.assert_called_once()
        parquet_output.write_batched_dataframe.assert_called_once()

    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_batched_dataframe")
    async def test_execute_single_db_success_without_write(
        self,
        mock_get_batched_dataframe,
        mock_activities,
    ):
        """Test _execute_single_db with write_to_file=False."""
        sql_engine = Mock()
        prepared_query = "SELECT * FROM test_table"
        parquet_output = Mock()

        # Mock dataframe iterator
        mock_dataframe = Mock()
        mock_dataframe.__len__ = Mock(return_value=5)
        mock_batched_iter = [mock_dataframe]
        mock_get_batched_dataframe.return_value = mock_batched_iter

        success, result = await mock_activities._execute_single_db(
            sql_engine, prepared_query, parquet_output, write_to_file=False
        )

        assert success is True
        assert result == mock_batched_iter
        mock_get_batched_dataframe.assert_called_once()

    async def test_execute_single_db_no_prepared_query(self, mock_activities):
        """Test _execute_single_db with no prepared query."""
        sql_engine = Mock()
        prepared_query = None
        parquet_output = Mock()

        success, result = await mock_activities._execute_single_db(
            sql_engine, prepared_query, parquet_output, write_to_file=True
        )

        assert success is False
        assert result is None

    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_batched_dataframe")
    async def test_execute_single_db_exception(
        self,
        mock_get_batched_dataframe,
        mock_activities,
    ):
        """Test _execute_single_db with exception during execution."""
        sql_engine = Mock()
        prepared_query = "SELECT * FROM test_table"
        parquet_output = Mock()

        # Mock exception
        mock_get_batched_dataframe.side_effect = Exception("Database error")

        with pytest.raises(Exception, match="Database error"):
            await mock_activities._execute_single_db(
                sql_engine, prepared_query, parquet_output, write_to_file=True
            )

    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_batched_dataframe")
    @patch(
        "application_sdk.outputs.parquet.ParquetOutput.write_batched_dataframe",
        new_callable=AsyncMock,
    )
    async def test_execute_single_db_async_iterator(
        self,
        mock_write_batched_dataframe,
        mock_get_batched_dataframe,
        mock_activities,
    ):
        """Test _execute_single_db with async iterator."""
        sql_engine = Mock()
        prepared_query = "SELECT * FROM test_table"
        parquet_output = Mock()
        parquet_output.write_batched_dataframe = AsyncMock(return_value=None)

        # Mock async iterator
        async def mock_async_iter():
            mock_df = Mock()
            mock_df.__len__ = Mock(return_value=3)
            yield mock_df

        mock_get_batched_dataframe.return_value = mock_async_iter()
        mock_write_batched_dataframe.return_value = None

        success, result = await mock_activities._execute_single_db(
            sql_engine, prepared_query, parquet_output, write_to_file=True
        )

        assert success is True
        assert result is None
        mock_get_batched_dataframe.assert_called_once()
        parquet_output.write_batched_dataframe.assert_called_once()

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch(
        "application_sdk.outputs.parquet.ParquetOutput.get_statistics",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_batched_dataframe")
    @patch("application_sdk.outputs.parquet.ParquetOutput.write_batched_dataframe")
    @patch(
        "application_sdk.outputs.parquet.ParquetOutput.write_dataframe",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.activities.metadata_extraction.sql.get_database_names")
    @patch("application_sdk.activities.metadata_extraction.sql.prepare_query")
    @patch("application_sdk.activities.metadata_extraction.sql.parse_credentials_extra")
    async def test_query_executor_multidb_concatenate_success(
        self,
        mock_parse_credentials_extra,
        mock_prepare_query,
        mock_get_database_names,
        mock_write_dataframe,
        mock_write_batched_dataframe,
        mock_get_batched_dataframe,
        mock_get_statistics,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        """Test query_executor with multidb mode and concatenate=True."""
        # Enable multidb mode
        mock_activities.multidb = True
        await mock_activities._set_state(sample_workflow_args)

        # Mock the SQL client's engine properly
        state = await mock_activities._get_state(sample_workflow_args)
        mock_engine = Mock()
        mock_connection = Mock()
        mock_engine.connect.return_value = mock_connection
        mock_connection.__enter__ = Mock(return_value=mock_connection)
        mock_connection.__exit__ = Mock(return_value=None)
        state.sql_client.engine = mock_engine
        state.sql_client.credentials = {"extra": "{}"}

        # Mock database names
        mock_get_database_names.return_value = ["db1", "db2"]
        mock_prepare_query.return_value = "SELECT * FROM test_table"
        mock_parse_credentials_extra.return_value = {}

        # Mock dataframe with real pandas DataFrame
        import pandas as pd

        mock_dataframe = pd.DataFrame({"col1": [1, 2, 3], "col2": ["a", "b", "c"]})
        mock_get_batched_dataframe.return_value = [mock_dataframe]
        mock_get_statistics.return_value = ActivityStatistics(total_record_count=10)

        sql_engine = Mock()
        sql_query = "SELECT * FROM {database_name}.test_table"
        output_suffix = "test_suffix"
        typename = "DATABASE"

        result = await mock_activities.query_executor(
            sql_engine,
            sql_query,
            sample_workflow_args,
            output_suffix,
            typename,
            write_to_file=False,
            concatenate=True,
        )

        assert result is not None
        assert isinstance(result, ActivityStatistics)
        assert result.total_record_count == 10

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch("application_sdk.inputs.sql_query.SQLQueryInput.get_batched_dataframe")
    @patch("application_sdk.activities.metadata_extraction.sql.get_database_names")
    @patch("application_sdk.activities.metadata_extraction.sql.prepare_query")
    @patch("application_sdk.activities.metadata_extraction.sql.parse_credentials_extra")
    async def test_query_executor_multidb_concatenate_return_dataframe(
        self,
        mock_parse_credentials_extra,
        mock_prepare_query,
        mock_get_database_names,
        mock_get_batched_dataframe,
        mock_exists,
        mock_makedirs,
        mock_activities,
        sample_workflow_args,
    ):
        """Test query_executor with multidb mode, concatenate=True, and return_dataframe=True."""
        # Enable multidb mode
        mock_activities.multidb = True
        await mock_activities._set_state(sample_workflow_args)

        # Mock the SQL client's engine properly
        state = await mock_activities._get_state(sample_workflow_args)
        mock_engine = Mock()
        mock_connection = Mock()
        mock_engine.connect.return_value = mock_connection
        mock_connection.__enter__ = Mock(return_value=mock_connection)
        mock_connection.__exit__ = Mock(return_value=None)
        state.sql_client.engine = mock_engine
        state.sql_client.credentials = {"extra": "{}"}

        # Mock database names
        mock_get_database_names.return_value = ["db1"]
        mock_prepare_query.return_value = "SELECT * FROM test_table"
        mock_parse_credentials_extra.return_value = {}

        # Mock dataframe with real pandas DataFrame
        import pandas as pd

        mock_dataframe = pd.DataFrame({"col1": [1, 2, 3], "col2": ["a", "b", "c"]})
        mock_get_batched_dataframe.return_value = [mock_dataframe]

        sql_engine = Mock()
        sql_query = "SELECT * FROM {database_name}.test_table"
        output_suffix = "test_suffix"
        typename = "DATABASE"

        result = await mock_activities.query_executor(
            sql_engine,
            sql_query,
            sample_workflow_args,
            output_suffix,
            typename,
            write_to_file=False,
            concatenate=True,
            return_dataframe=True,
        )

        # Should return the concatenated dataframe directly
        assert result is not None
        # Note: In the actual implementation, this would be a pandas DataFrame
        # but in our mock, we're just checking it's not None
