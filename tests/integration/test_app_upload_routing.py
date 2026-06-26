"""Integration tests pinning two-store routing for App.upload() (ADR-0014).

Invariants:

(a) App.upload() dual-writes to BOTH stores when both are configured and dual-write
    is enabled (BLDX-1464 default).  The file lands in deployment first, then in
    upstream, at the **identical key**.  The returned ref reflects the upstream
    (authoritative) write.
(a2) Dual-write disabled (ATLAN_DEPLOYMENT_ARTIFACT_DUAL_WRITE=disabled) → upstream
    only (pre-BLDX-1464 behaviour).
(a3) Same-store guard (upstream is deployment object) → single write, no double.
(b) App.upload() falls back to storage when upstream_storage is None.
(b2) Both stores None → ObjectStoreNotConfiguredError raised immediately.
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

New invariants added for BLDX-1464 (artifact dual-write):

(i_fail_soft) Deployment write fails + best_effort mode → ERROR logged,
    upstream still written, run succeeds.
(i_fail_hard) Deployment write fails + required mode → upstream still written
    first, then run fails with the deployment exception.

See:
  docs/concepts/file-reference.md   (decision matrix and lifecycle)
  docs/adr/0014-two-store-storage-architecture.md   (full rationale)
"""

from __future__ import annotations

import hashlib
from typing import ClassVar

import pytest

import application_sdk.constants as _constants
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
    workflow_id: str = "local-no-temporal",
) -> _UploadApp:
    """Build an _UploadApp with the given stores wired into its context."""
    app = _UploadApp()
    app._context = AppContext(
        app_name=app._app_name,
        app_version="1",
        run_id=run_id,
        workflow_id=workflow_id,
        _storage=deployment_store,
        _upstream_storage=upstream_store,
    )
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def dual_write_enabled(monkeypatch):
    """Enable dual-write in best_effort mode (the default)."""
    monkeypatch.setattr(_constants, "DEPLOYMENT_ARTIFACT_DUAL_WRITE_ENABLED", True)
    monkeypatch.setattr(_constants, "DEPLOYMENT_ARTIFACT_DUAL_WRITE_REQUIRED", False)


@pytest.fixture()
def dual_write_disabled(monkeypatch):
    """Disable dual-write (upstream-only, pre-BLDX-1464 behaviour)."""
    monkeypatch.setattr(_constants, "DEPLOYMENT_ARTIFACT_DUAL_WRITE_ENABLED", False)
    monkeypatch.setattr(_constants, "DEPLOYMENT_ARTIFACT_DUAL_WRITE_REQUIRED", False)


@pytest.fixture()
def dual_write_required(monkeypatch):
    """Enable dual-write in required mode (a deployment failure fails the run)."""
    monkeypatch.setattr(_constants, "DEPLOYMENT_ARTIFACT_DUAL_WRITE_ENABLED", True)
    monkeypatch.setattr(_constants, "DEPLOYMENT_ARTIFACT_DUAL_WRITE_REQUIRED", True)


class _DeploymentWriteError(RuntimeError):
    """Sentinel: injected to simulate a deployment-store write failure.

    Using a distinct type lets tests pin pytest.raises() precisely and avoids
    masking unrelated errors (ObjectStoreNotConfiguredError, AttributeError, etc.).
    """


def _inject_deployment_failure(deployment_store, monkeypatch):
    """Monkeypatch storage.transfer.upload so the deployment-store call raises
    _DeploymentWriteError while the upstream call runs the real implementation.

    Works regardless of the process UID (unlike chmod 0o555 which is ignored
    by root and in most CI container runtimes).
    """
    import application_sdk.storage.transfer as _transfer_mod

    _real_upload = _transfer_mod.upload  # capture before patching

    async def _patched(*args, store, **kwargs):
        if store is deployment_store:
            raise _DeploymentWriteError("injected deployment write failure")
        return await _real_upload(*args, store=store, **kwargs)

    monkeypatch.setattr(_transfer_mod, "upload", _patched)


