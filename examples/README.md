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

1. Add the following settings to the `.vscode/launch.json` file, configure the program and the environment variables, and run the configuration:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Run SQL Connector",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/examples/application_sql.py",
      "cwd": "${workspaceFolder}",
      "justMyCode": false,
      "env": {
        "PYTHONPATH": "${workspaceFolder}",
        "POSTGRES_HOST": "host",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "password",
        "POSTGRES_DATABASE": "postgres"
      }
    },
    {
      "name": "Python: Debug Tests",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/.venv/bin/pytest",
      "args": ["-v"],
      "cwd": "${workspaceFolder}/tests/unit/paas",
      "env": {
        "PYTHONPATH": "${workspaceFolder}"
      }
    }
  ]
}
```

- You can navigate to the Run and Debug section in the IDE to run the configurations of your choice.

> **Need help?** If you encounter any issues during setup, reach out on Slack (#pod-app-framework) or email connect@atlan.com.