import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.objectstore import ObjectStore

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


# TODO: Refactor to use the new Input / Output interfaces
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


class ParquetChunkedObjectStoreWriter(ChunkedObjectStoreWriterInterface):
    def __init__(
        self,
        local_file_prefix: str,
        upload_file_prefix: str,
        chunk_size: int = 100000,
        schema: pq.ParquetSchema = None,
        parquet_writer_options: Dict[str, Any] = {},
    ):
        super().__init__(local_file_prefix, upload_file_prefix, chunk_size)
        self.schema = schema
        self.parquet_writer_options = parquet_writer_options

    async def update_schema(self, new_schema: pq.ParquetSchema):
        if self.schema is None:
            self.schema = new_schema
        else:
            # Merge the existing schema with the new one
            merged_fields = list(self.schema)
            for field in new_schema:
                if field.name not in self.schema.names:
                    merged_fields.append(field)
            self.schema = pa.schema(merged_fields)

    async def write(self, data: Dict[str, Any]) -> None:
        async with self.lock:
            if (
                self.current_file is None
                or self.current_record_count >= self.chunk_size
            ):
                await self._create_new_file()

            table = pa.Table.from_pydict(data)
            new_schema = table.schema

            await self.update_schema(new_schema)
            # Ensure the table conforms to the current schema
            table = table.cast(self.schema)
            self.current_file.write_table(table)

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
            f"{self.local_file_prefix}_{self.current_file_number}.parquet"
        )
        self.current_file = pq.ParquetWriter(
            self.current_file_name, self.schema, **self.parquet_writer_options
        )

        activity.logger.info(f"Created new file: {self.current_file_name}")
        self.current_record_count = 0
