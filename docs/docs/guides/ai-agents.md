# LangGraph Agent Development Guide

This guide explains how to develop and use agents with LangGraph and Temporal workflows in our application SDK.

## Overview

The LangGraph agent system combines LangChain's capabilities with Temporal workflows to create robust, stateful agents that can handle complex tasks. The system is built around two main components:

1. `LangGraphAgent`: A base agent class that manages LangGraph workflows
2. `StateGraph`: A workflow definition system for creating agent logic flows

## Getting Started

### Prerequisites

- Python 3.9+
- Temporal server running locally or accessible
- Required Python packages (install via pip):
  ```bash
  pip install application-sdk[langgraph_agent]
  ```

### Basic Usage

1. First, define your agent's state structure:

```python
class AtlanAgentState(TypedDict):
    user_query: str
    messages: Annotated[list[AnyMessage], add_messages]
    steps_list: list[AtlanAgentTaskStep]
    validation_response: str
    enriched_query: str

state = {
    "messages": [],        # Conversation history
    "enriched_query": "",  # Processed user query
    "steps_list": [],     # Steps taken by the agent
    "validation_response": "", # Validation results
}
```

2. Create your workflow using StateGraph:

- You can check the examples on how to build the workflow: https://langchain-ai.github.io/langgraph/tutorials/introduction/

```python
from langgraph.graph import StateGraph, START, END

def get_state_graph():
    workflow = StateGraph(AtlanAgentState)

    # Add your nodes
    workflow.add_node("enrichment_node", enrichment_node)
    workflow.add_node("planner_agent", planner_agent)
    workflow.add_node("tool_node", tool_node)

    # Define the flow
    workflow.add_edge(START, "enrichment_node")
    workflow.add_edge("enrichment_node", "planner_agent")
    workflow.add_edge("planner_agent", END)

    return workflow
```

3. Initialize and run the agent:

```python
from application_sdk.agents import LangGraphAgent

agent = LangGraphAgent(
    workflow=get_state_graph(),
    state=state
)
agent.compile_graph()
result = agent.run("What is the capital of France?")
```

## FastAPI Integration

The system provides a FastAPI integration for serving your LangGraph agents via HTTP endpoints. Here's how to create a FastAPI server for your agent:

### Creating a FastAPI Agent Server

```python
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
    import asyncio
    asyncio.run(run_agent_server())
```

### Using the API

Once the server is running, you can:

1. Access the API documentation at `/docs`
2. Send requests to the agent endpoints:
   - POST `/api/v1/agent/query` - Run a single agent query
   - GET `/api/v1/agent/result/{workflow_id}` - Get the result of an agent workflow

Example request:
```bash
curl -X POST "http://localhost:8000/api/v1/agent/query" \
     -H "Content-Type: application/json" \
     -d '{"user_query": "What is the capital of France?", "workflow_state": {"messages": []}}'
```

## Additional Resources

- [LangChain Documentation](https://python.langchain.com/docs/get_started/introduction)
- [Temporal Documentation](https://docs.temporal.io/dev-guide/python)
- [LangGraph GitHub](https://github.com/langchain-ai/langgraph)