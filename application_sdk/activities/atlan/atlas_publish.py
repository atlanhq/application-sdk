from typing import Optional, Dict, Any

from application_sdk.activities import ActivitiesInterface, ActivitiesState, get_workflow_id
from temporalio import activity
import aiohttp


class AtlasPublishActivitiesState(ActivitiesState):
    access_token: str

class AtlasPublishActivities(ActivitiesInterface):

    def __init__(self, host: str, port: int, client_id: str, client_secret: str, token_url: str,
                 impersonate_user: Optional[str]):
        super().__init__()
        self.host = host
        self.port = port
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.impersonate_user = impersonate_user


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
            self._state[workflow_id] = AtlasPublishActivitiesState()

        if self._state.get("access_token") is not None:
            return

        self._state[workflow_id].access_token = await self.get_impersonation_token()


    async def publish_attributes(self):
        with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._state[get_workflow_id()].access_token}"}
        ) as session:
            async with session.post(f"http://{self.host}:{self.port}/api/v1/attributes", data={}) as response:
                if response.status != 200:
                    raise Exception(f"Failed to publish attributes: {response.status}")

    async def publish_classifications(self):
        pass

    async def publish_custom_attributes(self):
        pass

