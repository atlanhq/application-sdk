# Activity Code Review Guidelines - Temporal Activities

## Context-Specific Patterns

This directory contains Temporal activity implementations that perform the actual work of workflows. Activities handle external I/O, database operations, and non-deterministic tasks.

### Phase 1: Critical Activity Safety Issues

**External Resource Safety:**

- All external connections (database, API, file) must have explicit timeouts
- Connection failures must be handled gracefully with proper retry logic
- Resource cleanup must happen in finally blocks or context managers
- Sensitive data must not be logged or exposed in error messages
- All user inputs must be validated before processing

**Activity Timeout Management:**

- Activities must respect Temporal heartbeat timeouts for long-running operations
- Progress should be reported via heartbeat for operations > 30 seconds
- Activities should check for cancellation requests periodically
- Timeout values must be realistic for the operation being performed

```python
# ✅ DO: Proper activity with heartbeat and cancellation
@activity.defn
async def process_large_dataset_activity(dataset_config: dict) -> dict:
    total_records = await get_record_count(dataset_config)
    processed = 0

    async for batch in process_in_batches(dataset_config):
        # Check for cancellation
        activity.heartbeat({"progress": processed, "total": total_records})

        try:
            await process_batch(batch)
            processed += len(batch)
        except Exception as e:
            activity.logger.error(f"Batch processing failed: {e}", exc_info=True)
            raise

    return {"processed_records": processed}

# ❌ NEVER: Long-running activity without heartbeat
@activity.defn
async def bad_process_activity(data):
    # No heartbeat, no cancellation check, no progress reporting
    return await process_all_data_at_once(data)
```

### Phase 2: Activity Architecture Patterns

**Resource Management:**

- Use connection pooling for database operations
- Implement proper connection context managers
- Clean up temporary files and resources
- Handle partial failures gracefully
- Implement idempotent operations where possible

**Error Handling and Retries:**

- Distinguish between retryable and non-retryable errors
- Use specific exception types for different error conditions
- Log errors with sufficient context for debugging
- Implement exponential backoff for retryable operations
- Preserve error context across retries

```python
# ✅ DO: Proper error handling with context
@activity.defn
async def extract_metadata_activity(connection_config: dict) -> dict:
    client = None
    try:
        client = await create_database_client(connection_config)
        await client.validate_connection()

        metadata = await client.extract_metadata()

        activity.logger.info(
            f"Extracted metadata for {len(metadata)} objects",
            extra={"database": connection_config.get("database", "unknown")}
        )

        return metadata

    except ConnectionError as e:
        # Retryable error
        activity.logger.warning(f"Connection failed, will retry: {e}")
        raise  # Let Temporal handle retry

    except ValidationError as e:
        # Non-retryable error
        activity.logger.error(f"Invalid connection config: {e}")
        raise ApplicationError(f"Configuration validation failed: {e}", non_retryable=True)

    finally:
        if client:
            await client.close()
```

### Phase 3: Activity Testing Requirements

**Activity Testing Standards:**

- Test activities independently from workflows
- Mock external dependencies (databases, APIs, file systems)
- Test timeout and cancellation behaviors
- Test retry scenarios with different error types
- Include performance tests for long-running activities
- Test heartbeat and progress reporting

**Integration Testing:**

- Use test databases/services for integration tests
- Test real connection failures and recovery
- Verify proper resource cleanup
- Test activity behavior under load
- Include end-to-end tests with real workflows

### Phase 4: Performance and Scalability

**Activity Performance:**

- Use async/await for all I/O operations
- Implement proper batching for bulk operations
- Use streaming for large datasets
- Monitor activity execution time and resource usage
- Optimize database queries and API calls

**Memory Management:**

- Process large datasets in chunks, not all at once
- Use generators for memory-efficient iteration
- Clean up large objects explicitly
- Monitor memory usage in long-running activities
- Use appropriate data types and structures

