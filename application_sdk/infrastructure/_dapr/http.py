"""Async Dapr HTTP client — replaces the sync ``dapr`` Python library.

Calls the Dapr sidecar HTTP API directly via ``httpx.AsyncClient``.
This avoids blocking the event loop (the ``dapr`` library uses sync gRPC)
while keeping the same sidecar + component model.

Endpoint reference: https://docs.dapr.io/reference/api/
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from httpx_retries import Retry, RetryTransport
from pydantic import BaseModel

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DAPR_HTTP_PORT = 3500
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_RETRY_TOTAL = 3

# Dapr HTTP API v1.0 building blocks
_API_PREFIX = "/v1.0"

STATE_PATH = f"{_API_PREFIX}/state/{{store_name}}"
STATE_KEY_PATH = f"{_API_PREFIX}/state/{{store_name}}/{{key}}"
SECRET_STORE_PATH = _API_PREFIX + "/secrets/{store_name}/{key}"
SECRET_STORE_BULK_PATH = _API_PREFIX + "/secrets/{store_name}/bulk"
PUBLISH_PATH = f"{_API_PREFIX}/publish/{{pubsub_name}}/{{topic}}"
BINDING_PATH = f"{_API_PREFIX}/bindings/{{binding_name}}"
METADATA_PATH = f"{_API_PREFIX}/metadata"


def _dapr_base_url() -> str:
    port = os.environ.get("DAPR_HTTP_PORT", str(_DEFAULT_DAPR_HTTP_PORT))
    return f"http://localhost:{port}"


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class BindingResult(BaseModel):
    """Response from a Dapr binding invocation."""

    data: bytes | None = None
    metadata: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AsyncDaprClient:
    """Thin async wrapper around the Dapr sidecar HTTP API.

    One instance should be created per application and shared across
    all infrastructure components (state store, secret store, etc.).
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        retries: int = _DEFAULT_RETRY_TOTAL,
    ):
        self._base_url = base_url or _dapr_base_url()
        transport = RetryTransport(
            transport=httpx.AsyncHTTPTransport(),
            retry=Retry(
                total=retries,
                backoff_factor=0.5,
                status_forcelist=[500, 502, 503, 504],
            ),
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncDaprClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # State Store
    # ------------------------------------------------------------------

    async def save_state(self, store_name: str, key: str, value: str) -> None:
        resp = await self._client.post(
            STATE_PATH.format(store_name=store_name),
            json=[{"key": key, "value": value}],
        )
        resp.raise_for_status()

    async def get_state(self, store_name: str, key: str) -> bytes | None:
        resp = await self._client.get(
            STATE_KEY_PATH.format(store_name=store_name, key=key)
        )
        if resp.is_error:
            resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.content

    async def delete_state(self, store_name: str, key: str) -> None:
        resp = await self._client.delete(
            STATE_KEY_PATH.format(store_name=store_name, key=key)
        )
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Secret Store
    # ------------------------------------------------------------------

    async def get_secret(self, store_name: str, key: str) -> dict[str, str]:
        from urllib.parse import quote

        resp = await self._client.get(
            SECRET_STORE_PATH.format(store_name=store_name, key=quote(key, safe=""))
        )
        resp.raise_for_status()
        return resp.json()

    async def get_bulk_secret(self, store_name: str) -> dict[str, dict[str, str]]:
        resp = await self._client.get(
            SECRET_STORE_BULK_PATH.format(store_name=store_name)
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Pub/Sub
    # ------------------------------------------------------------------

    async def publish_event(
        self,
        pubsub_name: str,
        topic: str,
        data: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> None:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if metadata:
            for k, v in metadata.items():
                headers[f"metadata.{k}"] = v
        resp = await self._client.post(
            PUBLISH_PATH.format(pubsub_name=pubsub_name, topic=topic),
            content=data,
            headers=headers,
        )
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    async def invoke_binding(
        self,
        binding_name: str,
        operation: str,
        data: bytes | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BindingResult:
        """Invoke a Dapr output binding.

        Note:
            ``data`` is decoded as UTF-8 text before JSON-encoding into the
            request body.  This means only text-compatible payloads are
            supported.  Binary data (protobuf, compressed) should be
            base64-encoded by the caller before passing to this method.
        """
        body: dict[str, Any] = {
            "operation": operation,
            "metadata": metadata or {},
        }
        if data:
            body["data"] = data.decode("utf-8", errors="replace")
        resp = await self._client.post(
            BINDING_PATH.format(binding_name=binding_name),
            json=body,
        )
        resp.raise_for_status()
        return BindingResult(
            data=resp.content if resp.content else None,
            metadata={
                k: v
                for k, v in resp.headers.items()
                if k.lower().startswith("metadata.")
            },
        )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    async def get_metadata(self) -> dict[str, Any]:
        resp = await self._client.get(METADATA_PATH)
        resp.raise_for_status()
        return resp.json()
