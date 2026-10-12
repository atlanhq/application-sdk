"""Directory FileReference hand-off via an explicit manifest (FND-3762).

A directory ref used to be a prefix plus a ``file_count``, and every consumer
re-listed the prefix and trusted the listing. These tests pin the two halves
of the fix:

* **Manifest path**: the producer writes the exact file set (path, sha256,
  size) last, outside the prefix; the consumer downloads exactly that set and
  never lists, so leftovers under a reused pinned prefix are inert and a late
  overwrite of a listed key fails its digest check.
* **Fallback path** (refs with no manifest): the listing is checked against
  ``file_count`` as a lower bound, so a short or empty listing fails
  retryably instead of handing the consumer fewer files than declared.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.storage._manifest import MANIFEST_SUFFIX, DirectoryManifest
from application_sdk.storage.errors import StorageError, StorageIntegrityError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import _get_bytes, _put
from application_sdk.storage.reference import (
    materialize_file_reference,
    persist_file_reference,
)

_FILES = {"a.json": b'{"a": 1}\n', "sub/b.json": b'{"b": 2}\n', "c.json": b"{}\n"}
_PIN = "artifacts/apps/x/workflows/wf/run/transformed/table/"


@pytest.fixture
def store():
    return create_memory_store()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_tree(root: Path, files: dict[str, bytes] = _FILES) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


async def _persist(store, root: Path, **ref_kwargs) -> FileReference:
    return await persist_file_reference(
        store, FileReference(local_path=str(root), **ref_kwargs)
    )


def _cross_pod(durable: FileReference, dest: Path, **overrides) -> FileReference:
    """The ref as a consumer on another pod receives it: same keys, new local dir."""
    return durable.model_copy(update={"local_path": str(dest), **overrides})


def _local_files(root: Path) -> set[str]:
    return {
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith(".sha256")
    }


class TestPersistWritesManifest:
    async def test_manifest_lists_every_file_with_digest_and_size(
        self, store, tmp_path
    ) -> None:
        _write_tree(tmp_path)
        durable = await _persist(store, tmp_path)

        assert durable.manifest_path is not None
        raw = await _get_bytes(durable.manifest_path, store, normalize=False)
        assert raw is not None
        manifest = DirectoryManifest.model_validate_json(raw)
        assert {(e.path, e.sha256, e.size_bytes) for e in manifest.files} == {
            (rel, _sha(data), len(data)) for rel, data in _FILES.items()
        }

    async def test_manifest_is_outside_the_prefix(self, store, tmp_path) -> None:
        """An older SDK lists the prefix; a manifest inside it would be data."""
        _write_tree(tmp_path)
        durable = await _persist(store, tmp_path, storage_path=_PIN)

        assert durable.manifest_path is not None
        assert not durable.manifest_path.startswith(_PIN)
        # Nor beside the pinned prefix, inside the parent a publish step walks.
        parent = _PIN.rstrip("/").rsplit("/", 1)[0] + "/"
        assert not durable.manifest_path.startswith(parent)

    async def test_manifest_key_never_matches_a_json_glob(
        self, store, tmp_path
    ) -> None:
        """Publish converters and apps glob run prefixes with ``**/*.json``.

        Regression: a ``.manifest.json`` key under ``{run}/file_refs/`` was
        parsed as asset records by a walk of the run prefix.
        """
        from fnmatch import fnmatch

        _write_tree(tmp_path)
        durable = await persist_file_reference(
            store,
            FileReference(local_path=str(tmp_path), tier=StorageTier.RETAINED),
            output_path="artifacts/apps/x/workflows/wf/run",
        )

        assert durable.manifest_path is not None
        assert not fnmatch(durable.manifest_path, "*.json")

    async def test_manifest_is_written_after_every_data_object(
        self, store, tmp_path
    ) -> None:
        """A manifest's presence must prove the hand-off is complete."""
        from application_sdk.storage import ops

        _write_tree(tmp_path)
        order: list[str] = []
        real_upload, real_put = ops.upload_file, ops._put

        async def _upload(key, *a, **k):
            order.append(key)
            return await real_upload(key, *a, **k)

        async def _put_spy(key, *a, **k):
            order.append(key)
            return await real_put(key, *a, **k)

        from unittest.mock import patch

        with (
            patch.object(ops, "upload_file", _upload),
            patch.object(ops, "_put", _put_spy),
        ):
            durable = await _persist(store, tmp_path)

        # Sidecar puts interleave with the uploads; only the manifest's
        # position matters.
        assert order[-1] == durable.manifest_path
        assert {durable.storage_path + rel for rel in _FILES} <= set(order[:-1])

    async def test_failed_upload_writes_no_manifest(self, store, tmp_path) -> None:
        """(a, producer side) A partial upload never produces a manifest."""
        from unittest.mock import patch

        from application_sdk.storage import ops

        _write_tree(tmp_path)
        real_upload = ops.upload_file
        written: list[str] = []

        async def _flaky(key, *a, **k):
            if key.endswith("c.json"):
                raise StorageError("boom", key=key)
            return await real_upload(key, *a, **k)

        async def _put_spy(key, *a, **k):
            written.append(key)

        with (
            patch.object(ops, "upload_file", _flaky),
            patch.object(ops, "_put", _put_spy),
            pytest.raises(StorageError),
        ):
            await _persist(store, tmp_path)

        assert not any(k.endswith(MANIFEST_SUFFIX) for k in written)


class TestPinnedDirectoryStoragePath:
    async def test_pinned_storage_path_is_honoured(self, store, tmp_path) -> None:
        _write_tree(tmp_path)
        durable = await _persist(store, tmp_path, storage_path=_PIN)

        assert durable.storage_path == _PIN
        for rel, data in _FILES.items():
            assert await _get_bytes(_PIN + rel, store, normalize=False) == data

    async def test_pin_without_trailing_slash_is_normalised(
        self, store, tmp_path
    ) -> None:
        _write_tree(tmp_path)
        durable = await _persist(store, tmp_path, storage_path=_PIN.rstrip("/"))

        assert durable.storage_path == _PIN

    async def test_explicit_key_wins_over_pin(self, store, tmp_path) -> None:
        _write_tree(tmp_path)
        durable = await persist_file_reference(
            store,
            FileReference(local_path=str(tmp_path), storage_path=_PIN),
            key="other/prefix",
        )

        assert durable.storage_path == "other/prefix/"

    async def test_unpinned_still_generates_a_unique_prefix(
        self, store, tmp_path
    ) -> None:
        _write_tree(tmp_path)
        first = await _persist(store, tmp_path)
        second = await _persist(store, tmp_path)

        assert first.storage_path != second.storage_path
        assert "/file_refs/" in f"/{first.storage_path}"

    _RUN = "artifacts/apps/x/workflows/wf/run"

    @pytest.mark.parametrize("tier", [StorageTier.TRANSIENT, StorageTier.RETAINED])
    async def test_run_scoped_pin_outside_run_prefix_is_rejected(
        self, store, tmp_path, tier
    ) -> None:
        """Two runs pinning ``shared/results/`` would delete each other's data."""
        from application_sdk.storage.errors import StorageConfigError

        _write_tree(tmp_path)
        with pytest.raises(StorageConfigError):
            await persist_file_reference(
                store,
                FileReference(
                    local_path=str(tmp_path), storage_path="shared/results/", tier=tier
                ),
                output_path=self._RUN,
            )

        # Rejected before anything is written.
        assert await _get_bytes("shared/results/a.json", store, normalize=False) is None

    async def test_sibling_of_run_prefix_is_not_inside_it(
        self, store, tmp_path
    ) -> None:
        from application_sdk.storage.errors import StorageConfigError

        _write_tree(tmp_path)
        with pytest.raises(StorageConfigError):
            await persist_file_reference(
                store,
                FileReference(
                    local_path=str(tmp_path), storage_path=self._RUN + "-2/t/"
                ),
                output_path=self._RUN,
            )

    @pytest.mark.parametrize("tier", [StorageTier.TRANSIENT, StorageTier.RETAINED])
    async def test_pin_under_run_prefix_is_honoured(
        self, store, tmp_path, tier
    ) -> None:
        _write_tree(tmp_path)
        pin = f"{self._RUN}/transformed/table/"
        durable = await persist_file_reference(
            store,
            FileReference(local_path=str(tmp_path), storage_path=pin, tier=tier),
            output_path=self._RUN,
        )

        assert durable.storage_path == pin
        assert durable.manifest_path is not None
        assert durable.manifest_path.startswith(f"{self._RUN}/file_refs/")

    async def test_persistent_pin_may_sit_outside_run_prefix(
        self, store, tmp_path
    ) -> None:
        """PERSISTENT data is shared across runs by design and never cleaned."""
        _write_tree(tmp_path)
        pin = "persistent-artifacts/apps/x/connection-cache/"
        durable = await persist_file_reference(
            store,
            FileReference(
                local_path=str(tmp_path), storage_path=pin, tier=StorageTier.PERSISTENT
            ),
            output_path=self._RUN,
        )

        assert durable.storage_path == pin


class TestMaterializeWithManifest:
    async def test_round_trip_cross_pod(self, store, tmp_path) -> None:
        producer = tmp_path / "producer"
        _write_tree(producer)
        durable = await _persist(store, producer, storage_path=_PIN)

        result = await materialize_file_reference(
            store, _cross_pod(durable, tmp_path / "consumer")
        )

        consumer = Path(result.local_path)
        assert _local_files(consumer) == set(_FILES)
        for rel, data in _FILES.items():
            assert (consumer / rel).read_bytes() == data
        assert result.manifest_path == durable.manifest_path

    async def test_leftovers_under_reused_pinned_prefix_are_not_materialised(
        self, store, tmp_path
    ) -> None:
        """(b) Attempt 1 left objects with different names under the pin."""
        await _put(_PIN + "part-old-7.json", b"stale", store, normalize=False)
        await _put(_PIN + "zombie.json", b"late", store, normalize=False)
        producer = tmp_path / "producer"
        _write_tree(producer)
        durable = await _persist(store, producer, storage_path=_PIN)

        result = await materialize_file_reference(
            store, _cross_pod(durable, tmp_path / "consumer")
        )

        assert _local_files(Path(result.local_path)) == set(_FILES)

    async def test_materialize_with_manifest_does_not_list(
        self, store, tmp_path
    ) -> None:
        from unittest.mock import patch

        from application_sdk.storage import batch

        producer = tmp_path / "producer"
        _write_tree(producer)
        durable = await _persist(store, producer)

        with patch.object(
            batch, "list_data_objects", side_effect=AssertionError("listed")
        ):
            await materialize_file_reference(
                store, _cross_pod(durable, tmp_path / "consumer")
            )

    async def test_zombie_overwrite_of_a_manifest_key_fails_the_hash_check(
        self, store, tmp_path
    ) -> None:
        """(c) Same size, different bytes: only the digest can tell."""
        producer = tmp_path / "producer"
        _write_tree(producer)
        durable = await _persist(store, producer, storage_path=_PIN)
        original = _FILES["a.json"]
        forged = b'{"z": 9}\n'
        assert len(forged) == len(original)
        await _put(_PIN + "a.json", forged, store, normalize=False)
        # The zombie also rewrites the per-object sidecar, so the listing-era
        # check would agree with it; the manifest's digest does not.
        await _put(
            _PIN + "a.json.sha256", _sha(forged).encode(), store, normalize=False
        )

        with pytest.raises(StorageIntegrityError):
            await materialize_file_reference(
                store, _cross_pod(durable, tmp_path / "consumer")
            )

    async def test_hash_check_holds_with_verification_switched_off(
        self, store, tmp_path, monkeypatch
    ) -> None:
        """The deployment kill-switch must not disable the manifest check."""
        import application_sdk.constants as constants

        monkeypatch.setattr(constants, "STORAGE_VERIFY_TRANSFERS", False)
        producer = tmp_path / "producer"
        _write_tree(producer)
        durable = await _persist(store, producer, storage_path=_PIN)
        await _put(_PIN + "a.json", b'{"z": 9}\n', store, normalize=False)

        with pytest.raises(StorageIntegrityError):
            await materialize_file_reference(
                store, _cross_pod(durable, tmp_path / "consumer")
            )

    async def test_stale_local_file_with_matching_own_sidecar_is_replaced(
        self, store, tmp_path
    ) -> None:
        """A file from an earlier pass agrees with its own sidecar, not the manifest."""
        producer = tmp_path / "producer"
        _write_tree(producer)
        durable = await _persist(store, producer)
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        (consumer / "a.json").write_bytes(b"old pass")
        (consumer / "a.json.sha256").write_text(_sha(b"old pass"))

        await materialize_file_reference(store, _cross_pod(durable, consumer))

        assert (consumer / "a.json").read_bytes() == _FILES["a.json"]

    async def test_same_pod_materialize_downloads_nothing(
        self, store, tmp_path
    ) -> None:
        from unittest.mock import patch

        from application_sdk.storage import ops

        _write_tree(tmp_path)
        durable = await _persist(store, tmp_path)

        with patch.object(
            ops, "download_file_chunked", side_effect=AssertionError("fetched")
        ):
            await materialize_file_reference(store, durable)

    async def test_missing_manifest_fails_retryably_without_listing(
        self, store, tmp_path
    ) -> None:
        """(a, consumer side) Data partly landed; the manifest never did."""
        _write_tree(tmp_path / "producer")
        durable = await _persist(store, tmp_path / "producer")
        ref = _cross_pod(
            durable, tmp_path / "consumer", manifest_path="file_refs/gone.sdk-manifest"
        )

        with pytest.raises(StorageError) as exc:
            await materialize_file_reference(store, ref)

        assert not isinstance(exc.value, StorageIntegrityError)
        assert exc.value.effective_retryable is True

    async def test_manifest_shorter_than_file_count_fails(
        self, store, tmp_path
    ) -> None:
        _write_tree(tmp_path / "producer")
        durable = await _persist(store, tmp_path / "producer")
        ref = _cross_pod(durable, tmp_path / "consumer", file_count=len(_FILES) + 1)

        with pytest.raises(StorageIntegrityError):
            await materialize_file_reference(store, ref)

    async def test_malformed_manifest_fails_non_retryably(
        self, store, tmp_path
    ) -> None:
        _write_tree(tmp_path / "producer")
        durable = await _persist(store, tmp_path / "producer")
        assert durable.manifest_path is not None
        await _put(durable.manifest_path, b"not json", store, normalize=False)

        with pytest.raises(StorageIntegrityError) as exc:
            await materialize_file_reference(
                store, _cross_pod(durable, tmp_path / "consumer")
            )
        assert exc.value.effective_retryable is False


class TestFallbackWithoutManifest:
    async def test_old_shape_ref_still_materialises(self, store, tmp_path) -> None:
        """(d) A ref from an SDK that predates manifests lists the prefix."""
        _write_tree(tmp_path / "producer")
        durable = await _persist(store, tmp_path / "producer")
        old_shape = _cross_pod(durable, tmp_path / "consumer", manifest_path=None)

        result = await materialize_file_reference(store, old_shape)

        assert _local_files(Path(result.local_path)) == set(_FILES)

    async def test_old_sdk_decodes_a_new_ref(self) -> None:
        """An older SDK's FileReference ignores the unknown field (extra=ignore)."""
        from pydantic import BaseModel

        class _OldFileReference(BaseModel, frozen=True):
            local_path: str | None = None
            storage_path: str | None = None
            is_durable: bool = False
            file_count: int = 1

        wire = FileReference(
            storage_path=_PIN, is_durable=True, manifest_path="m.sdk-manifest"
        ).model_dump_json()

        assert _OldFileReference.model_validate_json(wire).storage_path == _PIN

    async def test_partial_listing_fails_retryably(self, store, tmp_path) -> None:
        """(a) Two of three declared objects visible."""
        for rel in ("a.json", "c.json"):
            await _put(_PIN + rel, _FILES[rel], store, normalize=False)
        ref = FileReference(
            local_path=str(tmp_path / "consumer"),
            storage_path=_PIN,
            is_durable=True,
            file_count=3,
        )

        with pytest.raises(StorageError) as exc:
            await materialize_file_reference(store, ref)

        assert exc.value.effective_retryable is True
        assert "2 are visible" in str(exc.value)

    async def test_over_count_warns_but_materialises(self, store, tmp_path) -> None:
        """Hand-built refs keep the default file_count=1 whatever they hold."""
        from unittest.mock import patch

        from application_sdk.storage import reference

        for rel, data in _FILES.items():
            await _put(_PIN + rel, data, store, normalize=False)
        ref = FileReference(
            local_path=str(tmp_path / "consumer"), storage_path=_PIN, is_durable=True
        )

        with patch.object(reference.logger, "warning") as warning:
            result = await materialize_file_reference(store, ref)

        assert _local_files(Path(result.local_path)) == set(_FILES)
        warning.assert_any_call(
            "file_ref.materialize.count_mismatch",
            storage_path=_PIN,
            file_count=1,
            files_listed=len(_FILES),
        )

    async def test_empty_listing_for_declared_files_fails_retryably(
        self, store, tmp_path
    ) -> None:
        """(e) GCS under conditional IAM lists empty without list permission."""
        dest = tmp_path / "consumer"
        dest.mkdir()
        ref = FileReference(
            local_path=str(dest),
            storage_path=_PIN,
            is_durable=True,
            file_count=4,
        )

        with pytest.raises(StorageError) as exc:
            await materialize_file_reference(store, ref)

        assert exc.value.effective_retryable is True

    async def test_empty_listing_for_default_file_count_fails(
        self, store, tmp_path
    ) -> None:
        """The default ``file_count=1`` declares a file, so empty is a shortfall."""
        dest = tmp_path / "consumer"
        dest.mkdir()
        ref = FileReference(local_path=str(dest), storage_path=_PIN, is_durable=True)

        with pytest.raises(StorageError):
            await materialize_file_reference(store, ref)
