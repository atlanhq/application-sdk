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
  pip install temporalio langchain langgraph
  ```

### Basic Usage

1. First, define your agent's state structure:

```python
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

def get_workflow():
    workflow = StateGraph()

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
    workflow=get_workflow(),
    state=state
)
agent.compile_graph()
result = agent.run("What is the capital of France?")
```

## Temporal Integration

The system uses Temporal for workflow orchestration. Below is a complete guide on implementing and running workflows with Temporal, based on the `application_agent.py` example:

### 1. Register Your Graph Builder

First, register your workflow builder function with a unique name:

```python
from application_sdk.activities.agents.langgraph import register_graph_builder
from examples.agents.workflow import get_workflow

# Register the graph builder with a unique name
register_graph_builder("workflow", get_workflow)
```

### 2. Create a Function to Run Your Workflow

```python
import asyncio
from application_sdk.agents import LangGraphWorkflow, AgentState
from application_sdk.clients.temporal import TemporalClient
from application_sdk.worker import Worker

async def run_my_workflow(daemon: bool = True):
    """Initializes and runs the LangGraph workflow."""
    # Initialize the Temporal client
    temporal_client = TemporalClient(application_name="my-langgraph")
    await temporal_client.load()

    # Create worker with workflow and activities
    worker = Worker(
        temporal_client=temporal_client,
        workflow_classes=[LangGraphWorkflow],
        temporal_activities=LangGraphWorkflow.get_activities(LangGraphWorkflow.activities_cls()),
        max_concurrent_activities=5,
    )

    # Start the worker (daemon=True for non-blocking)
    print("Starting worker...")
    await worker.start(daemon=daemon)

    # Give the worker a moment to fully initialize
    await asyncio.sleep(1)

    # Prepare workflow input with query, initial state, and graph builder name
    workflow_input = {
        "user_query": "Your query here",  # The user's question or request
        "state": AgentState(messages=[]),  # Initial state for the workflow
        "graph_builder_name": "workflow",  # Name of the registered graph builder
    }

    try:
        # Start the workflow
        response = await temporal_client.start_workflow(
            workflow_class=LangGraphWorkflow,
            workflow_args=workflow_input,
        )

        print("Workflow started, waiting for result...")

        # Get the workflow handle from the response and await its result
        if "handle" in response:
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
```

### 3. Run Your Workflow

```python
if __name__ == "__main__":
    asyncio.run(run_my_workflow(daemon=False))
```

### Key Components Explained

#### AgentState

`AgentState` is used to initialize the workflow state. It can contain various fields like:
- `messages`: Conversation history
- Other fields as needed by your workflow

#### TemporalClient

The `TemporalClient` handles communication with the Temporal server:
- `application_name`: Name for your application
- `load()`: Initializes the connection

#### Worker

The `Worker` processes workflow and activity tasks:
- `workflow_classes`: List of workflow classes to run
- `temporal_activities`: Activities required by the workflow
- `max_concurrent_activities`: Maximum number of parallel activities

#### Workflow Input

The workflow input dictionary includes:
- `user_query`: The query to process
- `state`: Initial state using `AgentState`
- `graph_builder_name`: Name of your registered graph builder

## Components

### LangGraphAgent

The `LangGraphAgent` class provides:
- Graph compilation and execution
- State management
- Error handling
- Workflow visualization

### StateGraph

The `StateGraph` allows you to:
- Define nodes for different processing steps
- Create edges for workflow transitions
- Handle conditional routing
- Manage state transitions

## Best Practices

1. **State Management**
   - Keep state immutable within nodes
   - Use clear state keys
   - Handle state updates atomically

2. **Error Handling**
   - Implement proper error handling in each node
   - Use try-catch blocks for external service calls
   - Log errors appropriately

3. **Workflow Design**
   - Keep nodes focused on single responsibilities
   - Use clear naming conventions
   - Document node purposes and requirements

## Debugging and Monitoring

- Use the `visualize()` method to see your workflow graph
- Monitor Temporal workflows through the Web UI
- Check activity logs for detailed execution information

## Contributing

When contributing new agents or workflows:
1. Follow the existing pattern for state management
2. Add comprehensive documentation
3. Include error handling
4. Add appropriate tests
5. Update this README with new patterns or best practices

## Troubleshooting

Common issues and solutions:
- **Graph Compilation Errors**: Check node connections and state structure
- **State Serialization**: Ensure all state values are JSON-serializable
- **Temporal Timeouts**: Adjust timeout settings in workflow configuration
- **Memory Issues**: Monitor state size and clean up when possible

## Additional Resources

- [LangChain Documentation](https://python.langchain.com/docs/get_started/introduction)
- [Temporal Documentation](https://docs.temporal.io/dev-guide/python)
- [LangGraph GitHub](https://github.com/langchain-ai/langgraph)
