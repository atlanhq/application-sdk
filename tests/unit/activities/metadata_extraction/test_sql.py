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
    @patch("application_sdk.outputs.json.JsonOutput.write_dataframe")
    async def test_query_executor_success(
        self,
        mock_write_dataframe,
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
    async def test_query_executor_empty_dataframe(
        self,
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
        "application_sdk.inputs.parquet.ParquetInput.download_files",
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
        mock_download_files.return_value = None
        mock_read_parquet.return_value = mock_dataframe
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
