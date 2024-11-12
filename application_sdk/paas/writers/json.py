import logging
from typing import Any, Dict, Optional

import aiofiles
import orjson
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.paas.writers import ChunkedObjectStoreWriterInterface

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class JSONChunkedObjectStoreWriter(ChunkedObjectStoreWriterInterface):
    async def write(self, data: Dict[str, Any]) -> None:
        async with self.lock:
            if (
                self.current_file is None
                or self.current_record_count >= self.chunk_size
            ):
                await self._flush_buffer()
                await self._create_new_file()

            record = orjson.dumps(data, option=orjson.OPT_APPEND_NEWLINE | orjson.OPT_NON_STR_KEYS).decode(
                "utf-8"
            )
            self.buffer.append(record)
            self.current_buffer_size += len(record)
            self.current_record_count += 1
            self.total_record_count += 1

            if self.current_buffer_size >= self.buffer_size:
                await self._flush_buffer()

    async def _flush_buffer(self):
        if not self.buffer or not self.current_buffer_size:
            return

        await self.current_file.write("".join(self.buffer))
        await self.current_file.flush()
        self.buffer.clear()
        self.current_buffer_size = 0

    async def close(self) -> int:
        await self._flush_buffer()
        await self.close_current_file()

    async def write_metadata(self, total_record_count: Optional[int] = None):
        # Write number of chunks
        with open(f"{self.local_file_prefix}-metadata.json", mode="w") as f:
            f.write(
                orjson.dumps(
                    {
                        "total_record_count": total_record_count
                        or self.total_record_count,
                        "chunk_count": self.current_file_number,
                    },
                    option=orjson.OPT_APPEND_NEWLINE,
                ).decode("utf-8")
            )
        await self.upload_file(f"{self.local_file_prefix}-metadata.json")

        return {
            "total_record_count": total_record_count or self.total_record_count,
            "chunk_count": self.current_file_number,
        }

    async def _create_new_file(self):
        await self.close_current_file()

        self.current_file_number += 1
        self.current_file_name = (
            f"{self.local_file_prefix}-{self.current_file_number}.json"
        )
        self.current_file = await aiofiles.open(self.current_file_name, mode="w")

        activity.logger.info(f"Created new file: {self.current_file_name}")
        self.current_record_count = 0
