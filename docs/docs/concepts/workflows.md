# Workflows

This module defines the orchestration logic for application processes using Temporal workflows. Workflows sequence and manage the execution of individual `Activities` to accomplish larger tasks like metadata extraction or query log mining.

## Purpose and Role of Workflows

Workflows are the backbone of the application's long-running processes. They provide:

1.  **Orchestration:** Define the sequence, parallelism, and conditional logic for executing a series of `Activities`. They dictate *what* activities run and *when*.
2.  **State Management (Implicit):** While workflows don't typically hold large amounts of business data directly (that's passed via arguments or handled by activities interacting with storage), their execution progress represents the state of the overall process.
3.  **Reliability:** Leverage Temporal's features for handling failures, including automatic retries of activities, timeouts, and ensuring eventual completion of the process even if workers restart.
4.  **Configuration Handling:** Receive initial configuration and typically load detailed arguments required for the entire process via `StateStoreInput`.

Essentially, workflows are durable functions that coordinate activities to achieve a specific business goal.

## `WorkflowInterface` (`application_sdk.workflows.__init__.py`)

This is the abstract base class for all workflow implementations within the SDK.

*   **`@workflow.defn`:** Decorator required by Temporal to identify the class as a workflow definition.
*   **`activities_cls` (Class Attribute):** Must be set by subclasses to specify the `ActivitiesInterface` implementation that contains the activity methods this workflow will orchestrate.
*   **`get_activities(activities: ActivitiesInterface)` (Static Method):**
    *   **Purpose:** Must be implemented by subclasses to define the specific sequence of activity *methods* that the Temporal `Worker` should register for this workflow type.
    *   **Input:** Takes an *instance* of the `activities_cls`.
    *   **Output:** Returns a sequence of *callable activity methods* from the provided activities instance (e.g., `[activities.preflight_check, activities.fetch_data, activities.transform_data]`).
*   **`run(self, workflow_config: Dict[str, Any])` (Async Method):**
    *   **Purpose:** The entry point for the workflow's execution logic. Must be decorated with `@workflow.run`.
    *   **Input:** Receives an initial configuration dictionary (`workflow_config`), which *must* contain the `workflow_id`.
    *   **Base Implementation:** The base `WorkflowInterface.run` method retrieves the full `workflow_args` using `StateStoreInput.extract_configuration(workflow_id)` and executes the `preflight_check` activity defined in the associated `activities_cls`. Subclasses typically call `await super().run(workflow_config)` first and then add their specific activity orchestration logic.

## Provided Base Workflows

The SDK includes base workflow implementations for common patterns:

1.  **`BaseSQLMetadataExtractionWorkflow` (`workflows/metadata_extraction/sql.py`)**:
    *   **Purpose:** Orchestrates the extraction of metadata (databases, schemas, tables, columns, procedures) from SQL sources.
    *   **Default Activities (`get_activities`)**: `preflight_check`, `fetch_databases`, `fetch_schemas`, `fetch_tables`, `fetch_columns`, `fetch_procedures`, `transform_data`.
    *   **Orchestration (`run` method):** Executes preflight, then fetches and transforms metadata types concurrently using `asyncio.gather`.

2.  **`SQLQueryExtractionWorkflow` (`workflows/query_extraction/sql.py`)**:
    *   **Purpose:** Orchestrates the extraction of query history/logs from SQL sources, often involving batching.
    *   **Default Activities (`get_activities`)**: `get_query_batches`, `fetch_queries`, `preflight_check`.
    *   **Orchestration (`run` method):** Executes preflight, gets query batches, then executes `fetch_queries` for each batch concurrently using `asyncio.gather`.

## Configuration

Workflows are initiated with a minimal `workflow_config` dictionary containing the `workflow_id`. The first step in the `run` method (in the base `WorkflowInterface`) uses this `workflow_id` to call `StateStoreInput.extract_configuration(workflow_id)`, fetching the complete set of arguments (`workflow_args`) required for the execution. These `workflow_args` are passed down to the activities executed by the workflow.

