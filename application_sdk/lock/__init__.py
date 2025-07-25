import random
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from dapr.clients import DaprClient

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class LockingMode(Enum):
    """Defines the locking strategy to be used by LockManager.

    Attributes:
        SEMAPHORE: Use a fixed pool of numbered locks to limit concurrent operations.
        RESOURCE: Lock specific named resources for exclusive access.
    """

    SEMAPHORE = "semaphore"
    RESOURCE = "resource"


@dataclass
class LockInfo:
    """Information about an acquired lock.

    Attributes:
        lock_id: Unique identifier for the lock in Dapr.
        owner_id: Identifier of the workflow/activity holding the lock.
        lock_number: For SEMAPHORE mode, the lock number acquired (0 to max_locks-1).
        resource_id: For RESOURCE mode, the resource identifier being locked.
    """

    lock_id: str
    owner_id: str
    lock_number: Optional[int] = None
    resource_id: Optional[str] = None


class LockManager:
    """Manages distributed locks using Dapr's lock building block.

    This class provides two locking strategies:
    1. SEMAPHORE: For limiting concurrent operations using a fixed pool of locks
    2. RESOURCE: For exclusive access to named resources

    Attributes:
        mode: The locking strategy to use (SEMAPHORE or RESOURCE).
        tenant_id: Identifier for the tenant using the locks.
        lock_ttl: Time-to-live for locks in seconds.
        component_name: Name of the Dapr lock store component.
        max_locks: (SEMAPHORE mode) Maximum number of concurrent locks.
        resource_prefix: (RESOURCE mode) Prefix for resource lock IDs.

    Example:
        ```python
        # Semaphore mode for concurrent operation limiting
        lock_manager = LockManager(
            tenant_id="tenant1",
            mode=LockingMode.SEMAPHORE,
            max_locks=5
        )
        # Get single lock
        lock = lock_manager.acquire_lock(workflow_id="wf1", activity_id="act1")

        # Resource mode for exclusive access
        lock_manager = LockManager(
            tenant_id="tenant1",
            mode=LockingMode.RESOURCE,
            resource_prefix="database"
        )
        lock = lock_manager.acquire_lock(
            resource_id="user:123",
            workflow_id="wf1",
            activity_id="act1"
        )
        ```
    """

    def __init__(
        self,
        tenant_id: str,
        mode: LockingMode,
        max_locks: Optional[int] = None,
        resource_prefix: Optional[str] = None,
        lock_ttl_seconds: int = 50,
        component_name: str = "lockstore",
    ) -> None:
        """Initialize the LockManager.

        Args:
            tenant_id: Identifier for the tenant using the locks.
            mode: The locking strategy to use (SEMAPHORE or RESOURCE).
            max_locks: Required for SEMAPHORE mode. Number of available locks.
            resource_prefix: Required for RESOURCE mode. Prefix for resource lock IDs.
            lock_ttl_seconds: Time-to-live for locks in seconds. Defaults to 50.
            component_name: Name of the Dapr lock store component. Defaults to "lockstore".

        Raises:
            ValueError: If required mode-specific parameters are missing.
        """
        self.mode = mode
        self.tenant_id = tenant_id
        self.lock_ttl = lock_ttl_seconds
        self.component_name = component_name

        if mode == LockingMode.SEMAPHORE:
            if not max_locks:
                raise ValueError("max_locks required for SEMAPHORE mode")
            self.max_locks = max_locks
        elif mode == LockingMode.RESOURCE:
            if not resource_prefix:
                raise ValueError("resource_prefix required for RESOURCE mode")
            self.resource_prefix = resource_prefix

    def acquire_lock(
        self,
        workflow_id: str,
        activity_id: str,
        resource_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Acquire a single lock based on the configured mode.

        Args:
            workflow_id: Identifier of the requesting workflow.
            activity_id: Identifier of the requesting activity.
            resource_id: (RESOURCE mode) Identifier of the resource to lock.

        Returns:
            Optional[Dict]: Lock information if acquired, None if not acquired.

        Raises:
            ValueError: If resource_id is missing in RESOURCE mode.
            Exception: If lock acquisition fails.

        Example:
            ```python
            # Semaphore mode
            lock = lock_manager.acquire_lock(
                workflow_id="workflow1",
                activity_id="activity1"
            )

            # Resource mode
            lock = lock_manager.acquire_lock(
                resource_id="user:123",
                workflow_id="workflow1",
                activity_id="activity1"
            )
            ```
        """
        if self.mode == LockingMode.SEMAPHORE:
            locks = self._acquire_semaphore(workflow_id, activity_id)
            return locks
        else:
            if resource_id is None:
                raise ValueError("resource_id required for RESOURCE mode")
            locks = self._acquire_resource(resource_id, workflow_id, activity_id)
            return locks

    def _acquire_semaphore(
        self, workflow_id: str, activity_id: str
    ) -> Optional[Dict[str, Any]]:
        """Internal method to acquire a single lock in SEMAPHORE mode.

        Uses random lock selection for fair distribution.

        Args:
            workflow_id: Identifier of the requesting workflow.
            activity_id: Identifier of the requesting activity.

        Returns:
            Optional[Dict]: Acquired lock information or None.

        Raises:
            Exception: If lock acquisition fails.
        """
        owner_id = self._generate_owner_id(workflow_id, activity_id)

        # Create randomized lock order for fair distribution
        lock_order = list(range(self.max_locks))
        random.shuffle(lock_order)

        # Try locks in random order
        for lock in lock_order:
            lock_id = self._generate_semaphore_lock_id(lock)
            try:
                with DaprClient() as client:
                    response = client.try_lock(
                        store_name=self.component_name,
                        resource_id=lock_id,
                        lock_owner=owner_id,
                        expiry_in_seconds=self.lock_ttl,
                    )

                if response.success:
                    lock_info = LockInfo(
                        lock_number=lock,
                        lock_id=lock_id,
                        owner_id=owner_id,
                    ).__dict__
                    logger.info(f"lock acquired {lock} {lock_id} {owner_id}")
                    return lock_info

            except Exception as e:
                logger.error(
                    f"Error acquiring lock {lock} {lock_id} {owner_id} {str(e)}",
                    exc_info=True,
                )
                raise

        return None

    def _acquire_resource(
        self, resource_id: str, workflow_id: str, activity_id: str
    ) -> Optional[Dict[str, Any]]:
        """Internal method to acquire a resource lock.

        Args:
            resource_id: Identifier of the resource to lock.
            workflow_id: Identifier of the requesting workflow.
            activity_id: Identifier of the requesting activity.

        Returns:
            List[Dict]: Single-item list containing resource lock information.

        Raises:
            Exception: If resource lock acquisition fails.
        """
        owner_id = self._generate_owner_id(workflow_id, activity_id)
        lock_id = self._generate_resource_lock_id(resource_id)

        try:
            with DaprClient() as client:
                response = client.try_lock(
                    store_name=self.component_name,
                    resource_id=lock_id,
                    lock_owner=owner_id,
                    expiry_in_seconds=self.lock_ttl,
                )

            if response.success:
                lock_info = LockInfo(
                    lock_id=lock_id, owner_id=owner_id, resource_id=resource_id
                ).__dict__
                logger.info(f"Resource locked {resource_id} {lock_id} {owner_id}")
                return lock_info

            logger.info(f"Failed to acquire resource lock {resource_id} {lock_id}")
            return None

        except Exception as e:
            logger.error(
                f"Error acquiring resource lock {resource_id} {lock_id} {str(e)}",
                exc_info=True,
            )
            raise

    def release_lock(self, lock_info: Dict[str, Any]) -> bool:
        """Release an acquired lock.

        Works for both SEMAPHORE and RESOURCE modes.

        Args:
            lock_info: Dictionary containing lock information returned from acquire_lock.
                Must contain 'lock_id' and 'owner_id' keys.

        Returns:
            bool: True if lock was successfully released, False otherwise.

        Raises:
            Exception: If lock release operation fails.

        Example:
            ```python
            lock = lock_manager.acquire_lock(...)
            if lock:
                try:
                    # Do work
                finally:
                    success = lock_manager.release_lock(lock)
                    if not success:
                        logger.error(f"Failed to release lock {lock['lock_id']}")
            ```
        """
        try:
            with DaprClient() as client:
                response = client.unlock(
                    store_name=self.component_name,
                    resource_id=lock_info["lock_id"],
                    lock_owner=lock_info["owner_id"],
                )

            if response.status.name == "success":
                logger.info(f"Lock released {lock_info['lock_id']}")
                return True

            logger.warning(f"Failed to release lock {lock_info['lock_id']}")
            return False

        except Exception as e:
            logger.error(
                f"Error releasing lock {lock_info['lock_id']} {str(e)}",
                exc_info=True,
            )
            raise

    def _generate_semaphore_lock_id(self, lock_number: int) -> str:
        """Generate lock ID for semaphore mode.

        Format: tenant:{tenant_id}:lock:{lock_number}
        """
        return f"tenant:{self.tenant_id}:lock:{lock_number}"

    def _generate_resource_lock_id(self, resource_id: str) -> str:
        """Generate lock ID for resource mode.

        Format: tenant:{tenant_id}:{resource_prefix}:{resource_id}
        """
        return f"tenant:{self.tenant_id}:{self.resource_prefix}:{resource_id}"

    def _generate_owner_id(self, workflow_id: str, activity_id: str) -> str:
        """Generate owner ID for lock ownership.

        Format: workflow:{workflow_id}:activity:{activity_id}
        """
        return f"workflow:{workflow_id}:activity:{activity_id}"
