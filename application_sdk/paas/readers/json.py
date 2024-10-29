import logging
import os

import orjson

from application_sdk.paas.readers import ChunkedObjectStoreReaderInterface

logger = logging.getLogger(__name__)


class JSONChunkedObjectStoreReader(ChunkedObjectStoreReaderInterface):
    async def read_chunks(self, typename, chunks) -> None:
        for chunk in chunks:
            return self.read_chunk(typename, chunk)

    async def read_chunk(self, typename, chunk) -> None:
        data = []
        async with self.lock:
            filename = os.path.join(self.local_file_path, f"{typename}-{chunk}.json")
            with open(filename) as f:
                while True:
                    line = f.readline()
                    if line == "":
                        break

                    data.append(orjson.loads(line))
        return data

    async def get_chunk_count(self) -> int:
        return self.chunk_count

    async def close(self) -> None:
        # await self._flush_buffer()
        # await self.close_current_file()

        # # Write number of chunks
        # with open(f"{self.local_file_prefix}-metadata.json", mode="w") as f:
        #     f.write(
        #         orjson.dumps(
        #             {
        #                 "total_record_count": self.total_record_count,
        #                 "chunk_count": self.current_file_number,
        #             },
        #             option=orjson.OPT_APPEND_NEWLINE,
        #         ).decode("utf-8")
        #     )
        # await self.upload_file(f"{self.local_file_prefix}-metadata.json")
        pass
