# Step-by-Step Guide to Create an SQL Application

This guide will walk you through the process of creating an SQL application using the Atlan Platform SDK, based on the example provided in [`examples/application_sql.py`](../examples/application_sql.py). We'll cover each component and explain how you can extend them for your specific needs.

When we say "SQL application" - we mean an application that interacts with a SQL database, extracts metadata from the database and stores it in the configured object store. The use-case can further be extended by adding more activities to the workflow.

At a high level, the process of creating an SQL application involves the following steps:
1. Setting up all the configurations required to interact with the database.
2. Setting up the Workflow Worker i.e the logic that will be executed when the workflow is run.
3. Building the Workflow i.e the order in which the activities will be executed.

## Prerequisites

Before you begin, make sure you have:
- Installed the Atlan Platform SDK
- Set up your development environment with Python 3.11 or above
- Familiarized yourself with the basics of SQL and database concepts

# Table of Contents
1. [Setting up all the configurations required to interact with the database.](#1-setting-up-all-the-configurations-required-to-interact-with-the-database)
2. [Setting up the Workflow Worker](#2-setting-up-the-workflow-worker)
3. [Building the Workflow](#3-building-the-workflow)


## 1. Setting up all the configurations required to interact with the database.

### Defining the `SQLWorkflowMetadataInterface` class

The `SQLWorkflowMetadataInterface` class is used to fetch metadata from the database. The `fetch_metadata` method is used to fetch the metadata from the database and powers the `GET /v2/metadata` API endpoint.

Create a class that inherits from `SQLWorkflowMetadataInterface` to define the metadata extraction queries:
```python
from application_sdk.workflows.sql.metadata import SQLWorkflowMetadataInterface
class YourSQLWorkflowMetadata(SQLWorkflowMetadataInterface):
    METADATA_SQL = """
    SELECT schema_name, catalog_name
    FROM INFORMATION_SCHEMA.SCHEMATA;
    """
```

### Defining the `SQLWorkflowPreflightInterface` class

The `SQLWorkflowPreflightInterface` class is used to perform preflight checks on the data. The `preflight_checks` method is used to perform preflight checks on the data and powers the `GET /v2/preflight` API endpoint.

Create a class that inherits from `SQLWorkflowPreflightInterface` to define the preflight checks:
```python
from application_sdk.workflows.sql.preflight import SQLWorkflowPreflightInterface
class YourSQLWorkflowPreflight(SQLWorkflowPreflightInterface):
    PREFLIGHT_SQL = """
    SELECT COUNT(*)
    FROM your_table;
    """
    TABLES_CHECK_SQL = """
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.TABLES;
    """

    def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # Your preflight check logic here
        return {"status": "success", "message": "Preflight check completed successfully"}
```

### Defining the `SQLWorkflowAuthInterface` class

The `SQLWorkflowAuthInterface` class is used to authenticate the SQL workflow. The `test_auth` method is used to test the authentication credentials.

Create a class that inherits from `SQLWorkflowAuthInterface` to define the authentication logic:
```python
from application_sdk.workflows.sql.auth import SQLWorkflowAuthInterface
class YourSQLWorkflowAuth(SQLWorkflowAuthInterface):
    def __init__(self, create_engine_fn: Callable[[Dict[str, Any]], Engine]):
        super().__init__(create_engine_fn)

    def test_auth(self, credential: Dict[str, Any]) -> bool:
        # Your authentication logic here
        return True
```

## 2. Setting up the Workflow Worker

### Defining the `SQLWorkflowWorkerInterface` class

The `SQLWorkflowWorkerInterface` class is used to execute the workflow. The `execute_workflow` method is used to execute the workflow.

Create a class that inherits from `SQLWorkflowWorkerInterface` to define the workflow worker:
```python
from application_sdk.workflows.sql.worker import SQLWorkflowWorkerInterface
class YourSQLWorkflowWorker(SQLWorkflowWorkerInterface):
    def __init__(self, metadata: SQLWorkflowMetadataInterface, preflight: SQLWorkflowPreflightInterface):
        super().__init__(metadata, preflight)

    def execute_workflow(self, **kwargs) -> Dict[str, Any]:
        # Your workflow logic here
        return {"status": "success", "message": "Workflow executed successfully"}
```

## 3. Building the Workflow

### Defining the `SQLWorkflowBuilderInterface` class

The `SQLWorkflowBuilderInterface` class is used to build the workflow. The `build_workflow` method is used to build the workflow.

Create a class that inherits from `SQLWorkflowBuilderInterface` to build the workflow:
```python
```