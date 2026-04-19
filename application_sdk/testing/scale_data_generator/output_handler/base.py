from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict


class OutputFormatHandler(ABC):
    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)
        self.file_handlers = {}

    @abstractmethod
    def initialize_file(self, table_name: str) -> None:
        """Initialize output file for a table."""
        pass

    @abstractmethod
    def write_record(
        self, table_name: str, record: Dict[str, Any], is_last: bool = False
    ) -> None:
        """Write a single record to the output file."""
        pass

    @abstractmethod
    def close_files(self) -> None:
        """Close all open file handlers."""
        pass

    def get_file_path(self, table_name: str) -> Path:
        """Get the full file path for a table."""
        return self.output_dir / f"{table_name}.{self.file_extension}"
