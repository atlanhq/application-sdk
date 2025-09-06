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

**Default Value Management:**

- **Always define sensible defaults**: Activity parameters should have reasonable default values where appropriate
- **Avoid required parameters for inferable values**: Values like `owner_id` that can be derived (e.g., from `application_name:run_id`) should not be required parameters
- **Default TTL values**: Lock operations, cache entries, and timeouts should have documented default values (e.g., 300 seconds for locks)
- **Environment-based defaults**: Different environments (dev/prod) may need different defaults

```python
# ✅ DO: Proper default value management
@activity.defn
async def acquire_distributed_lock_activity(
    lock_name: str,
    max_locks: int = 10,  # Sensible default
    ttl_seconds: int = 300,  # 5 minutes default
    owner_id: Optional[str] = None  # Will be inferred
) -> dict:
    """Acquire a distributed lock with proper defaults."""

    # Infer owner_id if not provided
    if owner_id is None:
        workflow_info = activity.info().workflow_execution
        owner_id = f"{workflow_info.workflow_type}:{workflow_info.workflow_id}"

    # Validate parameters
    if max_locks <= 0:
        raise ValueError(f"max_locks must be positive, got: {max_locks}")

    return await lock_manager.acquire_lock(lock_name, max_locks, ttl_seconds, owner_id)

# ❌ REJECT: Poor parameter management
@activity.defn
async def bad_acquire_lock_activity(
    lock_name: str,
    max_locks: int,  # No default
    ttl_seconds: int,  # No default
    owner_id: str,  # Required but could be inferred
    application_name: str,  # Redundant - should be inferred
    run_id: str  # Redundant - should be inferred
) -> dict:
    # Forces users to pass values that could be automatically determined
    pass
```

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

**Resource Validation and Limits:**

- **Key length validation**: Ensure generated keys (Redis, cache) don't exceed system limits
- **Memory constraints**: Validate that operations won't exceed available memory
- **Connection limits**: Check that concurrent operations stay within connection pool limits
- **Processing time estimates**: Validate that operations can complete within activity timeouts

```python
# ✅ DO: Resource validation
@activity.defn
async def process_with_validation_activity(
    resource_name: str,
    data_size_mb: int,
    max_processing_time_minutes: int = 30
) -> dict:
    """Process data with proper resource validation."""

    # Validate resource constraints
    if len(resource_name.encode('utf-8')) > 512 * 1024 * 1024:  # 512MB Redis key limit
        raise ValueError(f"Resource name too long: {len(resource_name)} bytes")

    if data_size_mb > 1000:  # 1GB memory limit
        raise ValueError(f"Data size {data_size_mb}MB exceeds 1GB limit")

    # Validate processing time against activity timeout
    activity_timeout = activity.info().start_to_close_timeout
    if max_processing_time_minutes * 60 > activity_timeout.total_seconds():
        raise ValueError(f"Processing time {max_processing_time_minutes}m exceeds timeout")

    return await process_data(resource_name, data_size_mb)
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

**Parallelization Opportunities:**

- **Flag sequential operations**: When processing multiple files or resources, suggest parallel processing
- **Batch operations**: Group related operations to reduce overhead
- **Connection reuse**: Optimize connection usage across operations
- **Async patterns**: Ensure I/O operations don't block other processing

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

**Error Context Enhancement:**

- **Operation identification**: Include the specific operation that failed in error messages
- **Parameter context**: Log relevant parameters (sanitized) when operations fail
- **Resource state**: Include information about resource availability/state in errors
- **Recovery suggestions**: Where possible, include suggestions for resolving errors

```python
# ✅ DO: Enhanced error context
@activity.defn
async def enhanced_error_activity(
    database_name: str,
    table_names: List[str],
    timeout_seconds: int = 300
) -> dict:
    """Activity with comprehensive error context."""

    try:
        result = await extract_table_metadata(database_name, table_names, timeout_seconds)
        return result

    except ConnectionTimeout as e:
        activity.logger.error(
            f"Database connection timeout during metadata extraction",
            extra={
                "database": database_name,
                "tables_requested": len(table_names),
                "timeout_used": timeout_seconds,
                "suggestion": "Consider increasing timeout or reducing table count"
            }
        )
        raise ApplicationError(
            f"Metadata extraction timed out after {timeout_seconds}s for database '{database_name}' "
            f"with {len(table_names)} tables. Consider reducing scope or increasing timeout.",
            non_retryable=True
        )

    except InsufficientPrivileges as e:
        activity.logger.error(
            f"Insufficient database privileges for metadata extraction",
            extra={
                "database": database_name,
                "required_privileges": ["SELECT", "INFORMATION_SCHEMA_READ"],
                "suggestion": "Grant required database privileges to connection user"
            }
        )
        raise ApplicationError(
            f"Missing database privileges for '{database_name}'. "
            f"Ensure connection user has SELECT and INFORMATION_SCHEMA access.",
            non_retryable=True
        )
```

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

**Parameter Management Anti-Patterns:**

- **Over-parameterization**: Requiring parameters that can be inferred from context
- **Missing defaults**: Parameters without reasonable default values
- **No validation**: Accepting parameters without validating constraints
- **Redundant parameters**: Multiple parameters representing the same concept

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

6. **Parameter Design Impact**: "Well-designed activity parameters with sensible defaults make activities easier to use and less error-prone. Over-parameterization creates maintenance burden and increases the chance of configuration errors."
