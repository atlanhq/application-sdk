"""Integration tests for create_store_from_binding → AWS S3 (real service, keyless).

Verifies that the SDK builds a working S3 store and authenticates end-to-end by
listing the bucket. Auth is KEYLESS: the binding carries no static credentials,
so the SDK falls back to the ambient AWS credential chain — in CI these are the
short-lived creds the GitHub OIDC role-assumption exports
(AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN). Embedded-credential resolution
(accessKey/secretKey, secretKeyRef, assumeRoleArn) is covered hermetically by the
emulator tests (``test_emulator_s3.py``) and unit tests.

Prerequisites (set by the keyless CI job; or locally for an ad-hoc run):
    # ambient AWS creds on the environment (any valid chain) +
    export S3_BUCKET=<existing-bucket>
    export AWS_DEFAULT_REGION=<region>     # default: us-east-1

Run:
    uv run pytest tests/integration/storage/test_binding_s3.py -m s3_integration -v
"""

from __future__ import annotations

import obstore
import pytest

from application_sdk.storage.binding import create_store_from_binding
from tests.integration.storage.conftest import (
    S3_BUCKET,
    S3_REGION,
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
async def test_ambient_chain_auth(tmp_path):
    """No creds in the binding → SDK uses the ambient AWS credential chain.

    This is the SDR / customer-infra production path (IRSA / instance-profile /
    OIDC role): the Dapr component carries only bucket + region, and obstore
    authenticates via the ambient chain the runtime provides.
    """
    write_dapr_component(
        tmp_path / "components",
        name="s3-ambient-store",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": S3_BUCKET,
            "region": S3_REGION,
        },
    )
    store = create_store_from_binding(
        "s3-ambient-store", components_dir=tmp_path / "components"
    )
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
