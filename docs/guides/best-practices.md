# Best Practices

## Scaling in Python

Python's Global Interpreter Lock (GIL) limits native multi-threading, but we can still leverage multi-threading for I/O-bound tasks since the GIL is released during I/O operations. This capability, combined with Temporal, enables scalable workflow execution.

## Async Functions

- Python 3.5+ supports asynchronous programming through `async` and `await` keywords
- Enables non-blocking, concurrent code execution

### Temporal Parallelism

- Temporal operates on a worker-pool model with separate processes
- Workers register to handle specific workflows and activities
- Applications bundle with workers that execute workflows and activities
- Production deployments require at least 3 workers for high availability
- Workers can run multiple workflows and activities concurrently while listening on activity queues

This architecture enables concurrency at both workflow and activity levels based on worker availability.

### Python Multiprocessing

- For additional scaling beyond Temporal activity parallelism, use Python's multiprocessing library
- Enables horizontal scaling across multiple CPU cores

## Memoization

- Implement memoization to cache expensive function results
- Use the state store system to save intermediate results at regular intervals
- Enables activity recovery from state store if pods are deleted and activities are re-run

## Reliability

### Task Heartbeats

- Temporal monitors task health through heartbeats
- Tasks that fail to heartbeat are retried on different workers
- Use heartbeats to report task progress and aid debugging
- The `@task(auto_heartbeat_seconds=10)` parameter automatically sends heartbeats at the specified interval
- `@task` methods are called directly from `run()` --- no separate `execute_activity` call is needed

> Read more about activity timeouts [here](https://temporal.io/blog/activity-timeouts)

## Temporal

### Tasks and Timeouts

Great [read](https://temporal.io/blog/activity-timeouts) on activity timeouts and heartbeats.

**TLDR;**

- Set `@task(timeout_seconds=...)` - Maximum task runtime
- Configure `@task(auto_heartbeat_seconds=...)` - Automatic heartbeat interval
    - Essential for long-running tasks
    - Tasks are retried if they fail to heartbeat within the timeout period