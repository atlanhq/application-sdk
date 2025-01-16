# Best Practices


## Scaling in Python
While python does not natively support multi-threading due to the Global Interpreter Lock (GIL), it is still possible to use multi-threading for I/O bound tasks. This is because the GIL is released when a thread is waiting for I/O operations to complete.
We are able to leverage this in our application development along with Temporal to scale our workflows.

## Async functions
- Python 3.5 introduced the `async` and `await` keywords to allow for asynchronous programming
- This allows us to write non-blocking code that can run concurrently

### Temporal parallelism
- Temporal works in a worker-pool model where each worker is a separate process
- Each worker registers itself to run certain workflows and activities
- Application is bundled with a worker that runs the workflows and activities
- In production, this will mean we will have at-least 3 workers (HA deployment) running at all times
- Each worker can run multiple workflows and activities in parallel and listens on a queue to fetch the activities to execute

This allows us to achieve concurrency at both workflow and activity level based on availability of workers.

### Python MultiProcessing
- If the temporal activity parallelism is still not enough, we can use Python's multiprocessing library to spawn multiple processes to run the activities in parallel
- This will allow us to scale the activities horizontally across multiple cores.


## Memoization
- It is recommended that application build memoization into the application to cache the results of expensive function calls.
  - You can use the state store system to save intermediate results at regular intervals and use them to avoid re-computation in lon running activities
- This will help when the pod is deleted and the activity is re-run, the application can fetch the intermediate results from the state store and continue from where it left off.

## Reliability

### Activity Heartbeats
- Temporal uses heartbeats to monitor the health of activities. If an activity fails to heartbeat, Temporal will retry the activity on a different worker. It is recommended to use heartbeats to report the progress of the activity. This will help in debugging long running activities.
- We provide a `heartbeat_timeout` parameter in the `execute_activity` call. This is the duration for which the activity is allowed to heartbeat. If the activity does not heartbeat within this time, it is marked as failed.
- You can use the `auto_heartbeater` decorator on top of the activity definition ([example](https://github.com/atlanhq/application-sdk/blob/main/application_sdk/workflows/sql/workflow.py#L238)) to automatically heartbeat the activity. This will heartbeat the activity 3 times per heartbeat_timeout interval by default.

> Read more about activity timeouts [here](https://temporal.io/blog/activity-timeouts)


## Temporal
### Activities and timeouts
Great [read](https://temporal.io/blog/activity-timeouts)

**TLDR;**
- Always set `StartToCloseTimeout` - Maximum time the activity can run
- Set `HeartbeatTimeout` - Maximum time between heartbeats. Use this to report progress and keep the activity alive
  - Great for long-running activities
  - Send periodic heartbeat from code and if the activity fails to heartbeat based on the timeout, it will be retried