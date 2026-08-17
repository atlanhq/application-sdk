"""FND-318 acceptance, one test per SDK writer that produces an app artifact.

The previous module tests the helper. These test the claim that actually
matters — that the paths apps use go *through* it — by driving each real
function with the disk out from under it and asserting the same two properties
every time: nothing at the artifact's final path, and a typed ``DiskFullError``
rather than a bare ``OSError`` for a broad ``except`` to swallow.

The list is the inventory FND-318 was filed against:

============================================  ==========================
writer                                        artifact
============================================  ==========================
``copy_directory_parallel``                   carry-forward state copy
``persist_marker_to_storage``                 the incremental marker
``_write_metadata``                           diff metadata for Argo
``Writer._write_statistics``                  the statistics sidecar
``ParquetFileWriter._write_chunk``            a parquet chunk
``_write_local_sidecar``                      the local ``.sha256``
``JsonFileWriter._write_chunk``               a JSON chunk (typed only)
============================================  ==========================

The JSON chunk is the one exception, and deliberately so: successive calls
append to the same file, and an append cannot be staged and renamed without
rewriting everything already in it. It gets the typed error and nothing more,
which its test asserts explicitly rather than leaving as an omission.
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from application_sdk.common.atomic import PARTIAL_DIRNAME
from application_sdk.common.incremental.helpers import copy_directory_parallel
from application_sdk.errors import DiskFullError


def _enospc(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError(errno.ENOSPC, "No space left on device")


class _Usage:
    def __init__(self, *, free: int) -> None:
        self.total = free * 2
        self.used = free
        self.free = free


def _artifacts(directory: Path) -> list[str]:
    """Names directly under *directory*, excluding the staging tree."""
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir() if p.name != PARTIAL_DIRNAME)


# ---------------------------------------------------------------------------
# The incident site: the carry-forward copy
# ---------------------------------------------------------------------------


class TestCarryForwardCopy:
    """``copy_directory_parallel`` — where the 184-byte column file came from."""

    def test_a_copy_that_runs_out_of_space_names_no_partial_file(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "column"
        source.mkdir()
        (source / "column-ancestral-201.json").write_bytes(b'{"rows": []}' * 100)
        destination = tmp_path / "current-state" / "column"

        def _truncating_copy2(src: Any, dst: Any, **_kwargs: Any) -> Any:
            # What `shutil.copy2` actually does on a full disk: it copies until
            # the filesystem stops it, *then* raises. Both halves matter — a
            # stub that raises without writing would pass against the very
            # behaviour this test exists to rule out.
            Path(dst).write_bytes(Path(src).read_bytes()[:184])
            _enospc()

        with patch("application_sdk.common.atomic.shutil.copy2", _truncating_copy2):
            with pytest.raises(DiskFullError) as caught:
                copy_directory_parallel(source, destination)

        assert caught.value.operation == "carry-forward copy"
        assert (
            _artifacts(destination) == []
        ), "a truncated file was left where a carried-forward artifact goes"

    def test_a_plainly_undersized_volume_fails_before_any_byte_moves(
        self, tmp_path: Path
    ) -> None:
        """The five-second failure, instead of silent corruption forty minutes in."""
        source = tmp_path / "column"
        source.mkdir()
        for index in range(3):
            (source / f"column-{index}.json").write_bytes(b"x" * 4096)
        destination = tmp_path / "current-state" / "column"

        copied: list[Any] = []

        def _record(*args: Any, **kwargs: Any) -> Any:
            copied.append(args)

        with patch(
            "application_sdk.common.atomic.shutil.disk_usage",
            return_value=_Usage(free=512),
        ):
            with patch("application_sdk.common.atomic.shutil.copy2", _record):
                with pytest.raises(DiskFullError) as caught:
                    copy_directory_parallel(source, destination)

        assert copied == [], "the preflight let the copy start anyway"
        assert caught.value.required_bytes == 3 * 4096
        assert caught.value.free_bytes == 512

    def test_a_healthy_copy_is_unchanged(self, tmp_path: Path) -> None:
        source = tmp_path / "column"
        source.mkdir()
        (source / "a.json").write_bytes(b'{"a": 1}')
        (source / "b.json").write_bytes(b'{"b": 2}')
        destination = tmp_path / "current-state" / "column"

        assert copy_directory_parallel(source, destination) == 2
        assert _artifacts(destination) == ["a.json", "b.json"]
        assert (destination / "a.json").read_bytes() == b'{"a": 1}'


# ---------------------------------------------------------------------------
# The incremental marker
# ---------------------------------------------------------------------------


class TestMarkerWrite:
    async def test_a_marker_that_runs_out_of_space_is_not_written_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A half-written marker still parses — it is just the wrong window."""
        from application_sdk.common.incremental import helpers, marker  # noqa: PLC0415

        monkeypatch.setattr(helpers, "TEMPORARY_PATH", str(tmp_path))
        upload = AsyncMock()
        monkeypatch.setattr(marker, "upload_file", upload)

        marker_path = helpers.get_persistent_artifacts_path(
            "default/oracle/1696528289", "marker.txt", "oracle"
        )

        with patch("os.fsync", _enospc):
            with pytest.raises(DiskFullError) as caught:
                await marker.persist_marker_to_storage(
                    connection_qualified_name="default/oracle/1696528289",
                    marker_value="2026-08-13T00:00:00Z",
                    application_name="oracle",
                )

        assert caught.value.operation == "marker write"
        assert not marker_path.exists()
        upload.assert_not_awaited()

    async def test_a_healthy_marker_write_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.common.incremental import helpers, marker  # noqa: PLC0415

        monkeypatch.setattr(helpers, "TEMPORARY_PATH", str(tmp_path))
        monkeypatch.setattr(marker, "upload_file", AsyncMock())

        result = await marker.persist_marker_to_storage(
            connection_qualified_name="default/oracle/1696528289",
            marker_value="2026-08-13T00:00:00Z",
            application_name="oracle",
        )

        assert result["marker_written"] is True
        assert Path(result["local_path"]).read_text(encoding="utf-8") == (
            "2026-08-13T00:00:00Z"
        )


