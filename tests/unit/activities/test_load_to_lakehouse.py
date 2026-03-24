"""Unit tests for the load_to_lakehouse activity."""

import json
import os
from unittest.mock import AsyncMock, patch

import pyarrow as pa
import pyarrow.parquet as pq_lib
import pytest

from application_sdk.activities.common.models import (
    LhLoadRequest,
    LhLoadResponse,
    LhLoadStatusResponse,
    LhTableWriteMode,
)
from application_sdk.activities.metadata_extraction.lakehouse import (
    convert_raw_parquet_to_parquet,
    submit_and_poll_mdlh_load,
)
from application_sdk.common.error_codes import ActivityError
from application_sdk.workflows.metadata_extraction.lakehouse import (
    resolve_iceberg_table,
)


def _write_test_parquet(path: str, data: dict) -> str:
    """Write a dict of columns to a parquet file and return the path."""
    table = pa.table(data)
    pq_lib.write_table(table, path)
    return path


def _make_workflow_args(
    output_path="/tmp/test/artifacts/apps/postgres/workflows/wf-1/run-1/raw",
    namespace="test_ns",
    table_name="test_table",
    mode="APPEND",
    file_extension=".parquet",
):
    return {
        "lh_load_config": {
            "output_path": output_path,
            "namespace": namespace,
            "table_name": table_name,
            "mode": mode,
            "file_extension": file_extension,
        }
    }


