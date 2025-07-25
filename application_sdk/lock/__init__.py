import asyncio
import random
from functools import wraps

from dapr.clients import DaprClient
from temporalio import workflow

from application_sdk.constants import APP_TENANT_ID, APPLICATION_NAME, LOCK_STORE_NAME
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def try_locks(func=None, *, max_slots: int = 5, ttl_seconds: int = 50):
    """Manages distributed locks for parallel task execution using Dapr's lock building block.

    Can be used either as a decorator or a direct function:
    1. As decorator without args: @try_locks
    2. As decorator with args: @try_locks(max_slots=3)
    3. Direct with tasks: try_locks(tasks)
    4. Direct with args: try_locks(max_slots=3)(tasks)

    Args:
        func: Optional[Callable | List], either the function to decorate or list of tasks to execute
        max_slots: int, maximum number of concurrent tasks allowed (default: 5)
        ttl_seconds: int, time-to-live for locks in seconds (default: 50)

    Returns:
        If used as decorator: Decorated async function that manages locks
        If used directly: Coroutine that executes tasks with lock management

    Example:
        ```python
        # Method 1: As decorator with default args
        @try_locks
        async def execute_parallel(tasks):
            return await asyncio.gather(*tasks)

        # Method 2: As decorator with custom args
        @try_locks(max_slots=3, ttl_seconds=30)
        async def execute_custom(tasks):
            return await asyncio.gather(*tasks)

        # Method 3: Direct usage with default args
        await try_locks(tasks)

        # Method 4: Direct usage with custom args
        await try_locks(max_slots=3)(tasks)
        ```

    Note:
        - Uses Dapr's lock building block with Redis backend
        - Prevents over-provisioning by acquiring lock before task execution
        - Implements random slot selection and delay for contention management
        - Automatically releases locks after task completion
    """

    async def run_with_lock(task):
        """Executes a single task with lock management.

        Continuously attempts to acquire a random lock slot before executing the task.
        Implements random delays between retry attempts to prevent thundering herd.

        Args:
            task: Coroutine to execute once lock is acquired

        Returns:
            Any: Result of the task execution
        """
        while True:
            slot = random.randint(0, max_slots - 1)
            lock_id = f"tenant:{APP_TENANT_ID}:app:{APPLICATION_NAME}:slot:{slot}"
            owner_id = f"workflow:{workflow.info().workflow_id}"

            with DaprClient() as client:
                with client.try_lock(
                    store_name=LOCK_STORE_NAME,
                    resource_id=lock_id,
                    lock_owner=owner_id,
                    expiry_in_seconds=ttl_seconds,
                ) as lock:
                    if lock.success:
                        try:
                            result = await task
                            return result
                        finally:
                            logger.info(f"Task completed with lock {lock_id}")

            delay = random.uniform(0, 0.5)
            await asyncio.sleep(delay)

    async def execute_with_locks(tasks):
        """Executes multiple tasks in parallel with lock management.

        Args:
            tasks: List of tasks to execute

        Returns:
            List[Any]: Results of all task executions
        """
        return await asyncio.gather(*[run_with_lock(task) for task in tasks])

    if func is None:
        # Called with arguments: @try_locks(max_slots=3) or try_locks(max_slots=3)(tasks)
        return (
            lambda f: try_locks(f, max_slots=max_slots, ttl_seconds=ttl_seconds)
            if callable(f)
            else execute_with_locks(f)
        )
    else:
        # Called without arguments: @try_locks or try_locks(tasks)
        return (
            execute_with_locks(func)
            if isinstance(func, list)
            else wraps(func)(lambda *args, **kwargs: execute_with_locks(args[0]))
        )
