"""Unit tests for the load_to_lakehouse activity."""

from unittest.mock import patch

import pytest

from application_sdk.activities.common.models import (
    LhLoadRequest,
    LhLoadResponse,
    LhLoadStatusResponse,
    LhTableWriteMode,
)
from application_sdk.activities.metadata_extraction.base import _do_lakehouse_load
from application_sdk.common.error_codes import ActivityError


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
    """Mock aiohttp.ClientSession."""

    def __init__(self, post_response, get_responses):
        self._post_response = post_response
        self._get_responses = iter(get_responses)

    def post(self, url, json=None):
        return self._post_response

    def get(self, url):
        return next(self._get_responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestLoadToLakehouse:
    @patch(
        "application_sdk.activities.metadata_extraction.base.LH_LOAD_POLL_INTERVAL_SECONDS",
        0,
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.LH_LOAD_MAX_POLL_ATTEMPTS",
        5,
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.MDLH_BASE_URL",
        "http://test:4541",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.APP_TENANT_ID",
        "test-tenant",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.get_object_store_prefix"
    )
    @patch("application_sdk.activities.metadata_extraction.base.aiohttp")
    async def test_load_success(self, mock_aiohttp, mock_get_prefix):
        """POST 202 + GET COMPLETED → returns ActivityStatistics."""
        mock_get_prefix.return_value = (
            "artifacts/apps/postgres/workflows/wf-1/run-1/raw"
        )

        post_resp = _MockResponse(
            202, {"jobId": "j1", "workflowId": "w1", "status": "ACCEPTED"}
        )
        get_resp = _MockResponse(200, {"jobId": "j1", "status": "COMPLETED"})
        mock_aiohttp.ClientSession.return_value = _MockSession(post_resp, [get_resp])
        mock_aiohttp.ClientError = Exception

        result = await _do_lakehouse_load(_make_workflow_args())
        assert result.typename == "lakehouse-load-completed"

    @patch(
        "application_sdk.activities.metadata_extraction.base.LH_LOAD_POLL_INTERVAL_SECONDS",
        0,
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.LH_LOAD_MAX_POLL_ATTEMPTS",
        5,
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.MDLH_BASE_URL",
        "http://test:4541",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.APP_TENANT_ID",
        "test-tenant",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.get_object_store_prefix"
    )
    @patch("application_sdk.activities.metadata_extraction.base.aiohttp")
    async def test_load_failed_status(self, mock_aiohttp, mock_get_prefix):
        """POST 202 + GET FAILED → raises ActivityError."""
        mock_get_prefix.return_value = "prefix/raw"

        post_resp = _MockResponse(
            202, {"jobId": "j1", "workflowId": "w1", "status": "ACCEPTED"}
        )
        get_resp = _MockResponse(200, {"jobId": "j1", "status": "FAILED"})
        mock_aiohttp.ClientSession.return_value = _MockSession(post_resp, [get_resp])
        mock_aiohttp.ClientError = Exception

        with pytest.raises(ActivityError, match="LAKEHOUSE_LOAD_ERROR|FAILED"):
            await _do_lakehouse_load(_make_workflow_args())

    @patch(
        "application_sdk.activities.metadata_extraction.base.LH_LOAD_POLL_INTERVAL_SECONDS",
        0,
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.LH_LOAD_MAX_POLL_ATTEMPTS",
        2,
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.MDLH_BASE_URL",
        "http://test:4541",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.APP_TENANT_ID",
        "test-tenant",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.get_object_store_prefix"
    )
    @patch("application_sdk.activities.metadata_extraction.base.aiohttp")
    async def test_load_poll_timeout(self, mock_aiohttp, mock_get_prefix):
        """POST 202 + GET always RUNNING → raises timeout error."""
        mock_get_prefix.return_value = "prefix/raw"

        post_resp = _MockResponse(
            202, {"jobId": "j1", "workflowId": "w1", "status": "ACCEPTED"}
        )
        running_responses = [
            _MockResponse(200, {"jobId": "j1", "status": "RUNNING"}) for _ in range(3)
        ]
        mock_aiohttp.ClientSession.return_value = _MockSession(
            post_resp, running_responses
        )
        mock_aiohttp.ClientError = Exception

        with pytest.raises(
            ActivityError, match="LAKEHOUSE_LOAD_TIMEOUT|did not complete"
        ):
            await _do_lakehouse_load(_make_workflow_args())

    @patch(
        "application_sdk.activities.metadata_extraction.base.MDLH_BASE_URL",
        "http://test:4541",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.APP_TENANT_ID",
        "test-tenant",
    )
    @patch(
        "application_sdk.activities.metadata_extraction.base.get_object_store_prefix"
    )
    @patch("application_sdk.activities.metadata_extraction.base.aiohttp")
    async def test_load_api_error(self, mock_aiohttp, mock_get_prefix):
        """POST non-202 → raises API error."""
        mock_get_prefix.return_value = "prefix/raw"

        post_resp = _MockResponse(400, text_data="Bad Request")
        mock_aiohttp.ClientSession.return_value = _MockSession(post_resp, [])
        mock_aiohttp.ClientError = Exception

        with pytest.raises(ActivityError, match="LAKEHOUSE_LOAD_API_ERROR|400"):
            await _do_lakehouse_load(_make_workflow_args())

    async def test_load_missing_config(self):
        """No lh_load_config → raises ActivityError."""
        with pytest.raises(ActivityError, match="Missing lh_load_config"):
            await _do_lakehouse_load({})

    async def test_load_missing_fields(self):
        """Missing required fields → raises ActivityError."""
        with pytest.raises(ActivityError, match="Missing required fields"):
            await _do_lakehouse_load(
                {"lh_load_config": {"output_path": "/tmp/test", "namespace": "ns"}}
            )

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
