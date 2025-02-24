import asyncio
from datetime import timedelta
from typing import Any, Dict

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker


class LangGraphActivities:
    """Defines activities related to the LangGraph agent."""

    @activity.defn
    async def compile_graph(self, workflow_args: Dict[str, Any]) -> Dict[str, str]:
        """Compiles and initializes the LangGraph agent."""
        try:
            from application_sdk.agents import LangGraphAgent
            from examples.agents.workflow import get_workflow

            agent = LangGraphAgent(
                workflow=get_workflow(), state=workflow_args["state"]
            )
            agent.compile_graph()
            return {"agent_status": "no error"}

        except ImportError as e:
            return {"error": f"Missing dependency - {str(e)}"}

    @activity.defn
    async def run_agent_task(self, activity_input: Dict[str, Any]) -> Dict[str, Any]:
        """Runs the LangGraph agent with the given task."""
        try:
            if activity_input.get("agent_status") != "no error":
                return {"error": "Error: Agent initialization failed."}

            user_query = activity_input.get(
                "user_query", "Get me the books that are related to science"
            )

            from application_sdk.agents import LangGraphAgent
            from examples.agents.workflow import get_workflow

            agent = LangGraphAgent(
                workflow=get_workflow(), state=activity_input["state"]
            )
            agent.compile_graph()  # this is because, the compiled graph cannot be passed between activities
            result = agent.run(user_query)
            state = agent.state
            return {"result": result, "state": state}

        except Exception as e:
            return {"error": f"Error: {str(e)}"}


@workflow.defn
class LangGraphWorkflow:
    """Temporal Workflow to execute LangGraph agent steps."""

    def __init__(self):
        self.state = {
            "messages": [],
            "enriched_query": "",
            "steps_list": [],
            "validation_response": "",
        }

    @workflow.query
    def get_state(self) -> Dict[str, Any]:
        return self.state

    @workflow.run
    async def run(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        """Executes the LangGraph workflow."""
        workflow_args["state"] = self.state

        # Step 1: Compile graph
        agent_data = await workflow.execute_activity(
            LangGraphActivities.compile_graph,
            workflow_args,
            schedule_to_close_timeout=timedelta(seconds=60),
        )
        # Step 2: Process user query
        user_query = workflow_args.get("user_query")
        if not user_query:
            user_query = input("Enter the task for LangGraph Agent: ")

        activity_input = {
            "agent_status": agent_data.get("agent_status", "failed"),
            "user_query": user_query,
            "state": self.state,
        }

        # Step 3: Run agent task
        result = await workflow.execute_activity(
            LangGraphActivities.run_agent_task,
            activity_input,
            schedule_to_close_timeout=timedelta(seconds=120),
        )
        return result


async def application_langgraph():
    """Initializes and runs the LangGraph workflow."""
    client = await Client.connect("localhost:7233")

    worker = Worker(
        client=client,
        task_queue="langgraph-task-queue",
        workflows=[LangGraphWorkflow],
        activities=[
            LangGraphActivities().compile_graph,
            LangGraphActivities().run_agent_task,
        ],
        max_concurrent_activities=5,
    )

    workflow_input = {
        "user_query": "Get me the books that are related to horror",
    }

    async with worker:
        handle = await client.start_workflow(
            LangGraphWorkflow.run,
            workflow_input,
            id="langgraph-workflow",
            task_queue="langgraph-task-queue",
        )

        return await handle.result()


if __name__ == "__main__":
    asyncio.run(application_langgraph())
