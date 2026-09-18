import asyncio
import contextlib
import os
import shutil
import threading
import time
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from application_sdk import constants
from application_sdk.common.atomic import PARTIAL_DIRNAME
from application_sdk.common.file_ops import SafeFileOps
from application_sdk.infrastructure.context import (
    InfrastructureContext,
    clear_infrastructure,
    set_infrastructure,
)
from application_sdk.storage.batch import list_keys, upload_prefix
from application_sdk.storage.errors import ObjectStoreNotProvidedError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.formats.parquet import ParquetFileReader, ParquetFileWriter
from application_sdk.storage.formats.utils import path_gen
from application_sdk.storage.reference import persist_file_reference


def _published_name(where: object) -> str:
    """Filename a path handed to ``pq.write_table`` publishes under.

    Each chunk is written to ``<dir>/.sdk-partial/<name>.<token>`` and renamed
    onto ``<dir>/<name>`` once the write succeeds (FND-318), so the staged
    filename carries a token the published one does not.
    """
    staged = Path(str(where))
    if staged.parent.name != PARTIAL_DIRNAME:
        return staged.name
    return staged.name.rsplit(".", 1)[0]


def _stub_write_table(table: pa.Table, where: object, **kwargs: object) -> None:
    """Stand in for ``pq.write_table`` while still producing a file.

    A bare ``MagicMock`` writes nothing, and the writer stages each chunk and
    renames it into place (FND-318) — so a stub that produces no file makes the
    writer report, correctly, that the chunk was never written. Touching the
    path keeps these tests about *which* path the writer resolves without
    paying for real parquet encoding.

    ``where`` is a buffer rather than a path on the writer's size-estimation
    call; there is nothing to create in that case.
    """
    if isinstance(where, (str, os.PathLike)):
        Path(where).write_bytes(b"")


@pytest.fixture
def base_output_path(tmp_path: Path) -> str:
    """Create a temporary directory for tests."""
    return str(tmp_path / "test_output")


@pytest.fixture
def sample_dataframe() -> pd.DataFrame:
    """Create a sample pandas DataFrame for testing."""
    return pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5],
            "name": ["Alice", "Bob", "Charlie", "Diana", "Eve"],
            "age": [25, 30, 35, 28, 32],
            "department": ["engineering", "sales", "engineering", "marketing", "sales"],
            "year": [2023, 2023, 2024, 2024, 2023],
        }
    )


@pytest.fixture
def large_dataframe() -> pd.DataFrame:
    """Create a large pandas DataFrame for testing chunking."""
    data = {
        "id": list(range(1000)),
        "name": [f"user_{i}" for i in range(1000)],
        "value": [i * 10 for i in range(1000)],
        "category": [["A", "B", "C"][i % 3] for i in range(1000)],
    }
    return pd.DataFrame(data)


@pytest.fixture
def consolidation_dataframes() -> Generator[pd.DataFrame, None, None]:
    """Create multiple DataFrames for consolidation testing."""
    for i in range(5):  # 5 DataFrames of 300 records each = 1500 total
        df = pd.DataFrame(
            {
                "id": range(i * 300, (i + 1) * 300),
                "value": [f"batch_{i}_value_{j}" for j in range(300)],
                "category": [f"cat_{j % 3}" for j in range(300)],
                "batch_id": [i] * 300,
            }
        )
        yield df


@pytest.fixture
def mock_consolidation_files():
    """Create a reusable context manager for mocking consolidation files with proper cleanup."""

    @contextlib.contextmanager
    def _create_mock_files(base_path: str, file_names: list[str]):
        """Create temporary files and return proper mock setup."""
        temp_dir = os.path.join(base_path, f"temp_mock_{id(file_names)}")
        os.makedirs(temp_dir, exist_ok=True)

        created_files = []
        try:
            # Create mock files and return their paths
            for file_name in file_names:
                file_path = os.path.join(temp_dir, file_name)
                with open(file_path, "w") as f:
                    f.write("dummy")
                created_files.append(file_path)

            # Return a function that creates properly mocked results
            def create_mock_result(paths: list[str]):
                mock_result = MagicMock()
                mock_result.to_pydict.return_value = {"path": paths}
                return mock_result

            yield created_files, create_mock_result

        finally:
            # Cleanup
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    return _create_mock_files


