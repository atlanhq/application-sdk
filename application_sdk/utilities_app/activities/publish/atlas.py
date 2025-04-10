import asyncio
from typing import Dict, Any

from application_sdk.activities import ActivitiesInterface, ActivitiesState, get_workflow_id
from temporalio import activity

from application_sdk.common.logger_adaptors import get_logger

logger = get_logger(__name__)

class AtlasPublishAtlanActivitiesState(ActivitiesState):
    access_token: str


class AtlasPublishAtlanActivities(ActivitiesInterface):
    async def get_impersonation_token(self) -> str:
        return "test"

    # async def get_impersonation_token(self) -> str:
    #     async with aiohttp.ClientSession() as session:
    #         async with session.post(self.token_url, data={
    #             "client_id": self.client_id,
    #             "client_secret": self.client_secret,
    #             "grant_type": "client_credentials",
    #         }) as first_response:
    #             response = await first_response.json()
    #             token = response.get("access_token")
    #             if not token:
    #                 raise Exception("Failed to get access token")

    #             async with session.post(self.token_url, data={
    #                          "client_id": self.client_id,
    #                 "client_secret": self.client_secret,
    #                 "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
    #                 "subject_token": token,
    #                 "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
    #             }) as second_response:
    #                 response = await second_response.json()
    #                 token = response.get("access_token")
    #                 if not token:
    #                     raise Exception("Failed to get access token")

    #                 return token

    async def _set_state(self, workflow_args: Dict[str, Any]) -> None:
        workflow_id = get_workflow_id()
        if not self._state.get(workflow_id):
            self._state[workflow_id] = AtlasPublishAtlanActivitiesState()

        if self._state.get("access_token") is not None:
            return

        self._state[workflow_id].access_token = await self.get_impersonation_token()

    @activity.defn
    async def plan(self, plan_args: Dict[str, Any]):
        """
        This activity is used to plan the publish to Atlas.
        It will return the assets types that will be published to Atlas.

        TODO:
            currently this is a placeholder activity, will be replaced
            with the actual activity that will plan the publish to Atlas.
        """
        logger.info("Planning to publish to Atlas")
        await asyncio.sleep(3)
        logger.info(f"Plan args: {plan_args}")
        return {
            "assets_types": [
                ["Database"],
                ["Schema"],
                ["Table", "View", "MaterializedView"],
                ["Column","TablePartition"],
            ],
        }

    @activity.defn
    async def publish(self, publish_args: Dict[str, Any]):
        """
        This activity is used to publish the assets to Atlas.

        TODO:
            currently this is a placeholder activity, will be replaced
            with the actual activity that will publish the assets to Atlas.
        """
        logger.info(f"Publishing to atlas with config: {publish_args}")

    @activity.defn
    async def publish_bulk(self, file_prefix: str):
        """
        This activity is used to publish the assets to Atlas in bulk.

        TODO:
            currently this is a placeholder activity, will be replaced
            with the actual activity that will publish the assets to Atlas in bulk.
        """

        logger.info("Publishing bulk to Atlas")
        # with aiohttp.ClientSession(
        #         headers={"Authorization": f"Bearer {self._state[get_workflow_id()].access_token}"}
        # ) as session:
        #     async with session.post(f"http://{self.host}:{self.port}/api/v1/bulk", data={}) as response:
        #         if response.status != 200:
        #             raise Exception(f"Failed to publish attributes: {response.status}")

    async def publish_classifications(self):
        pass

    async def publish_custom_attributes(self):
        pass