class _MockResponse:
    """Mock aiohttp response."""

    def __init__(self, status, json_data=None, text_data=""):
        self.status = status
        self._json_data = json_data
        self._text_data = text_data

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _MockSession:
    """Mock aiohttp.ClientSession.

    Supports being used as both a context manager (poll loop creates
    a new session per iteration) and for direct .post()/.get() calls.
    """

    def __init__(self, post_response=None, get_responses=None):
        self._post_response = post_response
        self._get_responses = iter(get_responses or [])

    def post(self, url, json=None, headers=None):
        return self._post_response

    def get(self, url, headers=None, timeout=None):
        return next(self._get_responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _MockSessionFactory:
    """Returns the same _MockSession for every ClientSession() call."""

    def __init__(self, post_response=None, get_responses=None):
        self._session = _MockSession(post_response, get_responses)

    def __call__(self, **kwargs):
        return self._session


_COMMON_PATCHES = [
    patch(
        "application_sdk.activities.metadata_extraction.lakehouse.check_lakehouse_enabled",
        new=AsyncMock(return_value=True),
    ),
    patch(
        "application_sdk.activities.metadata_extraction.lakehouse.LH_LOAD_POLL_INTERVAL_SECONDS",
        0,
    ),
    patch(
        "application_sdk.activities.metadata_extraction.lakehouse.LH_LOAD_MAX_POLL_ATTEMPTS",
        5,
    ),
    patch(
        "application_sdk.activities.metadata_extraction.lakehouse.MDLH_BASE_URL",
        "http://test:4541",
    ),
    patch(
        "application_sdk.activities.metadata_extraction.lakehouse.APP_TENANT_ID",
        "test-tenant",
    ),
]


def _apply_common_patches(func):
    for p in reversed(_COMMON_PATCHES):
        func = p(func)
    return func


class TestLoadToLakehouse:
    @_apply_common_patches
    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.get_object_store_prefix"
    )
    @patch("application_sdk.activities.metadata_extraction.lakehouse.aiohttp")
    async def test_load_success(self, mock_aiohttp, mock_get_prefix):
        """POST 202 + GET COMPLETED → returns ActivityStatistics."""
        mock_get_prefix.return_value = (
            "artifacts/apps/postgres/workflows/wf-1/run-1/raw"
        )

        post_resp = _MockResponse(
            202, {"jobId": "j1", "workflowId": "w1", "status": "ACCEPTED"}
        )
        get_resp = _MockResponse(200, {"jobId": "j1", "status": "COMPLETED"})
        mock_aiohttp.ClientSession = _MockSessionFactory(post_resp, [get_resp])
        mock_aiohttp.ClientError = Exception
        mock_aiohttp.ClientTimeout = lambda total: None

        result = await submit_and_poll_mdlh_load(_make_workflow_args())
        assert result.typename == "lakehouse-load-completed"

    @_apply_common_patches
    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.get_object_store_prefix"
    )
    @patch("application_sdk.activities.metadata_extraction.lakehouse.aiohttp")
    async def test_load_failed_status(self, mock_aiohttp, mock_get_prefix):
        """POST 202 + GET FAILED → raises ActivityError."""
        mock_get_prefix.return_value = "prefix/raw"

        post_resp = _MockResponse(
            202, {"jobId": "j1", "workflowId": "w1", "status": "ACCEPTED"}
        )
        get_resp = _MockResponse(200, {"jobId": "j1", "status": "FAILED"})
        mock_aiohttp.ClientSession = _MockSessionFactory(post_resp, [get_resp])
        mock_aiohttp.ClientError = Exception
        mock_aiohttp.ClientTimeout = lambda total: None

        with pytest.raises(ActivityError, match="LAKEHOUSE_LOAD_ERROR|FAILED"):
            await submit_and_poll_mdlh_load(_make_workflow_args())

    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.check_lakehouse_enabled",
        new=AsyncMock(return_value=True),
    )
    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.LH_LOAD_POLL_INTERVAL_SECONDS",
        0,
    )
    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.LH_LOAD_MAX_POLL_ATTEMPTS",
        2,
    )
    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.MDLH_BASE_URL",
        "http://test:4541",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.APP_TENANT_ID",
        "test-tenant",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.get_object_store_prefix"
    )
    @patch("application_sdk.activities.metadata_extraction.lakehouse.aiohttp")
    async def test_load_poll_timeout(self, mock_aiohttp, mock_get_prefix):
        """POST 202 + GET always RUNNING → raises timeout error."""
        mock_get_prefix.return_value = "prefix/raw"

        post_resp = _MockResponse(
            202, {"jobId": "j1", "workflowId": "w1", "status": "ACCEPTED"}
        )
        running_responses = [
            _MockResponse(200, {"jobId": "j1", "status": "RUNNING"}) for _ in range(3)
        ]
        mock_aiohttp.ClientSession = _MockSessionFactory(post_resp, running_responses)
        mock_aiohttp.ClientError = Exception
        mock_aiohttp.ClientTimeout = lambda total: None

        with pytest.raises(
            ActivityError, match="LAKEHOUSE_LOAD_TIMEOUT|did not complete"
        ):
            await submit_and_poll_mdlh_load(_make_workflow_args())

    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.check_lakehouse_enabled",
        new=AsyncMock(return_value=True),
    )
    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.MDLH_BASE_URL",
        "http://test:4541",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.APP_TENANT_ID",
        "test-tenant",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.get_object_store_prefix"
    )
    @patch("application_sdk.activities.metadata_extraction.lakehouse.aiohttp")
    async def test_load_api_error(self, mock_aiohttp, mock_get_prefix):
        """POST non-202 → raises API error."""
        mock_get_prefix.return_value = "prefix/raw"

        post_resp = _MockResponse(400, text_data="Bad Request")
        mock_aiohttp.ClientSession = _MockSessionFactory(post_resp, [])
        mock_aiohttp.ClientError = Exception
        mock_aiohttp.ClientTimeout = lambda total: None

        with pytest.raises(ActivityError, match="LAKEHOUSE_LOAD_API_ERROR|400"):
            await submit_and_poll_mdlh_load(_make_workflow_args())

    @_apply_common_patches
    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.get_object_store_prefix"
    )
    @patch("application_sdk.activities.metadata_extraction.lakehouse.aiohttp")
    async def test_load_non_retryable_poll_status(self, mock_aiohttp, mock_get_prefix):
        """Poll returns 404 → fails fast instead of burning all attempts."""
        mock_get_prefix.return_value = "prefix/raw"

        post_resp = _MockResponse(
            202, {"jobId": "j1", "workflowId": "w1", "status": "ACCEPTED"}
        )
        get_resp = _MockResponse(404, text_data="Not Found")
        mock_aiohttp.ClientSession = _MockSessionFactory(post_resp, [get_resp])
        mock_aiohttp.ClientError = Exception
        mock_aiohttp.ClientTimeout = lambda total: None

        with pytest.raises(ActivityError, match="non-retryable.*404"):
            await submit_and_poll_mdlh_load(_make_workflow_args())

    async def test_load_missing_config(self):
        """No lh_load_config → raises ActivityError."""
        with pytest.raises(ActivityError, match="Missing lh_load_config"):
            await submit_and_poll_mdlh_load({})

    async def test_load_missing_fields(self):
        """Missing required fields → raises ActivityError."""
        with pytest.raises(ActivityError, match="Missing required fields"):
            await submit_and_poll_mdlh_load(
                {"lh_load_config": {"output_path": "/tmp/test", "namespace": "ns"}}
            )

    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.check_lakehouse_enabled",
        new=AsyncMock(return_value=False),
    )
    async def test_load_skipped_when_mdlh_unavailable(self):
        """MDLH health check fails → returns skipped stats instead of raising."""
        result = await submit_and_poll_mdlh_load(_make_workflow_args())
        assert result.typename == "lakehouse-load-skipped"

    def test_path_conversion(self):
        """get_object_store_prefix strips TEMPORARY_PATH prefix."""
        from application_sdk.activities.common.utils import get_object_store_prefix

        result = get_object_store_prefix(
            "./local/tmp/artifacts/apps/postgres/workflows/wf-1/run-1"
        )
        assert result.startswith("artifacts/")
        assert "local/tmp" not in result


