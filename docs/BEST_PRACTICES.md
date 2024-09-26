# Best Practices

## Table of Contents
- [Scaling in Python](#scaling-in-python)
  - [Async functions](#async-functions)
  - [Temporal parallelism](#temporal-parallelism)
  - [Python MultiProcessing](#python-multiprocessing)
- [Memoization](#memoization)
  
    
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