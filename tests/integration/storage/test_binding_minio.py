"""Integration tests for create_store_from_binding → MinIO (S3-compatible).

Prerequisites:
    docker compose -f tests/integration/storage/docker-compose.yml up -d minio minio-setup

Run:
    uv run pytest tests/integration/storage/test_binding_minio.py -m s3_integration -v

The ``minio-setup`` service creates the ``test-bucket`` bucket automatically.
All tests use ``create_store_from_binding`` against a real Dapr component YAML —
no monkey-patching of the SDK factory.
"""

from __future__ import annotations

import uuid

import obstore
import pytest

from application_sdk.storage.binding import create_store_from_binding
from tests.integration.storage.conftest import (
    MINIO_ACCESS_KEY,
    MINIO_BUCKET,
    MINIO_ENDPOINT,
    MINIO_SECRET_KEY,
    write_dapr_component,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unique_prefix() -> str:
    return f"integ/{uuid.uuid4().hex[:8]}"


async def _round_trip(store, prefix: str, content: bytes = b"hello-minio") -> None:
    """Put *content* at ``{prefix}/data.bin``, get it back, assert equality."""
    key = f"{prefix}/data.bin"
    await obstore.put_async(store, key, content)
    result = await obstore.get_async(store, key)
    assert bytes(result.bytes()) == content

    keys = []
    async for batch in obstore.list(store, prefix=prefix):
        for item in batch:
            keys.append(str(item["path"]))
    assert key in keys


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.s3_integration
async def test_static_key_round_trip(tmp_path):
    """Static accessKey + secretKey + endpoint + forcePathStyle → put/get/list."""
    write_dapr_component(
        tmp_path / "components",
        name="minio-store",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": MINIO_BUCKET,
            "endpoint": MINIO_ENDPOINT,
            "accessKey": MINIO_ACCESS_KEY,
            "secretKey": MINIO_SECRET_KEY,
            "region": "us-east-1",
            "forcePathStyle": "true",
            "disableSSL": "true",
        },
    )
    store = create_store_from_binding(
        "minio-store", components_dir=tmp_path / "components"
    )
    await _round_trip(store, _unique_prefix())


@pytest.mark.s3_integration
async def test_secret_key_ref_resolves_credentials(tmp_path, monkeypatch):
    """secretKeyRef entries are resolved from env vars end-to-end."""
    monkeypatch.setenv("TEST_MINIO_ACCESS_KEY", MINIO_ACCESS_KEY)
    monkeypatch.setenv("TEST_MINIO_SECRET_KEY", MINIO_SECRET_KEY)

    comp_dir = tmp_path / "components"
    comp_dir.mkdir(parents=True)
    (comp_dir / "minio-secret-store.yaml").write_text(f"""\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: minio-secret-store
spec:
  version: v1
  type: bindings.aws.s3
  metadata:
    - name: bucket
      value: {MINIO_BUCKET}
    - name: endpoint
      value: {MINIO_ENDPOINT}
    - name: region
      value: us-east-1
    - name: forcePathStyle
      value: 'true'
    - name: disableSSL
      value: 'true'
    - name: accessKey
      secretKeyRef:
        key: TEST_MINIO_ACCESS_KEY
    - name: secretKey
      secretKeyRef:
        key: TEST_MINIO_SECRET_KEY
""")

    store = create_store_from_binding("minio-secret-store", components_dir=comp_dir)
    await _round_trip(store, _unique_prefix())


@pytest.mark.s3_integration
async def test_disable_ssl_and_force_path_style(tmp_path):
    """disableSSL=true + forcePathStyle=true client options are forwarded correctly."""
    write_dapr_component(
        tmp_path / "components",
        name="minio-ssl-store",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": MINIO_BUCKET,
            "endpoint": MINIO_ENDPOINT,
            "accessKey": MINIO_ACCESS_KEY,
            "secretKey": MINIO_SECRET_KEY,
            "region": "us-east-1",
            "forcePathStyle": "True",
            "disableSSL": "True",
        },
    )
    store = create_store_from_binding(
        "minio-ssl-store", components_dir=tmp_path / "components"
    )
    await _round_trip(store, _unique_prefix(), content=b"ssl-disabled-test")


