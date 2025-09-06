# Input Code Review Guidelines - Data Input Processing

## Context-Specific Patterns

This directory contains input processing implementations for various data formats (JSON, Parquet, SQL). Input processors must handle data efficiently while maintaining data integrity and performance.

### Phase 1: Critical Input Safety Issues

**Object Store Path Management:**

- **Correct path calculation**: Source paths must use the actual object store prefix, not derived local paths
- **Path validation**: Verify that object store keys are valid and within constraints
- **User-provided prefixes**: Respect user-configured input prefixes and download paths
- **Path consistency**: Ensure downloaded files match the expected object store locations

**Data Validation and Security:**

- All input data must be validated before processing
- File size limits must be enforced to prevent resource exhaustion
- File type validation required for uploaded/downloaded files
- Malicious file content detection for executable or script files
- Input path traversal prevention

```python
# ✅ DO: Proper object store path handling
class JsonInput:
    async def download_from_object_store(
        self,
        input_prefix: str,  # User-provided prefix
        local_destination: str
    ) -> List[str]:
        """Download files with correct path handling."""

        # Use the actual input prefix, not derived local path
        object_store_source = input_prefix  # Keep user's intended source

        downloaded_files = await self.object_store.download_files(
            source=object_store_source,
            destination=local_destination
        )

        return downloaded_files

# ❌ REJECT: Incorrect path handling
class BadJsonInput:
    async def download_from_object_store(
        self,
        input_prefix: str,
        local_destination: str
    ) -> List[str]:
        # Wrong: derives object store path from local path
        object_store_source = get_object_store_prefix(local_destination)
        # This ignores the user's actual input_prefix!

        return await self.object_store.download_files(
            source=object_store_source,  # Wrong source!
            destination=local_destination
        )
```

### Phase 2: Input Architecture Patterns

**Performance Optimization Requirements:**

- **Parallelization opportunities**: Flag sequential file operations that could be parallelized
- **Batch processing**: Group related operations to reduce overhead
- **Memory efficiency**: Process large files in chunks, not all at once
- **Connection reuse**: Optimize object store connections across operations

**Resource Management:**

- Use proper connection pooling for object store operations
- Implement timeout handling for download operations
- Clean up temporary files after processing
- Handle partial download failures gracefully
- Monitor memory usage during large file processing

```python
# ✅ DO: Parallelized file processing
async def download_multiple_files_parallel(
    self,
    file_paths: List[str],
    destination_dir: str
) -> List[str]:
    """Download multiple files in parallel for better performance."""

    async def download_single_file(file_path: str) -> str:
        """Download a single file with error handling."""
        try:
            return await self.object_store.download_file(
                source=file_path,
                destination=os.path.join(destination_dir, os.path.basename(file_path))
            )
        except Exception as e:
            logger.error(f"Failed to download {file_path}: {e}")
            raise

    # Parallel processing with controlled concurrency
    semaphore = asyncio.Semaphore(10)  # Limit concurrent downloads

    async def download_with_semaphore(file_path: str) -> str:
        async with semaphore:
            return await download_single_file(file_path)

    tasks = [download_with_semaphore(path) for path in file_paths]
    return await asyncio.gather(*tasks)

# ❌ REJECT: Sequential processing
async def download_multiple_files_sequential(self, file_paths: List[str]) -> List[str]:
    """Sequential download - should be flagged for parallelization."""
    downloaded = []
    for file_path in file_paths:  # FLAG: Could be parallelized
        result = await self.object_store.download_file(file_path)
        downloaded.append(result)
    return downloaded
```

### Phase 3: Input Testing Requirements

**Data Input Testing:**

- Test with various file formats and sizes
- Test malformed data handling
- Test partial download/upload scenarios
- Mock object store operations in unit tests
- Include integration tests with real object store
- Test error recovery and retry logic

**Performance Testing:**

- Include tests for large file processing
- Test memory usage with different chunk sizes
- Test concurrent download/upload operations
- Verify timeout handling works correctly
- Test connection pool behavior