## Extending Workflows

While most customization typically happens in `Activities`, `Handlers`, or `Clients`, you might need to extend a workflow if you want to:

1.  **Change the Sequence/Set of Activities:** Modify the order, add new custom activities, or remove existing activities. This is the most common reason.
2.  **Implement Complex Control Flow:** Add conditional logic (`if/else`), loops, or specific error handling *between* activity executions directly within the workflow's orchestration logic.

**How to Extend:**

*   Create a new class inheriting from the base workflow (e.g., `class MyCustomMetadataWorkflow(BaseSQLMetadataExtractionWorkflow):`).

*   **Option 1: Override `get_activities` (Most Common)**
    *   **Use Case:** When you only need to change the list or order of activities executed sequentially or in simple parallel patterns managed by the base workflow's `run` method.
    *   **Implementation:** Define a new static `get_activities` method returning your desired sequence of activity methods. Requires a corresponding extended `Activities` class containing any new activity methods.

    ```python
    # In my_connector/workflows.py
    # ... (imports including MyExtendedActivities) ...
    from typing import Any, Callable, Sequence, cast, Type
    from temporalio import workflow
    from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow
    from application_sdk.activities import ActivitiesInterface
    from .activities import MyExtendedActivities

    @workflow.defn
    class MyWorkflowWithCustomSequence(BaseSQLMetadataExtractionWorkflow):
        # Point to your extended Activities class
        activities_cls: Type[ActivitiesInterface] = MyExtendedActivities

        @staticmethod
        def get_activities(
            activities: ActivitiesInterface,
        ) -> Sequence[Callable[..., Any]]:
            my_activities = cast(MyExtendedActivities, activities)
            # Return a modified sequence
            return [
                my_activities.preflight_check,
                my_activities.fetch_databases,
                # ... other fetch activities ...
                my_activities.perform_custom_validation, # Added custom step
                my_activities.transform_data,
            ]
        # Inherits the 'run' method logic from BaseSQLMetadataExtractionWorkflow
    ```

