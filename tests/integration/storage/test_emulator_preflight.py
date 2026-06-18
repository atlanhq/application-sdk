"""Integration tests: SDR boot-time object-store access preflight.

Exercises ``verify_object_store_access`` against real MinIO stores, covering the
actual obstore code paths including network I/O, credential checking, and
round-trip write/read/delete semantics.

Marked ``storage_emulator`` (deselected by default; run in CI with a MinIO sidecar).
Local:

    docker run -d --rm -p 9000:9000 \\
        -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \\
        minio/minio server /data
    AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \\
        aws --endpoint-url http://localhost:9000 s3 mb s3://sdk-customer-objectstore
    AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \\
        aws --endpoint-url http://localhost:9000 s3 mb s3://sdk-atlan-objectstore
    uv run pytest tests/integration/storage/test_emulator_preflight.py \\
        -m storage_emulator -v
"""

from __future__ import annotations

import os

import pytest

from application_sdk.storage.binding import create_store_from_binding
from application_sdk.storage.errors import ObjectStorePreflightError
from application_sdk.storage.preflight import verify_object_store_access
from tests.integration.storage.conftest import write_dapr_component

pytestmark = pytest.mark.storage_emulator

_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:9000")
_USER = os.environ.get("MINIO_ROOT_USER", "minioadmin")
_PASS = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")
_CUSTOMER_BUCKET = os.environ.get(
    "CUSTOMER_OBJECT_STORE_BUCKET", "sdk-customer-objectstore"
)
_ATLAN_BUCKET = os.environ.get("ATLAN_OBJECT_STORE_BUCKET", "sdk-atlan-objectstore")


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


def _s3_store(
    component_name: str,
    bucket: str,
    components_dir,
    *,
    access_key: str = _USER,
    secret_key: str = _PASS,
):
    """Build a real obstore S3 store from a Dapr component pointed at MinIO."""
    write_dapr_component(
        components_dir,
        name=component_name,
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": bucket,
            "region": "us-east-1",
            "endpoint": _ENDPOINT,
            "forcePathStyle": "true",
            "accessKey": access_key,
            "secretKey": secret_key,
        },
    )
    return create_store_from_binding(component_name, components_dir=components_dir)


def _make_infra(*, storage=None, upstream_storage=None):
    """Build a minimal InfrastructureContext with only the storage fields populated."""
    from application_sdk.infrastructure.context import InfrastructureContext

    return InfrastructureContext(storage=storage, upstream_storage=upstream_storage)


async def test_preflight_passes_when_both_stores_healthy(tmp_path, monkeypatch):
    """SDR mode + both stores configured and accessible → preflight passes cleanly."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    components = tmp_path / "components"
    deployment_store = _s3_store("objectstore", _CUSTOMER_BUCKET, components)
    upstream_store = _s3_store("atlan-objectstore", _ATLAN_BUCKET, components)

    infra = _make_infra(storage=deployment_store, upstream_storage=upstream_store)

    # Must not raise
    await verify_object_store_access(infra)


async def test_preflight_noop_when_not_sdr(monkeypatch):
    """ENABLE_ATLAN_UPLOAD=false → preflight is a no-op; neither store is touched."""
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", False)

    # storage=None would blow up if the preflight attempted any I/O
    infra = _make_infra(storage=None, upstream_storage=None)
    await verify_object_store_access(infra)  # returns without error


async def test_preflight_skips_upstream_probe_when_upstream_absent(
    tmp_path, monkeypatch
):
    """SDR mode + upstream_storage=None → upstream probe skipped; no error raised.

    Absent upstream binding in SDR mode is caught earlier at construction time
    by ``_create_store_from_binding_optional_with_put_attrs(required=True)``,
    which raises ``StorageBindingNotFoundError``.  ``verify_object_store_access``
    itself only probes stores that are present.
    """
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    components = tmp_path / "components"
    deployment_store = _s3_store("objectstore", _CUSTOMER_BUCKET, components)

    infra = _make_infra(storage=deployment_store, upstream_storage=None)

    await verify_object_store_access(infra)  # must not raise


async def test_preflight_fails_with_bad_credentials(tmp_path, monkeypatch):
    """SDR mode + wrong secret key on deployment store → ObjectStorePreflightError.

    MinIO returns SignatureDoesNotMatch / AccessDenied for a wrong secret key;
    the preflight classifier should map this to "invalid credentials" or
    "permission denied" (both indicate a credential/auth failure, not a
    connectivity issue).
    """
    import application_sdk.constants as constants_mod

    monkeypatch.setattr(constants_mod, "ENABLE_ATLAN_UPLOAD", True)

    components = tmp_path / "components"
    # Correct access key ID but wrong secret — MinIO returns auth error on first write
    bad_deployment = _s3_store(
        "objectstore",
        _CUSTOMER_BUCKET,
        components,
        secret_key="definitely-wrong-secret-key",
    )
    # Valid upstream store so only the deployment probe fails
    upstream_store = _s3_store(
        "atlan-objectstore", _ATLAN_BUCKET, components / "upstream"
    )

    infra = _make_infra(storage=bad_deployment, upstream_storage=upstream_store)

    with pytest.raises(ObjectStorePreflightError) as exc_info:
        await verify_object_store_access(infra)

    err = exc_info.value
    assert err.failure_count >= 1
    msg = str(err).lower()
    # Both "invalid credentials" and "permission denied" are acceptable —
    # the exact wording depends on how MinIO surfaces the auth error.
    assert "invalid credentials" in msg or "permission denied" in msg
    # Upstream store (valid credentials) should not appear as a failure
    assert "upstream" not in msg or "not configured" not in msg