class TestParquetFileWriterInit:
    """Test ParquetFileWriter initialization."""

    def test_init_default_values(self, base_output_path: str):
        """Test ParquetFileWriter initialization with default values.

        Default mode (defer_uploads=False) preserves main's path behaviour:
        when no typename is supplied, the writer uses `path` directly so
        existing apps see no surprise sub-directory.
        """
        parquet_output = ParquetFileWriter(path=base_output_path)

        # Default mode → path is unchanged when typename is absent.
        assert parquet_output.path == base_output_path
        assert parquet_output.typename is None
        assert parquet_output.defer_uploads is False

        assert parquet_output.chunk_size == 100000
        assert parquet_output.total_record_count == 0
        assert parquet_output.chunk_count == 0
        assert parquet_output.chunk_start is None
        assert parquet_output.start_marker is None
        assert parquet_output.end_marker is None
        # partition_cols was removed from the implementation

    def test_init_defer_uploads_creates_scoped_subdir(self, base_output_path: str):
        """defer_uploads=True without typename → writer-owned scoped subdir.

        Manager's /tmp concern only matters when the caller opts into the
        deferred-upload contract (because that's when close()'s FileReference
        flows through the interceptor). In that mode, the writer creates its
        own sub-directory so the resulting FileReference covers only what
        this writer wrote.
        """
        writer = ParquetFileWriter(path=base_output_path, defer_uploads=True)

        assert writer.path.startswith(base_output_path + os.sep)
        assert os.path.basename(writer.path).startswith("_parquet_")
        assert writer.path != base_output_path

    def test_init_isolates_writes_from_sibling_content(self, tmp_path):
        """defer_uploads=True scoped subdir must isolate output from siblings.

        If a caller passes a shared directory and opts into deferred uploads,
        the writer must never co-mingle its chunks with other files —
        otherwise close()'s FileReference would upload everything in the
        shared dir, not just the parquet output.
        """
        shared = tmp_path / "shared"
        shared.mkdir()
        # Pre-existing sibling file the writer must not touch.
        sibling = shared / "do_not_upload.txt"
        sibling.write_text("hands off")

        writer = ParquetFileWriter(path=str(shared), defer_uploads=True)

        # Writer chose a subdir, not the shared dir itself.
        assert writer.path != str(shared)
        assert writer.path.startswith(str(shared) + os.sep)
        # FileReference.from_local(writer.path) at the end will scope uploads
        # to writer.path — sibling stays untouched outside.
        assert sibling.exists()
        assert sibling.read_text() == "hands off"

    def test_init_custom_values(self, base_output_path: str):
        """Test ParquetFileWriter initialization with custom values."""
        parquet_output = ParquetFileWriter(
            path=os.path.join(base_output_path, "test_suffix"),
            typename="test_table",
            chunk_size=50000,
            total_record_count=100,
            chunk_count=2,
            chunk_start=10,
            start_marker="start",
            end_marker="end",
        )

        assert parquet_output.typename == "test_table"

        assert parquet_output.chunk_size == 50000
        assert parquet_output.total_record_count == 100
        assert (
            parquet_output.chunk_count == 12
        )  # chunk_start (10) + initial chunk_count (2)
        assert parquet_output.chunk_start == 10
        assert parquet_output.start_marker == "start"
        assert parquet_output.end_marker == "end"
        # partition_cols was removed from the implementation

    def test_init_creates_output_directory(self, base_output_path: str):
        """Test that initialization creates the output directory."""
        parquet_output = ParquetFileWriter(
            path=os.path.join(base_output_path, "test_dir"),
            typename="test_table",
        )

        expected_path = os.path.join(base_output_path, "test_dir", "test_table")
        assert os.path.exists(expected_path)
        assert parquet_output.path == expected_path

    def test_init_emits_deprecation_warning(self, base_output_path: str):
        """Construction must signal removal in v4.0."""
        import warnings as _warnings

        with _warnings.catch_warnings(record=True) as captured:
            _warnings.simplefilter("always")
            ParquetFileWriter(path=base_output_path, typename="t")

        messages = [
            str(w.message)
            for w in captured
            if issubclass(w.category, DeprecationWarning)
        ]
        assert any(
            "v4.0" in m for m in messages
        ), f"Expected DeprecationWarning mentioning v4.0; got: {messages}"
        assert any("ParquetFileWriter is deprecated" in m for m in messages)

    def test_init_daft_dataframe_type_emits_deprecation_and_routes_to_pandas(
        self, base_output_path: str
    ):
        """DataframeType.daft must emit DeprecationWarning and route to pandas."""
        import warnings as _warnings

        from application_sdk.common.types import DataframeType

        with _warnings.catch_warnings(record=True) as captured:
            _warnings.simplefilter("always")
            writer = ParquetFileWriter(
                path=base_output_path,
                typename="t",
                dataframe_type=DataframeType.daft,
            )

        assert writer.dataframe_type == DataframeType.pandas
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "DataframeType.daft is deprecated" in str(w.message)
            for w in captured
        )


class TestParquetFileWriterPathGen:
    """Test ParquetFileWriter path generation."""

    def test_path_gen_with_markers(self, base_output_path: str):
        """Test path generation with start and end markers."""
        path = path_gen(
            start_marker="start_123", end_marker="end_456", extension=".parquet"
        )

        assert path == "start_123_end_456.parquet"

    def test_path_gen_without_chunk_start(self, base_output_path: str):
        """Test path generation without chunk count."""
        path = path_gen(chunk_part=5, extension=".parquet")

        assert path == "5.parquet"

    def test_path_gen_with_chunk_count(self, base_output_path: str):
        """Test path generation with chunk count."""
        path = path_gen(chunk_count=10, chunk_part=3, extension=".parquet")

        assert path == "chunk-10-part3.parquet"


