from typing import Any, AsyncIterator, List

import pytest

from application_sdk.io import IOStats, Reader, Writer


class DummyReader(Reader):
    async def read(self) -> List[int]:
        return [1, 2, 3]

    async def read_batches(self) -> AsyncIterator[List[int]]:
        async def agen() -> AsyncIterator[List[int]]:
            for i in range(3):
                yield [i]

        return agen()


class DummyWriter(Writer):
    def __init__(self) -> None:
        self.items: List[Any] = []

    async def write(self, dataframe: Any) -> None:
        self.items.append(dataframe)

    async def close(self) -> IOStats:
        return IOStats(
            record_count=len(self.items),
            chunk_count=len(self.items),
            file_paths=[],
            bytes_written=0,
        )


class TestIOBase:
    @pytest.mark.asyncio
    async def test_reader_contract(self):
        reader = DummyReader()
        data = await reader.read()
        assert data == [1, 2, 3]

        batches = await reader.read_batches()

        out: List[int] = []
        async for chunk in batches:
            out.extend(chunk)

        assert out == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_writer_write_batches_sync(self):
        writer = DummyWriter()
        await writer.write_batches(iter([[1], [2]]))
        stats = await writer.close()
        assert writer.items == [[1], [2]]
        assert stats.record_count == 2

    @pytest.mark.asyncio
    async def test_writer_write_batches_async(self):
        writer = DummyWriter()

        async def agen():
            yield [1]
            yield [2]

        await writer.write_batches(agen())
        stats = await writer.close()
        assert writer.items == [[1], [2]]
        assert stats.chunk_count == 2