# ---------------------------------------------------------------------------
# Diff metadata — the file Argo routes on
# ---------------------------------------------------------------------------


class TestDiffMetadataWrite:
    def test_a_truncated_routing_file_is_never_named(self, tmp_path: Path) -> None:
        from application_sdk.common.incremental.models import (  # noqa: PLC0415
            IncrementalDiffResult,
        )
        from application_sdk.common.incremental.state.incremental_diff import (  # noqa: PLC0415
            _write_metadata,
        )

        result = IncrementalDiffResult(is_incremental=True)

        with patch("os.fsync", _enospc):
            with pytest.raises(DiskFullError) as caught:
                _write_metadata(tmp_path, result)

        assert caught.value.operation == "diff metadata write"
        assert _artifacts(tmp_path) == []

    def test_a_healthy_metadata_write_is_unchanged(self, tmp_path: Path) -> None:
        import orjson  # noqa: PLC0415

        from application_sdk.common.incremental.models import (  # noqa: PLC0415
            IncrementalDiffResult,
        )
        from application_sdk.common.incremental.state.incremental_diff import (  # noqa: PLC0415
            _write_metadata,
        )

        _write_metadata(tmp_path, IncrementalDiffResult(is_incremental=True))

        written = orjson.loads((tmp_path / "metadata.json").read_bytes())
        assert written["is_incremental"] is True


# ---------------------------------------------------------------------------
# Writer output
# ---------------------------------------------------------------------------


class TestWriterStatistics:
    async def test_a_statistics_sidecar_that_runs_out_of_space_is_not_named(
        self, tmp_path: Path
    ) -> None:
        """A truncated sidecar reads as a smaller, plausible row count."""
        from application_sdk.storage.formats.format_errors import (  # noqa: PLC0415
            FormatStatisticsWriteError,
        )
        from application_sdk.storage.formats.json import JsonFileWriter  # noqa: PLC0415

        writer = JsonFileWriter(path=str(tmp_path / "out"))
        writer.total_record_count = 4000

        with patch("os.fsync", _enospc):
            with pytest.raises(FormatStatisticsWriteError) as caught:
                await writer._write_statistics()

        # The writer wraps everything it raises; the disk-full cause has to
        # survive that wrap or the failure is unattributable.
        assert isinstance(caught.value.cause, DiskFullError)
        assert _artifacts(tmp_path / "out" / "statistics") == []


