import asyncio

from application_sdk.activities.agents.langgraph import register_graph_builder
from application_sdk.agents import AgentState, LangGraphWorkflow
from application_sdk.clients.temporal import TemporalClient
from application_sdk.worker import Worker
from examples.agents.workflow import get_workflow

# Register the graph builder
register_graph_builder("workflow", get_workflow)


async def run_my_workflow(daemon: bool = True):
    """Initializes and runs the LangGraph workflow."""
    temporal_client = TemporalClient(application_name="my-langgraph")
    await temporal_client.load()

    # Create worker but don't start it yet
    worker = Worker(
        temporal_client=temporal_client,
        workflow_classes=[LangGraphWorkflow],
        temporal_activities=LangGraphWorkflow.get_activities(
            LangGraphWorkflow.activities_cls()
        ),
        max_concurrent_activities=5,
    )

    # Start the worker as a daemon (non-blocking)
    print("Starting worker...")
    await worker.start(daemon=daemon)

    # Give the worker a moment to fully initialize
    await asyncio.sleep(1)

    # Prepare workflow input with query, initial state, and graph builder name
    workflow_input = {
        "user_query": "Get me the books that are related to horror",
        "state": AgentState(messages=[]),  # Initial state for the workflow
        "graph_builder_name": "workflow",  # Name of the registered graph builder
    }

    try:
        # Start the workflow with the correct parameter order
        response = await temporal_client.start_workflow(
            workflow_class=LangGraphWorkflow,
            workflow_args=workflow_input,
        )

        print("Workflow started, waiting for result...")

        # Get the workflow handle from the response and await its result
        if "handle" in response:
            # Since worker is running as daemon, we can just wait for the result
            result = await response["handle"].result()
            print("Workflow result:", result)

            # Allow some time for cleanup
            await asyncio.sleep(2)

            return result
        else:
            print("Workflow started with ID:", response["workflow_id"])
            return response

    except Exception as e:
        print(f"Error in workflow execution: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(run_my_workflow(daemon=True))