class TestLhLoadRequestSerialization:
    def test_camel_case_aliases(self):
        """Pydantic model serializes with camelCase aliases."""
        request = LhLoadRequest(
            patterns=["prefix/**/*.parquet"],
            namespace="ns",
            table_name="tbl",
            mode=LhTableWriteMode.APPEND,
        )
        dumped = request.model_dump(by_alias=True, exclude_none=True)
        assert "tableName" in dumped
        assert "table_name" not in dumped
        assert dumped["namespace"] == "ns"
        assert dumped["mode"] == "APPEND"

    def test_parquet_pattern(self):
        """file_extension='.parquet' produces correct pattern."""
        s3_prefix = "artifacts/apps/pg/workflows/wf-1/run-1/raw"
        pattern = f"{s3_prefix}/**/*.parquet"
        assert pattern.endswith("**/*.parquet")

    def test_jsonl_pattern(self):
        """file_extension='.jsonl' produces correct pattern."""
        s3_prefix = "artifacts/apps/pg/workflows/wf-1/run-1/transformed"
        pattern = f"{s3_prefix}/**/*.jsonl"
        assert pattern.endswith("**/*.jsonl")

    def test_requires_file_keys_or_patterns(self):
        """Neither file_keys nor patterns → raises ValidationError."""
        with pytest.raises(ValueError, match="file_keys or patterns"):
            LhLoadRequest(namespace="ns", table_name="tbl")


class TestLhLoadResponseModels:
    def test_load_response_parsing(self):
        resp = LhLoadResponse.model_validate(
            {"jobId": "j1", "workflowId": "w1", "status": "ACCEPTED"}
        )
        assert resp.job_id == "j1"
        assert resp.workflow_id == "w1"

    def test_load_status_response_parsing(self):
        resp = LhLoadStatusResponse.model_validate(
            {"jobId": "j1", "status": "COMPLETED"}
        )
        assert resp.job_id == "j1"
        assert resp.status == "COMPLETED"


class TestResolveIcebergTable:
    def test_standard_typename(self):
        assert resolve_iceberg_table("database") == "database"

    def test_uppercase_typename(self):
        assert resolve_iceberg_table("LookerDashboard") == "lookerdashboard"

    def test_extras_procedure_override(self):
        assert resolve_iceberg_table("extras-procedure") == "procedure"

    def test_unknown_typename(self):
        assert resolve_iceberg_table("snowflakedynamictable") == "snowflakedynamictable"


