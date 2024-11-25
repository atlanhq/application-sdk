import logging
import os
from typing import Any, Dict, Optional

import aiofiles
import orjson
import pandas as pd
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.objectstore import ObjectStore
from application_sdk.outputs import Output
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

            record = orjson.dumps(data, option=orjson.OPT_APPEND_NEWLINE).decode(
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


class JsonOutput(Output):
    def __init__(self, output_path: str, upload_file_prefix: str, typename: str):
        self.output_path = output_path
        self.upload_file_prefix = upload_file_prefix
        self.typename = typename
        self.total_record_count = 0
        self.chunk_count = 0
        os.makedirs(f"{output_path}", exist_ok=True)

    async def write_df(self, df: pd.DataFrame):
        """
        Method to write the dataframe to a json file and push it to the object store
        """
        try:
            self.chunk_count += 1
            self.total_record_count += len(df)

            # Write the dataframe to a json file
            output_file_name = (
                f"{self.output_path}/{self.typename}-{str(self.chunk_count)}.json"
            )
            df.to_json(output_file_name, orient="records", lines=True)

            # Push the file to the object store
            await ObjectStore.push_file_to_object_store(
                self.upload_file_prefix, output_file_name
            )
        except Exception as e:
            activity.logger.error(f"Error writing dataframe to json: {str(e)}")
