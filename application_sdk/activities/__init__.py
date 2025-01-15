from typing import Dict, Any
from abc import ABC
from application_sdk.activities.utils import get_workflow_id

class ActivitiesInterface(ABC):
    def __init__(self):
        self._state: Dict[str, Any] = {}

    # State methods
    async def _set_state(self, workflow_args: Dict[str, Any]):
        self._state[get_workflow_id()] = {
            "workflow_args": workflow_args,
        }
    
    async def _get_state(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        if get_workflow_id() not in self._state:
            await self._set_state(workflow_args)
        return self._state[get_workflow_id()]
    
    async def _clean_state(self):
        self._state.pop(get_workflow_id())
