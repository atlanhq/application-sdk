import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from application_sdk.activities.agents.langgraph import register_graph_builder
from application_sdk.agents import AgentState, LangGraphWorkflow
from application_sdk.application.fastapi import FastAPIApplication, HttpWorkflowTrigger
from application_sdk.clients.temporal import TemporalClient
from application_sdk.handlers import HandlerInterface
from application_sdk.worker import Worker
from examples.agents.workflow import get_workflow

# Register the graph builder
register_graph_builder("workflow", get_workflow)


class AgentRequest(BaseModel):
    """Request model for agent endpoints."""

    user_query: str


class AgentHandler(HandlerInterface):
    """Handler for agent-specific operations."""

    async def load(self, **kwargs: Any) -> None:
        pass

    async def test_auth(self, **kwargs: Any) -> bool:
        return True

    async def fetch_metadata(self, **kwargs: Any) -> Any:
        return []

    async def preflight_check(self, **kwargs: Any) -> Any:
        return {"status": "ok"}


class FastAPIAgentApplication(FastAPIApplication):
    """FastAPI application for agent operations."""

    agent_router: APIRouter = APIRouter()
    temporal_client: Optional[TemporalClient] = None
    worker: Optional[Worker] = None
    # Store workflow handles for result retrieval
    workflow_handles: Dict[str, Any] = {}

    def register_routers(self):
        """Register all routers including the agent router."""
        # Register routes first
        self.register_routes()

        # Then include all routers
        self.app.include_router(self.agent_router, prefix="/agent")
        super().register_routers()

    def register_routes(self):
        """Register agent-specific routes."""
        self.agent_router.add_api_route(
            "/query",
            self.process_query,
            methods=["POST"],
        )
        self.agent_router.add_api_route(
            "/result/{workflow_id}",
            self.get_workflow_result,
            methods=["GET"],
        )
        super().register_routes()

    async def process_query(self, request: AgentRequest) -> Dict[str, Any]:
        """Process an agent query.

        Args:
            request: The agent request containing the user query.

        Returns:
            Dict containing the workflow ID and run ID.
        """
        if not self.temporal_client:
            raise Exception("Temporal client not initialized")

        workflow_input = {
            "user_query": request.user_query,
            "state": AgentState(messages=[]),
            "graph_builder_name": "workflow",
        }

        response = await self.temporal_client.start_workflow(
            workflow_class=LangGraphWorkflow,
            workflow_args=workflow_input,
        )

        # Store the workflow handle if available
        if "handle" in response:
            self.workflow_handles[response["workflow_id"]] = response["handle"]

        return {
            "success": True,
            "message": "Workflow started successfully",
            "data": {
                "workflow_id": response.get("workflow_id", ""),
                "run_id": response.get("run_id", ""),
            },
        }

    async def get_workflow_result(self, workflow_id: str) -> Dict[str, Any]:
        """Get the result of a completed workflow.

        Args:
            workflow_id: The ID of the workflow to get results for.

        Returns:
            Dict containing the workflow result.
        """
        if workflow_id not in self.workflow_handles:
            return {
                "success": False,
                "message": "Workflow not found or already completed",
                "data": None,
            }

        try:
            handle = self.workflow_handles[workflow_id]
            result = await handle.result()
            # Remove the handle after getting the result
            del self.workflow_handles[workflow_id]

            return {
                "success": True,
                "message": "Workflow completed successfully",
                "data": result,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Error getting workflow result: {str(e)}",
                "data": None,
            }


async def setup_worker(temporal_client: TemporalClient) -> Worker:
    """Set up and start the Temporal worker.

    Args:
        temporal_client: The Temporal client instance.

    Returns:
        The initialized Worker instance.
    """
    worker = Worker(
        temporal_client=temporal_client,
        workflow_classes=[LangGraphWorkflow],
        temporal_activities=LangGraphWorkflow.get_activities(
            LangGraphWorkflow.activities_cls()
        ),
        max_concurrent_activities=5,
    )

    print("Starting worker...")
    await worker.start(daemon=True)
    await asyncio.sleep(1)
    return worker


async def run_fastapi_agent_application():
    """Initialize and run the FastAPI agent application."""
    # Initialize Temporal client
    temporal_client = TemporalClient(application_name="agent-langgraph")
    await temporal_client.load()

    # Start the worker
    worker = await setup_worker(temporal_client)

    # Create and start the FastAPI application
    fast_api_app = FastAPIAgentApplication(
        handler=AgentHandler(),
        temporal_client=temporal_client,
    )
    fast_api_app.worker = worker

    # Register the LangGraph workflow
    fast_api_app.register_workflow(
        LangGraphWorkflow,
        [
            HttpWorkflowTrigger(
                endpoint="/run",
                methods=["POST"],
                workflow_class=LangGraphWorkflow,
            )
        ],
    )

    await fast_api_app.start()


if __name__ == "__main__":
    asyncio.run(run_fastapi_agent_application())
