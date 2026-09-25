# storage-io: Object store, files, FileReference
- Flag: ObjectStore key/prefix params accept `./local/tmp/...` paths or `artifacts/...` keys and the SDK normalises them; flag hand-rolled or double normalisation. In `upload_file(key, local_path)`/`download_file(key, local_path)` the `local_path` is a filesystem path, never a key; flag swapped args.
- Flag: artifacts written straight to their final path (`Path.write_text`, `open(final, "w")`, `pq.write_table(final)`); use `atomic_write`/`atomic_path`/`atomic_copy` from `application_sdk.common.atomic`, or `disk_full_guard` for appends.
- Flag: a `FileReference` meant for Atlan system apps with no `App.upload()` from `run()` (SDR silent zero assets); `App.upload()` used for task-to-task data instead of `FileReference` fields.
- Flag: large data inline in contracts; new use of deprecated `ParquetFileWriter`/`JsonFileWriter` (use `RollingFileWriter`).
- Flag: empty listing/download treated as success; use `ObjectStoreReadError`/`ObjectStoreDownloadError`. ENOSPC should surface as `DiskFullError`.
- Flag: keys without tenant/run scoping; credential files read through the data object-store binding.
- Flag: storage errors re-raised without key/path context.
- Severity: critical for credential/data store mixing or cross-tenant keys; high for truncated artifacts, silent empty hand-off, swapped key/path; medium otherwise.
