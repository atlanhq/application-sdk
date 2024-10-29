import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, List

import orjson

from application_sdk.paas.objectstore import ObjectStore

logger = logging.getLogger(__name__)


class ChunkedObjectStoreReaderInterface(ABC):
    def __init__(
        self,
        local_file_path: str,
        download_file_prefix: str,
        typename: str,
    ):
        self.local_file_path = local_file_path
        self.download_file_prefix = download_file_prefix
        self.lock = asyncio.Lock()
        self.total_record_count = 0
        self.chunk_count = 0
        self.typename = typename

    @abstractmethod
    async def read_chunk(self, chunk: int) -> List[Any]:
        raise NotImplementedError

    async def get_chunk_count(self) -> int:
        return self.chunk_count

    async def download_file(self, file_path: str) -> None:
        await ObjectStore.download_file_from_object_store(
            self.download_file_prefix, os.path.join(self.local_file_path, file_path)
        )

    async def close(self) -> None:
        pass

    async def __aenter__(self):
        await self.download_file(f"{self.typename}-metadata.json")

        with open(
            os.path.join(self.local_file_path, f"{self.typename}-metadata.json")
        ) as f:
            json_data = f.read()
            data = orjson.loads(json_data)

            self.total_record_count = data["total_record_count"]
            self.chunk_count = data["chunk_count"]

        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()
        return False
