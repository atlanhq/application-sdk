# Workflow Code Review Guidelines - Temporal Workflows

## Context-Specific Patterns

This directory contains Temporal workflow definitions and orchestration logic. Workflows must be deterministic, resilient, and handle long-running processes gracefully.

### Phase 1: Critical Workflow Safety Issues

**Determinism Requirements (CRITICAL):**

- Workflows MUST be deterministic - same inputs always produce same sequence of decisions
- No random number generation, system time calls, or external API calls in workflow code
- No file I/O, database queries, or network calls directly in workflows
- All non-deterministic operations must be delegated to activities
- Thread-safe operations only - no global state mutation

**Example Determinism Violations:**

```python
# ❌ NEVER: Non-deterministic operations in workflows
async def process_data_workflow(data: dict):
    timestamp = datetime.now()  # Non-deterministic!
    random_id = uuid.uuid4()   # Non-deterministic!
    user_data = await fetch_user_api(data['user_id'])  # External call!

# ✅ DO: Delegate to activities
async def process_data_workflow(data: dict):
    # All external operations via activities
    timestamp = await workflow.execute_activity(
        get_current_timestamp,
        schedule_to_close_timeout=timedelta(seconds=10)
    )
    user_data = await workflow.execute_activity(
        fetch_user_activity,
        data['user_id'],
        schedule_to_close_timeout=timedelta(minutes=5)
    )
```

**Workflow State Management:**

- All workflow data must be serializable (JSON-compatible)
- No complex objects that can't be reliably serialized/deserialized
- Workflow parameters and return values must use Pydantic models
- State changes must be explicit and traceable
- No mutable shared state between workflow executions

### Phase 2: Workflow Architecture Patterns

**Activity Execution Patterns:**

- All activities must have explicit timeouts (schedule_to_close_timeout)
- Retry policies must be defined for activities that can fail
- Use appropriate heartbeat timeouts for long-running activities
- Error handling must distinguish between retryable and non-retryable errors

```python
# ✅ DO: Proper activity execution with timeouts and retries
await workflow.execute_activity(
    extract_metadata_activity,
    connection_config,
    schedule_to_close_timeout=timedelta(minutes=30),
    retry_policy=RetryPolicy(
        initial_interval=timedelta(seconds=1),
        maximum_interval=timedelta(minutes=1),
        maximum_attempts=3,
        non_retryable_error_types=["ValidationError"]
    )
)

# ❌ NEVER: Activity without proper configuration
await workflow.execute_activity(process_data)  # No timeout, no retry policy
```

**Workflow Composition:**

- Break complex workflows into smaller, reusable sub-workflows
- Use child workflows for independent business processes
- Implement proper cancellation handling
- Use workflow signals for external event handling
- Design workflows to be resumable after interruption

### Phase 3: Workflow Testing Requirements

**Workflow Testing Standards:**

- Use Temporal's test framework for workflow unit tests
- Mock activities in workflow tests
- Test workflow failure and retry scenarios
- Test workflow cancellation and timeout behaviors
- Verify workflow determinism with replay tests
- Include integration tests with real activities

**Activity Testing:**

- Test activities independently from workflows
- Mock external dependencies in activity tests
- Test activity timeout and retry behaviors
- Verify proper error handling and logging
- Include performance tests for long-running activities

### Phase 4: Performance and Scalability

**Workflow Performance:**

- Minimize workflow execution time - delegate work to activities
- Avoid deeply nested workflow structures
- Use parallel activity execution where possible
- Implement proper batching for bulk operations
- Monitor workflow execution metrics and duration

**Resource Management:**

- Activities must clean up resources (connections, files, etc.)
- Implement proper timeout handling to prevent resource leaks
- Use connection pooling in activities, not workflows
- Monitor memory usage in long-running workflows
- Implement proper backpressure handling

### Phase 5: Workflow Maintainability

**Workflow Versioning:**

- All workflow changes must be backward compatible
- Use workflow versioning for breaking changes
- Document workflow version migration paths
- Test workflow compatibility across versions
- Implement graceful handling of version mismatches

**Error Handling and Observability:**

- All workflow failures must be logged with context
- Include workflow execution ID in all log messages
- Implement proper metrics for workflow success/failure rates
- Use structured logging for workflow events
- Include tracing information for debugging

---

## Workflow-Specific Anti-Patterns

**Always Reject:**

- Non-deterministic operations in workflow code
- Direct database or API calls from workflows
- Missing timeouts on activity executions
- Workflows without proper error handling
- Global state mutation in workflows
- Synchronous operations in async workflows
- Activities without retry policies
- Workflows without proper logging

**Determinism Anti-Patterns:**

```python
# ❌ REJECT: Non-deterministic workflow code
@workflow.defn
class BadWorkflow:
    async def run(self, data: dict):
        # All of these break determinism
        current_time = datetime.now()
        random_value = random.random()
        file_content = open('config.txt').read()
        api_response = requests.get('http://api.example.com')

# ✅ REQUIRE: Deterministic workflow with activities
@workflow.defn
class GoodWorkflow:
    async def run(self, data: dict):
        # Delegate all non-deterministic operations to activities
        current_time = await workflow.execute_activity(
            get_current_timestamp_activity,
            schedule_to_close_timeout=timedelta(seconds=5)
        )
        config = await workflow.execute_activity(
            read_config_activity,
            schedule_to_close_timeout=timedelta(seconds=10)
        )
```

**Activity Execution Anti-Patterns:**

```python
# ❌ REJECT: Poor activity configuration
async def bad_workflow():
    # No timeout, no retry policy, no error handling
    result = await workflow.execute_activity(critical_operation)

# ✅ REQUIRE: Proper activity execution
async def good_workflow():
    try:
        result = await workflow.execute_activity(
            critical_operation,
            schedule_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                non_retryable_error_types=["ValidationError"]
            )
        )
    except ActivityError as e:
        # Proper error handling with context
        workflow.logger.error(f"Activity failed: {e}", extra={"workflow_id": workflow.info().workflow_id})
        raise
```

## Educational Context for Workflow Reviews

When reviewing workflow code, emphasize:

1. **Determinism Impact**: "Workflow determinism isn't just a best practice - it's required for Temporal's replay mechanism to work correctly. Non-deterministic workflows can cause infinite replay loops and corrupt execution history."

2. **Resilience Impact**: "Proper timeout and retry configuration determines whether temporary failures cause workflow abandonment or graceful recovery. At enterprise scale, workflows must handle infrastructure failures transparently."

3. **Performance Impact**: "Workflows should orchestrate, not execute. Heavy computation in workflows blocks the task queue and reduces overall system throughput. Always delegate work to activities."

4. **Maintainability Impact**: "Workflow versioning and backward compatibility are essential for production systems. Breaking workflow compatibility can leave running executions in an unrecoverable state."

5. **Observability Impact**: "Workflow logging and metrics are critical for debugging long-running processes. Proper observability makes the difference between quick problem resolution and hours of investigation."
