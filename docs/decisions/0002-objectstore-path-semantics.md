# 0002: ObjectStore Path Semantics

**Status**: Accepted

## Context

The SDK operates in environments where code references files by local filesystem paths (e.g., `./local/tmp/artifacts/...`) and by object store keys (e.g., `artifacts/...`). Activities and services need to accept both forms seamlessly without requiring callers to manually convert between them.

## Decision

### Path normalization via `as_store_key()`

`ObjectStore.as_store_key(path)` normalizes any input path to a relative object store key:
- Strips the `TEMPORARY_PATH` (`./local/tmp/`) prefix if the path falls under it
- Converts absolute paths to relative keys
- Returns forward-slash-separated keys with no leading or trailing `/`
- Treats temp-root `.` as empty string to avoid malformed keys

### Key/prefix params accept either form

All ObjectStore API methods that accept `key` or `prefix` parameters run inputs through `as_store_key()`. This means callers can pass:
- `./local/tmp/artifacts/workflow-123/output.parquet` (local workflow path)
- `artifacts/workflow-123/output.parquet` (object store key)

Both resolve to the same object store key.

### Local file params are never normalized

Parameters that represent local filesystem paths — specifically `source` in upload methods and `destination` in download methods — are **not** run through `as_store_key()`. These remain literal filesystem paths.

## Consequences

- **Pro**: Callers don't need to know whether they're holding a local path or a store key
- **Pro**: Path conversion bugs are centralized in one function
- **Con**: The implicit normalization can mask bugs where a local path is accidentally used as a key
- **Con**: Developers must remember the asymmetry: key/prefix are normalized, source/destination are not

## References

- `application_sdk/services/objectstore.py` — `as_store_key()`, upload/download methods
- `application_sdk/constants.py` — `TEMPORARY_PATH`
