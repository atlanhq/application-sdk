# Activities

This module contains the building blocks for defining the individual steps (activities) within your Temporal workflows. Activities represent distinct units of work, such as querying a database, calling an API, or transforming data.

## Core Concepts

1.  **Activities Interface (`ActivitiesInterface`)**:
    *   **Purpose:** This is the abstract base class that all activity collections should inherit from. It provides foundational functionality, particularly for managing state across different activities within the same workflow run.
    *   **State Management:** It maintains a dictionary (`_state`) mapping each unique `workflow_id` to its corresponding `ActivitiesState`. This ensures that state (like handler instances or workflow arguments) is isolated between concurrent workflow executions. Key methods include `_get_state`, `_set_state`, and `_clean_state`.

2.  **Activity State (`ActivitiesState`)**:
    *   **Purpose:** A Pydantic model representing the shared state for a single workflow run's activities.
    *   **Contents:** Typically holds an instance of a `HandlerInterface` subclass (responsible for actual external interactions) and the `workflow_args` passed to the workflow execution.

3.  **Handlers (`HandlerInterface`)**:
    *   **Purpose:** While not defined directly in this module, activities heavily rely on Handlers (defined in `application_sdk.handlers`). Handlers encapsulate the logic for interacting with specific external systems (e.g., SQL databases, APIs). Activities delegate the actual work to methods within the handler instance stored in their `ActivitiesState`. This promotes separation of concerns between workflow orchestration (Activities) and external system interaction (Handlers).

4.  **Temporal Integration**:
    *   Activities are typically defined as methods within a class inheriting from `ActivitiesInterface`.
    *   Methods intended to be Temporal activities must be decorated with `@activity.defn`.
    *   The `@auto_heartbeater` decorator (from `application_sdk.activities.common.utils`) can be used to automatically manage Temporal heartbeating for long-running activities.

## Base Activity Classes

The SDK provides pre-built base activity classes for common tasks, primarily focused on metadata extraction.

*   **`BaseSQLMetadataExtractionActivities`** (in `application_sdk.activities.metadata_extraction.sql`):
    *   **Purpose:** Provides a standard set of activities for extracting metadata (databases, schemas, tables, columns, procedures) from SQL-like sources.
    *   **Activities:** Includes `preflight_check`, `fetch_databases`, `fetch_schemas`, `fetch_tables`, `fetch_columns`, `fetch_procedures`, and `transform_data`.
    *   **Extensibility:** Defines default SQL queries as class attributes (e.g., `fetch_database_sql`, `fetch_schema_sql`) which can be easily overridden in subclasses.

*   **`SQLQueryExtractionActivities`** (in `application_sdk.activities.query_extraction.sql`):
    *   **Purpose:** Provides activities specifically for extracting query history or logs from SQL sources.
    *   **Activities:** Includes `preflight_check`, `fetch_queries`, `transform_data`.
    *   **Extensibility:** Defines `fetch_queries_sql` as a class attribute for customization.

## Usage Patterns

### 1. Reusing Base Activities (No Customization)

If the default behavior and SQL queries provided by a base class (like `BaseSQLMetadataExtractionActivities`) are sufficient, you can use it directly when setting up your worker. You'll need to provide appropriate `sql_client_class` and `handler_class` implementations during instantiation.

```python
# In your application setup (e.g., application_sql.py)
from application_sdk.activities.metadata_extraction.sql import BaseSQLMetadataExtractionActivities
from application_sdk.worker import Worker
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow
from your_app.clients import CustomSQLClient # Your SQL client implementation
from your_app.handlers import CustomSQLHandler # Your SQL handler implementation

# Instantiate the base activities with your specific client and handler
activities_instance = BaseSQLMetadataExtractionActivities(
    sql_client_class=CustomSQLClient,
    handler_class=CustomSQLHandler
)

# Register the workflow and its activities with the worker
worker = Worker(
    workflow_client=workflow_client,
    workflow_classes=[BaseSQLMetadataExtractionWorkflow],
    # The workflow's get_activities method links the activities instance
    workflow_activities=BaseSQLMetadataExtractionWorkflow.get_activities(activities_instance),
)

# ... rest of worker setup and start
```

### 2. Customizing SQL Queries

There are two main ways to customize the SQL queries used by the base activities:

**a) Overriding SQL String Attributes (Inline)**

If you only need to change the SQL queries used by the base activities, create a new class inheriting from the base and redefine the relevant SQL string class attributes directly within the class definition.

