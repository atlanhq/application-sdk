"""
Pluggable parser system for log ingestion.

Supports multiple log formats:
- Parquet (existing format from production)
- OTLP JSON (awss3 exporter output)
"""

from .base import LogParser, ParserRegistry
from .parquet_parser import ParquetParser
from .otlp_json_parser import OTLPJsonParser

__all__ = ['LogParser', 'ParserRegistry', 'ParquetParser', 'OTLPJsonParser']
