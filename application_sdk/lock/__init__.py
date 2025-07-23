import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from dapr.clients import DaprClient

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import get_metrics

logger = get_logger(__name__)
metrics = get_metrics()


@dataclass
class SlotInfo:
    """Information about an acquired slot."""

    slot_number: int
    lock_id: str
    owner_id: str
    acquired_at: datetime


class LockManager:
    """
    Manages distributed locks and tenant activity concurrency using Dapr's lock building block.
    Implements the TRD specifications for slot-based activity management.
    """

    def __init__(
        self,
        tenant_id: str,
        max_slots: int = 5,
        lock_ttl_seconds: int = 30,
        component_name: str = "lockstore",
    ):
        self.tenant_id = tenant_id
        self.max_slots = max_slots
        self.lock_ttl = lock_ttl_seconds
        self.component_name = component_name
        self.logger = logger.bind(tenant_id=tenant_id)

    def _generate_lock_id(self, slot_number: int) -> str:
        """Generate lock resource ID as per TRD format."""
        return f"tenant:{self.tenant_id}:slot:{slot_number}"

    def _generate_owner_id(self, workflow_id: str, activity_id: str) -> str:
        """Generate owner ID as per TRD format."""
        return f"workflow:{workflow_id}:activity:{activity_id}"

    async def acquire_slots(
        self, count: int, workflow_id: str, activity_id: str
    ) -> List[SlotInfo]:
        """
        Acquire specified number of slots for activities.

        Args:
            count: Number of slots needed
            workflow_id: ID of the workflow
            activity_id: ID of the activity

        Returns:
            List of acquired slot information
        """
        acquired_slots = []
        owner_id = self._generate_owner_id(workflow_id, activity_id)

        with DaprClient() as dapr_client:
            for slot in range(self.max_slots):
                if len(acquired_slots) >= count:
                    break

                lock_id = self._generate_lock_id(slot)
                try:
                    response = await dapr_client.lock(
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
                                acquired_at=datetime.utcnow(),
                            )
                        )
                        metrics.increment("slot.acquisition.success")
                        self.logger.info(
                            "Slot acquired",
                            extra={"slot": slot, "workflow_id": workflow_id},
                        )
                except Exception as e:
                    self.logger.error(
                        "Error acquiring slot",
                        extra={"slot": slot, "error": str(e)},
                        exc_info=True,
                    )
                    metrics.increment("slot.acquisition.error")

        return acquired_slots

    async def release_slot(self, slot_info: SlotInfo) -> bool:
        """
        Release a specific slot.

        Args:
            slot_info: Information about the slot to release

        Returns:
            bool: True if release was successful
        """
        try:
            with DaprClient() as dapr_client:
                response = await dapr_client.unlock(
                    store_name=self.component_name,
                    resource_id=slot_info.lock_id,
                    lock_owner=slot_info.owner_id,
                )

            if response.success:
                self.logger.info("Slot released", extra={"slot": slot_info.slot_number})
                metrics.increment("slot.release.success")
                return True

            self.logger.warning(
                "Failed to release slot", extra={"slot": slot_info.slot_number}
            )
            metrics.increment("slot.release.failed")
            return False

        except Exception as e:
            self.logger.error(
                "Error releasing slot",
                extra={"slot": slot_info.slot_number, "error": str(e)},
                exc_info=True,
            )
            metrics.increment("slot.release.error")
            return False

    async def get_active_slots(self) -> int:
        """Get number of currently active slots."""
        active_count = 0
        for slot in range(self.max_slots):
            lock_id = self._generate_lock_id(slot)
            try:
                with DaprClient() as dapr_client:
                    response = await dapr_client.lock(
                        store_name=self.component_name,
                        resource_id=lock_id,
                        lock_owner="health_check",
                        expiry_in_seconds=0,
                    )
                    if not response.success:
                        active_count += 1
            except:
                active_count += 1

        metrics.gauge("slots.active", active_count)
        return active_count
