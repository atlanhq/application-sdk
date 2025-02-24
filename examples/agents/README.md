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

The system uses Temporal for workflow orchestration. Here's how to run your agent with Temporal:

```python
async def run_workflow():
    client = await Client.connect("localhost:7233")

    workflow_input = {
        "user_query": "Your query here"
    }
    # check the examples examples/agents/applicaiton_agent.py
    handle = await client.start_workflow(
        LangGraphWorkflow.run,
        workflow_input,
        id="langgraph-workflow",
        task_queue="langgraph-task-queue"
    )

    return await handle.result()
```

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