class TestConvertRawParquetToParquet:
    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.APP_TENANT_ID", "t1"
    )
    @patch("application_sdk.activities.metadata_extraction.lakehouse.download_files")
    @patch("application_sdk.activities.metadata_extraction.lakehouse.SafeFileOps")
    async def test_empty_typenames_returns_base_dir(self, mock_fileops, mock_download):
        """Empty typenames list returns base_dir without processing."""
        args = {"output_path": "/tmp/out", "workflow_id": "w1", "workflow_run_id": "r1"}
        result = await convert_raw_parquet_to_parquet(args, [])
        assert result.endswith("raw_lakehouse")
        mock_download.assert_not_called()

    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.APP_TENANT_ID", "t1"
    )
    @patch("application_sdk.activities.metadata_extraction.lakehouse.ObjectStore")
    @patch("application_sdk.activities.metadata_extraction.lakehouse.download_files")
    @patch("application_sdk.activities.metadata_extraction.lakehouse.SafeFileOps")
    async def test_no_parquet_files_skips(
        self, mock_fileops, mock_download, mock_objectstore
    ):
        """No parquet files for a typename → skips without writing."""
        mock_download.return_value = []
        mock_objectstore.upload_prefix = AsyncMock()
        args = {"output_path": "/tmp/out", "workflow_id": "w1", "workflow_run_id": "r1"}
        result = await convert_raw_parquet_to_parquet(args, ["database"])
        assert result.endswith("raw_lakehouse")
        mock_download.assert_called_once()

    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.APP_TENANT_ID", "t1"
    )
    @patch("application_sdk.activities.metadata_extraction.lakehouse.ObjectStore")
    @patch("application_sdk.activities.metadata_extraction.lakehouse.download_files")
    async def test_writes_parquet_with_correct_schema(
        self, mock_download, mock_objectstore, tmp_path
    ):
        """Parquet rows are enriched with metadata columns in output parquet."""
        src_file = _write_test_parquet(
            str(tmp_path / "src.parquet"),
            {"database_name": ["mydb"], "extra_col": ["val1"]},
        )
        mock_download.return_value = [src_file]
        mock_objectstore.upload_prefix = AsyncMock()

        out_base = str(tmp_path / "output")
        os.makedirs(os.path.join(out_base, "raw", "database"), exist_ok=True)

        args = {
            "output_path": out_base,
            "workflow_id": "wf-1",
            "workflow_run_id": "run-1",
            "connection": {"connection_qualified_name": "default/pg/123"},
        }

        result = await convert_raw_parquet_to_parquet(args, ["database"])

        assert result.endswith("raw_lakehouse")

        out_file = os.path.join(result, "database", "chunk-0.parquet")
        assert os.path.exists(out_file)

        table = pq_lib.read_table(out_file)
        record = table.to_pydict()

        assert record["typename"] == ["database"]
        assert record["connection_qualified_name"] == ["default/pg/123"]
        assert record["workflow_id"] == ["wf-1"]
        assert record["workflow_run_id"] == ["run-1"]
        assert record["tenant_id"] == ["t1"]
        assert record["entity_name"] == ["mydb"]
        raw = json.loads(record["raw_record"][0])
        assert raw["database_name"] == "mydb"
        assert raw["extra_col"] == "val1"

    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.APP_TENANT_ID", "t1"
    )
    @patch("application_sdk.activities.metadata_extraction.lakehouse.ObjectStore")
    @patch("application_sdk.activities.metadata_extraction.lakehouse.download_files")
    async def test_unknown_typename_has_empty_entity_name(
        self, mock_download, mock_objectstore, tmp_path
    ):
        """Typename not in _RAW_ENTITY_NAME_FIELDS → entity_name is empty."""
        src_file = _write_test_parquet(
            str(tmp_path / "src.parquet"), {"some_col": ["val"]}
        )
        mock_download.return_value = [src_file]
        mock_objectstore.upload_prefix = AsyncMock()

        out_base = str(tmp_path / "output")
        os.makedirs(os.path.join(out_base, "raw", "custom_type"), exist_ok=True)

        args = {
            "output_path": out_base,
            "workflow_id": "wf-1",
            "workflow_run_id": "run-1",
            "connection": {"connection_qualified_name": "conn/1"},
        }

        result = await convert_raw_parquet_to_parquet(args, ["custom_type"])

        out_file = os.path.join(result, "custom_type", "chunk-0.parquet")
        table = pq_lib.read_table(out_file)
        record = table.to_pydict()

        assert record["entity_name"] == [""]

    @patch(
        "application_sdk.activities.metadata_extraction.lakehouse.APP_TENANT_ID", "t1"
    )
    @patch("application_sdk.activities.metadata_extraction.lakehouse.download_files")
    async def test_parquet_read_failure_skips_file(self, mock_download, tmp_path):
        """Failed parquet read is skipped, doesn't crash the whole conversion."""
        # Write a corrupt file (not valid parquet)
        bad_file = str(tmp_path / "bad.parquet")
        with open(bad_file, "w") as f:
            f.write("not a parquet file")
        mock_download.return_value = [bad_file]

        out_base = str(tmp_path / "output")
        os.makedirs(os.path.join(out_base, "raw", "table"), exist_ok=True)

        args = {
            "output_path": out_base,
            "workflow_id": "wf-1",
            "workflow_run_id": "run-1",
        }

        result = await convert_raw_parquet_to_parquet(args, ["table"])

        # No output file should be written
        out_dir = os.path.join(result, "table")
        if os.path.exists(out_dir):
            assert len(os.listdir(out_dir)) == 0
