from application_sdk.activities import ActivitiesInterface
from typing import Dict, Any, Type
from application_sdk.activities.utils import get_workflow_id
import asyncio
from temporalio import activity


# ------------------------------ TEMPORARY ------------------------------
class SQLClient:
    def set_credentials(self, credentials: Dict[str, Any]):
        pass
    async def load(self):
        pass
    async def close(self):
        pass

class SQLHandler:
    def set_sql_client(self, sql_client: SQLClient):
        pass
class SQLAuthHandler:
    def set_sql_client(self, sql_client: SQLClient):
        pass
class FetchMetadataHandler:
    def set_sql_client(self, sql_client: SQLClient):
        pass
# ------------------------------ TEMPORARY ------------------------------



class SQLExtractionActivities(ActivitiesInterface):
    state: Dict[str, Any] = {}

    sql_client_class: Type[SQLClient] = SQLClient
    handler_class: Type[SQLHandler] = SQLHandler

    
    # State methods
    async def _set_state(self, workflow_args: Dict[str, Any]):
        await super()._set_state(workflow_args)

        sql_client = self.sql_client_class()
        await sql_client.load()
        sql_client.set_credentials(workflow_args["credentials"])

        handler = self.handler_class()
        handler.set_sql_client(sql_client)

        self.state[get_workflow_id()] = {
            # Client
            "sql_client": sql_client,

            # Handlers
            "handler": handler,
        }

    async def _clean_state(self):
        await self.state["sql_client"].close()

        await super()._clean_state()

    # Activities
    # TODO: Add decorators
    @activity.defn
    async def fetch_databases(self, workflow_args: Dict[str, Any]):
        state = await self._get_state(workflow_args)
        print(state)

        await asyncio.sleep(5)
        
    # TODO: Add decorators
    @activity.defn
    async def fetch_tables(self, workflow_args: Dict[str, Any]):
        state = await self._get_state(workflow_args)
        print(state)

        await asyncio.sleep(5)

    # TODO: Add decorators
    @activity.defn
    async def fetch_columns(self, workflow_args: Dict[str, Any]):
        state = await self._get_state(workflow_args)
        print(state)

        await asyncio.sleep(5)

    @activity.defn
    async def transform_metadata(self, workflow_args: Dict[str, Any]):
        state = await self._get_state(workflow_args)
        print(state)

        await asyncio.sleep(5)
        
    @activity.defn
    async def write_metadata(self, workflow_args: Dict[str, Any]):
        state = await self._get_state(workflow_args)
        print(state)

        await asyncio.sleep(5)
