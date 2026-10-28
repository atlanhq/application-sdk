"""Fakes for driving ``DaprCredentialVault`` against a missing config blob.

Shared by the vault's own tests and the preflight-gate tests that replay a
missing credential config end to end, so both exercise the same binding answer.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import httpx

from tests.unit.private_stdlib import private_stdlib


@contextmanager
def vault_sleep() -> Iterator[AsyncMock]:
    """No-op the vault's own ``asyncio.sleep``, and only the vault's."""
    with (
        private_stdlib(
            "application_sdk.infrastructure._dapr.credential_vault", "asyncio"
        ) as local_asyncio,
        patch.object(local_asyncio, "sleep", new_callable=AsyncMock) as sleep,
    ):
        yield sleep


def binding_500(dapr_message: str) -> httpx.HTTPStatusError:
    """A Dapr binding GET answered with HTTP 500 and Dapr's JSON error body."""
    req = httpx.Request("POST", "http://localhost:3500/v1.0/bindings/upstream")
    body = {"errorCode": "ERR_INVOKE_OUTPUT_BINDING", "message": dapr_message}
    resp = httpx.Response(500, request=req, content=json.dumps(body).encode())
    return httpx.HTTPStatusError(
        "Server error '500 Internal Server Error'", request=req, response=resp
    )