*   **Option 2: Override `run` (Advanced Customization)**
    *   **Use Case:** When you need fundamentally different orchestration logic beyond just changing the sequence in `get_activities`.
    *   **Implementation:** Define an `async def run(self, workflow_config: Dict[str, Any])` method decorated with `@workflow.run`.
        *   You are responsible for the entire orchestration flow.
        *   You **must** handle retrieving the full `workflow_args` using the `get_workflow_args` activity.
        *   You execute activities using `await workflow.execute_activity_method(...)`.
        *   Calling `await super().run(workflow_config)` might be useful for initial setup (like preflight) but isn't required if you handle all steps.

    ```python
    # In my_connector/workflows.py
    # ... (imports including MyExtendedActivities, StateStoreInput, RetryPolicy) ...
    import asyncio
    from datetime import timedelta
    from temporalio.common import RetryPolicy
    from application_sdk.inputs.statestore import StateStoreInput
    # Assume MyExtendedActivities defines: preflight_check, custom_step_one, custom_step_two

    @workflow.defn
    class MyWorkflowWithCustomRunLogic(BaseSQLMetadataExtractionWorkflow):
        # Still need to point to the Activities class containing the methods
        activities_cls: Type[ActivitiesInterface] = MyExtendedActivities

        # get_activities might still be defined if needed by worker registration,
        # but the 'run' method now dictates execution.
        @staticmethod
        def get_activities(
            activities: ActivitiesInterface,
        ) -> Sequence[Callable[..., Any]]:
             my_activities = cast(MyExtendedActivities, activities)
             return [
                 my_activities.preflight_check, # Still needed if super().run is called
                 my_activities.custom_step_one,
                 my_activities.custom_step_two,
             ]

        @workflow.run
        async def run(self, workflow_config: Dict[str, Any]) -> str: # Return type example
            # 1. Load full arguments (Essential step)
            workflow_id = workflow_config["workflow_id"]
            workflow_args: Dict[str, Any] = await workflow.execute_activity_method(
                self.activities_cls.get_workflow_args,
                workflow_config,  # Pass the whole config containing workflow_id
                retry_policy=RetryPolicy(maximum_attempts=3, backoff_coefficient=2),
                start_to_close_timeout=self.default_start_to_close_timeout,
                heartbeat_timeout=self.default_heartbeat_timeout,
            )
            workflow.logger.info(f"Starting custom run logic for {workflow_id}")
            workflow_args["workflow_run_id"] = workflow.info().run_id # Add run ID

            # Define a standard retry policy for activities
            retry_policy = RetryPolicy(maximum_attempts=3)

            # 2. Execute initial activities (Optional: Reuse base preflight)
            # await super().run(workflow_config) # Option 1: Run base preflight first

            # Option 2: Or call preflight manually if not calling super().run
            await workflow.execute_activity_method(
                 self.activities_cls.preflight_check,
                 args=[workflow_args], retry_policy=retry_policy,
                 start_to_close_timeout=self.default_start_to_close_timeout,
                 heartbeat_timeout=self.default_heartbeat_timeout,
            )
            # Add checks on preflight result if needed...

            # 3. Custom Orchestration Logic (Simplified Example)
            workflow.logger.info("Executing custom step one...")
            result_one = await workflow.execute_activity_method(
                 self.activities_cls.custom_step_one,
                 args=[workflow_args],
                 retry_policy=retry_policy,
                 start_to_close_timeout=timedelta(minutes=10),
            )

            # Potentially use result_one to modify workflow_args for the next step
            workflow_args["step_one_output"] = result_one

            workflow.logger.info("Executing custom step two...")
            result_two = await workflow.execute_activity_method(
                 self.activities_cls.custom_step_two,
                 args=[workflow_args],
                 retry_policy=retry_policy,
                 start_to_close_timeout=timedelta(hours=1),
            )

            # 4. Return a result
            return f"Workflow {workflow_id} finished custom run. Result: {result_two}"

    ```

## Registration and Execution

*   **Registration:** When creating a `Worker`, you register your workflow *classes* (base or custom) using `workflow_classes`. You link this to a specific *instance* of your activities class using `workflow_activities`, which calls the workflow's `get_activities` method.
    ```python
    # In worker setup / main application file
    # ... imports including your custom workflow and activities ...
    from temporalio.worker import Worker # Ensure Worker is imported
    from my_connector.workflows import MyWorkflowWithCustomRunLogic # Your custom workflow
    from my_connector.activities import MyExtendedActivities      # Your custom activities
    # Assume workflow_client is defined and connected

    my_activities_instance = MyExtendedActivities(...)

    worker = Worker(
        workflow_client=workflow_client,
        task_queue="my-custom-task-queue",
        # Register YOUR custom workflow class(es)
        workflow_classes=[MyWorkflowWithCustomRunLogic],
        # Link using the get_activities from YOUR workflow class
        workflow_activities=MyWorkflowWithCustomRunLogic.get_activities(my_activities_instance),
    )
    ```
*   **Execution:** Start workflows using `WorkflowClient.start_workflow(workflow_args, workflow_class=MyWorkflowWithCustomRunLogic)`.

## Relationship with Activities

Workflows orchestrate, Activities execute.

*   **Workflow:** Defines *which* activities run, in *what order*, handles control flow (including conditional logic if `run` is overridden), loads configuration, and leverages Temporal features like retries. Uses `workflow.execute_activity_method`.
*   **Activity:** Contains the actual implementation code for a single step. Defined with `@activity.defn`.

## Summary

Workflows define the high-level business logic and orchestration within the SDK. They sequence `Activity` calls, manage the process state via Temporal, and load configuration. Base workflows are provided for common patterns. Extend workflows by overriding `get_activities` (for sequence changes) or `run` (for complex control flow). Register your specific workflow class with the `Worker`.