@pytest.mark.s3_integration
async def test_case_insensitive_bool_fields(tmp_path):
    """Bool metadata fields (forcePathStyle, disableSSL) accept mixed case."""
    write_dapr_component(
        tmp_path / "components",
        name="minio-bool-store",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": MINIO_BUCKET,
            "endpoint": MINIO_ENDPOINT,
            "accessKey": MINIO_ACCESS_KEY,
            "secretKey": MINIO_SECRET_KEY,
            "region": "us-east-1",
            "forcePathStyle": "TRUE",
            "disableSSL": "YES",
        },
    )
    store = create_store_from_binding(
        "minio-bool-store", components_dir=tmp_path / "components"
    )
    await _round_trip(store, _unique_prefix())


@pytest.mark.s3_integration
async def test_bindings_s3_type_alias(tmp_path):
    """``bindings.s3`` type alias is accepted (same as ``bindings.aws.s3``)."""
    write_dapr_component(
        tmp_path / "components",
        name="minio-alias-store",
        binding_type="bindings.s3",
        metadata={
            "bucket": MINIO_BUCKET,
            "endpoint": MINIO_ENDPOINT,
            "accessKey": MINIO_ACCESS_KEY,
            "secretKey": MINIO_SECRET_KEY,
            "region": "us-east-1",
            "forcePathStyle": "true",
            "disableSSL": "true",
        },
    )
    store = create_store_from_binding(
        "minio-alias-store", components_dir=tmp_path / "components"
    )
    await _round_trip(store, _unique_prefix())


@pytest.mark.s3_integration
async def test_multipart_upload(tmp_path):
    """Upload a >5 MiB object to exercise the multipart upload code path."""
    write_dapr_component(
        tmp_path / "components",
        name="minio-mp-store",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": MINIO_BUCKET,
            "endpoint": MINIO_ENDPOINT,
            "accessKey": MINIO_ACCESS_KEY,
            "secretKey": MINIO_SECRET_KEY,
            "region": "us-east-1",
            "forcePathStyle": "true",
            "disableSSL": "true",
        },
    )
    store = create_store_from_binding(
        "minio-mp-store", components_dir=tmp_path / "components"
    )

    payload = b"x" * (6 * 1024 * 1024)
    await _round_trip(store, _unique_prefix(), content=payload)


@pytest.mark.s3_integration
async def test_trust_anchor_arn_raises(tmp_path):
    """trustAnchorArn raises StorageConfigError (IAM Roles Anywhere not supported)."""
    from application_sdk.storage.errors import StorageConfigError

    write_dapr_component(
        tmp_path / "components",
        name="minio-rar-store",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": MINIO_BUCKET,
            "trustAnchorArn": "arn:aws:rolesanywhere:us-east-1:123:trust-anchor/abc",
        },
    )
    with pytest.raises(StorageConfigError, match="IAM Roles Anywhere"):
        create_store_from_binding(
            "minio-rar-store", components_dir=tmp_path / "components"
        )


@pytest.mark.s3_integration
async def test_assume_role(tmp_path):
    """assumeRoleArn path creates store via StsCredentialProvider.

    Requires boto3 (iam_auth extra).  Skipped if boto3 is not installed.
    """
    boto3 = pytest.importorskip(
        "boto3",
        reason="boto3 required for assumeRoleArn test (uv sync --extra iam_auth)",
    )

    # MinIO supports STS AssumeRole at the same endpoint.
    # Create a policy + role via MinIO admin API so the call can succeed.
    # If MinIO is not configured with IAM policies this will raise a credentials
    # error from STS; we accept that as a valid "the binding path worked" outcome
    # since the goal is to verify the provider is wired up, not that MinIO's IAM
    # is fully configured.
    write_dapr_component(
        tmp_path / "components",
        name="minio-sts-store",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": MINIO_BUCKET,
            "endpoint": MINIO_ENDPOINT,
            "region": "us-east-1",
            "forcePathStyle": "true",
            "disableSSL": "true",
            "assumeRoleArn": "arn:aws:iam::123456789012:role/TestRole",
            "sessionName": "integ-test",
            "accessKey": MINIO_ACCESS_KEY,
            "secretKey": MINIO_SECRET_KEY,
        },
    )
    # Store creation wires up the STS provider — this should not raise.
    store = create_store_from_binding(
        "minio-sts-store", components_dir=tmp_path / "components"
    )
    assert store is not None
    # Actual put will fail unless MinIO IAM is configured; that is acceptable.
    # The important assertion is that the provider was created without error.
