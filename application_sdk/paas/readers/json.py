import logging
from typing import Any, Dict

import aiofiles
import orjson

from application_sdk.paas.readers import ChunkedObjectStoreReaderInterface

logger = logging.getLogger(__name__)


class JSONChunkedObjectStoreReader(ChunkedObjectStoreReaderInterface):
    async def read_chunks(self, typename, chunks) -> None:
        for chunk in chunks:
            return self.read_chunk(typename, chunk)

    async def read_chunk(self, typename, chunk) -> None:
        async with self.lock:
            async with open(self.local_file_prefix + "-" + str(chunk) + ".json") as f:
                json_data = f.read()
                data = orgjson.loads(json_data)
                print(data)

        #     record = orjson.dumps(data, option=orjson.OPT_APPEND_NEWLINE).decode(
        #         "utf-8"
        #     )
        #     self.buffer.append(record)
        #     self.current_buffer_size += len(record)
        #     self.current_record_count += 1
        #     self.total_record_count += 1

        #     if self.current_buffer_size >= self.buffer_size:
        #         await self._flush_buffer()
        print("reading chunk")
        return {}

    async def get_chunk_count(self) -> int:
        pass

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
