import logging
import os
from typing import Callable, List, Optional

import daft
import pandas as pd
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.objectstore import ObjectStore
from application_sdk.outputs import Output

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


def path_gen(chunk_start: int | None, chunk_count: int) -> str:
    if chunk_start is None:
        return f"{str(chunk_count)}.json"
    else:
        return f"{str(chunk_start+1)}-{str(chunk_count)}.json"


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
        path_gen: Callable[[int | None, int], str] = path_gen,
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
        self.path_gen = path_gen
        os.makedirs(f"{output_path}", exist_ok=True)

    async def write_df(self, df: pd.DataFrame):
        """
        Method to write the dataframe to a json file and push it to the object store
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
                    await self._flush_buffer()

            await self._flush_buffer()

        except Exception as e:
            activity.logger.error(f"Error writing dataframe to json: {str(e)}")

    async def write_daft_df(self, df: daft.DataFrame):
        """
        Method to write the dataframe to a json file and push it to the object store
        """
        # Daft does not have a built in method to write the daft dataframe to json
        # So we convert it to pandas dataframe and write it to json
        await self.write_df(df.to_pandas())

    async def _flush_buffer(self):
        if not self.buffer or not self.current_buffer_size:
            return
        combined_df = pd.concat(self.buffer)

        # Write DataFrame to JSON file
        if not combined_df.empty:
            self.chunk_count += 1
            self.total_record_count += len(combined_df)
            output_file_name = f"{self.output_path}/{self.path_gen(self.chunk_start, self.chunk_count)}"
            json_content = combined_df.to_json(orient="records", lines=True, force_ascii=False).replace('\\/', '/')
            with open(output_file_name, 'w') as f:
                f.write(json_content)
            
            # Push the file to the object store
            await ObjectStore.push_file_to_object_store(
                self.upload_file_prefix, output_file_name
            )

        self.buffer.clear()
        self.current_buffer_size = 0
