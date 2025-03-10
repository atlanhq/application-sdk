"""
Example showing how to use the FastAPIAgentApplication.
Just define your workflow and run it!
"""

import asyncio

from application_sdk.application.fastapi import FastAPIAgentApplication
from application_sdk.clients.temporal import TemporalClient
from examples.agents.workflow import get_state_graph


async def run_agent_server():
    """Initialize and run the agent server."""
    # Create the application instance
    temporal_client = TemporalClient(application_name="agent-langgraph")
    await temporal_client.load()
    app = FastAPIAgentApplication(temporal_client=temporal_client)

    # Register your langgraphs' state graph
    app.register_graph(
        state_graph_builder=get_state_graph, graph_builder_name="my_agent"
    )
    await app.setup_worker(temporal_client)
    await app.worker.start(daemon=True)

    # Start the server
    await app.start()


if __name__ == "__main__":
    asyncio.run(run_agent_server())
