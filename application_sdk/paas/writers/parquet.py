import orjson
import logging
import pyarrow as pa
import pyarrow.parquet as pq
from typing import Dict, Any

from application_sdk.paas.writers import ChunkedObjectStoreWriterInterface


logger = logging.getLogger(__name__)

class ParquetChunkedObjectStoreWriter(ChunkedObjectStoreWriterInterface):

    def __init__(self, local_file_prefix: str, upload_file_prefix: str, chunk_size: int=100000,
                 parquet_writer_options: Dict[str, Any] = {}):
        super().__init__(local_file_prefix, upload_file_prefix, chunk_size)
        self.schema = None
        self.parquet_writer_options = parquet_writer_options

    def update_schema(self, new_schema: pq.ParquetSchema):
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
            if self.current_file is None or self.current_record_count >= self.chunk_size:
                await self._create_new_file()

            table = pa.Table.from_pydict(data)
            new_schema = table.schema

            self.update_schema(new_schema)
            # Ensure the table conforms to the current schema
            table = table.cast(self.schema)
            self.current_file.write_table(table)

            self.current_record_count += 1
            self.total_record_count += 1

    async def close(self) -> None:
        await self.close_current_file()

        # Write number of chunks
        with open(f"{self.local_file_prefix}-metadata.json", mode='w') as f:
            f.write(orjson.dumps(
                {
                    "total_record_count": self.total_record_count,
                    "chunk_count": self.current_file_number
                },
                option=orjson.OPT_APPEND_NEWLINE).decode("utf-8")
            )
        await self.upload_file(f"{self.local_file_prefix}-metadata.json")


    async def _create_new_file(self):
        await self.close_current_file()

        self.current_file_number += 1
        self.current_file_name = f"{self.local_file_prefix}_{self.current_file_number}.parquet"
        self.current_file = pq.ParquetWriter(
            self.current_file_name, self.schema, **self.parquet_writer_options
        )

        logger.info(f"Created new file: {self.current_file_name}")
        self.current_record_count = 0
