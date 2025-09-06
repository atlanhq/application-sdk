# Interceptor Code Review Guidelines - Temporal Interceptors

## Context-Specific Patterns

This directory contains Temporal interceptor implementations that provide cross-cutting functionality like distributed locking, observability, and event handling. Interceptors must be robust and not interfere with normal workflow/activity execution.

### Phase 1: Critical Interceptor Safety Issues

**Infinite Loop Prevention:**

- **Lock acquisition loops must have termination conditions**: No `while True` loops without max retries or timeouts
- **Bounded retry logic**: All retry mechanisms must have explicit limits
- **Timeout enforcement**: Operations must respect activity and workflow timeouts
- **Resource exhaustion prevention**: Prevent scenarios that could consume all available resources

**Resource Management in Interceptors:**

- **Context manager handling**: Ensure proper cleanup when context managers fail
- **Connection lifecycle**: Don't hold connections longer than necessary
- **Lock timing**: Acquire locks as late as possible, release as early as possible
- **Error state cleanup**: Clean up resources even when intercepted operations fail

```python
# ✅ DO: Bounded lock acquisition with proper cleanup
class GoodLockInterceptor:
    async def intercept_activity(self, next_fn, input):
        """Intercept with bounded retry and proper cleanup."""
        max_retries = 10
        retry_count = 0

        while retry_count < max_retries:  # Bounded loop
            try:
                # Acquire lock with timeout
                async with self.lock_manager.acquire_lock(
                    lock_name=self.lock_name,
                    timeout=30  # Maximum lock wait time
                ) as lock:
                    # Execute activity while holding lock
                    return await next_fn(input)

            except LockUnavailableError:
                retry_count += 1
                if retry_count >= max_retries:
                    raise LockAcquisitionError(
                        f"Failed to acquire lock after {max_retries} attempts"
                    )

                # Exponential backoff, but bounded
                delay = min(10 + random.randint(0, 10), 60)  # Max 60 seconds
                await asyncio.sleep(delay)  # Non-blocking sleep

        raise LockAcquisitionError("Lock acquisition retries exhausted")

# ❌ NEVER: Infinite loops and poor resource management
class BadLockInterceptor:
    async def intercept_activity(self, next_fn, input):
        while True:  # INFINITE LOOP!
            try:
                with self.dapr_client.try_lock(lock_name) as response:
                    if response.success:
                        result = await next_fn(input)
                        return result
                    else:
                        time.sleep(10)  # BLOCKING SLEEP IN ASYNC!
                        # SLEEP INSIDE CONTEXT MANAGER - RESOURCE LEAK!

            except Exception as e:
                raise  # No retry for transient errors
```

**Parameter Validation and Edge Cases:**

- **Range validation**: Validate parameters that are used in range operations (random.randint, array slicing)
- **Zero and negative handling**: Explicitly handle edge cases like 0 values, negative numbers
- **Type validation**: Ensure interceptor parameters match expected types
- **Configuration validation**: Validate interceptor configuration before use

```python
# ✅ DO: Comprehensive parameter validation
def validate_lock_configuration(max_locks: int, ttl_seconds: int) -> None:
    """Validate lock configuration parameters."""

    if max_locks <= 0:
        raise ValueError(f"max_locks must be positive, got: {max_locks}")

    if max_locks > 1000:  # Reasonable upper bound
        raise ValueError(f"max_locks too high (max 1000), got: {max_locks}")

    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be positive, got: {ttl_seconds}")

    if ttl_seconds > 3600:  # 1 hour max
        raise ValueError(f"ttl_seconds too high (max 3600), got: {ttl_seconds}")

def get_random_lock_slot(max_locks: int) -> int:
    """Get random lock slot with proper validation."""
    validate_lock_configuration(max_locks, 1)  # Quick validation
    return random.randint(0, max_locks - 1)  # Safe after validation

# ❌ REJECT: No validation leading to crashes
def bad_random_slot(max_locks: int) -> int:
    # Crashes if max_locks is 0 or negative
    return random.randint(0, max_locks - 1)  # ValueError!
```

### Phase 2: Interceptor Architecture Patterns

**Lock Acquisition and Release Timing:**