# ---------------------------------------------------------------------------
# (a) App.upload() dual-writes to BOTH stores when both are configured (BLDX-1464)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_dual_writes_when_both_stores_present(
    tmp_path, dual_write_enabled
):
    """App.upload() must write to BOTH stores at the identical key when both are configured.

    BLDX-1464: the deployment (customer) bucket receives a copy alongside
    the upstream (Atlan) bucket.  The returned ref reflects the upstream write;
    because keys are identical the ref is valid for reading from either store.
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

    def _non_sidecar(keys: list[str]) -> list[str]:
        return [k for k in keys if not k.endswith(".sha256")]

    # The file must be in upstream (Atlan authoritative handoff).
    upstream_keys = _non_sidecar(await list_keys("", store=upstream_store))
    assert any(storage_path in key or key in storage_path for key in upstream_keys), (
        f"App.upload() did not write to upstream_storage. "
        f"Expected key containing '{storage_path}' in upstream, found: {upstream_keys}"
    )

    # The file must ALSO be in the deployment store (customer audit copy) at the
    # IDENTICAL key — this is the BLDX-1464 guarantee.
    deployment_keys = _non_sidecar(await list_keys("", store=deployment_store))
    assert any(storage_path in key or key in storage_path for key in deployment_keys), (
        f"App.upload() did not write the dual-write copy to deployment store. "
        f"Expected key containing '{storage_path}' in deployment, found: {deployment_keys}"
    )

    # Keys must match exactly (not just overlap).
    upstream_artifact_keys = sorted(
        k for k in upstream_keys if storage_path in k or k in storage_path
    )
    deployment_artifact_keys = sorted(
        k for k in deployment_keys if storage_path in k or k in storage_path
    )
    assert upstream_artifact_keys == deployment_artifact_keys, (
        f"Artifact keys differ between stores — must be identical for audit copy. "
        f"upstream={upstream_artifact_keys}, deployment={deployment_artifact_keys}"
    )


# ---------------------------------------------------------------------------
# (a2) Dual-write disabled → upstream only
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_dual_write_disabled_writes_upstream_only(
    tmp_path, dual_write_disabled
):
    """When ATLAN_DEPLOYMENT_ARTIFACT_DUAL_WRITE=disabled, upload goes to upstream only.

    This preserves pre-BLDX-1464 behaviour for operators who opt out.
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

    # File must be in upstream.
    upstream_keys = [
        k
        for k in await list_keys("", store=upstream_store)
        if not k.endswith(".sha256")
    ]
    assert any(
        storage_path in key or key in storage_path for key in upstream_keys
    ), f"File not in upstream when dual-write disabled. upstream_keys={upstream_keys}"

    # File must NOT be in deployment store when dual-write is disabled.
    deployment_keys = [
        k
        for k in await list_keys("", store=deployment_store)
        if not k.endswith(".sha256")
    ]
    assert not any(
        storage_path in key or key in storage_path for key in deployment_keys
    ), (
        f"App.upload() wrote to deployment store when dual-write was disabled. "
        f"deployment_keys={deployment_keys}"
    )


