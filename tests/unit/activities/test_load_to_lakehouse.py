"""Unit tests for the load_to_lakehouse activity."""

from unittest.mock import patch

import pytest

from application_sdk.activities.common.models import (
    LhLoadRequest,
    LhLoadResponse,
    LhLoadStatusResponse,
    LhTableWriteMode,
)
from application_sdk.activities.metadata_extraction.lakehouse import submit_and_poll_mdlh_load
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
    """Mock aiohttp.ClientSession.

    Supports being used as both a context manager (poll loop creates
    a new session per iteration) and for direct .post()/.get() calls.
    """

    def __init__(self, post_response=None, get_responses=None):
        self._post_response = post_response
        self._get_responses = iter(get_responses or [])

    def post(self, url, json=None, headers=None):
        return self._post_response

    def get(self, url, headers=None):
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
        "application_sdk.activities.metadata_extraction.base.LH_LOAD_POLL_INTERVAL_SECONDS",
        0,
    ),
    patch(
        "application_sdk.activities.metadata_extraction.base.LH_LOAD_MAX_POLL_ATTEMPTS",
        5,
    ),
    patch(
        "application_sdk.activities.metadata_extraction.base.MDLH_BASE_URL",
        "http://test:4541",
    ),
    patch(
        "application_sdk.activities.metadata_extraction.base.APP_TENANT_ID",
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
        mock_aiohttp.ClientSession = _MockSessionFactory(post_resp, [get_resp])
        mock_aiohttp.ClientError = Exception
        mock_aiohttp.ClientTimeout = lambda total: None

        result = await submit_and_poll_mdlh_load(_make_workflow_args())
        assert result.typename == "lakehouse-load-completed"

    @_apply_common_patches
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
        mock_aiohttp.ClientSession = _MockSessionFactory(post_resp, [get_resp])
        mock_aiohttp.ClientError = Exception
        mock_aiohttp.ClientTimeout = lambda total: None

        with pytest.raises(ActivityError, match="LAKEHOUSE_LOAD_ERROR|FAILED"):
            await submit_and_poll_mdlh_load(_make_workflow_args())

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
        mock_aiohttp.ClientSession = _MockSessionFactory(post_resp, running_responses)
        mock_aiohttp.ClientError = Exception
        mock_aiohttp.ClientTimeout = lambda total: None

        with pytest.raises(
            ActivityError, match="LAKEHOUSE_LOAD_TIMEOUT|did not complete"
        ):
            await submit_and_poll_mdlh_load(_make_workflow_args())

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
        mock_aiohttp.ClientSession = _MockSessionFactory(post_resp, [])
        mock_aiohttp.ClientError = Exception
        mock_aiohttp.ClientTimeout = lambda total: None

        with pytest.raises(ActivityError, match="LAKEHOUSE_LOAD_API_ERROR|400"):
            await submit_and_poll_mdlh_load(_make_workflow_args())

    @_apply_common_patches
    @patch(
        "application_sdk.activities.metadata_extraction.base.get_object_store_prefix"
    )
    @patch("application_sdk.activities.metadata_extraction.base.aiohttp")
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
