import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from dapr.clients import DaprClient
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@dataclass
class SlotInfo:
    """Information about an acquired slot."""

    slot_number: int
    lock_id: str
    owner_id: str


class LockManager:
    """
    Manages distributed locks and tenant activity concurrency using Dapr's lock building block.
    """

    def __init__(
        self,
        tenant_id: str,
        max_slots: int = 5,
        lock_ttl_seconds: int = 50,
        component_name: str = "lockstore",
    ):
        self.tenant_id = tenant_id
        self.dapr_client = DaprClient()
        self.max_slots = max_slots
        self.lock_ttl = lock_ttl_seconds
        self.component_name = component_name

        self.logger = get_logger(__name__)

    def __del__(self):
        """Cleanup DaprClient when LockManager is destroyed."""
        if hasattr(self, "dapr_client"):
            self.dapr_client.close()

    def acquire_slots(
        self, count: int, workflow_id: str, activity_id: str
    ) -> List[Dict]:
        """Acquire specified number of slots."""
        acquired_slots = []
        owner_id = self._generate_owner_id(workflow_id, activity_id)

        for slot in range(self.max_slots):
            if len(acquired_slots) >= count:
                break

            lock_id = self._generate_lock_id(slot)
            try:
                response = self.dapr_client.try_lock(
                    store_name=self.component_name,
                    resource_id=lock_id,
                    lock_owner=owner_id,
                    expiry_in_seconds=self.lock_ttl,
                )

                if response.success:
                    acquired_slots.append(
                        SlotInfo(
                            slot_number=slot,
                            lock_id=lock_id,
                            owner_id=owner_id,
                        ).__dict__
                    )
                    self.logger.info(f"Slot acquired {slot} {lock_id} {owner_id}")
            except Exception as e:
                self.logger.error(
                    f"Error acquiring slot {slot} {lock_id} {owner_id} {str(e)}",
                    exc_info=True,
                )
                raise

        return acquired_slots

    def release_slot(self, slot_info: Dict) -> bool:
        """Release a specific slot."""
        try:
            response = self.dapr_client.unlock(
                store_name=self.component_name,
                resource_id=slot_info["lock_id"],
                lock_owner=slot_info["owner_id"],
            )

            if response.status.name == "success":
                self.logger.info(f"Slot released {slot_info['lock_id']}")
                return True

            self.logger.warning(f"Failed to release slot {slot_info['lock_id']}")
            return False

        except Exception as e:
            self.logger.error(
                f"Error releasing slot {slot_info['lock_id']} {str(e)}",
                exc_info=True,
            )
            raise

    def _generate_lock_id(self, slot_number: int) -> str:
        """Generate lock resource ID as per TRD format."""
        return f"tenant:{self.tenant_id}:slot:{slot_number}"

    def _generate_owner_id(self, workflow_id: str, activity_id: str) -> str:
        """Generate owner ID as per TRD format."""
        return f"workflow:{workflow_id}:activity:{activity_id}"
