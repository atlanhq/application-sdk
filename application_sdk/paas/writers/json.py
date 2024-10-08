import logging
from typing import Any, Dict

import aiofiles
import orjson

from application_sdk.paas.writers import ChunkedObjectStoreWriterInterface

logger = logging.getLogger(__name__)


class JSONChunkedObjectStoreWriter(ChunkedObjectStoreWriterInterface):
    async def write(self, data: Dict[str, Any]) -> None:
        async with self.lock:
            if (
                self.current_file is None
                or self.current_record_count >= self.chunk_size
            ):
                await self._create_new_file()

            await self.current_file.write(
                orjson.dumps(data, option=orjson.OPT_APPEND_NEWLINE).decode("utf-8")
            )
            self.current_record_count += 1
            self.total_record_count += 1

    async def close(self) -> None:
        await self.close_current_file()

        # Write number of chunks
        with open(f"{self.local_file_prefix}-metadata.json", mode="w") as f:
            f.write(
                orjson.dumps(
                    {
                        "total_record_count": self.total_record_count,
                        "chunk_count": self.current_file_number,
                    },
                    option=orjson.OPT_APPEND_NEWLINE,
                ).decode("utf-8")
            )
        await self.upload_file(f"{self.local_file_prefix}-metadata.json")

    async def _create_new_file(self):
        await self.close_current_file()

        self.current_file_number += 1
        self.current_file_name = (
            f"{self.local_file_prefix}_{self.current_file_number}.json"
        )
        self.current_file = await aiofiles.open(self.current_file_name, mode="w")

        logger.info(f"Created new file: {self.current_file_name}")
        self.current_record_count = 0
