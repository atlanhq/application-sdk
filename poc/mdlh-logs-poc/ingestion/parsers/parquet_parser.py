"""
Parquet log parser - extracts and normalizes logs from Parquet files.

This is the existing format from production logs.
"""

import io
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq

from .base import LogParser


class ParquetParser(LogParser):
    """Parser for Parquet log files."""

    @property
    def name(self) -> str:
        return "parquet"

    def can_parse(self, filename: str) -> bool:
        """Check if file is a Parquet file."""
        return filename.lower().endswith('.parquet')

    def parse(self, file_content: bytes, arrow_schema: pa.Schema) -> pa.Table:
        """Parse Parquet content and normalize to target schema."""
        tbl = pq.read_table(io.BytesIO(file_content))
        return self._normalize_table(tbl, arrow_schema)

    def _normalize_table(self, pa_tbl: pa.Table, target_schema: pa.Schema) -> pa.Table:
        """
        Normalize table to match target schema.

        - Keep only fields in target_schema
        - Add missing fields as nulls
        - Extract top-level fields from extra struct
        """
        columns = []

        # Get the extra struct column if it exists
        extra_col = None
        if 'extra' in pa_tbl.column_names:
            extra_col = pa_tbl['extra']

        for field in target_schema:
            name = field.name

            # Special handling for fields extracted from extra struct
            if name in ['trace_id', 'atlan_argo_workflow_name', 'atlan_argo_workflow_node'] and extra_col is not None:
                columns.append(self._extract_from_struct(extra_col, name, field.type, pa_tbl.num_rows))
            elif name in pa_tbl.column_names:
                col = pa_tbl[name]
                # Try cast to expected type
                try:
                    if col.type != field.type:
                        col = col.cast(field.type)
                except Exception:
                    col = pa.array([None] * pa_tbl.num_rows, type=field.type)
                columns.append(col)
            else:
                columns.append(pa.array([None] * pa_tbl.num_rows, type=field.type))

        return pa.Table.from_arrays(columns, schema=target_schema)

    def _extract_from_struct(
        self,
        extra_col: pa.ChunkedArray,
        field_name: str,
        field_type: pa.DataType,
        num_rows: int
    ) -> pa.Array:
        """Extract a field from the extra struct column."""
        # Map schema field names to struct field names
        field_mapping = {
            'trace_id': 'trace_id',
            'atlan_argo_workflow_name': 'atlan-argo-workflow-name',
            'atlan_argo_workflow_node': 'atlan-argo-workflow-node'
        }
        struct_field_name = field_mapping.get(field_name, field_name)

        try:
            extracted_values = []
            for row in extra_col:
                if row.is_valid:
                    try:
                        value = row[struct_field_name]
                        extracted_values.append(value.as_py() if value.is_valid else None)
                    except (KeyError, IndexError):
                        extracted_values.append(None)
                else:
                    extracted_values.append(None)
            return pa.array(extracted_values, type=field_type)
        except Exception:
            return pa.array([None] * num_rows, type=field_type)
