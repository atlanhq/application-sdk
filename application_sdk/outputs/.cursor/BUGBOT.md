# Output Code Review Guidelines - Data Output Processing

## Context-Specific Patterns

This directory contains output processing implementations for various data formats (JSON, Parquet, Iceberg). Output processors must handle data uploads efficiently while maintaining data integrity and correct destination paths.

### Phase 1: Critical Output Safety Issues

**Object Store Path Management:**

- **Correct destination paths**: Upload paths must respect user-configured output prefixes
- **Path construction accuracy**: Object store keys must be calculated correctly, not hardcoded
- **User prefix preservation**: Respect user-provided output directories and naming conventions
- **Path validation**: Ensure upload paths don't conflict with existing data

**Data Integrity and Security:**

- All output data must be validated before upload
- File permissions and access controls must be properly set
- Data serialization must be consistent and recoverable
- Prevent overwriting critical data without confirmation
- Maintain data lineage information in output metadata

```python
# ✅ DO: Proper object store upload path handling
class JsonOutput:
    async def upload_to_object_store(
        self,
        data: List[dict],
        output_prefix: str,  # User-provided output location
        filename: str
    ) -> dict:
        """Upload data with correct path handling."""

        # Construct full object store path respecting user's output prefix
        object_store_key = os.path.join(output_prefix, filename)

        # Serialize data
        json_data = orjson.dumps(data, option=orjson.OPT_APPEND_NEWLINE)

        # Upload to correct location
        result = await self.object_store.upload_file(
            data=json_data,
            destination=object_store_key  # Respect user's intended location
        )

        return result

# ❌ REJECT: Incorrect path handling
class BadJsonOutput:
    async def upload_to_object_store(self, data: List[dict], filename: str):
        # Wrong: hardcoded or derived path, ignoring user configuration
        object_store_key = get_object_store_prefix(f"/tmp/{filename}")  # Ignores output_prefix!

        result = await self.object_store.upload_file(
            data=orjson.dumps(data),
            destination=object_store_key  # Wrong destination!
        )
        return result
```

### Phase 2: Output Architecture Patterns

**Performance Optimization Requirements:**

- **Parallelization opportunities**: Flag sequential upload operations that could be parallelized
- **Batch processing**: Group related uploads to reduce overhead
- **Streaming uploads**: Use streaming for large datasets instead of loading into memory
- **Connection optimization**: Reuse object store connections across operations

**Resource Management:**

- Use proper connection pooling for object store operations
- Implement timeout handling for upload operations
- Clean up temporary files after upload
- Handle partial upload failures gracefully
- Monitor memory usage during large data serialization

```python
# ✅ DO: Parallel upload processing
async def upload_multiple_datasets_parallel(
    self,
    datasets: List[Tuple[List[dict], str]],  # (data, filename) pairs
    output_prefix: str
) -> List[dict]:
    """Upload multiple datasets in parallel for better performance."""

    async def upload_single_dataset(data: List[dict], filename: str) -> dict:
        """Upload a single dataset with error handling."""
        try:
            object_store_key = os.path.join(output_prefix, filename)
            serialized_data = orjson.dumps(data, option=orjson.OPT_APPEND_NEWLINE)

            return await self.object_store.upload_file(
                data=serialized_data,
                destination=object_store_key
            )
        except Exception as e:
            logger.error(f"Failed to upload {filename}: {e}")
            raise

    # Parallel processing with controlled concurrency
    semaphore = asyncio.Semaphore(5)  # Limit concurrent uploads

    async def upload_with_semaphore(data: List[dict], filename: str) -> dict:
        async with semaphore:
            return await upload_single_dataset(data, filename)

    tasks = [upload_with_semaphore(data, filename) for data, filename in datasets]
    return await asyncio.gather(*tasks)

# ❌ REJECT: Sequential upload processing
async def upload_multiple_datasets_sequential(
    self,
    datasets: List[Tuple[List[dict], str]],
    output_prefix: str
) -> List[dict]:
    """Sequential uploads - should be flagged for parallelization."""
    results = []
    for data, filename in datasets:  # FLAG: Could be parallelized
        object_store_key = os.path.join(output_prefix, filename)
        result = await self.object_store.upload_file(data, object_store_key)
        results.append(result)
    return results
```

### Phase 3: Output Testing Requirements

**Data Output Testing:**

- Test with various data formats and sizes
- Test serialization and deserialization consistency
- Test partial upload scenarios and recovery
- Mock object store operations in unit tests
- Include integration tests with real object store
- Test data corruption detection and prevention

**Performance Testing:**

- Include tests for large dataset uploads
- Test memory usage during serialization
- Test concurrent upload operations
- Verify timeout handling works correctly
- Test connection pool behavior under load

### Phase 4: Performance and Scalability

**Data Upload Efficiency:**

- Use streaming uploads for large datasets
- Implement proper chunking for oversized data
- Use compression for large text-based outputs
- Monitor upload progress and provide feedback
- Optimize serialization performance (use orjson over json)

**Object Store Optimization:**

