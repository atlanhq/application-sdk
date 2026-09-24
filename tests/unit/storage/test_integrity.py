"""Transfer integrity validation (FND-306).

Covers the digest helpers in ``storage.integrity`` and the validations they
back in the three transfer primitives every SDK path funnels through —
``ops.upload_file``, ``ops.download_file``, ``chunked.download_file_chunked``
— plus proof that the higher-level paths (``transfer``, ``batch``) inherit
them rather than each implementing their own.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import obstore
import orjson
import pytest

from application_sdk.storage import batch, integrity, transfer
from application_sdk.storage.chunked import _part_path, _transfer_state_path
from application_sdk.storage.errors import StorageError, StorageIntegrityError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import (
    _get_bytes,
    _put,
    download_file,
    download_file_chunked,
    get_file_meta,
    upload_file,
)


@pytest.fixture
def store():
    return create_memory_store()


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Artifact:
    """A file that exists both locally and in the store, with its digest."""

    key: str
    local_path: Path
    content: bytes

    @property
    def digest(self) -> str:
        return _hash_bytes(self.content)


async def _seed(store, tmp_path: Path, key: str, content: bytes) -> Artifact:
    """Upload *content* at *key* through the real upload path (sidecar included)."""
    local = tmp_path / Path(key).name
    local.write_bytes(content)
    await upload_file(key, local, store, normalize=False)
    return Artifact(key=key, local_path=local, content=content)


async def _corrupt_object(store, key: str, content: bytes) -> None:
    """Replace the object at *key* without touching its sidecar.

    This is the shape of the production failure: the producer died part-way
    through writing, so the bytes in the store are a truncated prefix of what
    the recorded digest describes.
    """
    await _put(key, content, store, normalize=False)


# ---------------------------------------------------------------------------
# Digest helpers
# ---------------------------------------------------------------------------


class TestStorageIntegrityErrorContract:
    """The wire contract the docstring promises, pinned directly.

    Everything else exercises this type through a transfer; nothing asserted
    the code / retryability / audience a consumer's failure handling keys off.
    """

    def test_error_code_and_classification(self) -> None:
        from application_sdk.errors import STORAGE_INTEGRITY, Audience, FailureCategory

        err = StorageIntegrityError("boom", key="a/b.json")
        assert err.DEFAULT_ERROR_CODE is STORAGE_INTEGRITY
        assert err.error_code.code == "AAF-STR-007"
        assert err.category is FailureCategory.DATA_INTEGRITY
        assert err.audience is Audience.APP_OWNER
        assert err.effective_retryable is False

    def test_catchable_as_storage_error(self) -> None:
        # Domain parent second in the MRO so existing `except StorageError:`
        # blocks keep firing; categorical parent first so category resolves
        # to DATA_INTEGRITY rather than DEPENDENCY_UNAVAILABLE.
        with pytest.raises(StorageError):
            raise StorageIntegrityError("boom", key="a/b.json")

    def test_evidence_reaches_the_wire_envelope(self) -> None:
        err = StorageIntegrityError(
            "boom",
            key="a/b.json",
            local_path="/tmp/x",
            check="digest",
            expectation="aaa",
            observed="bbb",
        )
        evidence = err.to_failure_details().evidence
        assert evidence["key"] == "a/b.json"
        assert evidence["expectation"] == "aaa"
        assert evidence["observed"] == "bbb"
        assert evidence["check"] == "digest"

    def test_exported_from_the_storage_package(self) -> None:
        import application_sdk.storage as storage_pkg

        assert storage_pkg.StorageIntegrityError is StorageIntegrityError


class TestListDataObjects:
    """The listing that feeds every sidecar-aware transfer decision."""

    async def test_pairs_each_object_with_its_sidecar_flag(
        self, store, tmp_path
    ) -> None:
        await _seed(store, tmp_path, "d/with.json", b'{"a": 1}')
        # A data object with no sidecar beside it (a non-SDK producer's file).
        await _put("d/without.json", b'{"b": 2}', store, normalize=False)

        objects = await batch.list_data_objects("d/", store, normalize=False)

        by_key = {o.key: o for o in objects}
        assert set(by_key) == {"d/with.json", "d/without.json"}
        assert by_key["d/with.json"].has_sidecar is True
        assert by_key["d/without.json"].has_sidecar is False

    async def test_excludes_sidecars_from_the_returned_objects(
        self, store, tmp_path
    ) -> None:
        """Sidecars are bookkeeping: they must never appear as data objects,
        or a prefix download would mirror them to disk."""
        await _seed(store, tmp_path, "d/a.json", b'{"a": 1}')
        objects = await batch.list_data_objects("d/", store, normalize=False)
        assert [o.key for o in objects] == ["d/a.json"]

    async def test_carries_size_and_etag_from_the_listing(
        self, store, tmp_path
    ) -> None:
        """Size and etag come free with the listing — that is what lets a
        prefix download chunk and version-pin without a per-file HEAD."""
        content = b'{"a": 1}'
        await _seed(store, tmp_path, "d/a.json", content)
        (obj,) = await batch.list_data_objects("d/", store, normalize=False)
        assert obj.size == len(content)
        assert obj.etag is None or isinstance(obj.etag, str)

    async def test_empty_prefix_returns_nothing(self, store) -> None:
        assert await batch.list_data_objects("nothing/", store, normalize=False) == []

    async def test_agrees_with_list_data_keys_with_meta(self, store, tmp_path) -> None:
        """The older tuple helper is expressed in terms of this one; if they
        ever disagree, one of the two listing filters has drifted."""
        await _seed(store, tmp_path, "d/a.json", b'{"a": 1}')
        await _put("d/b.json", b'{"b": 2}', store, normalize=False)

        objects = await batch.list_data_objects("d/", store, normalize=False)
        tuples = await batch.list_data_keys_with_meta("d/", store, normalize=False)
        assert [(o.key, o.size, o.etag) for o in objects] == tuples


class TestSidecarNaming:
    def test_sidecar_key_appends_suffix(self) -> None:
        assert integrity.sidecar_key("a/b.json") == "a/b.json.sha256"

    def test_is_sidecar_key(self) -> None:
        assert integrity.is_sidecar_key("a/b.json.sha256")
        assert not integrity.is_sidecar_key("a/b.json")

    def test_batch_reexports_the_same_definition(self) -> None:
        # One definition of what counts as a sidecar: the listing filters and
        # the transfer validations must never disagree about it.
        assert batch.SIDECAR_SUFFIX is integrity.SIDECAR_SUFFIX
        assert batch.is_sidecar_key is integrity.is_sidecar_key


class TestSha256File:
    async def test_matches_one_shot_digest(self, tmp_path) -> None:
        f = tmp_path / "data.bin"
        content = b"some bytes here" * 1024
        f.write_bytes(content)
        assert await integrity.sha256_file(f) == _hash_bytes(content)

    async def test_empty_file(self, tmp_path) -> None:
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert await integrity.sha256_file(f) == hashlib.sha256(b"").hexdigest()

    async def test_hashing_does_not_block_event_loop(
        self, tmp_path, monkeypatch
    ) -> None:
        """Hashing a file on the event loop blocks it for the full read+digest.

        A blocked loop cannot run the SDK's auto-heartbeat coroutine, so
        long-running activities heartbeat-time-out even while making progress.
        This was the root cause of a production RCA: an extract-output
        materialize verified thousands of files with a synchronous hash on the
        loop, blocking it ~104s and starving heartbeats.
        """
        entered = threading.Event()
        release = threading.Event()

        def blocking_hash(_path: Path) -> str:
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("release was never signaled")
            return "deadbeef"

        monkeypatch.setattr(integrity, "_sha256_file_sync", blocking_hash)

        f = tmp_path / "f.bin"
        f.write_bytes(b"data")
        task = asyncio.create_task(integrity.sha256_file(f))

        # If hashing ran on the loop, blocking_hash() would freeze the loop here
        # and these awaits would never progress. Offloaded → loop stays live.
        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set(), "hash never started"

        progressed = 0
        for _ in range(10):
            await asyncio.sleep(0.005)
            progressed += 1
        assert progressed == 10  # loop advanced while the hash held its thread

        release.set()
        assert await task == "deadbeef"

    async def test_uses_run_in_thread_pool(self, tmp_path) -> None:
        """Pins the pool choice: ``run_in_thread``, not ``asyncio.to_thread``.

        ``run_in_thread`` dispatches to the SDK's dedicated ``sdk-blocking-*``
        pool rather than asyncio's shared default executor, which Temporal's own
        SDK also uses for internal scheduling — sharing that pool risks
        exhausting it. Fails if a future edit reverts to ``asyncio.to_thread``.
        """
        f = tmp_path / "data.bin"
        f.write_bytes(b"payload")

        # Patched on the *consuming* module: since ADR-0019 ``integrity`` binds
        # ``run_in_thread`` at module scope, so a patch on the substrate module
        # would leave this caller's reference untouched.
        with patch.object(
            integrity, "run_in_thread", wraps=integrity.run_in_thread
        ) as mock_run_in_thread:
            digest = await integrity.sha256_file(f)

        mock_run_in_thread.assert_awaited_once_with(integrity._sha256_file_sync, f)
        assert digest == _hash_bytes(b"payload")


class TestReadExpectedDigest:
    async def test_returns_none_when_absent(self, store) -> None:
        await _put("plain.json", b"{}", store, normalize=False)
        assert await integrity.read_expected_digest(store, "plain.json") is None

    async def test_reads_sidecar_written_by_upload(self, store, tmp_path) -> None:
        art = await _seed(store, tmp_path, "a/b.json", b'{"x": 1}')
        assert await integrity.read_expected_digest(store, art.key) == art.digest

    async def test_sidecar_present_false_short_circuits(self, store, tmp_path) -> None:
        art = await _seed(store, tmp_path, "a/b.json", b'{"x": 1}')
        # Listing already told us there is no sidecar — believe it, no requests.
        with patch.object(
            obstore, "head_async", side_effect=AssertionError("probed anyway")
        ):
            assert (
                await integrity.read_expected_digest(
                    store, art.key, sidecar_present=False
                )
                is None
            )

    async def test_unreadable_sidecar_degrades_to_unverified(
        self, store, tmp_path, caplog
    ) -> None:
        """An IAM policy covering data objects but not sidecars must not break
        every download in the fleet — degrade with a WARNING instead."""
        art = await _seed(store, tmp_path, "a/b.json", b'{"x": 1}')
        with patch(
            "application_sdk.storage.ops.obstore.get_async",
            side_effect=RuntimeError("access denied"),
        ):
            assert (
                await integrity.read_expected_digest(
                    store, art.key, sidecar_present=True
                )
                is None
            )

    async def test_never_looks_up_a_sidecar_for_a_sidecar(self, store) -> None:
        assert await integrity.read_expected_digest(store, "a/b.json.sha256") is None

    @pytest.mark.parametrize(
        "garbage",
        [b"not-a-digest", b"", b"deadbeef", b"z" * 64],
        ids=["prose", "empty", "too-short", "non-hex"],
    )
    async def test_malformed_sidecar_is_treated_as_absent(
        self, store, tmp_path, garbage
    ) -> None:
        """Only our own format is trustworthy as an expectation.

        A stray file at ``{key}.sha256`` — another tool's metadata, a
        half-written sidecar — must not fail a *healthy* artifact with a
        non-retryable corruption error. Not verifying one file is the lesser
        harm, and it is logged.
        """
        art = await _seed(store, tmp_path, "bad/data.json", b'{"a": 1}')
        await _put(art.key + ".sha256", garbage, store, normalize=False)

        assert await integrity.read_expected_digest(store, art.key) is None

        # And the download of that healthy object still succeeds.
        dest = tmp_path / "out.json"
        await download_file(art.key, dest, store, normalize=False)
        assert dest.read_bytes() == art.content

    async def test_uppercase_digest_is_normalised_not_rejected(
        self, store, tmp_path
    ) -> None:
        """Hex is case-insensitive, so an uppercase sidecar describes the same
        bytes. Returning it raw would fail a healthy artifact against our
        lowercase ``hexdigest()`` — a false corruption report, which is the
        worst possible outcome for this feature.
        """
        art = await _seed(store, tmp_path, "ok/data.json", b'{"a": 1}')
        await _put(
            art.key + ".sha256",
            f"  {art.digest.upper()}\n".encode(),
            store,
            normalize=False,
        )
        assert await integrity.read_expected_digest(store, art.key) == art.digest

        # The end-to-end proof: the download must succeed, not raise.
        dest = tmp_path / "out.json"
        await download_file(art.key, dest, store, normalize=False)
        assert dest.read_bytes() == art.content


class TestComparators:
    def test_size_mismatch_is_a_retryable_dependency_error(self) -> None:
        with pytest.raises(StorageError) as exc:
            integrity.check_transfer_size("download", "k", expected=10, actual=4)
        assert not isinstance(exc.value, StorageIntegrityError)
        assert exc.value.effective_retryable is True

    def test_size_match_passes(self) -> None:
        integrity.check_transfer_size("download", "k", expected=10, actual=10)

    def test_digest_mismatch_is_non_retryable_and_names_both_digests(self) -> None:
        with pytest.raises(StorageIntegrityError) as exc:
            integrity.check_transfer_digest(
                "download", "k", expected="aaa", actual="bbb"
            )
        err = exc.value
        assert err.effective_retryable is False
        assert err.check == "digest"
        assert err.expectation == "aaa"
        assert err.observed == "bbb"
        assert err.key == "k"

    def test_local_shrink_is_an_integrity_error(self) -> None:
        with pytest.raises(StorageIntegrityError) as exc:
            integrity.check_local_file_stable(
                "k", "/tmp/f", declared_size=100, bytes_read=40
            )
        assert exc.value.check == "local_size"

    def test_local_growth_is_not_an_error(self) -> None:
        # A file appended to during the read still uploaded a complete,
        # self-consistent snapshot — only shrinkage means truncation.
        integrity.check_local_file_stable(
            "k", "/tmp/f", declared_size=100, bytes_read=140
        )


# ---------------------------------------------------------------------------
# Upstream: upload_file
# ---------------------------------------------------------------------------


class TestUploadValidation:
    async def test_upload_writes_a_sidecar(self, store, tmp_path) -> None:
        art = await _seed(store, tmp_path, "out/data.json", b'{"a": 1}')
        raw = await _get_bytes("out/data.json.sha256", store, normalize=False)
        assert raw is not None
        assert raw.decode() == art.digest

    async def test_sidecar_suppressed_when_hashing_is_off(
        self, store, tmp_path
    ) -> None:
        f = tmp_path / "x.json"
        f.write_bytes(b"{}")
        digest = await upload_file(
            "ext/x.json", f, store, normalize=False, compute_hash=False
        )
        assert digest is None
        assert await _get_bytes("ext/x.json.sha256", store, normalize=False) is None

    async def test_sidecar_suppressed_by_flag(self, store, tmp_path) -> None:
        f = tmp_path / "x.json"
        f.write_bytes(b"{}")
        await upload_file("ns/x.json", f, store, normalize=False, write_sidecar=False)
        assert await _get_bytes("ns/x.json.sha256", store, normalize=False) is None

    async def test_sidecar_suppressed_by_the_env_kill_switch(
        self, store, tmp_path, monkeypatch
    ) -> None:
        """The deployment-wide escape hatch, exercised through the same
        resolution path the default takes — a per-call ``write_sidecar=False``
        would not prove the env var is wired up at all."""
        monkeypatch.setattr("application_sdk.constants.STORAGE_WRITE_SIDECARS", False)
        f = tmp_path / "x.json"
        f.write_bytes(b"{}")
        await upload_file("env/x.json", f, store, normalize=False)
        assert await _get_bytes("env/x.json.sha256", store, normalize=False) is None
        # The object itself still landed, and was still size-validated.
        assert await _get_bytes("env/x.json", store, normalize=False) == b"{}"

    async def test_local_truncation_mid_upload_raises(self, store, tmp_path) -> None:
        """The file shrinks between the opening stat and the read reaching EOF,
        so the object in the store is a prefix of the intended artifact."""
        f = tmp_path / "big.json"
        f.write_bytes(b"x" * 4096)

        real_open = Path.open

        def truncating_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            # Shrink after the size was stat'd but before any bytes are read.
            with open(self, "r+b") as trunc:
                trunc.truncate(16)
            return handle

        with (
            patch.object(Path, "open", truncating_open),
            pytest.raises(StorageIntegrityError) as exc,
        ):
            await upload_file("trunc/big.json", f, store, normalize=False)
        assert exc.value.check == "local_size"
        assert exc.value.effective_retryable is False

    async def test_object_dropped_by_the_store_raises(self, store, tmp_path) -> None:
        """A backend that reports success but persists nothing — the documented
        S3-style empty-object case — must not pass silently."""
        f = tmp_path / "x.json"
        f.write_bytes(b"{}")
        with (
            patch(
                "application_sdk.storage.ops.get_file_meta",
                new=_async_return(None),
            ),
            pytest.raises(StorageError) as exc,
        ):
            await upload_file("gone/x.json", f, store, normalize=False)
        assert not isinstance(exc.value, StorageIntegrityError)
        assert "absent from the store" in exc.value.message

    async def test_short_object_readback_raises(self, store, tmp_path) -> None:
        f = tmp_path / "x.json"
        f.write_bytes(b"0123456789")
        with (
            patch(
                "application_sdk.storage.ops.get_file_meta",
                new=_async_return((4, "etag")),
            ),
            pytest.raises(StorageError) as exc,
        ):
            await upload_file("short/x.json", f, store, normalize=False)
        assert "Incomplete upload" in exc.value.message

    async def test_verification_can_be_switched_off(self, store, tmp_path) -> None:
        f = tmp_path / "x.json"
        f.write_bytes(b"{}")
        with patch(
            "application_sdk.storage.ops.get_file_meta",
            new=_async_return(None),
        ):
            await upload_file("off/x.json", f, store, normalize=False, verify=False)

    async def test_sidecar_write_skips_a_sidecar_key(self, store) -> None:
        """``{key}.sha256.sha256`` must never be written — a sidecar is SDK
        bookkeeping, not a user artifact, and looking one up is a guaranteed
        miss on every read."""
        await integrity.write_digest_sidecar(store, "a/b.json.sha256", "deadbeef")
        assert (
            await _get_bytes("a/b.json.sha256.sha256", store, normalize=False) is None
        )

    async def test_sidecar_write_skips_transfer_state_key(self, store) -> None:
        """The resumable-download checkpoint is local-only bookkeeping rewritten
        in place; it never carries a digest sidecar either."""
        await integrity.write_digest_sidecar(
            store, "a/b.parquet.transfer-state", "deadbeef"
        )
        assert (
            await _get_bytes(
                "a/b.parquet.transfer-state.sha256", store, normalize=False
            )
            is None
        )

    async def test_sidecar_write_failure_does_not_fail_the_upload(self, store) -> None:
        """The object itself uploaded fine; it just cannot be verified later.

        Losing verification must not also lose the artifact — a store that
        accepts objects but rejects the sidecar (a narrower write policy, a
        transient failure) would otherwise turn every upload into a hard error.
        """
        with patch(
            "application_sdk.storage.ops.obstore.put_async",
            side_effect=RuntimeError("sidecar denied"),
        ):
            await integrity.write_digest_sidecar(store, "w/ok.json", "abc123")


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


# ---------------------------------------------------------------------------
# Downstream: download_file / download_file_chunked
# ---------------------------------------------------------------------------


class TestDownloadValidation:
    async def test_clean_roundtrip_passes(self, store, tmp_path) -> None:
        art = await _seed(store, tmp_path, "ok/data.json", b'{"rows": [1, 2, 3]}')
        dest = tmp_path / "out.json"
        await download_file(art.key, dest, store, normalize=False)
        assert dest.read_bytes() == art.content

    async def test_truncated_object_raises_typed_error(self, store, tmp_path) -> None:
        """The FND-306 case: a producer died mid-write, so the stored object is
        a truncated prefix of what its sidecar describes. The consumer must get
        a typed, non-retryable error naming the file — not a parser blowup."""
        art = await _seed(
            store, tmp_path, "corrupt/column-ancestral-201.json", b'{"rows": [1, 2, 3]}'
        )
        await _corrupt_object(store, art.key, b'{"rows": [1, 2')

        with pytest.raises(StorageIntegrityError) as exc:
            await download_file(art.key, tmp_path / "out.json", store, normalize=False)
        err = exc.value
        assert err.key == art.key
        assert err.expectation == art.digest
        assert err.observed == _hash_bytes(b'{"rows": [1, 2')
        assert err.effective_retryable is False

    async def test_zero_byte_object_is_caught(self, store, tmp_path) -> None:
        art = await _seed(store, tmp_path, "corrupt/empty.json", b'{"a": 1}')
        await _corrupt_object(store, art.key, b"")
        with pytest.raises(StorageIntegrityError):
            await download_file(art.key, tmp_path / "out.json", store, normalize=False)

    async def test_failure_is_byte_stable_across_fresh_downloads(
        self, store, tmp_path
    ) -> None:
        """Byte-stability across attempts is the discriminator between a corrupt
        source and a flaky transfer — the same digest must be reported each
        time, which is what makes the error safe to mark non-retryable."""
        art = await _seed(store, tmp_path, "corrupt/stable.json", b'{"a": 1, "b": 2}')
        await _corrupt_object(store, art.key, b'{"a": 1, "b"')

        observed = []
        for i in range(3):
            with pytest.raises(StorageIntegrityError) as exc:
                await download_file(
                    art.key, tmp_path / f"out{i}.json", store, normalize=False
                )
            observed.append(exc.value.observed)
        assert len(set(observed)) == 1

    async def test_no_sidecar_means_no_digest_check(self, store, tmp_path) -> None:
        await _put("bare/data.json", b"whatever", store, normalize=False)
        dest = tmp_path / "out.json"
        await download_file("bare/data.json", dest, store, normalize=False)
        assert dest.read_bytes() == b"whatever"

    async def test_short_write_raises_a_retryable_error(self, store, tmp_path) -> None:
        """Fewer bytes on disk than the store declared is a truncated transfer,
        not a corrupt source — retrying it is worthwhile, so it must not be the
        non-retryable integrity error."""
        await _put("t/data.bin", b"0123456789", store, normalize=False)

        real_get = obstore.get_async

        async def short_get(st, key, **kw):
            result = await real_get(st, key, **kw)
            return _ShortStream(result, keep=4)

        with (
            patch("application_sdk.storage.ops.obstore.get_async", new=short_get),
            pytest.raises(StorageError) as exc,
        ):
            await download_file(
                "t/data.bin", tmp_path / "o.bin", store, normalize=False
            )
        assert not isinstance(exc.value, StorageIntegrityError)
        assert "Incomplete download" in exc.value.message

    async def test_caller_supplied_digest_skips_the_sidecar_lookup(
        self, store, tmp_path
    ) -> None:
        art = await _seed(store, tmp_path, "hint/data.json", b'{"a": 1}')
        with patch.object(
            integrity, "read_expected_digest", side_effect=AssertionError("looked up")
        ):
            await download_file(
                art.key,
                tmp_path / "o.json",
                store,
                normalize=False,
                expected_sha256=art.digest,
            )

    async def test_verification_can_be_switched_off(self, store, tmp_path) -> None:
        art = await _seed(store, tmp_path, "off/data.json", b'{"a": 1}')
        await _corrupt_object(store, art.key, b"{")
        dest = tmp_path / "o.json"
        await download_file(art.key, dest, store, normalize=False, verify=False)
        assert dest.read_bytes() == b"{"

    async def test_kill_switch_env_disables_verification(
        self, store, tmp_path, monkeypatch
    ) -> None:
        art = await _seed(store, tmp_path, "sw/data.json", b'{"a": 1}')
        await _corrupt_object(store, art.key, b"{")
        monkeypatch.setattr("application_sdk.constants.STORAGE_VERIFY_TRANSFERS", False)
        await download_file(art.key, tmp_path / "o.json", store, normalize=False)


class _ShortStream:
    """Wraps a ``GetResult`` and truncates its stream to *keep* bytes."""

    def __init__(self, inner, *, keep: int) -> None:
        self._inner = inner
        self._keep = keep

    @property
    def meta(self):
        return self._inner.meta

    def stream(self, **kwargs):
        inner_stream = self._inner.stream(**kwargs)
        keep = self._keep

        async def _gen():
            async for chunk in inner_stream:
                raw = bytes(chunk)[:keep]
                keep_left = keep - len(raw)
                if raw:
                    yield raw
                if keep_left <= 0:
                    return

        return _gen()


class TestChunkedDownloadValidation:
    CONTENT = b"".join(bytes([i % 251]) for i in range(4096))

    async def test_truncated_object_raises_typed_error(self, store, tmp_path) -> None:
        art = await _seed(store, tmp_path, "big/data.bin", self.CONTENT)
        await _corrupt_object(store, art.key, self.CONTENT[:2048])

        with pytest.raises(StorageIntegrityError) as exc:
            await download_file_chunked(
                art.key,
                tmp_path / "o.bin",
                store,
                chunk_size_bytes=512,
                normalize=False,
            )
        assert exc.value.expectation == art.digest
        assert exc.value.observed == _hash_bytes(self.CONTENT[:2048])

    async def test_clean_chunked_roundtrip_passes(self, store, tmp_path) -> None:
        art = await _seed(store, tmp_path, "big/ok.bin", self.CONTENT)
        dest = tmp_path / "o.bin"
        await download_file_chunked(
            art.key, dest, store, chunk_size_bytes=512, normalize=False
        )
        assert dest.read_bytes() == self.CONTENT

    async def test_short_range_read_raises(self, store, tmp_path) -> None:
        """A pre-allocated file means a short range leaves a hole of NULs that
        reads back as a plausible file of exactly the right length."""
        await _put("big/short.bin", self.CONTENT, store, normalize=False)

        real_range = obstore.get_range_async
        real_get = obstore.get_async

        async def short_range(st, key, *, start, length):
            raw = await real_range(st, key, start=start, length=length)
            return bytes(raw)[: length - 1] if start == 512 else raw

        async def unpinned_get(st, key, **kw):
            # Force the unpinned range-GET path so short_range is exercised.
            options = kw.get("options") or {}
            if "range" in options:
                rng = options["range"]
                raw = await short_range(st, key, start=rng[0], length=rng[1] - rng[0])
                return _Bytes(raw)
            return await real_get(st, key, **kw)

        with (
            patch(
                "application_sdk.storage.chunked.obstore.get_async", new=unpinned_get
            ),
            pytest.raises(StorageError) as exc,
        ):
            await download_file_chunked(
                "big/short.bin",
                tmp_path / "o.bin",
                store,
                chunk_size_bytes=512,
                normalize=False,
                resume=False,
            )
        # The chunk failure handler wraps it as the generic chunked-download
        # error; the short-read diagnosis survives as the cause.
        assert "Short range read" in str(exc.value.cause)

    async def test_unpinned_download_never_resumes_from_a_checkpoint(
        self, store, tmp_path
    ) -> None:
        """With no etag, nothing pins the object generation.

        Reusing a previous attempt's chunks could splice a rewritten object's
        new bytes onto the old ones — a file of exactly the right length that
        reads as a clean transfer. The digest check catches that only where a
        sidecar exists; on a bare download nothing would. So an unpinned
        download starts fresh and leaves no checkpoint behind.
        """
        await _put("np/data.bin", self.CONTENT, store, normalize=False)
        out = tmp_path / "o.bin"
        state = _transfer_state_path(out)
        state.parent.mkdir(parents=True, exist_ok=True)

        # A checkpoint from a hypothetical earlier attempt, matching this
        # object's generation as far as an unpinned download could tell.
        out.write_bytes(b"\x00" * len(self.CONTENT))
        state.write_bytes(
            orjson.dumps(
                {
                    "key": "np/data.bin",
                    "file_size": len(self.CONTENT),
                    "chunk_size": 512,
                    "etag": None,
                    "done": [0, 1, 2],
                }
            )
        )

        await download_file_chunked(
            "np/data.bin",
            out,
            store,
            chunk_size_bytes=512,
            normalize=False,
            file_size=len(self.CONTENT),
            etag=None,
            resume=True,
        )

        # Every chunk re-fetched: the NULs the checkpoint claimed were done
        # are gone, and no checkpoint survives.
        assert out.read_bytes() == self.CONTENT
        assert not state.exists()

    async def test_pinned_download_still_resumes(self, store, tmp_path) -> None:
        """The complement: an etag pins the generation, so resume stays on —
        this is the BLDX-1523 behaviour the fix above must not have broken."""
        await _put("p/data.bin", self.CONTENT, store, normalize=False)
        meta = await get_file_meta("p/data.bin", store, normalize=False)
        assert meta is not None
        _, etag = meta
        if etag is None:
            pytest.skip("store does not provide etags; nothing to pin")

        out = tmp_path / "o.bin"
        state = _transfer_state_path(out)

        _part_path(out).parent.mkdir(parents=True, exist_ok=True)
        _part_path(out).write_bytes(b"\x00" * len(self.CONTENT))
        state.write_bytes(
            orjson.dumps(
                {
                    "key": "p/data.bin",
                    "file_size": len(self.CONTENT),
                    "chunk_size": 512,
                    "etag": etag,
                    "done": [0],
                }
            )
        )

        fetched: list[int] = []
        real_get = obstore.get_async

        async def counting_get(st, key, **kw):
            rng = (kw.get("options") or {}).get("range")
            if rng:
                fetched.append(rng[0])
            return await real_get(st, key, **kw)

        with patch(
            "application_sdk.storage.chunked.obstore.get_async", new=counting_get
        ):
            await download_file_chunked(
                "p/data.bin",
                out,
                store,
                chunk_size_bytes=512,
                normalize=False,
                file_size=len(self.CONTENT),
                etag=etag,
                resume=True,
            )

        # Chunk 0 was checkpointed as done, so it must not be re-fetched.
        assert 0 not in fetched
        assert fetched, "no ranges fetched at all — resume swallowed the download"

    async def test_small_file_delegation_still_verifies(self, store, tmp_path) -> None:
        """Below the chunking threshold the call delegates to ``download_file``
        — the delegation must carry the verification with it."""
        art = await _seed(store, tmp_path, "small/data.json", b'{"a": 1}')
        await _corrupt_object(store, art.key, b"{")
        with pytest.raises(StorageIntegrityError):
            await download_file_chunked(
                art.key,
                tmp_path / "o.json",
                store,
                chunk_size_bytes=1 << 20,
                normalize=False,
            )


class _Bytes:
    """Minimal stand-in for a range ``GetResult``."""

    def __init__(self, raw: bytes) -> None:
        self._raw = bytes(raw)

    async def bytes_async(self) -> bytes:
        return self._raw


# ---------------------------------------------------------------------------
# Convergence: the higher-level paths inherit the same validations
# ---------------------------------------------------------------------------


class TestAllPathsConverge:
    async def test_transfer_download_single_file(self, store, tmp_path) -> None:
        art = await _seed(store, tmp_path, "conv/one.json", b'{"a": 1}')
        await _corrupt_object(store, art.key, b"{")
        with pytest.raises(StorageIntegrityError):
            await transfer.download(art.key, str(tmp_path / "o.json"), store=store)

    async def test_transfer_download_prefix(self, store, tmp_path) -> None:
        art = await _seed(store, tmp_path, "conv/dir/one.json", b'{"a": 1}')
        await _seed(store, tmp_path, "conv/dir/two.json", b'{"b": 2}')
        await _corrupt_object(store, art.key, b"{")
        with pytest.raises(StorageIntegrityError):
            await transfer.download("conv/dir/", str(tmp_path / "out"), store=store)

    async def test_batch_download_prefix(self, store, tmp_path) -> None:
        art = await _seed(store, tmp_path, "conv/b/one.json", b'{"a": 1}')
        await _corrupt_object(store, art.key, b"{")
        with pytest.raises(StorageIntegrityError):
            await batch.download_prefix("conv/b/", tmp_path / "out", store)

    async def test_batch_download_prefix_does_not_mirror_sidecars(
        self, store, tmp_path
    ) -> None:
        """Sidecars are SDK bookkeeping. Mirroring them into a directory handed
        to a reader that globs it (RocksDB / DuckDB state, a parquet dataset)
        would put files there that the producer never wrote."""
        await _seed(store, tmp_path, "conv/state/a.sst", b"sst-bytes")
        dest = tmp_path / "state"
        await batch.download_prefix("conv/state/", dest, store)
        written = sorted(p.name for p in dest.rglob("*") if p.is_file())
        assert written == ["a.sst"]

    async def test_transfer_upload_then_download_roundtrip(
        self, store, tmp_path
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.json").write_bytes(b'{"a": 1}')
        (src / "b.json").write_bytes(b'{"b": 2}')

        out = await transfer.upload(str(src), "conv/rt", store=store)
        assert out.synced

        dest = tmp_path / "dest"
        await transfer.download("conv/rt/", str(dest), store=store)
        assert (dest / "a.json").read_bytes() == b'{"a": 1}'
        assert (dest / "b.json").read_bytes() == b'{"b": 2}'


class TestDeferredRemoteVerify:
    """``upload_file(defer_remote_verify=True)``: local check on, readback HEAD off.

    The directory upload path verifies a whole batch against one listing of
    its prefix (FND-1339); this is the per-file primitive it leans on.
    """

    @staticmethod
    def _count_heads(monkeypatch) -> list[str]:
        import application_sdk.storage.ops as ops_mod

        heads: list[str] = []
        real = ops_mod.get_file_meta

        async def meta(key, *a, **kw):
            heads.append(key)
            return await real(key, *a, **kw)

        monkeypatch.setattr(ops_mod, "get_file_meta", meta)
        return heads

    async def test_default_still_reads_back_with_a_head(
        self, store, tmp_path, monkeypatch
    ) -> None:
        from application_sdk.storage.ops import upload_file

        heads = self._count_heads(monkeypatch)
        f = tmp_path / "c.bin"
        f.write_bytes(b"x" * 100)

        await upload_file("k/c.bin", f, store)

        assert heads == ["k/c.bin"]

    async def test_deferred_skips_the_head_but_keeps_digest_and_sidecar(
        self, store, tmp_path, monkeypatch
    ) -> None:
        from application_sdk.storage.ops import _get_bytes, upload_file

        heads = self._count_heads(monkeypatch)
        f = tmp_path / "a.bin"
        f.write_bytes(b"x" * 100)

        digest = await upload_file("k/a.bin", f, store, defer_remote_verify=True)

        assert digest == hashlib.sha256(b"x" * 100).hexdigest()
        assert heads == []
        sidecar = await _get_bytes("k/a.bin.sha256", store, normalize=False)
        assert sidecar is not None
        assert sidecar.decode().strip() == digest

    async def test_deferred_with_write_sidecar_false_holds_the_sidecar_back(
        self, store, tmp_path, monkeypatch
    ) -> None:
        from application_sdk.storage.ops import exists, upload_file

        self._count_heads(monkeypatch)
        f = tmp_path / "b.bin"
        f.write_bytes(b"y" * 50)

        digest = await upload_file(
            "k/b.bin", f, store, defer_remote_verify=True, write_sidecar=False
        )

        assert digest == hashlib.sha256(b"y" * 50).hexdigest()
        assert await exists("k/b.bin", store, normalize=False) is True
        assert await exists("k/b.bin.sha256", store, normalize=False) is False
