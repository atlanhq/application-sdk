import csv
from typing import Any, Dict

from .base import OutputFormatHandler


class CsvFormatHandler(OutputFormatHandler):
    file_extension = "csv"
    _writers: Dict[str, csv.DictWriter] = {}

    def initialize_file(self, table_name: str) -> None:
        file_path = self.get_file_path(table_name)
        self.file_handlers[table_name] = open(file_path, "w", newline="")

    def write_record(
        self, table_name: str, record: Dict[str, Any], is_last: bool = False
    ) -> None:
        if table_name not in self.file_handlers:
            self.initialize_file(table_name)

        fh = self.file_handlers[table_name]
        if table_name not in self._writers:
            writer = csv.DictWriter(fh, fieldnames=list(record.keys()))
            writer.writeheader()
            self._writers[table_name] = writer

        self._writers[table_name].writerow(record)

    def close_files(self) -> None:
        for handler in self.file_handlers.values():
            handler.close()
        self._writers.clear()
