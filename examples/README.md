# Atlan Sample Applications

This folder contains sample applications that demonstrate how to use the Atlan SDK to build applications on the Atlan Platform.

## Example Applications

| Example Script | Description |
|---------------|-------------|
| [application_sql.py](./application_sql.py) | SQL workflow for extracting metadata from a PostgreSQL database. |
| [application_sql_with_custom_transformer.py](./application_sql_with_custom_transformer.py) | SQL workflow with a custom transformer for database entities. Demonstrates advanced metadata extraction and transformation. |
| [application_sql_miner.py](./application_sql_miner.py) | SQL Miner workflow for extracting query metadata from a Snowflake database. |
| [application_hello_world.py](./application_hello_world.py) | Minimal "Hello World" workflow using the Atlan SDK and Temporal. |
| [application_fastapi.py](./application_fastapi.py) | Example of exposing workflow operations via a FastAPI server. |
| [application_custom_fastapi.py](./application_custom_fastapi.py) | FastAPI server with custom routes and workflow integration. |
| [application_subscriber.py](./application_subscriber.py) | Demonstrates event-driven workflow execution using event triggers and subscriptions. |
| [run_examples.py](./run_examples.py) | Utility to run and monitor all example workflows, outputting results to a markdown file. |

---

## 1. Setup Your Environment

Before running any examples, you must set up your development environment. Please follow the OS-specific setup guide:

- [Setup for macOS](../docs/docs/setup/MAC.md)
- [Setup for Linux](../docs/docs/setup/LINUX.md)
- [Setup for Windows](../docs/docs/setup/WINDOWS.md)

---

## 2. Running Examples

Once your environment is set up:

1. Run `uv run poe start-deps` to start the Dapr runtime and Temporal server
2. Run the example using `uv run <example_script.py>` or use the VSCode launch configuration provided below.

> **Warning:**
> Example scripts use default credentials (e.g., `password`, `postgres`). **Never use these defaults in production.** Always set secure environment variables for real deployments.

### Run and Debug examples via VSCode or Cursor

- Install the [Debugpy](https://github.com/microsoft/debugpy) extension for VSCode or Cursor.
- Update the `.vscode/launch.json` file with the appropriate program and environment variables.
- Run the configurations available in the `.vscode/launch.json` file.

> [!NOTE]
> The `PYTHONPATH` is set to the `.venv/bin/python` path. If you are using a different Python path, you can update the `PYTHONPATH` environment variable in the `.vscode/launch.json` file.
