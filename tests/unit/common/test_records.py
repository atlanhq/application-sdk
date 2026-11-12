"""Tests for application_sdk.common.records: streaming JSONL I/O and KeyedIndex."""

from __future__ import annotations

import orjson
import pytest

from application_sdk.common.records import (
    JsonlWriter,
    KeyedIndex,
    aiter_jsonl,
    iter_jsonl,
    write_jsonl,
    write_jsonl_async,
)


class TestIterJsonl:
    def test_yields_each_record_in_file_order(self, tmp_path):
        path = tmp_path / "in.json"
        path.write_bytes(b'{"id": 1}\n{"id": 2}\n')

        assert list(iter_jsonl(str(path))) == [{"id": 1}, {"id": 2}]

    def test_skips_blank_lines(self, tmp_path):
        path = tmp_path / "in.json"
        path.write_bytes(b'{"id": 1}\n\n   \n{"id": 2}\n')

        assert [r["id"] for r in iter_jsonl(str(path))] == [1, 2]

    @pytest.mark.parametrize("missing", [None, "", "/nonexistent/file.json"])
    def test_missing_path_yields_nothing(self, missing):
        assert list(iter_jsonl(missing)) == []

    def test_invalid_line_raises_by_default(self, tmp_path):
        path = tmp_path / "in.json"
        path.write_bytes(b'{"id": 1}\nnot json\n')

        with pytest.raises(orjson.JSONDecodeError):
            list(iter_jsonl(str(path)))

    def test_invalid_line_is_skipped_when_asked(self, tmp_path):
        path = tmp_path / "in.json"
        path.write_bytes(b'{"id": 1}\nnot json\n{"id": 3}\n')

        assert [r["id"] for r in iter_jsonl(str(path), skip_invalid=True)] == [1, 3]

    def test_is_lazy(self, tmp_path):
        """The first record is available without reading the rest of the file."""
        path = tmp_path / "in.json"
        path.write_bytes(b'{"id": 1}\nnot json\n')

        assert next(iter_jsonl(str(path))) == {"id": 1}


class TestJsonlWriter:
    def test_writes_one_line_per_record_and_counts(self, tmp_path):
        path = tmp_path / "nested" / "out.json"

        with JsonlWriter(str(path)) as out:
            out.write({"id": 1})
            out.write({"id": 2})

        assert path.read_bytes() == b'{"id":1}\n{"id":2}\n'
        assert out.count == 2
        assert out.bytes_written == len(path.read_bytes())

    def test_no_records_still_creates_an_empty_file(self, tmp_path):
        path = tmp_path / "out.json"

        with JsonlWriter(str(path)):
            pass

        assert path.read_bytes() == b""

    def test_write_line_adds_missing_newline_only(self, tmp_path):
        path = tmp_path / "out.json"

        with JsonlWriter(str(path)) as out:
            out.write_line(b'{"a":1}')
            out.write_line(b'{"a":2}\n')

        assert path.read_bytes() == b'{"a":1}\n{"a":2}\n'

    def test_write_after_close_raises(self, tmp_path):
        out = JsonlWriter(str(tmp_path / "out.json"))
        out.close()

        with pytest.raises(ValueError):
            out.write({"id": 1})

    def test_write_jsonl_consumes_a_generator(self, tmp_path):
        path = tmp_path / "out.json"

        count = write_jsonl(str(path), ({"id": i} for i in range(3)))

        assert count == 3
        assert list(iter_jsonl(str(path))) == [{"id": 0}, {"id": 1}, {"id": 2}]

    def test_bytes_match_the_list_based_writer(self, tmp_path):
        """A drop-in replacement must not change the bytes on disk."""
        records = [
            {"id": 1, "nested": {"b": [1, 2]}, "name": "é"},
            {"id": "root", "x": None},
        ]
        path = tmp_path / "out.json"

        write_jsonl(str(path), records)

        assert path.read_bytes() == b"".join(orjson.dumps(r) + b"\n" for r in records)


