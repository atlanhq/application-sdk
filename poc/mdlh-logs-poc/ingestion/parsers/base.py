"""
Base parser interface and registry for pluggable log parsing.
"""

from abc import ABC, abstractmethod
from typing import List, Type, Optional

import pyarrow as pa


class LogParser(ABC):
    """Abstract base class for log parsers."""

    @abstractmethod
    def can_parse(self, filename: str) -> bool:
        """
        Check if this parser can handle the given file.

        Args:
            filename: The filename (or full path) to check

        Returns:
            True if this parser can handle the file
        """
        pass

    @abstractmethod
    def parse(self, file_content: bytes, arrow_schema: pa.Schema) -> pa.Table:
        """
        Parse file content into a PyArrow table.

        Args:
            file_content: Raw bytes of the file
            arrow_schema: Target PyArrow schema to match

        Returns:
            PyArrow table matching the target schema
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the parser name for logging."""
        pass


class ParserRegistry:
    """Registry for log parsers with automatic format detection."""

    def __init__(self):
        self._parsers: List[LogParser] = []

    def register(self, parser: LogParser) -> None:
        """Register a parser instance."""
        self._parsers.append(parser)

    def get_parser(self, filename: str) -> Optional[LogParser]:
        """
        Get a parser that can handle the given file.

        Args:
            filename: The filename to check

        Returns:
            A parser instance or None if no parser matches
        """
        for parser in self._parsers:
            if parser.can_parse(filename):
                return parser
        return None

    def list_parsers(self) -> List[str]:
        """Return list of registered parser names."""
        return [p.name for p in self._parsers]


# Global registry instance
_default_registry = None


def get_default_registry() -> ParserRegistry:
    """Get the default parser registry with all built-in parsers."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ParserRegistry()
        # Import here to avoid circular imports
        from .parquet_parser import ParquetParser
        from .otlp_json_parser import OTLPJsonParser
        _default_registry.register(ParquetParser())
        _default_registry.register(OTLPJsonParser())
    return _default_registry