```python
# In your application's activities file (e.g., my_activities.py)
from application_sdk.activities.metadata_extraction.sql import BaseSQLMetadataExtractionActivities

# Define custom SQL queries inline
FETCH_MY_TABLES_SQL = """
SELECT table_name, schema_name FROM my_custom_catalog.tables WHERE ...
"""

FETCH_MY_COLUMNS_SQL = """
SELECT column_name, data_type FROM my_custom_catalog.columns WHERE ...
"""

# Inherit from the base and override specific SQL attributes
class CustomSQLActivitiesInline(BaseSQLMetadataExtractionActivities):
    fetch_table_sql = FETCH_MY_TABLES_SQL
    fetch_column_sql = FETCH_MY_COLUMNS_SQL
    # Other activities will use the base implementation and SQL

# In your application setup:
# ... import CustomSQLActivitiesInline, CustomSQLClient, CustomSQLHandler ...

activities_instance = CustomSQLActivitiesInline(
    sql_client_class=CustomSQLClient,
    handler_class=CustomSQLHandler
)

# ... register with worker as before ...
```

**b) Loading SQL from Files**

For more complex or numerous queries, it's often cleaner to store them in separate `.sql` files. The SDK provides a utility function `read_sql_files` in `application_sdk.common.utils` for this purpose. By default, it looks in a directory named `queries` relative to where it's called, but you can specify a different path.

```python
# Example file structure:
# my_app/
#   activities.py
#   sql_queries/ # Directory containing SQL files
#     FETCH_TABLES.sql
#     FETCH_COLUMNS.sql
#   main.py

# In your application's activities file (e.g., my_app/activities.py)
import os
from application_sdk.activities.metadata_extraction.sql import BaseSQLMetadataExtractionActivities
from application_sdk.common.utils import read_sql_files

# Define the path to your SQL files relative to this file
SQL_QUERIES_PATH = os.path.join(os.path.dirname(__file__), "sql_queries")

# Load all SQL files from the specified directory
# The keys in the dictionary will be uppercase filenames without .sql (e.g., 'FETCH_TABLES')
loaded_queries = read_sql_files(queries_prefix=SQL_QUERIES_PATH)

# Inherit from the base and assign loaded SQL using the keys
class CustomSQLActivitiesFromFile(BaseSQLMetadataExtractionActivities):
    # Use .get() for safety in case a file is missing
    fetch_table_sql = loaded_queries.get("FETCH_TABLES")
    fetch_column_sql = loaded_queries.get("FETCH_COLUMNS")
    # Other activities will use the base implementation and SQL

# In your application setup (e.g., my_app/main.py):
# ... import CustomSQLActivitiesFromFile, CustomSQLClient, CustomSQLHandler ...

activities_instance = CustomSQLActivitiesFromFile(
    sql_client_class=CustomSQLClient,
    handler_class=CustomSQLHandler
)

# ... register with worker as before ...
```

### 3. Overriding Activity Logic

If you need to change the fundamental behavior of an activity (not just its SQL query), override the corresponding method in your subclass. Ensure you decorate it appropriately.

```python
# In your application's activities file (e.g., my_activities.py)
from typing import Any, Dict
from temporalio import activity
from application_sdk.activities.metadata_extraction.sql import BaseSQLMetadataExtractionActivities
from application_sdk.activities.common.utils import auto_heartbeater
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger

class AdvancedSQLActivities(BaseSQLMetadataExtractionActivities):

    @activity.defn
    @auto_heartbeater # Important for long-running activities
    async def fetch_tables(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Starting custom fetch_tables logic")
        state = await self._get_state(workflow_args)
        # Access handler via state.handler
        # Implement completely custom logic, maybe call handler differently
        custom_result = await state.handler.execute_custom_table_fetch(workflow_args)
        logger.info("Finished custom fetch_tables logic")
        # Ensure the return format matches what the workflow expects (often ActivityStatistics)
        return custom_result

    # Other activities (fetch_schemas, fetch_columns, etc.) inherit from base

# In your application setup:
# ... import AdvancedSQLActivities, CustomSQLClient, CustomSQLHandler ...

activities_instance = AdvancedSQLActivities(
    sql_client_class=CustomSQLClient,
    handler_class=CustomSQLHandler
)

# ... register with worker as before ...

```

### 4. Adding New Activities