### Phase 4: Performance and Scalability

**Data Processing Efficiency:**

- Use streaming for large files instead of loading entirely into memory
- Implement proper chunking for batch operations
- Use async generators for memory-efficient data processing
- Monitor memory usage and processing time
- Optimize file I/O operations

**Object Store Optimization:**

- Use connection pooling for object store clients
- Implement proper retry logic for transient failures
- Use parallel operations where appropriate
- Cache frequently accessed metadata
- Monitor object store operation metrics

### Phase 5: Input Data Maintainability

**Error Handling and Recovery:**

- Implement comprehensive error handling for all input operations
- Provide meaningful error messages with context
- Handle partial failures gracefully (some files fail, others succeed)
- Implement proper retry logic for transient failures
- Log all input operations with sufficient context

**Configuration Management:**

- Externalize all input-related configuration
- Support different input sources and formats
- Validate input configuration before processing
- Document all supported input parameters
- Handle environment-specific input requirements

---

## Input-Specific Anti-Patterns

**Always Reject:**

- **Path calculation errors**: Using local paths to derive object store paths
- **Sequential processing**: Processing multiple files sequentially when parallel processing is possible
- **Memory inefficiency**: Loading large files entirely into memory
- **Missing error handling**: Input operations without proper try-catch blocks
- **Poor path validation**: Not validating object store keys or file paths
- **Resource leaks**: Not cleaning up temporary files or connections

**Object Store Anti-Patterns:**

```python
# ❌ REJECT: Incorrect object store usage
class BadInputProcessor:
    async def process_files(self, local_files: List[str]):
        # Wrong: derives object store path from local path
        for local_file in local_files:
            object_store_key = get_object_store_prefix(local_file)  # Incorrect!
            await self.object_store.download_file(object_store_key, local_file)

# ✅ REQUIRE: Correct object store usage
class GoodInputProcessor:
    async def process_files(
        self,
        object_store_paths: List[str],  # Actual object store paths
        local_destination_dir: str
    ):
        # Use actual object store paths, not derived ones
        for object_store_path in object_store_paths:
            local_file = os.path.join(
                local_destination_dir,
                os.path.basename(object_store_path)
            )
            await self.object_store.download_file(object_store_path, local_file)
```

**Performance Anti-Patterns:**

```python
# ❌ REJECT: Sequential file processing
async def process_files_sequential(file_list: List[str]):
    results = []
    for file_path in file_list:  # Should be parallelized
        result = await process_single_file(file_path)
        results.append(result)
    return results

# ✅ REQUIRE: Parallel file processing
async def process_files_parallel(file_list: List[str], max_concurrency: int = 10):
    semaphore = asyncio.Semaphore(max_concurrency)

    async def process_with_semaphore(file_path: str):
        async with semaphore:
            return await process_single_file(file_path)

    tasks = [process_with_semaphore(path) for path in file_list]
    return await asyncio.gather(*tasks, return_exceptions=True)
```

## Educational Context for Input Reviews

When reviewing input code, emphasize:

1. **Data Integrity Impact**: "Incorrect object store path handling can cause data loss or corruption. Files uploaded to wrong locations become inaccessible, breaking data processing pipelines."

2. **Performance Impact**: "Sequential file processing creates unnecessary bottlenecks. For enterprise datasets with hundreds of files, parallelization can reduce processing time from hours to minutes."

3. **Resource Impact**: "Poor memory management in input processing can cause out-of-memory errors with large datasets. Streaming and chunking are essential for enterprise-scale data processing."

4. **User Experience Impact**: "Input path handling errors are often silent until runtime, causing difficult-to-debug failures. Proper validation and clear error messages save hours of troubleshooting."

5. **Scalability Impact**: "Input processing patterns that work for small datasets can fail catastrophically at enterprise scale. Always design for the largest expected dataset size."

6. **Reliability Impact**: "Input operations are often the first point of failure in data pipelines. Robust error handling and retry logic in input processing prevents entire workflows from failing due to transient issues."