class TestParquetChunkWrite:
    async def test_a_chunk_that_runs_out_of_space_leaves_no_parquet_behind(
        self, tmp_path: Path
    ) -> None:
        """The truncated-parquet case: no footer, and every reader fails alike."""
        from application_sdk.storage.formats.parquet import (  # noqa: PLC0415
            ParquetFileWriter,
        )

        output = tmp_path / "out"
        output.mkdir()
        writer = ParquetFileWriter(path=str(output))
        chunk = pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]})
        target = output / "chunk-0-part0.parquet"

        with patch("os.fsync", _enospc):
            with pytest.raises(DiskFullError) as caught:
                await writer._write_chunk(chunk, str(target))

        assert caught.value.operation == "parquet chunk write"
        assert not target.exists()
        assert _artifacts(output) == []

    async def test_a_healthy_chunk_write_publishes_under_its_own_name(
        self, tmp_path: Path
    ) -> None:
        from application_sdk.storage.formats.parquet import (  # noqa: PLC0415
            ParquetFileWriter,
        )

        output = tmp_path / "out"
        output.mkdir()
        writer = ParquetFileWriter(path=str(output))
        chunk = pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]})
        target = output / "chunk-0-part0.parquet"

        await writer._write_chunk(chunk, str(target))

        assert _artifacts(output) == ["chunk-0-part0.parquet"]
        assert pd.read_parquet(target)["id"].tolist() == [1, 2, 3]


class TestJsonChunkWrite:
    async def test_it_is_typed_even_though_it_cannot_be_atomic(
        self, tmp_path: Path
    ) -> None:
        """Stated as a test rather than left as a silent gap.

        The append protocol rules out staging, so the file *can* be left short.
        What must not happen is the failure arriving as a bare ``OSError`` with
        nothing naming the file or the step.
        """
        from application_sdk.storage.formats.json import JsonFileWriter  # noqa: PLC0415

        writer = JsonFileWriter(path=str(tmp_path / "out"))
        chunk = pd.DataFrame({"id": [1], "value": ["a"]})
        target = tmp_path / "out" / "chunk-0-part0.json"

        class _FullHandle:
            def write(self, _data: bytes) -> int:
                raise OSError(errno.ENOSPC, "No space left on device")

            def close(self) -> None:
                pass

        with patch(
            "application_sdk.storage.formats.json.SafeFileOps.open"
        ) as mock_open:
            mock_open.return_value.__enter__ = lambda _self: _FullHandle()
            mock_open.return_value.__exit__ = lambda *_args: None
            with pytest.raises(DiskFullError) as caught:
                await writer._write_chunk(chunk, str(target))

        assert caught.value.operation == "json chunk write"
        assert caught.value.path == str(target)


class TestLocalSidecarWrite:
    def test_a_sidecar_that_runs_out_of_space_is_absent_rather_than_wrong(
        self, tmp_path: Path
    ) -> None:
        """A truncated digest is worse than none: it re-downloads a good file forever."""
        from application_sdk.storage.reference import (  # noqa: PLC0415
            _write_local_sidecar,
        )

        artifact = tmp_path / "data.parquet"
        artifact.write_bytes(b"parquet")

        with patch("os.fsync", _enospc):
            # Best-effort by contract: the caller continues without a sidecar.
            _write_local_sidecar(str(artifact), "a" * 64)

        assert _artifacts(tmp_path) == ["data.parquet"]

    def test_a_healthy_sidecar_write_is_unchanged(self, tmp_path: Path) -> None:
        from application_sdk.storage.reference import (  # noqa: PLC0415
            _write_local_sidecar,
        )

        artifact = tmp_path / "data.parquet"
        artifact.write_bytes(b"parquet")

        _write_local_sidecar(str(artifact), "a" * 64)

        assert (tmp_path / "data.parquet.sha256").read_text() == "a" * 64


# ---------------------------------------------------------------------------
# Nothing an SDK writer stages may reach the object store
# ---------------------------------------------------------------------------