To add entirely new steps to the process, define new methods decorated with `@activity.defn` in your custom activities class. You will also need to modify the associated workflow to include this new activity in its execution sequence (usually by overriding the workflow's `get_activities` static method).

```python
# In your application's activities file (e.g., my_activities.py)
# ... (imports as above) ...

class ExtendedSQLActivities(BaseSQLMetadataExtractionActivities):
    # ... potentially override existing activities or SQL ...

    @activity.defn
    @auto_heartbeater
    async def perform_custom_validation(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Performing custom validation step")
        state = await self._get_state(workflow_args)
        validation_results = await state.handler.validate_metadata(workflow_args)
        logger.info("Custom validation complete")
        return {"validation_status": "success" if validation_results else "failed"}

# In your application's workflow file (e.g., my_workflow.py)
from typing import Any, Callable, Sequence, cast, Type
from temporalio import workflow # Added import
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow
from application_sdk.activities import ActivitiesInterface
# Import your custom activities class
from .my_activities import ExtendedSQLActivities # Adjust import path if needed

@workflow.defn
class ExtendedSQLWorkflow(BaseSQLMetadataExtractionWorkflow):
    # Point to your custom activities class
    activities_cls: Type[ActivitiesInterface] = ExtendedSQLActivities

    @staticmethod
    def get_activities(
        activities: ActivitiesInterface,
    ) -> Sequence[Callable[..., Any]]:
        # Cast to your specific activities type
        sql_activities = cast(ExtendedSQLActivities, activities)
        # Get the base activities list using the superclass method correctly
        base_activities_list = BaseSQLMetadataExtractionWorkflow.get_activities(activities) # Use the base directly

        # Example: Insert custom activity before transform_data
        # Find the index of transform_data (assuming it exists)
        transform_index = -1
        for i, act in enumerate(base_activities_list):
             # Check function name (this might be fragile if methods are wrapped)
             if hasattr(act, '__name__') and act.__name__ == 'transform_data':
                 transform_index = i
                 break

        if transform_index != -1:
            # Insert custom activity before transform_data
            return list(base_activities_list[:transform_index]) + \
                   [sql_activities.perform_custom_validation] + \
                   list(base_activities_list[transform_index:])
        else:
            # Fallback: append if transform_data wasn't found (or handle error)
             return list(base_activities_list) + [sql_activities.perform_custom_validation]


    # Potentially override the 'run' method if more complex orchestration is needed

# In your application setup:
# ... import ExtendedSQLActivities, CustomSQLClient, CustomSQLHandler, ExtendedSQLWorkflow ...
from application_sdk.worker import Worker # Added import
# Assume workflow_client is already defined/imported

activities_instance = ExtendedSQLActivities(
    sql_client_class=CustomSQLClient,
    handler_class=CustomSQLHandler
)

worker = Worker(
    workflow_client=workflow_client,
    workflow_classes=[ExtendedSQLWorkflow], # Use your extended workflow
    # Use the get_activities from your extended workflow
    workflow_activities=ExtendedSQLWorkflow.get_activities(activities_instance),
)
# ... rest of worker setup ...
```

## Utilities

*   **`@auto_heartbeater`**: Decorator found in `application_sdk.activities.common.utils`. Simplifies Temporal activity heartbeating. Apply it to long-running activity methods.
*   **`get_workflow_id()`**: Function found in `application_sdk.activities.common.utils`. Retrieves the unique ID of the current workflow execution from within an activity context. Used internally by `ActivitiesInterface` for state management.

## State Explained

*   **Why State?** In Temporal, activity functions are potentially executed on different worker processes, even within the same workflow run. They don't share memory like standard class instances. However, activities within a single workflow execution often need access to shared information (like configuration, credentials, or initialized clients/handlers).
*   **How it Works:** The `ActivitiesInterface` base class implements a state management pattern. When an activity runs, it calls `_get_state(workflow_args)`. This method uses the unique `workflow_id` (obtained via `get_workflow_id()`) to look up or create an `ActivitiesState` object in the `_state` dictionary held by the *activity worker's instance* of the `ActivitiesInterface` subclass.
*   **What it Holds:** The `ActivitiesState` object stores the `workflow_args` and, crucially, the initialized `handler` instance. This allows different activities in the same workflow run *on the same worker process* to access the *same* handler instance and configuration without needing to re-initialize it for every activity call.
*   **Lifecycle:** State is typically initialized during the first activity call (often `preflight_check`) for a given workflow run on that worker. It's cleaned up (`_clean_state`) if errors occur during state retrieval or potentially at the end of a workflow (though explicit cleanup activities are less common).
*   **Important Note:** This state is local to the specific worker process handling the activities for that workflow run. If an activity execution is retried on a different worker, the state will be re-initialized on that new worker. It's *not* persisted in Temporal itself; only the activity inputs/outputs are.