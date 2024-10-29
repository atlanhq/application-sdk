import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List

import orjson

from application_sdk.paas.objectstore import ObjectStore

logger = logging.getLogger(__name__)


class ChunkedObjectStoreReaderInterface(ABC):
    def __init__(
        self,
        local_file_path: str,
        download_file_prefix: str,
    ):  # 10MB buffer by default
        self.local_file_path = local_file_path
        self.download_file_prefix = download_file_prefix
        self.lock = asyncio.Lock()
        self.total_record_count = 0
        self.chunk_count = 0

    @abstractmethod
    async def read_chunk(self, typename, chunk) -> None:
        raise NotImplementedError

    async def download_file(self, file_path) -> None:
        await ObjectStore.download_file_from_object_store(
            self.download_file_prefix, os.path.join(self.local_file_path, file_path)
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

        with open(os.path.join(self.local_file_path, "database-metadata.json")) as f:
            json_data = f.read()
            data = orjson.loads(json_data)

            self.total_record_count = data["total_record_count"]
            self.chunk_count = data["chunk_count"]

        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()
        return False
