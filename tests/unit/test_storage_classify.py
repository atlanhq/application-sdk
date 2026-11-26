"""The storage failure table the worker's store and the API host's store share.

``application_sdk._storage_classify`` is imported by the api wheel's core
install, which has neither ``obstore`` nor ``application_sdk.storage``, so these
tests also pin that it loads neither.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import obstore.exceptions
import pytest

from application_sdk import _storage_classify
from application_sdk._storage_classify import (
    GATEWAY_NO_ROUTE_RETRY_DELAYS,
    StorageFailureKind,
    classify_storage_failure,
    http_evidence,
    is_not_found,
    past_gateway_no_route,
)

# The shapes object_store renders a backend HTTP failure in. Hosts synthetic.
_NO_ROUTE = (
    "Generic S3 error: Error performing GET "
    "https://gateway.example.invalid/api/blobstorage/k in 8ms - "
    "Server returned non-2xx status code: 404 Not Found: "
    '{"message":"no Route matched with those values"}'
)
_GATEWAY_AUTH = (
    "Generic S3 error: Error performing PUT "
    "https://gateway.example.invalid/api/blobstorage/k in 8ms - "
    "Server returned non-2xx status code: 401 Unauthorized: "
    '{"code": 1005, "error": "invalid_client", "message": "x"}'
)
_OTHER_401 = (
    "Server returned non-2xx status code: 401 Unauthorized: "
    "<Error><Code>InvalidAccessKeyId</Code></Error>"
)
_CONTAINER_MISSING = (
    "Generic MicrosoftAzure error: Server returned non-2xx status code: "
    "404 Not Found: <Error><Code>ContainerNotFound</Code>"
    "<Message>The specified container does not exist.</Message></Error>"
)
_NO_SUCH_KEY = (
    "Server returned non-2xx status code: 404 Not Found: "
    "<Error><Code>NoSuchKey</Code></Error>"
)
_GCS_CLIENT_ERROR = "Client error with status 403 Forbidden: <Code>AccessDenied</Code>"


@pytest.mark.parametrize(
    ("exc", "kind", "status", "code"),
    [
        (RuntimeError(_NO_ROUTE), StorageFailureKind.GATEWAY_NO_ROUTE, 404, None),
        (
            RuntimeError(_GATEWAY_AUTH),
            StorageFailureKind.GATEWAY_AUTH_UNAVAILABLE,
            401,
            "1005",
        ),
        (
            RuntimeError(_OTHER_401),
            StorageFailureKind.UNAVAILABLE,
            401,
            "InvalidAccessKeyId",
        ),
        (
            RuntimeError(_CONTAINER_MISSING),
            StorageFailureKind.AZURE_CONTAINER_MISSING,
            404,
            "ContainerNotFound",
        ),
        (RuntimeError(_NO_SUCH_KEY), StorageFailureKind.NOT_FOUND, 404, "NoSuchKey"),
        (FileNotFoundError("k"), StorageFailureKind.NOT_FOUND, None, None),
        (
            obstore.exceptions.NotFoundError("k"),
            StorageFailureKind.NOT_FOUND,
            None,
            None,
        ),
        (
            RuntimeError(_GCS_CLIENT_ERROR),
            StorageFailureKind.UNAVAILABLE,
            403,
            "AccessDenied",
        ),
        (TimeoutError("timed out"), StorageFailureKind.UNAVAILABLE, None, None),
    ],
)
def test_the_table_names_each_failure_with_its_evidence(
    exc: Exception,
    kind: StorageFailureKind,
    status: int | None,
    code: str | None,
) -> None:
    failure = classify_storage_failure(exc)
    assert failure.kind is kind
    assert failure.http_status == status
    assert failure.provider_code == code


def test_the_gateway_no_route_404_is_never_a_missing_object() -> None:
    assert not is_not_found(RuntimeError(_NO_ROUTE))
    # obstore types it as not-found; the table still reads the gateway.
    assert not is_not_found(FileNotFoundError(_NO_ROUTE))


def test_a_missing_container_is_not_read_as_a_missing_object() -> None:
    """Its text also matches the not-found fallback; the container rule wins."""
    assert is_not_found(RuntimeError(_CONTAINER_MISSING))
    assert (
        classify_storage_failure(RuntimeError(_CONTAINER_MISSING)).kind
        is StorageFailureKind.AZURE_CONTAINER_MISSING
    )


def test_evidence_parsing_never_raises() -> None:
    assert http_evidence(Exception()) == (None, None)


def test_the_worker_routes_through_the_same_table() -> None:
    from application_sdk.storage import integrity, ops

    assert ops._obstore_http_evidence(RuntimeError(_GATEWAY_AUTH)) == (401, "1005")
    assert ops._is_gateway_no_route(RuntimeError(_NO_ROUTE))
    assert ops._is_not_found(RuntimeError(_NO_SUCH_KEY))
    assert ops._GATEWAY_NO_ROUTE_RETRY_DELAYS == GATEWAY_NO_ROUTE_RETRY_DELAYS
    assert integrity.sidecar_key("a/b.csv") == "a/b.csv.sha256"
    assert integrity.SIDECAR_SUFFIX == _storage_classify.SIDECAR_SUFFIX


def test_the_sidecar_switch_name_matches_the_workers_constant() -> None:
    import inspect

    from application_sdk import constants

    source = inspect.getsource(constants)
    assert (
        f'STORAGE_WRITE_SIDECARS = env_bool("{_storage_classify.WRITE_SIDECARS_ENV}"'
        in source
    )


def test_the_retry_rides_out_a_no_route_404_and_only_that() -> None:
    slept: list[float] = []

    async def _sleep(delay: float) -> None:
        slept.append(delay)

    attempts = iter([RuntimeError(_NO_ROUTE), RuntimeError(_NO_ROUTE)])

    async def _call() -> str:
        failure = next(attempts, None)
        if failure is not None:
            raise failure
        return "ok"

    assert asyncio.run(past_gateway_no_route("get", "k", _call, sleep=_sleep)) == "ok"
    assert len(slept) == 2

    async def _missing() -> str:
        raise RuntimeError(_NO_SUCH_KEY)

    with pytest.raises(RuntimeError, match="NoSuchKey"):
        asyncio.run(past_gateway_no_route("get", "k", _missing, sleep=_sleep))
    assert len(slept) == 2


def test_the_module_loads_neither_obstore_nor_the_storage_package() -> None:
    probe = (
        "import sys\n"
        "import application_sdk._storage_classify\n"
        "print(sorted(m for m in sys.modules if m == 'obstore' "
        "or m.startswith(('obstore.', 'application_sdk.storage'))))\n"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert out.stdout.strip() == "[]"
