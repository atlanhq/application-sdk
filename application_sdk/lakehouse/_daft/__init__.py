"""Internal: Daft-based DataFrame writes to Iceberg tables.

Apps must not import from this package; the public surface is
:meth:`application_sdk.lakehouse.LakehouseWriter.append_dataframe`.

Daft is the recommended writer for large batches because
``daft.DataFrame.write_iceberg`` streams Parquet files into the Iceberg
table without round-tripping through PyArrow in memory.
"""