class TestParquetFileWriterWriteDataframe:
    """Test ParquetFileWriter pandas DataFrame writing."""

    @pytest.mark.asyncio
    async def test_write_empty_dataframe(self, base_output_path: str):
        """Test writing an empty DataFrame."""
        parquet_output = ParquetFileWriter(path=base_output_path)
        empty_df = pd.DataFrame()

        await parquet_output.write(empty_df)

        assert parquet_output.chunk_count == 0
        assert parquet_output.total_record_count == 0

    @pytest.mark.asyncio
    async def test_write_success(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Test successful DataFrame writing."""
        with patch(
            "pyarrow.parquet.write_table", side_effect=_stub_write_table
        ) as mock_write_table:
            parquet_output = ParquetFileWriter(
                path=os.path.join(base_output_path, "test"),
                use_consolidation=False,
            )

            await parquet_output.write(sample_dataframe)

            assert parquet_output.chunk_count == 1
            mock_write_table.assert_called()

    @pytest.mark.asyncio
    async def test_write_with_custom_path_gen(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Test DataFrame writing with custom path generation."""
        with patch(
            "pyarrow.parquet.write_table", side_effect=_stub_write_table
        ) as mock_write_table:
            parquet_output = ParquetFileWriter(
                path=base_output_path,
                start_marker="test_start",
                end_marker="test_end",
            )

            await parquet_output.write(sample_dataframe)

            mock_write_table.assert_called()
            call_args = mock_write_table.call_args
            file_path = call_args[0][1]  # Second positional arg is the file path
            # Mapped back through the per-file staging directory before the
            # name is read: the chunk is written to
            # `<dir>/.sdk-partial/<name>.<token>` and renamed onto `<dir>/<name>`
            # once the write succeeds (FND-318). The generated name is what this
            # test is about, and it is unchanged.
            assert "chunk-" in _published_name(file_path)
            assert _published_name(file_path).endswith(".parquet")

    @pytest.mark.asyncio
    async def test_write_error_handling(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Test error handling during DataFrame writing."""
        with patch("pyarrow.parquet.write_table") as mock_write_table:
            mock_write_table.side_effect = Exception("Test error")

            parquet_output = ParquetFileWriter(path=base_output_path)

            with pytest.raises(Exception, match="Test error"):
                await parquet_output.write(sample_dataframe)


class TestParquetFileWriterReplacePrefix:
    """Regression coverage for replacing object-store parquet prefixes."""

    @pytest.mark.asyncio
    async def test_replace_prefix_removes_stale_object_store_chunks_at_scale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A rewrite must not leak chunk-1+ files from an earlier task."""
        expected_rows = 10020
        qualified_names = [
            f"connection/db/schema/table_{i}" for i in range(expected_rows)
        ]

        staging = tmp_path / "staging"
        staging.mkdir()
        monkeypatch.setattr(constants, "TEMPORARY_PATH", str(staging))

        import application_sdk.storage.formats.utils as formats_utils

        monkeypatch.setattr(formats_utils, "TEMPORARY_PATH", str(staging))

        store = create_memory_store()
        set_infrastructure(InfrastructureContext(storage=store))
        raw_path = (
            staging / "artifacts" / "apps" / "default" / "workflows" / "wf" / "raw"
        )
        table_path = raw_path / "table"

        try:
            first_writer = ParquetFileWriter(
                path=str(raw_path),
                typename="table",
                buffer_size=5000,
                retain_local_copy=False,
            )
            await first_writer.write(
                pd.DataFrame(
                    {
                        "qualifiedName": qualified_names[:3],
                        "raw_shape_only": ["raw"] * 3,
                    }
                )
            )
            await first_writer.write(
                pd.DataFrame(
                    {
                        "qualifiedName": qualified_names[3:4],
                        "raw_shape_only": ["raw"],
                    }
                )
            )
            await first_writer.write(
                pd.DataFrame(
                    {
                        "qualifiedName": qualified_names[4:],
                        "raw_shape_only": ["raw"] * len(qualified_names[4:]),
                    }
                )
            )
            await first_writer.close()
            await upload_prefix(str(table_path), str(table_path), store)

            first_keys = await list_keys(str(table_path), store, suffix=".parquet")
            assert any("/chunk-1-part0.parquet" in key for key in first_keys)
            assert any("/chunk-2-part0.parquet" in key for key in first_keys)

            shutil.rmtree(table_path, ignore_errors=True)

            second_writer = ParquetFileWriter(
                path=str(raw_path),
                typename="table",
                buffer_size=5000,
                retain_local_copy=False,
                replace_prefix=True,
            )
            await second_writer.write(
                pd.DataFrame(
                    {
                        "qualifiedName": qualified_names,
                        "is_partitioned": [False] * expected_rows,
                    }
                )
            )
            await second_writer.close()
            await upload_prefix(str(table_path), str(table_path), store)

            second_keys = await list_keys(str(table_path), store, suffix=".parquet")
            assert len(second_keys) == 3
            assert all("/chunk-0-" in key for key in second_keys)

            shutil.rmtree(table_path, ignore_errors=True)

            reader = ParquetFileReader(path=str(table_path))
            try:
                result = await reader.read()
            finally:
                await reader.close()

            assert len(result) == expected_rows
            assert result["qualifiedName"].nunique() == expected_rows
            assert "is_partitioned" in result.columns
            assert "raw_shape_only" not in result.columns
        finally:
            clear_infrastructure()


class TestParquetFileWriterCloseContract:
    """End-to-end verification of the opt-in close() → WriterResult contract.

    All tests in this class use ``defer_uploads=True`` because the contract
    under test (deferred uploads, ephemeral FileReference on close) is opt-in.
    Apps that do not pass the flag get main's inline-upload behaviour and a
    ``result.files`` of ``None``.
    """

    @pytest.mark.asyncio
    async def test_close_returns_writer_result_with_filereference(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """close() must hand back statistics + an ephemeral FileReference."""
        writer = ParquetFileWriter(
            path=base_output_path, typename="users", defer_uploads=True
        )
        await writer.write(sample_dataframe)
        result = await writer.close()

        # WriterResult subclasses TaskStatistics — fields are direct.
        assert result.total_record_count == len(sample_dataframe)
        assert result.typename == "users"
        assert result.files.local_path == writer.path
        assert result.files.is_durable is False

        # And it's surfaced via last_result for async-with callers.
        assert writer.last_result is result

    @pytest.mark.asyncio
    async def test_default_mode_returns_no_filereference(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """defer_uploads=False (default) → result.files is None.

        Apps on the legacy inline-upload path should never see a FileReference
        in their result. Surfacing one would cause the activity interceptor
        to re-upload files that are already in the store.
        """
        with patch(
            "application_sdk.storage.formats._upload_file", new_callable=AsyncMock
        ):
            writer = ParquetFileWriter(path=base_output_path, typename="users")
            await writer.write(sample_dataframe)
            result = await writer.close()

        assert result.total_record_count == len(sample_dataframe)
        assert result.files is None

    @pytest.mark.asyncio
    async def test_close_then_persist_file_reference_uploads_full_output(
        self, tmp_path: Path
    ):
        """The 'trivial' caller pattern: close() then persist the ref.

        Mirrors the docstring example — no caller-side upload_prefix /
        upload_file boilerplate, just persist the returned FileReference.
        Validates that every parquet chunk plus the statistics sidecar
        appear in the store under the persisted prefix.
        """
        writer = ParquetFileWriter(
            path=str(tmp_path / "out"),
            typename="orders",
            buffer_size=50,
            defer_uploads=True,
        )
        # 120 rows -> 3 sub-chunks (50+50+20) -> HYP-773 territory.
        df = pd.DataFrame({"id": list(range(120))})
        await writer.write(df)
        result = await writer.close()

        store = create_memory_store()
        durable = await persist_file_reference(store, result.files)
        assert durable.is_durable is True
        assert durable.storage_path is not None

        parquet_keys = await list_keys(durable.storage_path, store, suffix=".parquet")
        assert len(parquet_keys) >= 3  # at least one per sub-chunk

        # Statistics sidecar landed inside the persisted prefix too — no
        # separate handoff needed by the caller.
        all_keys = await list_keys(durable.storage_path, store)
        assert any("statistics" in k for k in all_keys)

    @pytest.mark.asyncio
    async def test_no_inline_uploads_during_write_when_deferred(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """defer_uploads=True must skip every inline upload site.

        Guards against regression in the deferred path where some flush
        sites might leak an inline upload.
        """
        with (
            patch(
                "application_sdk.storage.formats._upload_file",
                new_callable=AsyncMock,
            ) as base_upload,
            patch(
                "application_sdk.storage.formats.parquet._upload_file",
                new_callable=AsyncMock,
            ) as parquet_upload,
            patch(
                "application_sdk.storage.formats.parquet._delete_prefix",
                new_callable=AsyncMock,
            ) as delete_prefix,
        ):
            writer = ParquetFileWriter(
                path=base_output_path, typename="t", defer_uploads=True
            )
            await writer.write(sample_dataframe)
            await writer.close()

        base_upload.assert_not_called()
        parquet_upload.assert_not_called()
        delete_prefix.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_buffer_swallows_only_object_store_not_provided_error(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """The 'no object store configured' local-dev error must still be
        swallowed (logged at WARNING) in _flush_buffer — the writer keeps
        the local parquet and the flush returns without raising.
        """
        writer = ParquetFileWriter(path=base_output_path, typename="t")
        # Stub the writer's own _upload_file to raise the exact exception
        # type _resolve_store raises for local dev. _flush_buffer's
        # inline-upload site must swallow it.
        writer._upload_file = AsyncMock(  # type: ignore[method-assign]
            side_effect=ObjectStoreNotProvidedError()
        )
        # _flush_buffer writes a local parquet then attempts inline upload.
        # With the narrow catch, this returns normally.
        await writer._flush_buffer(sample_dataframe, chunk_part=0)
        # Base _flush_buffer bumped the counter for what it wrote locally;
        # nothing was thrown, so the writer accepted the flush.
        assert writer.total_record_count == len(sample_dataframe)

    @pytest.mark.asyncio
    async def test_flush_buffer_propagates_non_object_store_not_provided_error(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Any error other than ObjectStoreNotProvidedError must propagate
        out of _flush_buffer so the activity fails loudly instead of
        reporting a rowcount for chunks that never reached the object
        store.

        Regression test — a prior implementation caught every
        RuntimeError (via string-matching a substring of the message),
        which silently masked transient object-store upload failures.
        That let statistics.json report more rows than actually landed
        in blob storage, tripping downstream diff-delete logic.
        """
        writer = ParquetFileWriter(path=base_output_path, typename="t")
        writer._upload_file = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("obstore: connection reset by peer")
        )
        with pytest.raises(RuntimeError, match="connection reset by peer"):
            await writer._flush_buffer(sample_dataframe, chunk_part=0)


class TestParquetFileWriterMetrics:
    """Test ParquetFileWriter metrics recording."""

    @pytest.mark.asyncio
    async def test_pandas_write_metrics(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Test that metrics are recorded for pandas DataFrame writes."""
        with (
            patch(
                "application_sdk.storage.formats.parquet.get_metrics"
            ) as mock_get_metrics,
            # Stub inline upload — no object store configured in this test.
            patch(
                "application_sdk.storage.formats._upload_file",
                new_callable=AsyncMock,
            ),
        ):
            mock_metrics = MagicMock()
            mock_get_metrics.return_value = mock_metrics

            parquet_output = ParquetFileWriter(path=base_output_path)

            await parquet_output.write(sample_dataframe)

            assert mock_metrics.record_metric.call_count >= 2


class TestParquetFileWriterConsolidation:
    """Test ParquetFileWriter consolidation functionality."""

    def test_consolidation_init_attributes(self, base_output_path: str):
        """Test that consolidation attributes are properly initialized."""
        parquet_output = ParquetFileWriter(
            path=base_output_path,
            chunk_size=1000,
            buffer_size=200,
            use_consolidation=True,
        )

        # Check consolidation attributes
        assert parquet_output.use_consolidation is True
        assert parquet_output.consolidation_threshold == 1000  # Should equal chunk_size
        assert parquet_output.current_folder_records == 0
        assert parquet_output.temp_folder_index == 0
        assert parquet_output.temp_folders_created == []
        assert parquet_output.current_temp_folder_path is None

    def test_consolidation_init_with_none_chunk_size(self, base_output_path: str):
        """Test consolidation threshold when chunk_size is None."""
        parquet_output = ParquetFileWriter(
            path=base_output_path, chunk_size=None, buffer_size=200
        )

        # Should default to 100000 when chunk_size is None
        assert parquet_output.consolidation_threshold == 100000

    def test_temp_folder_path_generation(self, base_output_path: str):
        """Test temp folder path generation."""
        parquet_output = ParquetFileWriter(
            path=os.path.join(base_output_path, "test_suffix"),
            typename="test_type",
        )

        # Test temp folder path generation. The accumulation tree hangs off the
        # writer's own token-named staging root, so no two writers can share an
        # accumulation directory (FND-315) and nothing left there by a
        # cancelled attempt sits inside the output directory (FND-317).
        temp_path = parquet_output._get_temp_folder_path(0)
        expected_path = os.path.join(
            parquet_output._scratch_root,
            "temp_accumulation",
            "folder-0",
        )
        assert temp_path == expected_path
        assert parquet_output._scratch_root.startswith(
            os.path.join(base_output_path, "test_suffix", ".sdk-writer-staging")
            + os.sep
        )

    def test_consolidated_file_path_generation(self, base_output_path: str):
        """Test consolidated file path generation."""
        parquet_output = ParquetFileWriter(
            path=os.path.join(base_output_path, "test_suffix"),
            typename="test_type",
        )

        # A consolidated file carries its published name from the moment it is
        # written; only the directory is private until close() publishes it.
        consolidated_path = parquet_output._get_consolidated_file_path(
            folder_index=0, chunk_part=0
        )
        assert consolidated_path == os.path.join(
            parquet_output._write_root, "chunk-0-part0.parquet"
        )
        assert parquet_output._published_path(consolidated_path) == os.path.join(
            base_output_path, "test_suffix", "test_type", "chunk-0-part0.parquet"
        )

    def test_start_new_temp_folder(self, base_output_path: str):
        """Test starting a new temp folder."""
        parquet_output = ParquetFileWriter(path=base_output_path)

        # Initially no temp folder
        assert parquet_output.current_temp_folder_path is None
        assert parquet_output.temp_folder_index == 0

        # Start first temp folder
        parquet_output._start_new_temp_folder()

        assert parquet_output.temp_folder_index == 0
        assert parquet_output.current_folder_records == 0
        assert parquet_output.current_temp_folder_path is not None
        assert os.path.exists(parquet_output.current_temp_folder_path)

        # Start second temp folder
        first_folder_path = parquet_output.current_temp_folder_path
        parquet_output._start_new_temp_folder()

        assert parquet_output.temp_folder_index == 1
        assert parquet_output.current_temp_folder_path != first_folder_path
        assert (
            0 in parquet_output.temp_folders_created
        )  # Previous folder should be tracked

    @pytest.mark.asyncio
    async def test_write_chunk_to_temp_folder(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Test writing chunk to temp folder."""
        parquet_output = ParquetFileWriter(path=base_output_path)

        # Start temp folder first
        parquet_output._start_new_temp_folder()

        # Write chunk
        await parquet_output._write_chunk_to_temp_folder(sample_dataframe)

        # Check that file was created
        assert parquet_output.current_temp_folder_path is not None
        temp_folder = parquet_output.current_temp_folder_path
        files = [f for f in os.listdir(temp_folder) if f.endswith(".parquet")]
        assert len(files) == 1
        assert files[0] == "chunk-0.parquet"

        # Write another chunk
        await parquet_output._write_chunk_to_temp_folder(sample_dataframe)

        files = [f for f in os.listdir(temp_folder) if f.endswith(".parquet")]
        assert len(files) == 2
        assert "chunk-1.parquet" in files

    @pytest.mark.asyncio
    async def test_write_chunk_to_temp_folder_no_path(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Test writing chunk to temp folder when no path is set."""
        parquet_output = ParquetFileWriter(path=base_output_path)

        # Should raise error when no temp folder path is set
        from application_sdk.storage.formats.format_errors import (
            TempFolderPathMissingError,
        )

        with pytest.raises(TempFolderPathMissingError) as exc_info:
            await parquet_output._write_chunk_to_temp_folder(sample_dataframe)
        assert exc_info.value.code == "INTERNAL_FORMAT_TEMP_FOLDER_PATH_MISSING"

    @pytest.mark.asyncio
    async def test_consolidate_empty_folder(self, base_output_path: str):
        """Test consolidating when folder is empty."""
        parquet_output = ParquetFileWriter(path=base_output_path)
        parquet_output.current_folder_records = 0
        parquet_output.current_temp_folder_path = None

        # Should return early without doing anything
        await parquet_output._consolidate_current_folder()

        assert parquet_output.chunk_count == 0
        assert parquet_output.total_record_count == 0

    @pytest.mark.asyncio
    async def test_cleanup_temp_folders(self, base_output_path: str):
        """Test cleanup of temp folders."""
        parquet_output = ParquetFileWriter(path=base_output_path)

        # Create multiple temp folders
        parquet_output._start_new_temp_folder()
        assert parquet_output.current_temp_folder_path is not None
        first_folder = parquet_output.current_temp_folder_path
        parquet_output._start_new_temp_folder()
        assert parquet_output.current_temp_folder_path is not None
        second_folder = parquet_output.current_temp_folder_path

        # Both folders should exist
        assert os.path.exists(first_folder)
        assert os.path.exists(second_folder)

        # Cleanup
        await parquet_output._cleanup_temp_folders()

        # Folders should be removed
        assert not os.path.exists(first_folder)
        assert not os.path.exists(second_folder)

        # State should be reset
        assert parquet_output.temp_folders_created == []
        assert parquet_output.current_temp_folder_path is None
        assert parquet_output.temp_folder_index == 0
        assert parquet_output.current_folder_records == 0

    @pytest.mark.asyncio
    async def test_temp_folder_removal_offloaded_to_thread(self, base_output_path: str):
        """Temp-folder rmtree must not run inline on the event loop.

        An accumulation folder holds a run's worth of parquet chunks, so
        removing it inline stalls every other coroutine — including the
        enclosing @task's auto-heartbeat — for the removal's full duration.
        """
        parquet_output = ParquetFileWriter(path=base_output_path)
        parquet_output._start_new_temp_folder()
        assert parquet_output.current_temp_folder_path is not None
        folder = parquet_output.current_temp_folder_path

        with patch(
            "application_sdk.storage.formats.parquet.run_in_thread",
            new_callable=AsyncMock,
            side_effect=lambda func, *a, **kw: func(*a, **kw),
        ) as mock_offload:
            await parquet_output._cleanup_temp_folders()

        assert mock_offload.await_args_list, "temp-folder removal was not offloaded"
        assert mock_offload.await_args_list[0].args[0] is SafeFileOps.rmtree
        assert not os.path.exists(folder)

    @pytest.mark.asyncio
    async def test_write_batches_without_consolidation(self, base_output_path: str):
        """Test write_batches with consolidation disabled."""
        parquet_output = ParquetFileWriter(path=base_output_path)
        parquet_output.use_consolidation = False

        def create_test_dataframes():
            df = pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]})
            yield df

        # Mock the super() call to avoid actual file operations
        with patch(
            "application_sdk.storage.formats.Writer.write_batches"
        ) as mock_base_method:
            mock_base_method.return_value = AsyncMock()

            await parquet_output.write_batches(create_test_dataframes())

            # Should have called base class method
            mock_base_method.assert_called_once()

    @pytest.mark.asyncio
    async def test_accumulate_dataframe(self, base_output_path: str):
        """Test accumulating DataFrame into temp folders."""
        parquet_output = ParquetFileWriter(
            path=base_output_path,
            chunk_size=500,  # This sets consolidation_threshold internally
            buffer_size=100,
        )

        # Create a DataFrame that will trigger folder creation and consolidation
        large_df = pd.DataFrame(
            {
                "id": range(600),  # 600 records > 500 threshold
                "value": [f"value_{i}" for i in range(600)],
            }
        )

        with (
            patch.object(
                parquet_output, "_consolidate_current_folder"
            ) as mock_consolidate,
            patch.object(
                parquet_output,
                "_start_new_temp_folder",
                wraps=parquet_output._start_new_temp_folder,
            ) as mock_start_folder,
            patch.object(parquet_output, "_write_chunk") as mock__write_chunk,
        ):
            mock_consolidate.return_value = AsyncMock()
            mock__write_chunk.return_value = AsyncMock()

            await parquet_output._accumulate_dataframe(large_df)

            # Should have triggered consolidation due to size
            mock_consolidate.assert_called()
            mock_start_folder.assert_called()

    @pytest.mark.asyncio
    async def test_consolidation_error_handling(self, base_output_path: str):
        """Test error handling in consolidation with cleanup."""
        parquet_output = ParquetFileWriter(
            path=base_output_path, use_consolidation=True
        )

        def create_test_dataframes():
            df = pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]})
            yield df

        # Mock _accumulate_dataframe to raise an exception
        with (
            patch.object(parquet_output, "_accumulate_dataframe") as mock_accumulate,
            patch.object(parquet_output, "_cleanup_temp_folders") as mock_cleanup,
        ):
            mock_accumulate.side_effect = Exception("Test error")
            mock_cleanup.return_value = AsyncMock()

            # Should raise the exception and call cleanup
            with pytest.raises(Exception, match="Test error"):
                await parquet_output.write_batches(create_test_dataframes())

            mock_cleanup.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_generator_support(self, base_output_path: str):
        """Test that consolidation works with async generators."""
        parquet_output = ParquetFileWriter(
            path=base_output_path, use_consolidation=True
        )

        async def create_async_dataframes():
            for i in range(2):
                df = pd.DataFrame(
                    {
                        "id": range(i * 100, (i + 1) * 100),
                        "value": [f"value_{j}" for j in range(100)],
                    }
                )
                yield df

        with (
            patch.object(parquet_output, "_accumulate_dataframe") as mock_accumulate,
            patch.object(parquet_output, "_cleanup_temp_folders") as mock_cleanup,
        ):
            mock_accumulate.return_value = AsyncMock()
            mock_cleanup.return_value = AsyncMock()

            await parquet_output.write_batches(create_async_dataframes())

            # Should have called accumulate for each DataFrame
            assert mock_accumulate.call_count == 2
            mock_cleanup.assert_called_once()

    @pytest.mark.asyncio
    async def test_consolidate_current_folder_pyarrow(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Consolidation writes combined DataFrame chunks to final location."""
        parquet_output = ParquetFileWriter(path=base_output_path, buffer_size=3)
        parquet_output._start_new_temp_folder()
        parquet_output.current_folder_records = len(sample_dataframe)

        # Write sample data to the temp folder
        assert parquet_output.current_temp_folder_path is not None
        temp_file = os.path.join(
            parquet_output.current_temp_folder_path, "chunk-0.parquet"
        )
        sample_dataframe.to_parquet(temp_file, index=False)

        with patch(
            "application_sdk.storage.formats.parquet._upload_file",
            new_callable=AsyncMock,
        ):
            await parquet_output._consolidate_current_folder()

        assert parquet_output.chunk_count == 1
        assert parquet_output.total_record_count == len(sample_dataframe)
        assert len(parquet_output.partitions) == 1

    @pytest.mark.asyncio
    async def test_consolidation_end_to_end_persist(self, tmp_path: Path):
        """`use_consolidation=True` + `defer_uploads=True` reach the store via close().

        Exercises the full chain: many small DataFrames → pyarrow consolidation
        → close() → persist → store has the consolidated keys.
        """
        writer = ParquetFileWriter(
            path=str(tmp_path / "out"),
            typename="orders",
            chunk_size=200,
            buffer_size=50,
            use_consolidation=True,
            defer_uploads=True,
        )

        async def _batches():
            # 3 DataFrames of 100 records each = 300 total.
            # consolidation_threshold=200 → one consolidation at ~200,
            # final consolidation at the end with the remaining 100.
            for i in range(3):
                yield pd.DataFrame(
                    {
                        "id": list(range(i * 100, (i + 1) * 100)),
                        "batch": [i] * 100,
                    }
                )

        await writer.write_batches(_batches())
        result = await writer.close()

        assert result.total_record_count == 300
        # Local consolidated files must exist on disk before persist.
        local_parquet = list(Path(writer.path).rglob("*.parquet"))
        assert local_parquet, "Consolidation produced no local parquet files"

        store = create_memory_store()
        durable = await persist_file_reference(store, result.files)
        assert durable.is_durable is True

        # Every consolidated file lands in the store under the persisted prefix.
        store_keys = await list_keys(durable.storage_path, store, suffix=".parquet")
        assert len(store_keys) == len(local_parquet), (
            f"Store key count {len(store_keys)} != local file count "
            f"{len(local_parquet)} — consolidation upload boundary broken"
        )

    @pytest.mark.asyncio
    async def test_write_batches_with_consolidation(self, base_output_path: str):
        """write_batches() with consolidation accumulates, consolidates, and cleans up.

        3 × 200-row DataFrames with consolidation_threshold=500 triggers one
        mid-stream consolidation at 600 records and a final flush, then the
        temp_accumulation dir is removed.
        """
        parquet_output = ParquetFileWriter(
            path=base_output_path,
            chunk_size=500,
            buffer_size=100,
            use_consolidation=True,
            defer_uploads=True,
        )

        def create_test_dataframes():
            for i in range(3):  # 3 × 200 = 600 total
                yield pd.DataFrame(
                    {
                        "id": list(range(i * 200, (i + 1) * 200)),
                        "value": [f"value_{j}" for j in range(200)],
                        "batch": [i] * 200,
                    }
                )

        await parquet_output.write_batches(create_test_dataframes())

        assert parquet_output.total_record_count == 600
        assert parquet_output.chunk_count >= 1

        temp_base = parquet_output._get_temp_base_path()
        assert not os.path.exists(temp_base) or not os.listdir(temp_base)

    @pytest.mark.asyncio
    async def test_multiple_write_batched_calls_with_consolidation(
        self, base_output_path: str
    ):
        """Two sequential write_batches() calls on the same writer accumulate correctly.

        Each call produces 4 consolidations (chunk_size=100, buffer_size=50,
        4 × 60-row batches). After both calls: 480 total records, ≥ 8 partitions.
        """
        parquet_output = ParquetFileWriter(
            path=base_output_path,
            chunk_size=100,
            buffer_size=50,
            use_consolidation=True,
            defer_uploads=True,
        )

        def make_batches(start: int):
            for i in range(4):  # 4 × 60 = 240 rows per call
                yield pd.DataFrame(
                    {"id": list(range(start + i * 60, start + (i + 1) * 60))}
                )

        await parquet_output.write_batches(make_batches(0))
        assert parquet_output.total_record_count == 240

        await parquet_output.write_batches(make_batches(1000))
        assert parquet_output.total_record_count == 480
        assert parquet_output.chunk_count >= 4
        assert len(parquet_output.partitions) >= 4

    @pytest.mark.asyncio
    async def test_consolidation_with_very_small_buffer_multiple_chunks(
        self, base_output_path: str
    ):
        """Very small buffer_size forces many temp chunks; consolidation still correct.

        buffer_size=10 with consolidation_threshold=200 and a single 250-row DataFrame
        triggers exactly 2 consolidations: one at 200 records mid-stream, one final
        flush of the remaining 50 on write_batches() completion.
        """
        parquet_output = ParquetFileWriter(
            path=base_output_path,
            chunk_size=200,
            buffer_size=10,
            use_consolidation=True,
            defer_uploads=True,
        )

        await parquet_output.write_batches(
            iter([pd.DataFrame({"id": list(range(250))})])
        )

        assert parquet_output.total_record_count == 250
        assert parquet_output.chunk_count == 2


class TestWriteChunkNullColumns:
    """Tests for _write_chunk handling of null-typed columns (CNCT-80).

    The old behavior (see git history, BLDX-837) cast an all-null column to
    large_string on write — a workaround for a daft merge bug. daft was
    removed entirely in #2300, but the cast stayed, which broke merging an
    all-null shard against a sibling shard typed e.g. double (CNCT-80): a
    permissive pa.concat_tables can promote null -> double directly, but
    can't reconcile large_string against double. Leaving the column typed
    null lets that promotion happen.
    """

    async def test_write_chunk_no_nulls_uses_fast_path(self, tmp_path) -> None:
        """Normal DataFrame without all-null columns uses pandas fast path."""
        df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        file_name = str(tmp_path / "no_nulls.parquet")

        writer = ParquetFileWriter(str(tmp_path / "output"))
        await writer._write_chunk(df, file_name)

        table = pq.read_table(file_name)
        assert table.num_rows == 3
        assert table.column("b").type == pa.string()

    async def test_write_chunk_all_null_column_stays_null_typed(self, tmp_path) -> None:
        """All-null column is written with its natural null type, uncast."""
        df = pd.DataFrame({"a": [1, 2], "b": [None, None]})
        file_name = str(tmp_path / "with_nulls.parquet")

        writer = ParquetFileWriter(str(tmp_path / "output"))
        await writer._write_chunk(df, file_name)

        schema = pq.read_schema(file_name)
        b_type = schema.field("b").type
        assert b_type == pa.null(), f"Expected null, got {b_type}"

    async def test_write_chunk_mixed_null_and_data_columns(self, tmp_path) -> None:
        """DataFrame with both null and non-null columns handles both correctly."""
        df = pd.DataFrame(
            {"id": [1, 2], "name": ["Alice", "Bob"], "notes": [None, None]}
        )
        file_name = str(tmp_path / "mixed.parquet")

        writer = ParquetFileWriter(str(tmp_path / "output"))
        await writer._write_chunk(df, file_name)

        schema = pq.read_schema(file_name)
        assert schema.field("name").type == pa.string()
        assert schema.field("notes").type == pa.null()

    async def test_multi_file_roundtrip_no_data_loss(self, tmp_path) -> None:
        """Write two files (one with null col, one with string data) — reading both back preserves data."""
        writer = ParquetFileWriter(str(tmp_path / "output"))

        # File 1: column 'extra' is all-null
        df1 = pd.DataFrame({"id": [1, 2], "extra": [None, None]})
        f1 = str(tmp_path / "chunk1.parquet")
        await writer._write_chunk(df1, f1)

        # File 2: column 'extra' has data
        df2 = pd.DataFrame({"id": [3, 4], "extra": ["foo", "bar"]})
        f2 = str(tmp_path / "chunk2.parquet")
        await writer._write_chunk(df2, f2)

        # Read both and concat — should not lose data
        t1 = pq.read_table(f1)
        t2 = pq.read_table(f2)
        combined = pa.concat_tables([t1, t2], promote_options="permissive")
        assert combined.num_rows == 4
        extra_col = combined.column("extra").to_pylist()
        assert extra_col == [None, None, "foo", "bar"]

    async def test_multi_file_roundtrip_null_vs_numeric_no_longer_crashes(
        self, tmp_path
    ) -> None:
        """CNCT-80 regression: all-null shard merged against a numeric shard.

        This is the production failure signature: 'Unable to merge: Field X
        has incompatible types: double vs large_string'. Before this fix, the
        writer cast the all-null 'extra' column to large_string, which a
        permissive concat cannot reconcile against the sibling shard's
        double. Leaving it null-typed lets permissive promote null -> double.
        """
        writer = ParquetFileWriter(str(tmp_path / "output"))

        # File 1: numeric metadata column entirely NULL (e.g. a batch of only
        # non-numeric source columns).
        df1 = pd.DataFrame({"id": [1, 2], "extra": [None, None]})
        f1 = str(tmp_path / "chunk1.parquet")
        await writer._write_chunk(df1, f1)

        # File 2: same column populated with real numeric values.
        df2 = pd.DataFrame({"id": [3, 4], "extra": [10, 18]})
        f2 = str(tmp_path / "chunk2.parquet")
        await writer._write_chunk(df2, f2)

        t1 = pq.read_table(f1)
        t2 = pq.read_table(f2)
        combined = pa.concat_tables([t1, t2], promote_options="permissive")
        assert combined.num_rows == 4
        assert combined.column("extra").to_pylist() == [None, None, 10, 18]

    async def test_write_chunk_offloaded_to_thread(self, tmp_path) -> None:
        """Chunk conversion and the disk write must not run on the event loop.

        A large chunk's Arrow conversion plus disk write can take long enough
        to stall every other coroutine — including the enclosing @task's
        auto-heartbeat — for the operation's full duration (ADR-0010).

        Asserts the thread the work actually lands on rather than which
        callable was handed to run_in_thread, so the guarantee survives the
        blocking section being regrouped.
        """
        df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        file_name = str(tmp_path / "offloaded.parquet")
        writer = ParquetFileWriter(str(tmp_path / "output"))

        loop_thread = threading.current_thread().name
        write_thread: list[str] = []
        real_write_table = pq.write_table

        def _slow_write_table(*args, **kwargs):
            write_thread.append(threading.current_thread().name)
            # Stand in for a large chunk's write cost, so a regression that
            # puts this back on the loop actually stalls the ticker below.
            time.sleep(0.3)
            return real_write_table(*args, **kwargs)

        ticks = 0

        async def _ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        with patch("pyarrow.parquet.write_table", side_effect=_slow_write_table):
            ticker = asyncio.create_task(_ticker())
            try:
                await writer._write_chunk(df, file_name)
            finally:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker

        assert write_thread, "pq.write_table was never called"
        assert write_thread[0] != loop_thread, "the write ran on the event loop"
        assert write_thread[0].startswith(
            "sdk-blocking-"
        ), f"the write ran on {write_thread[0]}, not the SDK blocking pool"
        # The loop kept running coroutines for the whole write — which is what
        # lets the enclosing @task's auto-heartbeat keep beating.
        assert ticks >= 5, f"event loop stalled during the write ({ticks} ticks)"
        table = pq.read_table(file_name)
        assert table.num_rows == 3


class TestConsolidationFileCount:
    """FND-1339: a consolidated chunk is one parquet file, not chunk/buffer files.

    Consolidation exists to turn the per-``buffer_size`` accumulation chunks
    into one file per ``chunk_size`` rows. The loop that wrote the consolidated
    output sliced by ``buffer_size`` again, so every chunk became
    ``chunk_size / buffer_size`` objects (20 at the defaults) — and every one of
    them cost the hand-off a set of round trips.
    """

    @pytest.mark.asyncio
    async def test_one_chunk_consolidates_to_one_file(self, tmp_path: Path):
        writer = ParquetFileWriter(
            path=str(tmp_path / "out"),
            typename="rows",
            chunk_size=1000,
            buffer_size=10,
            use_consolidation=True,
            defer_uploads=True,
        )

        await writer.write_batches(
            iter([pd.DataFrame({"id": list(range(1000)), "v": ["x"] * 1000})])
        )
        result = await writer.close()

        assert result.total_record_count == 1000
        assert writer.chunk_count == 1
        assert writer.partitions == [1]
        files = sorted(Path(writer.path).rglob("*.parquet"))
        assert len(files) == 1, [f.name for f in files]
        assert len(pd.read_parquet(files[0])) == 1000

    @pytest.mark.asyncio
    async def test_two_chunks_consolidate_to_two_files(self, tmp_path: Path):
        writer = ParquetFileWriter(
            path=str(tmp_path / "out"),
            typename="rows",
            chunk_size=200,
            buffer_size=10,
            use_consolidation=True,
            defer_uploads=True,
        )

        await writer.write_batches(iter([pd.DataFrame({"id": list(range(250))})]))
        await writer.close()

        assert writer.chunk_count == 2
        assert writer.partitions == [1, 1]
        files = sorted(Path(writer.path).rglob("*.parquet"))
        assert len(files) == 2, [f.name for f in files]
        assert sum(len(pd.read_parquet(f)) for f in files) == 250

    @pytest.mark.asyncio
    async def test_max_file_size_still_splits_a_wide_chunk(self, tmp_path: Path):
        writer = ParquetFileWriter(
            path=str(tmp_path / "out"),
            typename="rows",
            chunk_size=1000,
            buffer_size=100,
            use_consolidation=True,
            defer_uploads=True,
        )
        # A cap far below one chunk's in-memory size forces the split the cap
        # exists for; it is the only thing that may split a consolidated chunk.
        writer.max_file_size_bytes = 4 * 1024

        await writer.write_batches(
            iter([pd.DataFrame({"id": list(range(1000)), "v": ["y" * 20] * 1000})])
        )
        await writer.close()

        assert writer.chunk_count == 1
        assert writer.partitions[0] > 1
        files = sorted(Path(writer.path).rglob("*.parquet"))
        assert len(files) == writer.partitions[0]
        assert sum(len(pd.read_parquet(f)) for f in files) == 1000