- **Late acquisition**: Acquire locks as close to the protected operation as possible
- **Early release**: Release locks immediately after the protected operation completes
- **Timeout alignment**: Lock TTL should align with activity execution time
- **Race condition prevention**: Ensure locks are held for the entire duration of the protected operation

**Error Handling in Interceptors:**

- **Transient error retry**: Distinguish between retryable errors (connection failures) and permanent errors (invalid configuration)
- **Interceptor isolation**: Interceptor failures should not prevent other interceptors from running
- **Activity result preservation**: Don't modify or lose activity results due to interceptor errors
- **Cleanup on failure**: Ensure resources are cleaned up even when intercepted operations fail

```python
# ✅ DO: Proper lock timing and error handling
class ProperLockInterceptor:
    async def intercept_activity(self, next_fn, input) -> Any:
        """Intercept with proper timing and error handling."""
        lock_acquired = False
        lock_handle = None

        try:
            # Validate configuration first
            if self.max_locks <= 0:
                raise ConfigurationError(f"Invalid max_locks: {self.max_locks}")

            # Bounded retry with exponential backoff
            for attempt in range(self.max_retries):
                try:
                    # Late acquisition - right before protected operation
                    lock_handle = await self.lock_manager.acquire_lock(
                        lock_name=self.lock_name,
                        ttl_seconds=self.ttl_seconds,
                        timeout=self.lock_timeout
                    )
                    lock_acquired = True
                    break

                except LockUnavailableError:
                    if attempt >= self.max_retries - 1:
                        raise LockAcquisitionError(f"Could not acquire lock after {self.max_retries} attempts")

                    # Exponential backoff with jitter
                    delay = min(2 ** attempt + random.uniform(0, 1), self.max_delay)
                    await asyncio.sleep(delay)
                    continue

                except (ConnectionError, TimeoutError) as e:
                    # Transient errors - retry
                    logger.warning(f"Transient lock error on attempt {attempt + 1}: {e}")
                    if attempt >= self.max_retries - 1:
                        raise LockAcquisitionError(f"Lock service unavailable after {self.max_retries} attempts")
                    await asyncio.sleep(1)  # Brief delay for transient errors
                    continue

            # Execute protected operation while holding lock
            result = await next_fn(input)

            # Early release - immediately after operation
            if lock_handle:
                await self.lock_manager.release_lock(lock_handle)
                lock_acquired = False

            return result

        finally:
            # Cleanup: ensure lock is released even on failure
            if lock_acquired and lock_handle:
                try:
                    await self.lock_manager.release_lock(lock_handle)
                except Exception as e:
                    logger.warning(f"Failed to release lock during cleanup: {e}")

# ❌ REJECT: Poor timing and error handling
class BadLockInterceptor:
    async def intercept_activity(self, next_fn, input):
        # No parameter validation
        while True:  # Infinite loop
            lock = await self.acquire_lock()
            if lock:
                result = await next_fn(input)
                # Lock released too early - before result is processed
                await self.release_lock(lock)
                return result
            time.sleep(10)  # Blocking sleep
```

### Phase 3: Interceptor Testing Requirements

**Interceptor Testing Standards:**

- **Test failure scenarios**: Verify behavior when locks can't be acquired, connections fail, etc.
- **Test retry logic**: Ensure bounded retries work correctly with various failure patterns
- **Test resource cleanup**: Verify proper cleanup in both success and failure cases
- **Test timing**: Ensure locks are acquired/released at correct times
- **Test integration**: Verify interceptors work correctly with actual activities/workflows

**Edge Case Testing:**

- **Zero and negative parameters**: Test with edge case values like `max_locks=0`
- **Timeout scenarios**: Test behavior when operations exceed configured timeouts
- **Concurrent access**: Test interceptor behavior under high concurrency
- **Resource exhaustion**: Test behavior when external resources are unavailable

### Phase 4: Performance and Scalability

**Interceptor Performance:**

- **Minimal overhead**: Interceptors should add minimal latency to operation execution
- **Efficient lock management**: Use optimal strategies for lock acquisition and release
- **Connection pooling**: Reuse connections to external services (Dapr, Redis, etc.)
- **Async efficiency**: Never block the event loop in async interceptors