class TestStagingNeverUploads:
    async def test_a_prefix_upload_of_a_run_directory_skips_staging_trees(
        self, tmp_path: Path
    ) -> None:
        """Staging is nested a level down, as it is under a real output root."""
        from application_sdk.storage.batch import upload_prefix  # noqa: PLC0415
        from application_sdk.storage.factory import create_memory_store  # noqa: PLC0415

        entity_dir = tmp_path / "column"
        staging = entity_dir / PARTIAL_DIRNAME
        staging.mkdir(parents=True)
        (entity_dir / "column-0.json").write_bytes(b"{}")
        (staging / "column-1.json.deadbeef").write_bytes(b'{"trunca')

        keys = await upload_prefix(
            tmp_path, "run", create_memory_store(), normalize=False
        )

        assert keys == ["run/column/column-0.json"]

    async def test_a_prefix_upload_of_a_run_root_skips_a_writer_staging_tree(
        self, tmp_path: Path
    ) -> None:
        """FND-317 sites the writer's staging tree as a *sibling* of its output.

        That puts it out of reach of a walk of the output directory, but a
        prefix upload of the run root walks a level higher — so without the
        shared exclusion a cancelled attempt's staged chunks ship as run output.
        """
        from application_sdk.storage.batch import upload_prefix  # noqa: PLC0415
        from application_sdk.storage.factory import create_memory_store  # noqa: PLC0415
        from application_sdk.storage.formats import (  # noqa: PLC0415
            _STAGING_ROOT_DIRNAME,
        )

        output = tmp_path / "output"
        output.mkdir()
        (output / "chunk-0-part0.parquet").write_bytes(b"published")
        orphaned = tmp_path / _STAGING_ROOT_DIRNAME / "deadbeef" / "output"
        orphaned.mkdir(parents=True)
        (orphaned / "chunk-0-part0.parquet").write_bytes(b"a cancelled attempt")

        keys = await upload_prefix(
            tmp_path, "run", create_memory_store(), normalize=False
        )

        assert keys == ["run/output/chunk-0-part0.parquet"]

    async def test_a_published_writer_directory_holds_no_staging_directory(
        self, tmp_path: Path
    ) -> None:
        """The two staging layers must not publish each other.

        A writer stages its whole output and publishes at ``close()``
        (FND-317); each file inside that tree is *also* staged individually
        (FND-318), so ``.sdk-partial`` sits inside the tree the publish walks.
        Publishing it would recreate the directory in the output — and, if an
        atomic write had failed, hand a partial file the exact name the staging
        exists to withhold.
        """
        from application_sdk.storage.formats.parquet import (  # noqa: PLC0415
            ParquetFileWriter,
        )

        output = tmp_path / "out"
        # defer_uploads keeps the object store out of this: the question is what
        # ends up on local disk under the writer's output directory.
        writer = ParquetFileWriter(
            path=str(output), typename="test_type", defer_uploads=True
        )
        await writer.write(pd.DataFrame({"id": [1, 2], "value": ["a", "b"]}))
        await writer.close()

        published = sorted(
            str(p.relative_to(writer.path)) for p in Path(writer.path).rglob("*")
        )
        assert PARTIAL_DIRNAME not in published
        assert all(not name.startswith(".sdk-") for name in published), published
        assert "chunk-0-part0.parquet" in published

    def test_the_exclusion_has_exactly_one_definition(self) -> None:
        """Every walker reads the same set, so they cannot drift apart."""
        from application_sdk.common import _listing  # noqa: PLC0415
        from application_sdk.storage import batch, formats  # noqa: PLC0415

        assert batch.prune_internal_dirs is _listing.prune_internal_dirs
        assert formats.prune_internal_dirs is _listing.prune_internal_dirs
        # The writer's staging tree is the same fact, not a second one: FND-317
        # names it and this module excludes it, from one definition.
        assert formats._STAGING_ROOT_DIRNAME is _listing.WRITER_STAGING_DIRNAME
        assert _listing.INTERNAL_DIRNAMES == {
            PARTIAL_DIRNAME,
            _listing.WRITER_STAGING_DIRNAME,
        }


def test_no_sdk_writer_reintroduces_a_direct_to_final_write() -> None:
    """The write sites FND-318 converted must stay converted.

    A line-level guard rather than a behavioural one, and that is the point: a
    future edit that reaches for ``write_text``/``copy2`` at one of these sites
    reintroduces exactly the bug, and it does so silently — every existing test
    keeps passing, because a direct write is only wrong when it fails.
    """
    banned = {
        "application_sdk/common/incremental/marker.py": ("write_text(",),
        "application_sdk/common/incremental/state/incremental_diff.py": (
            "metadata_path.write_text(",
        ),
        "application_sdk/common/incremental/helpers.py": ("shutil.copy2(",),
        "application_sdk/storage/reference.py": ('+ ".sha256").write_text(',),
        # _write_statistics in formats/__init__.py wrote the statistics sidecar
        # with a bare open() at method-body indent; the pattern carries that
        # indent so a top-level helper elsewhere does not trip it.
        "application_sdk/storage/formats/__init__.py": ("\n            with open(",),
        # _write_chunk in formats/parquet.py passed the final path straight to
        # pq.write_table; the converted form writes to the atomic_path staging
        # path instead, so the banned form is write_table on `file_name`.
        "application_sdk/storage/formats/parquet.py": (
            "pq.write_table(\n                table,\n                file_name,",
        ),
    }
    root = Path(__file__).resolve().parents[3]

    offenders = [
        f"{relative}: {pattern}"
        for relative, patterns in banned.items()
        for pattern in patterns
        if pattern in (root / relative).read_text()
    ]
    assert offenders == [], (
        "direct-to-final artifact write reintroduced; use "
        "application_sdk.common.atomic instead: " + ", ".join(offenders)
    )
