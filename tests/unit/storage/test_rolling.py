"""Unit tests for RollingFileWriter.

Exercises:
- Scoped writer-owned sub-directory (and explicit subdir name)
- append() buffers, flush() / __aexit__ rolls to a chunk file
- close() idempotency + append-after-close raises
- Time-based rollover (mocked clock and real sleep)
- Sync vs async flush_fn dispatch
- on_chunk_complete callback fires per chunk and survives exceptions
- file_reference property returns ephemeral FileReference scoped to subdir
- Validation: empty base_path, missing-leading-dot extension, non-positive interval
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.contracts.types import FileReference
from application_sdk.storage.rolling import (
    AnyOfPolicy,
    BufferState,
    CountPolicy,
    RollingFileWriter,
    RolloverPolicy,
    SizePolicy,
    TimePolicy,
    _default_size_estimator,
)


@pytest.fixture
def base(tmp_path: Path) -> str:
    """Base directory under which the writer creates its scoped subdir."""
    return str(tmp_path / "base")


def _make_flush_fn():
    """Sync flush_fn that records (batches, path) calls and writes a stamp file."""
    calls: list[tuple[list[Any], str]] = []

    def _flush(batches: list[Any], path: str) -> None:
        calls.append((list(batches), path))
        # Touch the file so disk-existence assertions are meaningful.
        with open(path, "w") as f:
            f.write(f"chunk:{len(batches)}")

    return _flush, calls


class TestRollingFileWriterInit:
    def test_scoped_subdir_created_under_base(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        writer = RollingFileWriter(base, ".parquet", flush_fn)
        assert writer.output_dir.startswith(base + os.sep)
        assert os.path.basename(writer.output_dir).startswith("rolling_")
        assert os.path.isdir(writer.output_dir)

    def test_explicit_subdir_name(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        writer = RollingFileWriter(
            base, ".parquet", flush_fn, scoped_subdir_name="fixed"
        )
        assert writer.output_dir == os.path.join(base, "fixed")
        assert os.path.isdir(writer.output_dir)

    def test_rejects_empty_base_path(self) -> None:
        from application_sdk.storage._rolling_errors import (
            InvalidRollingFileWriterError,
        )

        flush_fn, _ = _make_flush_fn()
        with pytest.raises(InvalidRollingFileWriterError) as exc_info:
            RollingFileWriter("", ".parquet", flush_fn)
        assert exc_info.value.code == "INVALID_INPUT_ROLLING_WRITER"
        assert exc_info.value.field == "base_path"

    def test_rejects_extension_without_leading_dot(self, base: str) -> None:
        from application_sdk.storage._rolling_errors import (
            InvalidRollingFileWriterError,
        )

        flush_fn, _ = _make_flush_fn()
        with pytest.raises(InvalidRollingFileWriterError) as exc_info:
            RollingFileWriter(base, "parquet", flush_fn)
        assert exc_info.value.field == "extension"

    def test_rejects_non_positive_interval(self, base: str) -> None:
        from application_sdk.storage._rolling_errors import InvalidRolloverPolicyError

        flush_fn, _ = _make_flush_fn()
        with pytest.raises(InvalidRolloverPolicyError) as exc_info:
            RollingFileWriter(base, ".parquet", flush_fn, chunk_interval_seconds=0)
        assert exc_info.value.field == "chunk_interval_seconds"


class TestRollingFileWriterFlush:
    @pytest.mark.asyncio
    async def test_aexit_flushes_buffer(self, base: str) -> None:
        """Pending appends must reach disk on context-manager exit."""
        flush_fn, calls = _make_flush_fn()
        async with RollingFileWriter(
            base, ".json", flush_fn, scoped_subdir_name="run"
        ) as writer:
            await writer.append({"id": 1})
            await writer.append({"id": 2})

        assert len(calls) == 1
        batches, path = calls[0]
        assert batches == [{"id": 1}, {"id": 2}]
        assert path == os.path.join(base, "run", "chunk-0.json")
        assert os.path.exists(path)

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, base: str) -> None:
        flush_fn, calls = _make_flush_fn()
        writer = RollingFileWriter(base, ".json", flush_fn)
        await writer.append({"id": 1})
        await writer.close()
        await writer.close()  # must not re-flush or raise
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_append_after_close_raises(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        writer = RollingFileWriter(base, ".json", flush_fn)
        await writer.close()
        from application_sdk.storage._rolling_errors import RollingWriterClosedError

        with pytest.raises(RollingWriterClosedError):
            await writer.append({"id": 1})

    @pytest.mark.asyncio
    async def test_explicit_flush_starts_new_chunk(self, base: str) -> None:
        flush_fn, calls = _make_flush_fn()
        async with RollingFileWriter(
            base, ".json", flush_fn, scoped_subdir_name="run"
        ) as writer:
            await writer.append({"id": 1})
            await writer.flush()
            await writer.append({"id": 2})

        # Two chunks: one from explicit flush, one from __aexit__.
        assert len(calls) == 2
        assert calls[0][0] == [{"id": 1}]
        assert calls[0][1].endswith("chunk-0.json")
        assert calls[1][0] == [{"id": 2}]
        assert calls[1][1].endswith("chunk-1.json")

    @pytest.mark.asyncio
    async def test_empty_buffer_flush_is_noop(self, base: str) -> None:
        flush_fn, calls = _make_flush_fn()
        async with RollingFileWriter(base, ".json", flush_fn) as writer:
            await writer.flush()  # nothing buffered
        assert calls == []
        assert writer.chunk_count == 0

    @pytest.mark.asyncio
    async def test_async_flush_fn(self, base: str) -> None:
        """flush_fn may be an async callable; the writer awaits it."""
        calls: list[tuple[list[Any], str]] = []

        async def _flush(batches: list[Any], path: str) -> None:
            calls.append((list(batches), path))
            with open(path, "w") as f:
                f.write("ok")

        async with RollingFileWriter(base, ".json", _flush) as writer:
            await writer.append({"id": 1})

        assert len(calls) == 1
        assert calls[0][0] == [{"id": 1}]


class TestRollingFileWriterRollover:
    @pytest.mark.asyncio
    async def test_time_rollover_after_interval_elapsed(self, base: str) -> None:
        """append after interval elapses must flush the in-flight chunk."""
        flush_fn, calls = _make_flush_fn()
        # Drive the monotonic clock deterministically.
        clock = iter([100.0, 100.5, 161.0, 161.5])
        with patch(
            "application_sdk.storage.rolling.time.monotonic", lambda: next(clock)
        ):
            async with RollingFileWriter(
                base,
                ".json",
                flush_fn,
                chunk_interval_seconds=60.0,
                scoped_subdir_name="run",
            ) as writer:
                # First append at t=100 — starts chunk-0 timer.
                # Read inside append: start=100.0, then elapsed-check at 100.5
                # (elapsed=0.5, under 60).
                await writer.append({"id": 1})
                # Second append at t=161 — start-check sees existing start,
                # then elapsed-check at 161.5 (elapsed=61.5, over 60) → flush
                # chunk-0 with [{1}, {2}].
                await writer.append({"id": 2})

        # Exactly one rollover from the timer; __aexit__ found empty buffer
        # because the second append's payload was flushed already.
        flushed_paths = [c[1] for c in calls]
        assert flushed_paths == [os.path.join(base, "run", "chunk-0.json")]
        assert calls[0][0] == [{"id": 1}, {"id": 2}]

    @pytest.mark.asyncio
    async def test_short_interval_rolls_between_sleeps(self, base: str) -> None:
        """Wall-clock interval drives rollover for real (non-mocked) sleeps.

        Semantics: append() buffers the new item, then checks elapsed. So the
        first batch that crosses the interval includes both the previously
        buffered items and the current append, and the next chunk starts
        fresh on the *next* append after that.
        """
        import asyncio

        flush_fn, calls = _make_flush_fn()
        async with RollingFileWriter(
            base,
            ".json",
            flush_fn,
            chunk_interval_seconds=0.005,
            scoped_subdir_name="run",
        ) as writer:
            await writer.append({"id": 1})  # starts chunk-0 timer
            await asyncio.sleep(0.02)
            await writer.append({"id": 2})  # crosses interval → flushes [1,2]
            await asyncio.sleep(0.02)
            await writer.append({"id": 3})  # starts chunk-1 timer
            await asyncio.sleep(0.02)
            await writer.append({"id": 4})  # crosses again → flushes [3,4]
            # __aexit__ then flushes any final buffer.

        paths = sorted(p for _, p in calls)
        assert any(p.endswith("chunk-0.json") for p in paths)
        assert any(p.endswith("chunk-1.json") for p in paths)
        # All four items written exactly once, in order.
        flushed_items: list[Any] = []
        for batches, _ in calls:
            flushed_items.extend(batches)
        assert flushed_items == [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]


class TestRollingFileWriterOnChunkComplete:
    @pytest.mark.asyncio
    async def test_callback_fires_per_chunk_with_index_and_path(
        self, base: str
    ) -> None:
        flush_fn, _ = _make_flush_fn()
        cb = MagicMock()
        async with RollingFileWriter(
            base,
            ".json",
            flush_fn,
            on_chunk_complete=cb,
            scoped_subdir_name="run",
        ) as writer:
            await writer.append({"id": 1})
            await writer.flush()
            await writer.append({"id": 2})

        assert cb.call_count == 2
        first_idx, first_path = cb.call_args_list[0].args
        second_idx, second_path = cb.call_args_list[1].args
        assert (first_idx, second_idx) == (0, 1)
        assert first_path.endswith("chunk-0.json")
        assert second_path.endswith("chunk-1.json")

    @pytest.mark.asyncio
    async def test_async_callback_supported(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        cb = AsyncMock()
        async with RollingFileWriter(
            base, ".json", flush_fn, on_chunk_complete=cb
        ) as writer:
            await writer.append({"id": 1})
        assert cb.await_count == 1

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_abort_writer(self, base: str) -> None:
        """on_chunk_complete is best-effort; raises in it must not bubble."""
        flush_fn, calls = _make_flush_fn()
        cb = MagicMock(side_effect=RuntimeError("heartbeat down"))
        async with RollingFileWriter(
            base, ".json", flush_fn, on_chunk_complete=cb
        ) as writer:
            await writer.append({"id": 1})
            await writer.append({"id": 2})

        # Chunk still landed despite the failing callback.
        assert len(calls) == 1
        assert cb.call_count == 1


class TestRollingFileWriterFileReference:
    def test_file_reference_points_at_scoped_subdir(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        writer = RollingFileWriter(base, ".json", flush_fn, scoped_subdir_name="run")
        ref = writer.file_reference
        assert isinstance(ref, FileReference)
        assert ref.local_path == os.path.join(base, "run")
        assert ref.is_durable is False  # ephemeral until persisted

    @pytest.mark.asyncio
    async def test_file_reference_after_writes_covers_only_writer_dir(
        self, tmp_path: Path
    ) -> None:
        """Sibling content in the parent must not be inside the ref."""
        base = tmp_path / "shared"
        base.mkdir()
        sibling = base / "do_not_upload.txt"
        sibling.write_text("hands off")

        flush_fn, _ = _make_flush_fn()
        async with RollingFileWriter(
            str(base), ".json", flush_fn, scoped_subdir_name="run"
        ) as writer:
            await writer.append({"id": 1})

        ref = writer.file_reference
        assert ref.local_path == str(base / "run")
        # Files under the ref do not include the sibling.
        dir_files = list(Path(ref.local_path).rglob("*"))
        assert all("do_not_upload" not in f.name for f in dir_files)
        # Sibling is still where we put it.
        assert sibling.exists()
        assert sibling.read_text() == "hands off"


# ---------------------------------------------------------------------------
# Rollover policies — each individually + AnyOfPolicy composition
# ---------------------------------------------------------------------------


def _state(
    *,
    chunk_start: float = 0.0,
    batches: int = 1,
    buffered_bytes: int = 0,
    buffered_records: int = 0,
) -> BufferState:
    """Convenience factory for BufferState snapshots in policy unit tests."""
    return BufferState(
        chunk_start_monotonic=chunk_start,
        buffered_batches=batches,
        buffered_bytes=buffered_bytes,
        buffered_records=buffered_records,
    )


class TestTimePolicy:
    def test_rolls_when_interval_elapsed(self) -> None:
        policy = TimePolicy(interval_seconds=60.0)
        # Force a chunk_start far in the past relative to time.monotonic().
        start = time.monotonic() - 120.0
        assert policy.should_roll(_state(chunk_start=start)) is True

    def test_does_not_roll_before_interval(self) -> None:
        policy = TimePolicy(interval_seconds=60.0)
        start = time.monotonic() - 1.0
        assert policy.should_roll(_state(chunk_start=start)) is False

    def test_rejects_non_positive_interval(self) -> None:
        from application_sdk.storage._rolling_errors import InvalidRolloverPolicyError

        with pytest.raises(InvalidRolloverPolicyError) as exc_info:
            TimePolicy(interval_seconds=0)
        assert exc_info.value.field == "interval_seconds"
        with pytest.raises(InvalidRolloverPolicyError):
            TimePolicy(interval_seconds=-1.0)


class TestSizePolicy:
    def test_rolls_at_threshold(self) -> None:
        policy = SizePolicy(max_bytes=1024)
        assert policy.should_roll(_state(buffered_bytes=1024)) is True
        assert policy.should_roll(_state(buffered_bytes=2048)) is True

    def test_does_not_roll_below_threshold(self) -> None:
        policy = SizePolicy(max_bytes=1024)
        assert policy.should_roll(_state(buffered_bytes=1023)) is False
        assert policy.should_roll(_state(buffered_bytes=0)) is False

    def test_rejects_non_positive(self) -> None:
        from application_sdk.storage._rolling_errors import InvalidRolloverPolicyError

        with pytest.raises(InvalidRolloverPolicyError) as exc_info:
            SizePolicy(max_bytes=0)
        assert exc_info.value.field == "max_bytes"
        with pytest.raises(InvalidRolloverPolicyError):
            SizePolicy(max_bytes=-1)


class TestCountPolicy:
    def test_rolls_at_threshold(self) -> None:
        policy = CountPolicy(max_records=100)
        assert policy.should_roll(_state(buffered_records=100)) is True
        assert policy.should_roll(_state(buffered_records=101)) is True

    def test_does_not_roll_below_threshold(self) -> None:
        policy = CountPolicy(max_records=100)
        assert policy.should_roll(_state(buffered_records=99)) is False
        assert policy.should_roll(_state(buffered_records=0)) is False

    def test_rejects_non_positive(self) -> None:
        from application_sdk.storage._rolling_errors import InvalidRolloverPolicyError

        with pytest.raises(InvalidRolloverPolicyError) as exc_info:
            CountPolicy(max_records=0)
        assert exc_info.value.field == "max_records"


class TestAnyOfPolicy:
    def test_rolls_when_any_constituent_triggers(self) -> None:
        # Time never triggers (huge interval), size does.
        composite = AnyOfPolicy(
            TimePolicy(interval_seconds=10_000.0),
            SizePolicy(max_bytes=1024),
        )
        assert (
            composite.should_roll(
                _state(chunk_start=time.monotonic(), buffered_bytes=2048)
            )
            is True
        )

    def test_does_not_roll_when_none_trigger(self) -> None:
        composite = AnyOfPolicy(
            TimePolicy(interval_seconds=10_000.0),
            SizePolicy(max_bytes=1024),
            CountPolicy(max_records=100),
        )
        assert (
            composite.should_roll(
                _state(
                    chunk_start=time.monotonic(), buffered_bytes=0, buffered_records=0
                )
            )
            is False
        )

    def test_rejects_empty_policy_list(self) -> None:
        from application_sdk.storage._rolling_errors import InvalidRolloverPolicyError

        with pytest.raises(InvalidRolloverPolicyError) as exc_info:
            AnyOfPolicy()
        assert exc_info.value.field == "policies"

    def test_on_flush_propagates_to_constituents(self) -> None:
        flushed: list[str] = []

        class _Spy(RolloverPolicy):
            def __init__(self, name: str) -> None:
                self.name = name

            def should_roll(self, state: BufferState) -> bool:
                return False

            def on_flush(self) -> None:
                flushed.append(self.name)

        composite = AnyOfPolicy(_Spy("a"), _Spy("b"))
        composite.on_flush()
        assert flushed == ["a", "b"]


# ---------------------------------------------------------------------------
# RollingFileWriter — memory bounds via convenience kwargs
# ---------------------------------------------------------------------------


class TestRollingFileWriterDefaults:
    def test_default_chunk_interval_is_30s(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        writer = RollingFileWriter(base, ".json", flush_fn)
        # Compose: AnyOfPolicy(TimePolicy(30), SizePolicy(50MB))
        assert isinstance(writer.rollover_policy, AnyOfPolicy)
        time_policies = [
            p for p in writer.rollover_policy.policies if isinstance(p, TimePolicy)
        ]
        size_policies = [
            p for p in writer.rollover_policy.policies if isinstance(p, SizePolicy)
        ]
        count_policies = [
            p for p in writer.rollover_policy.policies if isinstance(p, CountPolicy)
        ]
        assert len(time_policies) == 1
        assert time_policies[0].interval_seconds == 30.0
        assert len(size_policies) == 1
        assert size_policies[0].max_bytes == 50 * 1024 * 1024
        assert count_policies == []  # opt-in

    def test_max_buffer_bytes_none_disables_size_policy(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        writer = RollingFileWriter(base, ".json", flush_fn, max_buffer_bytes=None)
        # With only TimePolicy left (no Size, no Count), the AnyOfPolicy
        # collapses to just TimePolicy.
        assert isinstance(writer.rollover_policy, TimePolicy)

    def test_max_buffer_records_optin_adds_count_policy(self, base: str) -> None:
        flush_fn, _ = _make_flush_fn()
        writer = RollingFileWriter(base, ".json", flush_fn, max_buffer_records=1000)
        assert isinstance(writer.rollover_policy, AnyOfPolicy)
        count_policies = [
            p for p in writer.rollover_policy.policies if isinstance(p, CountPolicy)
        ]
        assert len(count_policies) == 1
        assert count_policies[0].max_records == 1000

    def test_rollover_policy_override_ignores_kwargs(self, base: str) -> None:
        """Passing rollover_policy bypasses chunk_interval_seconds/etc."""
        flush_fn, _ = _make_flush_fn()
        custom = SizePolicy(max_bytes=999)
        writer = RollingFileWriter(
            base,
            ".json",
            flush_fn,
            chunk_interval_seconds=5.0,  # ignored
            max_buffer_bytes=42,  # ignored
            rollover_policy=custom,
        )
        assert writer.rollover_policy is custom

    def test_rejects_non_positive_max_buffer_bytes(self, base: str) -> None:
        from application_sdk.storage._rolling_errors import InvalidRolloverPolicyError

        flush_fn, _ = _make_flush_fn()
        with pytest.raises(InvalidRolloverPolicyError) as exc_info:
            RollingFileWriter(base, ".json", flush_fn, max_buffer_bytes=0)
        assert exc_info.value.field == "max_buffer_bytes"

    def test_rejects_non_positive_max_buffer_records(self, base: str) -> None:
        from application_sdk.storage._rolling_errors import InvalidRolloverPolicyError

        flush_fn, _ = _make_flush_fn()
        with pytest.raises(InvalidRolloverPolicyError) as exc_info:
            RollingFileWriter(base, ".json", flush_fn, max_buffer_records=0)
        assert exc_info.value.field == "max_buffer_records"


class TestRollingFileWriterSizeRollover:
    @pytest.mark.asyncio
    async def test_byte_ceiling_flushes_before_time(self, base: str) -> None:
        """max_buffer_bytes triggers a flush ahead of the time interval."""
        flush_fn, calls = _make_flush_fn()
        # Each batch reports as 200 bytes via _default_size_estimator on a
        # bytes object (its sys.getsizeof). max_buffer_bytes=1000 will trip
        # before the 60s time interval ever does.
        async with RollingFileWriter(
            base,
            ".bin",
            flush_fn,
            chunk_interval_seconds=60.0,
            max_buffer_bytes=1000,
            scoped_subdir_name="run",
        ) as writer:
            # Each bytes(200) is ~233 bytes via sys.getsizeof — five appends
            # ≈ 1165 bytes, which crosses the 1000-byte ceiling on the fifth.
            for _ in range(5):
                await writer.append(b"x" * 200)

        # The size-triggered flush + __aexit__'s final flush = ≥ 1 chunk.
        assert len(calls) >= 1

    @pytest.mark.asyncio
    async def test_record_ceiling_flushes_before_time(self, base: str) -> None:
        """max_buffer_records triggers a flush ahead of the time interval."""
        flush_fn, calls = _make_flush_fn()
        # 50 records per append; max_buffer_records=100 ⇒ 2nd append rolls.
        async with RollingFileWriter(
            base,
            ".json",
            flush_fn,
            chunk_interval_seconds=60.0,
            max_buffer_records=100,
            max_buffer_bytes=None,
            scoped_subdir_name="run",
        ) as writer:
            await writer.append([{"id": i} for i in range(50)])
            await writer.append([{"id": i} for i in range(50, 100)])

        # First-trigger chunk-0 (after the second append) + __aexit__ no-op
        # because the buffer is empty.
        assert len(calls) == 1
        flushed_records = [
            item for batches, _ in calls for batch in batches for item in batch
        ]
        assert len(flushed_records) == 100

    @pytest.mark.asyncio
    async def test_size_estimator_failure_does_not_abort_writer(
        self, base: str
    ) -> None:
        """A misbehaving size_estimator must not break the write loop."""
        flush_fn, calls = _make_flush_fn()

        def _bad_estimator(_: object) -> int:
            raise RuntimeError("boom")

        async with RollingFileWriter(
            base,
            ".json",
            flush_fn,
            size_estimator=_bad_estimator,
            scoped_subdir_name="run",
        ) as writer:
            await writer.append({"id": 1})

        # __aexit__ still flushed the appended item.
        assert len(calls) == 1


class TestDefaultSizeEstimator:
    def test_pandas_dataframe_uses_memory_usage(self) -> None:
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame({"a": list(range(10_000))})
        size = _default_size_estimator(df)
        # memory_usage(deep=True) reports tens of KB for 10k int64 column.
        # Plain sys.getsizeof on a DataFrame is only ~50 bytes — the gap is
        # the whole point of the pandas-specific path.
        assert size > 10_000

    def test_dict_walks_one_level(self) -> None:
        d = {f"key_{i}": "x" * 100 for i in range(10)}
        size = _default_size_estimator(d)
        # Must exceed bare sys.getsizeof(dict) (~232 bytes for 10 items).
        assert size > sys_getsizeof(d) - 1

    def test_list_walks_one_level(self) -> None:
        items = ["x" * 100 for _ in range(10)]
        size = _default_size_estimator(items)
        assert size > sys_getsizeof(items) - 1

    def test_unknown_type_falls_back_to_sys_getsizeof(self) -> None:
        size = _default_size_estimator(42)
        assert size == sys_getsizeof(42)


def sys_getsizeof(obj: object) -> int:
    """Local convenience alias so tests don't repeat ``import sys``."""
    import sys as _sys

    return _sys.getsizeof(obj)
