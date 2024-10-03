# Atlan Sample Applications

This folder contains sample applications that demonstrate how to use the Atlan SDK to build applications on the Atlan Platform.

## Requirements
- Install [Dapr CLI](https://docs.dapr.io/getting-started/install-dapr-cli/)

## Dapr PaaS Components
1. State store - Uses SQLite as the state store at `/tmp/dapr/statestore.db`
2. Object store - Uses local file system at `/tmp/dapr/objectstore`
3. Secret store - Uses system environment variables
4. Event store - Uses in-memory event store

## Running examples
1. Create virtual environment using `virtualenv .venv`
2. Activate the virtual environment using `source .venv/bin/activate`
3. Install the dependencies using `poetry install --group examples`
4. Make sure you start the Dapr runtime before running the examples `dapr run --app-id app --app-port 3000 --dapr-http-port 3500 --dapr-grpc-port 50001 --dapr-http-max-request-size 1024 --resources-path ./components`
5. cd into the example directory `cd examples`
6. Start the temporal server `temporal server start-dev`
7. You can now run any of the examples using `python <example>.py`

### Run and Debug examples via VSCode or Cursor
1. Add the following settings to the `.vscode/launch.json` file, configure the program and the environment variables and run the configuration
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
                "POSTGRES_DATABASE": "postgres",
            }
        },
        {
            "name": "Python: Debug Tests",
            "type": "debugpy",
            "request": "launch",
            "program": "${workspaceFolder}/.venv/bin/pytest",
            "args": [
                "-v",
            ],
            "cwd": "${workspaceFolder}/tests/unit/paas",
            "env": {
                "PYTHONPATH": "${workspaceFolder}"
            },
        },
    ]
}
```
- You can navigate to the Run and Debug section in the IDE to run the configurations of your choice.


## Examples
1. [SQL Application](./application_sql.py) - Demonstrates how to build a SQL application using the Atlan SDK
