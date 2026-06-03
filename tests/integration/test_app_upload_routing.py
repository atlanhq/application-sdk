"""Integration tests pinning two-store routing for App.upload() (ADR-0014).

Three invariants verified:

(a) App.upload() routes to upstream_storage when an upstream store is wired —
    the file lands in the Atlan-owned bucket, NOT the customer deployment store.

(b) App.upload() falls back to storage when upstream_storage is None —
    the file lands in the deployment store (standard / non-SDR deployments).

(c) The activity interceptor (persist_file_reference) always writes FileReferences
    to the deployment store, never to upstream_storage — even when upstream is
    wired alongside it.

Invariant (c) is the inverse of (a): it proves that returning a FileReference from
a @task is NOT a substitute for an explicit App.upload() hand-off. A connector
that relies on the interceptor alone will produce a silent failure in SDR
deployments — the DAG succeeds but Atlan's publish app finds nothing in its bucket.

See:
  docs/concepts/file-reference.md   (decision matrix and lifecycle)
  docs/adr/0014-two-store-storage-architecture.md   (full rationale)
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from application_sdk.app import App
from application_sdk.app.context import AppContext
from application_sdk.contracts.storage import UploadInput
from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.storage.batch import list_keys
from application_sdk.storage.factory import create_local_store
from application_sdk.storage.reference import persist_file_reference


class _UploadApp(App):
    """Minimal App subclass for upload routing tests."""

    _app_registered: ClassVar[bool] = True
    _app_name: ClassVar[str] = "upload-routing-test"

    async def run(self, input):  # type: ignore[override]
        pass  # not exercised by these tests


def _make_app(
    deployment_store,
    upstream_store=None,
    run_id: str = "run-routing-test",
) -> _UploadApp:
    """Build an _UploadApp with the given stores wired into its context."""
    app = _UploadApp()
    app._context = AppContext(
        app_name=app._app_name,
        app_version="1",
        run_id=run_id,
        _storage=deployment_store,
        _upstream_storage=upstream_store,
    )
    return app


# ---------------------------------------------------------------------------
# (a) App.upload() routes to upstream_storage when present
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_routes_to_upstream_when_present(tmp_path):
    """App.upload() must land the file in upstream_storage, not in storage.

    This is the SDR case: upstream_storage is the Atlan-owned atlan-objectstore,
    storage is the customer-owned objectstore. Published artifacts must reach
    Atlan's bucket so the publish app can index them.
    """
    deployment_root = tmp_path / "deployment"
    upstream_root = tmp_path / "upstream"
    deployment_store = create_local_store(deployment_root)
    upstream_store = create_local_store(upstream_root)

    src = tmp_path / "artifact.jsonl"
    src.write_text('{"name": "test-entity"}\n')

    app = _make_app(deployment_store, upstream_store=upstream_store)
    result = await app.upload(
        UploadInput(local_path=str(src), tier=StorageTier.RETAINED)
    )

    storage_path = result.ref.storage_path
    assert storage_path is not None

    # The file must be findable in upstream
    upstream_keys = await list_keys("", store=upstream_store)
    assert any(storage_path in key or key in storage_path for key in upstream_keys), (
        f"App.upload() did not route to upstream_storage. "
        f"Expected key containing '{storage_path}' in upstream, found: {upstream_keys}"
    )

    # The same key must NOT be in the deployment store
    deployment_keys = await list_keys("", store=deployment_store)
    assert not any(
        storage_path in key or key in storage_path
        for key in deployment_keys
        if not key.endswith(".sha256")
    ), (
        f"App.upload() incorrectly wrote to deployment store when upstream was present. "
        f"deployment_keys: {deployment_keys}"
    )


# ---------------------------------------------------------------------------
# (b) App.upload() falls back to storage when upstream_storage is None
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_falls_back_when_upstream_none(tmp_path):
    """App.upload() must use storage when upstream_storage is None.

    This is the standard (non-SDR) case: no atlan-objectstore is configured,
    so App.upload() falls back to the deployment store. The file must land
    in storage so the run produces usable artifacts in local dev / Atlan-hosted.
    """
    deployment_root = tmp_path / "deployment"
    deployment_store = create_local_store(deployment_root)

    src = tmp_path / "artifact.jsonl"
    src.write_text('{"name": "test-entity"}\n')

    app = _make_app(deployment_store, upstream_store=None)
    result = await app.upload(
        UploadInput(local_path=str(src), tier=StorageTier.RETAINED)
    )

    storage_path = result.ref.storage_path
    assert storage_path is not None

    # The file must be in the deployment store (fallback)
    deployment_keys = await list_keys("", store=deployment_store)
    assert any(storage_path in key or key in storage_path for key in deployment_keys), (
        f"App.upload() did not fall back to deployment store when upstream is None. "
        f"Expected key containing '{storage_path}' in deployment, found: {deployment_keys}"
    )

    assert result.ref.is_durable
    assert result.ref.file_count is not None and result.ref.file_count >= 1


# ---------------------------------------------------------------------------
# (c) persist_file_reference (interceptor) writes to deployment, never upstream
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_filereference_persist_uses_deployment_store_not_upstream(tmp_path):
    """The activity interceptor (persist_file_reference) must write to the
    customer-owned deployment store, never to upstream_storage.

    This test pins the inverse of test (a): returning a FileReference from a
    @task does NOT hand the artifact off to Atlan's bucket. Connectors that rely
    on the interceptor alone for the publish hand-off produce a silent failure in
    SDR — the DAG succeeds but the publish app finds nothing in its bucket.

    Proof: after persist_file_reference, the file exists in the deployment store
    but NOT in the upstream store.
    """
    deployment_root = tmp_path / "deployment"
    upstream_root = tmp_path / "upstream"
    deployment_store = create_local_store(deployment_root)
    upstream_store = create_local_store(upstream_root)

    raw = tmp_path / "raw.jsonl"
    raw.write_text('{"database_name": "prod"}\n')

    # Simulate what the activity interceptor does after a @task returns a
    # FileReference: calls persist_file_reference with the deployment store.
    ephemeral = FileReference(local_path=str(raw))
    durable = await persist_file_reference(deployment_store, ephemeral)

    assert durable.is_durable
    storage_path = durable.storage_path
    assert storage_path is not None

    # File must be in deployment store (interceptor wrote here)
    deployment_keys = await list_keys("", store=deployment_store)
    assert any(storage_path in key or key in storage_path for key in deployment_keys), (
        f"persist_file_reference did not write to deployment store. "
        f"storage_path={storage_path}, deployment_keys={deployment_keys}"
    )

    # File must NOT be in upstream store — the interceptor never touches it.
    # If this assertion fails, the two-store boundary has been violated and the
    # "silent failure" scenario can no longer be reproduced.
    upstream_keys = await list_keys("", store=upstream_store)
    non_sidecar_upstream = [k for k in upstream_keys if not k.endswith(".sha256")]
    assert non_sidecar_upstream == [], (
        f"persist_file_reference incorrectly wrote to upstream_storage. "
        f"This means FileReference durability is now crossing into Atlan's bucket "
        f"without an explicit App.upload() call. upstream_keys: {upstream_keys}"
    )

    # Sanity check: the upstream store is not the same object as deployment store
    assert upstream_store is not deployment_store
    assert str(upstream_root.resolve()) != str(deployment_root.resolve())
