"""Hermetic integration test: the S3 ``assumeRoleArn`` path, via MinIO's own STS.

Covers the one S3 auth mode with no non-mock coverage anywhere else: the Dapr
binding's ``assumeRoleArn``, which routes through
``make_s3_assume_role_provider`` → ``StsCredentialProvider`` → botocore's
``sts:AssumeRole``.

Why this needs a *real* STS response rather than a fake session: botocore
deserialises the ``Expiration`` timestamp with ``dateutil``, yielding
``tzinfo=dateutil.tz.tzutc()``. obstore's Python-side check only tests
``tzinfo is not None``, so a non-stdlib UTC passes there and is rejected at the
Rust boundary with ``ValueError: expected datetime.timezone.utc``, surfaced as
``UnauthenticatedError ... path: "External AWS credential provider"``. A fake
STS client built with ``datetime.timezone.utc`` cannot reproduce that — the
tzinfo under test is produced by botocore's parser, not by our code.

MinIO implements ``AssumeRole`` for internal-IDP users and ignores the supplied
``RoleArn``, so the whole exchange runs against the sidecar the emulator job
already starts. The STS endpoint is resolved by botocore from the
``AWS_ENDPOINT_URL`` environment variable that job already sets — the SDK does
not override the STS endpoint itself.

Marked ``storage_emulator`` (deselected by default; run in CI with a MinIO
sidecar). Local:

    docker run -d --rm -p 9000:9000 \\
        -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \\
        minio/minio server /data
    AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \\
        aws --endpoint-url http://localhost:9000 s3 mb s3://sdk-emulator-test
    AWS_ENDPOINT_URL=http://localhost:9000 uv run pytest \\
        tests/integration/storage/test_emulator_s3_assume_role.py \\
        -m storage_emulator -v
"""

from __future__ import annotations

import os
from datetime import timezone

import pytest

from application_sdk.storage.binding import create_store_from_binding
from application_sdk.storage.cloud import CloudStore
from tests.integration.storage.conftest import write_dapr_component

pytestmark = pytest.mark.storage_emulator

_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:9000")
_USER = os.environ.get("MINIO_ROOT_USER", "minioadmin")
_PASS = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")
_BUCKET = os.environ.get("S3_EMULATOR_BUCKET", "sdk-emulator-test")

# MinIO ignores the RoleArn but boto3 requires the parameter, so any
# well-formed ARN works here.
_ROLE_ARN = "arn:aws:iam::000000000000:role/sdk-emulator-assume-role"


@pytest.fixture(scope="module", autouse=True)
def require_minio() -> None:
    """Skip the module when MinIO isn't reachable or unhealthy."""
    import httpx

    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{_ENDPOINT}/minio/health/live")
    except Exception as exc:  # pragma: no cover — env guard
        pytest.skip(f"MinIO not reachable at {_ENDPOINT}: {exc}")
    if resp.status_code >= 500:  # pragma: no cover — env guard
        pytest.skip(f"MinIO unhealthy at {_ENDPOINT}: HTTP {resp.status_code}")


@pytest.fixture(autouse=True)
def sts_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point botocore's STS client at MinIO.

    Set explicitly rather than relying on the CI job's ``AWS_ENDPOINT_URL`` so a
    local run without that variable exercises the same path instead of silently
    calling real AWS STS.
    """
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", _ENDPOINT)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _assume_role_cloud_store(components_dir) -> CloudStore:
    """Build a CloudStore whose S3 binding authenticates via STS AssumeRole."""
    write_dapr_component(
        components_dir,
        name="s3-emulator-sts",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": _BUCKET,
            "region": "us-east-1",
            "endpoint": _ENDPOINT,
            "forcePathStyle": "true",
            "assumeRoleArn": _ROLE_ARN,
            # Base credentials for the STS call itself.
            "accessKey": _USER,
            "secretKey": _PASS,
        },
    )
    store = create_store_from_binding("s3-emulator-sts", components_dir=components_dir)
    return CloudStore(store, provider="s3")


async def test_assume_role_binding_roundtrips_via_sdk(tmp_path):
    """assumeRoleArn binding round-trips real bytes through a real STS exchange.

    This is the regression test for the obstore expiry-tzinfo rejection: without
    the normalisation in ``_credential_providers``, this fails on the first
    object-store call with ``UnauthenticatedError ... expected
    datetime.timezone.utc`` and never reaches the bucket.
    """
    cs = _assume_role_cloud_store(tmp_path / "components")

    key = "sdk-emulator-sts/roundtrip.txt"
    payload = b"hello-from-sdk-s3-assume-role-test"
    assert await cs.upload_bytes(key, payload) == len(payload)
    assert key in await cs.list(prefix="sdk-emulator-sts/")
    assert await cs.get_bytes(key) == payload

    from application_sdk.storage import ops

    await ops.delete(key, store=cs.store, normalize=False)


def test_sts_expiry_carries_stdlib_utc() -> None:
    """The provider hands obstore an expiry with exactly ``datetime.timezone.utc``.

    Pins the premise the round-trip test depends on against a real botocore
    parse: MinIO's STS answers over the wire, botocore deserialises the
    ``Expiration`` with dateutil, and the provider must convert the result.
    Asserted with ``is``, because obstore's Rust binding requires that exact
    object — and ``dateutil.tz.tzutc()`` is neither identical to nor equal to
    ``datetime.timezone.utc`` (only the datetimes they annotate compare equal).
    """
    from application_sdk.storage._credential_providers import (
        make_s3_assume_role_provider,
    )

    provider = make_s3_assume_role_provider(
        role_arn=_ROLE_ARN,
        region="us-east-1",
        base_access_key=_USER,
        base_secret_key=_PASS,
    )

    credential = provider()

    assert credential["expires_at"].tzinfo is timezone.utc
