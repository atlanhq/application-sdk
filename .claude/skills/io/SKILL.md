---
name: io
description: Read and write Parquet and JSON data files
user-invocable: true
---

# Read/Write Data Files

## Process

1. **Parquet files** — Use `ParquetFileReader` / `ParquetFileWriter`:
   - Reader: Context manager, supports single files and directories, Pandas or Daft output
   - Writer: Buffered accumulation with consolidation, Hive partitioning support
   - Write modes: APPEND, OVERWRITE, OVERWRITE_PARTITIONS
   - Auto-uploads to deployment object store; optional upstream upload via `ENABLE_ATLAN_UPLOAD`
   - Consolidation merges temp folders into optimized files using Daft at `parquet_target_filesize`
2. **JSON files** — Use `JsonFileReader` / `JsonFileWriter`:
   - Uses JSONL format (one JSON object per line)
   - Writer uses `orjson` for fast serialization
   - Auto-converts datetime to epoch, cleans null fields
   - Buffer flushes at `buffer_size` limit or every 50k records
3. **Common patterns**:
   - Always use as async context managers (`async with`)
   - Prefer Daft DataFrames over Pandas (`DataframeType.DAFT`)
   - Use `read_batches()` for streaming large files
   - Check `file_names` parameter for selective reads from directories

## Key References

- `application_sdk/io/parquet.py` — `ParquetFileReader`, `ParquetFileWriter`
- `application_sdk/io/json.py` — `JsonFileReader`, `JsonFileWriter`

## Handling `$ARGUMENTS`

If arguments specify the file format or operation (read/write), focus on that specific reader/writer's API and configuration options.
