"""Integration tests for create_store_from_binding → AWS S3 (real service).

Verifies that each supported auth mode is correctly wired end-to-end by calling
``list`` on the resulting store. No data is written or deleted.

Prerequisites:
    export AWS_ACCESS_KEY_ID=<key>
    export AWS_SECRET_ACCESS_KEY=<secret>
    export AWS_DEFAULT_REGION=<region>     # default: us-east-1
    export S3_BUCKET=<existing-bucket>

Run:
    uv run pytest tests/integration/storage/test_binding_s3.py -m s3_integration -v

All tests use ``create_store_from_binding`` against a real Dapr component YAML —
no monkey-patching of the SDK factory.
"""

from __future__ import annotations

import obstore
import pytest

from application_sdk.storage.binding import create_store_from_binding
from tests.integration.storage.conftest import (
    S3_ACCESS_KEY,
    S3_BUCKET,
    S3_REGION,
    S3_SECRET_KEY,
    write_dapr_component,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_auth(store) -> None:
    """Confirm auth works by listing the bucket root (no blobs required)."""
    async for _batch in obstore.list(store, prefix="integ-auth-probe/"):
        break


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.s3_integration
async def test_static_key_auth(tmp_path):
    """accessKey + secretKey → list succeeds (Shared Key auth)."""
    write_dapr_component(
        tmp_path / "components",
        name="s3-key-store",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": S3_BUCKET,
            "region": S3_REGION,
            "accessKey": S3_ACCESS_KEY,
            "secretKey": S3_SECRET_KEY,
        },
    )
    store = create_store_from_binding(
        "s3-key-store", components_dir=tmp_path / "components"
    )
    await _assert_auth(store)


@pytest.mark.s3_integration
async def test_secret_key_ref_resolves(tmp_path, monkeypatch):
    """secretKeyRef entries resolve from env vars and auth succeeds."""
    monkeypatch.setenv("_TEST_S3_ACCESS_KEY", S3_ACCESS_KEY)
    monkeypatch.setenv("_TEST_S3_SECRET_KEY", S3_SECRET_KEY)

    comp_dir = tmp_path / "components"
    comp_dir.mkdir(parents=True)
    (comp_dir / "s3-ref-store.yaml").write_text(f"""\
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: s3-ref-store
spec:
  version: v1
  type: bindings.aws.s3
  metadata:
    - name: bucket
      value: {S3_BUCKET}
    - name: region
      value: {S3_REGION}
    - name: accessKey
      secretKeyRef:
        key: _TEST_S3_ACCESS_KEY
    - name: secretKey
      secretKeyRef:
        key: _TEST_S3_SECRET_KEY
""")
    store = create_store_from_binding("s3-ref-store", components_dir=comp_dir)
    await _assert_auth(store)


@pytest.mark.s3_integration
async def test_bindings_s3_type_alias(tmp_path):
    """``bindings.s3`` type alias is accepted (same as ``bindings.aws.s3``)."""
    write_dapr_component(
        tmp_path / "components",
        name="s3-alias-store",
        binding_type="bindings.s3",
        metadata={
            "bucket": S3_BUCKET,
            "region": S3_REGION,
            "accessKey": S3_ACCESS_KEY,
            "secretKey": S3_SECRET_KEY,
        },
    )
    store = create_store_from_binding(
        "s3-alias-store", components_dir=tmp_path / "components"
    )
    await _assert_auth(store)


@pytest.mark.s3_integration
async def test_trust_anchor_arn_raises(tmp_path):
    """trustAnchorArn raises StorageConfigError (IAM Roles Anywhere not supported)."""
    from application_sdk.storage.errors import StorageConfigError

    write_dapr_component(
        tmp_path / "components",
        name="s3-rar-store",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": S3_BUCKET,
            "trustAnchorArn": "arn:aws:rolesanywhere:us-east-1:123:trust-anchor/abc",
        },
    )
    with pytest.raises(StorageConfigError, match="IAM Roles Anywhere"):
        create_store_from_binding(
            "s3-rar-store", components_dir=tmp_path / "components"
        )


@pytest.mark.s3_integration
async def test_assume_role_wires_up(tmp_path):
    """assumeRoleArn creates the STS credential provider without error.

    boto3 is a core dependency. The actual STS call may fail if the role does
    not exist in this account — that is acceptable: the binding wiring is what
    this test validates, not IAM policy configuration.
    """
    write_dapr_component(
        tmp_path / "components",
        name="s3-sts-store",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": S3_BUCKET,
            "region": S3_REGION,
            "accessKey": S3_ACCESS_KEY,
            "secretKey": S3_SECRET_KEY,
            "assumeRoleArn": "arn:aws:iam::123456789012:role/IntegTestRole",
            "sessionName": "integ-auth-test",
        },
    )
    store = create_store_from_binding(
        "s3-sts-store", components_dir=tmp_path / "components"
    )
    assert store is not None
