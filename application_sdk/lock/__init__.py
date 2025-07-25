from dataclasses import dataclass
from typing import Dict, List
from application_sdk.constants import LOCK_STORE_NAME
from dapr.clients import DaprClient
from application_sdk.observability.logger_adaptor import get_logger
import random

logger = get_logger(__name__)

@dataclass
class LockInfo:
    """Information about an acquired lock."""
    lock_number: int
    lock_id: str
    owner_id: str

class LockManager:
    """
    Manages distributed locks and tenant activity concurrency using Dapr's lock building block.
    """
    def __init__(
        self,
        tenant_id: str,
        max_locks: int = 5,
        lock_ttl_seconds: int = 50,
    ):
        self.tenant_id = tenant_id
        self.dapr_client = DaprClient()
        self.max_locks = max_locks
        self.lock_ttl = lock_ttl_seconds
        self.component_name = LOCK_STORE_NAME
        self.logger = get_logger(__name__)

    def __del__(self):
        """Cleanup DaprClient when LockManager is destroyed."""
        if hasattr(self, 'dapr_client'):
            self.dapr_client.close()

    def acquire_locks(self, count: int, workflow_id: str, activity_id: str) -> List[Dict]:
        """Acquire specified number of locks using random lock selection."""
        acquired_locks = []
        owner_id = self._generate_owner_id(workflow_id, activity_id)
        
        # Create randomized lock order
        lock_order = list(range(self.max_locks))
        random.shuffle(lock_order)
        
        # Try all locks in random order
        for lock in lock_order:
            if len(acquired_locks) >= count:
                break

            lock_id = self._generate_lock_id(lock)
            try:
                response = self.dapr_client.try_lock(
                    store_name=self.component_name,
                    resource_id=lock_id,
                    lock_owner=owner_id,
                    expiry_in_seconds=self.lock_ttl,
                )

                if response.success:
                    acquired_locks.append(
                        LockInfo(
                            lock_number=lock,
                            lock_id=lock_id,
                            owner_id=owner_id,
                        ).__dict__
                    )
                    self.logger.info(f"lock acquired {lock} {lock_id} {owner_id}")
            except Exception as e:
                self.logger.error(
                    f"Error acquiring lock {lock} {lock_id} {owner_id} {str(e)}",
                    exc_info=True,
                )
                raise

        return acquired_locks

    def release_lock(self, lock_info: Dict) -> bool:
        """Release a specific lock."""
        try:
            response = self.dapr_client.unlock(
                store_name=self.component_name,
                resource_id=lock_info["lock_id"],
                lock_owner=lock_info["owner_id"],
            )

            if response.status.name == "success":
                self.logger.info(f"lock released {lock_info['lock_id']}")
                return True

            self.logger.warning(f"Failed to release lock {lock_info['lock_id']}")
            return False

        except Exception as e:
            self.logger.error(
                f"Error releasing lock {lock_info['lock_id']} {str(e)}",
                exc_info=True,
            )
            raise

    def _generate_lock_id(self, lock_number: int) -> str:
        """Generate lock resource ID as per TRD format."""
        return f"tenant:{self.tenant_id}:lock:{lock_number}"


    def _generate_owner_id(self, workflow_id: str, activity_id: str) -> str:
        """Generate owner ID as per TRD format."""
        return f"workflow:{workflow_id}:activity:{activity_id}"