**Scalability Patterns:**

- **Bounded resource usage**: Prevent interceptors from consuming unbounded resources
- **Graceful degradation**: Handle scenarios where external services are unavailable
- **Circuit breaker patterns**: Implement circuit breakers for external service dependencies
- **Monitoring and metrics**: Include appropriate metrics for interceptor performance

### Phase 5: Interceptor Maintainability

**Code Organization:**

- **Single responsibility**: Each interceptor should handle one cross-cutting concern
- **Clear interfaces**: Interceptor interfaces should be well-defined and documented
- **Configuration externalization**: All interceptor behavior should be configurable
- **Error reporting**: Provide clear error messages when interceptors fail

**Integration Safety:**

- **Non-interference**: Interceptors should not interfere with each other
- **Order independence**: Interceptor order should not affect correctness (when possible)
- **Backwards compatibility**: Changes to interceptors should maintain API compatibility
- **Graceful failure**: Interceptor failures should not prevent core functionality

---

## Interceptor-Specific Anti-Patterns

**Always Reject:**

- **Infinite retry loops**: `while True` without bounded conditions
- **Resource leaks in context managers**: Sleeping or blocking inside context managers
- **Parameter validation gaps**: Not validating inputs that are used in range operations
- **Blocking operations in async**: Using synchronous operations that block the event loop
- **Generic error handling**: Not distinguishing between retryable and permanent errors
- **Lock timing issues**: Releasing locks before operations complete
- **Missing cleanup**: Not cleaning up resources in failure scenarios

**Lock Acquisition Anti-Patterns:**

```python
# ❌ REJECT: Multiple critical issues
class CriticallyFlawedInterceptor:
    async def intercept(self, next_fn, input):
        while True:  # 1. Infinite loop
            async with dapr_client.try_lock(lock_name) as response:
                if response.success:
                    return await next_fn(input)
                else:
                    time.sleep(10)  # 2. Blocking sleep in async
                    # 3. Sleep inside context manager - resource leak

        # 4. No parameter validation for max_locks
        slot = random.randint(0, max_locks - 1)  # Crashes if max_locks <= 0

# ✅ REQUIRE: Proper implementation
class WellImplementedInterceptor:
    def __init__(self, max_locks: int = 10, max_retries: int = 5):
        # Validate configuration at initialization
        if max_locks <= 0:
            raise ValueError(f"max_locks must be positive: {max_locks}")
        self.max_locks = max_locks
        self.max_retries = max_retries

    async def intercept(self, next_fn, input) -> Any:
        for attempt in range(self.max_retries):  # Bounded retries
            try:
                # Context manager properly exits before sleep
                async with self.lock_client.try_lock(self.lock_name) as response:
                    if response.success:
                        return await next_fn(input)

                # Sleep outside context manager
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(min(2 ** attempt, 30))  # Non-blocking sleep

            except ConnectionError:
                # Retry transient errors
                if attempt >= self.max_retries - 1:
                    raise
                await asyncio.sleep(1)

        raise LockAcquisitionError("Could not acquire lock within retry limit")
```

## Educational Context for Interceptor Reviews

When reviewing interceptor code, emphasize:

1. **System Stability Impact**: "Interceptors run for every activity/workflow execution. Infinite loops or resource leaks in interceptors can bring down the entire system by exhausting resources."

2. **Performance Impact**: "Interceptor overhead affects every operation. Blocking operations in async interceptors can degrade performance for all concurrent executions."

3. **Reliability Impact**: "Poor error handling in interceptors can mask or cause cascading failures. Proper error distinction and recovery logic are essential for system reliability."

4. **Resource Impact**: "Interceptors often manage external resources (locks, connections). Resource leaks in interceptors compound quickly under load and can cause system-wide failures."

5. **Debugging Impact**: "Interceptor issues are often hard to debug because they affect multiple operations. Clear error messages and proper logging are critical for troubleshooting."

6. **Scalability Impact**: "Interceptor patterns that work under light load can fail catastrophically under heavy load. Always design for high-concurrency scenarios with proper resource bounds."
