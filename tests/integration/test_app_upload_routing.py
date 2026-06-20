"""Integration tests pinning two-store routing for App.upload() (ADR-0014).

Invariants:

(a) App.upload() dual-writes to BOTH stores when both are configured and mirror
    is enabled (BLDX-1464 default).  The file lands in deployment first, then in
    upstream, at the **identical key**.  The returned ref reflects the upstream
    (authoritative) write.
(a2) Mirror disabled (ATLAN_ENABLE_DEPLOYMENT_ARTIFACT_MIRROR=false) → upstream
    only (pre-BLDX-1464 behaviour).
(a3) Same-store guard (upstream is deployment object) → single write, no double.
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

New invariants added for BLDX-1464 (artifact mirror):

(i_fail_soft) Deployment write fails + best-effort (default) → WARNING logged,
    upstream still written, run succeeds.
(i_fail_hard) Deployment write fails + DEPLOYMENT_ARTIFACT_MIRROR_REQUIRED=true
    → upstream still written first, then run fails.

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
# (a) App.upload() dual-writes to BOTH stores when both are configured (BLDX-1464)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_dual_writes_when_both_stores_present(tmp_path, monkeypatch):
    """App.upload() must write to BOTH stores at the identical key when both are configured.

    BLDX-1464: the deployment (customer) bucket receives a mirror copy alongside
    the upstream (Atlan) bucket.  The returned ref reflects the upstream write;
    because keys are identical the ref is valid for reading from either store.
    """
    monkeypatch.setenv("ATLAN_ENABLE_DEPLOYMENT_ARTIFACT_MIRROR", "true")
    # Re-read the constant so the env-var is picked up for this process.
    import importlib

    import application_sdk.constants as _c

    importlib.reload(_c)

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
        f"App.upload() did not write the mirror copy to deployment store. "
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
# (a2) Mirror disabled → upstream only
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_mirror_disabled_writes_upstream_only(tmp_path, monkeypatch):
    """When ATLAN_ENABLE_DEPLOYMENT_ARTIFACT_MIRROR=false, upload goes to upstream only.

    This preserves pre-BLDX-1464 behaviour for operators who opt out.
    """
    monkeypatch.setenv("ATLAN_ENABLE_DEPLOYMENT_ARTIFACT_MIRROR", "false")
    import importlib

    import application_sdk.constants as _c

    importlib.reload(_c)

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
    ), f"File not in upstream when mirror disabled. upstream_keys={upstream_keys}"

    # File must NOT be in deployment store when mirror is disabled.
    deployment_keys = [
        k
        for k in await list_keys("", store=deployment_store)
        if not k.endswith(".sha256")
    ]
    assert not any(
        storage_path in key or key in storage_path for key in deployment_keys
    ), (
        f"App.upload() wrote to deployment store when mirror was disabled. "
        f"deployment_keys={deployment_keys}"
    )


# ---------------------------------------------------------------------------
# (a3) Same-store guard: upstream is deployment → single write only
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_same_store_guard_prevents_double_write(tmp_path, monkeypatch):
    """When upstream_storage is the same object as storage, only one write occurs.

    The identity guard (``upstream is not deployment``) prevents the SDK from
    uploading the file twice to the same store.
    """
    monkeypatch.setenv("ATLAN_ENABLE_DEPLOYMENT_ARTIFACT_MIRROR", "true")
    import importlib

    import application_sdk.constants as _c

    importlib.reload(_c)

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
# (i_fail_soft) Deployment mirror fails + best-effort → WARNING, upstream succeeds
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_mirror_failure_best_effort(tmp_path, monkeypatch):
    """When the deployment-bucket mirror write fails, a WARNING is logged and the
    run continues.  The upstream write must still succeed and the returned
    UploadOutput must reflect the upstream copy.

    Invariant: ATLAN_DEPLOYMENT_ARTIFACT_MIRROR_REQUIRED=false (default)
    → a failed deployment write is non-fatal.
    """
    import importlib

    import application_sdk.constants as _c

    monkeypatch.setenv("ATLAN_ENABLE_DEPLOYMENT_ARTIFACT_MIRROR", "true")
    monkeypatch.setenv("ATLAN_DEPLOYMENT_ARTIFACT_MIRROR_REQUIRED", "false")
    importlib.reload(_c)

    upstream_root = tmp_path / "upstream"
    upstream_store = create_local_store(upstream_root)

    # Use a read-only deployment store path to force a write failure.
    import os

    readonly_root = tmp_path / "readonly"
    readonly_root.mkdir(mode=0o555)
    deployment_store = create_local_store(readonly_root)

    src = tmp_path / "artifact.jsonl"
    src.write_text('{"name": "test-entity"}\n')

    app = _make_app(deployment_store, upstream_store=upstream_store)

    # The upload must succeed (non-fatal deployment failure).
    result = await app.upload(
        UploadInput(local_path=str(src), tier=StorageTier.RETAINED)
    )

    # Restore permissions so tmp_path cleanup can remove the directory.
    readonly_root.chmod(0o755)

    assert result.ref.storage_path is not None

    # The upstream copy must exist.
    upstream_keys = [
        k
        for k in await list_keys("", store=upstream_store)
        if not k.endswith(".sha256")
    ]
    assert upstream_keys, (
        f"Upstream write did not succeed after best-effort deployment failure. "
        f"upstream_keys={upstream_keys}"
    )

    # Restore so cleanup can proceed.
    os.chmod(readonly_root, 0o755)


# ---------------------------------------------------------------------------
# (i_fail_hard) Deployment mirror fails + required → upstream writes, then raises
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_app_upload_mirror_failure_required_raises_after_upstream(
    tmp_path, monkeypatch
):
    """When ATLAN_DEPLOYMENT_ARTIFACT_MIRROR_REQUIRED=true and the deployment
    write fails, the upstream write must still complete before the exception is
    raised — ensuring a copy always lands somewhere.
    """
    import importlib

    import application_sdk.constants as _c

    monkeypatch.setenv("ATLAN_ENABLE_DEPLOYMENT_ARTIFACT_MIRROR", "true")
    monkeypatch.setenv("ATLAN_DEPLOYMENT_ARTIFACT_MIRROR_REQUIRED", "true")
    importlib.reload(_c)

    upstream_root = tmp_path / "upstream"
    upstream_store = create_local_store(upstream_root)

    readonly_root = tmp_path / "readonly"
    readonly_root.mkdir(mode=0o555)
    deployment_store = create_local_store(readonly_root)

    src = tmp_path / "artifact.jsonl"
    src.write_text('{"name": "test-entity"}\n')

    app = _make_app(deployment_store, upstream_store=upstream_store)

    with pytest.raises(Exception):
        await app.upload(UploadInput(local_path=str(src), tier=StorageTier.RETAINED))

    # Restore permissions before asserting so cleanup works even on failure.
    readonly_root.chmod(0o755)

    # The upstream write must have completed before the raise — a copy exists.
    upstream_keys = [
        k
        for k in await list_keys("", store=upstream_store)
        if not k.endswith(".sha256")
    ]
    assert upstream_keys, (
        f"Upstream write did not complete before the required-mirror exception. "
        f"upstream_keys={upstream_keys}"
    )