# ---------------------------------------------------------------------------
# (a3) Same-store guard: upstream is deployment → single write only
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_same_store_guard_prevents_double_write(
    tmp_path, dual_write_enabled
):
    """When upstream_storage is the same object as storage, only one write occurs.

    The identity guard (``upstream is not deployment``) prevents the SDK from
    uploading the file twice to the same store.
    """
    store = create_local_store(tmp_path / "store")

    src = tmp_path / "artifact.jsonl"
    src.write_text('{"name": "test-entity"}\n')

    # Pass the same store object as both deployment and upstream.
    app = _make_app(store, upstream_store=store)
    result = await app.upload(
        UploadInput(local_path=str(src), tier=StorageTier.RETAINED)
    )

    assert result.ref.storage_path is not None
    all_keys = [
        k for k in await list_keys("", store=store) if not k.endswith(".sha256")
    ]
    # Exactly one copy — no duplicate keys.
    assert len(all_keys) == 1, (
        f"Expected exactly one artifact key when both stores are identical, "
        f"got: {all_keys}"
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
# (b2) Both stores None → ObjectStoreNotConfiguredError raised immediately
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_upload_raises_when_no_stores_configured(tmp_path):
    """When both storage and upstream_storage are None, ObjectStoreNotConfiguredError
    must be raised at target-list construction time, before any upload is attempted.
    """
    from application_sdk.app.base_errors import ObjectStoreNotConfiguredError

    app = _make_app(None, upstream_store=None)  # type: ignore[arg-type]

    with pytest.raises(ObjectStoreNotConfiguredError):
        await app.upload(UploadInput(local_path=str(tmp_path / "x.jsonl")))


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
    from application_sdk.storage.transfer import _put_remote_sha256

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
    from application_sdk.storage.ops import upload_file

    deployment_root = tmp_path / "deployment"
    upstream_root = tmp_path / "upstream"
    deployment_store = create_local_store(deployment_root)
    upstream_store = create_local_store(upstream_root)

    content = b'{"schema": "public"}\n'

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
    from application_sdk.storage.ops import upload_file
    from application_sdk.storage.transfer import _put_remote_sha256

    deployment_root = tmp_path / "deployment"
    upstream_root = tmp_path / "upstream"
    deployment_store = create_local_store(deployment_root)
    upstream_store = create_local_store(upstream_root)

    content = b'{"table": "orders"}\n'

    deploy_key = "artifacts/apps/test-app/workflows/run-dedup/tables.jsonl"
    src_file = tmp_path / "tables.jsonl"
    src_file.write_bytes(content)
    await upload_file(deploy_key, src_file, deployment_store, normalize=False)

    digest = hashlib.sha256(content).hexdigest()
    await _put_remote_sha256(deployment_store, deploy_key, digest)

    ref = FileReference(local_path=str(src_file), storage_path=deploy_key)
    app = _make_app(deployment_store, upstream_store=upstream_store, run_id="run-dedup")

    # First upload: file is absent locally but present in deployment store.
    src_file.unlink()
    first = await app.upload(UploadInput(ref=ref, tier=StorageTier.RETAINED))
    assert first.synced is True, "First upload should transfer the file"

    # Capture the upstream file's on-disk state before the second call so we can
    # prove that "skipped:hash_match" means zero bytes were written to the store.
    upstream_key_path = upstream_root / first.ref.storage_path
    pre_stat = upstream_key_path.stat()

    # Second upload: deployment sidecar == upstream sidecar → skip.
    second = await app.upload(UploadInput(ref=ref, tier=StorageTier.RETAINED))
    assert second.synced is False, "Second upload should be skipped (SHA-256 match)"
    assert "hash_match" in second.reason

    # Prove no bytes touched the wire: upstream file must be byte-for-byte
    # identical (same size, same mtime) after the second call.
    post_stat = upstream_key_path.stat()
    assert post_stat.st_size == pre_stat.st_size, "upstream file size changed on skip"
    assert (
        post_stat.st_mtime == pre_stat.st_mtime
    ), "upstream file mtime changed on skip"


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
# (i) Sensitive-path blocking fires even when local file is absent
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_blocks_sensitive_path_when_local_absent(tmp_path):
    """_validate_upload_path must reject sensitive local_path before the fallback runs.

    The guard comment in transfer.py claims path validation runs even when the
    path does not exist locally. This test proves it: local_path="/etc/passwd"
    (never present) with a valid deployment-store ref still raises
    UnsafeUploadPathError before any store interaction occurs.
    """
    from application_sdk.storage.errors import UnsafeUploadPathError

    deployment_store = create_local_store(tmp_path / "deployment")
    upstream_store = create_local_store(tmp_path / "upstream")
    app = _make_app(deployment_store, upstream_store=upstream_store)

    ref = FileReference(
        local_path="/etc/passwd",
        storage_path="artifacts/apps/test/safe-key.txt",
    )
    with pytest.raises(UnsafeUploadPathError):
        await app.upload(
            UploadInput(local_path="/etc/passwd", ref=ref, tier=StorageTier.RETAINED)
        )


# ---------------------------------------------------------------------------
# (i_fail_soft) Deployment write fails + best_effort → ERROR logged, upstream ok
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_dual_write_failure_best_effort(
    tmp_path, monkeypatch, dual_write_enabled
):
    """When the deployment-bucket write fails in best_effort mode, an ERROR is logged
    and the run continues.  The upstream write must still succeed and the returned
    UploadOutput must reflect the upstream copy.

    Failure is injected deterministically via monkeypatch — avoids the chmod 0o555
    approach which silently degenerates to a pass when running as root.
    """
    upstream_root = tmp_path / "upstream"
    upstream_store = create_local_store(upstream_root)
    deployment_store = create_local_store(tmp_path / "deployment")

    _inject_deployment_failure(deployment_store, monkeypatch)

    src = tmp_path / "artifact.jsonl"
    src.write_text('{"name": "test-entity"}\n')

    app = _make_app(deployment_store, upstream_store=upstream_store)

    # Upload must succeed (non-fatal deployment failure in best_effort mode).
    result = await app.upload(
        UploadInput(local_path=str(src), tier=StorageTier.RETAINED)
    )

    assert result.ref.storage_path is not None

    upstream_keys = [
        k
        for k in await list_keys("", store=upstream_store)
        if not k.endswith(".sha256")
    ]
    assert upstream_keys, (
        f"Upstream write did not succeed after best-effort deployment failure. "
        f"upstream_keys={upstream_keys}"
    )


# ---------------------------------------------------------------------------
# (i_fail_hard) Deployment write fails + required → upstream writes, then raises
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_dual_write_failure_required_raises_after_upstream(
    tmp_path, monkeypatch, dual_write_required
):
    """When ATLAN_DEPLOYMENT_ARTIFACT_DUAL_WRITE=required and the deployment
    write fails, the upstream write must still complete before the exception is
    raised — ensuring a copy always lands somewhere.
    """
    upstream_root = tmp_path / "upstream"
    upstream_store = create_local_store(upstream_root)
    deployment_store = create_local_store(tmp_path / "deployment")

    _inject_deployment_failure(deployment_store, monkeypatch)

    src = tmp_path / "artifact.jsonl"
    src.write_text('{"name": "test-entity"}\n')

    app = _make_app(deployment_store, upstream_store=upstream_store)

    with pytest.raises(_DeploymentWriteError):
        await app.upload(UploadInput(local_path=str(src), tier=StorageTier.RETAINED))

    # The upstream write must have completed before the raise — a copy exists.
    upstream_keys = [
        k
        for k in await list_keys("", store=upstream_store)
        if not k.endswith(".sha256")
    ]
    assert upstream_keys, (
        f"Upstream write did not complete before the required-mode exception. "
        f"upstream_keys={upstream_keys}"
    )


# ---------------------------------------------------------------------------
# Regression: upload prefix uses real workflow_id, not "local-no-temporal"
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_upload_path_embeds_workflow_id_from_context(tmp_path):
    """App.upload() run_prefix must embed context.workflow_id, not the sentinel.

    Regression guard: workflow_id was never set on the activity-side AppContext,
    so uploads landed under the 'local-no-temporal' sentinel in production.
    The fix threads workflow_id through TaskContext so both construction sites
    read from one transport (TaskContext.workflow_id).
    """
    store = create_local_store(tmp_path / "store")
    src = tmp_path / "artifact.jsonl"
    src.write_text('{"name": "test"}\n')

    real_workflow_id = "wf-temporal-abc123"
    app = _make_app(store, workflow_id=real_workflow_id, run_id="run-wfid")

    result = await app.upload(
        UploadInput(local_path=str(src), tier=StorageTier.RETAINED)
    )

    storage_path = result.ref.storage_path
    assert storage_path is not None
    assert real_workflow_id in storage_path, (
        f"Expected workflow_id '{real_workflow_id}' in storage path '{storage_path}'. "
        f"workflow_id must flow from TaskContext.workflow_id into app_context."
    )
    assert "local-no-temporal" not in storage_path