- Use connection pooling for object store clients
- Implement proper retry logic for upload failures
- Use parallel uploads where appropriate
- Monitor upload metrics and error rates
- Handle bandwidth limitations gracefully

### Phase 5: Output Maintainability

**Error Handling and Recovery:**

- Implement comprehensive error handling for all upload operations
- Provide meaningful error messages with upload context
- Handle partial upload failures gracefully
- Implement proper retry logic for transient failures
- Log all upload operations with destination information

**Configuration Management:**

- Externalize all output-related configuration
- Support different output destinations and formats
- Validate output configuration before processing
- Document all supported output parameters
- Handle environment-specific output requirements

---

## Output-Specific Anti-Patterns

**Always Reject:**

- **Path derivation errors**: Deriving object store paths from local temporary paths
- **Sequential uploads**: Uploading multiple files sequentially when parallel uploads are possible
- **Memory inefficiency**: Loading entire datasets into memory for serialization
- **Missing upload verification**: Not verifying successful uploads
- **Poor error recovery**: Not handling partial upload failures gracefully
- **Resource leaks**: Not cleaning up temporary files or connections

**Object Store Upload Anti-Patterns:**

```python
# ❌ REJECT: Incorrect upload path handling
class BadOutputProcessor:
    async def upload_results(self, results: List[dict]):
        # Wrong: derives upload path from temporary local path
        local_temp_file = "/tmp/results.json"
        upload_key = get_object_store_prefix(local_temp_file)  # Incorrect!

        await self.object_store.upload_file(results, upload_key)

# ✅ REQUIRE: Correct upload path handling
class GoodOutputProcessor:
    async def upload_results(
        self,
        results: List[dict],
        output_prefix: str,  # User-specified destination
        filename: str = "results.json"
    ):
        # Use actual user-configured output location
        upload_key = os.path.join(output_prefix, filename)

        await self.object_store.upload_file(
            data=orjson.dumps(results),
            destination=upload_key  # Correct destination
        )
```

**Performance Anti-Patterns:**

```python
# ❌ REJECT: Sequential upload processing
async def upload_multiple_files_sequential(file_data_pairs: List[Tuple]):
    results = []
    for data, filename in file_data_pairs:  # Should be parallelized
        result = await upload_single_file(data, filename)
        results.append(result)
    return results

# ✅ REQUIRE: Parallel upload processing with proper error handling
async def upload_multiple_files_parallel(
    file_data_pairs: List[Tuple],
    max_concurrency: int = 5
) -> List[dict]:
    semaphore = asyncio.Semaphore(max_concurrency)

    async def upload_with_semaphore(data, filename):
        async with semaphore:
            try:
                return await upload_single_file(data, filename)
            except Exception as e:
                logger.error(f"Upload failed for {filename}: {e}")
                return {"filename": filename, "status": "failed", "error": str(e)}

    tasks = [upload_with_semaphore(data, filename) for data, filename in file_data_pairs]
    return await asyncio.gather(*tasks)
```

**Memory Management Anti-Patterns:**

```python
# ❌ REJECT: Loading entire dataset for serialization
async def bad_large_dataset_upload(large_dataset: List[dict]):
    # Loads entire dataset into memory
    json_data = orjson.dumps(large_dataset)  # Could exceed memory limits
    await upload_data(json_data)

# ✅ REQUIRE: Streaming serialization for large datasets
async def good_large_dataset_upload(large_dataset: List[dict], chunk_size: int = 1000):
    """Stream large datasets to avoid memory issues."""

    async def serialize_chunk(chunk: List[dict]) -> bytes:
        return orjson.dumps(chunk, option=orjson.OPT_APPEND_NEWLINE)

    # Process in chunks to manage memory
    for i in range(0, len(large_dataset), chunk_size):
        chunk = large_dataset[i:i + chunk_size]
        serialized_chunk = await serialize_chunk(chunk)

        await upload_chunk(
            data=serialized_chunk,
            chunk_index=i // chunk_size
        )
```

## Educational Context for Output Reviews

When reviewing output code, emphasize:

1. **Data Integrity Impact**: "Incorrect upload path handling can cause data to be stored in wrong locations, making it inaccessible to downstream processes. This breaks data pipelines and can cause data loss."

2. **Performance Impact**: "Sequential uploads create unnecessary bottlenecks. For enterprise datasets with multiple output files, parallelization can significantly reduce processing time and improve user experience."

3. **Resource Impact**: "Poor memory management during serialization can cause out-of-memory errors with large datasets. Streaming and chunking are essential for enterprise-scale data output."

4. **User Experience Impact**: "Output path errors are often discovered late in processing, causing wasted computation and frustrating delays. Proper validation and clear error messages improve reliability."

5. **Scalability Impact**: "Output patterns that work for small datasets can fail at enterprise scale. Always design output processes to handle the largest expected dataset sizes efficiently."

6. **Data Pipeline Impact**: "Output processing is the final step in data pipelines. Failures here can invalidate all upstream processing work. Robust error handling and verification are critical for pipeline reliability."
