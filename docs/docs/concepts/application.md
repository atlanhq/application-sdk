# Application

This module provides the generic application abstraction for orchestrating workflows, workers, and (optionally) servers in the Application SDK. It is designed to be flexible enough for both simple workflows (like Hello World) and more specialized applications (like SQL metadata extraction).

## Core Concepts

### 1. `BaseApplication` (`application_sdk.application.__init__.py`)
- **Purpose:**
  - Provides a standard, reusable way to set up and run Temporal workflows, including workflow client, worker, and (optionally) FastAPI server setup.
  - Can be used directly for simple applications, or subclassed for more complex/specialized use cases.

- **Key Methods:**
  - `__init__(name, handler=None, server=None)`: Initializes the application with a name, and optionally a handler or server.
  - `setup_workflow(workflow_classes, activities_class)`: Sets up the workflow client and worker for the application.
  - `start_workflow(workflow_args, workflow_class)`: Starts a new workflow execution.
  - `start_worker(daemon=True)`: Starts the worker for the application.
  - `setup_server(workflow_class)`: (Optional) Sets up a server for the application (no-op by default).
  - `start_server()`: (Optional) Starts the server for the application (no-op by default).

- **Usage Patterns:**
  - For simple workflows (e.g., Hello World):
    ```python
    from application_sdk.application import BaseApplication
    from my_workflows import HelloWorldWorkflow, HelloWorldActivities

    app = BaseApplication(
        name="hello-world"
    )
    await app.setup_workflow(
        workflow_and_activities_classes=[
            (HelloWorldWorkflow, HelloWorldActivities)
        ],
    )
    await app.start_workflow(workflow_args={}, workflow_class=HelloWorldWorkflow)
    await app.start_worker()
    ```
  - For specialized workflows (e.g., SQL):
    - Subclass `BaseApplication` and add/override methods as needed.

- **Extensibility:**
  - The class is designed to be subclassed for more complex applications (see `BaseSQLMetadataExtractionApplication` for an example).

## Summary

The `BaseApplication` class provides a unified, flexible entry point for building workflow-driven applications in the SDK. It enables both rapid prototyping of simple workflows and robust orchestration for advanced use cases.
