from abc import ABC, abstractmethod
from typing import Any, Dict

from temporalio import activity

from application_sdk.activities.utils import auto_heartbeater, get_workflow_id


class ActivitiesInterface(ABC):
    def __init__(self):
        self._state: Dict[str, Any] = {}

    # State methods
    @abstractmethod
    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        raise ValueError("_set_state not implemented")

    async def _get_state(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        if get_workflow_id() not in self._state:
            await self._set_state(workflow_args)
        return self._state[get_workflow_id()]

    async def _clean_state(self):
        self._state.pop(get_workflow_id())

    # TODO: Try it out
    @abstractmethod
    async def preflight_check(self, workflow_args: Dict[str, Any]) -> None:
        raise NotImplementedError("Preflight check not implemented")

    @activity.defn
    @auto_heartbeater
    async def clean_state(self, workflow_args: Dict[str, Any]):
        await self._clean_state()
