"""S3-backed :class:`~server_sdk.config.store.ConfigStore` (``[aws]`` extra).

Persists workflow/credential config JSON to the tenant object store using the
same key convention the v2/v3 application-sdk writes and workers read
(``persistent-artifacts/apps/{app}/{type}/{id}/config.json``), so config saved
by a consolidated server is readable by that app's own worker unchanged.

Auth is ambient (node instance role / IRSA / env) — exactly how the fleet's
Dapr ``bindings.aws.s3`` components authenticate today: their specs carry only
``bucket`` + ``region``, no key material, so a plain boto3 client on the same
node resolves the same identity. No credential values are read or handled here.

``load`` mirrors :class:`LocalFileConfigStore`'s contract — missing key →
``None`` (the endpoint's 404). Non-NoSuchKey failures (IAM denied, network,
throttling) also return ``None`` but are logged at WARNING, so a misconfigured
deployment shows up in logs instead of masquerading as "config not found".
``save`` raises on failure (→ 500), never silently dropping a credential write.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from server_sdk.config.store import _json_dumps, _json_loads
from server_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class S3ConfigStore:
    """Config persistence against one S3 bucket via a sync boto3 client.

    boto3 calls are pushed to a worker thread (``asyncio.to_thread``) so the
    serving event loop never blocks on S3 latency. Pass ``client`` explicitly
    in tests; by default one is built from the ambient AWS identity.
    """

    def __init__(
        self, bucket: str, region: str | None = None, client: Any = None
    ) -> None:
        if client is None:
            import boto3  # noqa: PLC0415 — [aws] extra; imported only when actually used

            client = boto3.client("s3", region_name=region or None)
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_env(cls) -> "S3ConfigStore | None":
        """Build from ``S3_BUCKET``/``S3_REGION`` (the chart-injected pair).

        Returns ``None`` — preserving the endpoints' 503 "not configured"
        semantics — when no bucket is set or boto3 isn't installed.
        """
        bucket = os.getenv("S3_BUCKET", "").strip()
        if not bucket:
            return None
        try:
            return cls(bucket, os.getenv("S3_REGION", "").strip() or None)
        except ModuleNotFoundError:
            logger.warning(
                "S3_BUCKET is set but boto3 is not installed; /config endpoints "
                "stay unconfigured (503). Install the server-sdk [aws] extra.",
                exc_info=True,
            )
            return None

    async def load(self, key: str) -> dict[str, Any] | None:
        def _get() -> bytes:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read()

        try:
            return _json_loads(await asyncio.to_thread(_get))
        except Exception as e:
            # Missing object is the ordinary 404 path; anything else is an
            # operational problem worth a log line (but still reads as absent).
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if code not in ("NoSuchKey", "404"):
                logger.warning(
                    "S3 config load failed for key %s: %s", key, e, exc_info=True
                )
            return None

    async def save(self, key: str, body: dict[str, Any]) -> None:
        data = _json_dumps(body)
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType="application/json",
        )


def default_config_store() -> "S3ConfigStore | None":
    """The environment-derived store ``build_asgi_app`` uses when none is injected.

    Mirrors the workflow-starter pattern: explicit injection wins; otherwise the
    deployment environment decides (``S3_BUCKET`` set → S3-backed store; unset →
    ``None`` → /config endpoints answer 503 "not configured").
    """
    return S3ConfigStore.from_env()