```python
# ✅ DO: Memory-efficient processing
@activity.defn
async def process_large_file_activity(file_path: str, chunk_size: int = 1000) -> dict:
    processed_count = 0

    async with aiofiles.open(file_path, 'r') as file:
        chunk = []
        async for line in file:
            chunk.append(line.strip())

            if len(chunk) >= chunk_size:
                await process_chunk(chunk)
                processed_count += len(chunk)
                chunk = []

                # Report progress and check for cancellation
                activity.heartbeat({"processed": processed_count})

        # Process remaining items
        if chunk:
            await process_chunk(chunk)
            processed_count += len(chunk)

    return {"total_processed": processed_count}

# ❌ NEVER: Load entire file into memory
@activity.defn
async def bad_file_activity(file_path: str):
    with open(file_path, 'r') as file:
        all_lines = file.readlines()  # Memory intensive!
    return process_all_lines(all_lines)
```

### Phase 5: Activity Maintainability

**Code Organization:**

- Keep activities focused on a single responsibility
- Use dependency injection for external services
- Implement proper logging with activity context
- Document activity parameters and return values
- Follow consistent naming conventions

**Configuration and Environment:**

- Externalize all configuration parameters
- Use environment-specific settings appropriately
- Validate configuration before using it
- Support development and production configurations
- Document all required configuration options

---

## Activity-Specific Anti-Patterns

**Always Reject:**

- Activities without proper timeout handling
- Long-running activities without heartbeat reporting
- Missing resource cleanup (connections, files, etc.)
- Generic exception handling without specific error types
- Activities that don't handle cancellation
- Synchronous I/O operations in async activities
- Missing logging for error conditions
- Activities without proper input validation

**Resource Management Anti-Patterns:**

```python
# ❌ REJECT: Poor resource management
@activity.defn
async def bad_database_activity(query: str):
    # No connection pooling, no cleanup, no error handling
    conn = await psycopg.connect("host=localhost...")
    result = await conn.execute(query)  # No timeout
    return result.fetchall()  # Connection never closed

# ✅ REQUIRE: Proper resource management
@activity.defn
async def good_database_activity(query: str, params: tuple = ()) -> list:
    async with get_connection_pool().acquire() as conn:
        try:
            # Set query timeout
            async with conn.cursor() as cursor:
                await cursor.execute(query, params)
                return await cursor.fetchall()
        except Exception as e:
            activity.logger.error(f"Database query failed: {query[:100]}...", exc_info=True)
            raise
        # Connection automatically returned to pool
```

**Heartbeat and Cancellation Anti-Patterns:**

```python
# ❌ REJECT: No heartbeat or cancellation handling
@activity.defn
async def bad_long_running_activity(data_list: list):
    results = []
    for item in data_list:  # Could take hours
        result = await expensive_operation(item)
        results.append(result)
    return results

# ✅ REQUIRE: Proper heartbeat and cancellation
@activity.defn
async def good_long_running_activity(data_list: list) -> list:
    results = []
    total_items = len(data_list)

    for i, item in enumerate(data_list):
        # Check for cancellation and report progress
        activity.heartbeat({
            "processed": i,
            "total": total_items,
            "percent_complete": (i / total_items) * 100
        })

        try:
            result = await expensive_operation(item)
            results.append(result)
        except Exception as e:
            activity.logger.error(f"Processing failed for item {i}: {e}")
            raise

    return results
```

## Educational Context for Activity Reviews

When reviewing activity code, emphasize:

1. **Reliability Impact**: "Activities are where the real work happens. Proper error handling and resource management in activities determines whether workflows succeed or fail under real-world conditions."

2. **Performance Impact**: "Activity performance directly affects workflow execution time. Inefficient activities create bottlenecks that slow down entire business processes."

3. **Observability Impact**: "Activity logging and heartbeat reporting are essential for monitoring long-running processes. Without proper observability, debugging workflow issues becomes nearly impossible."

4. **Resource Impact**: "Activities consume actual system resources. Poor resource management in activities can cause memory leaks, connection pool exhaustion, and system instability."

5. **Cancellation Impact**: "Activities that don't handle cancellation properly can continue consuming resources even after workflows are cancelled, leading to resource waste and potential system overload."
