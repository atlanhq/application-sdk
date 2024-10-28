import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from application_sdk.paas.objectstore import ObjectStore

logger = logging.getLogger(__name__)


class ChunkedObjectStoreReaderInterface(ABC):
    def __init__(
        self,
        local_file_prefix: str,
        download_file_prefix: str,
    ):  # 10MB buffer by default
        self.local_file_prefix = local_file_prefix
        self.download_file_prefix = download_file_prefix
        self.lock = asyncio.Lock()
        self.current_file = None
        self.current_file_name = None
        self.current_file_number = 0
        self.current_record_count = 0

        self.total_chunk_count = 0
        self.total_record_count = 0

        # # Read number of chunks
        # with open(f"{self.local_file_prefix}-metadata.json", mode="r") as f:
        #     data = f.read()
        #     print(data)

    @abstractmethod
    async def read_chunk(self, typename, chunk) -> None:
        raise NotImplementedError

    async def download_file(self, file_path) -> None:
        # logger.info(f"Downloading file: {self.download_file_prefix} to {filename}")
        await ObjectStore.download_file_from_object_store(
            self.download_file_prefix, file_path
        )

    async def read_list(self, data: List[Dict[str, Any]]) -> None:
        # for record in data:
        #     await self.read(record)
        pass

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

    async def close_current_file(self):
        # if not self.current_file:
        #     return

        # await self.current_file.close()
        # # os.unlink(self.current_file_name)
        pass

    async def __aenter__(self):
        await self.download_file(f"database-metadata.json")

        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()
        return False
