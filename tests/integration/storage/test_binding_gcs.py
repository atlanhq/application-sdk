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

import hashlib
import json
import os
import secrets

import pytest

from application_sdk.storage import ops
from application_sdk.storage.binding import create_store_from_binding
from application_sdk.storage.cloud import CloudStore
from tests.integration.storage.conftest import (
    GCS_BUCKET,
    GCS_PROJECT_ID,
    GOOGLE_APPLICATION_CREDENTIALS,
    write_dapr_component,
)

# >=100 MiB so obstore's GCS multipart/resumable upload + streaming range-GET
# actually engage — the paths the hermetic storage-testbench emulator can't
# serve, so GCS large-payload coverage lives here (real GCS, on-demand).
_LARGE_BYTES = int(os.environ.get("SDK_LARGE_PAYLOAD_MIB", "101")) * 1024 * 1024

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_auth(store) -> None:
    """Confirm auth works by listing the bucket root (no blobs required)."""
    cs = CloudStore(store, provider="gcs")
    await cs.list(prefix="integ-auth-probe/")


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
async def test_large_payload_round_trip(tmp_path):
    """>=100 MiB round-trip against real GCS — the multipart/streaming paths the
    storage-testbench emulator can't serve (hermetic GCS test stays write+read).

    Exercises both SDK entry points end-to-end with SHA-256: CloudStore.upload/
    download (single PUT/GET) and ops.upload_file/download_file (obstore
    multipart writer + streaming range-GET).
    """
    sa = _load_sa_key()
    write_dapr_component(
        tmp_path / "components",
        name="gcs-large-store",
        binding_type="bindings.gcp.bucket",
        metadata={
            "bucket": GCS_BUCKET,
            "project_id": sa.get("project_id", GCS_PROJECT_ID),
            "type": sa.get("type", "service_account"),
            "private_key_id": sa["private_key_id"],
            "private_key": sa["private_key"],
            "client_email": sa["client_email"],
        },
    )
    cs = CloudStore(
        create_store_from_binding(
            "gcs-large-store", components_dir=tmp_path / "components"
        ),
        provider="gcs",
    )

    # Generate a >=100 MiB file once.
    src = tmp_path / "payload.bin"
    block = secrets.token_bytes(8 * 1024 * 1024)
    h = hashlib.sha256()
    written = 0
    with src.open("wb") as f:
        while written < _LARGE_BYTES:
            f.write(block)
            h.update(block)
            written += len(block)
    src_sha, src_size = h.hexdigest(), written

    cs_key = "integ-large/cloudstore.bin"
    ops_key = "integ-large/ops-chunked.bin"
    try:
        # CloudStore single PUT/GET.
        assert await cs.upload(local_path=src, key=cs_key) == src_size
        dl = await cs.download(key=cs_key, output_dir=str(tmp_path / "cs-dl"))
        assert len(dl) == 1 and dl[0].stat().st_size == src_size
        assert hashlib.sha256(dl[0].read_bytes()).hexdigest() == src_sha

        # ops multipart writer + streaming range-GET.
        assert (
            await ops.upload_file(ops_key, src, store=cs.store, normalize=False)
            == src_sha
        )
        ops_dl = tmp_path / "ops-dl.bin"
        assert (
            await ops.download_file(
                ops_key, ops_dl, store=cs.store, compute_hash=True, normalize=False
            )
            == src_sha
        )
        assert ops_dl.stat().st_size == src_size
    finally:
        import contextlib

        for k in (cs_key, ops_key):
            with contextlib.suppress(Exception):
                await ops.delete(k, store=cs.store)


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