class TestKeyedIndex:
    def test_lookup_by_field(self, tmp_path):
        path = tmp_path / "in.json"
        write_jsonl(str(path), [{"id": 1, "name": "a"}, {"id": "root", "name": "b"}])

        with KeyedIndex.from_jsonl(str(path), key="id") as index:
            assert index[1] == {"id": 1, "name": "a"}
            assert index.get("root")["name"] == "b"
            assert 2 not in index
            assert index.get(2) is None
            assert len(index) == 2

    def test_fields_keeps_only_the_named_fields(self, tmp_path):
        path = tmp_path / "in.json"
        write_jsonl(str(path), [{"id": 1, "name": "a", "big": "x" * 1000}])

        with KeyedIndex.from_jsonl(
            str(path), key="id", fields=["name", "absent"]
        ) as index:
            assert index[1] == {"name": "a"}

    def test_value_function_and_key_function(self, tmp_path):
        path = tmp_path / "in.json"
        write_jsonl(
            str(path),
            [{"qid": 7, "query": "select 1"}, {"qid": None, "query": "dropped"}],
        )

        with KeyedIndex.from_jsonl(
            str(path), key=lambda r: r["qid"], value=lambda r: r["query"]
        ) as index:
            assert index[7] == "select 1"
            assert len(index) == 1

    def test_fields_and_value_together_is_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            KeyedIndex.from_jsonl(
                str(tmp_path / "x"), key="id", fields=["a"], value=lambda r: r
            )

    def test_last_write_wins_like_a_dict(self):
        with KeyedIndex() as index:
            index[1] = "first"
            index[1] = "second"
            assert index[1] == "second"

    def test_missing_key_raises_key_error(self):
        with KeyedIndex() as index, pytest.raises(KeyError):
            index["nope"]

    def test_stays_in_memory_under_budget(self):
        with KeyedIndex() as index:
            index[1] = {"name": "a"}
            assert not index.spilled

    def test_moves_to_disk_over_budget_and_keeps_every_entry(self):
        with KeyedIndex(max_memory_bytes=2_000) as index:
            for i in range(200):
                index[i] = {"name": f"record-{i}", "pad": "x" * 50}
            index["root"] = {"name": "root"}

            assert index.spilled
            assert len(index) == 201
            assert index[0]["name"] == "record-0"
            assert index[199]["name"] == "record-199"
            assert index["root"] == {"name": "root"}
            assert 500 not in index

    def test_append_builds_lists_in_memory_and_on_disk(self):
        with KeyedIndex(max_memory_bytes=500) as index:
            for i in range(100):
                index.append(i % 5, i)

            assert index.spilled
            assert index[0] == list(range(0, 100, 5))
            assert index[4] == list(range(4, 100, 5))

    def test_unhashable_key_is_a_miss_not_an_error(self):
        with KeyedIndex() as index:
            index[1] = "a"
            assert index.get(["not", "hashable"]) is None
            assert ["not", "hashable"] not in index

    def test_empty_index_from_missing_file(self):
        with KeyedIndex.from_jsonl(None, key="id") as index:
            assert len(index) == 0


class TestAsyncHelpers:
    async def test_aiter_jsonl_yields_every_record_across_batches(self, tmp_path):
        path = tmp_path / "in.json"
        write_jsonl(str(path), ({"id": i} for i in range(25)))

        seen = [r["id"] async for r in aiter_jsonl(str(path), batch_size=10)]

        assert seen == list(range(25))

    async def test_aiter_jsonl_missing_path_yields_nothing(self):
        assert [r async for r in aiter_jsonl(None)] == []

    async def test_write_jsonl_async_matches_sync_bytes(self, tmp_path):
        records = [{"id": i, "pad": "x" * 100} for i in range(50)]
        sync_path, async_path = tmp_path / "sync.json", tmp_path / "async.json"
        write_jsonl(str(sync_path), records)

        async def source():
            for record in records:
                yield record

        count = await write_jsonl_async(str(async_path), source(), flush_bytes=500)

        assert count == 50
        assert async_path.read_bytes() == sync_path.read_bytes()

    async def test_write_jsonl_async_empty_source_creates_empty_file(self, tmp_path):
        path = tmp_path / "out.json"

        async def source():
            return
            yield  # pragma: no cover - makes this an async generator

        assert await write_jsonl_async(str(path), source()) == 0
        assert path.read_bytes() == b""

    async def test_write_jsonl_async_closes_the_file_when_the_source_fails(
        self, tmp_path
    ):
        path = tmp_path / "out.json"

        async def source():
            yield {"id": 1}
            raise RuntimeError("source failed")

        with pytest.raises(RuntimeError):
            await write_jsonl_async(str(path), source())

        # the file handle is closed: the file can be reopened for writing and removed
        path.unlink()
        assert not path.exists()
