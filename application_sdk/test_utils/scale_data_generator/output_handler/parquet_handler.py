from pathlib import Path
from typing import Any, Dict

import pyarrow as pa
import pyarrow.parquet as pq

from .base import OutputFormatHandler


class ParquetFormatHandler(OutputFormatHandler):
    file_extension = "parquet"
    BATCH_SIZE = 1000

    def initialize_file(self, table_name: str) -> None:
        file_path = self.get_file_path(table_name)
        self.file_handlers[table_name] = {"records": [], "path": file_path}

    def write_record(
        self, table_name: str, record: Dict[str, Any], is_last: bool = False
    ) -> None:
        if table_name not in self.file_handlers:
            self.initialize_file(table_name)

        self.file_handlers[table_name]["records"].append(record)
        if len(self.file_handlers[table_name]["records"]) >= self.BATCH_SIZE or is_last:
            self._write_batch(table_name)

    def _write_batch(self, table_name: str) -> None:
        if self.file_handlers[table_name]["records"]:
            import pandas as pd

            df = pd.DataFrame(self.file_handlers[table_name]["records"])
            table = pa.Table.from_pandas(df)
            file_path = Path(self.file_handlers[table_name]["path"])

            if file_path.exists():
                pq.write_to_dataset(table, file_path)
            else:
                pq.write_table(table, file_path)

            self.file_handlers[table_name]["records"] = []

    def close_files(self) -> None:
        for table_name in self.file_handlers:
            self._write_batch(table_name)
