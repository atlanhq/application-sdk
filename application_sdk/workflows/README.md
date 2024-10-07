# Workflows

This module enables developers to build workflows that can be executed on the Atlan Platform.

## Table of contents
- [Basic Workflow Builder](#basic-workflow-builder)
    - [`WorkflowAuthInterface`](#workflowauthinterface)
    - [`WorkflowMetadataInterface`](#workflowmetadatainterface)
    - [`WorkflowPreflightCheckInterface`](#workflowpreflightcheckinterface)
    - [`WorkflowWorkerInterface`](#workflowworkerinterface)
    - [`WorkflowBuilderInterface`](#workflowbuilderinterface)
- [SQL](./sql/README.md)
- [Transformers](./transformers/README.md)

## Basic Workflow Builder

### `WorkflowAuthInterface`
This class is used to handle the `Test Auth` feature.
- This is automatically linked to the default UI interface and the `test-auth` widget.

You can create a custom auth interface by extending this class and overriding the `test_auth` method.


### `WorkflowMetadataInterface`
This class is used to handle the `Include/Exclude` feature.
- This is automatically linked to the default UI interface and the `include-filter` and `exclude-filter` widget.

You can create a custom metadata interface by extending this class and overriding the `fetch_metadata` method.


### `WorkflowPreflightCheckInterface`
This class is used to handle the `Preflight Check` feature.
- This is automatically linked to the default UI interface and the `preflight-check` widget.

You can create a custom preflight check interface by extending this class and overriding the `preflight_check` method.


### `WorkflowWorkerInterface`
This class provides a default implementation for the workflow, with hooks for subclasses to customize specific behaviors.


### `WorkflowBuilderInterface`
Base class for workflow builder interfaces