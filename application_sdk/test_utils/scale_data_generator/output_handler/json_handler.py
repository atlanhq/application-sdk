import json
from typing import Any, Dict

from .base import OutputFormatHandler


class JsonFormatHandler(OutputFormatHandler):
    file_extension = "json"

    def initialize_file(self, table_name: str) -> None:
        file_path = self.get_file_path(table_name)
        self.file_handlers[table_name] = open(file_path, "w")

    def write_record(
        self, table_name: str, record: Dict[str, Any], is_last: bool = False
    ) -> None:
        if table_name not in self.file_handlers:
            self.initialize_file(table_name)
        self.file_handlers[table_name].write(json.dumps(record) + "\n")

    def close_files(self) -> None:
        for handler in self.file_handlers.values():
            handler.close()
