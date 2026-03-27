"""Client for the Metadata Lakehouse (MDLH) REST API.

Provides a single aiohttp session for all MDLH interactions within a
workflow run: health check, load submission, and status polling.
"""

import asyncio
from typing import Any, Dict, Optional

import aiohttp

from application_sdk.activities.common.models import (
    ActivityStatistics,
    LhLoadRequest,
    LhLoadResponse,
    LhLoadStatusResponse,
)
from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.common.error_codes import ActivityError
from application_sdk.constants import (
    APP_TENANT_ID,
    LH_LOAD_MAX_POLL_ATTEMPTS,
    LH_LOAD_POLL_INTERVAL_SECONDS,
    MDLH_BASE_URL,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
_HEALTH_TIMEOUT = aiohttp.ClientTimeout(total=5)
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 405, 422}
_TERMINAL_FAILURE_STATES = {"FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}


class MdlhClient:
    """Client for the MDLH REST API.

    Manages a single aiohttp session for the lifetime of the client.
    Call ``load()`` before use and ``close()`` when done.

    Usage::

        client = MdlhClient()
        await client.load()
        if client.is_available:
            stats = await client.submit_and_poll(lh_config)
        await client.close()
    """

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._is_available: bool = False
        self._base_url = f"{MDLH_BASE_URL}/atlan/lh/v1/{APP_TENANT_ID}/load"
        self._headers: Dict[str, str] = {"X-Atlan-Tenant-Id": APP_TENANT_ID}

    @property
    def is_available(self) -> bool:
        """Whether MDLH is deployed and healthy on this tenant."""
        return self._is_available

    @property
    def _active_session(self) -> aiohttp.ClientSession:
        """Return the session, raising if load() was not called."""
        if self._session is None:
            raise RuntimeError("MdlhClient.load() must be called before use")
        return self._session

    async def load(self) -> None:
        """Create the HTTP session and check MDLH availability."""
        self._session = aiohttp.ClientSession()
        self._is_available = await self._health_check()

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _health_check(self) -> bool:
        """Hit the MDLH actuator health endpoint."""
        health_url = f"{MDLH_BASE_URL}/actuator/health"
        try:
            async with self._active_session.get(
                health_url, timeout=_HEALTH_TIMEOUT
            ) as resp:
                if resp.status == 200:
                    return True
                logger.info(
                    f"MDLH health check returned {resp.status}, "
                    "lakehouse not available on this tenant"
                )
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            logger.info(
                f"MDLH not reachable ({e}), lakehouse not available on this tenant"
            )
            return False

    async def submit_and_poll(self, lh_config: Dict[str, Any]) -> ActivityStatistics:
        """Submit a load job to MDLH and poll until completion.

        Args:
            lh_config: Dict with output_path, namespace, table_name, mode,
                file_extension.

        Returns:
            ActivityStatistics with typename indicating the outcome.

        Raises:
            ActivityError: On missing config, API errors, or timeout.
        """
        output_path = lh_config.get("output_path", "")
        namespace = lh_config.get("namespace", "")
        table_name = lh_config.get("table_name", "")
        mode = lh_config.get("mode", "APPEND")
        file_extension = lh_config.get("file_extension", "")

        if not namespace or not table_name or not output_path or not file_extension:
            raise ActivityError(
                f"{ActivityError.LAKEHOUSE_LOAD_ERROR}: "
                "Missing required fields in lh_load_config "
                "(namespace, table_name, output_path, file_extension)"
            )

        s3_prefix = get_object_store_prefix(output_path)
        pattern = f"{s3_prefix}/**/*{file_extension}"

        request = LhLoadRequest(
            file_keys=[],
            patterns=[pattern],
            namespace=namespace,
            table_name=table_name,
            mode=mode,
        )
        request_payload = request.model_dump(by_alias=True, exclude_none=True)

        # Submit load job
        job_id = await self._submit_load(request_payload)
        logger.info(f"Lakehouse load job submitted: job_id={job_id}")

        # Poll until completion
        return await self._poll_until_done(job_id)

    async def _submit_load(self, payload: Dict[str, Any]) -> str:
        """POST to /load and return the job_id."""
        async with self._active_session.post(
            self._base_url,
            json=payload,
            headers=self._headers,
            timeout=_REQUEST_TIMEOUT,
        ) as resp:
            if resp.status != 202:
                body = await resp.text()
                raise ActivityError(
                    f"{ActivityError.LAKEHOUSE_LOAD_API_ERROR}: "
                    f"MDLH load API returned {resp.status}: {body}"
                )
            response_data = await resp.json()

        load_response = LhLoadResponse.model_validate(response_data)
        return load_response.job_id

    async def _poll_until_done(self, job_id: str) -> ActivityStatistics:
        """Poll GET /load/{job_id}/status until terminal state."""
        status_url = f"{self._base_url}/{job_id}/status"
        poll_headers = {**self._headers, "X-Lakehouse-Job-Id": job_id}

        for attempt in range(LH_LOAD_MAX_POLL_ATTEMPTS):
            await asyncio.sleep(LH_LOAD_POLL_INTERVAL_SECONDS)
            try:
                async with self._active_session.get(
                    status_url, headers=poll_headers, timeout=_REQUEST_TIMEOUT
                ) as resp:
                    if resp.status in _NON_RETRYABLE_STATUS_CODES:
                        body = await resp.text()
                        raise ActivityError(
                            f"{ActivityError.LAKEHOUSE_LOAD_API_ERROR}: "
                            f"MDLH status poll returned non-retryable "
                            f"{resp.status}: {body}"
                        )
                    if resp.status != 200:
                        logger.warning(
                            f"Lakehouse load status poll returned {resp.status} "
                            f"(attempt {attempt + 1}/{LH_LOAD_MAX_POLL_ATTEMPTS}), "
                            f"retrying..."
                        )
                        continue
                    status_data = await resp.json()
            except ActivityError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(
                    f"Lakehouse load status poll error "
                    f"(attempt {attempt + 1}/{LH_LOAD_MAX_POLL_ATTEMPTS}): "
                    f"{e}, retrying..."
                )
                continue

            status_response = LhLoadStatusResponse.model_validate(status_data)
            current_status = status_response.status.upper()

            if current_status == "COMPLETED":
                logger.info(f"Lakehouse load job completed: job_id={job_id}")
                return ActivityStatistics(typename="lakehouse-load-completed")

            if current_status in _TERMINAL_FAILURE_STATES:
                raise ActivityError(
                    f"{ActivityError.LAKEHOUSE_LOAD_ERROR}: "
                    f"Lakehouse load job {job_id} ended with status: "
                    f"{current_status}"
                )

        raise ActivityError(
            f"{ActivityError.LAKEHOUSE_LOAD_TIMEOUT_ERROR}: "
            f"Lakehouse load job {job_id} did not complete within "
            f"{LH_LOAD_MAX_POLL_ATTEMPTS * LH_LOAD_POLL_INTERVAL_SECONDS}s"
        )
