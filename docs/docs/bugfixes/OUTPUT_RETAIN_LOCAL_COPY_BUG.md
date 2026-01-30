# Bug Fix: `retain_local_copy` Parameter Not Used in Output Classes

## Summary

The `retain_local_copy` parameter in `JsonOutput`, `ParquetOutput`, and other output classes was accepted but **never actually used** when uploading files to the object store. This caused files to always be deleted after upload, regardless of the parameter value.

## Affected Versions

- All versions prior to the fix
- Affected classes: `JsonOutput`, `ParquetOutput`, `IcebergOutput`

## Bug Details

### Symptom

When using output classes with `retain_local_copy=True`, local files were still being deleted after upload to S3/object store.

```python
# User expected this to keep local files:
transformed_output = JsonOutput(
    output_path=output_path,
    output_suffix="transformed",
    typename=typename,
    retain_local_copy=True,  # ❌ This was ignored!
)
```

### Root Cause

The `Output` base class stored the `retain_local_copy` attribute but the `_upload_file` method did not pass it to `ObjectStore.upload_file`:

**Before (Buggy):**
```python
# application_sdk/outputs/__init__.py

class Output(ABC):
    def __init__(self, ...):
        self.retain_local_copy = retain_local_copy  # ✓ Stored correctly
    
    async def _upload_file(self, file_name: str):
        """Upload a file to the object store."""
        await ObjectStore.upload_file(
            source=file_name,
            destination=get_object_store_prefix(file_name),
            # ❌ Missing: retain_local_copy parameter
        )
```

The `ObjectStore.upload_file` method defaults to `retain_local_copy=False`, which deletes local files after upload:

```python
# application_sdk/services/objectstore.py

@classmethod
async def upload_file(
    cls,
    source: str,
    destination: str,
    store_name: str = DEPLOYMENT_OBJECT_STORE_NAME,
    retain_local_copy: bool = False,  # Default: DELETE local files
) -> None:
    # ... upload logic ...
    
    # Clean up local file after successful upload
    if not retain_local_copy:
        cls._cleanup_local_path(source)  # ❌ Always called when not specified
```

### Impact

This bug affected any workflow that needed to:

1. **Access transformed data after upload** - Activities running after transform couldn't find local files
2. **Create current-state snapshots** - `write_current_state` activities failed because transformed files were deleted
3. **Chain activities that share local data** - Any activity depending on local files from a previous activity failed

Example error:
```
FileNotFoundError: No transformed files found at local/tmp/artifacts/.../transformed. 
Ensure transform_data completed with retain_local_copy=True.
```

## The Fix

**After (Fixed):**
```python
# application_sdk/outputs/__init__.py

async def _upload_file(self, file_name: str):
    """Upload a file to the object store."""
    await ObjectStore.upload_file(
        source=file_name,
        destination=get_object_store_prefix(file_name),
        retain_local_copy=getattr(self, "retain_local_copy", False),  # ✓ Now used
    )
```

The fix uses `getattr(self, "retain_local_copy", False)` to:
- Use the instance's `retain_local_copy` attribute if set
- Default to `False` (original behavior) if not set, maintaining backward compatibility

## Usage After Fix

Now `retain_local_copy=True` works as expected:

```python
# Keep local files after S3 upload
transformed_output = JsonOutput(
    output_path=output_path,
    output_suffix="transformed",
    typename=typename,
    retain_local_copy=True,  # ✓ Now works correctly!
)

await transformed_output.write_daft_dataframe(data)
# Files are uploaded to S3 AND kept locally
```

## Testing the Fix

To verify the fix works:

```python
import os
from application_sdk.outputs.json import JsonOutput

# Test with retain_local_copy=True
output = JsonOutput(
    output_path="/tmp/test",
    output_suffix="test",
    retain_local_copy=True,
)

# Write some data
await output.write_dataframe(test_df)
await output.get_statistics()

# Verify local files still exist
assert os.path.exists("/tmp/test/test/0.json"), "Local files should be retained"
```

## Related Files

- `application_sdk/outputs/__init__.py` - Base `Output` class with `_upload_file` method
- `application_sdk/outputs/json.py` - `JsonOutput` class
- `application_sdk/outputs/parquet.py` - `ParquetOutput` class  
- `application_sdk/services/objectstore.py` - `ObjectStore.upload_file` method

## Commit Reference

Fix applied to `application_sdk/outputs/__init__.py`:
```diff
     async def _upload_file(self, file_name: str):
         """Upload a file to the object store."""
         await ObjectStore.upload_file(
             source=file_name,
             destination=get_object_store_prefix(file_name),
+            retain_local_copy=getattr(self, "retain_local_copy", False),
         )
```

