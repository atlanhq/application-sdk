# storage-io: Object store, files, FileReference
- ObjectStore key/prefix params accept `./local/tmp/...` paths or `artifacts/...` keys and the SDK normalises them. Flag: hand-rolled or double normalisation. In `upload_file(key, local_path)`/`download_file(key, local_path)` the `local_path` is a filesystem path, never a key; flag swapped args.
- Flag: artifacts written straight to their final path (`Path.write_text`, `open(final, "w")`, `pq.write_table(final)`); use `atomic_write`/`atomic_path`/`atomic_copy` from `application_sdk.common.atomic`, or `disk_full_guard` for appends.
- Flag: a `FileReference` meant for Atlan system apps with no `App.upload()` from `run()` (SDR silent zero assets, `docs/concepts/file-reference.md`); `App.upload()` used for task-to-task data instead of `FileReference` fields.
- Flag: large data inline in contracts; new use of the deprecated `ParquetFileWriter`/`JsonFileWriter` or their readers (use `RollingFileWriter`).
- Flag: an empty listing/download treated as success; raise `ObjectStoreReadError`/`ObjectStoreDownloadError`. ENOSPC should surface as `DiskFullError`.
- Flag: keys that bypass the run-scoped layout (`WORKFLOW_OUTPUT_PATH_TEMPLATE`); credential files read through the data object-store binding.
- Flag: a change to how `get_persistent_s3_prefix()` derives its path: it is a data migration (markers relocate and every connection re-extracts in full, `docs/standards/cross-repo-contracts.md`).
- Flag: storage errors re-raised without key/path context.
- Severity: critical for credential/data store mixing or cross-run keys; high for truncated artifacts, silent empty hand-off, swapped key/path; medium otherwise.
