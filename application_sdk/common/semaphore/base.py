"""Base interface for semaphore implementations."""

from abc import ABC, abstractmethod


class Semaphore(ABC):
    """Abstract base class for semaphore implementations."""

    @staticmethod
    @abstractmethod
    def acquire_lock(
        resource_id: str,
        lock_owner: str,
        permits: int = 1,
    ) -> bool:
        """Acquire permits from a semaphore.

        Args:
            resource_id: The unique identifier of the resource to lock.
            lock_owner: A unique identifier for the lock owner.
            permits: Number of permits to acquire. Defaults to 1.

        Returns:
            bool: True if the permits were acquired successfully, False otherwise.

        Raises:
            Exception: If there's an error during permit acquisition.
        """
        pass

    @staticmethod
    @abstractmethod
    def release_lock(
        resource_id: str, 
        lock_owner: str,
        permits: int = 1,
    ) -> bool:
        """Release previously acquired permits.

        Args:
            resource_id: The unique identifier of the locked resource.
            lock_owner: The unique identifier of the lock owner.
            permits: Number of permits to release. Defaults to 1.

        Returns:
            bool: True if the permits were released successfully, False otherwise.

        Raises:
            Exception: If there's an error during permit release.
        """
        pass

    @staticmethod
    @abstractmethod
    def get_permits(resource_id: str) -> int:
        """Get the number of available permits for a resource.

        Args:
            resource_id: The unique identifier of the resource.

        Returns:
            int: Number of available permits.
        """
        pass 