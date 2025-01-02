import logging
import os
from typing import Any, Dict, List, Optional

import aiofiles
import daft
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
            if self.current_file is None or (
                self.current_record_count >= self.chunk_size and self.chunk_size >= 0
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
        if self.chunk_size < 0:
            # If chunk size is negative, we don't want to write metadata
            return

        # Write number of chunks
        with open(f"{self.local_file_prefix}/metadata.json", mode="w") as f:
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
        await self.upload_file(f"{self.local_file_prefix}/metadata.json")

        return {
            "total_record_count": total_record_count or self.total_record_count,
            "chunk_count": self.current_file_number,
        }

    async def _create_new_file(self):
        await self.close_current_file()

        self.current_file_number += 1
        self.current_file_name = (
            f"{self.local_file_prefix}/{self.current_file_number}.json"
            if self.chunk_size >= 0
            else f"{self.local_file_prefix}.json"
        )
        self.current_file = await aiofiles.open(self.current_file_name, mode="w")

        activity.logger.info(f"Created new file: {self.current_file_name}")
        self.current_record_count = 0


class JsonOutput(Output):
    def __init__(
        self,
        output_path: str,
        upload_file_prefix: str,
        chunk_start: Optional[int] = None,
        buffer_size: int = 1024 * 1024 * 10,
        chunk_size: int = 100000,
        total_record_count: int = 0,
        chunk_count: int = 0,
    ):
        self.output_path = output_path
        self.upload_file_prefix = upload_file_prefix
        self.chunk_start = chunk_start
        self.total_record_count = total_record_count
        self.chunk_count = chunk_count
        self.buffer_size = buffer_size
        self.chunk_size = chunk_size
        self.buffer: List[pd.DataFrame] = []
        self.current_buffer_size = 0
        os.makedirs(f"{output_path}", exist_ok=True)

    async def write_df(self, df: pd.DataFrame, file_suffix: str = None):
        """
        Method to write the dataframe to a json file and push it to the object store.
        :param file_suffix: Optional suffix to be added to the file name.
        """
        if len(df) == 0:
            return

        try:
            # Split the DataFrame into chunks
            partition = (
                self.chunk_size
                if self.chunk_start is None
                else min(self.chunk_size, self.buffer_size)
            )
            chunks = [df[i : i + partition] for i in range(0, len(df), partition)]

            for chunk in chunks:
                self.buffer.append(chunk)
                self.current_buffer_size += len(chunk)

                if self.current_buffer_size >= partition:
                    await self._flush_buffer(file_suffix)

            await self._flush_buffer(file_suffix)

        except Exception as e:
            activity.logger.error(f"Error writing dataframe to json: {str(e)}")

    async def write_daft_df(self, df: daft.DataFrame):
        """
        Method to write the dataframe to a json file and push it to the object store
        """
        # Daft does not have a built in method to write the daft dataframe to json
        # So we convert it to pandas dataframe and write it to json
        await self.write_df(df.to_pandas())

    async def _flush_buffer(self, file_suffix: str = None):
        if not self.buffer or not self.current_buffer_size:
            return
        combined_df = pd.concat(self.buffer)

        # Write DataFrame to JSON file
        if not combined_df.empty:
            self.chunk_count += 1
            self.total_record_count += len(combined_df)

            # If a suffix is provided, include it in the file name
            suffix_part = f"_{file_suffix}" if file_suffix else ""

            if self.chunk_start is None:
                output_file_name = (
                    f"{self.output_path}/{str(self.chunk_count)}{suffix_part}.json"
                )
            else:
                output_file_name = f"{self.output_path}/{str(self.chunk_start + 1)}-{str(self.chunk_count)}{suffix_part}.json"

            combined_df.to_json(output_file_name, orient="records", lines=True)

            # Push the file to the object store
            await ObjectStore.push_file_to_object_store(
                self.upload_file_prefix, output_file_name
            )

        self.buffer.clear()
        self.current_buffer_size = 0
