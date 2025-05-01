import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.objectstore import ObjectStore

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class ChunkedObjectStoreWriterInterface(ABC):
    def __init__(
        self,
        local_file_prefix: str,
        upload_file_prefix: str,
        chunk_size: int = 30000,
        buffer_size: int = 1024 * 1024 * 10,
        start_file_number: int = 0,
    ):  # 10MB buffer by default
        self.local_file_prefix = local_file_prefix
        self.upload_file_prefix = upload_file_prefix
        self.chunk_size = chunk_size
        self.lock = asyncio.Lock()
        self.current_file = None
        self.current_file_name = None
        self.current_file_number = start_file_number
        self.current_record_count = 0
        self.total_record_count = 0

        self.buffer: List[str] = []
        self.buffer_size = buffer_size
        self.current_buffer_size = 0

        os.makedirs(self.local_file_prefix, exist_ok=True)

    @abstractmethod
    async def write(self, data: Dict[str, Any]) -> None:
        raise NotImplementedError

    async def write_list(self, data: List[Dict[str, Any]]) -> None:
        for record in data:
            await self.write(record)

    @abstractmethod
    async def close(self) -> int:
        raise NotImplementedError

    async def close_current_file(self):
        if not self.current_file:
            return

        await self.current_file.close()
        await self.upload_file(self.current_file_name)
        # os.unlink(self.current_file_name)
        activity.logger.info(
            f"Uploaded file: {self.current_file_name} and removed local copy"
        )

    async def upload_file(self, local_file_path: str) -> None:
        activity.logger.info(
            f"Uploading file: {local_file_path} to {self.upload_file_prefix}"
        )
        await ObjectStore.push_file_to_object_store(
            self.upload_file_prefix, local_file_path
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()
        return False
