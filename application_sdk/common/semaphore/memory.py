"""In-memory semaphore implementation."""

import threading
from dataclasses import dataclass
from typing import Dict

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.semaphore.base import Semaphore

logger = get_logger(__name__)


@dataclass
class SemaphoreInfo:
    """Information about a semaphore."""
    max_permits: int
    permits_in_use: Dict[str, int]  # owner -> permits held


class InMemorySemaphore(Semaphore):
    """In-memory semaphore implementation for local development.

    This implementation uses a dictionary to store semaphores in memory, with thread-safe
    operations. It's suitable for local development and testing, but should not be
    used in production where distributed coordination is required.

    Example:
        ```python
        # Basic usage - binary semaphore
        if InMemorySemaphore.acquire("my_resource", "my_owner"):
            try:
                # Critical section
                pass
            finally:
                InMemorySemaphore.release("my_resource", "my_owner")

        # Using as counting semaphore
        if InMemorySemaphore.acquire("my_resource", "my_owner", permits=3):
            try:
                # Use the resource with 3 permits
                pass
            finally:
                InMemorySemaphore.release("my_resource", "my_owner", permits=3)
        ```
    """

    _semaphores: Dict[str, SemaphoreInfo] = {}
    _lock = threading.Lock()

    @staticmethod
    def acquire(
        resource_id: str,
        lock_owner: str,
        permits: int = 1,
        max_permits: int = 1,
    ) -> bool:
        """Acquire permits from a semaphore.

        Args:
            resource_id: The unique identifier of the resource.
            lock_owner: A unique identifier for the lock owner.
            permits: Number of permits to acquire. Defaults to 1.
            max_permits: Maximum number of permits for this resource. Only used when
                       creating a new semaphore. Defaults to 1 (binary semaphore).

        Returns:
            bool: True if the permits were acquired successfully, False otherwise.
        """
        if permits < 1:
            logger.error("Cannot acquire less than 1 permit")
            return False

        try:
            with InMemorySemaphore._lock:
                # Get or create semaphore
                if resource_id not in InMemorySemaphore._semaphores:
                    InMemorySemaphore._semaphores[resource_id] = SemaphoreInfo(
                        max_permits=max_permits,
                        permits_in_use={},
                    )

                semaphore = InMemorySemaphore._semaphores[resource_id]

                # Calculate total permits in use
                total_in_use = sum(semaphore.permits_in_use.values())
                owner_current = semaphore.permits_in_use.get(lock_owner, 0)

                # Check if we can acquire the permits
                if total_in_use - owner_current + permits > semaphore.max_permits:
                    logger.debug(
                        f"Cannot acquire {permits} permits for resource {resource_id}. "
                        f"Total in use: {total_in_use}, Max: {semaphore.max_permits}"
                    )
                    return False

                # Update permits
                semaphore.permits_in_use[lock_owner] = owner_current + permits

                logger.debug(
                    f"Acquired {permits} permits for resource {resource_id}. "
                    f"Owner: {lock_owner}"
                )
                return True

        except Exception as e:
            logger.error(f"Failed to acquire permits: {str(e)}")
            raise e

    @staticmethod
    def release(
        resource_id: str,
        lock_owner: str,
        permits: int = 1,
    ) -> bool:
        """Release previously acquired permits.

        Args:
            resource_id: The unique identifier of the resource.
            lock_owner: The unique identifier of the lock owner.
            permits: Number of permits to release. Defaults to 1.

        Returns:
            bool: True if the permits were released successfully, False otherwise.
        """
        if permits < 1:
            logger.error("Cannot release less than 1 permit")
            return False

        try:
            with InMemorySemaphore._lock:
                if resource_id not in InMemorySemaphore._semaphores:
                    logger.debug(f"Resource {resource_id} not found")
                    return False

                semaphore = InMemorySemaphore._semaphores[resource_id]
                if lock_owner not in semaphore.permits_in_use:
                    logger.debug(f"Owner {lock_owner} has no permits for {resource_id}")
                    return False

                current_permits = semaphore.permits_in_use[lock_owner]
                if current_permits < permits:
                    logger.debug(
                        f"Owner {lock_owner} cannot release {permits} permits, "
                        f"only has {current_permits}"
                    )
                    return False

                # Update or remove the owner's permits
                if current_permits == permits:
                    del semaphore.permits_in_use[lock_owner]
                else:
                    semaphore.permits_in_use[lock_owner] = current_permits - permits

                logger.debug(
                    f"Released {permits} permits for resource {resource_id}. "
                    f"Owner: {lock_owner}"
                )
                return True

        except Exception as e:
            logger.error(f"Failed to release permits: {str(e)}")
            raise e

    @staticmethod
    def get_permits(resource_id: str) -> int:
        """Get the number of available permits for a resource.

        Args:
            resource_id: The unique identifier of the resource.

        Returns:
            int: Number of available permits. Returns 0 if the resource doesn't exist.
        """
        try:
            with InMemorySemaphore._lock:
                if resource_id not in InMemorySemaphore._semaphores:
                    return 0

                semaphore = InMemorySemaphore._semaphores[resource_id]
                total_in_use = sum(semaphore.permits_in_use.values())
                return semaphore.max_permits - total_in_use

        except Exception as e:
            logger.error(f"Failed to get available permits: {str(e)}")
            raise e 