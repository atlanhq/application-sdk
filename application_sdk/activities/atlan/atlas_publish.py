from typing import Optional, Dict, Any

from application_sdk.activities import ActivitiesInterface, ActivitiesState, get_workflow_id
from temporalio import activity
import aiohttp

from application_sdk.common.logger_adaptors import get_logger

logger = get_logger(__name__)

class AtlasPublishAtlanActivitiesState(ActivitiesState):
    access_token: str


class AtlasPublishAtlanActivities:
    async def get_impersonation_token(self) -> str:
        async with aiohttp.ClientSession() as session:
            async with session.post(self.token_url, data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            }) as first_response:
                response = await first_response.json()
                token = response.get("access_token")
                if not token:
                    raise Exception("Failed to get access token")

                async with session.post(self.token_url, data={
                             "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token": token,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                }) as second_response:
                    response = await second_response.json()
                    token = response.get("access_token")
                    if not token:
                        raise Exception("Failed to get access token")

                    return token

    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        workflow_id = get_workflow_id()
        if not self._state.get(workflow_id):
            self._state[workflow_id] = AtlasPublishAtlanActivitiesState()

        if self._state.get("access_token") is not None:
            return

        self._state[workflow_id].access_token = await self.get_impersonation_token()

    @activity.defn
    async def publish(self):
        logger.info("Publishing to Atlas")

    @activity.defn
    async def publish_bulk(self, file_prefix: str):
        with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._state[get_workflow_id()].access_token}"}
        ) as session:
            async with session.post(f"http://{self.host}:{self.port}/api/v1/bulk", data={}) as response:
                if response.status != 200:
                    raise Exception(f"Failed to publish attributes: {response.status}")

    async def publish_classifications(self):
        pass

    async def publish_custom_attributes(self):
        pass

