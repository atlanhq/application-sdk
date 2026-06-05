"""Integration tests pinning two-store routing for App.upload() (ADR-0014).

Three existing invariants:

(a) App.upload() routes to upstream_storage when an upstream store is wired.
(b) App.upload() falls back to storage when upstream_storage is None.
(c) persist_file_reference always writes to the deployment store, never upstream.

New invariants added for BLDX-1377 (FileReference / cross-pod safety):

(d) App.upload(local_path=...) with files absent falls back to the deployment
    store and uploads to upstream — the cross-pod SDR fix.
(e) App.upload(ref=file_ref) uses ref.storage_path as the deployment-store
    source key.
(f) SHA-256 match between deployment store and upstream causes skip
    (no bytes transferred — idempotency).
(g) normalize_key(local_path) == "" guard: no fallback attempted, existing
    StorageError raised (prevents accidental full-store queries).
(h) UploadInput.ref is symmetric with DownloadInput.ref.

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

    # The file must be findable in upstream (excluding .sha256 sidecars)
    upstream_keys = await list_keys("", store=upstream_store)
    non_sidecar_upstream = [k for k in upstream_keys if not k.endswith(".sha256")]
    assert any(
        storage_path in key or key in storage_path for key in non_sidecar_upstream
    ), (
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

    # The file must be in the deployment store (fallback, excluding .sha256 sidecars)
    deployment_keys = await list_keys("", store=deployment_store)
    non_sidecar_deployment = [k for k in deployment_keys if not k.endswith(".sha256")]
    assert any(
        storage_path in key or key in storage_path for key in non_sidecar_deployment
    ), (
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


# ---------------------------------------------------------------------------
# (d) App.upload(local_path=...) with files absent → deployment-store fallback
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_falls_back_to_deployment_store_when_local_absent(tmp_path):
    """App.upload(local_path=...) must fall back to the deployment store when the
    local file is absent.

    Models the cross-pod SDR scenario: the extract activity ran on pod A
    (which wrote the file to the deployment store) but the upload activity
    was scheduled on pod B (which never wrote the file locally).

    The SDK auto-derives the deployment-store key from normalize_key(local_path),
    so existing call sites get the fallback for free on the next SDK bump.
    """
    from application_sdk.storage.ops import normalize_key, upload_file

    deployment_root = tmp_path / "deployment"
    upstream_root = tmp_path / "upstream"
    deployment_store = create_local_store(deployment_root)
    upstream_store = create_local_store(upstream_root)

    local_src = tmp_path / "artifact.jsonl"
    content = b'{"name": "test-entity"}\n'
    local_src.write_bytes(content)

    # Compute the deployment-store key that normalize_key would derive from local_path
    # (mirrors what the task interceptor / upload_file writes under the hood).
    deploy_key = normalize_key(str(local_src))
    await upload_file(deploy_key, local_src, deployment_store, normalize=False)

    # Write the SHA-256 sidecar that the interceptor would have created.
    import hashlib

    from application_sdk.storage.transfer import _put_remote_sha256

    digest = hashlib.sha256(content).hexdigest()
    await _put_remote_sha256(deployment_store, deploy_key, digest)

    # Pod B: local_path no longer exists.
    local_src.unlink()
    assert not local_src.exists()

    app = _make_app(
        deployment_store, upstream_store=upstream_store, run_id="run-fallback"
    )
    # Existing call-site style — no ref needed.
    result = await app.upload(
        UploadInput(local_path=str(local_src), tier=StorageTier.RETAINED)
    )

    assert result.ref.is_durable
    assert result.synced is True
    assert result.reason == "uploaded"

    # File must be in upstream (SDR target).
    upstream_keys = await list_keys("", store=upstream_store)
    non_sidecar = [k for k in upstream_keys if not k.endswith(".sha256")]
    assert non_sidecar, (
        f"No files found in upstream after local-absent fallback. "
        f"upstream_keys={upstream_keys}"
    )


# ---------------------------------------------------------------------------
# (e) App.upload(ref=file_ref) uses ref.storage_path as the source key
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_ref_uses_storage_path_as_source_key(tmp_path):
    """App.upload(ref=...) must use ref.storage_path as the deployment-store key.

    Verifies that passing an explicit FileReference (e.g. from a task output)
    correctly routes the fallback to ref.storage_path in the deployment store.
    """
    deployment_root = tmp_path / "deployment"
    upstream_root = tmp_path / "upstream"
    deployment_store = create_local_store(deployment_root)
    upstream_store = create_local_store(upstream_root)

    content = b'{"schema": "public"}\n'
    from application_sdk.storage.ops import upload_file

    deploy_key = "artifacts/apps/test-app/workflows/run-ref/schema.jsonl"
    src_file = tmp_path / "schema.jsonl"
    src_file.write_bytes(content)
    await upload_file(deploy_key, src_file, deployment_store, normalize=False)
    src_file.unlink()  # simulate writer-deleted / cross-pod

    ref = FileReference(local_path=str(src_file), storage_path=deploy_key)

    app = _make_app(deployment_store, upstream_store=upstream_store, run_id="run-ref")
    result = await app.upload(UploadInput(ref=ref, tier=StorageTier.RETAINED))

    assert result.ref.is_durable
    assert result.synced is True

    upstream_keys = await list_keys("", store=upstream_store)
    non_sidecar = [k for k in upstream_keys if not k.endswith(".sha256")]
    assert non_sidecar, (
        f"File did not land in upstream after ref-based upload. "
        f"upstream_keys={upstream_keys}"
    )


# ---------------------------------------------------------------------------
# (f) SHA-256 match between deployment store and upstream causes skip
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_cross_store_sha256_match_causes_skip(tmp_path):
    """A SHA-256 match between deployment and upstream stores must cause a skip.

    On the second App.upload() call (or after a Temporal replay), if the
    deployment-store sidecar already matches the upstream sidecar the SDK
    must return synced=False without transferring any bytes.
    """
    deployment_root = tmp_path / "deployment"
    upstream_root = tmp_path / "upstream"
    deployment_store = create_local_store(deployment_root)
    upstream_store = create_local_store(upstream_root)

    content = b'{"table": "orders"}\n'
    from application_sdk.storage.ops import upload_file

    deploy_key = "artifacts/apps/test-app/workflows/run-dedup/tables.jsonl"
    src_file = tmp_path / "tables.jsonl"
    src_file.write_bytes(content)
    await upload_file(deploy_key, src_file, deployment_store, normalize=False)
    import hashlib

    from application_sdk.storage.transfer import _put_remote_sha256

    digest = hashlib.sha256(content).hexdigest()
    await _put_remote_sha256(deployment_store, deploy_key, digest)

    ref = FileReference(local_path=str(src_file), storage_path=deploy_key)
    app = _make_app(deployment_store, upstream_store=upstream_store, run_id="run-dedup")

    # First upload: file is absent locally but present in deployment store.
    src_file.unlink()
    first = await app.upload(UploadInput(ref=ref, tier=StorageTier.RETAINED))
    assert first.synced is True, "First upload should transfer the file"

    # Second upload: deployment sidecar == upstream sidecar → skip.
    second = await app.upload(UploadInput(ref=ref, tier=StorageTier.RETAINED))
    assert second.synced is False, "Second upload should be skipped (SHA-256 match)"
    assert "hash_match" in second.reason


# ---------------------------------------------------------------------------
# (g) normalize_key == "" guard: no fallback, StorageError raised
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_empty_local_path_raises_storage_error(tmp_path):
    """When local_path is empty and no ref is set, StorageError must be raised.

    normalize_key("") returns "" which triggers the guard that prevents
    creating source_ref, so the fallback is never attempted.
    """
    from application_sdk.storage.errors import StorageError

    deployment_store = create_local_store(tmp_path / "deployment")
    upstream_store = create_local_store(tmp_path / "upstream")

    app = _make_app(deployment_store, upstream_store=upstream_store)

    with pytest.raises(StorageError):
        await app.upload(UploadInput(local_path="", tier=StorageTier.RETAINED))


# ---------------------------------------------------------------------------
# (h) UploadInput.ref field is symmetric with DownloadInput.ref
# ---------------------------------------------------------------------------


def test_upload_input_has_ref_field():
    """UploadInput must expose a ref: FileReference | None field."""
    # Field present with default None
    ui = UploadInput()
    assert ui.ref is None

    # Field accepts a FileReference
    ref = FileReference(local_path="/tmp/x.jsonl", storage_path="artifacts/x.jsonl")
    ui_with_ref = UploadInput(ref=ref)
    assert ui_with_ref.ref == ref

    from application_sdk.contracts.storage import DownloadInput

    # Symmetric with DownloadInput
    di = DownloadInput()
    assert di.ref is None
