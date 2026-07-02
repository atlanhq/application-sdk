"""Integration tests for create_store_from_binding → Google Cloud Storage (real, keyless).

Verifies that the SDK builds a working GCS store and authenticates end-to-end.
Auth is KEYLESS: the binding carries only bucket + project_id, so obstore uses
Application Default Credentials — in CI these come from the GitHub Workload
Identity Federation auth step (GOOGLE_APPLICATION_CREDENTIALS). Embedded
service-account-key resolution (inline SA JSON fields in Dapr metadata) is
covered hermetically by the emulator tests (``test_emulator_gcs.py``) and unit
tests.

Prerequisites (set by the keyless CI job; or locally for an ad-hoc run):
    export GCS_BUCKET=<existing-bucket>
    export GCS_PROJECT_ID=<project-id>
    export GOOGLE_APPLICATION_CREDENTIALS=<ADC / WIF credential file>

Run:
    uv run pytest tests/integration/storage/test_binding_gcs.py -m gcs_integration -v
"""

from __future__ import annotations

import hashlib
import os
import secrets
import warnings

import pytest

from application_sdk.storage import ops
from application_sdk.storage.binding import create_store_from_binding
from application_sdk.storage.cloud import CloudStore
from tests.integration.storage.conftest import (
    GCS_BUCKET,
    GCS_PROJECT_ID,
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


def _adc_metadata() -> dict[str, str]:
    """ADC / Workload-Identity binding metadata (no embedded key)."""
    return {"bucket": GCS_BUCKET, "project_id": GCS_PROJECT_ID}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.gcs_integration
async def test_adc_auth(tmp_path):
    """bucket + project_id only → ADC / Workload Identity → list succeeds.

    The SDR / customer-infra production path on GKE: no key in the binding,
    obstore authenticates via Application Default Credentials.
    """
    write_dapr_component(
        tmp_path / "components",
        name="gcs-adc-store",
        binding_type="bindings.gcp.bucket",
        metadata=_adc_metadata(),
    )
    store = create_store_from_binding(
        "gcs-adc-store", components_dir=tmp_path / "components"
    )
    await _assert_auth(store)


@pytest.mark.gcs_integration
async def test_bindings_gcs_type_alias(tmp_path):
    """``bindings.gcs`` type alias is accepted (same as ``bindings.gcp.bucket``)."""
    write_dapr_component(
        tmp_path / "components",
        name="gcs-alias-store",
        binding_type="bindings.gcs",
        metadata=_adc_metadata(),
    )
    store = create_store_from_binding(
        "gcs-alias-store", components_dir=tmp_path / "components"
    )
    await _assert_auth(store)


@pytest.mark.gcs_integration
async def test_large_payload_round_trip(tmp_path):
    """>=100 MiB round-trip against real GCS — the multipart/streaming paths the
    storage-testbench emulator can't serve (hermetic GCS test stays write+read).

    Exercises both SDK entry points end-to-end with SHA-256: CloudStore.upload/
    download (single PUT/GET) and ops.upload_file/download_file (obstore
    multipart writer + streaming range-GET). Keyless (ADC).
    """
    write_dapr_component(
        tmp_path / "components",
        name="gcs-large-store",
        binding_type="bindings.gcp.bucket",
        metadata=_adc_metadata(),
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
        for k in (cs_key, ops_key):
            try:
                await ops.delete(k, store=cs.store)
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                warnings.warn(f"GCS cleanup failed for {k!r}: {exc}", stacklevel=1)
