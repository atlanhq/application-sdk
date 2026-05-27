"""Integration tests for create_store_from_binding → Google Cloud Storage (real service).

Verifies that each supported auth mode is correctly wired end-to-end by calling
``list`` on the resulting store. No data is written or deleted.

Prerequisites:
    export GCS_BUCKET=<existing-bucket>
    export GCS_PROJECT_ID=<project-id>
    # For SA key test, also set:
    export GOOGLE_APPLICATION_CREDENTIALS=<path-to-sa-key.json>

Run:
    uv run pytest tests/integration/storage/test_binding_gcs.py -m gcs_integration -v

All tests use ``create_store_from_binding`` against a real Dapr component YAML —
no monkey-patching of the SDK factory.
"""

from __future__ import annotations

import json
import os

import obstore
import pytest

from application_sdk.storage.binding import create_store_from_binding
from tests.integration.storage.conftest import (
    GCS_BUCKET,
    GCS_PROJECT_ID,
    GOOGLE_APPLICATION_CREDENTIALS,
    write_dapr_component,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_auth(store) -> None:
    """Confirm auth works by listing the bucket root (no blobs required)."""
    async for _batch in obstore.list(store, prefix="integ-auth-probe/"):
        break


def _load_sa_key() -> dict:
    """Load SA JSON from GOOGLE_APPLICATION_CREDENTIALS; skip if unavailable."""
    if not GOOGLE_APPLICATION_CREDENTIALS:
        pytest.skip("GOOGLE_APPLICATION_CREDENTIALS not set")
    path = os.path.expandvars(GOOGLE_APPLICATION_CREDENTIALS)
    if not os.path.isfile(path):
        pytest.skip(f"SA key file not found: {path}")
    with open(path) as fh:
        data = json.load(fh)
    if "private_key" not in data:
        pytest.skip(
            "GOOGLE_APPLICATION_CREDENTIALS does not contain a private_key (not a SA key file)"
        )
    return data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.gcs_integration
async def test_service_account_key_auth(tmp_path):
    """Inline SA key fields in Dapr metadata → list succeeds."""
    sa = _load_sa_key()
    # Dapr gcp.bucket binding embeds SA JSON fields as individual metadata entries.
    metadata = {
        "bucket": GCS_BUCKET,
        "project_id": sa.get("project_id", GCS_PROJECT_ID),
        "type": sa.get("type", "service_account"),
        "private_key_id": sa["private_key_id"],
        "private_key": sa["private_key"],
        "client_email": sa["client_email"],
        "client_id": sa.get("client_id", ""),
        "auth_uri": sa.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
        "token_uri": sa.get("token_uri", "https://oauth2.googleapis.com/token"),
    }
    write_dapr_component(
        tmp_path / "components",
        name="gcs-sa-store",
        binding_type="bindings.gcp.bucket",
        metadata=metadata,
    )
    store = create_store_from_binding(
        "gcs-sa-store", components_dir=tmp_path / "components"
    )
    await _assert_auth(store)


@pytest.mark.gcs_integration
async def test_adc_auth(tmp_path):
    """bucket + project_id only → ADC / Workload Identity → list succeeds."""
    write_dapr_component(
        tmp_path / "components",
        name="gcs-adc-store",
        binding_type="bindings.gcp.bucket",
        metadata={
            "bucket": GCS_BUCKET,
            "project_id": GCS_PROJECT_ID,
        },
    )
    store = create_store_from_binding(
        "gcs-adc-store", components_dir=tmp_path / "components"
    )
    await _assert_auth(store)


@pytest.mark.gcs_integration
async def test_bindings_gcs_type_alias(tmp_path):
    """``bindings.gcs`` type alias is accepted (same as ``bindings.gcp.bucket``)."""
    sa = _load_sa_key()
    metadata = {
        "bucket": GCS_BUCKET,
        "project_id": sa.get("project_id", GCS_PROJECT_ID),
        "type": sa.get("type", "service_account"),
        "private_key_id": sa["private_key_id"],
        "private_key": sa["private_key"],
        "client_email": sa["client_email"],
    }
    write_dapr_component(
        tmp_path / "components",
        name="gcs-alias-store",
        binding_type="bindings.gcs",
        metadata=metadata,
    )
    store = create_store_from_binding(
        "gcs-alias-store", components_dir=tmp_path / "components"
    )
    await _assert_auth(store)
